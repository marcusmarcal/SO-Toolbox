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
                                        legacy=yes   TXEdge older than 1.46.0: it has no batch endpoint, so
                                                     objects are created one by one
                                                     (POST /mwedge/<id>/stream | source | output)
                                        version=1.24.1  alternative to legacy=: the edge is legacy when the
                                                     version is below BTE_LEGACY_BELOW (an explicit
                                                     legacy= always wins)
    BTE_TXCORE_EDGE_PATH                Batch create endpoint, default /mwedge/{edge}
    BTE_TXCORE_OBJECT_PATH              Single object (GET/DELETE), default /mwedge/{edge}/{kind}/{id}
                                        {kind} is stream | source | output
    BTE_TXCORE_LEGACY_CREATE_PATH       Per-object create endpoint for legacy edges,
                                        default /mwedge/{edge}/{kind}
    BTE_LEGACY_BELOW                    First TXEdge version with the batch endpoint (1.46.0)
    BTE_DEFAULT_DURATION_MINUTES        Lease length offered by default (60)
    BTE_DEFAULT_EXTEND_MINUTES          Extension offered by default (30)
    BTE_MAX_DURATION_MINUTES            Hard cap for a lease, including extensions (1440)
    BTE_REAPER_DISABLED                 "true" to disable automatic deletion of expired leases
    BTE_AUDIT_MAX_LINES                 Audit log is trimmed to this many most-recent lines (50000)

Audit log: /opt/web/data/bte_audit.jsonl (append-only, mode 0600, one UTC-timestamped
JSON event per line: created, deleted, extended, destination_added). Independent of the
lease registry above so history survives lease history trimming (HISTORY_KEEP).

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
AUDIT_FILE = os.path.join(DATA_DIR, 'bte_audit.jsonl')      # append-only, one JSON object per line, UTC timestamps
AUDIT_LOCK_FILE = AUDIT_FILE + '.lock'

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
AUDIT_MAX_LINES = _env_int('BTE_AUDIT_MAX_LINES', 50000, minimum=1000)

EDGE_PATH = _env('BTE_TXCORE_EDGE_PATH') or '/mwedge/{edge}'
OBJECT_PATH = _env('BTE_TXCORE_OBJECT_PATH') or '/mwedge/{edge}/{kind}/{id}'
LEGACY_CREATE_PATH = _env('BTE_TXCORE_LEGACY_CREATE_PATH') or '/mwedge/{edge}/{kind}'
KINDS = ('stream', 'source', 'output')          # creation order inside a batch
STREAM_ID_PREFIX = 'BTE_'

# TXCore SRT option field names. Confirmed against Skyline's own Dataminer ->
# TXCore automation script (StreamResourceCreation / SRM PLS E2E OptionsSrt /
# OutputsRequest.Options): {type, hostAddress, address, port, latency,
# encryption, passphrase}. "encryption" is a single INTEGER field, not a
# boolean + key-length pair: 0 = no encryption, 16/24/32 = AES-128/192/256
# (the same numbers as the key length in bytes). "passphrase" is always
# present and is null when encryption is 0.
# Unlike UDP, SRT has no separate "networkInterface": the local bind address is
# "hostAddress"; the remote target (pull source, caller mode only) is "address" —
# same key as UDP's target field. Regional edges pulling from the DC edge's
# public SRT address rely on this "address" field.
SRT_OPTION_KEYS = {
    'host': 'hostAddress',     # local interface IP — always present
    'target': 'address',       # remote IP to pull from — caller mode only
    'latency': 'latency',      # ms
    'passphrase': 'passphrase',
}
ENCRYPTION_KEYLEN = {'AES-128': 16, 'AES-192': 24, 'AES-256': 32}  # -> "encryption" field value
# "type" on a SOURCE: 1 = listener (confirmed by the API reference), 0 = caller
# (assumed by elimination — confirm with "Inspect live TXEdge" if a caller
# source is rejected).
SRT_TYPE = {'listener': 1, 'caller': 0}
# "type" on an OUTPUT uses a different enum (Skyline's TXCore automation script:
# StreamModeOutput — Listener=0, Push=1). BTE's own internal DC output (the one
# regional edges pull from) is always a listener (0). A destination output can
# be either: "listen" (0, the destination pulls from us) or "push" (1, we send
# to the destination's address) depending on the Destination pool item.
SRT_OUTPUT_TYPE = {'listen': 0, 'push': 1}
DELETE_ORDER = ('output', 'source', 'stream')   # outputs first, the stream last

