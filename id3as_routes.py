"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy endpoints.

IMPORTANT: Several id3as API endpoints return HTTP 500 even when they have
valid data. The proxy always attempts to parse the response body first,
and only falls back to [] if parsing fails or body is empty.

Register in proxy.py:
    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)
"""

import os
import re
import json
import html as _html
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote as url_quote

import requests
from flask import Blueprint, jsonify, Response

id3as_bp = Blueprint("id3as", __name__)

_SESSION = requests.Session()

ID3AS_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

ID3AS_PACKAGING_BASE = {
    "ix": "http://id3as.prod.ix.perform.local/ctl/api/packaging",
    "eq": "http://id3as.prod.eq.perform.local/ctl/api/packaging",
}


# ── Auth ────────────────────────────────────────────────────────────

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


# ── Core fetch ──────────────────────────────────────────────────────────

def _id3as_get(dc, path, expect_list=True):
    """
    Authenticated GET to id3as API.
    Critical behaviour: several endpoints return HTTP 500 even when they
    contain valid JSON data (API quirk). We ALWAYS attempt to parse the
    body regardless of status code. Only if the body is empty or
    unparseable AND the status is an error do we return an error response.
    
    Returns (data, status_code, None) on success, (None, None, flask_response) on hard error.
    """
    host = ID3AS_DC_HOSTS.get(dc)
    if not host:
        return None, None, (jsonify({"error": "Unknown DC: {}".format(dc)}), 400)

    token = _read_prfauth()
    if not token:
        return None, None, (jsonify({"error": "PRFAUTH not set in .env"}), 500)

    url = "https://{}/ctl/api/data/{}".format(host, path.lstrip("/"))
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=25,
            verify=True,
        )
    except requests.exceptions.ConnectionError as exc:
        return None, None, (jsonify({"error": "Connection error: {}".format(exc)}), 502)
    except requests.exceptions.Timeout:
        return None, None, (jsonify({"error": "Request timed out"}), 504)
    except Exception as exc:
        return None, None, (jsonify({"error": str(exc)}), 500)

    body = resp.text.strip() if resp.text else ""

    # Always try to parse the body first, regardless of HTTP status code.
    # The id3as API returns 500 even for valid data on several endpoints.
    if body:
        try:
            data = json.loads(body)
            # Normalise dict → list if needed
            if isinstance(data, dict) and expect_list:
                data = list(data.values())
            # ✅ IMPORTANT: Always return data if parsing succeeds!
            # Return 200 status explicitly even if upstream returned 500
            return data, 200, None
        except ValueError:
            pass

    # Body empty or unparseable — treat as error only if non-2xx
    if resp.status_code not in (200, 201, 204):
        return None, None, (
            jsonify({"error": "Upstream {}".format(resp.status_code), "url": url}),
            resp.status_code,
        )

    # 2xx but empty/invalid body
    return ([] if expect_list else {}), 200, None


def _packaging_get(dc, path):
    base = ID3AS_PACKAGING_BASE.get(dc)
    if not base:
        return None, (jsonify({"error": "Unknown DC"}), 400)
    token = _read_prfauth()
    if not token:
        return None, (jsonify({"error": "PRFAUTH not set"}), 500)
    url = "{}/{}".format(base.rstrip("/"), path.lstrip("/"))
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=5,
        )
        if not resp.text:
            return None, (jsonify({"error": "empty response"}), 502)
        try:
            return json.loads(resp.text), None
        except ValueError:
            return None, (jsonify({"error": "invalid json"}), 502)
    except requests.exceptions.Timeout:
        return None, (jsonify({"error": "packaging_timeout"}), 504)
    except Exception as exc:
        return None, (jsonify({"error": str(exc)}), 502)


# ── Channel routes ─────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    data, status, err = _id3as_get(dc, "channels?variant={}".format(variant))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>", methods=["GET"])
def id3as_channel(dc, ch_id):
    data, status, err = _id3as_get(dc, "channel/{}".format(url_quote(ch_id, safe="")), expect_list=False)
    if err:
        return err
    return jsonify(data), status


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    data, status, err = _id3as_get(dc, "channel/{}/status".format(url_quote(ch_id, safe="")), expect_list=False)
    if err:
        return err
    return jsonify(data), status


# ── Flags routes ─────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    data, status, err = _id3as_get(dc, "flags/channels")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/flags/events", methods=["GET"])
def id3as_flags_events(dc):
    data, status, err = _id3as_get(dc, "flags/events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/flags", methods=["GET"])
def id3as_flags_all(dc):
    data, status, err = _id3as_get(dc, "flags")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


# ── Events routes ─────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    data, status, err = _id3as_get(dc, "running_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/running_events/channel/<ch_id>", methods=["GET"])
def id3as_running_events_channel(dc, ch_id):
    data, status, err = _id3as_get(dc, "running_events?channel_id={}".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/scheduled_events", methods=["GET"])
def id3as_scheduled_events(dc):
    data, status, err = _id3as_get(dc, "scheduled_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else []), status


# ── Nodes routes ─────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    data, status, err = _id3as_get(dc, "nodes")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/nodes/info", methods=["GET"])
def id3as_nodes_info(dc):
    data, status, err = _id3as_get(dc, "nodes/info")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else []), status


# ── Logs route ──────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/logs", methods=["GET"])
@id3as_bp.route("/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>", methods=["GET"])
def id3as_logs(dc, year=None, month=None, day=None):
    if year is None:
        now = datetime.now(timezone.utc)
        year, month, day = now.year, now.month, now.day

    token = _read_prfauth()
    host = ID3AS_DC_HOSTS.get(dc)
    if not host or not token:
        return jsonify({"error": "PRFAUTH not set or unknown DC"}), 500

    url = "https://{}/ctl/api/data/system_events/{}/{}/{}".format(host, year, month, day)
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=25,
            verify=True,
        )
        raw = resp.text or ""
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    events = _parse_log_response(raw)
    return jsonify([e for e in events if isinstance(e, dict)])


# ── Packaging (internal network only) ────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/packaging/event/<event_id>", methods=["GET"])
def id3as_packaging_event(dc, event_id):
    data, err = _packaging_get(dc, "event/{}".format(url_quote(event_id, safe="")))
    if err:
        return err
    return jsonify(data)


# ── Log JSON normaliser ───────────────────────────────────────────────────────

def _parse_log_response(raw):
    if not raw:
        return []
    # Strategy 1 — straight parse
    try:
        data = json.loads(raw)
        return [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except ValueError:
        pass
    # Strategy 2 — unescape HTML entities, fix adjacent objects, wrap in array
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
    # Strategy 3 — split on object boundaries
    parts = re.split(r"}\s*,\s*{", txt.strip().strip("[]"))
    events = []
    for p in parts:
        obj = ("{" if not p.startswith("{") else "") + p + ("}" if not p.endswith("}") else "")
        try:
            events.append(json.loads(obj))
        except ValueError:
            pass
    return events
