"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy endpoints.

Register in proxy.py:
    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)

Reads PRFAUTH from .env — never sent to the browser.
DC routing:
    ix → id3as-ix.performgroup.co.uk
    eq → id3as-eq.performgroup.co.uk

Packaging API (internal network only):
    ix → http://id3as.prod.ix.perform.local/ctl/api/packaging
    eq → http://id3as.prod.eq.perform.local/ctl/api/packaging

Endpoints:
    GET /id3as/<dc>/channels/<variant>              channel list (default | racing_uk)
    GET /id3as/<dc>/channel/<id>                    single channel config
    GET /id3as/<dc>/channel/<id>/status             live enc/src state + stream info
    GET /id3as/<dc>/flags/channels                  active warnings per channel
    GET /id3as/<dc>/flags/events                    active warnings per event
    GET /id3as/<dc>/flags                           all system-level flags
    GET /id3as/<dc>/running_events                  currently active events
    GET /id3as/<dc>/scheduled_events                upcoming events
    GET /id3as/<dc>/nodes                           node list + status
    GET /id3as/<dc>/nodes/info                      live CPU/memory/GPU metrics
    GET /id3as/<dc>/logs                            today's system events (UTC)
    GET /id3as/<dc>/logs/<year>/<month>/<day>       specific date logs
    GET /id3as/<dc>/packaging/event/<event_id>      HLS publication config (internal only)
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

# ── Blueprint ─────────────────────────────────────────────────────────────────

id3as_bp = Blueprint("id3as", __name__)

_SESSION = requests.Session()

ID3AS_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

ID3AS_PACKAGING_HOSTS = {
    "ix": "http://id3as.prod.ix.perform.local/ctl/api/packaging",
    "eq": "http://id3as.prod.eq.perform.local/ctl/api/packaging",
}

# Endpoints that legitimately return HTTP 500 when empty — treat as []
_EMPTY_ON_500 = ("running_events", "flags/channels", "flags/events", "flags")

# ── Auth helper ───────────────────────────────────────────────────────────────

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
            timeout=20,
            verify=True,
        )
        # Several endpoints return 500 when the result set is empty — treat as []
        if resp.status_code == 500:
            for ep in _EMPTY_ON_500:
                if ep in path:
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


def _packaging_get(dc, path):
    """Fetch from internal packaging API. Returns (data, err). Degrades gracefully on timeout."""
    base = ID3AS_PACKAGING_HOSTS.get(dc)
    if not base:
        return None, (jsonify({"error": "Unknown DC: {}".format(dc)}), 400)

    token = _read_prfauth()
    if not token:
        return None, (jsonify({"error": "PRFAUTH not set in .env"}), 500)

    url = "{}/{}".format(base.rstrip("/"), path.lstrip("/"))
    try:
        resp = _SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=5,   # short timeout — internal network only
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
    """GET /id3as/<dc>/channels/default  |  /id3as/<dc>/channels/racing_uk"""
    data, err = _id3as_get(dc, "channels?variant={}".format(variant))
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>", methods=["GET"])
def id3as_channel(dc, ch_id):
    """GET /id3as/<dc>/channel/<ch_id> — single channel config dict"""
    data, err = _id3as_get(dc, "channel/{}".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data)


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    """GET /id3as/<dc>/channel/<ch_id>/status — live enc/src state + stream info"""
    data, err = _id3as_get(dc, "channel/{}/status".format(url_quote(ch_id, safe="")))
    if err:
        return err
    return jsonify(data)


# ── Flags routes ──────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    """GET /id3as/<dc>/flags/channels — active warnings per channel (500 = no flags = [])"""
    data, err = _id3as_get(dc, "flags/channels")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/flags/events", methods=["GET"])
def id3as_flags_events(dc):
    """GET /id3as/<dc>/flags/events — active warnings per running event"""
    data, err = _id3as_get(dc, "flags/events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/flags", methods=["GET"])
def id3as_flags_all(dc):
    """GET /id3as/<dc>/flags — all system-level flags"""
    data, err = _id3as_get(dc, "flags")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Events routes ─────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    """GET /id3as/<dc>/running_events — currently active events"""
    data, err = _id3as_get(dc, "running_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/scheduled_events", methods=["GET"])
def id3as_scheduled_events(dc):
    """GET /id3as/<dc>/scheduled_events — upcoming events not yet started"""
    data, err = _id3as_get(dc, "scheduled_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Nodes routes ──────────────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    """GET /id3as/<dc>/nodes — node list with status and capacity"""
    data, err = _id3as_get(dc, "nodes")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else [])


@id3as_bp.route("/id3as/<dc>/nodes/info", methods=["GET"])
def id3as_nodes_info(dc):
    """GET /id3as/<dc>/nodes/info — live CPU / memory / GPU / scheduler utilisation"""
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
    """
    GET /id3as/<dc>/logs               → today UTC
    GET /id3as/<dc>/logs/2026/5/6      → specific date
    Normalises the sometimes-broken JSON the id3as API returns.
    """
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


# ── Packaging route (internal network only) ───────────────────────────────────

@id3as_bp.route("/id3as/<dc>/packaging/event/<event_id>", methods=["GET"])
def id3as_packaging_event(dc, event_id):
    """
    GET /id3as/<dc>/packaging/event/<event_id>
    Returns HLS publication config. Only reachable inside Perform network.
    Returns 504 with {"error":"packaging_timeout"} when unreachable — client degrades gracefully.
    """
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
    # Strategy 2 — unescape HTML, wrap in array, fix adjacent objects
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