# Regional sites: site prefix (edge keys AVE02, LMK01, ... start with it) -> Dataminer multicast property.
REGIONAL_SITES = (
    ('AVE', 'Aveiro Multicast'),
    ('LMK', 'Limerick Multicast'),
    ('YER', 'Yerevan Multicast'),
)
_EDGE_KEY_RE = re.compile(r'BTE_EDGE_([A-Za-z0-9]+)')
_HOST_LIST_RE = re.compile(r'\s*([A-Za-z]+)@([^,;\s]+)')


def _version_tuple(value):
    """'1.24.1' -> (1, 24, 1); '1.46' -> (1, 46, 0); unparsable -> None."""
    nums = [int(n) for n in re.findall(r'\d+', str(value or ''))[:3]]
    return tuple((nums + [0, 0, 0])[:3]) if nums else None


# TXEdges older than this have no {streams, sources, outputs} batch endpoint.
LEGACY_BELOW = _version_tuple(_env('BTE_LEGACY_BELOW')) or (1, 46, 0)


def _is_legacy(fields):
    """legacy=yes|no wins; otherwise version=<x.y.z> below LEGACY_BELOW means legacy."""
    flag = fields.get('legacy')
    if flag:
        return flag.lower() in ('yes', 'true', '1')
    version = _version_tuple(fields.get('version'))
    return bool(version and version < LEGACY_BELOW)


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
        'legacy': _is_legacy(fields),
        'version': fields.get('version') or None,
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
                      'in': v['in'], 'out': v['out'], 'pub': v['pub'],
                      'legacy': v['legacy'], 'version': v['version']} for k, v in sorted(EDGES.items())},
        'legacy_below': '.'.join(str(n) for n in LEGACY_BELOW),
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


def destination_output_name(base, edge_key, protocol, destination):
    return f'OUT_{_slug(base)}_{protocol}_{edge_key}_DEST_{_slug(destination)}'


def has_tag(name):
    return BTE_TAG in str(name or '')


def is_bte_stream_id(value):
    return str(value or '').startswith(STREAM_ID_PREFIX)


def parse_destination(dest):
    """Best-effort parse of a Destination pool item into an output spec.

    Property names (Protocol / IP / Port / Type / "Output Port") are ASSUMED
    from the existing read-only Destinations viewer -- adjust here if the live
    schema differs. Returns None when Protocol/IP/Port cannot be determined.
    "listen" True means the destination pulls from us (output type "listen");
    False means we push to the destination's address (output type "push"),
    inferred from the "Type" property containing the word "listen".
    """
    props = dest.get('properties') or {}
    protocol = str(props.get('Protocol') or '').strip().upper()
    ip = str(props.get('IP') or '').strip()
    port = _int_or_none(props.get('Output Port')) or _int_or_none(props.get('Port'))
    listen = 'listen' in str(props.get('Type') or '').lower()
    if protocol not in ('UDP', 'SRT') or not port:
        return None
    # UDP and SRT "push" (we send to the destination) need a target address;
    # SRT "listen" (the destination pulls from us) does not.
    if not ip and not (protocol == 'SRT' and listen):
        return None
    return {'protocol': protocol, 'address': ip, 'port': port, 'listen': listen}


