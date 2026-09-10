"""
SO-Toolbox - TXCore Channel/Category Management
Blueprint for bulk creation of TXCore categories and channels via the
TXCore API. Ported from the original standalone create_channels.py script.

Two TXCore clusters are supported. Every endpoint accepts a ``cluster``
selector (JSON field or ``?cluster=`` query parameter); it defaults to
``stb`` so existing callers keep working unchanged.

    stb   Multicast UDP sources, one per geofenced site (AVE / LMK / YER).
    main  SRT caller sources with passphrase, primary + backup TXEdge.

Environment variables (.env):
    # STB cluster (multicast)
    BEARER_TOKEN_STB        - Bearer token for the STB TXCore API
    APIURLSTB               - Base URL for the STB TXCore API (no trailing slash)
    AVEGEOID                - Geofence ID for the "ave" location
    LMKGEOID                - Geofence ID for the "lmk" location
    YERGEOID                - Geofence ID for the "yer" location

    # MAIN cluster (SRT)
    BEARER_TOKEN_MAIN       - Bearer token for the MAIN TXCore API
    APIURLMAIN              - Base URL for the MAIN TXCore API (no trailing slash)
    INTERNALSRTPASSPHRASE   - Passphrase applied to every SRT source
                              (falls back to SRT_PASSPHRASE when not set)
    SRT_SERVER_n=IP|Label   - Optional presets offered as SRT source hosts
                              (shared with the SRT URI Builder)

Secrets are never returned to the browser: status reports only whether a
value is set, and passphrases are redacted from previews and persisted jobs.
"""

import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Blueprint, jsonify, request

from routes_auth import _get_session, _token_from_request

# Load .env explicitly so config is available regardless of whether
# proxy.py has already called load_dotenv() before importing this
# blueprint. Safe to call again if proxy.py already did — load_dotenv()
# does not override variables already set in the environment.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

txcore_bp = Blueprint('txcore', __name__, url_prefix='/api/txcore')

ALLOWED_ROLES = ('admin', 'engineer')

# ---------------------------------------------------------------------------
# Configuration (populated from environment variables set in .env)
# ---------------------------------------------------------------------------


def _env(*names):
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _env_url(name):
    value = _env(name)
    return value.rstrip('/') if value else None


GEOFENCE_IDS = {
    'ave': _env('AVEGEOID'),
    'lmk': _env('LMKGEOID'),
    'yer': _env('YERGEOID'),
}

# Passphrase applied to SRT sources on the MAIN cluster. INTERNALSRTPASSPHRASE
# mirrors the original script; SRT_PASSPHRASE is the toolbox-wide default.
SRT_PASSPHRASE = _env('INTERNALSRTPASSPHRASE', 'SRT_PASSPHRASE')

SRT_PROTOCOL = 6        # TXCore source protocol id for SRT
MULTICAST_PROTOCOL = 0  # TXCore source protocol id for UDP multicast
REDACTED = '********'

CLUSTERS = {
    'stb': {
        'key': 'stb',
        'label': 'STB',
        'mode': 'multicast',
        'description': 'Multicast UDP sources, one per geofenced site (AVE / LMK / YER)',
        'token': _env('BEARER_TOKEN_STB'),
        'url': _env_url('APIURLSTB'),
    },
    'main': {
        'key': 'main',
        'label': 'MAIN',
        'mode': 'srt',
        'description': 'SRT caller sources with passphrase, primary + backup TXEdge',
        'token': _env('BEARER_TOKEN_MAIN'),
        'url': _env_url('APIURLMAIN'),
    },
}
DEFAULT_CLUSTER = 'stb'

# One HTTP session per cluster so the bearer token never crosses clusters.
for _cluster in CLUSTERS.values():
    _session = requests.Session()
    _session.headers.update({
        'Authorization': f"Bearer {_cluster['token']}",
        'Content-Type': 'application/json',
    })
    _cluster['session'] = _session

# Backwards-compatible aliases (STB was the only cluster before v3.58).
API_TOKEN = CLUSTERS['stb']['token']
API_URL_STB = CLUSTERS['stb']['url']
api_session = CLUSTERS['stb']['session']

JOBS_DIR = '/opt/web/data/txcore_jobs'
JOBS_LOCK = threading.Lock()

os.makedirs(JOBS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_path(job_id):
    return os.path.join(JOBS_DIR, f'{job_id}.json')


def _write_job(job_id, data):
    """Atomic write of job state to disk (copy + os.replace)."""
    path = _job_path(job_id)
    tmp_path = path + '.tmp'
    with JOBS_LOCK:
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)


def _read_job(job_id):
    path = _job_path(job_id)
    if not os.path.exists(path):
        return None
    with JOBS_LOCK:
        with open(path, 'r') as f:
            return json.load(f)


