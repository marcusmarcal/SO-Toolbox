
# ═══════════════════════════════════════════════════════════
#  id3as DC Monitor — proxy endpoints  (v2)
#  Paste this block into proxy.py BEFORE the `if __name__` line.
#
#  All internal names are prefixed _dc_ to avoid any collision
#  with existing proxy.py functions (session, _check_password, etc).
# ═══════════════════════════════════════════════════════════

import os as _os

_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

# Dedicated session — does NOT conflict with the global `session` used by PhenixRTS routes
_dc_session = requests.Session()


def _dc_read_prfauth():
    """
    Read PRFAUTH from .env next to proxy.py.
    Mirrors the same pattern used by _get_admin_password() above.
    """
    env_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "PRFAUTH":
                    return v.strip().strip("'").strip('"')
    except Exception:
        pass
    return None


def _dc_fetch(dc, api_path):
    """
    Authenticated GET to id3as API.
    Returns parsed JSON (list or dict) on success.
    Returns a Flask Response tuple on failure — check with _dc_is_error().

    IMPORTANT: always returns a SINGLE value or a tuple(response, status_code).
    Never returns (data, error) — that was the bug in v1.
    """
    host = _DC_HOSTS.get(dc)
    if not host:
        return jsonify({"error": f"Unknown DC: {dc}"}), 400

    token = _dc_read_prfauth()
    if not token:
        return jsonify({"error": "PRFAUTH not set in .env"}), 500

    url = f"https://{host}/ctl/api/data/{api_path.lstrip('/')}"
    try:
        resp = _dc_session.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=20,
            verify=True,
        )
        # id3as returns 500 for /running_events when there are no active events
        if resp.status_code == 500 and "running_events" in api_path:
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


def _dc_is_error(result):
    """Return True if _dc_fetch returned a Flask error response tuple."""
    return isinstance(result, tuple)


# ── Channels ──────────────────────────────────────────────────

@app.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    """
    GET /so-proxy/id3as/ix/channels/default
    GET /so-proxy/id3as/ix/channels/racing_uk
    """
    result = _dc_fetch(dc, f"channels?variant={variant}")
    if _dc_is_error(result):
        return result
    return jsonify(result if isinstance(result, list) else [])


# ── Flags (warnings) ──────────────────────────────────────────

@app.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    """GET /so-proxy/id3as/ix/flags/channels"""
    result = _dc_fetch(dc, "flags/channels")
    if _dc_is_error(result):
        return result
    return jsonify(result if isinstance(result, list) else [])


# ── Running events ────────────────────────────────────────────

@app.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    """GET /so-proxy/id3as/ix/running_events"""
    result = _dc_fetch(dc, "running_events")
    if _dc_is_error(result):
        return result
    return jsonify(result if isinstance(result, list) else [])


# ── Nodes ─────────────────────────────────────────────────────

@app.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    """GET /so-proxy/id3as/ix/nodes"""
    result = _dc_fetch(dc, "nodes")
    if _dc_is_error(result):
        return result
    if isinstance(result, dict):
        result = list(result.values())
    return jsonify(result if isinstance(result, list) else [])


# ── System event logs ─────────────────────────────────────────

@app.route("/id3as/<dc>/logs", methods=["GET"])
@app.route("/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>", methods=["GET"])
def id3as_logs(dc, year=None, month=None, day=None):
    """
    GET /so-proxy/id3as/ix/logs             -> today UTC
    GET /so-proxy/id3as/ix/logs/2026/5/5   -> specific date
    Handles the broken/partial JSON that id3as sometimes returns for log endpoints.
    """
    import json as _json
    import re as _re
    import html as _html
    from datetime import datetime, timezone

    if year is None:
        now = datetime.now(timezone.utc)
        year, month, day = now.year, now.month, now.day

    # Try direct fetch — if it parses as JSON we are done
    result = _dc_fetch(dc, f"system_events/{year}/{month}/{day}")
    if _dc_is_error(result):
        # Upstream returned non-200; try raw text fetch to handle partial JSON
        host  = _DC_HOSTS.get(dc, "")
        token = _dc_read_prfauth()
        if not host or not token:
            return result
        url = f"https://{host}/ctl/api/data/system_events/{year}/{month}/{day}"
        try:
            raw_resp = _dc_session.get(
                url,
                cookies={"prfauth": token},
                headers={"Accept": "application/json"},
                timeout=25,
                verify=True,
            )
            raw = raw_resp.text
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    else:
        if isinstance(result, list):
            return jsonify(result)
        raw = _json.dumps(result)

    # Normalise broken JSON (id3as logs endpoint sometimes returns malformed payloads)
    def _normalise(txt):
        txt = _html.unescape(txt).strip()
        if txt.startswith('[') and txt.endswith(']'):
            return txt
        txt = _re.sub(r'}\s*{', '},{', txt)
        if not txt.startswith('['):
            txt = '[' + txt
        if not txt.endswith(']'):
            txt += ']'
        return txt

    events = []
    try:
        parsed = _json.loads(raw)
        events = [parsed] if isinstance(parsed, dict) else parsed
    except Exception:
        try:
            parsed = _json.loads(_normalise(raw))
            events = [parsed] if isinstance(parsed, dict) else parsed
        except Exception:
            parts = _re.split(r'}\s*,\s*{', _normalise(raw).strip().strip('[]'))
            for p in parts:
                obj = ('{' if not p.startswith('{') else '') + p + ('}' if not p.endswith('}') else '')
                try:
                    events.append(_json.loads(obj))
                except Exception:
                    pass

    return jsonify([e for e in events if isinstance(e, dict)])


# ── Single channel status ─────────────────────────────────────

@app.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    """GET /so-proxy/id3as/ix/channel/<ch_id>/status"""
    from urllib.parse import quote as _quote
    result = _dc_fetch(dc, f"channel/{_quote(ch_id, safe='')}/status")
    if _dc_is_error(result):
        return result
    return jsonify(result)
