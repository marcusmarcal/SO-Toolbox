
# ═══════════════════════════════════════════════════════════
#  id3as DC Monitor — proxy endpoints
#  Add these routes to proxy.py (before the if __name__ block)
#
#  Reads PRFAUTH from .env — never exposed to the browser.
#  DC routing: ix → id3as-ix.performgroup.co.uk
#              eq → id3as-eq.performgroup.co.uk
#
#  Browser calls:  GET /so-proxy/id3as/<dc>/<path>
#  Proxy calls:    GET https://id3as-<dc>.performgroup.co.uk/ctl/api/data/<path>
#    authenticated via cookie: prfauth=<token>
# ═══════════════════════════════════════════════════════════

ID3AS_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}
ID3AS_SESSION = requests.Session()


def _get_id3as_token():
    """Read PRFAUTH from .env. Returns None if not set."""
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


def _id3as_get(dc, path):
    """
    Authenticated GET to id3as API. Returns (data, error_response).
    data is the parsed JSON (list or dict); error_response is a Flask Response or None.
    """
    import os
    host = ID3AS_DC_HOSTS.get(dc)
    if not host:
        return None, (jsonify({"error": f"Unknown DC: {dc}"}), 400)

    token = _get_id3as_token()
    if not token:
        return None, (jsonify({"error": "PRFAUTH not set in .env"}), 500)

    url = f"https://{host}/ctl/api/data/{path.lstrip('/')}"
    try:
        resp = ID3AS_SESSION.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=20,
            verify=True,
        )
        # /running_events returns 500 when there are no active events — treat as empty
        if resp.status_code == 500 and "running_events" in path:
            return [], None
        if resp.status_code != 200:
            return None, (jsonify({"error": f"Upstream {resp.status_code}", "url": url}), resp.status_code)
        return resp.json(), None
    except requests.exceptions.ConnectionError as e:
        return None, (jsonify({"error": f"Connection error: {e}"}), 502)
    except requests.exceptions.Timeout:
        return None, (jsonify({"error": "Request timed out"}), 504)
    except Exception as e:
        return None, (jsonify({"error": str(e)}), 500)


# ── Channel variants ──────────────────────────────────────────

@app.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    """
    GET /so-proxy/id3as/<dc>/channels/default
    GET /so-proxy/id3as/<dc>/channels/racing_uk
    Returns the raw channel list from id3as.
    """
    data, err = _id3as_get(dc, f"channels?variant={variant}")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Flags (active warnings) ───────────────────────────────────

@app.route("/id3as/<dc>/flags/channels", methods=["GET"])
def id3as_flags_channels(dc):
    """GET /so-proxy/id3as/<dc>/flags/channels"""
    data, err = _id3as_get(dc, "flags/channels")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Running events ────────────────────────────────────────────

@app.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    """GET /so-proxy/id3as/<dc>/running_events"""
    data, err = _id3as_get(dc, "running_events")
    if err:
        return err
    return jsonify(data if isinstance(data, list) else [])


# ── Nodes ─────────────────────────────────────────────────────

@app.route("/id3as/<dc>/nodes", methods=["GET"])
def id3as_nodes(dc):
    """GET /so-proxy/id3as/<dc>/nodes"""
    data, err = _id3as_get(dc, "nodes")
    if err:
        return err
    if isinstance(data, dict):
        data = list(data.values())
    return jsonify(data if isinstance(data, list) else [])


# ── System event logs ─────────────────────────────────────────

@app.route("/id3as/<dc>/logs", methods=["GET"])
@app.route("/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>", methods=["GET"])
def id3as_logs(dc, year=None, month=None, day=None):
    """
    GET /so-proxy/id3as/<dc>/logs                    → today UTC
    GET /so-proxy/id3as/<dc>/logs/2026/5/5           → specific date
    Returns parsed event list, normalising broken JSON from the API.
    """
    import html as _html
    from datetime import datetime, timezone

    if year is None:
        now = datetime.now(timezone.utc)
        year, month, day = now.year, now.month, now.day

    data, err = _id3as_get(dc, f"system_events/{year}/{month}/{day}")
    if err:
        # API may return raw text for logs — try raw fetch
        host  = ID3AS_DC_HOSTS.get(dc, "")
        token = _get_id3as_token()
        if not host or not token:
            return err
        url = f"https://{host}/ctl/api/data/system_events/{year}/{month}/{day}"
        try:
            resp = ID3AS_SESSION.get(
                url,
                cookies={"prfauth": token},
                headers={"Accept": "application/json"},
                timeout=20,
                verify=True,
            )
            raw = resp.text
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        if isinstance(data, list):
            return jsonify(data)
        # Sometimes comes back as raw text embedded in JSON — re-serialize to text
        import json as _json
        raw = _json.dumps(data)

    # Normalise broken/partial JSON that the id3as API sometimes returns
    def _normalize(txt):
        txt = _html.unescape(txt).strip()
        if txt.startswith('[') and txt.endswith(']'):
            return txt
        import re
        txt = re.sub(r'}\s*{', '},{', txt)
        if not txt.startswith('['):
            txt = '[' + txt
        if not txt.endswith(']'):
            txt += ']'
        return txt

    import json as _json, re as _re
    events = []
    try:
        events = _json.loads(raw)
        if isinstance(events, dict):
            events = [events]
    except Exception:
        try:
            events = _json.loads(_normalize(raw))
            if isinstance(events, dict):
                events = [events]
        except Exception:
            parts = _re.split(r'}\s*,\s*{', _normalize(raw).strip().strip('[]'))
            for p in parts:
                obj = ('{' if not p.startswith('{') else '') + p + ('}' if not p.endswith('}') else '')
                try:
                    events.append(_json.loads(obj))
                except Exception:
                    pass

    return jsonify([e for e in events if isinstance(e, dict)])


# ── Channel status (single channel) ──────────────────────────

@app.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    """GET /so-proxy/id3as/<dc>/channel/<ch_id>/status"""
    from urllib.parse import quote as _quote
    data, err = _id3as_get(dc, f"channel/{_quote(ch_id, safe='')}/status")
    if err:
        return err
    return jsonify(data)