def destination_object(base, edge_key, edge, dest, sid):
    """Build the {kind, name, body, destination_id, destination_name} output
    object for one Destination pool item. Only used on DC edges, per BTE's
    design: destinations are additional outputs alongside the DC edge's
    primary output, never on regional edges. Returns None when the
    destination cannot be parsed (caller should warn instead of failing).
    """
    spec = parse_destination(dest)
    if spec is None:
        return None
    label = dest.get('name') or dest.get('id') or 'dest'
    name = destination_output_name(base, edge_key, spec['protocol'], label)
    if spec['protocol'] == 'UDP':
        interface = edge['out'].get('UDP') or edge['out'].get('SRT')
        body = _endpoint_obj(sid, name, 'UDP', _udp_options(spec['address'], spec['port'], interface))
    else:
        interface = edge['out'].get('SRT')
        mode = 'listener' if spec['listen'] else 'caller'
        srt_type = SRT_OUTPUT_TYPE['listen'] if spec['listen'] else SRT_OUTPUT_TYPE['push']
        target = None if spec['listen'] else spec['address']
        body = _endpoint_obj(sid, name, 'SRT', _srt_options(mode, target, spec['port'], None, None, None, interface, srt_type=srt_type))
    return {'kind': 'output', 'name': name, 'body': body,
            'destination_id': dest.get('id'), 'destination_name': dest.get('name')}


def _own_stream_ids(lease):
    """TXCore ids of the tagged streams of a lease (server-assigned on legacy edges)."""
    return {o['id'] for o in lease['objects']
            if o['kind'] == 'stream' and o.get('id') and has_tag(o['name'])}


def registry_is_bte(obj, stream_ids=()):
    """Guard 1 — what the registry says about an object we created.

    On a legacy edge TXCore assigns the stream id itself (no "BTE_" prefix), so
    the tag in the stream name is the marker there and sources/outputs are
    recognised by pointing at one of the lease's own tagged streams.
    """
    body = obj.get('body') or {}
    legacy = bool(obj.get('legacy'))
    if obj['kind'] == 'stream':
        return has_tag(obj['name']) and (is_bte_stream_id(body.get('id')) or legacy)
    stream = body.get('stream')
    return is_bte_stream_id(stream) or (legacy and stream in stream_ids)


def live_is_bte(obj, live, stream_ids=()):
    """Guard 2 — what TXCore says about the object right now."""
    if not isinstance(live, dict):
        return False
    legacy = bool(obj.get('legacy'))
    live_id = live.get('id') or live.get('_id') or obj.get('id')
    if obj['kind'] == 'stream':
        return has_tag(live.get('name')) and (
            is_bte_stream_id(live_id) or (legacy and live_id == obj.get('id')))
    stream = live.get('stream')
    return is_bte_stream_id(stream) or (legacy and stream in stream_ids)


def _stream_id(base, edge_key):
    """Client-chosen TXCore stream id: unique per lease, still readable in TXCore."""
    return f'{STREAM_ID_PREFIX}{_slug(base)[:40]}_{edge_key}_{uuid.uuid4().hex[:8]}'


def _srt_options(mode, target_address, port, latency, passphrase, encryption, interface, srt_type=None):
    """SRT option block: {type, hostAddress, address, port, latency, encryption, passphrase}.

    ``interface`` (local bind IP) always goes to ``hostAddress``. ``target_address``
    (the remote host to pull from / push to) goes to ``address`` and only applies
    when ``mode == 'caller'``.
    ``srt_type`` overrides the numeric "type" field directly — used for outputs,
    which have their own enum (see SRT_OUTPUT_TYPE) distinct from a source's
    listener/caller. When omitted, "type" is derived from ``mode`` via SRT_TYPE
    (source semantics: 1 = listener, 0 = caller).
    ``encryption``/``passphrase`` are always present: encryption=0 and
    passphrase=null when there is no passphrase to send.
    """
    k = SRT_OPTION_KEYS
    keylen = ENCRYPTION_KEYLEN.get(str(encryption or 'AES-256').upper(), 32) if passphrase else 0
    return {
        'type': SRT_TYPE[mode] if srt_type is None else srt_type,
        k['host']: interface,
        k['target']: target_address if mode == 'caller' else None,
        'port': port,
        k['latency']: latency or 500,
        'encryption': keylen,
        k['passphrase']: passphrase if keylen else None,
    }


def _udp_options(host, port, interface):
    return {'port': port, 'address': host, 'networkInterface': interface}


def _stream_obj(stream_id, name, failover):
    return {'id': stream_id, 'name': name, 'options': {'failoverMode': failover}}


