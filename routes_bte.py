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

    # TXEdge topology of the MAIN cluster (never hard-coded in the source)
    BTE_EDGES                    Optional display order, e.g.
                                 INX01,INX02,INX03,AVE02,LMK01,YER01
    BTE_EDGE_<NAME>              One entry per TXEdge, semicolon-separated
                                 key=value fields:
                                   id=<24-hex MWEdge id>
                                   location=<free text>
                                   dc=yes|no        (yes = DC edge, no = regional)
                                   in=PROTO@ip[,PROTO@ip...]   incoming interfaces
                                   out=PROTO@ip[,PROTO@ip...]  outgoing interfaces
                                 PROTO is one of SRT, UDP, RTP. Example:
                                 BTE_EDGE_INX01=id=69a5...;location=INX (DC1);dc=yes;
                                   in=SRT@10.11.203.1,UDP@10.11.235.1;out=SRT@10.11.203.1

    The TXCore MAIN API itself is configured through BEARER_TOKEN_MAIN /
    APIURLMAIN (shared with the TXCore bulk-creation blueprint).

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
import ipaddress
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
from routes_txcore import CLUSTERS as TXCORE_CLUSTERS

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

# ---------------------------------------------------------------------------
# TXEdge topology (MAIN cluster), loaded from .env — see module docstring
# ---------------------------------------------------------------------------

_EDGE_ID_RE = re.compile(r'^[0-9a-f]{24}$')
_EDGE_VAR_RE = re.compile(r'^BTE_EDGE_([A-Z0-9]{2,16})$')
EDGE_PROTOCOLS = ('SRT', 'UDP', 'RTP')


def _parse_interfaces(spec, direction, edge_name):
    """Parse ``PROTO@ip,PROTO@ip`` into interface dicts. Returns (interfaces, errors)."""
    interfaces, errors = [], []
    for token in (t.strip() for t in (spec or '').split(',')):
        if not token:
            continue
        proto, sep, ip = token.partition('@')
        proto, ip = proto.strip().upper(), ip.strip()
        if not sep or proto not in EDGE_PROTOCOLS:
            errors.append(f'{edge_name}: invalid {direction} interface "{token}" (expected PROTO@ip)')
            continue
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            errors.append(f'{edge_name}: invalid IP "{ip}" on {direction} {proto} interface')
            continue
        interfaces.append({'direction': direction, 'protocol': proto, 'ip': ip})
    return interfaces, errors


def _parse_edge(name, raw):
    """Parse one BTE_EDGE_<NAME> value. Returns (edge, errors); edge is None on error."""
    fields = {}
    for part in raw.split(';'):
        key, _, value = part.partition('=')
        if key.strip():
            fields[key.strip().lower()] = value.strip()

    errors = []
    edge_id = fields.get('id', '').lower()
    if not _EDGE_ID_RE.match(edge_id):
        errors.append(f'{name}: id must be a 24-character hex MWEdge id')
    dc = fields.get('dc', 'no').lower() in ('1', 'y', 'yes', 'true', 'on')
    inbound, in_errors = _parse_interfaces(fields.get('in'), 'in', name)
    outbound, out_errors = _parse_interfaces(fields.get('out'), 'out', name)
    errors.extend(in_errors + out_errors)
    if not inbound and not outbound:
        errors.append(f'{name}: no interfaces defined (in=/out=)')
    if errors:
        return None, errors
    return {
        'name': name,
        'id': edge_id,
        'location': fields.get('location') or name,
        'dc': dc,
        'interfaces': inbound + outbound,
    }, []


def _load_edges():
    """Read every BTE_EDGE_* variable. Invalid edges are dropped and reported (fail closed)."""
    order = [n.strip().upper() for n in (_env('BTE_EDGES') or '').split(',') if n.strip()]
    edges, errors = {}, []
    for key, raw in os.environ.items():
        match = _EDGE_VAR_RE.match(key)
        if not match or not raw.strip():
            continue
        edge, edge_errors = _parse_edge(match.group(1), raw)
        if edge_errors:
            errors.extend(edge_errors)
            log.error('BTE edge %s ignored: %s', match.group(1), '; '.join(edge_errors))
        else:
            edges[edge['name']] = edge
    for name in order:
        if name not in edges and not any(e.startswith(name + ':') for e in errors):
            errors.append(f'{name}: listed in BTE_EDGES but BTE_EDGE_{name} is not set')
    ordered = [edges[n] for n in order if n in edges]
    ordered += [edges[n] for n in sorted(edges) if n not in order]
    return ordered, errors


