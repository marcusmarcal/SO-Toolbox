"""
SO-Toolbox - BTE ("Better Than EMO")
Blueprint that keeps a local, hourly snapshot of the relevant Dataminer
resources and exposes it on the toolbox internal API. BTE operates against
the MAIN TXCore cluster; the stream/source/output provisioning logic will be
added on top of this snapshot in a later iteration.

Snapshot file: /opt/web/data/dataminer.resources.json

    {
      "version": 1,
      "fetched_at":      "<UTC ISO of the last refresh attempt>",
      "last_success_at": "<UTC ISO of the last refresh that fetched >= 1 pool>",
      "source":          "https://<dataminer>/api/custom/resources",
      "duration_ms":     1234,
      "pools": {
        "resources":    {"pool": "Supplier Dynamic", "count": N, "fetched_at": "...", "items": [...]},
        "destinations": {"pool": "Destination",      "count": M, "fetched_at": "...", "items": [...]}
      },
      "errors": {"<pool key>": "<error message>"}   # only for pools that failed this run
    }

A pool that fails to refresh keeps its previous items (the last good copy is
never thrown away because of a transient API error).

Two Dataminer response shapes are accepted and normalised to {pool, count, items}:
    {"pool": "...", "count": N, "items": [...]}                  (original)
    {"pools": {"<pool name>": {"count": N, "items": [...]}}}   (current)

Each item may carry ``capabilities`` (e.g. Type = supplier name, interface
capabilities) next to ``properties``. The supplier is ``capabilities.Type``;
the TXEdge is ``properties["DC MWEdge"]``.

Environment variables (.env):
    DATAMINER_API_URL            Base URL, no trailing slash
                                 e.g. https://dataminer-stage.statsperform.technology
    DATAMINER_BEARER_TOKEN       Bearer token for the Dataminer custom API
    DATAMINER_VERIFY_SSL         "true" (default) | "false". Setting this to false
                                 mirrors `curl -k` and is NOT recommended outside a
                                 lab: prefer DATAMINER_CA_BUNDLE.
    DATAMINER_CA_BUNDLE          Optional path to a PEM bundle for the Dataminer
                                 certificate chain (overrides DATAMINER_VERIFY_SSL).
    DATAMINER_SNAPSHOT_INTERVAL  Seconds between refreshes (default 3600).
    DATAMINER_SNAPSHOT_DISABLED  "true" to disable the background refresher
                                 (manual POST /refresh still works).

Provisioning (v1.4.0)
    The write side lives in bte_provisioning.py: "Create resources" turns a
    snapshot item into TXCore MWEdge objects on the DC edge + AVE/LMK/YER,
    every object tagged "[BTE]" and tracked in a lease with an expiry. Leases
    can be extended, deleted one by one or all at once; expired leases are
    removed by a background reaper. Only registry-known objects whose live
    name still carries the tag are ever deleted.

Security notes
    * The bearer token is never returned to the browser; /status only reports
      whether it is set.
    * Any resource property whose key looks like a secret (passphrase, password,
      secret, token, key) is redacted in every HTTP response. The on-disk
      snapshot keeps the raw value (mode 0600) because the provisioning step
      will need it server-side.
    * Concurrent refreshes across gunicorn workers are prevented with a file
      lock; workers that lose the race simply reload the file written by the
      winner.
"""

import copy
import fcntl
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Blueprint, jsonify, request

from routes_auth import _get_session, _token_from_request

import bte_provisioning as prov

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

log = logging.getLogger('so-toolbox.bte')

bte_bp = Blueprint('bte', __name__, url_prefix='/api/bte')

ALLOWED_ROLES = ('admin', 'engineer')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(*names):
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _env_bool(name, default):
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default, minimum=60):
    value = _env(name)
    try:
        return max(minimum, int(value)) if value else default
    except ValueError:
        return default


DATAMINER_URL = (_env('DATAMINER_API_URL') or '').rstrip('/') or None
DATAMINER_TOKEN = _env('DATAMINER_BEARER_TOKEN')
DATAMINER_VERIFY_SSL = _env_bool('DATAMINER_VERIFY_SSL', True)
DATAMINER_CA_BUNDLE = _env('DATAMINER_CA_BUNDLE')
SNAPSHOT_INTERVAL = _env_int('DATAMINER_SNAPSHOT_INTERVAL', 3600)
SNAPSHOT_DISABLED = _env_bool('DATAMINER_SNAPSHOT_DISABLED', False)

