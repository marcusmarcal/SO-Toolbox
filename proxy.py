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

if __name__ == "__main__":
    # threaded=True permite lidar com várias requisições ao mesmo tempo
    app.run(host='0.0.0.0', port=5050, threaded=True)