EDGES, EDGE_ERRORS = _load_edges()
EDGES_BY_NAME = {e['name']: e for e in EDGES}
DC_EDGES = [e['name'] for e in EDGES if e['dc']]
REGIONAL_EDGES = [e['name'] for e in EDGES if not e['dc']]


def edge_interface(edge_name, direction, protocol):
    """IP of the ``direction`` (in/out) ``protocol`` interface of a TXEdge, or None."""
    edge = EDGES_BY_NAME.get(str(edge_name or '').upper())
    for iface in (edge or {}).get('interfaces', []):
        if iface['direction'] == direction and iface['protocol'] == protocol.upper():
            return iface['ip']
    return None


def _txcore_main_meta():
    """Whether the TXCore MAIN API (target of BTE provisioning) is configured. No secrets."""
    main = TXCORE_CLUSTERS.get('main') or {}
    return {
        'api_url_set': bool(main.get('url')),
        'api_url': main.get('url'),
        'bearer_token_set': bool(main.get('token')),
    }

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


# ---------------------------------------------------------------------------
# Helpers for the HTTP layer
# ---------------------------------------------------------------------------

def _get_role():
    session = _get_session(_token_from_request())
    return session.get('role') if session else None


def _forbidden():
    return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403


NO_TYPE = '(no type)'
# Group keyword accepted by ?edge= : 'dc' (or legacy 'inx0123') = every DC edge
# from .env; falls back to INX01-03 when no topology is configured.
EDGE_GROUP_KEYWORDS = ('dc', 'inx0123')
EDGE_GROUP_DC = tuple(DC_EDGES) or ('INX01', 'INX02', 'INX03')


def _item_type(item):
    caps = item.get('capabilities') or {}
    return str(caps.get('Type') or '').strip() or NO_TYPE


def _item_edge(item):
    props = item.get('properties') or {}
    return str(props.get('DC MWEdge') or '').strip()


def _edge_matches(item, edge):
    """``edge`` is '', a TXEdge name, or a group keyword ('dc' / 'inx0123')."""
    if not edge:
        return True
    actual = _item_edge(item).upper()
    if edge.lower() in EDGE_GROUP_KEYWORDS:
        return actual in EDGE_GROUP_DC
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


def _snapshot_edge_counts(snapshot):
    """Channels per ``DC MWEdge`` value (upper-cased) in the Supplier Dynamic pool."""
    counts = {}
    pool = ((snapshot or {}).get('pools') or {}).get('resources') or {}
    for item in pool.get('items') or []:
        name = _item_edge(item).upper() or '(none)'
        counts[name] = counts.get(name, 0) + 1
    return counts


def _edges_meta(snapshot):
    counts = _snapshot_edge_counts(snapshot)
    return {
        'count': len(EDGES),
        'dc': DC_EDGES,
        'regional': REGIONAL_EDGES,
        'errors': EDGE_ERRORS,
        # TXEdges referenced by Dataminer channels but absent from .env:
        # provisioning for those channels would have nowhere to go.
        'unmapped_in_snapshot': sorted(
            n for n in counts if n != '(none)' and n not in EDGES_BY_NAME
        ),
    }


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
        'edges': _edges_meta(_current_snapshot()),
        'txcore_main': _txcore_main_meta(),
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
    i.e. capabilities.Type), ?edge= (TXEdge name, or 'dc' for every DC edge).
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    return _pool_response('resources')


@bte_bp.route('/suppliers', methods=['GET'])
def list_suppliers():
    """Suppliers (capabilities.Type) present in the Supplier Dynamic pool.

    Returns per supplier: channel count, count per TXEdge and per mode.
    Optional ?edge= narrows the counts to that TXEdge (or 'dc' for every DC edge).
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


@bte_bp.route('/edges', methods=['GET'])
def list_edges():
    """MAIN TXEdge topology from .env, with the Dataminer channel count per edge.

    Interface IPs are internal infrastructure data and are only returned to
    admin/engineer sessions (same audience as the SRT presets of the MAIN tab).
    """
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    snapshot = _current_snapshot()
    counts = _snapshot_edge_counts(snapshot)
    edges = []
    for edge in EDGES:
        out = copy.deepcopy(edge)
        out['channels'] = counts.get(edge['name'], 0)
        edges.append(out)
    meta = _edges_meta(snapshot)
    meta['edges'] = edges
    if not edges:
        meta['hint'] = 'No TXEdge configured — add BTE_EDGE_<NAME> entries to the server .env'
    return jsonify(meta)


@bte_bp.route('/destinations', methods=['GET'])
def list_destinations():
    """Destination pool resources from the snapshot. Filters: ?q=, ?mode=."""
    if _get_role() not in ALLOWED_ROLES:
        return _forbidden()
    return _pool_response('destinations')