def _endpoint_obj(stream_id, name, protocol, options):
    return {'stream': stream_id, 'name': name, 'tags': 'bte', 'protocol': protocol, 'active': True, 'options': options}


def build_plan(item, edges=None, passphrase_override=None, destinations=None):
    """Return {'ok', 'steps', 'warnings', 'errors', 'summary'} for a snapshot item.

    One step per edge = one POST /mwedge/<edge id>. Each step lists the objects
    it creates (kind, name, body); the request body is assembled at run time.
    ``passphrase_override`` replaces the supplier passphrase of the resource
    (used while the Dataminer API does not expose the real value).
    ``destinations`` is an optional list of Destination pool items: each
    becomes an extra output on the DC edge only, alongside the primary output.
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
                      'location': edge.get('location'), 'legacy': bool(edge.get('legacy')),
                      'objects': objects})

    # ---- DC edge ----------------------------------------------------------
    sid = _stream_id(base, dc_key)
    proto = main_in['protocol'].upper()
    n_stream = stream_name(base, dc_key)
    n_src = source_name(base, dc_key, proto)
    n_out = output_name(base, dc_key, 'SRT')
    if proto == 'SRT':
        mode = 'listener' if main_in['mode'] == 'listener' else 'caller'
        src_opts = _srt_options(mode, main_in['host'], main_in['port'], latency,
                                passphrase if encrypted else None, encryption, dc['in'].get('SRT'))
    else:
        src_opts = _udp_options(main_in['host'], main_in['port'], dc['in'].get(proto) or dc['in'].get('UDP'))
    dc_objects = [
        {'kind': 'stream', 'name': n_stream, 'body': _stream_obj(sid, n_stream, 'none')},
        {'kind': 'source', 'name': n_src, 'body': _endpoint_obj(sid, n_src, proto, src_opts)},
        {'kind': 'output', 'name': n_out, 'body': _endpoint_obj(sid, n_out, 'SRT', _srt_options(
            'listener', None, out_port, latency, INTERNAL_PASSPHRASE, 'AES-256', dc['out'].get('SRT'), srt_type=SRT_OUTPUT_TYPE['listen']))},
    ]
    seen_dest_ids = set()
    for dest in (destinations or []):
        dest_id = dest.get('id')
        label = dest.get('name') or dest_id or 'destination'
        if dest_id and dest_id in seen_dest_ids:
            warnings.append(f'Destination {label}: selected twice — added only once')
            continue
        obj = destination_object(base, dc_key, dc, dest, sid)
        if obj is None:
            warnings.append(f'Destination {label}: could not parse Protocol/IP/Port — skipped')
            continue
        seen_dest_ids.add(dest_id)
        dc_objects.append(obj)
    step(dc, dc_objects)

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
                'caller', dc['pub']['SRT'], out_port, latency, INTERNAL_PASSPHRASE, 'AES-256', edge['in'].get('SRT')))},
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
            'destinations': len(seen_dest_ids),
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


def _legacy_body(kind, body):
    """Body for the per-object endpoints of TXEdge < 1.46.0 (documented fields only)."""
    if kind == 'stream':
        return {'name': body.get('name')}          # id is assigned by TXCore
    out = dict(body)
    if kind == 'output':
        out.pop('active', None)                     # "active" only exists on sources
    return out


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

    def legacy_url(self, kind, edge_id):
        return self.url + LEGACY_CREATE_PATH.replace('{edge}', str(edge_id)).replace('{kind}', kind)

    def create_objects(self, edge_id, objects, legacy=False):
        """Create ``objects`` ([{'kind', 'body'}]) on one edge.

        Modern edges take one batch POST; legacy edges (< 1.46.0) one POST per
        object. Returns [(kind, requested_name, id, name)]; on any failure raises
        TXCoreError whose ``created`` attribute lists what already exists.
        """
        if legacy:
            return self.create_sequential(edge_id, objects)
        return self.create_batch(edge_id, batch_body(objects))

    def create_sequential(self, edge_id, objects):
        """Legacy create: stream first, then sources/outputs against the id TXCore assigned.

        A failing object never stops the others (except those that depend on a
        stream that could not be created); everything that was created is
        reported in the exception so the caller can keep tracking it.
        """
        created, failures = [], []
        stream_map = {}          # planned stream id -> id assigned by TXCore
        failed_streams = set()
        for kind in KINDS:
            for obj in objects:
                if obj['kind'] != kind:
                    continue
                body = _legacy_body(kind, obj['body'])
                name = body.get('name')
                planned = (obj['body'] or {}).get('id' if kind == 'stream' else 'stream')
                if kind != 'stream':
                    if planned in failed_streams:
                        failures.append(f'{kind} {name!r}: skipped, its stream was not created')
                        continue
                    body['stream'] = stream_map.get(planned, planned)
                try:
                    obj_id, live_name = self._post_object(edge_id, kind, body)
                except TXCoreError as exc:
                    failures.append(f'{kind} {name!r}: {exc}')
                    if kind == 'stream':
                        failed_streams.add(planned)
                    continue
                if kind == 'stream':
                    stream_map[planned] = obj_id
                created.append((kind, name, obj_id, live_name))
        if failures:
            err = TXCoreError('; '.join(failures))
            err.created = created
            raise err
        return created

    def _post_object(self, edge_id, kind, body):
        """POST one object to a legacy edge. Returns (id, name)."""
        url = self.legacy_url(kind, edge_id)
        try:
            resp = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TXCoreError(f'POST {url} failed: {exc}') from exc
        payload = self._json(resp)
        if not resp.ok:
            raise TXCoreError(f'POST {kind} returned HTTP {resp.status_code}: {json.dumps(payload)[:300]}')
        if isinstance(payload, dict) and payload.get('success') is False:
            raise TXCoreError(f'POST {kind} refused: {json.dumps(payload)[:300]}')
        data = payload.get('data') if isinstance(payload, dict) and isinstance(payload.get('data'), dict) else payload
        obj_id = live_name = None
        if isinstance(data, dict):
            obj_id = data.get('id') or data.get('_id')
            live_name = data.get('name')
        if not obj_id:
            # The object may exist even though the answer carried no id: look it up by name
            # so it is never left behind untracked.
            obj_id = self._find_created(edge_id, kind, body)
        if not obj_id:
            raise TXCoreError(f'{kind} {body.get("name")!r} may exist on the edge but TXCore returned no id '
                              f'(check the TXEdge for an orphan)')
        return str(obj_id), live_name

    def _find_created(self, edge_id, kind, body):
        try:
            doc = self.get_edge(edge_id)
        except TXCoreError:
            return None
        candidates = (doc.get(kind + 's') or []) if isinstance(doc, dict) else []
        for cand in candidates:
            if cand.get('name') == body.get('name') and (kind == 'stream' or cand.get('stream') == body.get('stream')):
                return cand.get('id') or cand.get('_id')
        return None

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


def _append_audit(event):
    """Append one UTC-timestamped event to the audit log. Best-effort: a failure
    here must never break lease creation/deletion, only get logged."""
    event = dict(event)
    event.setdefault('ts', _iso(_now()))
    line = json.dumps(event, separators=(',', ':'), default=str)
    try:
        with _Locked(AUDIT_LOCK_FILE):
            with open(AUDIT_FILE, 'a') as f:
                f.write(line + '\n')
            try:
                os.chmod(AUDIT_FILE, 0o600)
            except OSError:
                pass
            _trim_audit()
    except OSError:
        log.exception('BTE audit: failed to append %s event', event.get('event'))


def _trim_audit():
    """Keep the audit log bounded: drop the oldest lines once it grows past the cap."""
    try:
        with open(AUDIT_FILE, 'r') as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= AUDIT_MAX_LINES:
        return
    keep = lines[-AUDIT_MAX_LINES:]
    tmp = AUDIT_FILE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.writelines(keep)
    os.replace(tmp, AUDIT_FILE)


def read_audit(limit=200, lease_id=None, resource_id=None):
    """Most-recent-first audit events, optionally filtered by lease_id / resource_id."""
    try:
        with _Locked(AUDIT_LOCK_FILE):
            with open(AUDIT_FILE, 'r') as f:
                lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if lease_id and event.get('lease_id') != lease_id:
            continue
        if resource_id and event.get('resource_id') != resource_id:
            continue
        out.append(event)
        if len(out) >= limit:
            break
    return out


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


def active_lease_for_resource(resource_id):
    """The active (non-final) lease for a snapshot resource, if any. Used to
    stop a channel from being provisioned twice at once."""
    with _Locked(LEASES_LOCK_FILE):
        leases = _read_registry()['leases']
    for lease in leases.values():
        if lease.get('resource_id') == resource_id and lease['status'] in ACTIVE_STATUSES:
            return copy.deepcopy(lease)
    return None


def used_destination_ids(exclude_lease=None):
    """Destination ids currently attached to any non-final lease.

    A destination cannot be attached twice system-wide; ``exclude_lease``
    lets a lease check against everyone else's usage (its own entries don't
    count against itself).
    """
    with _Locked(LEASES_LOCK_FILE):
        leases = _read_registry()['leases']
    used = set()
    for lease_id, lease in leases.items():
        if exclude_lease and lease_id == exclude_lease:
            continue
        if lease['status'] in FINAL_STATUSES:
            continue
        for obj in lease.get('objects', []):
            if obj.get('destination_id') and obj.get('status') not in ('refused', 'error'):
                used.add(obj['destination_id'])
    return used


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
             'status': 'pending', 'error': None, 'legacy': bool(s.get('legacy'))}
            for s in plan['steps']
        ],
        # Objects created in TXCore (filled in while the plan runs). Every entry
        # must carry the [BTE] tag in its name to be deletable.
        'objects': [
            {'seq': s['seq'], 'edge': s['edge'], 'edge_id': s['edge_id'], 'kind': o['kind'],
             'name': o['name'], 'body': o['body'], 'id': None, 'status': 'pending', 'error': None,
             'destination_id': o.get('destination_id'), 'destination_name': o.get('destination_name'),
             'legacy': bool(s.get('legacy'))}
            for s in plan['steps'] for o in s['objects']
        ],
        'partial': False,
        'finished_at': None,
    }

    def _add(data):
        data['leases'][lease['lease_id']] = lease
        return lease

    lease = _mutate(_add)
    dests = [{'id': o['destination_id'], 'name': o['destination_name']}
             for s in plan['steps'] for o in s['objects'] if o.get('destination_id')]
    _append_audit({
        'event': 'created',
        'lease_id': lease['lease_id'],
        'resource_id': lease['resource_id'],
        'resource_name': lease['resource_name'],
        'dc_edge': lease['dc_edge'],
        'user': username,
        'dry_run': lease['dry_run'],
        'duration_minutes': lease['duration_minutes'],
        'expires_at': lease['expires_at'],
        'destinations': dests,
    })
    return lease


def _update_lease(lease_id, fn):
    def _apply(data):
        lease = data['leases'].get(lease_id)
        if lease is None:
            return None
        fn(lease)
        return copy.deepcopy(lease)
    return _mutate(_apply)


def run_lease(lease_id, client=None):
    """Execute the lease plan: one call (batch) or one call per object (legacy) per edge.

    A failing edge never aborts the others, and nothing is rolled back:
    everything TXCore reports as created stays in the lease (visible in the UI,
    deletable, reaped on expiry). The lease ends 'active' with ``partial`` set
    when some edge is incomplete, or 'failed' when nothing at all was created.
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
        seq = step['seq']
        label = f"step {seq} ({step['edge']})"
        objs = [o for o in lease['objects'] if o['seq'] == seq]
        untagged = [o['name'] for o in objs if not registry_is_bte(o)]
        if untagged:
            # Defensive: never create an object we could not recognise as ours later.
            reason = f"{label}: refused, not identifiable as BTE objects: {untagged}"
            log.error('BTE lease %s: %s', lease_id, reason)
            _update_lease(lease_id, lambda l, s=seq, e=reason: (l['errors'].append(e), _mark_step(l, s, 'error', e)))
            continue
        try:
            created = client.create_objects(
                step['edge_id'], [{'kind': o['kind'], 'body': o['body']} for o in objs],
                legacy=bool(step.get('legacy')))
        except Exception as exc:  # noqa: BLE001 — one edge failing must not stop the others
            if not isinstance(exc, TXCoreError):
                log.exception('BTE lease %s: unexpected error on %s', lease_id, label)
            partial = getattr(exc, 'created', [])
            reason = f'{label}: {exc}'
            log.error('BTE lease %s: %s (%d objects were created and are kept)', lease_id, reason, len(partial))
            _update_lease(lease_id, lambda l, s=seq, c=partial, e=reason: (
                _record_created(l, s, c), l['errors'].append(e), _mark_step(l, s, 'error', e)))
            continue
        _update_lease(lease_id, lambda l, s=seq, c=created: (
            _record_created(l, s, c), _mark_step(l, s, 'created')))

    def _finish(l):
        made = sum(1 for o in l['objects'] if o.get('id'))
        bad = [st for st in l['steps'] if st['status'] == 'error']
        if not made:
            l['status'] = 'failed'
            l['finished_at'] = _iso(_now())
            return
        l['status'] = 'active'
        l['partial'] = bool(bad)
        if bad:
            l['warnings'].append(
                f"Partial: {len(bad)} of {len(l['steps'])} edges incomplete ({', '.join(st['edge'] for st in bad)}) "
                f"— what was created is kept and can be deleted from the stream list")
    result = _update_lease(lease_id, _finish)
    log.info('BTE lease %s %s%s: %s (%d edges)', lease_id, result['status'],
             ' (partial)' if result.get('partial') else '', lease['resource_name'], len(lease['steps']))
    return result


