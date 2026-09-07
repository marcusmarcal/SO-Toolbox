import base64
import requests
import routes_auth
import routes_gop
import routes_rota

from routes_auth import require_admin_role

from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)

from id3as_routes import id3as_bp
app.register_blueprint(id3as_bp)

from rts_routes import rts_bp
app.register_blueprint(rts_bp)

from routes_srt import srt_bp
app.register_blueprint(srt_bp)

from wc2026_routes import wc2026_bp
app.register_blueprint(wc2026_bp)

from routes_txcore import txcore_bp
app.register_blueprint(txcore_bp)

from routes_live_probe import live_probe_bp
app.register_blueprint(live_probe_bp)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit
CORS(app)

# Usar Session melhora MUITO a performance para múltiplas requisições
session = requests.Session()
PHENIX_BASE = "https://pcast.phenixrts.com"

# Share the session with the RTS blueprint so it reuses the same connection pool
rts_bp.session = session

def make_auth_header(app_id, password):
    credentials = f"{app_id}:{password}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


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
@require_admin_role
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

@app.route("/git-branch", methods=["GET"])
def git_branch():
    import subprocess, os

    repo_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )

        branch = result.stdout.decode().strip()
        error = result.stderr.decode().strip()

        return jsonify({
            "success": result.returncode == 0,
            "branch": branch if result.returncode == 0 else None,
            "output": error if result.returncode != 0 else ""
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "branch": None,
            "output": str(e)
        }), 500


@app.route("/restart-proxy", methods=["POST"])
@require_admin_role
def restart_proxy():
    import subprocess
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



def _mtr_running_items():
    """Return list of currently running MTR jobs (from .running.json files).
    Shared by /mtr/running and /proxy/activity."""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    if not os.path.isdir(results_dir):
        return []
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
    return items


@app.route("/mtr/running", methods=["GET"])
def mtr_running():
    """Return list of currently running MTR jobs (from .running.json files)."""
    return jsonify(_mtr_running_items())


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

