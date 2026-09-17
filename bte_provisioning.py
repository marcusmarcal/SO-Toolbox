"""
TXCore Provisioning - BTE provisioning layer (leases, planner, TXCore client)

Turns a Dataminer "Supplier Dynamic" resource (as kept in the BTE snapshot)
into a set of TXCore MWEdge objects and keeps track of everything it creates
so it can be removed again — on demand or automatically when the lease
expires.

Topology created per channel ("Create resources" in the BTE tab), mirroring
the property model of the Dataminer StreamResourceCreation script:

    DC edge (properties["DC MWEdge"], e.g. INX01)
        source  SRT   from properties["Input Main"]   (+ "Input Backup" if different)
        stream
        output  SRT listener on properties["Output"]  (internal passphrase)

    AVE / LMK / YER edges (always, one per site with a multicast address)
        source  SRT caller  <DC edge host>:<Output port> (internal passphrase)
        stream
        output  UDP multicast from properties["<Site> Multicast"]

Every object name carries BTE_TAG ("[BTE]"). The tag is the primary guard:
nothing is ever deleted unless (a) it is recorded in the lease registry AND
(b) the live object name fetched from TXCore still contains the tag.

Lease registry: /opt/web/data/bte_leases.json (mode 0600, atomic replace,
fcntl lock shared by all gunicorn workers).

Environment variables (.env):
    APIURLMAIN / BEARER_TOKEN_MAIN      TXCore MAIN API (shared with routes_txcore)
    INTERNALSRTPASSPHRASE               Passphrase for edge-to-edge / edge-to-core SRT hops
    BTE_PROVISIONING_ENABLED            "true" to allow live TXCore writes (default false:
                                        every request is forced to dry-run)
    BTE_EDGE_<KEY>                      One entry per TXEdge, ``;``-separated key=value fields:
        id=<txcore edge id>;location=<label>;dc=yes|no;in=SRT@<host>;out=SRT@<host>,UDP@<host>
                                        e.g. BTE_EDGE_INX01=id=611d…;location=DC1 (INX);dc=yes;
                                             in=SRT@10.138.38.25;out=SRT@10.138.38.25,UDP@10.138.38.25
                                        The key must match properties["DC MWEdge"] for DC edges
                                        (INX01…) and start with the site prefix AVE / LMK / YER
                                        for regional edges (AVE02, LMK01, YER01…). Regional
                                        edges pull from the DC edge's out=SRT@<host>.
    BTE_TXCORE_SOURCE_PATH              Default /mwedge/{edge}/source/
    BTE_TXCORE_STREAM_PATH              Default /mwedge/{edge}/stream/
    BTE_TXCORE_OUTPUT_PATH              Default /mwedge/{edge}/output/
                                        {edge} is replaced by the TXCore edge id. A single
                                        object is addressed as <path><object id>.
    BTE_DEFAULT_DURATION_MINUTES        Lease length offered by default (60)
    BTE_DEFAULT_EXTEND_MINUTES          Extension offered by default (30)
    BTE_MAX_DURATION_MINUTES            Hard cap for a lease, including extensions (1440)
    BTE_REAPER_DISABLED                 "true" to disable automatic deletion of expired leases

!! The MWEdge endpoint paths and request-body field names below are the
!! integration contract with TXCore and must be confirmed against the TXCore
!! API reference of the MAIN cluster before BTE_PROVISIONING_ENABLED is set.
!! Until then the plan preview shows exactly what would be sent.
"""

import copy
import fcntl
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

log = logging.getLogger('so-toolbox.bte.provisioning')

BTE_TAG = '[BTE]'
REDACTED = '********'

DATA_DIR = '/opt/web/data'
LEASES_FILE = os.path.join(DATA_DIR, 'bte_leases.json')
LEASES_LOCK_FILE = LEASES_FILE + '.lock'
REAPER_LOCK_FILE = os.path.join(DATA_DIR, 'bte_reaper.lock')

REQUEST_TIMEOUT = (10, 30)
HISTORY_KEEP = 200          # finished leases kept for the UI history
REAPER_INTERVAL = 60        # seconds

os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env(*names):
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


def _env_int(name, default, minimum=1):
    value = _env(name)
    try:
        return max(minimum, int(value)) if value else default
    except ValueError:
        return default