def _mark_step(lease, seq, status, error=None):
    for st in lease['steps']:
        if st['seq'] == seq:
            st['status'] = status
            if error is not None:
                st['error'] = error
    if status == 'error':
        # What never got an id was not created: flag it so the UI counts it as a problem.
        for obj in lease['objects']:
            if obj['seq'] == seq and obj.get('id') is None and obj['status'] == 'pending':
                obj['status'] = 'error'
                obj['error'] = 'not created'


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
    # Legacy edges assign the stream id themselves: point the step's sources/outputs at it.
    stream_obj = next((o for o in lease['objects']
                       if o['seq'] == seq and o['kind'] == 'stream' and o.get('id')), None)
    if stream_obj:
        for obj in lease['objects']:
            body = obj.get('body')
            if obj['seq'] == seq and obj['kind'] != 'stream' and isinstance(body, dict) \
                    and body.get('stream') != stream_obj['id']:
                body['stream'] = stream_obj['id']


def _delete_objects(lease_id, client):
    """Delete a lease's objects: outputs, then sources, then streams; last edge first.

    Returns (deleted, refused, failed)."""
    lease = get_lease(lease_id)
    own_streams = _own_stream_ids(lease)
    deleted = refused = failed = 0
    order = {k: i for i, k in enumerate(DELETE_ORDER)}
    objs = sorted((o for o in lease['objects'] if o.get('id') and o['status'] not in ('deleted', 'gone')),
                  key=lambda o: (-o['seq'], order.get(o['kind'], 9)))
    for obj in objs:
        # Guard 1: the registry itself must say this is a BTE object.
        if not registry_is_bte(obj, own_streams):
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
            if not live_is_bte(obj, live, own_streams):
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
    """Remove everything a lease created. Returns the updated lease or None.

    ``reason`` 'expired' marks the deletion as automatic (reaper); any other
    reason ('manual', 'manual-all', ...) is logged with ``username`` as the
    person who deleted it.
    """
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
        result = _update_lease(lease_id, _finish_dry)
    else:
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

    _append_audit({
        'event': 'deleted',
        'lease_id': lease_id,
        'resource_id': lease.get('resource_id'),
        'resource_name': lease.get('resource_name'),
        'dc_edge': lease.get('dc_edge'),
        'user': username,
        'auto': reason == 'expired',
        'reason': reason,
        'status': result['status'],
    })
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
    if outcome['error'] is None:
        granted = (lease['extensions'][-1] or {}).get('minutes') if lease['extensions'] else None
        _append_audit({
            'event': 'extended',
            'lease_id': lease_id,
            'resource_id': lease.get('resource_id'),
            'resource_name': lease.get('resource_name'),
            'user': username,
            'minutes_requested': minutes,
            'minutes_granted': granted,
            'expires_at': lease['expires_at'],
            'note': outcome['note'],
        })
    return lease, outcome['error'], outcome['note']


