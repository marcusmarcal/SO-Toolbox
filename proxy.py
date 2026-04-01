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
    """Stream mtr output line by line using Server-Sent Events."""
    import subprocess, shlex

    host   = (request.args.get("host") or "").strip()
    count  = max(1, min(int(request.args.get("count") or 50), 200))
    no_dns = request.args.get("no_dns") == "1"

    if not host:
        def err():
            yield "data: ERROR: Host is required.\n\n"
            yield "data: __DONE__\n\n"
        return Response(err(), content_type="text/event-stream")

    cmd = ["mtr", "--report", "--report-cycles", str(count), "--report-wide"]
    if no_dns:
        cmd.append("--no-dns")
    cmd.append(host)

    def generate():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1
            )
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="replace").rstrip()
                if line:
                    yield f"data: {line}\n\n"
            proc.wait()
        except FileNotFoundError:
            yield "data: ERROR: mtr is not installed.\n\n"
            yield "data: Install it: apt install mtr-tiny  OR  yum install mtr\n\n"
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
        yield "data: __DONE__\n\n"

    return Response(generate(), content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


if __name__ == "__main__":
    # threaded=True permite lidar com várias requisições ao mesmo tempo
    app.run(host='0.0.0.0', port=5050, threaded=True)