TXCORE_URL = (_env('APIURLMAIN') or '').rstrip('/') or None
TXCORE_TOKEN = _env('BEARER_TOKEN_MAIN')
INTERNAL_PASSPHRASE = _env('INTERNALSRTPASSPHRASE', 'SRT_PASSPHRASE')

PROVISIONING_ENABLED = _env_bool('BTE_PROVISIONING_ENABLED', False)
REAPER_DISABLED = _env_bool('BTE_REAPER_DISABLED', False)
DEFAULT_DURATION_MIN = _env_int('BTE_DEFAULT_DURATION_MINUTES', 60)
DEFAULT_EXTEND_MIN = _env_int('BTE_DEFAULT_EXTEND_MINUTES', 30)
MAX_DURATION_MIN = _env_int('BTE_MAX_DURATION_MINUTES', 24 * 60)

PATHS = {
    'source': _env('BTE_TXCORE_SOURCE_PATH') or '/mwedge/{edge}/source/',
    'stream': _env('BTE_TXCORE_STREAM_PATH') or '/mwedge/{edge}/stream/',
    'output': _env('BTE_TXCORE_OUTPUT_PATH') or '/mwedge/{edge}/output/',
}

# Regional sites: site prefix (edge keys AVE02, LMK01, ... start with it) -> Dataminer multicast property.
REGIONAL_SITES = (
    ('AVE', 'Aveiro Multicast'),
    ('LMK', 'Limerick Multicast'),
    ('YER', 'Yerevan Multicast'),
)
_EDGE_KEY_RE = re.compile(r'BTE_EDGE_([A-Za-z0-9]+)')
_HOST_LIST_RE = re.compile(r'\s*([A-Za-z]+)@([^,;\s]+)')


def _parse_host_list(value):
    """'SRT@10.0.0.1,UDP@10.0.0.2' -> {'SRT': '10.0.0.1', 'UDP': '10.0.0.2'}."""
    return {m.group(1).upper(): m.group(2) for m in _HOST_LIST_RE.finditer(value or '')}


def parse_edge(key, raw):
    """Parse one BTE_EDGE_<KEY> value. Returns the edge dict or None when it has no id."""
    fields = {}
    for part in str(raw).split(';'):
        name, sep, value = part.partition('=')
        if sep:
            fields[name.strip().lower()] = value.strip()
    edge_id = fields.get('id')
    if not edge_id:
        return None
    key = key.upper()
    return {
        'key': key,
        'id': edge_id,
        'location': fields.get('location') or key,
        'dc': fields.get('dc', '').lower() in ('yes', 'true', '1'),
        'in': _parse_host_list(fields.get('in')),
        'out': _parse_host_list(fields.get('out')),
        'site': next((s for s, _ in REGIONAL_SITES if key.startswith(s)), None),
    }


def _load_edges():
    edges = {}
    for name, raw in os.environ.items():
        m = _EDGE_KEY_RE.fullmatch(name)
        if not m or not raw.strip():
            continue
        edge = parse_edge(m.group(1), raw)
        if edge is None:
            log.warning('BTE: %s ignored — no id= field', name)
            continue
        edges[edge['key']] = edge
    return edges


EDGES = _load_edges()


def site_edge(site, edges=None):
    """First configured non-DC edge whose key starts with the site prefix (AVE02 for AVE, ...)."""
    edges = EDGES if edges is None else edges
    for key in sorted(edges):
        edge = edges[key]
        if edge.get('site') == site and not edge.get('dc'):
            return edge
    return None


def missing_edges(edges=None):
    """Human-readable list of what the edge configuration still lacks."""
    edges = EDGES if edges is None else edges
    missing = []
    if not any(e.get('dc') for e in edges.values()):
        missing.append('a DC edge (dc=yes)')
    for e in edges.values():
        if e.get('dc') and not e['out'].get('SRT'):
            missing.append(f"{e['key']} out=SRT@<host>")
    for site, _ in REGIONAL_SITES:
        if site_edge(site, edges) is None:
            missing.append(f'site {site}')
    return missing


def configured():
    return bool(TXCORE_URL and TXCORE_TOKEN)


