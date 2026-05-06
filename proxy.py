import base64
import requests
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)

from id3as_routes import id3as_bp
app.register_blueprint(id3as_bp)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit
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


def _run_ingest(job_id, url, output_dir, is_file_upload=False):
    """Background thread: run analysis, let script choose its own output dir.
    If is_file_upload=True, url is a local .ts file path — pass directly to script.
    """
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _ingest_lock:
            _ingest_jobs[job_id]["log"] = list(log_lines)

    # Get tag from job state
    with _ingest_lock:
        tag        = _ingest_jobs[job_id].get("tag", "")
        started_at = _ingest_jobs[job_id].get("started_at", "")
        url_display = _ingest_jobs[job_id].get("url", url)

    # For non-upload: clean URL for display (remove passphrase)
    if not is_file_upload:
        url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')
        with _ingest_lock:
            _ingest_jobs[job_id]["url"] = url_display

    try:
        if is_file_upload:
            log(f"Starting analysis on uploaded file: {url_display}")
            ts_size = os.path.getsize(url) if os.path.isfile(url) else 0
            log(f"File size: {ts_size:,} bytes")
            script_input = url  # pass local path directly to script
        else:
            log(f"Starting analysis for: {url_display}")
            script_input = url

        result = subprocess.run(
            ["run-ingest-analysis.sh", script_input],
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

        # Clean up uploaded temp file (it was a staging copy, results are in ingest-results/)
        if is_file_upload and os.path.isfile(url) and os.path.dirname(url) == INGEST_RESULTS_DIR:
            try:
                os.remove(url)
            except Exception:
                pass

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


@app.route("/ingest/upload", methods=["POST"])
def ingest_upload():
    """Accept an uploaded .ts file and run ingest analysis on it (skips live capture)."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".ts"):
        return jsonify({"error": "Only .ts files are supported"}), 400

    tag = (request.form.get("tag") or "").strip()

    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".ts", delete=False, dir=INGEST_RESULTS_DIR) as tmp:
        ts_save_path = tmp.name
    f.save(ts_save_path)

    if os.path.getsize(ts_save_path) < 500:
        os.remove(ts_save_path)
        return jsonify({"error": "Uploaded file is empty or too small"}), 400

    job_id     = str(uuid.uuid4())[:8]
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    url_display = f"upload:{f.filename}"

    with _ingest_lock:
        _ingest_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "url":        url_display,
            "tag":        tag,
            "started_at": started_at,
            "ended_at":   None,
            "zip":        None,
            "dir":        None,
            "summary":    {},
            "log":        [],
        }

    t = threading.Thread(target=_run_ingest, args=(job_id, ts_save_path, None, True), daemon=True)
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


def _run_gop_on_file(job_id, ts_path, tag, url_display, started_at):
    """Run GOP analysis on an already-captured/uploaded .ts file (no ffmpeg capture)."""
    log_lines = []
    def log(msg):
        log_lines.append(msg)
        with _gop_lock:
            if job_id in _gop_jobs:
                _gop_jobs[job_id]["log"] = list(log_lines)

    try:
        log(f"Analysing file: {url_display}")
        ts_size = os.path.getsize(ts_path)
        log(f"File size: {ts_size:,} bytes")

        # Parse url_display for host/port (upload has none)
        url_host = "upload"
        url_port = ""

        # Re-use the shared analysis block by calling _run_gop_analysis
        # with a special sentinel URL — but the cleanest way is to call
        # the same analysis steps. We signal "skip capture" by passing
        # ts_path as the url with a special prefix that _run_gop_analysis detects.

        # Actually: monkey-patch _gop_jobs to inject ts_path, then call full analysis
        # Simplest correct approach: duplicate just the ffprobe+parse steps here.
        # Since _run_gop_analysis is one long function, we use a thread-safe trick:
        # write ts to a known location and call _run_gop_analysis with url="file://..."
        # which it will detect and skip the ffmpeg capture.

        # We patch the job to include ts_path so _run_gop_analysis can read it
        with _gop_lock:
            _gop_jobs[job_id]["_ts_path"] = ts_path

        # Call the main analysis function with a file:// URL
        # Extract original filename from url_display ("upload:filename.ts")
        original_name = url_display.split("upload:", 1)[-1] if url_display.startswith("upload:") else None
        _run_gop_analysis(job_id, f"file://{ts_path}", 9999, "", tag,
                          _started_at=started_at, _original_name=original_name)

    except Exception as e:
        log(f"ERROR: {e}")
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "error", "log": log_lines,
                "ended_at": datetime.datetime.utcnow().isoformat() + "Z"
            })


def _run_gop_analysis(job_id, url, duration, passphrase, tag, _started_at=None, _original_name=None):
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

    # Detect file:// URL (uploaded file — skip capture)
    is_file_upload = url.startswith("file://")
    ts_path_from_upload = url[7:] if is_file_upload else None

    # Parse host:port from URL for display
    m_host = re.search(r'srt://([^:/?]+):(\d+)', url_display)
    if is_file_upload:
        url_host = "upload"
        url_port = ""
        url_display = "upload:" + (_original_name or os.path.basename(ts_path_from_upload or ""))
    else:
        url_host = m_host.group(1) if m_host else url_display
        url_port = m_host.group(2) if m_host else ""

    ts_path = None
    cap_returncode = 0

    # Use _started_at if provided (for upload jobs)
    if _started_at and job_id in _gop_jobs:
        with _gop_lock:
            _gop_jobs[job_id]["started_at"] = _started_at

    try:
        log(f"Starting GOP analysis for: {url_display}")

        if is_file_upload:
            # Skip capture — use already-saved file
            ts_path = ts_path_from_upload
            ts_size = os.path.getsize(ts_path) if ts_path and os.path.isfile(ts_path) else 0
            log(f"Using uploaded file: {ts_size:,} bytes")
        else:
            log(f"Capture duration: {duration}s")
            with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
                ts_path = tmp.name

            # ── Step 1: capture stream (graceful timeout) ─────────────────
            log("Capturing stream with ffmpeg…")
            cap_cmd = [
                "ffmpeg", "-y",
                "-timeout", str((duration + 10) * 1000000),
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

            ts_size = os.path.getsize(ts_path) if (ts_path and os.path.isfile(ts_path)) else 0
            log(f"Captured {ts_size:,} bytes")

        if ts_size < 500:
            log("ERROR: Capture produced no usable data. Is the stream reachable?")
            if not is_file_upload and 'cap_out' in dir():
                log(cap_out[-800:])
            ended_at = datetime.datetime.utcnow().isoformat() + "Z"
            err_result = {
                "url": url_display, "url_host": url_host, "url_port": url_port,
                "tag": tag, "started_at": _gop_jobs.get(job_id, {}).get("started_at",""),
                "ended_at": ended_at,
                "status": "failed",
                "error": "Stream unreachable or produced no data",
                "log": log_lines,
                "has_idr": False, "idr_count": 0, "total_frames": 0,
                "overall_status": "FAILED", "is_scheduled": False, "override": None,
            }
            # Always save JSON so scheduled runs are logged
            ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            safe_url = re.sub(r"[^\w\-]", "_", url_display)[:40]
            res_file = f"{ts_str}_{safe_url}_FAILED.json"
            try:
                with open(os.path.join(GOP_DIR, res_file), "w") as f:
                    json.dump(err_result, f, indent=2)
                log(f"Failure log saved: {res_file}")
            except Exception as ex:
                log(f"WARNING: Could not save failure log: {ex}")
            # Clean up temp ts file
            try:
                if ts_path and os.path.isfile(ts_path): os.remove(ts_path)
            except Exception: pass
            with _gop_lock:
                _gop_jobs[job_id].update({
                    "status": "failed", "log": log_lines,
                    "ended_at": ended_at,
                    "res_file": res_file,
                    "result": err_result,
                })
            return

        # ── Step 2: container/stream info ─────────────────────────────
        log("Running ffprobe for stream info…")
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", "-show_programs", ts_path
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

        # ── Step 4: AV sync and PTS jitter analysis ───────────────────
        log("Running ffprobe for AV sync analysis…")
        av_sync = {"av_sync_min_ms": None, "av_sync_max_ms": None,
                   "av_sync_avg_ms": None, "av_sync_median_ms": None,
                   "v_pts_jitter_ms": None, "a_pts_jitter_ms": None}
        try:
            def _get_pts(f):
                """Return pts_time or pkt_dts_time as float, or None if unavailable."""
                for key in ("pts_time", "pkt_dts_time"):
                    val = f.get(key)
                    if val not in (None, "N/A"):
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            pass
                return None

            def _probe_pts(stream_spec):
                """Run ffprobe for a single stream and return list of PTS floats."""
                cmd = [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-select_streams", stream_spec,
                    "-show_frames",
                    "-show_entries", "frame=pts_time,pkt_dts_time",
                    ts_path
                ]
                r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=60)
                stderr_out = r.stderr.decode(errors="replace").strip()
                if stderr_out:
                    log(f"ffprobe [{stream_spec}] stderr: {stderr_out[:200]}")
                frames = json.loads(r.stdout.decode()).get("frames", [])
                pts = sorted([t for f in frames for t in [_get_pts(f)] if t is not None])
                log(f"ffprobe [{stream_spec}]: {len(frames)} frames, {len(pts)} valid PTS")
                return pts

            v_pts = _probe_pts("v:0")
            a_pts = _probe_pts("a:0")

            if v_pts and a_pts:
                # AV offset: difference between video PTS and nearest audio PTS
                offsets = []
                a_idx = 0
                for vt in v_pts:
                    # Binary search for nearest audio PTS
                    while a_idx + 1 < len(a_pts) and abs(a_pts[a_idx+1] - vt) < abs(a_pts[a_idx] - vt):
                        a_idx += 1
                    offsets.append(abs(vt - a_pts[a_idx]) * 1000)  # ms

                if offsets:
                    av_sync["av_sync_min_ms"]    = round(min(offsets), 2)
                    av_sync["av_sync_max_ms"]    = round(max(offsets), 2)
                    av_sync["av_sync_avg_ms"]    = round(sum(offsets)/len(offsets), 2)
                    s_off = sorted(offsets)
                    mid = len(s_off) // 2
                    av_sync["av_sync_median_ms"] = round(
                        s_off[mid] if len(s_off) % 2 else (s_off[mid-1]+s_off[mid])/2, 2)

            # PTS jitter — frame-to-frame interval variation
            def _jitter(pts_list, expected_interval=None):
                if len(pts_list) < 2:
                    return 0.0
                diffs = [abs(pts_list[i+1] - pts_list[i]) for i in range(len(pts_list)-1)]
                avg = sum(diffs) / len(diffs)
                variations = [abs(d - avg) * 1000 for d in diffs]  # ms
                return round(sum(variations)/len(variations), 2)

            if len(v_pts) > 2:
                av_sync["v_pts_jitter_ms"] = _jitter(v_pts)
            if len(a_pts) > 2:
                av_sync["a_pts_jitter_ms"] = _jitter(a_pts)

            log(f"AV sync: min={av_sync['av_sync_min_ms']}ms max={av_sync['av_sync_max_ms']}ms "
                f"avg={av_sync['av_sync_avg_ms']}ms jitter V={av_sync['v_pts_jitter_ms']}ms "
                f"A={av_sync['a_pts_jitter_ms']}ms")
        except Exception as e:
            log(f"WARNING: AV sync analysis failed: {e}")
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

        # Scan type normalisation — must be defined before FPS and HDR logic
        scan_map = {"progressive":"progressive","tt":"interlaced","bb":"interlaced",
                    "tb":"interlaced","bt":"interlaced","unknown":"progressive"}
        v_scan = scan_map.get(v_field, v_field)

        r_fps_raw   = vid.get("r_frame_rate", "0/1")
        def _fps_val(raw):
            try: n, d = raw.split("/"); return float(n)/float(d) if float(d) else 0
            except: return 0
        v_fps_val   = _fps_val(r_fps_raw)

        # INTERLACED FPS: ffprobe r_frame_rate can report either:
        #   - field rate: 50/1 for 50i (needs ÷2 → 25 fps)
        #   - frame rate: 25/1 for 25p stored as interlaced container (no division needed)
        # Rule: only divide if fps_val > 30 AND stream is interlaced.
        # A 25/1 interlaced stream is already at frame rate.
        v_fps_for_compliance = v_fps_val
        if v_scan == "interlaced" and v_fps_val > 30:
            v_fps_for_compliance = v_fps_val / 2  # 50 fields/s → 25 frames/s
        v_fps_interlaced_note = ""
        if v_scan == "interlaced" and v_fps_val > 30:
            v_fps_interlaced_note = f" (50i→{v_fps_for_compliance:.3f}fps)"
        v_fps_str = f"{r_fps_raw} | {v_fps_val:.3f}{v_fps_interlaced_note}"

        dar = vid.get("display_aspect_ratio", "")
        if not dar and v_width and v_height:
            from math import gcd; g = gcd(v_width, v_height); dar = f"{v_width//g}:{v_height//g}"

        chroma_map  = {"yuv420p":"4:2:0","yuv422p":"4:2:2","yuv444p":"4:4:4"}
        v_chroma    = chroma_map.get(v_pix_fmt, v_pix_fmt)

        # Entropy coding (CABAC vs CAVLC) from profile heuristic
        v_entropy   = "CABAC" if v_profile in ("High","Main","High 10","High 422","High 444") else "CAVLC"

        # SDR/HDR — arib-std-b67 (HLG) on interlaced broadcast streams is heritage,
        # not true HDR content. Treat as SDR unless progressive.
        hdr_transfers = ("smpte2084", "smpte428")  # removed arib-std-b67
        if v_color_tr in hdr_transfers:
            v_hdr = "HDR"
        elif v_color_tr == "arib-std-b67" and v_scan == "progressive":
            v_hdr = "HDR"  # HLG on progressive = genuine HDR
        else:
            v_hdr = "SDR"  # interlaced arib-std-b67 = broadcast legacy, SDR

        # Audio: normalise codec display name including profile
        def _audio_display_name(codec, profile):
            c = (codec or "").lower()
            p = (profile or "").upper()
            if c == "aac":
                if "LATM" in p:     return "AAC-LATM"
                if "HE" in p:       return "AAC-HE"
                if "LD" in p:       return "AAC-LD"
                if "ELD" in p:      return "AAC-ELD"
                return f"AAC-{p}" if p and p != "?" and p != "UNKNOWN" else "AAC-LC"
            if c in ("mp1","mp2","mp3"): return c.upper()
            return codec.upper() if codec else "?"

        a_codec    = aud.get("codec_name", "unknown")
        a_profile  = aud.get("profile", "?")
        a_codec_display = _audio_display_name(a_codec, a_profile)
        a_ch       = aud.get("channels", 0)
        a_layout   = aud.get("channel_layout", "?")
        a_rate     = aud.get("sample_rate", "?")
        a_lang     = aud.get("tags", {}).get("language", "?")

        # Audio bits per sample: AAC uses float (fltp), ffprobe reports 0 → normalise
        a_bps_raw  = aud.get("bits_per_raw_sample") or aud.get("bits_per_coded_sample")
        if a_bps_raw and int(a_bps_raw) > 0:
            a_bps  = str(int(a_bps_raw))
        elif a_codec in ("aac", "mp3", "mp2", "mp1", "opus", "vorbis"):
            a_bps  = "FLTP"
        else:
            a_bps  = "?"
        a_br_kbps  = round(a_br / 1000) if a_br else 0

        # Count ALL audio streams across all programs (not just first program)
        # ffprobe -show_programs gives per-program stream lists
        all_audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        # For MPEG-TS: count unique audio stream indices across all programs
        programs = probe_data.get("programs", [])
        if programs:
            audio_indices = set()
            for prog in programs:
                for s in prog.get("streams", []):
                    if s.get("codec_type") == "audio":
                        audio_indices.add(s.get("index", id(s)))
            audio_track_count = len(audio_indices) if audio_indices else len(all_audio_streams)
        else:
            audio_track_count = len(all_audio_streams)

        # VBR/CBR heuristic
        v_rate_ctrl = "CBR" if file_br and v_br and abs(file_br - v_br) < file_br * 0.1 else "VBR"

        def _av_check(measured_ms, warn_threshold, hard_limit, label):
            """AV sync / jitter check: PASS < warn, WARN < hard_limit, FAIL >= hard_limit."""
            if measured_ms is None:
                return ("UNKNOWN", "—", "Could not measure")
            m = round(measured_ms, 2)
            if m < warn_threshold:
                return ("COMPLIANT", f"{m} ms", f"< {warn_threshold}ms preferred")
            if m < hard_limit:
                return ("ACCEPTED", f"{m} ms", f"< {hard_limit}ms hard limit; prefer < {warn_threshold}ms")
            return ("REJECTED", f"{m} ms", f"Exceeds hard limit of {hard_limit}ms")

        # ── Compliance checks — driven by specs.json ──────────────────
        # Load current specs from specs.json (or defaults)
        specs = _load_specs()

        def _s(key):
            """Get spec dict for a key, falling back to DEFAULT_SPECS."""
            return specs.get(key, DEFAULT_SPECS.get(key, {}))

        def comply_range(measured, key):
            """Check a numeric value against a range spec."""
            sp = _s(key)
            lo, hi = sp.get("lo", 0), sp.get("hi", float("inf"))
            plo = sp.get("pref_lo")
            phi = sp.get("pref_hi")
            label = sp.get("label", key)
            if measured is None:
                return "UNKNOWN", "—", ""
            in_range = lo <= measured <= hi
            if not in_range:
                return "REJECTED", str(measured), f"Expected {lo}–{hi}"
            if plo is not None and phi is not None:
                if plo <= measured <= phi:
                    return "COMPLIANT", str(measured), ""
                return "ACCEPTED", str(measured), f"Preferred {plo}–{phi}"
            return "COMPLIANT", str(measured), ""

        def comply_enum_s(measured, key):
            """Check an enum value against an enum spec."""
            sp = _s(key)
            allowed   = [str(v).lower() for v in sp.get("values", [])]
            preferred = str(sp.get("preferred", "")).lower()
            m = str(measured).strip().lower()
            if not allowed:
                return "UNKNOWN", measured, "No spec defined"
            if m not in allowed:
                return "REJECTED", measured, f"Expected one of {sp.get('values', [])}"
            if preferred and m == preferred:
                return "COMPLIANT", measured, ""
            if preferred and m != preferred:
                return "ACCEPTED", measured, f"Preferred {sp.get('preferred','')}"
            return "COMPLIANT", measured, ""

        # Helper: also accept multi-preferred
        def comply_enum_multi(measured, key):
            """Like comply_enum_s but preferred can be a list."""
            sp = _s(key)
            allowed    = [str(v).lower() for v in sp.get("values", [])]
            pref_raw   = sp.get("preferred", "")
            if isinstance(pref_raw, list):
                preferred = [str(p).lower() for p in pref_raw]
            else:
                preferred = [str(pref_raw).lower()] if pref_raw else []
            m = str(measured).strip().lower()
            if not allowed:
                return "UNKNOWN", measured, "No spec defined"
            if m not in allowed:
                return "REJECTED", measured, f"Expected one of {sp.get('values', [])}"
            if preferred and m in preferred:
                return "COMPLIANT", measured, ""
            if preferred:
                return "ACCEPTED", measured, f"Preferred {pref_raw}"
            return "COMPLIANT", measured, ""

        file_br_mbps = round(file_br / 1e6, 5) if file_br else 0
        v_br_mbps    = round(v_br   / 1e6, 5) if v_br   else 0
        a_br_kbps_f  = round(a_br   / 1000, 1) if a_br  else 0
        a_rate_khz   = round(float(a_rate) / 1000, 1) if str(a_rate).isdigit() else 0

        # ── FPS (specs-driven, with interlaced compensation) ──────────
        fps_eff = v_fps_for_compliance
        fps_sp  = _s("fps")
        fps_values = [float(v) for v in fps_sp.get("values", [25.0, 29.97, 30.0])]
        fps_pref   = fps_sp.get("preferred", 25.0)
        allow_50p_720 = fps_sp.get("allow_50p_720", False)

        # Accept 50p if 720p allowed
        fps_to_check = fps_eff
        if allow_50p_720 and v_height == 720 and abs(fps_eff - 50.0) < 0.5:
            fps_to_check = 50.0
            fps_values = list(fps_values) + [50.0]

        fps_ok = any(abs(fps_to_check - f) < 0.1 for f in fps_values)
        fps_pref_ok = isinstance(fps_pref, (int, float)) and abs(fps_to_check - float(fps_pref)) < 0.1
        if not fps_ok:
            fps_status = "REJECTED"
        elif fps_pref_ok:
            fps_status = "COMPLIANT"
        else:
            fps_status = "ACCEPTED"
        fps_measured_str = f"{fps_eff:.3f}" + (" (50i→25fps)" if v_scan == "interlaced" and v_fps_val != fps_eff else "")

        # ── GOP size (specs-driven, with seconds option) ──────────────
        gop_sp      = _s("gop_size")
        gop_values  = [int(v) for v in gop_sp.get("values", [30, 50])]
        gop_tol     = int(gop_sp.get("tolerance", 3))
        allow_secs  = gop_sp.get("allow_seconds", True)

        if allow_secs and avg_gop > 0:
            # Also accept GOPs that are 1s or 2s at the measured fps
            fps_for_gop = fps_eff if fps_eff > 0 else 25.0
            gop_values_ext = list(gop_values)
            for secs in [1, 2]:
                gop_values_ext.append(round(fps_for_gop * secs))
            gop_values_check = gop_values_ext
        else:
            gop_values_check = gop_values

        gop_exact = any(abs(avg_gop - g) < 1 for g in gop_values_check)
        gop_near  = any(abs(avg_gop - g) <= gop_tol for g in gop_values_check)
        gop_status = "COMPLIANT" if gop_exact else ("ACCEPTED" if gop_near else "REJECTED")
        gop_expected_str = ", ".join(str(v) for v in gop_values)
        if allow_secs:
            gop_expected_str += " or 1s/2s"

        # ── GOP type ──────────────────────────────────────────────────
        gop_type_sp  = _s("gop_type")
        required_gop = gop_type_sp.get("required", "CLOSED").upper()
        if gop_type.upper() == required_gop:
            gop_type_status = "COMPLIANT"
        else:
            gop_type_status = "REJECTED"

        # ── B-frames ──────────────────────────────────────────────────
        b_sp = _s("b_frames")
        pref_b = str(b_sp.get("preferred", "absent")).lower()
        if pref_b == "absent":
            b_status = "COMPLIANT" if not has_b_frames else "ACCEPTED"
        else:
            b_status = "COMPLIANT" if has_b_frames else "ACCEPTED"

        compliance = {
            "overall_br":   comply_range(file_br_mbps, "overall_br"),
            "gop_size":     (gop_status, str(avg_gop), f"Expected {gop_expected_str}"),
            "gop_type":     (gop_type_status, gop_type, f"Must be {required_gop}"),
            "b_frames":     (b_status, "Absent" if not has_b_frames else "Present", f"Preferred {pref_b}"),
            "idr":          ("COMPLIANT" if has_idr else "REJECTED",
                             "Present" if has_idr else "ABSENT", "IDR frames required"),
            "frame_size":   comply_enum_multi(f"{v_width}x{v_height}", "frame_size"),
            "aspect_ratio": comply_enum_multi(dar, "aspect_ratio"),
            "chroma":       comply_enum_multi(v_chroma, "chroma"),
            "scan_type":    comply_enum_multi(v_scan, "scan_type"),
            "bit_depth":    comply_enum_multi(str(v_bits), "bit_depth"),
            "colour_gamut": comply_enum_multi(v_color_sp, "colour_gamut"),
            "codec":        comply_enum_multi(v_codec.lower() if v_codec else "", "codec"),
            "codec_level":  comply_range(v_level_f, "codec_level"),
            "codec_profile":comply_enum_multi(v_profile.lower() if v_profile else "", "codec_profile"),
            "entropy":      comply_enum_multi(v_entropy, "entropy"),
            "rate_ctrl_v":  comply_enum_multi(v_rate_ctrl, "rate_ctrl_v"),
            "v_br":         comply_range(v_br_mbps, "v_br"),
            "hdr_scheme":   comply_enum_multi(v_hdr, "hdr_scheme"),
            "fps":          (fps_status, fps_measured_str, f"Expected {fps_values}"),
            "a_codec":      comply_enum_multi(a_codec_display, "a_codec"),
            "a_streams":    comply_range(audio_track_count, "a_streams"),
            "a_channels":   comply_range(a_ch, "a_channels"),
            "a_rate_ctrl":  comply_enum_multi("VBR", "a_rate_ctrl"),
            "a_sample_rate":comply_range(a_rate_khz, "a_sample_rate"),
            "a_bits":       comply_enum_multi(a_bps.lower(), "a_bits"),
            "a_br_kbps":    comply_range(a_br_kbps_f, "a_br_kbps"),
            # AV Sync
            "av_sync_warn": _av_check(av_sync.get("av_sync_avg_ms"), 15, 230, "AV Sync Avg Offset (ms)"),
            "av_sync_max":  _av_check(av_sync.get("av_sync_max_ms"), 175, 230, "AV Sync Max Offset (ms)"),
            "v_pts_jitter": _av_check(av_sync.get("v_pts_jitter_ms"), 5.0, 10.0, "Video PTS Jitter (ms)"),
            "a_pts_jitter": _av_check(av_sync.get("a_pts_jitter_ms"), 5.0, 10.0, "Audio PTS Jitter (ms)"),
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
            "v_fps": v_fps_str, "v_fps_val": v_fps_val, "v_fps_compliance": v_fps_for_compliance,
            "v_pix_fmt": v_pix_fmt, "v_b_frames": v_b_frames, "v_refs": v_refs,
            "v_br": v_br, "v_br_mbps": v_br_mbps,
            "v_color_sp": v_color_sp, "v_color_tr": v_color_tr,
            "v_color_combined": f"{v_color_sp} | {v_color_tr}",
            "v_field": v_field, "v_scan": v_scan,
            "v_bits": str(v_bits), "v_chroma": v_chroma, "v_dar": dar,
            "v_entropy": v_entropy, "v_hdr": v_hdr, "v_rate_ctrl": v_rate_ctrl,
            # Audio
            "a_codec": a_codec, "a_codec_display": a_codec_display,
            "a_profile": a_profile, "a_channels": a_ch,
            "a_layout": a_layout, "a_rate": a_rate, "a_rate_khz": a_rate_khz,
            "a_br": a_br, "a_br_kbps": a_br_kbps_f, "a_lang": a_lang,
            "a_bps": str(a_bps), "audio_tracks": audio_track_count,
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
            "test_id": str(uuid.uuid4()),
            # AV Sync
            "av_sync_min_ms":    av_sync.get("av_sync_min_ms"),
            "av_sync_max_ms":    av_sync.get("av_sync_max_ms"),
            "av_sync_avg_ms":    av_sync.get("av_sync_avg_ms"),
            "av_sync_median_ms": av_sync.get("av_sync_median_ms"),
            "v_pts_jitter_ms":   av_sync.get("v_pts_jitter_ms"),
            "a_pts_jitter_ms":   av_sync.get("a_pts_jitter_ms"),
        }

        # Save result JSON
        ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        safe_url = re.sub(r"[^\w\-]", "_", url_display)[:40]
        res_file = f"{ts_str}_{safe_url}.json"
        ts_dest  = os.path.join(GOP_DIR, res_file.replace(".json", ".ts"))

        # Keep .ts file for download — copy to gop-results dir (if not already there)
        if ts_path and os.path.isfile(ts_path):
            try:
                if is_file_upload and os.path.dirname(ts_path) == GOP_DIR:
                    # Already in GOP_DIR (uploaded directly)
                    ts_dest = ts_path
                    ts_saved = os.path.basename(ts_dest)
                else:
                    import shutil as _shutil
                    _shutil.move(ts_path, ts_dest)
                    ts_saved = os.path.basename(ts_dest)
                log(f"TS saved: {ts_saved}")
            except Exception as e:
                log(f"WARNING: Could not save .ts file: {e}")
                ts_saved = None
        else:
            ts_saved = None

        result["ts_file"] = ts_saved
        result["override"] = None  # placeholder — set via /gop/override endpoint

        with open(os.path.join(GOP_DIR, res_file), "w") as f:
            json.dump(result, f, indent=2)
        log(f"Result saved: {res_file}")

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        with _gop_lock:
            _gop_jobs[job_id].update({
                "status": "done", "ended_at": ended_at,
                "result": result, "res_file": res_file, "log": log_lines,
            })

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        # Clean up temp ts file if still exists (not yet moved)
        try:
            if ts_path and os.path.isfile(ts_path): os.remove(ts_path)
        except Exception: pass

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        err_result = {
            "url": url_display if 'url_display' in dir() else url,
            "url_host": url_host if 'url_host' in dir() else "",
            "url_port": url_port if 'url_port' in dir() else "",
            "tag": tag,
            "started_at": _gop_jobs.get(job_id, {}).get("started_at", ""),
            "ended_at": ended_at,
            "status": "error",
            "error": str(e),
            "log": log_lines,
            "has_idr": False, "idr_count": 0, "total_frames": 0,
            "overall_status": "ERROR", "is_scheduled": False, "override": None,
        }
        # Always save JSON so scheduled runs are fully logged
        try:
            ts_str   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            safe_url = re.sub(r"[^\w\-]", "_", err_result["url"])[:40]
            res_file = f"{ts_str}_{safe_url}_ERROR.json"
            with open(os.path.join(GOP_DIR, res_file), "w") as f:
                json.dump(err_result, f, indent=2)
            log_lines.append(f"Error log saved: {res_file}")
            err_result["log"] = log_lines
        except Exception as ex2:
            res_file = None
            log_lines.append(f"WARNING: Could not save error log: {ex2}")

        with _gop_lock:
            _gop_jobs[job_id].update({
                "status":   "error",
                "log":      log_lines,
                "ended_at": ended_at,
                "res_file": res_file,
                "result":   err_result,
            })


@app.route("/gop/upload", methods=["POST"])
def gop_upload():
    """Accept an uploaded .ts file and run GOP analysis on it (skips capture step)."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".ts"):
        return jsonify({"error": "Only .ts files are supported"}), 400

    tag = (request.form.get("tag") or "").strip()

    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".ts", delete=False, dir=GOP_DIR) as tmp:
        ts_save_path = tmp.name
    f.save(ts_save_path)

    if os.path.getsize(ts_save_path) < 500:
        os.remove(ts_save_path)
        return jsonify({"error": "Uploaded file is empty or too small"}), 400

    job_id     = str(uuid.uuid4())[:8]
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    url_display = f"upload:{f.filename}"

    with _gop_lock:
        _gop_jobs[job_id] = {
            "job_id": job_id, "status": "running",
            "started_at": started_at, "ended_at": None,
            "url": url_display, "tag": tag, "result": None, "log": [],
        }

    t = threading.Thread(
        target=_run_gop_on_file,
        args=(job_id, ts_save_path, tag, url_display, started_at),
        daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})


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
            override         = d.get("override")
            raw_status       = d.get("status", "done")          # done | failed | error
            raw_ov_status    = d.get("overall_status", "UNKNOWN")
            # If override, show that; if failed/error, show that instead of compliance
            if override:
                eff_status = "ACCEPTED (Override)"
            elif raw_status in ("failed", "error"):
                eff_status = raw_status.upper()
            else:
                eff_status = raw_ov_status

            v_fps_val        = d.get("v_fps_val", 0)
            v_fps_compliance = d.get("v_fps_compliance", v_fps_val)
            v_scan           = d.get("v_scan", "progressive")
            items.append({
                "file":              f,
                "url":               d.get("url", ""),
                "url_host":          d.get("url_host", ""),
                "url_port":          d.get("url_port", ""),
                "tag":               d.get("tag", ""),
                "started_at":        d.get("started_at", ""),
                "ended_at":          d.get("ended_at", ""),
                "has_idr":           d.get("has_idr", False),
                "has_b_frames":      d.get("has_b_frames", False),
                "gop_type":          d.get("gop_type", ""),
                "gop_avg":           d.get("gop_avg", 0),
                "v_codec":           d.get("v_codec", ""),
                "v_width":           d.get("v_width", 0),
                "v_height":          d.get("v_height", 0),
                "v_fps_val":         v_fps_val,
                "v_fps_compliance":  v_fps_compliance,
                "v_scan":            v_scan,
                "run_status":        raw_status,       # done|failed|error
                "overall_status":    eff_status,
                "override":          override,
                "error":             d.get("error", ""),
                "ts_file":           d.get("ts_file"),
                "is_scheduled":      d.get("is_scheduled", False),
                "log_count":         len(d.get("log", [])),
                "test_id":           d.get("test_id", ""),
            })
        except Exception:
            pass
    return jsonify(items)