RESOURCES_PATH = '/api/custom/resources'

# Pool key exposed on the internal API -> Dataminer ``pool`` query parameter.
# ``None`` means "no parameter" (the API defaults to the Supplier Dynamic pool).
POOLS = {
    'resources': None,
    'destinations': 'Destination',
}
DEFAULT_POOL_LABEL = 'Supplier Dynamic'

DATA_DIR = '/opt/web/data'
SNAPSHOT_FILE = os.path.join(DATA_DIR, 'dataminer.resources.json')
LOCK_FILE = SNAPSHOT_FILE + '.lock'

REQUEST_TIMEOUT = (10, 90)  # (connect, read) seconds — the default pool is >1k items
REDACTED = '********'
_SECRET_KEY_RE = re.compile(r'passphrase|password|secret|token|api[-_ ]?key', re.IGNORECASE)

os.makedirs(DATA_DIR, exist_ok=True)


def _verify_option():
    if DATAMINER_CA_BUNDLE:
        return DATAMINER_CA_BUNDLE
    return DATAMINER_VERIFY_SSL


_session = requests.Session()
_session.headers.update({
    'Authorization': f'Bearer {DATAMINER_TOKEN}',
    'Accept': 'application/json',
})
_session.verify = _verify_option()
if _session.verify is False:
    # Equivalent of `curl -k`. Logged loudly so it does not go unnoticed.
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log.warning('DATAMINER_VERIFY_SSL=false — TLS certificate verification is DISABLED for Dataminer')


def _configured():
    return bool(DATAMINER_URL and DATAMINER_TOKEN)


# ---------------------------------------------------------------------------
# Snapshot state (in-memory cache + on-disk file)
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()
_state = {
    'snapshot': None,      # parsed snapshot dict
    'file_mtime': None,    # mtime of the file the cache was loaded from
    'refreshing': False,
    'last_attempt': None,  # ISO timestamp of the last refresh attempt in this process
    'last_error': None,    # last error string (any pool) in this process
}
_WAKE = threading.Event()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _load_from_disk(force=False):
    """(Re)load the snapshot file into memory if it changed since last load."""
    try:
        mtime = os.path.getmtime(SNAPSHOT_FILE)
    except OSError:
        with _STATE_LOCK:
            _state['snapshot'] = None
            _state['file_mtime'] = None
        return None

    with _STATE_LOCK:
        if not force and _state['file_mtime'] == mtime and _state['snapshot'] is not None:
            return _state['snapshot']
    try:
        with open(SNAPSHOT_FILE, 'r') as f:
            snapshot = json.load(f)
    except (OSError, ValueError) as exc:
        log.error('Failed to read %s: %s', SNAPSHOT_FILE, exc)
        return _state['snapshot']
    with _STATE_LOCK:
        _state['snapshot'] = snapshot
        _state['file_mtime'] = mtime
    return snapshot


def _current_snapshot():
    return _load_from_disk()


def _write_snapshot(snapshot):
    """Atomic write (tmp + os.replace), owner-only permissions."""
    tmp_path = SNAPSHOT_FILE + '.tmp'
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(snapshot, f, indent=1)
    os.replace(tmp_path, SNAPSHOT_FILE)
    try:
        os.chmod(SNAPSHOT_FILE, 0o600)
    except OSError:
        pass