def config_status():
    """Configuration report for /provisioning/status. Never leaks secrets."""
    return {
        'enabled': PROVISIONING_ENABLED and configured(),
        'live_writes_allowed': PROVISIONING_ENABLED,
        'txcore_api_url_set': bool(TXCORE_URL),
        'txcore_api_url': TXCORE_URL,
        'txcore_token_set': bool(TXCORE_TOKEN),
        'internal_passphrase_set': bool(INTERNAL_PASSPHRASE),
        'edges': {k: {'id': v['id'], 'location': v['location'], 'dc': v['dc'], 'site': v['site'],
                      'in': v['in'], 'out': v['out']} for k, v in sorted(EDGES.items())},
        'missing_edges': missing_edges(),
        'paths': PATHS,
        'tag': BTE_TAG,
        'default_duration_minutes': DEFAULT_DURATION_MIN,
        'default_extend_minutes': DEFAULT_EXTEND_MIN,
        'max_duration_minutes': MAX_DURATION_MIN,
        'reaper_enabled': not REAPER_DISABLED,
    }


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dataminer property parsing
# ---------------------------------------------------------------------------

_INPUT_RE = re.compile(r'^(?P<proto>[a-z]+)://(?P<host>[^:|/]*):(?P<port>\d+)(?:\|(?P<mode>[^|]*))?(?:\|(?P<state>[^|]*))?$', re.I)
_MCAST_RE = re.compile(r'^(?:(?P<proto>[a-z]+)://)?(?P<host>[^:/|]+):(?P<port>\d+)$', re.I)


def _blank(value):
    return value is None or str(value).strip().upper() in ('', 'NA', 'N/A', 'NONE', '-1')


def parse_input(value):
    """'srt://1.2.3.4:3000|Pull' -> {protocol, host, port, mode, state} or None."""
    if _blank(value):
        return None
    m = _INPUT_RE.match(str(value).strip())
    if not m:
        return None
    return {
        'protocol': m.group('proto').lower(),
        'host': m.group('host') or None,
        'port': int(m.group('port')),
        'mode': (m.group('mode') or '').strip().lower() or None,
        'state': (m.group('state') or '').strip() or None,
    }


def parse_multicast(value):
    """'udp://231.216.10.1:1234' -> {host, port} or None."""
    if _blank(value):
        return None
    m = _MCAST_RE.match(str(value).strip())
    if not m:
        return None
    return {'host': m.group('host'), 'port': int(m.group('port'))}


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def bte_name(base, edge_key, kind):
    """Object name convention. The tag is mandatory and checked on delete."""
    return f'{base} {BTE_TAG} {edge_key} {kind}'


def has_tag(name):
    return BTE_TAG in str(name or '')


def _srt_source_body(name, inp, latency, passphrase, encryption):
    body = {
        'name': name,
        'type': 'srt',
        'mode': 'listener' if inp['mode'] == 'listener' else 'caller',
        'port': inp['port'],
    }
    if body['mode'] == 'caller':
        body['address'] = inp['host']
    if latency:
        body['latency'] = latency
    if passphrase:
        body['encryption'] = encryption or 'AES-256'
        body['passphrase'] = passphrase
    return body


def _udp_source_body(name, inp):
    body = {'name': name, 'type': inp['protocol'], 'port': inp['port']}
    if inp['host']:
        body['address'] = inp['host']
    return body


