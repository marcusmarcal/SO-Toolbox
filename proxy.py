import base64
import requests
from flask import Flask, request, jsonify, Response
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
            "no_dns": no_dns, "tag": tag,
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
            m = re.match(
                r'\s*\d+\.\s*[|`]-+\s*(\S+)\s+'
                r'([\d.]+)%\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
                line)
            if m:
                hops.append({
                    "host": m.group(1), "loss": float(m.group(2)),
                    "sent": int(m.group(3)), "last": float(m.group(4)),
                    "avg": float(m.group(5)), "best": float(m.group(6)),
                    "worst": float(m.group(7)),
                })
        return hops

    # ── Background thread: runs full mtr --report, saves result ──────
    def run_background():
        if mode == "time":
            bg_cmd = ["mtr", "--report", "--report-wide", "--interval", "1", "-z", "-u", "-P", "53"
                      "--report-cycles", str(seconds)]
        else:
            bg_cmd = ["mtr", "--report", "--report-wide", "-z", "-u", "-P", "53"
                      "--report-cycles", str(count)]
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


if __name__ == "__main__":
    # threaded=True permite lidar com várias requisições ao mesmo tempo
    app.run(host='0.0.0.0', port=5050, threaded=True)
