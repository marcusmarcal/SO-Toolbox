import base64
import csv
import io
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, request, jsonify, Response
from urllib.parse import quote
from edgeauth.token_builder import TokenBuilder

rts_bp = Blueprint("rts", __name__)

# Shared session (reuse the one from the app if injected, else create local)
# To share the proxy.py session, call rts_bp.session = session after import.
_session = requests.Session()

PHENIX_BASE = "https://pcast.phenixrts.com"

# ── Fork-origin cache ─────────────────────────────────────────────────
# Phenix's fork-history report cannot be filtered by channel and always
# returns the full CSV, so the cheapest strategy is to poll it once per
# App ID (not once per browser tab), over incremental time windows, and
# keep only the latest successful fork per destination channel.
#
# Cache layout: { app_id: {"map": {dest_id: {"sourceId", "timestamp"}},
#                          "last_end": datetime, "updated": float,
#                          "backfilling": bool} }
# Only derived fork relationships are cached — credentials are never stored
# beyond the lifetime of the in-flight backfill thread.
_fork_cache = {}
_fork_cache_lock = threading.Lock()

FORK_CACHE_TTL_S           = 55        # serve cached map if younger than this
FORK_INITIAL_LOOKBACK_H    = 24        # backfill window for a new App ID (hours)
FORK_OVERLAP_S             = 120       # re-read this much of the previous window
FORK_END_SAFETY_S          = 60        # Phenix requires "end" strictly in the past
FORK_MAX_WINDOW_H          = 23        # stay under Phenix's 1-day-per-request cap


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


@rts_bp.route("/channel/members/<path:channel_id>", methods=["GET"])
def get_channel_members(channel_id):
    """Proxy for the Phenix channel members endpoint.
    Returns the current members (publishers) of a channel, each with its
    session ID, screen name, role, state, last update and stream list.
    Response is passed through unchanged: { "status": "ok", "members": [...] }
    """
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing credentials headers"}), 400
    try:
        encoded_id = quote(channel_id, safe="")
        resp = _get_session().get(
            f"{PHENIX_BASE}/pcast/channel/{encoded_id}/members",
            headers={
                "Authorization": _make_auth_header(app_id, password),
                "Accept": "application/json",
            },
            timeout=15,
        )
        return Response(resp.content, status=resp.status_code, content_type="application/json")
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
            timeout=120,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "text/csv"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@rts_bp.route("/rts/fork-history", methods=["POST"])