def _get_user_and_role():
    """Return (username, role) for the current request, or (None, None) if unauthenticated."""
    session = _get_session(_token_from_request())
    if not session:
        return None, None
    return session.get('username', 'anonymous'), session.get('role')


def _resolve_cluster(data=None):
    """Pick the target cluster from the JSON body or query string. Returns (cluster, error)."""
    key = (data or {}).get('cluster') or request.args.get('cluster') or DEFAULT_CLUSTER
    key = str(key).strip().lower()
    cluster = CLUSTERS.get(key)
    if cluster is None:
        return None, f"Unknown cluster '{key}' (expected one of: {', '.join(CLUSTERS)})"
    return cluster, None


def _cluster_configured(cluster):
    return bool(cluster['token'] and cluster['url'])


def _not_configured(cluster):
    return jsonify({
        'error': f"TXCore API for cluster {cluster['label']} is not configured on the server",
        'cluster': cluster['key'],
    }), 500


def _srt_source_presets():
    """SRT source hosts offered in the UI, from SRT_SERVER_n=IP|Label entries."""
    keys = [k for k in os.environ if re.fullmatch(r'SRT_SERVER_\d+', k)]
    keys.sort(key=lambda k: int(k.rsplit('_', 1)[1]))
    presets = []
    for key in keys:
        ip, _, label = os.environ[key].partition('|')
        ip = ip.strip()
        if not ip:
            continue
        presets.append({'ip': ip, 'label': label.strip() or ip})
    return presets


def _cluster_status(cluster):
    """Per-cluster configuration report, without leaking secret values."""
    status = {
        'label': cluster['label'],
        'mode': cluster['mode'],
        'description': cluster['description'],
        'bearer_token_set': bool(cluster['token']),
        'api_url_set': bool(cluster['url']),
        'api_url': cluster['url'],
    }
    if cluster['mode'] == 'multicast':
        status['geofence_ids_set'] = {k: bool(v) for k, v in GEOFENCE_IDS.items()}
    else:
        status['srt_passphrase_set'] = bool(SRT_PASSPHRASE)
        status['srt_source_presets'] = _srt_source_presets()
    return status


def _config_status():
    """Report which required env vars are set for every cluster."""
    clusters = {key: _cluster_status(c) for key, c in CLUSTERS.items()}
    status = {'default_cluster': DEFAULT_CLUSTER, 'clusters': clusters}
    # Legacy top-level fields (pre-3.58 frontend expected STB only).
    stb = clusters['stb']
    status.update({
        'bearer_token_set': stb['bearer_token_set'],
        'api_url_set': stb['api_url_set'],
        'api_url': stb['api_url'],
        'geofence_ids_set': stb['geofence_ids_set'],
    })
    return status


def _build_multicast_sources(params, index):
    sources = []
    for loc in ('ave', 'lmk', 'yer'):
        loc_cfg = params['locations'][loc]
        octet4 = loc_cfg['udp_ip_4_oct'] + index
        sources.append({
            'protocol': MULTICAST_PROTOCOL,
            'address': f"{loc_cfg['udp_ip_3_oct']}.{octet4}:{loc_cfg['port']}",
            'geofence': GEOFENCE_IDS[loc],
        })
    return sources


def _build_srt_sources(params, index, passphrase):
    srt = params['srt']
    port = srt['first_port'] + index
    sources = []
    for priority, ip in enumerate((srt['primary_ip'], srt['backup_ip'])):
        if not ip:
            continue  # backup edge is optional
        source = {
            'protocol': SRT_PROTOCOL,
            'address': f'{ip}:{port}',
            'priority': priority,
        }
        if srt['encrypted']:
            source['options'] = {'srt': {'encrypted': True, 'passphrase': passphrase}}
        sources.append(source)
    return sources


def _build_channel_body(params, index, passphrase=None):
    """Build a single channel request body given the bulk params and offset index."""
    name_id = '{0:0>2}'.format(params['first_ch'] + index)
    if params['mode'] == 'srt':
        sources = _build_srt_sources(params, index, passphrase)
    else:
        sources = _build_multicast_sources(params, index)

    return {
        'number': params['channel_number'] + index,
        'name': f"{params['provider_name']}_CH{name_id}",
        'type': 0,
        'category': params['category_id'],
        'enabled': True,
        'sources': sources,
    }


def _redact_body(body):
    """Copy of a channel body with any SRT passphrase masked."""
    redacted = copy.deepcopy(body)
    for source in redacted.get('sources', []):
        srt_opts = (source.get('options') or {}).get('srt')
        if srt_opts and 'passphrase' in srt_opts:
            srt_opts['passphrase'] = REDACTED
    return redacted


