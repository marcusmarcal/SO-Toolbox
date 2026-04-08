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

        return jsonify({"status": "ok", "config": safe})
    except FileNotFoundError:
        return jsonify({"status": "error", "message": ".env not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



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
    import subprocess, os
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["systemctl", "restart", "phenix-proxy"],
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
    Start an MTR job in a background thread (survives browser disconnect)
    and stream SSE ticks to the browser while it runs.
    Job state is saved to disk so it persists across proxy restarts.
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

    if mode == "time":
        cmd = ["mtr", "--report", "--report-wide", "--interval", "1",
               "--report-cycles", str(seconds)]
    else:
        cmd = ["mtr", "--report", "--report-wide",
               "--report-cycles", str(count)]
    if no_dns:
        cmd.append("--no-dns")
    cmd.append(host)

    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    ts         = started_at[:19].replace(":", "-").replace("T", "_")
    safe_host  = re.sub(r"[^\w\.\-]", "_", host)
    job_id     = f"{ts}_{safe_host}"
    state_file = os.path.join(results_dir, f"{job_id}.running.json")
    done_file  = os.path.join(results_dir, f"{job_id}.json")

    # Write initial state to disk immediately
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

    def run_background():
        lines = []
        try:
            # bufsize=0 avoids the Python 3.9 binary-mode line-buffer warning
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
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

    t = threading.Thread(target=run_background, daemon=True)
    t.start()

    def stream_ticks():
        start = _time.time()
        last  = start
        # Stream ticks while job is running (state file exists)
        while os.path.isfile(state_file):
            _time.sleep(0.3)
            now = _time.time()
            if now - last >= 1.0:
                elapsed   = int(now - start)
                remaining = max(0, total_cycles - elapsed)
                last = now
                yield f"data: __TICK__ {elapsed} {remaining}\n\n"
        # Job finished — stream the raw output lines
        try:
            with open(done_file) as f:
                d = json.load(f)
            for line in (d.get("raw") or "").splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
            yield f"data: \n\n"
            yield f"data: ✔ Result saved to mtr-results/{job_id}.json\n\n"
        except Exception as e:
            yield f"data: ⚠ Could not read result: {e}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_ticks(), content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


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

    try:
        log(f"Starting analysis for: {url}")

        # Do NOT pass output_dir — let the script generate its own directory name.
        # This avoids issues with the script behaving differently when given an
        # external output path (perl -g flag, relative paths in generate-report.sh).
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
                # "Report location: /tmp/.../index.html"
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

        with _ingest_lock:
            _ingest_jobs[job_id].update({
                "status":    "done" if exit_code in (0, 45) else "failed",
                "exit_code": exit_code,
                "ended_at":  datetime.datetime.utcnow().isoformat() + "Z",
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
    """List saved ingest results (ZIP + report dir)."""
    files = set(os.listdir(INGEST_RESULTS_DIR))
    zips  = sorted([f for f in files if f.endswith(".zip")], reverse=True)
    items = []
    for z in zips[:30]:
        dirname = z.replace(".zip", "")
        has_dir = dirname in files and os.path.isdir(os.path.join(INGEST_RESULTS_DIR, dirname))
        # Try to read tag from the report.json inside the dir
        tag = ""
        if has_dir:
            rj = os.path.join(INGEST_RESULTS_DIR, dirname, "report.json")
            try:
                with open(rj) as f:
                    tag = json.load(f).get("tag", "")
            except Exception:
                pass
        items.append({
            "zip":  z,
            "dir":  dirname if has_dir else None,
            "name": dirname,
            "tag":  tag,
        })
    return jsonify(items)


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
