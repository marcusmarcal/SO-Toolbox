"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy endpoints.

Register in proxy.py:
    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)

Reads PRFAUTH from .env — never sent to the browser.
DC routing:
    ix → id3as-ix.performgroup.co.uk
    eq → id3as-eq.performgroup.co.uk

Endpoints (all under /id3as/<dc>/):
    GET /id3as/<dc>/channels/<variant>          channel list
    GET /id3as/<dc>/flags/channels              active warnings
    GET /id3as/<dc>/running_events              active events
    GET /id3as/<dc>/nodes                       node list
    GET /id3as/<dc>/logs                        today's system events (UTC)
    GET /id3as/<dc>/logs/<year>/<month>/<day>   specific date logs
    GET /id3as/<dc>/channel/<ch_id>/status      single channel enc/src state
"""

import os
import re
import json
import html as _html
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
from urllib.parse import quote as url_quote

import requests
from flask import Blueprint, jsonify, Response

# ── Blueprint ────────────────────────────────────────────────────────────────

id3as_bp = Blueprint("id3as", __name__)

_SESSION = requests.Session()

ID3AS_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

# ── Auth helper ──────────────────────────────────────────────────────────────

def _read_prfauth():
    # type: () -> Optional[str]
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "PRFAUTH":
                    return v.strip().strip("'").strip('"')
    except Exception:
        pass
    return None


# ── Core fetch ───────────────────────────────────────────────────────────────

def _id3as_get(dc, path):
    host = ID3AS_DC_HOSTS.get(dc)
    if not host:
        return None, (jsonify({"error": "Unknown DC: {}".format(dc)}), 400)

    token = _read_prfauth()
    if not token:
        return None, (jsonify({"error": "PRFAUTH not set in .env"}), 500)

    url = "https://{}/ctl/api/data/{}".format(host, path.lstrip("/"))
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=20,
            verify=True,
        )
        if resp.status_code == 500 and "running_events" in path:
            return [], None
        if resp.status_code != 200:
            return None, (
                jsonify({"error": "Upstream {}".format(resp.status_code), "url": url}),
                resp.status_code,
            )
        return resp.json(), None
    except requests.exceptions.ConnectionError as exc:
        return None, (jsonify({"error": "Connection error: {}".format(exc)}), 502)
    except requests.exceptions.Timeout:
        return None, (jsonify({"error": "Request timed out"}), 504)
    except Exception as exc:
        return None, (jsonify({"error": str(exc)}), 500)


# ── Routes ────────────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    data, err = _id3as_get(dc, "channels?variant={}".format(variant))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    data, err = _id3as_get(dc, "flags/channels")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    data, err = _id3as_get(dc, "running_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    data, err = _id3as_get(dc, "nodes")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    data, err = _id3as_get(dc, "channel/{}/status".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data)


@id3as_bp.route("/id3as/<dc>/logs", methods=["GET"])
@id3as_bp.route("/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>", methods=["GET"])
def id3as_logs(dc, year=None, month=None, day=None):
    if year is None:
        now = datetime.now(timezone.utc)
        year, month, day = now.year, now.month, now.day

    token = _read_prfauth()
    host  = ID3AS_DC_HOSTS.get(dc)

    if not host or not token:
        return jsonify({"error": "PRFAUTH not set or unknown DC"}), 500

    url = "https://{}/ctl/api/data/system_events/{}/{}/{}".format(host, year, month, day)
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=20,
            verify=True,
        )
        raw = resp.text
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    events = _parse_log_response(raw)
    return jsonify([e for e in events if isinstance(e, dict)])


# ── Log JSON normaliser ───────────────────────────────────────────────────────

def _parse_log_response(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except ValueError:
        pass
    txt = _html.unescape(raw).strip()
    txt = re.sub(r"}\s*{", "},{", txt)
    if not txt.startswith("["):
        txt = "[" + txt
    if not txt.endswith("]"):
        txt += "]"
    try:
        data = json.loads(txt)
        return data if isinstance(data, list) else [data]
    except ValueError:
        pass
    parts = re.split(r"}\s*,\s*{", txt.strip().strip("[]"))
    events = []
    for p in parts:
        obj = ("{" if not p.startswith("{") else "") + p + ("}" if not p.endswith("}") else "")
        try:
            events.append(json.loads(obj))
        except ValueError:
            pass
    return events
