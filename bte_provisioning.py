"""
TXCore Provisioning - BTE provisioning layer (leases, planner, TXCore client)

Turns a Dataminer "Supplier Dynamic" resource (as kept in the BTE snapshot)
into a set of TXCore MWEdge objects and keeps track of everything it creates
so it can be removed again — on demand or automatically when the lease
expires.

Topology created per channel ("Create resources" in the BTE tab), mirroring
the property model of the Dataminer StreamResourceCreation script. One
TXCore call per edge: POST /api/mwedge/<edge id> with {streams, sources, outputs}
(the stream id is chosen by BTE, sources/outputs reference it):

    DC edge (properties["DC MWEdge"], e.g. INX01)
        stream
        source  SRT   from properties["Input Main"] (or "Input") — primary only, no backup
        output  SRT listener on properties["Output"]  (internal passphrase)

    AVE / LMK / YER edges (always, one per site with a multicast address)
        stream
        source  SRT caller  <DC edge pub=SRT ip>:<Output port> (internal passphrase)
        output  UDP multicast from properties["<Site> Multicast"]

Naming (mirrors the existing TXCore conventions):
    stream  <channel>_<edge>_[BTE]          e.g. VET_CH01_INX01_[BTE]
    source  SRC_<channel>_A_<proto>_<edge>  e.g. SRC_VET_CH01_A_SRT_INX01
    output  OUT_<channel>_<proto>_<edge>    e.g. OUT_VET_CH01_SRT_INX01
The stream carries BTE_TAG in its name and its id starts with "BTE_"; sources
and outputs belong to that stream. Nothing is ever deleted unless (a) it is
recorded in the lease registry AND (b) the live object fetched from TXCore is
still a BTE object: a stream whose name carries the tag, or a source/output
whose ``stream`` still points at a "BTE_…" stream id.

Lease registry: /opt/web/data/bte_leases.json (mode 0600, atomic replace,
fcntl lock shared by all gunicorn workers).

Environment variables (.env):
    APIURLMAIN / BEARER_TOKEN_MAIN      TXCore MAIN API (shared with routes_txcore)
    INTERNALSRTPASSPHRASE               Passphrase for edge-to-edge / edge-to-core SRT hops
    BTE_PROVISIONING_ENABLED            "true" to allow live TXCore writes (default false:
                                        every request is forced to dry-run)
    BTE_EDGE_<KEY>                      One entry per TXEdge, ``;``-separated key=value fields:
        id=<txcore edge id>;location=<label>;dc=yes|no;
        in=SRT@<ip>;out=SRT@<ip>,UDP@<ip>;pub=SRT@<public ip>
                                        in/out  local interface IPs -> ``networkInterface`` of the
                                                sources (in) and outputs (out) created on that edge
                                        pub     public IPs of a DC edge; regional edges pull SRT
                                                from pub=SRT@<ip> (never from the internal ``out``)
                                        The key must match properties["DC MWEdge"] for DC edges
                                        (INX01…) and start with the site prefix AVE / LMK / YER
                                        for regional edges (AVE02, LMK01, YER01…).
    BTE_TXCORE_EDGE_PATH                Batch create endpoint, default /mwedge/{edge}
    BTE_TXCORE_OBJECT_PATH              Single object (GET/DELETE), default /mwedge/{edge}/{kind}/{id}
                                        {kind} is stream | source | output
    BTE_DEFAULT_DURATION_MINUTES        Lease length offered by default (60)
    BTE_DEFAULT_EXTEND_MINUTES          Extension offered by default (30)
    BTE_MAX_DURATION_MINUTES            Hard cap for a lease, including extensions (1440)
    BTE_REAPER_DISABLED                 "true" to disable automatic deletion of expired leases

!! Confirmed against the TXCore API reference: POST /mwedge/<id> with
!! {streams, sources, outputs} and its response shape. Still assumed and to be
!! confirmed on stage: the SRT-specific option names, the single-object
!! GET/DELETE path (BTE_TXCORE_OBJECT_PATH) used for the tag check and removal.
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
OUTPUT_PORT_OFFSET = 1000   # Output port = Input port + 1000 when the resource has no "Output"
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

EDGE_PATH = _env('BTE_TXCORE_EDGE_PATH') or '/mwedge/{edge}'
OBJECT_PATH = _env('BTE_TXCORE_OBJECT_PATH') or '/mwedge/{edge}/{kind}/{id}'
KINDS = ('stream', 'source', 'output')          # creation order inside a batch
STREAM_ID_PREFIX = 'BTE_'

# TXCore SRT option field names, confirmed against the API reference example
# for /mwedge/<id>/source/: {type, hostAddress, port, latency, pbkeylen, passphrase}.
SRT_OPTION_KEYS = {
    'host': 'hostAddress',
    'latency': 'latency',      # ms
    'passphrase': 'passphrase',
    'keylen': 'pbkeylen',      # 16 | 24 | 32  (AES-128 / 192 / 256)
}
ENCRYPTION_KEYLEN = {'AES-128': 16, 'AES-192': 24, 'AES-256': 32}
# "type": 1 = listener, confirmed by the API reference. The caller value is not
# shown in the reference example; 0 is assumed by elimination — confirm with
# "Inspect live TXEdge" against a real caller source/output on stage if a call
# is rejected.
SRT_TYPE = {'listener': 1, 'caller': 0}
DELETE_ORDER = ('output', 'source', 'stream')   # outputs first, the stream last

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
        'pub': _parse_host_list(fields.get('pub')),
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
        if e.get('dc') and not e['pub'].get('SRT'):
            missing.append(f"{e['key']} pub=SRT@<public ip>")
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
                      'in': v['in'], 'out': v['out'], 'pub': v['pub']} for k, v in sorted(EDGES.items())},
        'srt_option_keys': SRT_OPTION_KEYS,
        'missing_edges': missing_edges(),
        'edge_path': EDGE_PATH,
        'object_path': OBJECT_PATH,
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


def _prop(props, *names):
    """First non-blank property among synonyms (e.g. "Input Main" / "Input")."""
    for name in names:
        if not _blank(props.get(name)):
            return props[name]
    return None


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def _slug(value):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(value)).strip('_')


def stream_name(base, edge_key):
    return f'{_slug(base)}_{edge_key}_{BTE_TAG}'          # VET_CH01_INX01_[BTE]


def source_name(base, edge_key, protocol):
    return f'SRC_{_slug(base)}_A_{protocol}_{edge_key}'  # SRC_VET_CH01_A_SRT_INX01


def output_name(base, edge_key, protocol):
    return f'OUT_{_slug(base)}_{protocol}_{edge_key}'    # OUT_VET_CH01_SRT_INX01


def has_tag(name):
    return BTE_TAG in str(name or '')


def is_bte_stream_id(value):
    return str(value or '').startswith(STREAM_ID_PREFIX)


def registry_is_bte(obj):
    """Guard 1 — what the registry says about an object we created."""
    if obj['kind'] == 'stream':
        return has_tag(obj['name']) and is_bte_stream_id((obj.get('body') or {}).get('id'))
    return is_bte_stream_id((obj.get('body') or {}).get('stream'))


def live_is_bte(obj, live):
    """Guard 2 — what TXCore says about the object right now."""
    if not isinstance(live, dict):
        return False
    if obj['kind'] == 'stream':
        return has_tag(live.get('name')) and is_bte_stream_id(live.get('id') or obj.get('id'))
    return is_bte_stream_id(live.get('stream'))


def _stream_id(base, edge_key):
    """Client-chosen TXCore stream id: unique per lease, still readable in TXCore."""
    return f'{STREAM_ID_PREFIX}{_slug(base)[:40]}_{edge_key}_{uuid.uuid4().hex[:8]}'


def _srt_options(mode, port, address, latency, passphrase, encryption, interface):
    """SRT option block: {type, hostAddress, port, latency, networkInterface, pbkeylen, passphrase}."""
    k = SRT_OPTION_KEYS
    opts = {
        'type': SRT_TYPE[mode],
        k['host']: address if mode == 'caller' else None,
        'port': port,
        k['latency']: latency or 500,
        'networkInterface': interface,
    }
    if passphrase:
        opts[k['passphrase']] = passphrase
        opts[k['keylen']] = ENCRYPTION_KEYLEN.get(str(encryption or 'AES-256').upper(), 32)
    return opts


def _udp_options(host, port, interface):
    return {'port': port, 'address': host, 'networkInterface': interface}


def _stream_obj(stream_id, name, failover):
    return {'id': stream_id, 'name': name, 'options': {'failoverMode': failover}}


def _endpoint_obj(stream_id, name, protocol, options):
    return {'stream': stream_id, 'name': name, 'tags': 'bte', 'protocol': protocol, 'active': True, 'options': options}


def build_plan(item, edges=None, passphrase_override=None):
    """Return {'ok', 'steps', 'warnings', 'errors', 'summary'} for a snapshot item.

    One step per edge = one POST /mwedge/<edge id>. Each step lists the objects
    it creates (kind, name, body); the request body is assembled at run time.
    ``passphrase_override`` replaces the supplier passphrase of the resource
    (used while the Dataminer API does not expose the real value).
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
    elif dc and not dc['pub'].get('SRT'):
        errors.append(f'BTE_EDGE_{dc_key} has no pub=SRT@<public ip> — regional edges cannot pull from it')

    # "Input Main" and "Input" are synonyms on Dataminer resources. Only the
    # primary input is provisioned: BTE creates streams on the primary DC (INX)
    # edge only, so backup inputs are ignored by design.
    raw_input = _prop(props, 'Input Main', 'Input')
    main_in = parse_input(raw_input)
    if not main_in:
        errors.append(f'"Input Main" / "Input" is missing or unparsable: {raw_input!r}')
    out_port = _int_or_none(props.get('Output'))
    if _blank(props.get('Output')) and main_in:
        # "+1000 rule": no Output on the resource -> Input port + 1000 (as the Dataminer script does).
        out_port = main_in['port'] + OUTPUT_PORT_OFFSET
        warnings.append(f'"Output" missing — inferred as Input port + {OUTPUT_PORT_OFFSET} = {out_port}')
    if not out_port or not 1 <= out_port <= 65535:
        errors.append(f'"Output" port is missing or invalid: {props.get("Output")!r}')
    if not INTERNAL_PASSPHRASE:
        errors.append('INTERNALSRTPASSPHRASE is not set on the server (needed for the edge-to-edge SRT hops)')

    if errors:
        return {'ok': False, 'steps': [], 'warnings': warnings, 'errors': errors, 'summary': {}}

    latency = _int_or_none(props.get('Latency'))
    latency = latency if latency and latency > 0 else None
    encryption = _prop(props, 'Encryption Main', 'Encryption')
    encryption = str(encryption).strip() if encryption else None
    encrypted = bool(encryption) and encryption.upper() != 'NONE'

    # Supplier passphrase: override > resource value. A redacted value ("********")
    # means the Dataminer API did not hand out the real secret.
    raw_pass = _prop(props, 'Passphrase Main', 'Passphrase')
    raw_pass = str(raw_pass) if raw_pass else None
    if passphrase_override:
        passphrase, passphrase_status = str(passphrase_override), 'override'
    elif raw_pass and raw_pass.strip('*') == '':
        passphrase, passphrase_status = None, 'redacted'
    else:
        passphrase, passphrase_status = raw_pass, ('resource' if raw_pass else 'missing')
    if encrypted and not passphrase:
        warnings.append(f'Encryption {encryption} requested but the resource passphrase is {passphrase_status} — '
                        f'the {dc_key} source will be created WITHOUT encryption (use the passphrase override)')
    elif not encrypted and passphrase:
        warnings.append('A passphrase is present but Encryption is None — the source is created without encryption')
        passphrase = None

    def step(edge, objects):
        steps.append({'seq': len(steps) + 1, 'edge': edge['key'], 'edge_id': edge['id'],
                      'location': edge.get('location'), 'objects': objects})

    # ---- DC edge ----------------------------------------------------------
    sid = _stream_id(base, dc_key)
    proto = main_in['protocol'].upper()
    n_stream = stream_name(base, dc_key)
    n_src = source_name(base, dc_key, proto)
    n_out = output_name(base, dc_key, 'SRT')
    if proto == 'SRT':
        mode = 'listener' if main_in['mode'] == 'listener' else 'caller'
        src_opts = _srt_options(mode, main_in['port'], main_in['host'], latency,
                                passphrase if encrypted else None, encryption, dc['in'].get('SRT'))
    else:
        src_opts = _udp_options(main_in['host'], main_in['port'], dc['in'].get(proto) or dc['in'].get('UDP'))
    step(dc, [
        {'kind': 'stream', 'name': n_stream, 'body': _stream_obj(sid, n_stream, 'none')},
        {'kind': 'source', 'name': n_src, 'body': _endpoint_obj(sid, n_src, proto, src_opts)},
        {'kind': 'output', 'name': n_out, 'body': _endpoint_obj(sid, n_out, 'SRT', _srt_options(
            'listener', out_port, None, latency, INTERNAL_PASSPHRASE, 'AES-256', dc['out'].get('SRT')))},
    ])

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
        sites += 1
        key = edge['key']
        sid = _stream_id(base, key)
        n_stream, n_src, n_out = stream_name(base, key), source_name(base, key, 'SRT'), output_name(base, key, 'UDP')
        step(edge, [
            {'kind': 'stream', 'name': n_stream, 'body': _stream_obj(sid, n_stream, 'none')},
            {'kind': 'source', 'name': n_src, 'body': _endpoint_obj(sid, n_src, 'SRT', _srt_options(
                'caller', out_port, dc['pub']['SRT'], latency, INTERNAL_PASSPHRASE, 'AES-256', edge['in'].get('SRT')))},
            {'kind': 'output', 'name': n_out, 'body': _endpoint_obj(sid, n_out, 'UDP', _udp_options(
                mcast['host'], mcast['port'], edge['out'].get('UDP')))},
        ])

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
            'input': raw_input,
            'output_port': out_port,
            'encryption': encryption if encrypted else None,
            'passphrase_status': passphrase_status,
            'sites': sites,
            'edges': len(steps),
            'objects': sum(len(s['objects']) for s in steps),
        },
    }


