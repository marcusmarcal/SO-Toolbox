"""
id3as_routes.py — Flask Blueprint for id3as DC Monitor proxy endpoints.

IMPORTANT:
Several id3as API endpoints return HTTP 500 even when they contain valid
JSON data. This proxy aggressively retries, parses body regardless of
status code, and optionally serves cached last-good responses.

Register in proxy.py:
    from id3as_routes import id3as_bp
    app.register_blueprint(id3as_bp)
"""

import os
import re
import json
import html as _html
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote as url_quote

import requests
from flask import Blueprint, jsonify

id3as_bp = Blueprint("id3as", __name__)

ID3AS_DC_HOSTS = {
    "ix": "id3as-ix.performgroup.co.uk",
    "eq": "id3as-eq.performgroup.co.uk",
}

ID3AS_PACKAGING_BASE = {
    "ix": "http://id3as.prod.ix.perform.local/ctl/api/packaging",
    "eq": "http://id3as.prod.eq.perform.local/ctl/api/packaging",
}

# ── Cache ───────────────────────────────────────────────────────────

_CACHE = {}
_LAST_GOOD = {}

CACHE_SECONDS = 10

# ── Auth ────────────────────────────────────────────────────────────


def _read_prfauth():
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


def _debug_log(msg):
    print(f"[ID3AS DEBUG] {msg}", file=sys.stderr, flush=True)


# ── Cache helpers ───────────────────────────────────────────────────


def _cache_key(dc, path):
    return f"{dc}:{path}"


def _cache_get(key):
    item = _CACHE.get(key)

    if not item:
        return None

    ts, data = item

    if time.time() - ts > CACHE_SECONDS:
        return None

    return data


def _cache_set(key, data):
    _CACHE[key] = (time.time(), data)
    _LAST_GOOD[key] = data


# ── Core fetch ──────────────────────────────────────────────────────


def _id3as_get(dc, path, expect_list=True):
    host = ID3AS_DC_HOSTS.get(dc)

    if not host:
        return None, None, (
            jsonify({"error": f"Unknown DC: {dc}"}),
            400,
        )

    token = _read_prfauth()

    if not token:
        return None, None, (
            jsonify({"error": "PRFAUTH not set in .env"}),
            500,
        )

    key = _cache_key(dc, path)

    # Fast cache hit
    cached = _cache_get(key)

    if cached is not None:
        _debug_log(f"Cache hit: {key}")

        return cached, 200, None

    url = f"https://{host}/ctl/api/data/{path.lstrip('/')}"

    _debug_log(f"Fetching: {url}")

    last_error = None

    # Retry loop
    for attempt in range(3):
        try:
            # IMPORTANT:
            # Using requests.get directly instead of shared Session.
            # requests.Session() can behave badly under concurrency.
            resp = requests.get(
                url,
                cookies={"prfauth": token},
                headers={"Accept": "application/json"},
                timeout=25,
                verify=True,
            )

            body = resp.text.strip() if resp.text else ""

            _debug_log(
                f"Attempt {attempt+1}: "
                f"status={resp.status_code}, "
                f"body={len(body)}"
            )

            # Always try parse first
            if body:
                try:
                    data = json.loads(body)

                    _debug_log(
                        f"Parsed successfully: "
                        f"type={type(data).__name__}"
                    )

                    if isinstance(data, dict) and expect_list:
                        data = list(data.values())
                        _debug_log(
                            f"Converted dict to list: {len(data)} items"
                        )

                    elif isinstance(data, list):
                        _debug_log(f"Got list: {len(data)} items")

                    _cache_set(key, data)

                    _debug_log(
                        f"Returning success: "
                        f"status=200, items={len(data) if isinstance(data, list) else 1}"
                    )

                    return data, 200, None

                except ValueError as exc:
                    _debug_log(f"JSON parse error: {exc}")

            # Retry on upstream server errors
            if resp.status_code >= 500:
                last_error = f"Upstream {resp.status_code}"

                _debug_log(
                    f"Retrying after upstream error "
                    f"(attempt {attempt+1})"
                )

                time.sleep(0.5)

                continue

            # Empty but valid 2xx
            if resp.status_code in (200, 201, 204):
                empty = [] if expect_list else {}

                _cache_set(key, empty)

                return empty, 200, None

        except requests.exceptions.Timeout:
            last_error = "Request timeout"

            _debug_log("Request timeout")

        except requests.exceptions.ConnectionError as exc:
            last_error = f"Connection error: {exc}"

            _debug_log(last_error)

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

            _debug_log(last_error)

        time.sleep(0.5)

    # Fallback to last good data
    if key in _LAST_GOOD:
        _debug_log(f"Serving LAST_GOOD cache for {key}")

        return _LAST_GOOD[key], 200, None

    _debug_log(f"Returning hard error: {last_error}")

    return None, None, (
        jsonify(
            {
                "error": last_error or "Unknown upstream error",
                "url": url,
            }
        ),
        502,
    )


# ── Packaging ───────────────────────────────────────────────────────


