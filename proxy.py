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
        SAFE_PREFIXES = ("TOOL_", "SRT_SERVER_")
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
    """Stream mtr output line by line using Server-Sent Events.
    Supports two modes:
      - packets: mtr --report --report-cycles N
      - time:    mtr runs for N seconds via timeout command
    At the end, saves a structured JSON summary to mtr-results/.
    """
    import subprocess, os, json, re, datetime

    host     = (request.args.get("host") or "").strip()
    mode     = request.args.get("mode") or "packets"   # "packets" | "time"
    count    = max(1, min(int(request.args.get("count") or 50), 500))
    seconds  = max(10, min(int(request.args.get("seconds") or 60), 86400))
    no_dns   = request.args.get("no_dns") == "1"
    src_ip   = request.args.get("src_ip") or "unknown"
    pub_ip   = request.args.get("pub_ip") or "unknown"

    if not host:
        def err():
            yield "data: ERROR: Host is required.\n\n"
            yield "data: __DONE__\n\n"
        return Response(err(), content_type="text/event-stream")

    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    os.makedirs(results_dir, exist_ok=True)

    if mode == "time":
        cmd = ["timeout", str(seconds),
               "mtr", "--report-wide", "--interval", "1",
               "--report-cycles", str(seconds)]
    else:
        cmd = ["mtr", "--report", "--report-wide",
               "--report-cycles", str(count)]

    if no_dns:
        cmd.append("--no-dns")
    cmd.append(host)

    def parse_mtr_output(lines):
        """Parse mtr --report output into structured hops."""
        hops = []
        for line in lines:
            # Match lines like: |-- 1.2.3.4   0.0%  50  1.2  2.3  0.8  5.1  0.6
            m = re.match(
                r'\s*\d+\.\s*[|`]-+\s*(\S+)\s+'   # hop num + host
                r'([\d.]+)%\s+'                    # loss%
                r'(\d+)\s+'                        # sent
                r'([\d.]+)\s+'                     # last
                r'([\d.]+)\s+'                     # avg
                r'([\d.]+)\s+'                     # best
                r'([\d.]+)',                        # worst
                line
            )
            if m:
                hops.append({
                    "host":   m.group(1),
                    "loss":   float(m.group(2)),
                    "sent":   int(m.group(3)),
                    "last":   float(m.group(4)),
                    "avg":    float(m.group(5)),
                    "best":   float(m.group(6)),
                    "worst":  float(m.group(7)),
                })
        return hops

    def generate():
        lines      = []
        started_at = datetime.datetime.utcnow().isoformat() + "Z"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1
            )
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="replace").rstrip()
                lines.append(line)
                if line:
                    yield f"data: {line}\n\n"
            proc.wait()
        except FileNotFoundError:
            yield "data: ERROR: mtr is not installed.\n\n"
            yield "data: Install: apt install mtr-tiny  OR  yum install mtr\n\n"
            yield "data: __DONE__\n\n"
            return
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: __DONE__\n\n"
            return

        # Save JSON result
        try:
            hops       = parse_mtr_output(lines)
            ended_at   = datetime.datetime.utcnow().isoformat() + "Z"
            ts         = ended_at[:19].replace(":", "-").replace("T", "_")
            safe_host  = re.sub(r"[^\w\.\-]", "_", host)
            filename   = f"{ts}_{safe_host}.json"
            filepath   = os.path.join(results_dir, filename)

            result = {
                "started_at":  started_at,
                "ended_at":    ended_at,
                "source_ip":   src_ip,
                "public_ip":   pub_ip,
                "destination": host,
                "mode":        mode,
                "packets":     count if mode == "packets" else None,
                "duration_s":  seconds if mode == "time" else None,
                "no_dns":      no_dns,
                "hops":        hops,
                "raw":         "\n".join(lines)
            }

            with open(filepath, "w") as f:
                json.dump(result, f, indent=2)

            yield f"data: \n\n"
            yield f"data: ✔ Result saved to mtr-results/{filename}\n\n"
        except Exception as e:
            yield f"data: ⚠ Could not save result: {e}\n\n"

        yield "data: __DONE__\n\n"

    return Response(generate(), content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/mtr/results", methods=["GET"])
def mtr_results():
    """List saved MTR result files."""
    import os, json
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(base_dir, "mtr-results")
    if not os.path.isdir(results_dir):
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(results_dir) if f.endswith(".json")],
        reverse=True
    )[:50]  # last 50
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
    """Background thread: run analysis, then generate PDF."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        with _ingest_lock:
            _ingest_jobs[job_id]["log"] = list(log_lines)

    try:
        log(f"Starting analysis for: {url}")
        result = subprocess.run(
            ["run-ingest-analysis.sh", url, output_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600   # 10 min hard limit
        )
        stdout = result.stdout.decode(errors="replace")
        for line in stdout.splitlines():
            log(line)

        exit_code = result.returncode
        log(f"Script exited with code {exit_code}")

        # Find zip produced by the script
        zip_path = None
        for f in os.listdir(os.path.dirname(output_dir)):
            if f.endswith(".zip") and os.path.basename(output_dir) in f:
                zip_path = os.path.join(os.path.dirname(output_dir), f)
                break
        # Also check output_dir itself
        if not zip_path:
            parent = os.path.dirname(output_dir)
            for f in os.listdir(parent):
                if f.endswith(".zip"):
                    zip_path = os.path.join(parent, f)
                    break

        # Copy zip to ingest-results
        saved_zip = None
        if zip_path and os.path.isfile(zip_path):
            dest = os.path.join(INGEST_RESULTS_DIR, os.path.basename(zip_path))
            shutil.copy2(zip_path, dest)
            saved_zip = os.path.basename(zip_path)
            log(f"ZIP saved: {saved_zip}")

        # Generate PDF from index.html using weasyprint
        saved_pdf = None
        html_report = os.path.join(output_dir, "index.html")
        if os.path.isfile(html_report):
            pdf_name = os.path.basename(output_dir) + ".pdf"
            pdf_path = os.path.join(INGEST_RESULTS_DIR, pdf_name)
            try:
                pr = subprocess.run(
                    ["weasyprint", html_report, pdf_path],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120
                )
                if pr.returncode == 0 and os.path.isfile(pdf_path):
                    saved_pdf = pdf_name
                    log(f"PDF saved: {saved_pdf}")
                else:
                    log(f"weasyprint failed: {pr.stdout.decode(errors='replace')}")
            except FileNotFoundError:
                log("weasyprint not installed — skipping PDF. Install: pip3 install weasyprint")
            except Exception as e:
                log(f"PDF error: {e}")

        # Read summary from report.json if available
        summary = {}
        json_report = os.path.join(output_dir, "report.json")
        if os.path.isfile(json_report):
            try:
                with open(json_report) as f:
                    summary = json.load(f)
            except Exception:
                pass

        with _ingest_lock:
            _ingest_jobs[job_id].update({
                "status":     "done" if exit_code in (0, 45) else "failed",
                "exit_code":  exit_code,
                "ended_at":   datetime.datetime.utcnow().isoformat() + "Z",
                "zip":        saved_zip,
                "pdf":        saved_pdf,
                "summary":    summary,
                "log":        log_lines,
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
    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id     = str(uuid.uuid4())[:8]
    ts         = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_host  = re.sub(r"[^\w\-]", "_", url)[:40]
    output_dir = f"/tmp/ingest-analyses/{ts}_{safe_host}"

    with _ingest_lock:
        _ingest_jobs[job_id] = {
            "job_id":     job_id,
            "status":     "running",
            "url":        url,
            "started_at": datetime.datetime.utcnow().isoformat() + "Z",
            "ended_at":   None,
            "zip":        None,
            "pdf":        None,
            "summary":    {},
            "log":        [],
            "output_dir": output_dir,
        }

    t = threading.Thread(target=_run_ingest, args=(job_id, url, output_dir), daemon=True)
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
    """List saved ingest results (ZIP + PDF pairs)."""
    files = sorted(os.listdir(INGEST_RESULTS_DIR), reverse=True)
    zips  = [f for f in files if f.endswith(".zip")]
    items = []
    for z in zips[:30]:
        pdf = z.replace(".zip", ".pdf")
        items.append({
            "zip": z,
            "pdf": pdf if pdf in files else None,
            "name": z.replace(".zip", ""),
        })
    return jsonify(items)


@app.route("/ingest/download/<path:filename>", methods=["GET"])
def ingest_download(filename):
    from flask import send_from_directory
    return send_from_directory(INGEST_RESULTS_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    # threaded=True permite lidar com várias requisições ao mesmo tempo
    app.run(host='0.0.0.0', port=5050, threaded=True)