def _parse_multicast_params(data):
    locations = data.get('locations')
    if not locations or not all(k in locations for k in ('ave', 'lmk', 'yer')):
        return None, 'Missing required field: locations (must include ave, lmk, yer)'

    for loc_key, loc_cfg in locations.items():
        for f in ('udp_ip_3_oct', 'udp_ip_4_oct', 'port'):
            if f not in loc_cfg:
                return None, f'Missing required field: locations.{loc_key}.{f}'

    return {
        loc: {
            'udp_ip_3_oct': str(cfg['udp_ip_3_oct']),
            'udp_ip_4_oct': int(cfg['udp_ip_4_oct']),
            'port': int(cfg['port']),
        } for loc, cfg in locations.items()
    }, None


_HOST_RE = re.compile(r'^[A-Za-z0-9.\-:\[\]]+$')


def _parse_srt_params(data, channel_count):
    srt = data.get('srt')
    if not srt or not isinstance(srt, dict):
        return None, 'Missing required field: srt (primary_ip, first_port)'
    for f in ('primary_ip', 'first_port'):
        if f not in srt or srt[f] in (None, ''):
            return None, f'Missing required field: srt.{f}'

    primary_ip = str(srt['primary_ip']).strip()
    backup_ip = str(srt.get('backup_ip') or '').strip()
    for label, host in (('primary_ip', primary_ip), ('backup_ip', backup_ip)):
        if host and not _HOST_RE.match(host):
            return None, f'Invalid host in srt.{label}'
    if backup_ip and backup_ip == primary_ip:
        return None, 'srt.backup_ip must differ from srt.primary_ip'

    first_port = int(srt['first_port'])
    if not 1 <= first_port <= 65535:
        return None, 'srt.first_port must be between 1 and 65535'
    if first_port + channel_count - 1 > 65535:
        return None, 'srt.first_port + channel_count exceeds port 65535'

    return {
        'primary_ip': primary_ip,
        'backup_ip': backup_ip,
        'first_port': first_port,
        'encrypted': bool(srt.get('encrypted', True)),
    }, None


def _parse_channel_params(data, cluster):
    """Validate and normalize the bulk channel creation payload.

    Returns (params, secrets, error). ``secrets`` holds values that must be
    used for the API call but never persisted (SRT passphrase override).
    """
    required_top = ['channel_count', 'first_ch', 'provider_name', 'channel_number', 'category_id']
    for field in required_top:
        if field not in data:
            return None, None, f'Missing required field: {field}'

    try:
        params = {
            'cluster': cluster['key'],
            'mode': cluster['mode'],
            'channel_count': int(data['channel_count']),
            'first_ch': int(data['first_ch']),
            'provider_name': str(data['provider_name']).strip(),
            'channel_number': int(data['channel_number']),
            'category_id': str(data['category_id']).strip(),
            'sleep_time': float(data.get('sleep_time', 1)),
            'dry_run': bool(data.get('dry_run', False)),
        }
    except (TypeError, ValueError) as exc:
        return None, None, f'Invalid field type: {exc}'

    if params['channel_count'] < 1:
        return None, None, 'channel_count must be >= 1'
    if not params['provider_name']:
        return None, None, 'provider_name must not be empty'

    secrets = {}
    try:
        if cluster['mode'] == 'multicast':
            params['locations'], error = _parse_multicast_params(data)
        else:
            params['srt'], error = _parse_srt_params(data, params['channel_count'])
            if not error:
                override = str((data.get('srt') or {}).get('passphrase') or '').strip()
                secrets['passphrase'] = override or SRT_PASSPHRASE
                secrets['passphrase_source'] = (
                    'override' if override else ('env' if SRT_PASSPHRASE else 'missing')
                )
    except (TypeError, ValueError) as exc:
        return None, None, f'Invalid field type: {exc}'
    if error:
        return None, None, error

    return params, secrets, None


def _run_channel_job(job_id, params, secrets):
    """Background worker: creates channels sequentially, persisting progress to disk."""
    cluster = CLUSTERS[params['cluster']]
    passphrase = secrets.get('passphrase')

    job = _read_job(job_id)
    job['status'] = 'running'
    job['started_at'] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, job)

    for i in range(params['channel_count']):
        body = _build_channel_body(params, i, passphrase)
        # Only the redacted body is ever written to disk.
        entry = {'index': i, 'request_body': _redact_body(body)}

        if params['dry_run']:
            entry['status'] = 'skipped'
            entry['dry_run'] = True
        else:
            try:
                resp = cluster['session'].post(f"{cluster['url']}/channel/", json=body)
                entry['status_code'] = resp.status_code
                try:
                    entry['response'] = resp.json() if resp.content else None
                except ValueError:
                    entry['response'] = resp.text[:2000]
                entry['status'] = 'ok' if resp.ok else 'error'
            except requests.RequestException as exc:
                entry['status'] = 'error'
                entry['error'] = str(exc)

        job = _read_job(job_id)
        job['results'].append(entry)
        job['progress'] = i + 1
        _write_job(job_id, job)

        is_last = i == params['channel_count'] - 1
        if not params['dry_run'] and params['sleep_time'] > 0 and not is_last:
            time.sleep(params['sleep_time'])

    job = _read_job(job_id)
    job['status'] = 'completed'
    job['finished_at'] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, job)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@txcore_bp.route('/status', methods=['GET'])