def _fetch_pool(pool):
    """GET /api/custom/resources[?pool=...] and validate the envelope."""
    params = {'pool': pool} if pool else None
    resp = _session.get(f'{DATAMINER_URL}{RESOURCES_PATH}', params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return _normalise_payload(resp.json(), pool)


def _normalise_payload(payload, pool):
    """Accept both Dataminer response shapes and return {pool, count, items}."""
    if isinstance(payload, dict) and isinstance(payload.get('items'), list):
        return {
            'pool': payload.get('pool') or pool or DEFAULT_POOL_LABEL,
            'count': payload.get('count', len(payload['items'])),
            'items': payload['items'],
        }
    if isinstance(payload, dict) and isinstance(payload.get('pools'), dict):
        pools = payload['pools']
        wanted = pool or DEFAULT_POOL_LABEL
        name, entry = wanted, pools.get(wanted)
        if entry is None and len(pools) == 1:
            name, entry = next(iter(pools.items()))
        if isinstance(entry, dict) and isinstance(entry.get('items'), list):
            return {'pool': name, 'count': entry.get('count', len(entry['items'])), 'items': entry['items']}
        raise ValueError(f"Dataminer response has no pool '{wanted}' (got: {', '.join(pools) or 'none'})")
    raise ValueError('Unexpected response shape from Dataminer (expected {pool,count,items} or {pools:{...}})')


def refresh_snapshot(reason='scheduled'):
    """Fetch every pool and persist a new snapshot. Safe across processes."""
    if not _configured():
        return {'ok': False, 'error': 'Dataminer API is not configured (DATAMINER_API_URL / DATAMINER_BEARER_TOKEN)'}

    lock_fd = open(LOCK_FILE, 'a+')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return {'ok': False, 'error': 'A refresh is already in progress'}

    started = time.monotonic()
    now = _now_iso()
    with _STATE_LOCK:
        _state['refreshing'] = True
        _state['last_attempt'] = now

    try:
        previous = _current_snapshot() or {}
        snapshot = {
            'version': 1,
            'fetched_at': now,
            'last_success_at': previous.get('last_success_at'),
            'reason': reason,
            'source': f'{DATAMINER_URL}{RESOURCES_PATH}',
            'pools': dict(previous.get('pools') or {}),
            'errors': {},
        }

        any_ok = False
        for key, pool in POOLS.items():
            try:
                payload = _fetch_pool(pool)
                items = payload['items']
                snapshot['pools'][key] = {
                    'pool': payload['pool'],
                    'count': payload.get('count', len(items)),
                    'fetched_at': now,
                    'items': items,
                }
                any_ok = True
                log.info('BTE snapshot: pool %s (%s) → %d items', key, snapshot['pools'][key]['pool'], len(items))
            except (requests.RequestException, ValueError) as exc:
                snapshot['errors'][key] = str(exc)
                log.error('BTE snapshot: pool %s failed: %s', key, exc)

        if any_ok:
            snapshot['last_success_at'] = now
        snapshot['duration_ms'] = int((time.monotonic() - started) * 1000)

        _write_snapshot(snapshot)
        _load_from_disk(force=True)

        with _STATE_LOCK:
            _state['last_error'] = '; '.join(f'{k}: {v}' for k, v in snapshot['errors'].items()) or None

        return {
            'ok': not snapshot['errors'],
            'partial': bool(snapshot['errors']) and any_ok,
            'fetched_at': now,
            'duration_ms': snapshot['duration_ms'],
            'errors': snapshot['errors'],
            'pools': {k: {'pool': v['pool'], 'count': len(v['items'])} for k, v in snapshot['pools'].items()},
        }
    finally:
        with _STATE_LOCK:
            _state['refreshing'] = False
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _seconds_until_due():
    snapshot = _current_snapshot()
    if not snapshot:
        return 0
    fetched = _parse_iso(snapshot.get('fetched_at'))
    if not fetched:
        return 0
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return max(0, SNAPSHOT_INTERVAL - age)


def _refresher_loop():
    log.info('BTE snapshot refresher started (interval %ss)', SNAPSHOT_INTERVAL)
    while True:
        due_in = _seconds_until_due()
        if due_in <= 0:
            result = refresh_snapshot('scheduled')
            # Back off on failure so a broken API is not hammered every loop.
            wait = SNAPSHOT_INTERVAL if result.get('ok') or result.get('partial') else 300
        else:
            wait = due_in
        _WAKE.wait(timeout=min(wait, 300))
        _WAKE.clear()


def start_background_refresher():
    if SNAPSHOT_DISABLED:
        log.info('BTE snapshot refresher disabled by DATAMINER_SNAPSHOT_DISABLED')
        return
    if not _configured():
        log.warning('BTE snapshot refresher not started: Dataminer API not configured')
        return
    thread = threading.Thread(target=_refresher_loop, name='bte-snapshot-refresher', daemon=True)
    thread.start()


start_background_refresher()
prov.start_reaper()


# ---------------------------------------------------------------------------
# Helpers for the HTTP layer
# ---------------------------------------------------------------------------

def _get_role():
    session = _get_session(_token_from_request())
    return session.get('role') if session else None


def _get_user_and_role():
    session = _get_session(_token_from_request())
    if not session:
        return None, None
    return session.get('username', 'anonymous'), session.get('role')


def _find_pool_item(pool_key, item_id):
    """Raw (un-redacted) item from a snapshot pool ('resources' or 'destinations'), or None."""
    snapshot = _current_snapshot() or {}
    pool = (snapshot.get('pools') or {}).get(pool_key) or {}
    for item in pool.get('items') or []:
        if item.get('id') == item_id:
            return item
    return None


def _find_snapshot_item(resource_id):
    """Raw (un-redacted) Supplier Dynamic item from the snapshot, or None."""
    return _find_pool_item('resources', resource_id)


def _resolve_destinations(data):
    """Resolve requested destination_ids to raw snapshot items.

    Returns (items, error). Rejects unknown ids and ids already attached to
    another (non-final) lease, without partially applying the request.
    """
    ids = data.get('destination_ids') or []
    if not isinstance(ids, list):
        return None, '"destination_ids" must be a list'
    ids = [str(i).strip() for i in ids if str(i).strip()]
    if not ids:
        return [], None
    used = prov.used_destination_ids()
    items, missing, taken = [], [], []
    for dest_id in ids:
        item = _find_pool_item('destinations', dest_id)
        if item is None:
            missing.append(dest_id)
        elif dest_id in used:
            taken.append(item.get('name') or dest_id)
        else:
            items.append(item)
    if missing:
        return None, f'Destination(s) not found in the snapshot: {", ".join(missing)}'
    if taken:
        return None, f'Destination(s) already in use: {", ".join(taken)}'
    return items, None


def _forbidden():
    return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403


NO_TYPE = '(no type)'
EDGE_GROUP_INX0123 = ('INX01', 'INX02', 'INX03')


def _item_type(item):
    caps = item.get('capabilities') or {}
    return str(caps.get('Type') or '').strip() or NO_TYPE


def _item_edge(item):
    props = item.get('properties') or {}
    return str(props.get('DC MWEdge') or '').strip()


def _edge_matches(item, edge):
    """``edge`` is '', a TXEdge name, or the group keyword 'inx0123'."""
    if not edge:
        return True
    actual = _item_edge(item).upper()
    if edge.lower() == 'inx0123':
        return actual in EDGE_GROUP_INX0123
    return actual == edge.upper()


def _redact_item(item):
    redacted = copy.deepcopy(item)
    for section in ('properties', 'capabilities'):
        block = redacted.get(section)
        if isinstance(block, dict):
            for key, value in block.items():
                if _SECRET_KEY_RE.search(str(key)) and value not in (None, ''):
                    block[key] = REDACTED
    return redacted


def _matches(item, q):
    if not q:
        return True
    q = q.lower()
    if q in str(item.get('name', '')).lower() or q in str(item.get('id', '')).lower():
        return True
    for section in ('properties', 'capabilities'):
        block = item.get(section) or {}
        if any(q in str(k).lower() or q in str(v).lower() for k, v in block.items()):
            return True
    return False


def _pool_response(key):
    snapshot = _current_snapshot()
    pool = (snapshot or {}).get('pools', {}).get(key)
    if not pool:
        # No snapshot yet: not an error, the UI shows the hint and offers a refresh.
        return jsonify({
            'key': key,
            'pool': None,
            'available': False,
            'hint': 'No snapshot yet — use "Refresh snapshot now" or wait for the hourly refresh',
            'fetched_at': None,
            'count': 0,
            'returned': 0,
            'error': ((snapshot or {}).get('errors') or {}).get(key),
            'items': [],
        })

    q = (request.args.get('q') or '').strip()
    mode = (request.args.get('mode') or '').strip().lower()
    rtype = (request.args.get('type') or '').strip()
    edge = (request.args.get('edge') or '').strip()

    items = pool['items']
    if q or mode or rtype or edge:
        items = [
            i for i in items
            if _matches(i, q)
            and (not mode or str(i.get('mode', '')).lower() == mode)
            and (not rtype or _item_type(i) == rtype)
            and _edge_matches(i, edge)
        ]
    items = sorted((_redact_item(i) for i in items), key=lambda i: str(i.get('name', '')).lower())

    return jsonify({
        'key': key,
        'pool': pool['pool'],
        'fetched_at': pool.get('fetched_at'),
        'count': pool.get('count', len(pool['items'])),
        'returned': len(items),
        'error': (snapshot.get('errors') or {}).get(key),
        'items': items,
    })


def _snapshot_meta(snapshot):
    if not snapshot:
        return {'exists': False}
    now = datetime.now(timezone.utc)
    fetched = _parse_iso(snapshot.get('fetched_at'))
    success = _parse_iso(snapshot.get('last_success_at'))
    return {
        'exists': True,
        'fetched_at': snapshot.get('fetched_at'),
        'last_success_at': snapshot.get('last_success_at'),
        'age_seconds': int((now - fetched).total_seconds()) if fetched else None,
        'success_age_seconds': int((now - success).total_seconds()) if success else None,
        'next_refresh_in_seconds': None if SNAPSHOT_DISABLED else int(_seconds_until_due()),
        'duration_ms': snapshot.get('duration_ms'),
        'errors': snapshot.get('errors') or {},
        'pools': {
            k: {'pool': v.get('pool'), 'count': len(v.get('items') or []), 'fetched_at': v.get('fetched_at')}
            for k, v in (snapshot.get('pools') or {}).items()
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bte_bp.route('/status', methods=['GET'])
def get_status():
    """Configuration + snapshot health. Never returns secret values."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    with _STATE_LOCK:
        refreshing = _state['refreshing']
        last_attempt = _state['last_attempt']
        last_error = _state['last_error']
    return jsonify({
        'label': 'BTE',
        'txcore_cluster': 'main',
        'api_url_set': bool(DATAMINER_URL),
        'api_url': DATAMINER_URL,
        'bearer_token_set': bool(DATAMINER_TOKEN),
        'verify_ssl': _session.verify is not False,
        'ca_bundle_set': bool(DATAMINER_CA_BUNDLE),
        'interval_seconds': SNAPSHOT_INTERVAL,
        'scheduler_enabled': not SNAPSHOT_DISABLED,
        'pools': {k: (v or DEFAULT_POOL_LABEL) for k, v in POOLS.items()},
        'refreshing': refreshing,
        'last_attempt': last_attempt,
        'last_error': last_error,
        'snapshot': _snapshot_meta(_current_snapshot()),
    })


@bte_bp.route('/refresh', methods=['POST'])
def post_refresh():
    """Force a snapshot refresh now (runs synchronously; ~seconds)."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    if not _configured():
        missing = [n for n, v in (('DATAMINER_API_URL', DATAMINER_URL),
                                  ('DATAMINER_BEARER_TOKEN', DATAMINER_TOKEN)) if not v]
        return jsonify({
            'error': 'Dataminer API is not configured on the server — missing: ' + ', '.join(missing),
        }), 500
    try:
        result = refresh_snapshot('manual')
    except OSError as exc:
        log.exception('BTE snapshot refresh failed (filesystem)')
        return jsonify({
            'error': f'Cannot write the snapshot under {DATA_DIR}: {exc.strerror or exc}',
            'hint': 'Check ownership/permissions of the data directory for the toolbox process user',
        }), 500
    if not result.get('ok') and not result.get('partial'):
        status = 409 if 'already in progress' in (result.get('error') or '') else 502
        return jsonify(result), status
    _WAKE.set()  # re-arm the scheduler clock from this refresh
    return jsonify(result)


@bte_bp.route('/resources', methods=['GET'])
def list_resources():
    """Supplier Dynamic resources from the snapshot.

    Filters: ?q= (text), ?mode= (Available/Unavailable), ?type= (supplier,
    i.e. capabilities.Type), ?edge= (TXEdge name or 'inx0123' for INX01-03).
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    return _pool_response('resources')


@bte_bp.route('/suppliers', methods=['GET'])
def list_suppliers():
    """Suppliers (capabilities.Type) present in the Supplier Dynamic pool.

    Returns per supplier: channel count, count per TXEdge and per mode.
    Optional ?edge= narrows the counts to that TXEdge (or 'inx0123').
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    snapshot = _current_snapshot() or {}
    pool = (snapshot.get('pools') or {}).get('resources')
    if not pool:
        return jsonify({'available': False, 'suppliers': [], 'count': 0,
                        'hint': 'No snapshot yet — refresh first'})

    edge = (request.args.get('edge') or '').strip()
    suppliers = {}
    for item in pool['items']:
        if not _edge_matches(item, edge):
            continue
        entry = suppliers.setdefault(_item_type(item), {'type': _item_type(item), 'count': 0, 'edges': {}, 'modes': {}})
        entry['count'] += 1
        e = _item_edge(item) or '(none)'
        entry['edges'][e] = entry['edges'].get(e, 0) + 1
        m = str(item.get('mode') or '?')
        entry['modes'][m] = entry['modes'].get(m, 0) + 1

    ordered = sorted(suppliers.values(), key=lambda s: (s['type'] == NO_TYPE, s['type'].lower()))
    return jsonify({
        'available': True,
        'pool': pool['pool'],
        'fetched_at': pool.get('fetched_at'),
        'edge': edge or None,
        'count': len(ordered),
        'suppliers': ordered,
    })


@bte_bp.route('/resources/<resource_id>', methods=['GET'])
def get_resource(resource_id):
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    if not re.fullmatch(r'[0-9a-fA-F-]{8,64}', resource_id):
        return jsonify({'error': 'Resource not found'}), 404
    snapshot = _current_snapshot() or {}
    for key, pool in (snapshot.get('pools') or {}).items():
        for item in pool.get('items') or []:
            if item.get('id') == resource_id:
                out = _redact_item(item)
                out['_pool'] = pool.get('pool')
                out['_pool_key'] = key
                return jsonify(out)
    return jsonify({'error': 'Resource not found'}), 404


@bte_bp.route('/destinations', methods=['GET'])
def list_destinations():
    """Destination pool resources from the snapshot. Filters: ?q=, ?mode=.

    Each item is annotated with ``in_use`` — attached to some non-final BTE
    lease already, so it cannot be selected again until freed.
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    resp = _pool_response('destinations')
    payload = resp.get_json()
    if isinstance(payload, dict) and isinstance(payload.get('items'), list):
        used = prov.used_destination_ids()
        for item in payload['items']:
            item['in_use'] = item.get('id') in used
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Provisioning — leases over TXCore MWEdge objects (see bte_provisioning.py)
# ---------------------------------------------------------------------------

_RESOURCE_ID_RE = re.compile(r'[0-9a-fA-F-]{8,64}')
_LEASE_ID_RE = re.compile(r'[0-9a-f]{32}')


def _passphrase_override(data):
    """Optional supplier passphrase typed in the UI; used for the call, never echoed back."""
    value = str(data.get('passphrase') or '').strip()
    return value or None


def _dry_run_forced(requested):
    """Live writes need BTE_PROVISIONING_ENABLED and a configured TXCore MAIN API."""
    return bool(requested) or not prov.PROVISIONING_ENABLED or not prov.configured()


@bte_bp.route('/provisioning/status', methods=['GET'])
def provisioning_status():
    """Configuration of the write side. Never returns secret values."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    status = prov.config_status()
    leases = prov.list_leases()
    status['active_leases'] = sum(1 for l in leases if l['status'] in prov.ACTIVE_STATUSES)
    return jsonify(status)


@bte_bp.route('/provision/plan', methods=['POST'])
def provision_plan():
    """Preview of the TXCore calls "Create resources" would make (passphrases masked)."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    resource_id = str(data.get('resource_id') or '').strip()
    if not _RESOURCE_ID_RE.fullmatch(resource_id):
        return jsonify({'error': 'Missing or invalid resource_id'}), 400
    item = _find_snapshot_item(resource_id)
    if item is None:
        return jsonify({'error': 'Resource not found in the snapshot'}), 404
    destinations, error = _resolve_destinations(data)
    if error:
        return jsonify({'error': error}), 400
    plan = prov.redact_plan(prov.build_plan(item, passphrase_override=_passphrase_override(data), destinations=destinations))
    plan['resource_id'] = resource_id
    plan['resource_name'] = item.get('name')
    plan['dry_run'] = _dry_run_forced(data.get('dry_run', True))
    plan['live_writes_allowed'] = prov.PROVISIONING_ENABLED and prov.configured()
    existing = prov.active_lease_for_resource(resource_id)
    if existing:
        plan['already_active'] = {'lease_id': existing['lease_id'], 'status': existing['status']}
    return jsonify(plan), (200 if plan['ok'] else 422)


@bte_bp.route('/provision', methods=['POST'])
def provision_create():
    """Create the resources for one channel and open a lease. Runs in the background."""
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    resource_id = str(data.get('resource_id') or '').strip()
    if not _RESOURCE_ID_RE.fullmatch(resource_id):
        return jsonify({'error': 'Missing or invalid resource_id'}), 400
    item = _find_snapshot_item(resource_id)
    if item is None:
        return jsonify({'error': 'Resource not found in the snapshot'}), 404

    existing = prov.active_lease_for_resource(resource_id)
    if existing:
        return jsonify({'error': f"This channel already has an active BTE lease ({existing['status']})",
                        'lease_id': existing['lease_id']}), 409

    destinations, error = _resolve_destinations(data)
    if error:
        return jsonify({'error': error}), 400

    plan = prov.build_plan(item, passphrase_override=_passphrase_override(data), destinations=destinations)
    if not plan['ok']:
        return jsonify({'error': 'Cannot build a plan for this resource', 'errors': plan['errors'],
                        'warnings': plan['warnings']}), 422

    dry_run = _dry_run_forced(data.get('dry_run', True))
    if not dry_run and prov.EDGES.get(plan['summary']['dc_edge']) is None:
        return jsonify({'error': f"Edge {plan['summary']['dc_edge']} is not configured"}), 422

    lease = prov.create_lease(item, plan, data.get('duration_minutes'), username, dry_run)
    threading.Thread(target=prov.run_lease, args=(lease['lease_id'],),
                     name=f"bte-lease-{lease['lease_id'][:8]}", daemon=True).start()
    return jsonify({
        'lease_id': lease['lease_id'],
        'status': lease['status'],
        'dry_run': dry_run,
        'expires_at': lease['expires_at'],
        'objects': len(lease['objects']),
        'warnings': plan['warnings'],
    }), 202


@bte_bp.route('/leases', methods=['GET'])
def leases_list():
    """All leases (active first) with remaining time; bodies are redacted."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    leases = prov.list_leases()
    return jsonify({
        'count': len(leases),
        'active': sum(1 for l in leases if l['status'] in prov.ACTIVE_STATUSES),
        'default_extend_minutes': prov.DEFAULT_EXTEND_MIN,
        'max_duration_minutes': prov.MAX_DURATION_MIN,
        'tag': prov.BTE_TAG,
        'leases': leases,
    })


@bte_bp.route('/leases/<lease_id>', methods=['GET'])
def lease_get(lease_id):
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    if not _LEASE_ID_RE.fullmatch(lease_id):
        return jsonify({'error': 'Lease not found'}), 404
    for lease in prov.list_leases():
        if lease['lease_id'] == lease_id:
            return jsonify(lease)
    return jsonify({'error': 'Lease not found'}), 404


@bte_bp.route('/leases/<lease_id>/extend', methods=['POST'])
def lease_extend(lease_id):
    """Extend a lease. Body: {"minutes": 30} (default BTE_DEFAULT_EXTEND_MINUTES)."""
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return _forbidden()
    if not _LEASE_ID_RE.fullmatch(lease_id):
        return jsonify({'error': 'Lease not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    lease, error, note = prov.extend_lease(lease_id, data.get('minutes', prov.DEFAULT_EXTEND_MIN), username)
    if lease is None:
        return jsonify({'error': error}), 404
    if error:
        return jsonify({'error': error, 'lease_id': lease_id}), 409
    return jsonify({'lease_id': lease_id, 'expires_at': lease['expires_at'],
                    'extensions': lease['extensions'], 'note': note})


@bte_bp.route('/leases/<lease_id>/destinations', methods=['POST'])
def lease_add_destination(lease_id):
    """Attach one Destination pool item as an extra output on this lease's DC edge.

    Body: {"destination_id": "..."}. Works for an already-active (live)
    lease and for destinations selected before "Create resources" runs —
    the frontend's "+ Destination" button covers both. A destination cannot
    be attached to more than one BTE stream at a time.
    """
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return _forbidden()
    if not _LEASE_ID_RE.fullmatch(lease_id):
        return jsonify({'error': 'Lease not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    dest_id = str(data.get('destination_id') or '').strip()
    if not dest_id:
        return jsonify({'error': 'Missing destination_id'}), 400
    item = _find_pool_item('destinations', dest_id)
    if item is None:
        return jsonify({'error': 'Destination not found in the snapshot'}), 404
    try:
        lease, error = prov.add_destination(lease_id, item, username)
    except prov.TXCoreError as exc:
        return jsonify({'error': str(exc)}), 502
    if lease is None:
        return jsonify({'error': error}), 404
    if error:
        return jsonify({'error': error, 'lease_id': lease_id}), 409
    return jsonify({'lease_id': lease_id, 'destination_id': dest_id, 'destination_name': item.get('name'),
                    'objects': len(lease['objects'])}), 201


@bte_bp.route('/leases/<lease_id>', methods=['DELETE'])
def lease_delete(lease_id):
    """Delete everything one lease created. Runs synchronously (a handful of calls)."""
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return _forbidden()
    if not _LEASE_ID_RE.fullmatch(lease_id):
        return jsonify({'error': 'Lease not found'}), 404
    if prov.get_lease(lease_id) is None:
        return jsonify({'error': 'Lease not found'}), 404
    try:
        lease = prov.delete_lease(lease_id, 'manual', username)
    except prov.TXCoreError as exc:
        return jsonify({'error': str(exc)}), 502
    return jsonify({'lease_id': lease_id, 'status': lease['status'], 'errors': lease.get('errors') or []})


@bte_bp.route('/leases', methods=['DELETE'])
def leases_delete_all():
    """Delete every active BTE lease. Requires ?confirm=BTE (or {"confirm": "BTE"})."""
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return _forbidden()
    data = request.get_json(force=True, silent=True) or {}
    if (request.args.get('confirm') or data.get('confirm')) != 'BTE':
        return jsonify({'error': 'Confirmation required: send confirm=BTE'}), 400
    results = prov.delete_all_leases('manual-all', username)
    return jsonify({
        'count': len(results),
        'deleted': sum(1 for r in results if r['status'] == 'deleted'),
        'results': results,
    })


_EDGE_KEY_RE = re.compile(r'[A-Za-z0-9]{2,16}')


@bte_bp.route('/txcore/edges/<edge_key>', methods=['GET'])
def txcore_edge_inspect(edge_key):
    """Live MWEdge document from TXCore MAIN (streams/sources/outputs) with secrets masked.

    Used to confirm option field names (e.g. how an SRT listener is stored)
    against real objects on the edge.
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    if not _EDGE_KEY_RE.fullmatch(edge_key):
        return jsonify({'error': 'Edge not found'}), 404
    edge = prov.EDGES.get(edge_key.upper())
    if edge is None:
        return jsonify({'error': f'Edge {edge_key} is not configured (BTE_EDGE_{edge_key.upper()})'}), 404
    if not prov.configured():
        return jsonify({'error': 'TXCore MAIN API is not configured (APIURLMAIN / BEARER_TOKEN_MAIN)'}), 500
    try:
        doc = prov.TXCoreClient().get_edge(edge['id'])
    except prov.TXCoreError as exc:
        return jsonify({'error': str(exc)}), 502
    q = (request.args.get('q') or '').strip().lower()
    if q and isinstance(doc, dict):
        # Narrow the (large) document to the objects whose JSON mentions the filter.
        for key in ('streams', 'sources', 'outputs'):
            if isinstance(doc.get(key), list):
                doc[key] = [o for o in doc[key] if q in json.dumps(o).lower()]
    return jsonify({'edge': edge['key'], 'edge_id': edge['id'], 'location': edge.get('location'),
                    'document': prov.redact_body(doc)})
