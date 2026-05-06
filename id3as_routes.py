#!/usr/bin/env python3
"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy routes.

Registers all /id3as/* endpoints. Import and register in proxy.py:

    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)

Reads PRFAUTH from .env (same directory as proxy.py).
Credentials are never forwarded to the browser.

Endpoints:
    GET /id3as/<dc>/channels/<variant>       default | racing_uk
    GET /id3as/<dc>/flags/channels           active warnings (500 → [])
    GET /id3as/<dc>/running_events           active events   (500 → [])
    GET /id3as/<dc>/nodes                    node list
    GET /id3as/<dc>/logs                     system events today UTC
    GET /id3as/<dc>/logs/<y>/<m>/<d>         system events for date
    GET /id3as/<dc>/channel/<id>/status      single channel enc/src state
"""

import os
import json
import re
import html as _html
from datetime import datetime, timezone
from urllib.parse import quote as _quote

import requests
from flask import Blueprint, jsonify

# ── Blueprint ─────────────────────────────────────────────────

id3as_bp = Blueprint("id3as", __name__)

# ── Config ────────────────────────────────────────────────────

_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

_session = requests.Session()

# ── Auth ──────────────────────────────────────────────────────

def _read_prfauth() -> str | None:
    """Read PRFAUTH from .env next to this file."""
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

# ── HTTP helper ───────────────────────────────────────────────

# Endpoints that return HTTP 500 when empty — treat as []
_EMPTY_ON_500 = ("running_events", "flags/channels", "flags")

def _fetch(dc: str, api_path: str):
    """
    Authenticated GET to id3as API.
    Returns parsed JSON on success, or a Flask (Response, status) tuple on error.
    """
    host = _DC_HOSTS.get(dc)
    if not host:
        return jsonify({"error": f"Unknown DC: {dc}"}), 400

    token = _read_prfauth()
    if not token:
        return jsonify({"error": "PRFAUTH not set in .env"}), 500

    url = f"https://{host}/ctl/api/data/{api_path.lstrip('/')}"
    try:
        resp = _session.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=20,
            verify=True,
        )
        if resp.status_code == 500 and any(p in api_path for p in _EMPTY_ON_500):
            return []
        if resp.status_code != 200:
            return jsonify({"error": f"Upstream HTTP {resp.status_code}", "dc": dc, "path": api_path}), resp.status_code
        return resp.json()
    except requests.exceptions.ConnectionError as exc:
        return jsonify({"error": f"Connection error: {exc}"}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _is_err(result) -> bool:
    return isinstance(result, tuple)

# ── Routes ────────────────────────────────────────────────────

@id3as_bp.route("/id3as/<dc>/channels/<variant>")
def channels(dc, variant):
    r = _fetch(dc, f"channels?variant={variant}")
    if _is_err(r): return r
    return jsonify(r if isinstance(r, list) else [])


@id3as_bp.route("/id3as/<dc>/flags/channels")
def flags_channels(dc):
    r = _fetch(dc, "flags/channels")
    if _is_err(r): return r
    return jsonify(r if isinstance(r, list) else [])


@id3as_bp.route("/id3as/<dc>/running_events")
def running_events(dc):
    r = _fetch(dc, "running_events")
    if _is_err(r): return r
    return jsonify(r if isinstance(r, list) else [])


@id3as_bp.route("/id3as/<dc>/nodes")
def nodes(dc):
    r = _fetch(dc, "nodes")
    if _is_err(r): return r
    if isinstance(r, dict):
        r = list(r.values())
    return jsonify(r if isinstance(r, list) else [])


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status")
def channel_status(dc, ch_id):
    r = _fetch(dc, f"channel/{_quote(ch_id, safe='')}/status")
    if _is_err(r): return r
    return jsonify(r)


@id3as_bp.route("/id3as/<dc>/logs")
@id3as_bp.route("/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>")
def logs(dc, year=None, month=None, day=None):
    """
    System event log. Handles the partial/broken JSON the id3as API
    sometimes returns for this endpoint.
    """
    if year is None:
        now = datetime.now(timezone.utc)
        year, month, day = now.year, now.month, now.day

    r = _fetch(dc, f"system_events/{year}/{month}/{day}")

    if _is_err(r):
        # Non-200 upstream — retry as raw text to handle partial JSON
        host  = _DC_HOSTS.get(dc, "")
        token = _read_prfauth()
        if not host or not token:
            return r
        url = f"https://{host}/ctl/api/data/system_events/{year}/{month}/{day}"
        try:
            raw = _session.get(
                url,
                cookies={"prfauth": token},
                headers={"Accept": "application/json"},
                timeout=25,
                verify=True,
            ).text
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    else:
        if isinstance(r, list):
            return jsonify(r)
        raw = json.dumps(r)

    return jsonify(_parse_log_text(raw))


def _parse_log_text(raw: str) -> list:
    """Normalise the broken/partial JSON id3as sometimes returns for logs."""
    def _fix(txt):
        txt = _html.unescape(txt).strip()
        if txt.startswith('[') and txt.endswith(']'):
            return txt
        txt = re.sub(r'}\s*{', '},{', txt)
        if not txt.startswith('['):
            txt = '[' + txt
        if not txt.endswith(']'):
            txt += ']'
        return txt

    for attempt in (raw, _fix(raw)):
        try:
            parsed = json.loads(attempt)
            return [parsed] if isinstance(parsed, dict) else parsed
        except Exception:
            pass

    # Last resort: split on object boundaries
    events = []
    for p in re.split(r'}\s*,\s*{', _fix(raw).strip().strip('[]')):
        obj = ('{' if not p.startswith('{') else '') + p + ('}' if not p.endswith('}') else '')
        try:
            events.append(json.loads(obj))
        except Exception:
            pass
    return [e for e in events if isinstance(e, dict)]