INGEST_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store/ingest-results")
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
    """List saved ingest results based on directories, reading meta.json for tag/url/dates."""
    try:
        all_entries = os.listdir(INGEST_RESULTS_DIR)
    except Exception:
        return jsonify([])

    # Filter only actual directories
    dirs = []
    for entry in all_entries:
        full_path = os.path.join(INGEST_RESULTS_DIR, entry)
        if os.path.isdir(full_path):
            dirs.append(entry)
    
    # Sort directories in descending order (newest first)
    dirs = sorted(dirs, reverse=True)
    
    items = []
    # Limit to the 50 most recent directories
    for dirname in dirs[:50]:
        # Check if the corresponding .zip file exists
        zip_name = f"{dirname}.zip"
        has_zip = zip_name in all_entries
        
        meta = {}
        meta_path = os.path.join(INGEST_RESULTS_DIR, dirname, "meta.json")
        
        # Try to read meta.json
        if os.path.isfile(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass
                
        # Fallback: try report.json if meta is empty
        if not meta:
            rj = os.path.join(INGEST_RESULTS_DIR, dirname, "report.json")
            if os.path.isfile(rj):
                try:
                    with open(rj) as f:
                        meta = {"tag": json.load(f).get("tag", "")}
                except Exception:
                    pass
                    
        items.append({
            "zip":        zip_name if has_zip else None,
            "dir":        dirname,
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



# ════════════════════════════════════════════════════════════════════════════
#  PROXY ACTIVITY — aggregated view of background jobs running inside the proxy
#  Consumed by index.html (sidebar indicators + Proxy Management panel).
# ════════════════════════════════════════════════════════════════════════════
import time as _act_time
import routes_srt as _act_srt
import routes_live_probe as _act_live_probe
import routes_txcore as _act_txcore
from routes_auth import require_auth

_PASSPHRASE_RE = re.compile(r'[?&]passphrase=[^&]*', re.IGNORECASE)


def _act_strip_secrets(text):
    """Remove passphrases from URLs/labels before exposing them to the UI."""
    if not text:
        return ""
    return _PASSPHRASE_RE.sub("", str(text)).rstrip("?&")


def _act_elapsed_iso(iso):
    """Seconds elapsed since an ISO-8601 UTC timestamp (with or without Z)."""
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "")
        if "+" in s[10:]:
            s = s[:s.index("+", 10)]
        st = datetime.datetime.fromisoformat(s)
        return max(0, int((datetime.datetime.utcnow() - st).total_seconds()))
    except Exception:
        return None


def _act_group(key, tool, icon, match, jobs):
    return {
        "key":   key,
        "tool":  tool,
        "icon":  icon,
        # Lower-case substrings matched by the UI against TOOL_n name/file/badge
        "match": match,
        "count": len(jobs),
        "jobs":  jobs,
    }


def _act_video_analyzer():
    jobs = []
    with routes_gop._gop_lock:
        for j in routes_gop._gop_jobs.values():
            if j.get("status") == "running":
                jobs.append({
                    "id": j.get("job_id"), "kind": "analysis", "status": "running",
                    "label": _act_strip_secrets(j.get("url")), "tag": j.get("tag", ""),
                    "user": j.get("username", ""), "started_at": j.get("started_at"),
                    "elapsed_s": _act_elapsed_iso(j.get("started_at")),
                    "extra": {"workflow": j.get("workflow", "")},
                })
    with routes_gop._rec_lock:
        for j in routes_gop._rec_jobs.values():
            if j.get("status") == "running":
                jobs.append({
                    "id": j.get("job_id"), "kind": "recording", "status": "running",
                    "label": _act_strip_secrets(j.get("url")), "tag": j.get("tag", ""),
                    "user": j.get("username", ""), "started_at": j.get("started_at"),
                    "elapsed_s": _act_elapsed_iso(j.get("started_at")),
                    "extra": {},
                })
    with routes_gop._gop_sched_lock:
        for s in routes_gop._gop_scheduled.values():
            if s.get("status") in ("pending", "running"):
                jobs.append({
                    "id": s.get("sched_id"), "kind": "scheduled", "status": s.get("status"),
                    "label": _act_strip_secrets(s.get("url")), "tag": s.get("tag", ""),
                    "user": s.get("username", ""), "started_at": None, "elapsed_s": None,
                    "extra": {"run_at_utc": s.get("run_at_utc"), "workflow": s.get("workflow", "")},
                })
    return _act_group("video_analyzer", "Video Analyzer", "🎞",
                      ["video analy", "video-analy", "gop"], jobs)


def _act_live_probe_sessions():
    jobs = []
    now = _act_time.time()
    with _act_live_probe._sessions_lock:
        sessions = list(_act_live_probe._sessions.values())
    for s in sessions:
        alive = s.proc is not None and s.proc.poll() is None
        jobs.append({
            "id": s.id, "kind": "probe", "status": "running" if alive else "connecting",
            "label": f"srt://{s.host}:{s.port}", "tag": getattr(s, "tag", "") or "",
            "user": getattr(s, "username", "") or "",
            "started_at": datetime.datetime.utcfromtimestamp(s.created_at).isoformat() + "Z",
            "elapsed_s": max(0, int(now - s.created_at)),
            "extra": {"viewers": len(s.subscribers)},
        })
    return _act_group("live_probe", "Live Probe", "📶",
                      ["live probe", "live-probe", "liveprobe"], jobs)


def _act_srt_ingest():
    active = ("running", "reconnecting", "starting", "stopping")
    jobs = []
    with _act_srt._jobs_lock:
        for j in _act_srt._running_jobs.values():
            if j.get("status") not in active:
                continue
            label = f"{j.get('host')}:{j.get('port')}"
            if j.get("type") == "shared" and j.get("destinations"):
                label = f"{len(j['destinations'])} destinations"
            src = os.path.basename(str(j.get("input_file") or "")) if j.get("source_mode") == "file" else j.get("source_mode", "")
            jobs.append({
                "id": j.get("id"), "kind": j.get("mode", "ingest"), "status": j.get("status"),
                "label": label, "tag": "", "user": "",
                "started_at": None, "elapsed_s": None,
                "extra": {"source": src, "pid": j.get("pid"), "retries": j.get("retry_count", 0)},
            })
    return _act_group("srt_ingest", "SRT Ingest", "📡",
                      ["srt ingest", "srt-ingest", "srt_ingest"], jobs)


def _act_ingest_analyzer():
    jobs = []
    with _ingest_lock:
        for j in _ingest_jobs.values():
            if j.get("status") == "running":
                jobs.append({
                    "id": j.get("job_id"), "kind": "analysis", "status": "running",
                    "label": _act_strip_secrets(j.get("url")), "tag": j.get("tag", ""),
                    "user": "", "started_at": j.get("started_at"),
                    "elapsed_s": _act_elapsed_iso(j.get("started_at")),
                    "extra": {},
                })
    return _act_group("ingest_analyzer", "Ingest Analyzer", "🧪",
                      ["ingest analy", "ingest-analy", "ingest_analy"], jobs)


def _act_mtr():
    jobs = []
    for m in _mtr_running_items():
        jobs.append({
            "id": m.get("job_id"), "kind": m.get("mode") or "trace", "status": "running",
            "label": m.get("destination", ""), "tag": m.get("tag", ""), "user": "",
            "started_at": m.get("started_at"), "elapsed_s": m.get("elapsed"),
            "extra": {"remaining_s": m.get("remaining"), "proto": m.get("proto")},
        })
    return _act_group("mtr", "MTR", "🛰", ["mtr"], jobs)


def _act_txcore():
    jobs = []
    jobs_dir = getattr(_act_txcore, "JOBS_DIR", None)
    if jobs_dir and os.path.isdir(jobs_dir):
        for f in os.listdir(jobs_dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(jobs_dir, f)) as fh:
                    j = json.load(fh)
            except Exception:
                continue
            if j.get("status") not in ("queued", "running"):
                continue
            params = j.get("params") or {}
            jobs.append({
                "id": j.get("job_id"), "kind": "channel-create", "status": j.get("status"),
                "label": f"{j.get('progress', 0)}/{j.get('total', 0)} channels",
                "tag": "", "user": j.get("created_by", ""),
                "started_at": j.get("started_at") or j.get("created_at"),
                "elapsed_s": _act_elapsed_iso(j.get("started_at") or j.get("created_at")),
                "extra": {"dry_run": bool(params.get("dry_run"))},
            })
    return _act_group("txcore", "TXCore", "📺", ["txcore", "tx core", "tx-core"], jobs)


_PUSH_STATE_CACHE = {"ts": 0.0, "state": None}


def _act_push_unit_state():
    """systemd state of the srt-push unit, cached for 5s (the endpoint is polled
    by every open browser and each call goes through sudo systemctl)."""
    now = _act_time.time()
    if _PUSH_STATE_CACHE["state"] is None or now - _PUSH_STATE_CACHE["ts"] > 5:
        _PUSH_STATE_CACHE["state"] = _act_srt._push_service_state()
        _PUSH_STATE_CACHE["ts"] = now
    return _PUSH_STATE_CACHE["state"]


def _act_srt_push():
    """SRT Push runs as its own systemd unit (srt-push.py); it reports per-service
    status via srt-push-stats.json. Only services actively pushing are listed."""
    jobs = []
    unit = _act_push_unit_state()
    if unit.get("active_state") == "active":
        stats  = (_act_srt._load_push_stats() or {}).get("services", {}) or {}
        config = {s.get("id"): s for s in (_act_srt._load_push_config() or {}).get("services", [])}
        for sid, st in stats.items():
            status = st.get("service_status", "")
            if status not in ("running", "starting"):
                continue
            cfg = config.get(sid, {})
            jobs.append({
                "id": sid, "kind": cfg.get("source_type", "push"), "status": status,
                "label": f"{cfg.get('name', sid)} → {cfg.get('srt_host', '?')}:{cfg.get('srt_port', '?')}",
                "tag": "", "user": "",
                "started_at": st.get("started_at"),
                "elapsed_s": _act_elapsed_iso(st.get("started_at")),
                "extra": {"pid": st.get("ffmpeg_pid"), "bitrate": st.get("bitrate"),
                          "fps": st.get("fps"), "unit": unit.get("sub_state")},
            })
    return _act_group("srt_push", "SRT Push", "📤",
                      ["srt push", "srt-push", "srt_push", "push control"], jobs)


@app.route("/proxy/activity", methods=["GET"])
@require_auth
def proxy_activity():
    """Aggregate every active background job the proxy is currently running,
    plus the SRT Push systemd unit. Read-only; safe for all authenticated users
    (secrets are stripped)."""
    groups = []
    for collector in (_act_video_analyzer, _act_live_probe_sessions, _act_srt_ingest,
                      _act_srt_push, _act_ingest_analyzer, _act_mtr, _act_txcore):
        try:
            groups.append(collector())
        except Exception as e:  # one broken collector must not hide the others
            groups.append({"key": collector.__name__, "tool": collector.__name__,
                           "icon": "⚠", "match": [], "count": 0, "jobs": [],
                           "error": str(e)})
    total = sum(g["count"] for g in groups)
    return jsonify({
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "total_active": total,
        "groups": groups,
    })


@app.route("/server-stats", methods=["GET"])
def server_stats():
    import subprocess, re
    try:
        # CPU usage — parse idle% via regex, handles any field order
        cpu_result = subprocess.run(
            ["top", "-bn1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        cpu_usage = 0

        for line in cpu_result.stdout.decode().split('\n'):
            if 'Cpu(s)' in line or 'cpu' in line.lower():
                match = re.search(r'(\d+(?:\.\d+)?)\s*id', line)

                if match:
                    idle = float(match.group(1))
                    cpu_usage = round(100 - idle, 2)

                break

        # Memory usage
        mem_result = subprocess.run(
            ["free", "-b"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        mem_usage = 0
        for line in mem_result.stdout.decode().split('\n'):
            if 'Mem:' in line:
                parts = line.split()
                total = float(parts[1])
                used = float(parts[2])
                mem_usage = (used / total) * 100
                break

        # Disk usage
        disk_result = subprocess.run(
            ["df", "-B1", "/opt/web/store"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        disk_usage = 0
        disk_lines = disk_result.stdout.decode().split('\n')
        if len(disk_lines) > 1:
            parts = disk_lines[1].split()
            total = float(parts[1])
            used = float(parts[2])
            disk_usage = (used / total) * 100

        return jsonify({
            "cpu": round(cpu_usage, 1),
            "memory": round(mem_usage, 1),
            "disk": round(disk_usage, 1)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

routes_auth.register_routes(app)
routes_gop.register_routes(app)

routes_rota.register_routes(app)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5050, threaded=True)