@app.route("/gop/result/<path:filename>", methods=["GET"])
def gop_result_file(filename):
    return send_from_directory(GOP_DIR, filename)


@app.route("/gop/ts/<path:filename>", methods=["GET"])
def gop_ts_download(filename):
    """Download the .ts capture file for a GOP analysis result."""
    return send_from_directory(GOP_DIR, filename, as_attachment=True)


@app.route("/gop/override/<path:filename>", methods=["POST"])
def gop_override(filename):
    """Save override reason to the result JSON and update overall_status."""
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(filepath) as f:
            d = json.load(f)
        d["override"] = {
            "reason":    reason,
            "applied_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        d["overall_status"] = "ACCEPTED (Override)"
        with open(filepath, "w") as f:
            json.dump(d, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/gop/override/<path:filename>", methods=["DELETE"])
def gop_override_remove(filename):
    """Remove override from a result JSON."""
    filepath = os.path.join(GOP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(filepath) as f:
            d = json.load(f)
        d.pop("override", None)
        # Re-compute overall_status from compliance
        statuses = [v[0] for v in (d.get("compliance") or {}).values()]
        if "REJECTED" in statuses:   d["overall_status"] = "REJECTED"
        elif "ACCEPTED" in statuses: d["overall_status"] = "ACCEPTED"
        else:                        d["overall_status"] = "COMPLIANT"
        with open(filepath, "w") as f:
            json.dump(d, f, indent=2)
        return jsonify({"success": True, "overall_status": d["overall_status"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/gop/delete/<path:filename>", methods=["DELETE"])
def gop_delete(filename):
    ok, err = _check_password(request)
    if not ok:
        return err
    filepath = os.path.join(GOP_DIR, filename)
    # Also delete associated .ts file if present
    ts_path  = filepath.replace(".json", ".ts")
    deleted  = []
    for fp in [filepath, ts_path]:
        if os.path.isfile(fp):
            try: os.remove(fp); deleted.append(os.path.basename(fp))
            except Exception: pass
    if filepath.replace(".json","") + ".json" not in [os.path.join(GOP_DIR, d) for d in deleted]:
        if not os.path.isfile(filepath):
            return jsonify({"success": True, "deleted": deleted})
        return jsonify({"error": "Could not delete file"}), 500
    return jsonify({"success": True, "deleted": deleted})


# ── SCHEDULED GOP JOBS ──────────────────────────────────────────────
_gop_scheduled = {}   # sched_id -> {sched_id, run_at_utc, url, duration, tag, status}
_gop_sched_lock = threading.Lock()


@app.route("/gop/schedule", methods=["POST"])
def gop_schedule():
    """Schedule a GOP analysis for a future UTC time."""
    data       = request.get_json(silent=True) or {}
    url        = (data.get("url") or "").strip()
    run_at     = (data.get("run_at_utc") or "").strip()   # ISO format: 2026-04-22T14:30:00
    duration   = min(int(data.get("duration") or 30), 120)
    passphrase = (data.get("passphrase") or "").strip()
    tag        = (data.get("tag") or "").strip()
    if not url or not run_at:
        return jsonify({"error": "url and run_at_utc are required"}), 400

    try:
        run_dt = datetime.datetime.fromisoformat(run_at.replace("Z",""))
    except ValueError:
        return jsonify({"error": "Invalid run_at_utc format (use ISO 8601)"}), 400

    if passphrase and "passphrase=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}passphrase={passphrase}"

    sched_id = str(uuid.uuid4())[:8]
    url_display = re.sub(r'[?&]passphrase=[^&]*', '', url).rstrip('?&')

    with _gop_sched_lock:
        _gop_scheduled[sched_id] = {
            "sched_id":   sched_id,
            "url":        url_display,
            "url_full":   url,
            "run_at_utc": run_dt.isoformat() + "Z",
            "duration":   duration,
            "tag":        tag,
            "status":     "pending",
        }

    def _wait_and_run():
        import time as _time2
        now_utc = datetime.datetime.utcnow()
        delay   = (run_dt - now_utc).total_seconds()
        if delay > 0:
            _time2.sleep(max(0, delay))

        with _gop_sched_lock:
            sched = _gop_scheduled.get(sched_id, {})
            if sched.get("status") == "cancelled":
                return
            sched["status"] = "running"

        # Create job entry first
        job_id     = str(uuid.uuid4())[:8]
        started_at = datetime.datetime.utcnow().isoformat() + "Z"
        with _gop_lock:
            _gop_jobs[job_id] = {
                "job_id":     job_id,
                "status":     "running",
                "started_at": started_at,
                "ended_at":   None,
                "url":        url_display,
                "tag":        tag,
                "result":     None,
                "log":        [],
            }
        with _gop_sched_lock:
            if sched_id in _gop_scheduled:
                _gop_scheduled[sched_id]["job_id"] = job_id

        # Run analysis — passphrase already injected into url
        _run_gop_analysis(job_id, url, duration, "", tag)

        # After completion, mark result as scheduled in the saved JSON
        with _gop_lock:
            job = _gop_jobs.get(job_id, {})
            res_file = job.get("res_file")
        if res_file:
            res_path = os.path.join(GOP_DIR, res_file)
            try:
                with open(res_path) as f:
                    d = json.load(f)
                d["is_scheduled"] = True
                d["sched_id"]     = sched_id
                with open(res_path, "w") as f:
                    json.dump(d, f, indent=2)
            except Exception:
                pass

        with _gop_sched_lock:
            if sched_id in _gop_scheduled:
                _gop_scheduled[sched_id]["status"] = "done"

    threading.Thread(target=_wait_and_run, daemon=True).start()
    return jsonify({"sched_id": sched_id, "run_at_utc": run_dt.isoformat() + "Z"})


@app.route("/gop/schedule", methods=["GET"])
def gop_schedule_list():
    with _gop_sched_lock:
        items = list(_gop_scheduled.values())
    return jsonify(items)


@app.route("/gop/schedule/<sched_id>/cancel", methods=["POST"])
def gop_schedule_cancel(sched_id):
    ok, err = _check_password(request)
    if not ok:
        return err
    with _gop_sched_lock:
        sched = _gop_scheduled.get(sched_id)
        if not sched:
            return jsonify({"error": "Scheduled job not found"}), 404
        if sched["status"] not in ("pending",):
            return jsonify({"error": f"Cannot cancel job with status '{sched['status']}'"}), 400
        sched["status"] = "cancelled"
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════
#  SPECS EDITOR (specs.json)
# ═══════════════════════════════════════════════════════════
SPECS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specs.json")

DEFAULT_SPECS = {
    "overall_br":   {"lo": 5.0,  "hi": 18.0, "pref_lo": 8.0,  "pref_hi": 15.0, "label": "Overall Bitrate (Mbps)"},
    "gop_size":     {"values": [30, 50], "tolerance": 3, "label": "GOP Size (frames)", "allow_seconds": True},
    "gop_type":     {"required": "CLOSED", "label": "GOP Type"},
    "b_frames":     {"preferred": "absent", "label": "B-Frames"},
    "idr":          {"required": True, "label": "IDR Frames"},
    "frame_size":   {"values": ["1280x720","1920x1080"], "preferred": "1920x1080", "label": "Frame Size"},
    "aspect_ratio": {"values": ["16:9"], "label": "Aspect Ratio"},
    "chroma":       {"values": ["4:2:0"], "label": "Chroma Subsampling"},
    "scan_type":    {"values": ["progressive","interlaced","mbaff"], "preferred": "interlaced", "label": "Scan Type"},
    "bit_depth":    {"values": ["8"], "label": "Bit Depth"},
    "colour_gamut": {"values": ["unknown","bt709"], "preferred": "bt709", "label": "Colour Gamut"},
    "codec":        {"values": ["h264","hevc"], "preferred": "h264", "label": "Coding Algorithm"},
    "codec_level":  {"lo": 4.0, "hi": 4.2, "pref_lo": 4.1, "pref_hi": 4.2, "label": "CODEC Level"},
    "codec_profile":{"values": ["main","high","constrained baseline","baseline"], "preferred": "high", "label": "CODEC Profile"},
    "entropy":      {"values": ["CABAC"], "label": "Entropy"},
    "rate_ctrl_v":  {"values": ["VBR","CBR"], "preferred": "CBR", "label": "Rate Control (Video)"},
    "v_br":         {"lo": 5.0, "hi": 18.0, "pref_lo": 8.0, "pref_hi": 15.0, "label": "Video Bitrate (Mbps)"},
    "hdr_scheme":   {"values": ["SDR"], "label": "SDR/HDR Scheme"},
    "fps":          {"values": [25.0, 29.97, 30.0], "preferred": 25.0, "label": "Frame Rate",
                     "allow_50p_720": True},
    "a_codec":      {"values": ["AAC-LC","AAC-LATM","AAC-HE","MP1","MP2"], "preferred": "AAC-LC", "label": "Audio Coding"},
    "a_streams":    {"lo": 1, "hi": 32, "pref_lo": 2, "pref_hi": 2, "label": "Audio Streams"},
    "a_channels":   {"lo": 2, "hi": 2, "label": "Audio Channels"},
    "a_rate_ctrl":  {"values": ["VBR","CBR"], "preferred": "CBR", "label": "Audio Rate Control"},
    "a_sample_rate":{"lo": 44.1, "hi": 48.0, "pref_lo": 48.0, "pref_hi": 48.0, "label": "Sample Rate (kHz)"},
    "a_bits":       {"values": ["fltp","16","s16"], "preferred": "16", "label": "Audio Bits per Sample"},
    "a_br_kbps":    {"lo": 118, "hi": 512, "pref_lo": 256, "pref_hi": 256, "label": "Audio Bitrate (Kbps)"},
}

def _load_specs():
    """Load specs.json and deep-merge with DEFAULT_SPECS so no field is lost."""
    import copy
    base = copy.deepcopy(DEFAULT_SPECS)
    if os.path.isfile(SPECS_FILE):
        try:
            with open(SPECS_FILE) as f:
                saved = json.load(f)
            # Deep merge: for each key, update the dict rather than replace it
            for key, val in saved.items():
                if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                    base[key].update(val)   # merge field-by-field
                else:
                    base[key] = val
        except Exception:
            pass
    return base

def _save_specs(incoming):
    """Deep-merge incoming with defaults then save complete specs.json."""
    import copy
    merged = copy.deepcopy(DEFAULT_SPECS)
    for key, val in incoming.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key].update(val)
        else:
            merged[key] = val
    with open(SPECS_FILE, "w") as f:
        json.dump(merged, f, indent=2)

@app.route("/gop/specs", methods=["GET"])
def gop_specs_get():
    return jsonify(_load_specs())

@app.route("/gop/specs", methods=["POST"])
def gop_specs_save():
    ok, err = _check_password(request)
    if not ok:
        return err
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No specs data provided"}), 400
    try:
        _save_specs(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/gop/specs/reset", methods=["POST"])
def gop_specs_reset():
    ok, err = _check_password(request)
    if not ok:
        return err
    try:
        if os.path.isfile(SPECS_FILE):
            os.remove(SPECS_FILE)
        return jsonify({"success": True, "specs": DEFAULT_SPECS})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5050, threaded=True)
