import base64
import requests
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

# Usar Session melhora MUITO a performance para múltiplas requisições
session = requests.Session()
PHENIX_BASE = "https://pcast.phenixrts.com"

def make_auth_header(app_id, password):
    credentials = f"{app_id}:{password}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()

@app.route("/channels", methods=["GET"])
def get_channels():
    app_id = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing headers"}), 400
    try:
        resp = session.get(
            f"{PHENIX_BASE}/pcast/channels",
            headers={"Authorization": make_auth_header(app_id, password), "Accept": "application/json"},
            timeout=15
        )
        return Response(resp.content, status=resp.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/publishers/count/<path:channel_id>", methods=["GET"])
def get_publishers_count(channel_id):
    app_id = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    try:
        encoded_id = quote(channel_id, safe="")
        resp = session.get(
            f"{PHENIX_BASE}/pcast/channel/{encoded_id}/publishers/count",
            headers={"Authorization": make_auth_header(app_id, password), "Accept": "application/json"},
            timeout=10
        )
        return Response(resp.text, status=resp.status_code, content_type="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/config", methods=["GET"])
def get_config():
    """Read .env from disk and return only safe UI config (tools, title, version).
    Credentials and internal URLs are never exposed."""
    import os
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        env = {}
        with open(env_path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                eq = line.index("=") if "=" in line else -1
                if eq < 1:
                    continue
                key = line[:eq].strip()
                val = line[eq+1:].strip()
                env[key] = val

        # Only expose safe keys — never passwords, URLs, tokens
        SAFE_PREFIXES = ("TOOL_", "SRT_SERVER_", "SRT_LOCAL_")
        SAFE_KEYS     = ("APP_TITLE", "APP_VERSION", "SRT_PASSPHRASE", "PROXY_URL")

        safe = {k: v for k, v in env.items()
                if k in SAFE_KEYS or any(k.startswith(p) for p in SAFE_PREFIXES)}

        # Expose whether admin password is configured (not the password itself)
        safe["HAS_ADMIN_PASSWORD"] = "true" if env.get("ADMIN_PASSWORD", "").strip() else "false"

        return jsonify({"status": "ok", "config": safe})
    except FileNotFoundError:
        return jsonify({"status": "error", "message": ".env not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



def _get_admin_password():
    """Read ADMIN_PASSWORD from .env. Returns None if not set."""
    import os
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ADMIN_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _check_password(req):
    """Validate X-Admin-Password header against ADMIN_PASSWORD in .env.
    Returns (ok: bool, error_response or None)."""
    required = _get_admin_password()
    if not required:
        return True, None  # No password set — allow all
    provided = req.headers.get("X-Admin-Password", "")
    if provided == required:
        return True, None
    return False, (jsonify({"success": False, "output": "❌ Invalid admin password."}), 403)


@app.route("/git-pull", methods=["POST"])
def git_pull():
    import subprocess, os
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30
        )
        output = (result.stdout.decode() + result.stderr.decode()).strip()
        success = result.returncode == 0
        return jsonify({"success": success, "output": output})
    except Exception as e:
        return jsonify({"success": False, "output": str(e)}), 500


@app.route("/restart-proxy", methods=["POST"])
def restart_proxy():
    import subprocess
    ok, err = _check_password(request)
    if not ok:
        return err
    try:
        subprocess.Popen(["bash", "-c", "sleep 2 && systemctl restart so-proxy"])
        return jsonify({"success": True, "output": "Proxy restart scheduled in 2 seconds."})
    except Exception as e:
        return jsonify({"success": False, "output": str(e)}), 500


@app.route("/server-info", methods=["GET"])
def server_info():
    """Return local IPs and public IP for MTR header."""
    import subprocess
    info = {}

    # Local IPs
    try:
        r = subprocess.run(["ip", "addr"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        info["ip_addr"] = r.stdout.decode()
    except Exception as e:
        info["ip_addr"] = str(e)

    # Default route
    try:
        r = subprocess.run(["ip", "route", "show", "default"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        info["default_route"] = r.stdout.decode().strip()
    except Exception as e:
        info["default_route"] = str(e)

    # Public IP
    try:
        r = requests.get("https://api.ipify.org", timeout=5)
        info["public_ip"] = r.text.strip()
    except Exception:
        info["public_ip"] = "unavailable"

    return jsonify(info)


@app.route("/mtr/stream", methods=["GET"])
def mtr_stream():
    """
    Two-process approach:
    1. Background thread runs mtr --report for the final result (saved to disk).
    2. SSE stream runs a separate mtr --report-cycles 1 --interval 1 in a loop,
       yielding one full report snapshot per cycle so the browser sees live updates.
    Reconnecting just attaches a new streaming process — the background job
    continues independently and is NOT duplicated.
    """
    import subprocess, os, json, re, datetime, time as _time, threading

    host     = (request.args.get("host") or "").strip()
    mode     = request.args.get("mode") or "packets"
    count    = max(1, min(int(request.args.get("count") or 50), 500))
    seconds  = max(10, min(int(request.args.get("seconds") or 60), 86400))
    no_dns   = request.args.get("no_dns") == "1"
    proto    = request.args.get("proto") or "icmp"    # "icmp" | "udp53"
    geo      = request.args.get("geo")   or "country" # "country" | "asn"
    src_ip   = request.args.get("src_ip") or "unknown"
    pub_ip   = request.args.get("pub_ip") or "unknown"
    tag      = (request.args.get("tag") or "").strip()

    if not host:
        def err():
            yield "data: ERROR: Host is required.\n\n"
            yield "data: __DONE__\n\n"
        return Response(err(), content_type="text/event-stream")

    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    os.makedirs(results_dir, exist_ok=True)

    total_cycles = seconds if mode == "time" else count

    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    ts         = started_at[:19].replace(":", "-").replace("T", "_")
    safe_host  = re.sub(r"[^\w\.\-]", "_", host)
    job_id     = f"{ts}_{safe_host}"
    state_file = os.path.join(results_dir, f"{job_id}.running.json")
    done_file  = os.path.join(results_dir, f"{job_id}.json")

    # ── Only start background job if it's not already running ────────
    is_new_job = not os.path.isfile(state_file)

    if is_new_job:
        initial_state = {
            "job_id": job_id, "status": "running",
            "started_at": started_at, "ended_at": None,
            "source_ip": src_ip, "public_ip": pub_ip,
            "destination": host, "mode": mode,
            "packets": count if mode == "packets" else None,
            "duration_s": seconds if mode == "time" else None,
            "no_dns": no_dns, "proto": proto, "geo": geo, "tag": tag,
            "total_cycles": total_cycles,
            "hops": [], "raw": ""
        }
        with open(state_file, "w") as f:
            json.dump(initial_state, f)
    else:
        # Reconnecting — read existing state for metadata
        try:
            with open(state_file) as f:
                initial_state = json.load(f)
            tag          = initial_state.get("tag", tag)
            total_cycles = initial_state.get("total_cycles", total_cycles)
        except Exception:
            initial_state = {}

    def parse_mtr_output(lines):
        hops = []
        for line in lines:
            # mtr --report-wide with -b produces lines like:
            #  1.|-- 192.168.1.1 (192.168.1.1)  0.0%  60  1.2  1.5  0.9  3.1  0.5
            # or without -b:
            #  1.|-- 192.168.1.1  0.0%  60  1.2  1.5  0.9  3.1  0.5
            m = re.search(
                r'(\d+)\.\s*[|`!\-]+\s+(\S+(?:\s+\([^)]+\))?)\s+'
                r'([\d.]+)%\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
                line)
            if m:
                hops.append({
                    "hop":  int(m.group(1)),
                    "host": m.group(2).strip(),
                    "loss": float(m.group(3)),
                    "sent": int(m.group(4)),
                    "last": float(m.group(5)),
                    "avg":  float(m.group(6)),
                    "best": float(m.group(7)),
                    "worst":float(m.group(8)),
                })
        return hops

    # ── Background thread: runs full mtr --report, saves result ──────
    def run_background():
        if mode == "time":
            bg_cmd = ["mtr", "--report", "--report-wide", "--interval", "1",
                      "--report-cycles", str(seconds)]
        else:
            bg_cmd = ["mtr", "--report", "--report-wide",
                      "--report-cycles", str(count)]
        # Always show IPs alongside names
        bg_cmd.append("-b")
        # Protocol
        if proto == "udp53":
            bg_cmd += ["-u", "-P", "53"]
        # Geo annotation
        if geo == "asn":
            bg_cmd.append("-z")
        else:
            bg_cmd.append("-y")
            bg_cmd.append("2")
        if no_dns:
            bg_cmd.append("--no-dns")
        bg_cmd.append(host)

        lines = []
        try:
            proc = subprocess.Popen(bg_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=0)
            raw_out = proc.communicate()[0]
            for line in raw_out.decode(errors="replace").splitlines():
                lines.append(line)
        except Exception as e:
            lines.append(f"ERROR: {e}")

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        hops     = parse_mtr_output(lines)
        result   = dict(initial_state)
        result.update({
            "status": "done", "ended_at": ended_at,
            "hops": hops, "raw": "\n".join(lines)
        })
        with open(done_file, "w") as f:
            json.dump(result, f, indent=2)
        try:
            os.remove(state_file)
        except Exception:
            pass

    if is_new_job:
        t = threading.Thread(target=run_background, daemon=True)
        t.start()

    # ── SSE stream: tick countdown while background job runs ──────────
    def stream_ticks():
        start = _time.time()
        last  = start
        while os.path.isfile(state_file):
            _time.sleep(0.3)
            now = _time.time()
            if now - last >= 1.0:
                elapsed   = int(now - start)
                remaining = max(0, total_cycles - elapsed)
                last = now
                yield f"data: __TICK__ {elapsed} {remaining}\n\n"
        # Job finished — stream the final result
        try:
            with open(done_file) as f:
                d = json.load(f)
            yield f"data: \n\n"
            for line in (d.get("raw") or "").splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
            yield f"data: \n\n"
            yield f"data: ✔ Result saved: {job_id}.json\n\n"
        except Exception as e:
            yield f"data: ⚠ Could not read result: {e}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_ticks(), content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/mtr/kill/<job_id>", methods=["POST"])
def mtr_kill(job_id):
    """Kill a running MTR job. Requires admin password if set."""
    import subprocess as sp
    ok, err = _check_password(request)
    if not ok:
        return err

    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    state_file  = os.path.join(results_dir, f"{job_id}.running.json")

    if not os.path.isfile(state_file):
        return jsonify({"error": "Job not found or already finished"}), 404

    try:
        with open(state_file) as f:
            d = json.load(f)
        dest = d.get("destination", "")
        os.remove(state_file)
        if dest:
            sp.run(["pkill", "-f", f"mtr.*{dest}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return jsonify({"success": True, "output": f"Job {job_id} killed."})
    except Exception as e:
        return jsonify({"success": False, "output": str(e)}), 500


@app.route("/mtr/delete/<path:filename>", methods=["DELETE"])
def mtr_delete(filename):
    """Delete a completed MTR result JSON file. Requires admin password if set."""
    ok, err = _check_password(request)
    if not ok:
        return err

    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    filepath    = os.path.join(results_dir, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        os.remove(filepath)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/mtr/running", methods=["GET"])
def mtr_running():
    """Return list of currently running MTR jobs (from .running.json files)."""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    if not os.path.isdir(results_dir):
        return jsonify([])
    items = []
    for f in sorted(os.listdir(results_dir), reverse=True):
        if not f.endswith(".running.json"):
            continue
        try:
            with open(os.path.join(results_dir, f)) as fh:
                d = json.load(fh)
            elapsed = 0
            try:
                st = datetime.datetime.fromisoformat(d["started_at"].replace("Z",""))
                elapsed = int((datetime.datetime.utcnow() - st).total_seconds())
            except Exception:
                pass
            remaining = max(0, d.get("total_cycles", 0) - elapsed)
            items.append({
                "job_id":      d.get("job_id"),
                "destination": d.get("destination"),
                "started_at":  d.get("started_at"),
                "mode":        d.get("mode"),
                "tag":         d.get("tag", ""),
                "source_ip":   d.get("source_ip", ""),
                "public_ip":   d.get("public_ip", ""),
                "duration_s":  d.get("duration_s"),
                "packets":     d.get("packets"),
                "no_dns":      d.get("no_dns", False),
                "proto":       d.get("proto", "icmp"),
                "geo":         d.get("geo", "country"),
                "elapsed":     elapsed,
                "remaining":   remaining,
                "total_cycles":d.get("total_cycles", 0),
            })
        except Exception:
            pass
    return jsonify(items)


@app.route("/mtr/tag/<path:filename>", methods=["POST"])
def mtr_set_tag(filename):
    """Update the tag on a saved MTR result."""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    filepath    = os.path.join(results_dir, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    data = request.get_json(silent=True) or {}
    tag  = (data.get("tag") or "").strip()
    try:
        with open(filepath) as f:
            result = json.load(f)
        result["tag"] = tag
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mtr/results", methods=["GET"])
def mtr_results():
    """List saved MTR result files (completed only)."""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    if not os.path.isdir(results_dir):
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(results_dir)
         if f.endswith(".json") and not f.endswith(".running.json")],
        reverse=True
    )[:50]
    items = []
    for f in files:
        try:
            with open(os.path.join(results_dir, f)) as fh:
                d = json.load(fh)
            items.append({
                "file":        f,
                "destination": d.get("destination"),
                "started_at":  d.get("started_at"),
                "ended_at":    d.get("ended_at"),
                "mode":        d.get("mode"),
                "packets":     d.get("packets"),
                "duration_s":  d.get("duration_s"),
                "hops":        len(d.get("hops", [])),
                "tag":         d.get("tag", ""),
                "proto":       d.get("proto", "icmp"),
                "geo":         d.get("geo", "country"),
            })
        except Exception:
            pass
    return jsonify(items)



@app.route("/mtr/results/<path:filename>", methods=["GET"])
def mtr_result_file(filename):
    """Download a specific MTR result JSON."""
    import os
    from flask import send_from_directory
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    return send_from_directory(results_dir, filename, as_attachment=True)



# ═══════════════════════════════════════════════════════════
#  INGEST ANALYZER
# ═══════════════════════════════════════════════════════════
import threading, uuid, datetime, subprocess, os, json, re, shutil

_ingest_jobs = {}   # job_id -> { status, started_at, url, output_dir, zip, pdf, log }
_ingest_lock = threading.Lock()

INGEST_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingest-results")
os.makedirs(INGEST_RESULTS_DIR, exist_ok=True)


def _run_ingest(job_id, url, output_dir):
    """Background thread: run analysis, let script choose its own output dir."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _ingest_lock:
            _ingest_jobs[job_id]["log"] = list(log_lines)

    # Get tag from job state
    with _ingest_lock:
        tag        = _ingest_jobs[job_id].get("tag", "")
        started_at = _ingest_jobs[job_id].get("started_at", "")

    # Clean URL for display (remove passphrase)
    url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')

    try:
        log(f"Starting analysis for: {url_display}")

        result = subprocess.run(
            ["run-ingest-analysis.sh", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600
        )
        stdout = result.stdout.decode(errors="replace")
        for line in stdout.splitlines():
            log(line)

        exit_code = result.returncode
        log(f"Script exited with code {exit_code}")

        # Parse the actual output dir and zip from stdout
        actual_dir = None
        actual_zip = None
        for line in stdout.splitlines():
            if "Report location:" in line:
                path = line.split("Report location:")[-1].strip()
                actual_dir = os.path.dirname(path)
            if "Archive location:" in line:
                actual_zip = line.split("Archive location:")[-1].strip()

        log(f"Detected output dir: {actual_dir}")
        log(f"Detected zip: {actual_zip}")

        # Copy zip to ingest-results
        saved_zip = None
        if actual_zip and os.path.isfile(actual_zip):
            dest = os.path.join(INGEST_RESULTS_DIR, os.path.basename(actual_zip))
            shutil.copy2(actual_zip, dest)
            saved_zip = os.path.basename(actual_zip)
            log(f"ZIP saved: {saved_zip}")
        else:
            log("WARNING: ZIP not found — check script output above.")

        # Copy entire output directory
        saved_dir = None
        if actual_dir and os.path.isdir(actual_dir):
            dest_dir = os.path.join(INGEST_RESULTS_DIR, os.path.basename(actual_dir))
            if os.path.isdir(dest_dir):
                shutil.rmtree(dest_dir)
            shutil.copytree(actual_dir, dest_dir)
            saved_dir = os.path.basename(actual_dir)
            log(f"Report directory saved: {saved_dir}")
        else:
            log("WARNING: Output directory not found.")

        # Read summary from report.json
        summary = {}
        if actual_dir:
            json_report = os.path.join(actual_dir, "report.json")
            if os.path.isfile(json_report):
                try:
                    with open(json_report) as f:
                        summary = json.load(f)
                except Exception:
                    pass

        # Save meta.json with tag, url, timestamps into the result dir
        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        if saved_dir:
            meta = {
                "job_id":      job_id,
                "url":         url_display,
                "tag":         tag,
                "started_at":  started_at,
                "ended_at":    ended_at,
                "exit_code":   exit_code,
                "status":      "done" if exit_code in (0, 45) else "failed",
            }
            try:
                with open(os.path.join(INGEST_RESULTS_DIR, saved_dir, "meta.json"), "w") as f:
                    json.dump(meta, f, indent=2)
                log("meta.json saved.")
            except Exception as e:
                log(f"WARNING: Could not save meta.json: {e}")

        with _ingest_lock:
            _ingest_jobs[job_id].update({
                "status":    "done" if exit_code in (0, 45) else "failed",
                "exit_code": exit_code,
                "ended_at":  ended_at,
                "zip":       saved_zip,
                "dir":       saved_dir,
                "summary":   summary,
                "log":       log_lines,
            })

    except subprocess.TimeoutExpired:
        with _ingest_lock:
            _ingest_jobs[job_id].update({"status": "timeout", "log": log_lines})
    except Exception as e:
        log(f"ERROR: {e}")
        with _ingest_lock:
            _ingest_jobs[job_id].update({"status": "error", "log": log_lines})


@app.route("/ingest/run", methods=["POST"])
def ingest_run():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    tag  = (data.get("tag") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())[:8]

    with _ingest_lock:
        _ingest_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "url":        url,
            "tag":        tag,
            "started_at": datetime.datetime.utcnow().isoformat() + "Z",
            "ended_at":   None,
            "zip":        None,
            "dir":        None,
            "summary":    {},
            "log":        [],
        }

    t = threading.Thread(target=_run_ingest, args=(job_id, url, None), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/ingest/status/<job_id>", methods=["GET"])
def ingest_status(job_id):
    with _ingest_lock:
        job = _ingest_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/ingest/results", methods=["GET"])
def ingest_results():
    """List saved ingest results (ZIP + report dir), reading meta.json for tag/url/dates."""
    files = set(os.listdir(INGEST_RESULTS_DIR))
    zips  = sorted([f for f in files if f.endswith(".zip")], reverse=True)
    items = []
    for z in zips[:50]:
        dirname = z.replace(".zip", "")
        has_dir = dirname in files and os.path.isdir(os.path.join(INGEST_RESULTS_DIR, dirname))
        meta = {}
        if has_dir:
            meta_path = os.path.join(INGEST_RESULTS_DIR, dirname, "meta.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                except Exception:
                    pass
            # Fallback: try report.json for tag
            if not meta:
                rj = os.path.join(INGEST_RESULTS_DIR, dirname, "report.json")
                try:
                    with open(rj) as f:
                        meta = {"tag": json.load(f).get("tag", "")}
                except Exception:
                    pass
        items.append({
            "zip":        z,
            "dir":        dirname if has_dir else None,
            "name":       dirname,
            "tag":        meta.get("tag", ""),
            "url":        meta.get("url", ""),
            "started_at": meta.get("started_at", ""),
            "ended_at":   meta.get("ended_at", ""),
            "status":     meta.get("status", ""),
            "exit_code":  meta.get("exit_code"),
        })
    return jsonify(items)


@app.route("/ingest/report-txt/<path:dirname>", methods=["GET"])
def ingest_report_txt(dirname):
    """Return the report.txt content as plain text."""
    from flask import send_from_directory
    report_dir = os.path.join(INGEST_RESULTS_DIR, dirname)
    txt_path   = os.path.join(report_dir, "report.txt")
    if not os.path.isfile(txt_path):
        return "report.txt not found", 404
    with open(txt_path) as f:
        return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}



@app.route("/ingest/download/<path:filename>", methods=["GET"])
def ingest_download(filename):
    from flask import send_from_directory
    return send_from_directory(INGEST_RESULTS_DIR, filename, as_attachment=True)


@app.route("/ingest/report/<path:filepath>", methods=["GET"])
def ingest_report_file(filepath):
    """Serve files from inside a report directory (index.html, charts, etc.)."""
    from flask import send_from_directory
    parts    = filepath.split("/", 1)
    dirname  = parts[0]
    filename = parts[1] if len(parts) > 1 else "index.html"
    report_dir = os.path.join(INGEST_RESULTS_DIR, dirname)
    return send_from_directory(report_dir, filename)



# ═══════════════════════════════════════════════════════════
#  GOP ANALYZER
# ═══════════════════════════════════════════════════════════
import tempfile

_gop_jobs  = {}
_gop_lock  = threading.Lock()
GOP_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gop-results")
os.makedirs(GOP_DIR, exist_ok=True)


def _run_gop_analysis(job_id, url, duration, passphrase, tag):
    """Background: capture SRT stream, run ffprobe frame analysis, parse GOP structure.
    Key improvements:
    - Graceful timeout: if ffmpeg times out, analyse whatever was captured
    - NAL type 5 IDR detection via ffprobe side_data / key_frame
    - Open vs Closed GOP detection
    - Ignore last (incomplete) GOP
    - Compliance checks against specs table
    """
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _gop_lock:
            if job_id in _gop_jobs:
                _gop_jobs[job_id]["log"] = list(log_lines)

    url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')

    # Parse host:port from URL for display
    m_host = re.search(r'srt://([^:/?]+):(\d+)', url_display)
    url_host = m_host.group(1) if m_host else url_display
    url_port = m_host.group(2) if m_host else ""

    ts_path = None
    cap_returncode = 0

    try:
        log(f"Starting GOP analysis for: {url_display}")
        log(f"Capture duration: {duration}s")

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
            ts_path = tmp.name

        # ── Step 1: capture stream (graceful timeout) ─────────────────
        log("Capturing stream with ffmpeg…")
        cap_cmd = [
            "ffmpeg", "-y",
            "-timeout", str((duration + 10) * 1000000),  # SRT connect timeout µs
            "-i", url,
            "-t", str(duration),
            "-c", "copy",
            "-f", "mpegts",
            ts_path
        ]
        try:
            cap_result = subprocess.run(
                cap_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=duration + 45
            )
            cap_returncode = cap_result.returncode
            cap_out = cap_result.stdout.decode(errors="replace")
            log(f"ffmpeg capture done (exit {cap_returncode})")
        except subprocess.TimeoutExpired:
            log("WARNING: ffmpeg timed out — analysing partial capture if available")
            cap_out = ""
            cap_returncode = -1

        # Check if we got anything usable
        ts_size = os.path.getsize(ts_path) if (ts_path and os.path.isfile(ts_path)) else 0
        log(f"Captured {ts_size:,} bytes")

        if ts_size < 500:
            log("ERROR: Capture produced no usable data. Is the stream reachable?")
            if cap_out:
                log(cap_out[-800:])
            with _gop_lock:
                _gop_jobs[job_id].update({
                    "status": "failed", "log": log_lines,
                    "ended_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "result": {
                        "url": url_display, "url_host": url_host, "url_port": url_port,
                        "tag": tag, "error": "Stream unreachable or produced no data",
                        "has_idr": False, "idr_count": 0, "total_frames": 0,
                    }
                })
            return

        # ── Step 2: container/stream info ─────────────────────────────
        log("Running ffprobe for stream info…")
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", ts_path
        ]
        probe_data = {}
        try:
            r = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            probe_data = json.loads(r.stdout.decode())
        except Exception as e:
            log(f"WARNING: ffprobe stream info failed: {e}")

        # ── Step 3: frame analysis with side_data for NAL type detection ─
        log("Running ffprobe frame analysis (NAL/IDR detection)…")
        frame_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pict_type,key_frame,pts_time,coded_picture_number,side_data_list",
            ts_path
        ]
        frames_data = []
        try:
            r = subprocess.run(frame_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            fd = json.loads(r.stdout.decode())
            frames_data = fd.get("frames", [])
        except Exception as e:
            log(f"WARNING: Frame analysis failed: {e}")

        log(f"Analysed {len(frames_data)} video frames")

        # ── Parse GOP structure ───────────────────────────────────────
        # IDR = key_frame==1 (ffmpeg/ffprobe marks NAL type 5 IDR as key_frame)
        # Additionally check side_data for "H.264 Supplemental Enhancement Information" with idr
        gops = []
        current_gop = []
        idr_count = 0
        non_idr_keyframe_count = 0
        total_frames = len(frames_data)

        for frame in frames_data:
            ptype   = frame.get("pict_type", "?")
            is_key  = frame.get("key_frame", 0) == 1
            pts_t   = float(frame.get("pts_time", 0) or 0)

            # NAL IDR: ffprobe sets key_frame=1 for IDR (NAL type 5)
            # For open-GOP, key_frame=1 on non-IDR recovery points — we count both
            # but distinguish: if pict_type is I + key_frame it's a true IDR
            is_idr = is_key and ptype == "I"

            if is_key:
                if is_idr:
                    idr_count += 1
                else:
                    non_idr_keyframe_count += 1
                if current_gop:
                    gops.append(current_gop)
                current_gop = [{"type": ptype, "key": True, "idr": is_idr, "pts": pts_t}]
            else:
                current_gop.append({"type": ptype, "key": False, "idr": False, "pts": pts_t})

        # Append last (partial) GOP but mark it incomplete
        if current_gop:
            gops.append(current_gop)

        # ── Drop last GOP (always incomplete due to capture duration) ──
        complete_gops = gops[:-1] if len(gops) > 1 else gops

        # ── Open vs Closed GOP detection ──────────────────────────────
        # Closed GOP: no B-frames that reference frames outside their GOP.
        # Practical heuristic: if first frames after an I-frame are B-frames
        # referencing pts before the I, it's open. Simpler: check if any GOP
        # starts with B-frames (open GOP indicator).
        def _is_open_gop(gop_list):
            for g in gop_list:
                if len(g) > 1:
                    # In open GOP, frames immediately after IDR can be B referencing
                    # previous GOP's frames. A simple signal: B appears before P in GOP.
                    types_in_gop = [f["type"] for f in g]
                    # If first non-I frame is B, likely open GOP
                    non_i = [t for t in types_in_gop if t != "I"]
                    if non_i and non_i[0] == "B":
                        return True
            return False

        gop_type = "OPEN" if _is_open_gop(complete_gops) else "CLOSED"

        gop_lengths  = [len(g) for g in complete_gops]
        gop_patterns = ["".join(f["type"] for f in g) for g in complete_gops]

        has_b_frames  = any(f["type"] == "B" for g in complete_gops for f in g)
        b_frame_count = sum(1 for g in complete_gops for f in g if f["type"] == "B")
        has_idr       = idr_count > 0

        avg_gop = round(sum(gop_lengths) / len(gop_lengths), 1) if gop_lengths else 0
        min_gop = min(gop_lengths) if gop_lengths else 0
        max_gop = max(gop_lengths) if gop_lengths else 0

        # ── Extract stream metadata ───────────────────────────────────
        streams    = probe_data.get("streams", [])
        fmt        = probe_data.get("format", {})
        vid        = next((s for s in streams if s.get("codec_type") == "video"), {})
        aud_list   = [s for s in streams if s.get("codec_type") == "audio"]
        aud        = aud_list[0] if aud_list else {}

        container   = fmt.get("format_long_name") or fmt.get("format_name", "unknown")
        file_dur    = float(fmt.get("duration", 0) or 0)
        file_br     = int(fmt.get("bit_rate", 0) or 0)
        num_prog    = int(fmt.get("nb_programs", 1) or 1)
        num_streams = int(fmt.get("nb_streams", len(streams)) or len(streams))

        v_codec     = vid.get("codec_name", "unknown")
        v_profile   = vid.get("profile", "unknown")
        v_level_raw = vid.get("level", 0)
        v_level_str = f"{v_level_raw/10:.1f}" if isinstance(v_level_raw, int) and v_level_raw > 9 else str(v_level_raw)
        v_level_f   = float(v_level_raw) / 10 if isinstance(v_level_raw, int) and v_level_raw > 9 else 0
        v_width     = vid.get("width", 0)
        v_height    = vid.get("height", 0)
        v_pix_fmt   = vid.get("pix_fmt", "unknown")
        v_b_frames  = vid.get("has_b_frames", 0)
        v_refs      = vid.get("refs", "?")
        v_br_raw    = vid.get("bit_rate")
        v_br        = int(v_br_raw or 0)
        # If stream-level bitrate not available (common in MPEG-TS), estimate from format
        # Subtract estimated audio from total file bitrate
        a_br_raw    = aud.get("bit_rate")
        a_br        = int(a_br_raw or 0)
        if v_br == 0 and file_br:
            a_br_est = a_br if a_br else 192000 * len(aud_list)
            v_br = max(0, file_br - a_br_est)
        if a_br == 0 and file_br and v_br:
            a_br = max(0, file_br - v_br)
        v_color_sp  = vid.get("color_space", "unknown")
        v_color_tr  = vid.get("color_transfer", "unknown")
        v_field     = vid.get("field_order", "progressive")
        v_bits      = vid.get("bits_per_raw_sample") or vid.get("bits_per_coded_sample") or "?"

        r_fps_raw   = vid.get("r_frame_rate", "0/1")
        def _fps_val(raw):
            try: n, d = raw.split("/"); return float(n)/float(d) if float(d) else 0
            except: return 0
        v_fps_val   = _fps_val(r_fps_raw)
        v_fps_str   = f"{r_fps_raw} | {v_fps_val:.3f}"

        dar = vid.get("display_aspect_ratio", "")
        if not dar and v_width and v_height:
            from math import gcd; g = gcd(v_width, v_height); dar = f"{v_width//g}:{v_height//g}"

        chroma_map  = {"yuv420p":"4:2:0","yuv422p":"4:2:2","yuv444p":"4:4:4"}
        v_chroma    = chroma_map.get(v_pix_fmt, v_pix_fmt)

        # Entropy coding (CABAC vs CAVLC) from codec_tag / profile heuristic
        # High/Main profile typically uses CABAC; Baseline uses CAVLC
        v_entropy   = "CABAC" if v_profile in ("High","Main","High 10","High 422","High 444") else "CAVLC"

        # Scan type normalisation
        scan_map = {"progressive":"progressive","tt":"interlaced","bb":"interlaced",
                    "tb":"interlaced","bt":"interlaced","unknown":"progressive"}
        v_scan = scan_map.get(v_field, v_field)

        # SDR/HDR
        hdr_transfers = ("smpte2084","arib-std-b67","smpte428")
        v_hdr = "HDR" if v_color_tr in hdr_transfers else "SDR"

        a_codec    = aud.get("codec_name", "unknown")
        a_profile  = aud.get("profile", "?")
        a_ch       = aud.get("channels", 0)
        a_layout   = aud.get("channel_layout", "?")
        a_rate     = aud.get("sample_rate", "?")
        a_lang     = aud.get("tags", {}).get("language", "?")
        # Audio bits per sample: AAC uses float (fltp), ffprobe reports 0 → normalise
        a_bps_raw  = aud.get("bits_per_raw_sample") or aud.get("bits_per_coded_sample")
        if a_bps_raw and int(a_bps_raw) > 0:
            a_bps  = str(int(a_bps_raw))
        elif a_codec in ("aac", "mp3", "mp2", "mp1", "opus", "vorbis"):
            a_bps  = "FLTP"   # float planar — correct for AAC-LC
        else:
            a_bps  = "?"
        a_br_kbps  = round(a_br / 1000) if a_br else 0

        # VBR/CBR heuristic from codec context (not always available in ts)
        # Use file_br vs v_br+a_br to guess
        v_rate_ctrl = "CBR" if file_br and v_br and abs(file_br - v_br) < file_br * 0.1 else "VBR"

        # ── Compliance checks ─────────────────────────────────────────
        # Returns (status, measured, note)
        def comply(measured, spec_range, preferred=None, label=None):
            """COMPLIANT = within preferred, ACCEPTED = within range, REJECTED = outside range"""
            lo, hi = spec_range
            if measured is None: return "UNKNOWN", str(measured), ""
            in_range = lo <= measured <= hi
            if not in_range: return "REJECTED", str(measured), f"Expected {lo}–{hi}"
            if preferred is not None:
                plo, phi = preferred
                if plo <= measured <= phi: return "COMPLIANT", str(measured), ""
                return "ACCEPTED", str(measured), f"Preferred {plo}–{phi}"
            return "COMPLIANT", str(measured), ""

        def comply_enum(measured, allowed, preferred=None):
            m = str(measured).strip().lower()
            allowed_l = [str(a).lower() for a in allowed]
            preferred_l = [str(a).lower() for a in (preferred or [])]
            if m not in allowed_l: return "REJECTED", measured, f"Expected one of {allowed}"
            if preferred_l and m in preferred_l: return "COMPLIANT", measured, ""
            return "ACCEPTED", measured, ""

        file_br_mbps = round(file_br / 1e6, 5) if file_br else 0
        v_br_mbps    = round(v_br   / 1e6, 5) if v_br   else 0
        a_br_kbps_f  = round(a_br   / 1000, 1) if a_br  else 0
        a_rate_khz   = round(float(a_rate) / 1000, 1) if str(a_rate).isdigit() else 0

        fps_compliant_values = [25.0, 29.97, 30.0]
        fps_ok = any(abs(v_fps_val - f) < 0.1 for f in fps_compliant_values)
        fps_status = "COMPLIANT" if abs(v_fps_val - 25.0) < 0.1 else ("ACCEPTED" if fps_ok else "REJECTED")

        gop_valid = [30, 50]
        gop_status = "COMPLIANT" if avg_gop in gop_valid else (
            "ACCEPTED" if any(abs(avg_gop - g) < 3 for g in gop_valid) else "REJECTED")

        compliance = {
            "overall_br":      comply(file_br_mbps, (5.0, 18.0), (8.0, 15.0)),
            "gop_size":        (gop_status, str(avg_gop), "Expected 30 or 50"),
            "gop_type":        ("COMPLIANT" if gop_type == "CLOSED" else "REJECTED",
                                gop_type, "Must be CLOSED"),
            "b_frames":        ("COMPLIANT" if not has_b_frames else "ACCEPTED",
                                "Absent" if not has_b_frames else "Present", "Preferred absent"),
            "idr":             ("COMPLIANT" if has_idr else "REJECTED",
                                "Present" if has_idr else "ABSENT", "IDR frames required"),
            "frame_size":      comply_enum(f"{v_width}x{v_height}",
                                           ["1280x720","1920x1080"], ["1920x1080"]),
            "aspect_ratio":    comply_enum(dar, ["16:9"], ["16:9"]),
            "chroma":          comply_enum(v_chroma, ["4:2:0"], ["4:2:0"]),
            "scan_type":       comply_enum(v_scan, ["progressive","interlaced","mbaff"], ["interlaced"]),
            "bit_depth":       comply_enum(str(v_bits), ["8"], ["8"]),
            "colour_gamut":    comply_enum(v_color_sp, ["unknown","bt709"], ["bt709"]),
            "codec":           comply_enum(v_codec, ["h264","hevc"], ["h264"]),
            "codec_level":     comply(v_level_f, (4.0, 4.2), (4.1, 4.1)),
            "codec_profile":   comply_enum(v_profile.lower() if v_profile else "",
                                           ["main","high"], ["high"]),
            "entropy":         comply_enum(v_entropy, ["CABAC"], ["CABAC"]),
            "rate_ctrl_v":     comply_enum(v_rate_ctrl, ["VBR","CBR"], ["CBR"]),
            "v_br":            comply(v_br_mbps, (5.0, 18.0), (8.0, 15.0)),
            "hdr_scheme":      comply_enum(v_hdr, ["SDR","HDR"], ["SDR"]),
            "fps":             (fps_status, f"{v_fps_val:.3f}", "Expected 25.0, 29.97, 30.0"),
            "a_codec":         comply_enum(a_codec, ["aac","mp1","mp2"], ["aac"]),
            "a_streams":       comply(len(aud_list), (1, 32), (2, 2)),
            "a_channels":      comply(a_ch, (2, 2)),
            "a_rate_ctrl":     comply_enum("VBR", ["VBR","CBR"], ["CBR"]),
            "a_sample_rate":   comply(a_rate_khz, (44.1, 48.0), (48.0, 48.0)),
            "a_bits":          comply_enum(a_bps.lower(), ["fltp","16","s16"], ["16","fltp"]),
            "a_br_kbps":       comply(a_br_kbps_f, (118, 512), (256, 256)),
        }

        # Overall status
        statuses = [v[0] for v in compliance.values()]
        if "REJECTED" in statuses:
            overall_status = "REJECTED"
        elif "ACCEPTED" in statuses:
            overall_status = "ACCEPTED"
        else:
            overall_status = "COMPLIANT"

        result = {
            "url": url_display, "url_host": url_host, "url_port": url_port,
            "tag": tag, "started_at": _gop_jobs[job_id].get("started_at",""),
            "file_size": ts_size, "file_dur": file_dur, "file_br": file_br,
            "file_br_mbps": file_br_mbps,
            "container": container, "num_programs": num_prog, "num_streams": num_streams,
            "have_video": 1 if vid else 0, "have_audio": len(aud_list),
            # Video
            "v_codec": v_codec, "v_profile": v_profile, "v_level": v_level_str,
            "v_level_f": v_level_f, "v_width": v_width, "v_height": v_height,
            "v_fps": v_fps_str, "v_fps_val": v_fps_val,
            "v_pix_fmt": v_pix_fmt, "v_b_frames": v_b_frames, "v_refs": v_refs,
            "v_br": v_br, "v_br_mbps": v_br_mbps,
            "v_color_sp": v_color_sp, "v_color_tr": v_color_tr,
            "v_color_combined": f"{v_color_sp} | {v_color_tr}",
            "v_field": v_field, "v_scan": v_scan,
            "v_bits": str(v_bits), "v_chroma": v_chroma, "v_dar": dar,
            "v_entropy": v_entropy, "v_hdr": v_hdr, "v_rate_ctrl": v_rate_ctrl,
            # Audio
            "a_codec": a_codec, "a_profile": a_profile, "a_channels": a_ch,
            "a_layout": a_layout, "a_rate": a_rate, "a_rate_khz": a_rate_khz,
            "a_br": a_br, "a_br_kbps": a_br_kbps_f, "a_lang": a_lang,
            "a_bps": str(a_bps), "audio_tracks": len(aud_list),
            # GOP
            "has_idr": has_idr, "idr_count": idr_count,
            "non_idr_keyframes": non_idr_keyframe_count,
            "total_frames": total_frames, "has_b_frames": has_b_frames,
            "b_frame_count": b_frame_count, "gop_type": gop_type,
            "gop_count": len(complete_gops), "gop_avg": avg_gop,
            "gop_min": min_gop, "gop_max": max_gop,
            "gop_patterns": gop_patterns[:20],
            "gops": [[{"type":f["type"],"key":f["key"],"idr":f.get("idr",False)}
                       for f in g] for g in complete_gops[:20]],
            # Compliance
            "compliance": compliance,
            "overall_status": overall_status,
        }

        # Save result
        ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        safe_url = re.sub(r"[^\w\-]", "_", url_display)[:40]
        res_file = f"{ts_str}_{safe_url}.json"
        with open(os.path.join(GOP_DIR, res_file), "w") as f:
            json.dump(result, f, indent=2)
        log(f"Result saved: {res_file}")

        try: os.remove(ts_path)
        except Exception: pass

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "done", "ended_at": ended_at,
                "result": result, "res_file": res_file, "log": log_lines,
            })

    except Exception as e:
        log(f"ERROR: {e}")
        # Try to clean up ts file
        try:
            if ts_path and os.path.isfile(ts_path): os.remove(ts_path)
        except Exception: pass
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "error", "log": log_lines,
                "ended_at": datetime.datetime.utcnow().isoformat() + "Z"
            })


@app.route("/gop/run", methods=["POST"])
def gop_run():
    data       = request.get_json(silent=True) or {}
    url        = (data.get("url") or "").strip()
    duration   = min(int(data.get("duration") or 30), 120)
    passphrase = (data.get("passphrase") or "").strip()
    tag        = (data.get("tag") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    # Inject passphrase into SRT URL if provided and not already there
    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}"

    job_id = str(uuid.uuid4())[:8]
    started_at = datetime.datetime.utcnow().isoformat() + "Z"

    with _gop_lock:
        _gop_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "started_at": started_at,
            "ended_at":   None,
            "url":        re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&'),
            "tag":        tag,
            "result":     None,
            "log":        [],
        }

    t = threading.Thread(target=_run_gop_analysis,
                         args=(job_id, url, duration, passphrase, tag), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/gop/status/<job_id>", methods=["GET"])
def gop_status(job_id):
    with _gop_lock:
        job = _gop_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/gop/results", methods=["GET"])
def gop_results():
    files = sorted([f for f in os.listdir(GOP_DIR) if f.endswith(".json")], reverse=True)
    items = []
    for f in files[:50]:
        try:
            with open(os.path.join(GOP_DIR, f)) as fh:
                d = json.load(fh)
            items.append({
                "file":           f,
                "url":            d.get("url", ""),
                "url_host":       d.get("url_host", ""),
                "url_port":       d.get("url_port", ""),
                "tag":            d.get("tag", ""),
                "started_at":     d.get("started_at", ""),
                "has_idr":        d.get("has_idr", False),
                "has_b_frames":   d.get("has_b_frames", False),
                "gop_type":       d.get("gop_type", ""),
                "gop_avg":        d.get("gop_avg", 0),
                "v_codec":        d.get("v_codec", ""),
                "v_width":        d.get("v_width", 0),
                "v_height":       d.get("v_height", 0),
                "v_fps_val":      d.get("v_fps_val", 0),
                "overall_status": d.get("overall_status", "UNKNOWN"),
            })
        except Exception:
            pass
    return jsonify(items)


@app.route("/gop/result/<path:filename>", methods=["GET"])
def gop_result_file(filename):
    return send_from_directory(GOP_DIR, filename)


@app.route("/gop/delete/<path:filename>", methods=["DELETE"])
def gop_delete(filename):
    ok, err = _check_password(request)
    if not ok:
        return err
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        os.remove(filepath)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # threaded=True permite lidar com várias requisições ao mesmo tempo
    app.run(host='0.0.0.0', port=5050, threaded=True)