def _packaging_get(dc, path):
    base = ID3AS_PACKAGING_BASE.get(dc)

    if not base:
        return None, (
            jsonify({"error": "Unknown DC"}),
            400,
        )

    token = _read_prfauth()

    if not token:
        return None, (
            jsonify({"error": "PRFAUTH not set"}),
            500,
        )

    url = f"{base.rstrip('/')}/{path.lstrip('/')}"

    try:
        resp = requests.get(
            url,
            cookies={"prfauth": token},
            headers={"Accept": "application/json"},
            timeout=5,
        )

        if not resp.text:
            return None, (
                jsonify({"error": "empty response"}),
                502,
            )

        try:
            return json.loads(resp.text), None

        except ValueError:
            return None, (
                jsonify({"error": "invalid json"}),
                502,
            )

    except requests.exceptions.Timeout:
        return None, (
            jsonify({"error": "packaging_timeout"}),
            504,
        )

    except Exception as exc:
        return None, (
            jsonify({"error": str(exc)}),
            502,
        )


# ── Channel routes ──────────────────────────────────────────────────


@id3as_bp.route("/id3as/<dc>/channels/<variant>", methods=["GET"])
def id3as_channels(dc, variant):
    data, status, err = _id3as_get(
        dc,
        f"channels?variant={variant}"
    )

    if err:
        return err

    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>", methods=["GET"])
def id3as_channel(dc, ch_id):
    data, status, err = _id3as_get(
        dc,
        f"channel/{url_quote(ch_id, safe='')}",
        expect_list=False,
    )

    if err:
        return err

    return jsonify(data), status


@id3as_bp.route("/id3as/<dc>/channel/<ch_id>/status", methods=["GET"])
def id3as_channel_status(dc, ch_id):
    data, status, err = _id3as_get(
        dc,
        f"channel/{url_quote(ch_id, safe='')}/status",
        expect_list=False,
    )

    if err:
        return err

    return jsonify(data), status


# ── Flags routes ────────────────────────────────────────────────────


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


# ── Events routes ───────────────────────────────────────────────────


@id3as_bp.route("/id3as/<dc>/running_events", methods=["GET"])
def id3as_running_events(dc):
    data, status, err = _id3as_get(dc, "running_events")

    if err:
        return err

    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/running_events/channel/<ch_id>", methods=["GET"])
def id3as_running_events_channel(dc, ch_id):
    data, status, err = _id3as_get(
        dc,
        f"running_events?channel_id={url_quote(ch_id, safe='')}"
    )

    if err:
        return err

    return jsonify(data if isinstance(data, list) else []), status


@id3as_bp.route("/id3as/<dc>/scheduled_events", methods=["GET"])
def id3as_scheduled_events(dc):
    data, status, err = _id3as_get(dc, "scheduled_events")

    if err:
        return err

    return jsonify(data if isinstance(data, list) else []), status


# ── Nodes routes ────────────────────────────────────────────────────


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


# ── Logs routes ─────────────────────────────────────────────────────


@id3as_bp.route("/id3as/<dc>/logs", methods=["GET"])
@id3as_bp.route(
    "/id3as/<dc>/logs/<int:year>/<int:month>/<int:day>",
    methods=["GET"],
)
def id3as_logs(dc, year=None, month=None, day=None):
    if year is None:
        now = datetime.now(timezone.utc)

        year = now.year
        month = now.month
        day = now.day

    token = _read_prfauth()

    host = ID3AS_DC_HOSTS.get(dc)

    if not host or not token:
        return jsonify(
            {"error": "PRFAUTH not set or unknown DC"}
        ), 500

    url = (
        f"https://{host}/ctl/api/data/system_events/"
        f"{year}/{month}/{day}"
    )

    try:
        resp = requests.get(
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

    return jsonify(
        [e for e in events if isinstance(e, dict)]
    )


# ── Packaging routes ────────────────────────────────────────────────


@id3as_bp.route(
    "/id3as/<dc>/packaging/event/<event_id>",
    methods=["GET"],
)
def id3as_packaging_event(dc, event_id):
    data, err = _packaging_get(
        dc,
        f"event/{url_quote(event_id, safe='')}"
    )

    if err:
        return err

    return jsonify(data)


# ── Log parser ──────────────────────────────────────────────────────


def _parse_log_response(raw):
    if not raw:
        return []

    # Strategy 1
    try:
        data = json.loads(raw)

        if isinstance(data, dict):
            return [data]

        if isinstance(data, list):
            return data

        return []

    except ValueError:
        pass

    # Strategy 2
    txt = _html.unescape(raw).strip()

    txt = re.sub(r"}\s*{", "},{", txt)

    if not txt.startswith("["):
        txt = "[" + txt

    if not txt.endswith("]"):
        txt += "]"

    try:
        data = json.loads(txt)

        if isinstance(data, list):
            return data

        return [data]

    except ValueError:
        pass

    # Strategy 3
    parts = re.split(
        r"}\s*,\s*{",
        txt.strip().strip("[]"),
    )

    events = []

    for p in parts:
        obj = (
            ("{" if not p.startswith("{") else "")
            + p
            + ("}" if not p.endswith("}") else "")
        )

        try:
            events.append(json.loads(obj))

        except ValueError:
            pass

    return events