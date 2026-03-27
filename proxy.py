import base64
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

session = requests.Session()
PHENIX_BASE = "https://pcast.phenixrts.com"

# 🔐 opcional: define um token simples para proteger o git-pull
GIT_PULL_TOKEN = "changeme"  # mete isto via env em produção!!

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
            headers={
                "Authorization": make_auth_header(app_id, password),
                "Accept": "application/json"
            },
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
            headers={
                "Authorization": make_auth_header(app_id, password),
                "Accept": "application/json"
            },
            timeout=10
        )
        return Response(resp.text, status=resp.status_code, content_type="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/config", methods=["GET"])
def get_config():
    import os
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        env = {}
        with open(env_path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()

        SAFE_PREFIXES = ("TOOL_", "SRT_SERVER_")
        SAFE_KEYS = ("APP_TITLE", "APP_VERSION", "SRT_PASSPHRASE")

        safe = {
            k: v for k, v in env.items()
            if k in SAFE_KEYS or any(k.startswith(p) for p in SAFE_PREFIXES)
        }

        return jsonify({"status": "ok", "config": safe})
    except FileNotFoundError:
        return jsonify({"status": "error", "message": ".env not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/git-pull", methods=["POST"])
def git_pull():
    import subprocess, os

    # 🔐 proteção simples por header
    token = request.headers.get("X-Token")
    if GIT_PULL_TOKEN and token != GIT_PULL_TOKEN:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

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

        return jsonify({
            "success": success,
            "output": output
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "output": str(e)
        }), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5050, threaded=True)