def build_plan(item, edges=None):
    """Return {'ok', 'steps', 'warnings', 'errors', 'summary'} for a snapshot item.

    Each step: {seq, edge, edge_id, kind, name, body}. A body value of
    {'$ref': <seq>} is replaced at run time by the TXCore id of that step.
    """
    edges = EDGES if edges is None else edges
    props = item.get('properties') or {}
    base = str(item.get('name') or '').strip()
    warnings, errors, steps = [], [], []

    if not base:
        errors.append('Resource has no name')
    dc_key = str(props.get('DC MWEdge') or '').strip().upper()
    if not dc_key:
        errors.append('Resource has no "DC MWEdge" property')
    dc = edges.get(dc_key)
    if dc_key and not dc:
        errors.append(f'Edge {dc_key} is not configured on the server (BTE_EDGE_{dc_key})')
    elif dc and not dc.get('dc'):
        errors.append(f'BTE_EDGE_{dc_key} is not flagged dc=yes')
    elif dc and not dc['out'].get('SRT'):
        errors.append(f'BTE_EDGE_{dc_key} has no out=SRT@<host> — regional edges cannot pull from it')

    main_in = parse_input(props.get('Input Main'))
    if not main_in:
        errors.append(f'"Input Main" is missing or unparsable: {props.get("Input Main")!r}')
    backup_in = parse_input(props.get('Input Backup'))
    out_port = _int_or_none(props.get('Output'))
    if not out_port or not 1 <= out_port <= 65535:
        errors.append(f'"Output" port is missing or invalid: {props.get("Output")!r}')
    if not INTERNAL_PASSPHRASE:
        errors.append('INTERNALSRTPASSPHRASE is not set on the server (needed for the edge-to-edge SRT hops)')

    if errors:
        return {'ok': False, 'steps': [], 'warnings': warnings, 'errors': errors, 'summary': {}}

    latency = _int_or_none(props.get('Latency'))
    latency = latency if latency and latency > 0 else None
    encryption = None if _blank(props.get('Encryption Main')) else str(props['Encryption Main']).strip()
    passphrase = None if _blank(props.get('Passphrase Main')) else str(props['Passphrase Main'])
    backup_passphrase = None if _blank(props.get('Passphrase Backup')) else str(props['Passphrase Backup'])
    backup_encryption = None if _blank(props.get('Encryption Backup')) else str(props['Encryption Backup']).strip()

    seq = [0]

    def add(edge_key, edge_id, kind, name, body):
        seq[0] += 1
        steps.append({'seq': seq[0], 'edge': edge_key, 'edge_id': edge_id, 'kind': kind, 'name': name, 'body': body})
        return seq[0]

    # ---- DC edge ----------------------------------------------------------
    src_refs = []
    name = bte_name(base, dc_key, 'source')
    if main_in['protocol'] == 'srt':
        src_refs.append(add(dc_key, dc['id'], 'source', name, _srt_source_body(name, main_in, latency, passphrase, encryption)))
    else:
        src_refs.append(add(dc_key, dc['id'], 'source', name, _udp_source_body(name, main_in)))

    if backup_in and backup_in != main_in:
        name = bte_name(base, dc_key, 'source-backup')
        if backup_in['protocol'] == 'srt':
            body = _srt_source_body(name, backup_in, latency, backup_passphrase or passphrase, backup_encryption or encryption)
        else:
            body = _udp_source_body(name, backup_in)
        if backup_in.get('state'):
            body['state'] = backup_in['state'].lower()
        src_refs.append(add(dc_key, dc['id'], 'source-backup', name, body))
    elif backup_in is None and not _blank(props.get('Input Backup')):
        warnings.append(f'"Input Backup" could not be parsed and was ignored: {props.get("Input Backup")!r}')

    name = bte_name(base, dc_key, 'stream')
    dc_stream = add(dc_key, dc['id'], 'stream', name, {
        'name': name,
        'sources': [{'$ref': r} for r in src_refs],
        'enabled': True,
    })
    name = bte_name(base, dc_key, 'output')
    add(dc_key, dc['id'], 'output', name, {
        'name': name,
        'type': 'srt',
        'mode': 'listener',
        'port': out_port,
        'latency': latency or 500,
        'encryption': 'AES-256',
        'passphrase': INTERNAL_PASSPHRASE,
        'stream': {'$ref': dc_stream},
    })

    # ---- regional edges ---------------------------------------------------
    sites = 0
    for site, prop in REGIONAL_SITES:
        mcast = parse_multicast(props.get(prop))
        edge = site_edge(site, edges)
        if not mcast:
            warnings.append(f'{site}: no "{prop}" on the resource — site skipped')
            continue
        if not edge:
            warnings.append(f'{site}: no regional edge configured for this site (BTE_EDGE_{site}xx) — site skipped')
            continue
        edge_key = edge['key']
        sites += 1
        name = bte_name(base, edge_key, 'source')
        src = add(edge_key, edge['id'], 'source', name, {
            'name': name,
            'type': 'srt',
            'mode': 'caller',
            'address': dc['out']['SRT'],
            'port': out_port,
            'latency': latency or 500,
            'encryption': 'AES-256',
            'passphrase': INTERNAL_PASSPHRASE,
        })
        name = bte_name(base, edge_key, 'stream')
        stream = add(edge_key, edge['id'], 'stream', name, {
            'name': name, 'sources': [{'$ref': src}], 'enabled': True,
        })
        name = bte_name(base, edge_key, 'output')
        add(edge_key, edge['id'], 'output', name, {
            'name': name,
            'type': 'udp',
            'address': mcast['host'],
            'port': mcast['port'],
            'stream': {'$ref': stream},
        })

    if sites == 0:
        warnings.append('No regional site will be provisioned (no multicast address / edge available)')

    return {
        'ok': True,
        'steps': steps,
        'warnings': warnings,
        'errors': [],
        'summary': {
            'resource': base,
            'dc_edge': dc_key,
            'output_port': out_port,
            'sites': sites,
            'objects': len(steps),
        },
    }


