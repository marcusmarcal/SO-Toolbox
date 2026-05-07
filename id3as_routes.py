"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy endpoints.

Register in proxy.py:
    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)

Endpoints exposed at /id3as/<dc>/*  (PRFAUTH always server-side)
"""

import os
import re
import json
import html as _html
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
from urllib.parse import quote as url_quote

import requests
from flask import Blueprint, jsonify

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

# These endpoints return HTTP 500 when the result set is empty — treat as []
_EMPTY_ON_500 = (
    "running_events",
    "flags/channels",
    "flags/events",
    "flags",
    "nodes/info",
    "scheduled_events",
)


# ── Auth ──────────────────────────────────────────────────────────────────────

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


# ── Core fetch ────────────────────────────────────────────────────────────────

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
            timeout=25,
            verify=True,
        )
        # Several endpoints legitimately return 500 when result set is empty
        if resp.status_code == 500:
            for ep in _EMPTY_ON_500:
                if ep in path:
                    return [], None
            # For other 500s, return the error
            return None, (
                jsonify({"error": "Upstream 500", "url": url}),
                500,
            )
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
        if resp.status_code != 200:
            return None, (jsonify({"error": "Upstream {}".format(resp.status_code)}), resp.status_code)
        return resp.json(), None
    except requests.exceptions.Timeout:
        return None, (jsonify({"error": "packaging_timeout"}), 504)
    except Exception as exc:
        return None, (jsonify({"error": str(exc)}), 502)


# ── Channel routes ────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    data, err = _id3as_get(dc, "channels?variant={}".format(variant))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>", methods=["GET"])
def id3as_channel(dc, ch_id):
    data, err = _id3as_get(dc, "channel/{}".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data)


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    data, err = _id3as_get(dc, "channel/{}/status".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data)


# ── Flags routes ──────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    data, err = _id3as_get(dc, "flags/channels")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/flags/events", methods=["GET"])
def id3as_flags_events(dc):
    data, err = _id3as_get(dc, "flags/events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/flags", methods=["GET"])
def id3as_flags_all(dc):
    data, err = _id3as_get(dc, "flags")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Events routes ─────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    data, err = _id3as_get(dc, "running_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/running_events/channel/<ch_id>", methods=["GET"])
def id3as_running_events_channel(dc, ch_id):
    """Filtered running events for one channel."""
    data, err = _id3as_get(dc, "running_events?channel_id={}".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/scheduled_events", methods=["GET"])
def id3as_scheduled_events(dc):
    data, err = _id3as_get(dc, "scheduled_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Nodes routes ──────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    data, err = _id3as_get(dc, "nodes")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/nodes/info", methods=["GET"])
def id3as_nodes_info(dc):
    data, err = _id3as_get(dc, "nodes/info")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else [])


# ── Logs route ────────────────────────────────────────────────────────────────

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
        raw = resp.text
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