def batch_body(objects):
    """{streams, sources, outputs} for one POST /mwedge/<edge id>."""
    body = {'streams': [], 'sources': [], 'outputs': []}
    for obj in objects:
        body[obj['kind'] + 's'].append(obj['body'])
    return body


def redact_body(body):
    """Mask secret-looking keys anywhere in a (nested) object."""
    if isinstance(body, dict):
        return {k: (REDACTED if re.search(r'passphrase|password|secret|token|key$', k, re.I) and v not in (None, '')
                    else redact_body(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v) for v in body]
    return body


def redact_plan(plan):
    out = copy.deepcopy(plan)
    for step in out.get('steps', []):
        for obj in step.get('objects', []):
            obj['body'] = redact_body(obj['body'])
    return out


# ---------------------------------------------------------------------------
# TXCore client
# ---------------------------------------------------------------------------

class TXCoreError(Exception):
    pass


class TXCoreClient:
    """Thin wrapper over the TXCore MAIN REST API for MWEdge objects."""

    def __init__(self, url=None, token=None, session=None):
        self.url = (url or TXCORE_URL or '').rstrip('/')
        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            self.session.headers.update({
                'Authorization': f'Bearer {token or TXCORE_TOKEN}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })

    def edge_url(self, edge_id):
        return self.url + EDGE_PATH.replace('{edge}', str(edge_id))

    def object_url(self, kind, edge_id, obj_id):
        return self.url + OBJECT_PATH.replace('{edge}', str(edge_id)).replace('{kind}', kind).replace('{id}', str(obj_id))

    @staticmethod
    def _json(resp):
        try:
            return resp.json() if resp.content else None
        except ValueError:
            return {'raw': resp.text[:1000]}

    def create_batch(self, edge_id, body):
        """POST {streams, sources, outputs}. Returns [(kind, requested_name, id, name)] for every
        entry; raises TXCoreError (carrying the ids already created) on any failure."""
        try:
            resp = self.session.post(self.edge_url(edge_id), json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'POST {self.edge_url(edge_id)} failed: {exc}') from exc
        payload = self._json(resp)
        if not resp.ok:
            raise TXCoreError(f'POST returned HTTP {resp.status_code}: {json.dumps(payload)[:400]}')
        if not isinstance(payload, dict):
            raise TXCoreError(f'Unexpected response: {json.dumps(payload)[:400]}')

        created, failures = [], []
        for kind in KINDS:
            requested = body.get(kind + 's') or []
            results = payload.get(kind + 's') or []
            if len(results) != len(requested):
                failures.append(f'{kind}s: sent {len(requested)}, TXCore answered for {len(results)}')
            for req, res in zip(requested, results):
                data = (res or {}).get('data') or {}
                if not (res or {}).get('success'):
                    failures.append(f"{kind} {req.get('name')!r}: {json.dumps(res)[:300]}")
                    continue
                obj_id = data.get('id') or req.get('id')
                if not obj_id:
                    failures.append(f"{kind} {req.get('name')!r}: no id in response")
                    continue
                created.append((kind, req.get('name'), str(obj_id), data.get('name')))
        if failures:
            err = TXCoreError('; '.join(failures))
            err.created = created
            raise err
        return created

    def get_edge(self, edge_id):
        """GET the whole MWEdge document (streams, sources, outputs as TXCore stores them)."""
        try:
            resp = self.session.get(self.edge_url(edge_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'edge GET failed: {exc}') from exc
        if not resp.ok:
            raise TXCoreError(f'edge GET returned HTTP {resp.status_code}: {resp.text[:300]}')
        return self._json(resp)

    def get_object(self, kind, edge_id, obj_id):
        """Return the live object, or None when TXCore reports 404."""
        try:
            resp = self.session.get(self.object_url(kind, edge_id, obj_id), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'{kind} GET failed: {exc}') from exc
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise TXCoreError(f'{kind} GET returned HTTP {resp.status_code}')
        payload = self._json(resp) or {}
        # Tolerate {"success": true, "data": {...}} envelopes.
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            payload = payload['data']
        return payload

    def delete_object(self, kind, edge_id, obj_id):
        try:
            resp = self.session.delete(self.object_url(kind, edge_id, obj_id), timeout=REQUEST_TIMEOUT)
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
        'steps': [
            {'seq': s['seq'], 'edge': s['edge'], 'edge_id': s['edge_id'], 'location': s.get('location'),
             'status': 'pending', 'error': None}
            for s in plan['steps']
        ],
        # Objects created in TXCore (filled in while the plan runs). Every entry
        # must carry the [BTE] tag in its name to be deletable.
        'objects': [
            {'seq': s['seq'], 'edge': s['edge'], 'edge_id': s['edge_id'], 'kind': o['kind'],
             'name': o['name'], 'body': o['body'], 'id': None, 'status': 'pending', 'error': None}
            for s in plan['steps'] for o in s['objects']
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


def run_lease(lease_id, client=None):
    """Execute the lease plan: one POST per edge (or mark everything skipped on dry run).

    On the first failing edge everything created so far — including objects
    TXCore reported as created in the failing batch — is rolled back and the
    lease ends as 'failed'.
    """
    lease = get_lease(lease_id)
    if lease is None:
        return None
    if lease['dry_run']:
        def _skip(l):
            for obj in l['objects']:
                obj['status'] = 'skipped'
            for st in l['steps']:
                st['status'] = 'skipped'
            l['status'] = 'active'
        return _update_lease(lease_id, _skip)

    client = client or TXCoreClient()
    for step in lease['steps']:
        objs = [o for o in lease['objects'] if o['seq'] == step['seq']]
        untagged = [o['name'] for o in objs if not registry_is_bte(o)]
        if untagged:
            # Defensive: never create an object we could not recognise as ours later.
            reason = f"step {step['seq']} ({step['edge']}): refused, not identifiable as BTE objects: {untagged}"
            _update_lease(lease_id, lambda l, s=step['seq'], e=reason: _mark_step(l, s, 'error', e))
            return _rollback(lease_id, client, reason)
        try:
            created = client.create_batch(step['edge_id'], batch_body(
                [{'kind': o['kind'], 'body': o['body']} for o in objs]))
        except TXCoreError as exc:
            partial = getattr(exc, 'created', [])
            _update_lease(lease_id, lambda l, s=step['seq'], c=partial, e=str(exc): (
                _record_created(l, s, c), _mark_step(l, s, 'error', e)))
            return _rollback(lease_id, client, f"step {step['seq']} ({step['edge']}): {exc}")
        _update_lease(lease_id, lambda l, s=step['seq'], c=created: (
            _record_created(l, s, c), _mark_step(l, s, 'created')))

    def _done(l):
        l['status'] = 'active'
    log.info('BTE lease %s active: %s (%d edges)', lease_id, lease['resource_name'], len(lease['steps']))
    return _update_lease(lease_id, _done)


def _mark_step(lease, seq, status, error=None):
    for st in lease['steps']:
        if st['seq'] == seq:
            st['status'] = status
            if error is not None:
                st['error'] = error


def _record_created(lease, seq, created):
    """Attach TXCore ids to the lease objects of one step (matched by kind + requested name)."""
    for kind, requested_name, obj_id, live_name in created:
        for obj in lease['objects']:
            if obj['seq'] == seq and obj['kind'] == kind and obj['name'] == requested_name and obj['id'] is None:
                obj['id'] = obj_id
                obj['status'] = 'created'
                if live_name and live_name != requested_name:
                    obj['error'] = f'TXCore stored the name as {live_name!r}'
                break


def _rollback(lease_id, client, reason):
    lease = get_lease(lease_id)
    n = sum(1 for o in lease['objects'] if o.get('id'))
    log.error('BTE lease %s failed, rolling back %d objects: %s', lease_id, n, reason)
    _update_lease(lease_id, lambda l: l['errors'].append(reason))
    _delete_objects(lease_id, client)

    def _fail(l):
        l['status'] = 'failed'
        l['finished_at'] = _iso(_now())
    return _update_lease(lease_id, _fail)


def _delete_objects(lease_id, client):
    """Delete a lease's objects: outputs, then sources, then streams; last edge first.

    Returns (deleted, refused, failed)."""
    lease = get_lease(lease_id)
    deleted = refused = failed = 0
    order = {k: i for i, k in enumerate(DELETE_ORDER)}
    objs = sorted((o for o in lease['objects'] if o.get('id') and o['status'] not in ('deleted', 'gone')),
                  key=lambda o: (-o['seq'], order.get(o['kind'], 9)))
    for obj in objs:
        # Guard 1: the registry itself must say this is a BTE object.
        if not registry_is_bte(obj):
            refused += 1
            _update_lease(lease_id, lambda l, s=obj: _mark_obj(l, s, 'refused', 'registry entry is not a BTE object'))
            continue
        try:
            live = client.get_object(obj['kind'], obj['edge_id'], obj['id'])
            if live is None:
                _update_lease(lease_id, lambda l, s=obj: _mark_obj(l, s, 'gone'))
                deleted += 1
                continue
            # Guard 2: the object as it exists in TXCore right now must still be ours
            # (tagged stream, or source/output still attached to a BTE_ stream).
            if not live_is_bte(obj, live):
                refused += 1
                desc = f"name={live.get('name')!r} stream={live.get('stream')!r}" if isinstance(live, dict) else repr(live)
                _update_lease(lease_id, lambda l, s=obj, d=desc: _mark_obj(
                    l, s, 'refused', f'live object is no longer a BTE object ({d}) — not deleted'))
                log.warning('BTE lease %s: refused to delete %s %s (%s)', lease_id, obj['kind'], obj['id'], desc)
                continue
            result = client.delete_object(obj['kind'], obj['edge_id'], obj['id'])
            _update_lease(lease_id, lambda l, s=obj, r=result: _mark_obj(l, s, r))
            deleted += 1
        except TXCoreError as exc:
            failed += 1
            _update_lease(lease_id, lambda l, s=obj, e=str(exc): _mark_obj(l, s, 'delete_error', e))
    return deleted, refused, failed


def _mark_obj(lease, ref, status, error=None):
    for obj in lease['objects']:
        if obj['seq'] == ref['seq'] and obj['kind'] == ref['kind'] and obj['name'] == ref['name']:
            obj['status'] = status
            if error is not None:
                obj['error'] = error


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