def rts_fork_history():
    """Proxy for the Phenix Fork History reporting endpoint.
    Expects JSON body: { start, end }
    Returns the raw CSV from Phenix listing fork API calls, including
    SourceId and DestinationId columns which map a fork destination
    channel back to its source (base) channel.
    """
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing credentials headers"}), 400

    data  = request.get_json(silent=True) or {}
    start = (data.get("start") or "").strip()
    end   = (data.get("end") or "").strip()

    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400

    payload = {
        "forkHistoryReport": {
            "applicationIds": [app_id],
            "start": start,
            "end": end,
        }
    }

    try:
        resp = _get_session().put(
            f"{PHENIX_BASE}/pcast/reporting/fork/history",
            auth=(app_id, password),
            headers={
                "Accept": "text/csv",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "text/csv"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _fetch_fork_rows(app_id, password, start, end):
    """Fetch the fork-history CSV for [start, end] and return parsed rows."""
    payload = {
        "forkHistoryReport": {
            "applicationIds": [app_id],
            "start": _iso_z(start),
            "end": _iso_z(end),
        }
    }
    resp = _get_session().put(
        f"{PHENIX_BASE}/pcast/reporting/fork/history",
        auth=(app_id, password),
        headers={"Accept": "text/csv", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Phenix {resp.status_code}: {resp.text[:200]}")
    return list(csv.DictReader(io.StringIO(resp.text)))


def _merge_fork_rows(fork_map, rows):
    """Merge rows into fork_map keeping the most recent HTTP-200 fork per
    destination. Timestamps are 'YYYY-MM-DD HH:MM:SS' so string comparison
    orders them correctly."""
    for r in rows:
        dest = (r.get("DestinationId") or "").strip()
        if not dest or (r.get("Status") or "").strip() != "200":
            continue
        ts = (r.get("Timestamp") or "").strip()
        cur = fork_map.get(dest)
        if not cur or ts > cur["timestamp"]:
            fork_map[dest] = {"sourceId": (r.get("SourceId") or "").strip(), "timestamp": ts}
    return fork_map


def _backfill_fork_history(app_id, password, start, end):
    """Background job: scan [start, end] in <= FORK_MAX_WINDOW_H chunks,
    newest first (so the most relevant forks land in the cache soonest),
    merging each chunk into the App ID's cached map. Older forks never
    overwrite newer ones because merge compares timestamps."""
    chunk = timedelta(hours=FORK_MAX_WINDOW_H)
    cur_end = end
    try:
        while cur_end > start:
            cur_start = max(start, cur_end - chunk)
            try:
                rows = _fetch_fork_rows(app_id, password, cur_start, cur_end)
            except Exception:
                # Give up on the remaining (older) chunks; the newer ones
                # already merged are still valid.
                break
            with _fork_cache_lock:
                entry = _fork_cache.get(app_id)
                if entry is None:
                    break
                _merge_fork_rows(entry["map"], rows)
            cur_end = cur_start
    finally:
        with _fork_cache_lock:
            entry = _fork_cache.get(app_id)
            if entry is not None:
                entry["backfilling"] = False
                entry["updated"] = time.time()


@rts_bp.route("/rts/fork-origin", methods=["GET"])
def rts_fork_origin():
    """Return the latest successful fork per destination channel as JSON:
        { "forks": { destId: { sourceId, timestamp } }, "updatedAt", "stale" }
    Polls Phenix at most once per FORK_CACHE_TTL_S per App ID, using an
    incremental window since the previous poll. Intended for the Channels
    tab's background "Forked From" column; the Fork Origin tab still uses
    /rts/fork-history for arbitrary user-selected periods.
    """
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing credentials headers"}), 400

    now = datetime.now(timezone.utc)
    end = now - timedelta(seconds=FORK_END_SAFETY_S)

    with _fork_cache_lock:
        entry = _fork_cache.get(app_id)

        # First time we see this App ID: register an empty entry and kick
        # off a background backfill of the last FORK_INITIAL_LOOKBACK_H
        # hours. Respond immediately; the map fills in over the next polls.
        if entry is None:
            entry = {"map": {}, "last_end": end, "updated": time.time(), "backfilling": True}
            _fork_cache[app_id] = entry
            start = end - timedelta(hours=FORK_INITIAL_LOOKBACK_H)
            threading.Thread(
                target=_backfill_fork_history,
                args=(app_id, password, start, end),
                daemon=True,
            ).start()
            return jsonify({"forks": {}, "updatedAt": entry["updated"],
                            "stale": False, "backfilling": True})

        # While the backfill is running, or if the cache is still fresh,
        # just return what we have.
        if entry["backfilling"] or time.time() - entry["updated"] < FORK_CACHE_TTL_S:
            return jsonify({"forks": entry["map"], "updatedAt": entry["updated"],
                            "stale": False, "backfilling": entry["backfilling"]})

        start = entry["last_end"] - timedelta(seconds=FORK_OVERLAP_S)
        # Long idle gap (e.g. proxy kept running with no clients): cap the
        # window so a single request stays within Phenix's limit.
        start = max(start, end - timedelta(hours=FORK_MAX_WINDOW_H))
        if start >= end:
            return jsonify({"forks": entry["map"], "updatedAt": entry["updated"],
                            "stale": False, "backfilling": False})

    # Incremental fetch outside the lock so other App IDs aren't blocked.
    try:
        rows = _fetch_fork_rows(app_id, password, start, end)
    except Exception as e:
        # Upstream hiccup: serve the last known map rather than failing.
        with _fork_cache_lock:
            entry = _fork_cache.get(app_id) or {"map": {}, "updated": time.time()}
            return jsonify({"forks": entry["map"], "updatedAt": entry["updated"],
                            "stale": True, "backfilling": False, "error": str(e)})

    with _fork_cache_lock:
        entry = _fork_cache.get(app_id)
        if entry is None:
            return jsonify({"forks": {}, "updatedAt": time.time(), "stale": False, "backfilling": False})
        _merge_fork_rows(entry["map"], rows)
        entry["last_end"] = end
        entry["updated"] = time.time()
        return jsonify({"forks": entry["map"], "updatedAt": entry["updated"],
                        "stale": False, "backfilling": False})


@rts_bp.route("/edge-token", methods=["POST"])
def rts_edge_token():
    """Generate a Phenix EdgeAuth digest token to view a channel's state.
    Expects JSON body: { channel_id? , channel_alias?, expires_in_seconds? }
    Either channel_id or channel_alias must be provided.
    Returns: { "token": "DIGEST:..." }
    """
    app_id   = request.headers.get("X-App-Id")
    password = request.headers.get("X-Password")
    if not app_id or not password:
        return jsonify({"error": "Missing credentials headers"}), 400

    data          = request.get_json(silent=True) or {}
    channel_id    = (data.get("channel_id") or "").strip()
    channel_alias = (data.get("channel_alias") or "").strip()
    expires_in    = data.get("expires_in_seconds", 3600)

    if not channel_id and not channel_alias:
        return jsonify({"error": "channel_id or channel_alias is required"}), 400

    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        return jsonify({"error": "expires_in_seconds must be an integer"}), 400

    try:
        builder = (
            TokenBuilder()
            .with_application_id(app_id)
            .with_secret(password)
            .expires_in_seconds(expires_in)
        )
        builder = builder.for_channel(channel_id) if channel_id else builder.for_channel_alias(channel_alias)

        token = builder.build()
        return jsonify({"token": token})
    except Exception as e:
        return jsonify({"error": str(e)}), 502