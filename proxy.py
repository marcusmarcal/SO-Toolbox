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

from routes_bte import bte_bp
app.register_blueprint(bte_bp)

from routes_mtr import mtr_bp, mtr_running_items
app.register_blueprint(mtr_bp)

from routes_env import env_bp
app.register_blueprint(env_bp)

from routes_mtr_remote import mtr_remote_bp
app.register_blueprint(mtr_remote_bp)

from routes_adhoc import adhoc_bp
app.register_blueprint(adhoc_bp)

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
    for m in mtr_running_items():
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
                "tag": (j.get("cluster") or "").upper(), "user": j.get("created_by", ""),
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