def get_status():
    """Return whether the required TXCore env vars are configured, per cluster."""
    _, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    return jsonify(_config_status())


@txcore_bp.route('/category', methods=['POST'])
def create_category():
    """Create a TXCore category on the selected cluster. Returns the new category_id."""
    _, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    data = request.get_json(force=True) or {}
    cluster, error = _resolve_cluster(data)
    if error:
        return jsonify({'error': error}), 400

    name = data.get('name')
    desc = data.get('desc', name)
    dry_run = bool(data.get('dry_run', False))

    if not name:
        return jsonify({'error': 'Missing required field: name'}), 400

    request_body = {'name': name, 'desc': desc}

    if dry_run:
        return jsonify({'dry_run': True, 'cluster': cluster['key'], 'request_body': request_body})

    if not _cluster_configured(cluster):
        return _not_configured(cluster)

    try:
        resp = cluster['session'].post(f"{cluster['url']}/category/", json=request_body)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({'error': 'TXCore API request failed', 'details': str(exc)}), 502

    payload = resp.json()
    return jsonify({'category_id': payload.get('_id'), 'cluster': cluster['key'], 'response': payload})


@txcore_bp.route('/categories', methods=['GET'])
def list_categories():
    """List existing TXCore categories (id, name, desc) on the selected cluster."""
    _, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    cluster, error = _resolve_cluster()
    if error:
        return jsonify({'error': error}), 400

    if not _cluster_configured(cluster):
        return _not_configured(cluster)

    try:
        resp = cluster['session'].get(
            f"{cluster['url']}/categories",
            params={'sort': 'name', 'order': 'asc', 'limit': 500},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({'error': 'TXCore API request failed', 'details': str(exc)}), 502

    payload = resp.json()
    if isinstance(payload, dict):
        payload['cluster'] = cluster['key']
    return jsonify(payload)


@txcore_bp.route('/channels/preview', methods=['POST'])
def preview_channels():
    """Return the request bodies that would be sent, without calling the API."""
    _, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    data = request.get_json(force=True) or {}
    cluster, error = _resolve_cluster(data)
    if error:
        return jsonify({'error': error}), 400

    params, secrets, error = _parse_channel_params(data, cluster)
    if error:
        return jsonify({'error': error}), 400

    bodies = [
        _redact_body(_build_channel_body(params, i, secrets.get('passphrase')))
        for i in range(params['channel_count'])
    ]
    result = {
        'cluster': cluster['key'],
        'mode': cluster['mode'],
        'count': len(bodies),
        'request_bodies': bodies,
    }
    if cluster['mode'] == 'srt':
        result['passphrase_source'] = secrets.get('passphrase_source')
    return jsonify(result)


@txcore_bp.route('/channels', methods=['POST'])
def create_channels():
    """Start a background job that creates channels sequentially in TXCore."""
    username, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    data = request.get_json(force=True) or {}
    cluster, error = _resolve_cluster(data)
    if error:
        return jsonify({'error': error}), 400

    params, secrets, error = _parse_channel_params(data, cluster)
    if error:
        return jsonify({'error': error}), 400

    if not params['dry_run']:
        if not _cluster_configured(cluster):
            return _not_configured(cluster)
        if (cluster['mode'] == 'srt' and params['srt']['encrypted']
                and not secrets.get('passphrase')):
            return jsonify({
                'error': 'SRT passphrase is not configured on the server '
                         '(INTERNALSRTPASSPHRASE) and no override was supplied',
            }), 400

    job_id = uuid.uuid4().hex
    job = {
        'job_id': job_id,
        'cluster': cluster['key'],
        'mode': cluster['mode'],
        'status': 'queued',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'created_by': username,
        'params': params,  # never contains the passphrase
        'progress': 0,
        'total': params['channel_count'],
        'results': [],
    }
    _write_job(job_id, job)

    thread = threading.Thread(
        target=_run_channel_job, args=(job_id, params, secrets), daemon=True
    )
    thread.start()

    return jsonify({'job_id': job_id, 'cluster': cluster['key'], 'status': 'queued'}), 202


@txcore_bp.route('/channels/job/<job_id>', methods=['GET'])
def get_job(job_id):
    """Poll the status/progress of a channel creation job."""
    _, role = _get_user_and_role()
    if role not in ALLOWED_ROLES:
        return jsonify({'error': 'Permission denied — admin or engineer role required'}), 403

    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        return jsonify({'error': 'Job not found'}), 404

    job = _read_job(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify(job)