def redact_body(body):
    out = copy.deepcopy(body)
    for key in list(out):
        if re.search(r'passphrase|password|secret|token', key, re.I) and out[key] not in (None, ''):
            out[key] = REDACTED
    return out


def redact_plan(plan):
    out = copy.deepcopy(plan)
    for step in out.get('steps', []):
        step['body'] = redact_body(step['body'])
    return out


# ---------------------------------------------------------------------------
# TXCore client
# ---------------------------------------------------------------------------

class TXCoreError(Exception):
    pass


class TXCoreClient:
    """Thin wrapper over the TXCore MAIN REST API for MWEdge objects."""

    def __init__(self, url=None, token=None, paths=None, session=None):
        self.url = (url or TXCORE_URL or '').rstrip('/')
        self.paths = paths or PATHS
        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            self.session.headers.update({
                'Authorization': f'Bearer {token or TXCORE_TOKEN}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })

    def _collection(self, kind, edge_id):
        kind = 'source' if kind.startswith('source') else kind
        return self.url + self.paths[kind].replace('{edge}', str(edge_id))

    def _object(self, kind, edge_id, obj_id):
        return self._collection(kind, edge_id) + str(obj_id)

    @staticmethod
    def _json(resp):
        try:
            return resp.json() if resp.content else None
        except ValueError:
            return {'raw': resp.text[:1000]}

    def create(self, kind, edge_id, body):
        """POST and return (object id, response payload)."""
        try:
            resp = self.session.post(self._collection(kind, edge_id), json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'{kind} POST failed: {exc}') from exc
        payload = self._json(resp)
        if not resp.ok:
            raise TXCoreError(f'{kind} POST returned HTTP {resp.status_code}: {json.dumps(payload)[:400]}')
        obj_id = None
        if isinstance(payload, dict):
            obj_id = payload.get('_id') or payload.get('id')
        if not obj_id:
            raise TXCoreError(f'{kind} POST succeeded but no id was returned: {json.dumps(payload)[:400]}')
        return str(obj_id), payload

    def get(self, kind, edge_id, obj_id):
        """Return the live object, or None when TXCore reports 404."""
        try:
            resp = self.session.get(self._object(kind, edge_id, obj_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'{kind} GET failed: {exc}') from exc
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise TXCoreError(f'{kind} GET returned HTTP {resp.status_code}')
        return self._json(resp) or {}

    def delete(self, kind, edge_id, obj_id):
        try:
            resp = self.session.delete(self._object(kind, edge_id, obj_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'{kind} DELETE failed: {exc}') from exc
        if resp.status_code == 404:
            return 'gone'
        if not resp.ok:
            raise TXCoreError(f'{kind} DELETE returned HTTP {resp.status_code}: {resp.text[:300]}')
        return 'deleted'


# ---------------------------------------------------------------------------
# Lease registry (file backed, cross-process)
# ---------------------------------------------------------------------------

class _Locked:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, 'a+')
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _read_registry():
    try:
        with open(LEASES_FILE, 'r') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault('version', 1)
    data.setdefault('leases', {})
    return data


def _write_registry(data):
    tmp = LEASES_FILE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, LEASES_FILE)
    try:
        os.chmod(LEASES_FILE, 0o600)
    except OSError:
        pass


def _mutate(fn):
    """Read-modify-write the registry under the cross-process lock."""
    with _Locked(LEASES_LOCK_FILE):
        data = _read_registry()
        result = fn(data)
        _trim_history(data)
        _write_registry(data)
        return result


def _trim_history(data):
    finished = [l for l in data['leases'].values() if l['status'] in FINAL_STATUSES]
    if len(finished) <= HISTORY_KEEP:
        return
    finished.sort(key=lambda l: l.get('finished_at') or l.get('created_at') or '')
    for lease in finished[:len(finished) - HISTORY_KEEP]:
        data['leases'].pop(lease['lease_id'], None)


ACTIVE_STATUSES = ('creating', 'active', 'delete_failed', 'deleting')
FINAL_STATUSES = ('deleted', 'failed', 'dry_run_expired')


def get_lease(lease_id):
    with _Locked(LEASES_LOCK_FILE):
        return _read_registry()['leases'].get(lease_id)


def list_leases():
    with _Locked(LEASES_LOCK_FILE):
        leases = list(_read_registry()['leases'].values())
    now = _now()
    out = []
    for lease in leases:
        lease = copy.deepcopy(lease)
        exp = _parse_iso(lease.get('expires_at'))
        lease['remaining_seconds'] = int((exp - now).total_seconds()) if exp and lease['status'] in ACTIVE_STATUSES else None
        for obj in lease.get('objects', []):
            obj['body'] = redact_body(obj.get('body') or {})
        out.append(lease)
    out.sort(key=lambda l: (l['status'] in FINAL_STATUSES, l.get('expires_at') or ''))
    return out


def _clamp_minutes(minutes, default):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = default
    return max(1, min(MAX_DURATION_MIN, minutes))


# ---------------------------------------------------------------------------
# Lease lifecycle
# ---------------------------------------------------------------------------

def create_lease(item, plan, duration_minutes, username, dry_run):
    """Persist a new lease (status 'creating') and return it. Run with run_lease()."""
    duration = _clamp_minutes(duration_minutes, DEFAULT_DURATION_MIN)
    now = _now()
    lease = {
        'lease_id': uuid.uuid4().hex,
        'resource_id': item.get('id'),
        'resource_name': item.get('name'),
        'supplier': ((item.get('capabilities') or {}).get('Type') or '').strip() or None,
        'dc_edge': plan['summary'].get('dc_edge'),
        'sites': plan['summary'].get('sites'),
        'created_at': _iso(now),
        'created_by': username,
        'duration_minutes': duration,
        'expires_at': _iso(now + timedelta(minutes=duration)),
        'extensions': [],
        'dry_run': bool(dry_run),
        'status': 'creating',
        'warnings': list(plan.get('warnings') or []),
        'errors': [],
        'objects': [
            {'seq': s['seq'], 'edge': s['edge'], 'edge_id': s['edge_id'], 'kind': s['kind'],
             'name': s['name'], 'body': s['body'], 'id': None, 'status': 'pending', 'error': None}
            for s in plan['steps']
        ],
        'finished_at': None,
    }

    def _add(data):
        data['leases'][lease['lease_id']] = lease
        return lease

    return _mutate(_add)


def _update_lease(lease_id, fn):
    def _apply(data):
        lease = data['leases'].get(lease_id)
        if lease is None:
            return None
        fn(lease)
        return copy.deepcopy(lease)
    return _mutate(_apply)


def _resolve_refs(body, ids):
    if isinstance(body, dict):
        if set(body) == {'$ref'}:
            return ids[body['$ref']]
        return {k: _resolve_refs(v, ids) for k, v in body.items()}
    if isinstance(body, list):
        return [_resolve_refs(v, ids) for v in body]
    return body


def run_lease(lease_id, client=None):
    """Execute the lease plan against TXCore (or mark objects skipped on dry run).

    Creation is sequential; on the first failure everything created so far
    is rolled back and the lease ends as 'failed'.
    """
    lease = get_lease(lease_id)
    if lease is None:
        return None
    if lease['dry_run']:
        def _skip(l):
            for obj in l['objects']:
                obj['status'] = 'skipped'
            l['status'] = 'active'
        return _update_lease(lease_id, _skip)

    client = client or TXCoreClient()
    ids = {}
    for obj in lease['objects']:
        if not has_tag(obj['name']):
            # Defensive: never create an untagged object; it could not be cleaned up.
            err = f'refused: object name lacks {BTE_TAG}'
            _update_lease(lease_id, lambda l, s=obj['seq'], e=err: _mark(l, s, 'error', error=e))
            return _rollback(lease_id, ids, client, f'step {obj["seq"]}: {err}')
        try:
            body = _resolve_refs(obj['body'], ids)
            obj_id, _ = client.create(obj['kind'], obj['edge_id'], body)
        except (TXCoreError, KeyError) as exc:
            _update_lease(lease_id, lambda l, s=obj['seq'], e=str(exc): _mark(l, s, 'error', error=e))
            return _rollback(lease_id, ids, client, f'step {obj["seq"]} ({obj["edge"]} {obj["kind"]}): {exc}')
        ids[obj['seq']] = obj_id
        _update_lease(lease_id, lambda l, s=obj['seq'], i=obj_id: _mark(l, s, 'created', obj_id=i))

    def _done(l):
        l['status'] = 'active'
    log.info('BTE lease %s active: %s (%d objects)', lease_id, lease['resource_name'], len(ids))
    return _update_lease(lease_id, _done)


def _mark(lease, seq, status, obj_id=None, error=None):
    for obj in lease['objects']:
        if obj['seq'] == seq:
            obj['status'] = status
            if obj_id is not None:
                obj['id'] = obj_id
            if error is not None:
                obj['error'] = error


def _rollback(lease_id, ids, client, reason):
    log.error('BTE lease %s failed, rolling back %d objects: %s', lease_id, len(ids), reason)
    _update_lease(lease_id, lambda l: l['errors'].append(reason))
    _delete_objects(lease_id, client, only_ids=set(ids.values()))

    def _fail(l):
        l['status'] = 'failed'
        l['finished_at'] = _iso(_now())
    return _update_lease(lease_id, _fail)


def _delete_objects(lease_id, client, only_ids=None):
    """Delete a lease's objects in reverse order. Returns (deleted, refused, failed)."""
    lease = get_lease(lease_id)
    deleted = refused = failed = 0
    for obj in reversed(lease['objects']):
        if not obj.get('id') or obj['status'] in ('deleted', 'gone'):
            continue
        if only_ids is not None and obj['id'] not in only_ids:
            continue
        # Guard 1: the registry itself must say this is a BTE object.
        if not has_tag(obj['name']):
            refused += 1
            _update_lease(lease_id, lambda l, s=obj['seq']: _mark(l, s, 'refused', error=f'registry name lacks {BTE_TAG}'))
            continue
        try:
            live = client.get(obj['kind'], obj['edge_id'], obj['id'])
            if live is None:
                _update_lease(lease_id, lambda l, s=obj['seq']: _mark(l, s, 'gone'))
                deleted += 1
                continue
            # Guard 2: the object as it exists in TXCore right now must still carry the tag.
            live_name = live.get('name') if isinstance(live, dict) else None
            if not has_tag(live_name):
                refused += 1
                _update_lease(lease_id, lambda l, s=obj['seq'], n=live_name: _mark(
                    l, s, 'refused', error=f'live name {n!r} lacks {BTE_TAG} — not deleted'))
                log.warning('BTE lease %s: refused to delete %s %s (name %r)', lease_id, obj['kind'], obj['id'], live_name)
                continue
            result = client.delete(obj['kind'], obj['edge_id'], obj['id'])
            _update_lease(lease_id, lambda l, s=obj['seq'], r=result: _mark(l, s, r))
            deleted += 1
        except TXCoreError as exc:
            failed += 1
            _update_lease(lease_id, lambda l, s=obj['seq'], e=str(exc): _mark(l, s, 'delete_error', error=e))
    return deleted, refused, failed


def delete_lease(lease_id, reason, username, client=None):
    """Remove everything a lease created. Returns the updated lease or None."""
    lease = get_lease(lease_id)
    if lease is None:
        return None
    if lease['status'] in FINAL_STATUSES:
        return lease

    def _start(l):
        l['status'] = 'deleting'
        l['delete_reason'] = reason
        l['deleted_by'] = username
    _update_lease(lease_id, _start)

    if lease['dry_run'] or not any(o.get('id') for o in lease['objects']):
        def _finish_dry(l):
            l['status'] = 'deleted'
            l['finished_at'] = _iso(_now())
        return _update_lease(lease_id, _finish_dry)

    client = client or TXCoreClient()
    deleted, refused, failed = _delete_objects(lease_id, client)

    def _finish(l):
        if failed or refused:
            l['status'] = 'delete_failed'
            l['errors'].append(f'delete: {deleted} removed, {refused} refused (tag check), {failed} failed')
        else:
            l['status'] = 'deleted'
            l['finished_at'] = _iso(_now())
    result = _update_lease(lease_id, _finish)
    log.info('BTE lease %s delete (%s): %d removed, %d refused, %d failed', lease_id, reason, deleted, refused, failed)
    return result


def extend_lease(lease_id, minutes, username):
    """Push expires_at forward by ``minutes`` (capped by MAX_DURATION_MIN from creation).

    Returns (lease, error, note). ``error`` set -> nothing changed.
    """
    minutes = _clamp_minutes(minutes, DEFAULT_EXTEND_MIN)
    outcome = {'error': None, 'note': None}

    def _extend(l):
        if l['status'] not in ACTIVE_STATUSES:
            outcome['error'] = f'lease is {l["status"]} and cannot be extended'
            return
        now = _now()
        current = _parse_iso(l['expires_at']) or now
        base = max(now, current)
        created = _parse_iso(l['created_at']) or now
        cap = created + timedelta(minutes=MAX_DURATION_MIN)
        new_exp = min(base + timedelta(minutes=minutes), cap)
        if new_exp <= current:
            outcome['error'] = f'lease already at the maximum length of {MAX_DURATION_MIN} min'
            return
        granted = int((new_exp - current).total_seconds() // 60)
        l['expires_at'] = _iso(new_exp)
        l['extensions'].append({'at': _iso(now), 'minutes': granted, 'requested': minutes, 'by': username})
        if new_exp == cap:
            outcome['note'] = f'capped at the maximum lease length of {MAX_DURATION_MIN} min'

    lease = _update_lease(lease_id, _extend)
    if lease is None:
        return None, 'Lease not found', None
    return lease, outcome['error'], outcome['note']


def delete_all_leases(reason, username, client=None):
    """Delete every active lease. Only registry-known, tagged objects are touched."""
    with _Locked(LEASES_LOCK_FILE):
        ids = [l['lease_id'] for l in _read_registry()['leases'].values() if l['status'] in ('active', 'delete_failed')]
    client = client or (TXCoreClient() if configured() else None)
    results = []
    for lease_id in ids:
        lease = delete_lease(lease_id, reason, username, client=client)
        results.append({'lease_id': lease_id, 'resource_name': lease['resource_name'], 'status': lease['status']})
    return results


# ---------------------------------------------------------------------------
# Reaper — deletes expired leases automatically
# ---------------------------------------------------------------------------

def reap_expired(client=None):
    """Delete leases whose expires_at has passed. Safe across workers."""
    lock_fd = open(REAPER_LOCK_FILE, 'a+')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return []
    try:
        now = _now()
        with _Locked(LEASES_LOCK_FILE):
            due = [l['lease_id'] for l in _read_registry()['leases'].values()
                   if l['status'] in ('active', 'delete_failed')
                   and (_parse_iso(l['expires_at']) or now) <= now]
        if not due:
            return []
        client = client or (TXCoreClient() if configured() else None)
        results = []
        for lease_id in due:
            lease = delete_lease(lease_id, 'expired', 'bte-reaper', client=client)
            results.append({'lease_id': lease_id, 'status': lease['status']})
            log.info('BTE reaper: lease %s expired → %s', lease_id, lease['status'])
        return results
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _reaper_loop():
    log.info('BTE lease reaper started (every %ss)', REAPER_INTERVAL)
    while True:
        try:
            reap_expired()
        except Exception:  # noqa: BLE001 — the reaper must never die
            log.exception('BTE reaper iteration failed')
        time.sleep(REAPER_INTERVAL)


def start_reaper():
    if REAPER_DISABLED:
        log.info('BTE lease reaper disabled by BTE_REAPER_DISABLED')
        return
    threading.Thread(target=_reaper_loop, name='bte-lease-reaper', daemon=True).start()
