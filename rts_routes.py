import base64
import requests
from flask import Blueprint, request, jsonify, Response
from urllib.parse import quote

rts_bp = Blueprint("rts", __name__)

# Shared session (reuse the one from the app if injected, else create local)
# To share the proxy.py session, call rts_bp.session = session after import.
_session = requests.Session()

PHENIX_BASE = "https://pcast.phenixrts.com"


def _get_session():
    """Return the shared requests.Session (set by proxy.py after registration)."""
    return getattr(rts_bp, "session", _session)


def _make_auth_header(app_id: str, password: str) -> str:
    credentials = f"{app_id}:{password}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


@rts_bp.route("/channels", methods=["GET"])
def get_channels():
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing headers"}), 400
    try:
        resp = _get_session().get(
            f"{PHENIX_BASE}/pcast/channels",
            headers={
                "Authorization": _make_auth_header(app_id, password),
                "Accept": "application/json",
            },
            timeout=15,
        )
        return Response(resp.content, status=resp.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@rts_bp.route("/publishers/count/<path:channel_id>", methods=["GET"])
def get_publishers_count(channel_id):
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    try:
        encoded_id = quote(channel_id, safe="")
        resp = _get_session().get(
            f"{PHENIX_BASE}/pcast/channel/{encoded_id}/publishers/count",
            headers={
                "Authorization": _make_auth_header(app_id, password),
                "Accept": "application/json",
            },
            timeout=10,
        )
        return Response(resp.text, status=resp.status_code, content_type="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@rts_bp.route("/rts/viewing-report", methods=["POST"])
def rts_viewing_report():
    """Proxy for the Phenix RTS viewing report endpoint.
    Expects JSON body: { channel_alias, start, end }
    Returns the raw CSV from Phenix.
    """
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing credentials headers"}), 400

    data          = request.get_json(silent=True) or {}
    channel_alias = (data.get("channel_alias") or "").strip()
    start         = (data.get("start") or "").strip()
    end           = (data.get("end") or "").strip()

    if not channel_alias or not start or not end:
        return jsonify({"error": "channel_alias, start and end are required"}), 400

    payload = {
        "viewingReport": {
            "kind": "RealTime",
            "channelAliases": [channel_alias],
            "start": start,
            "end": end,
        }
    }

    try:
        resp = _get_session().put(
            f"{PHENIX_BASE}/pcast/reporting/viewing",
            auth=(app_id, password),
            headers={
                "Accept": "text/csv",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "text/csv"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