def add_destination(lease_id, destination_item, username, client=None):
    """Attach one Destination pool item as an extra output on a lease's DC edge.

    Reuses the DC edge's already-created stream id, so this is a single
    outputs-only POST — no new stream. Works for an already-active lease
    (the "already live" case) or a dry-run lease (marked skipped, like the
    rest of that lease's objects). Returns (lease, error); on error nothing
    is created and no object is added.
    """
    lease = get_lease(lease_id)
    if lease is None:
        return None, 'Lease not found'
    if lease['status'] != 'active':
        return lease, f"Lease is {lease['status']} — destinations can only be added to an active lease"

    dest_id = destination_item.get('id')
    if not dest_id:
        return lease, 'Destination has no id'
    if dest_id in used_destination_ids(exclude_lease=lease_id):
        return lease, 'This destination is already attached to another BTE stream'
    if any(o.get('destination_id') == dest_id and o['status'] not in ('refused', 'error') for o in lease['objects']):
        return lease, 'This destination is already attached to this stream'

    dc_key = lease['dc_edge']
    edge = EDGES.get(dc_key)
    if edge is None:
        return lease, f'Edge {dc_key} is not configured on the server'
    stream_obj = next((o for o in lease['objects'] if o['kind'] == 'stream' and o['edge'] == dc_key), None)
    if stream_obj is None:
        return lease, 'DC edge stream not found on this lease'
    sid = stream_obj.get('id') or (stream_obj.get('body') or {}).get('id')
    if not sid:
        return lease, 'DC edge stream has no id yet — try again shortly'

    obj = destination_object(lease['resource_name'] or 'STREAM', dc_key, edge, destination_item, sid)
    if obj is None:
        return lease, 'Could not parse this destination (missing Protocol/IP/Port)'

    seq = max((o['seq'] for o in lease['objects']), default=0) + 1
    new_obj = {'seq': seq, 'edge': dc_key, 'edge_id': edge['id'], 'kind': obj['kind'], 'name': obj['name'],
               'body': obj['body'], 'id': None, 'status': 'pending', 'error': None,
               'destination_id': obj['destination_id'], 'destination_name': obj['destination_name'],
               'legacy': bool(edge.get('legacy'))}

    def _append(l):
        l['objects'].append(dict(new_obj))
    lease = _update_lease(lease_id, _append)

    def _audit_added(dry_run):
        _append_audit({
            'event': 'destination_added',
            'lease_id': lease_id,
            'resource_id': lease.get('resource_id'),
            'resource_name': lease.get('resource_name'),
            'user': username,
            'at_creation': False,
            'destination_id': obj['destination_id'],
            'destination_name': obj['destination_name'],
            'dry_run': dry_run,
        })

    if lease['dry_run']:
        def _skip(l):
            _mark_obj(l, new_obj, 'skipped')
        lease = _update_lease(lease_id, _skip)
        _audit_added(True)
        return lease, None

    client = client or TXCoreClient()
    try:
        created = client.create_objects(edge['id'], [{'kind': new_obj['kind'], 'body': new_obj['body']}],
                                        legacy=bool(edge.get('legacy')))
    except TXCoreError as exc:
        lease = _update_lease(lease_id, lambda l: _mark_obj(l, new_obj, 'error', str(exc)))
        return lease, str(exc)
    lease = _update_lease(lease_id, lambda l: _record_created(l, seq, created))
    _audit_added(False)
    log.info('BTE lease %s: added destination %s (%s)', lease_id, obj['destination_name'] or dest_id, username)
    return lease, None


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
