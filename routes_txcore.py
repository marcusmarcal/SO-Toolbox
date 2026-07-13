"""
SO-Toolbox - TXCore Channel/Category Management
Blueprint for bulk creation of TXCore categories and channels via the
TXCore API. Ported from the original standalone create_channels.py script.

Environment variables required (.env):
    BEARER_TOKEN_STB   - Bearer token for TXCore API authentication
    APIURLSTB          - Base URL for the TXCore API (no trailing slash)
    AVEGEOID           - Geofence ID for the "ave" location
    LMKGEOID           - Geofence ID for the "lmk" location
    YERGEOID           - Geofence ID for the "yer" location
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

# TODO: adjust this import to match the auth module used by the other blueprints
from auth import _check_role, _get_session

txcore_bp = Blueprint('txcore', __name__, url_prefix='/api/txcore')

ALLOWED_ROLES = ['admin', 'engineer']

# ---------------------------------------------------------------------------
# Configuration (populated from environment variables set in .env)
# ---------------------------------------------------------------------------

API_TOKEN = os.environ.get('BEARER_TOKEN_STB')
API_URL_STB = os.environ.get('APIURLSTB')

GEOFENCE_IDS = {
    'ave': os.environ.get('AVEGEOID'),
    'lmk': os.environ.get('LMKGEOID'),
    'yer': os.environ.get('YERGEOID'),
}

API_HEADERS = {
    'Authorization': f'Bearer {API_TOKEN}',
    'Content-Type': 'application/json',
}

JOBS_DIR = '/opt/web/data/txcore_jobs'
JOBS_LOCK = threading.Lock()

os.makedirs(JOBS_DIR, exist_ok=True)

api_session = requests.Session()
api_session.headers.update(API_HEADERS)


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


def _config_status():
    """Report which required env vars are set, without leaking secret values."""
    return {
        'bearer_token_set': bool(API_TOKEN),
        'api_url_set': bool(API_URL_STB),
        'api_url': API_URL_STB or None,
        'geofence_ids_set': {k: bool(v) for k, v in GEOFENCE_IDS.items()},
    }


def _build_channel_body(params, index):
    """Build a single channel request body given the bulk params and offset index."""
    name_id = '{0:0>2}'.format(params['first_ch'] + index)
    sources = []
    for loc in ('ave', 'lmk', 'yer'):
        loc_cfg = params['locations'][loc]
        octet4 = loc_cfg['udp_ip_4_oct'] + index
        sources.append({
            'protocol': 0,
            'address': f"{loc_cfg['udp_ip_3_oct']}.{octet4}:{loc_cfg['port']}",
            'geofence': GEOFENCE_IDS[loc],
        })

    return {
        'number': params['channel_number'] + index,
        'name': f"{params['provider_name']}_CH{name_id}",
        'type': 0,
        'category': params['category_id'],
        'enabled': True,
        'sources': sources,
    }


def _parse_channel_params(data):
    """Validate and normalize the bulk channel creation payload. Returns (params, error)."""
    required_top = ['channel_count', 'first_ch', 'provider_name', 'channel_number', 'category_id']
    for field in required_top:
        if field not in data:
            return None, f'Missing required field: {field}'

    locations = data.get('locations')
    if not locations or not all(k in locations for k in ('ave', 'lmk', 'yer')):
        return None, 'Missing required field: locations (must include ave, lmk, yer)'

    for loc_key, loc_cfg in locations.items():
        for f in ('udp_ip_3_oct', 'udp_ip_4_oct', 'port'):
            if f not in loc_cfg:
                return None, f'Missing required field: locations.{loc_key}.{f}'

    try:
        params = {
            'channel_count': int(data['channel_count']),
            'first_ch': int(data['first_ch']),
            'provider_name': str(data['provider_name']),
            'channel_number': int(data['channel_number']),
            'category_id': str(data['category_id']),
            'sleep_time': float(data.get('sleep_time', 1)),
            'dry_run': bool(data.get('dry_run', False)),
            'locations': {
                loc: {
                    'udp_ip_3_oct': str(cfg['udp_ip_3_oct']),
                    'udp_ip_4_oct': int(cfg['udp_ip_4_oct']),
                    'port': int(cfg['port']),
                } for loc, cfg in locations.items()
            },
        }
    except (TypeError, ValueError) as exc:
        return None, f'Invalid field type: {exc}'

    if params['channel_count'] < 1:
        return None, 'channel_count must be >= 1'

    return params, None


def _run_channel_job(job_id, params):
    """Background worker: creates channels sequentially, persisting progress to disk."""
    job = _read_job(job_id)
    job['status'] = 'running'
    job['started_at'] = datetime.now(timezone.utc).isoformat()
    _write_job(job_id, job)

    for i in range(params['channel_count']):
        body = _build_channel_body(params, i)
        entry = {'index': i, 'request_body': body}

        if params['dry_run']:
            entry['status'] = 'skipped'
            entry['dry_run'] = True
        else:
            try:
                resp = api_session.post(f"{API_URL_STB}/channel/", json=body)
                entry['status_code'] = resp.status_code
                entry['response'] = resp.json() if resp.content else None
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
    """Return whether the required TXCore env vars are configured."""
    sess = _get_session()
    if not _check_role(sess, ALLOWED_ROLES):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(_config_status())


@txcore_bp.route('/category', methods=['POST'])
def create_category():
    """Create a TXCore category. Returns the new category_id."""
    sess = _get_session()
    if not _check_role(sess, ALLOWED_ROLES):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(force=True) or {}
    name = data.get('name')
    desc = data.get('desc', name)
    dry_run = bool(data.get('dry_run', False))

    if not name:
        return jsonify({'error': 'Missing required field: name'}), 400

    request_body = {'name': name, 'desc': desc}

    if dry_run:
        return jsonify({'dry_run': True, 'request_body': request_body})

    if not API_TOKEN or not API_URL_STB:
        return jsonify({'error': 'TXCore API is not configured on the server'}), 500

    try:
        resp = api_session.post(f"{API_URL_STB}/category/", json=request_body)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({'error': 'TXCore API request failed', 'details': str(exc)}), 502

    payload = resp.json()
    return jsonify({'category_id': payload.get('_id'), 'response': payload})


@txcore_bp.route('/channels/preview', methods=['POST'])
def preview_channels():
    """Return the request bodies that would be sent, without calling the API."""
    sess = _get_session()
    if not _check_role(sess, ALLOWED_ROLES):
        return jsonify({'error': 'Forbidden'}), 403

    params, error = _parse_channel_params(request.get_json(force=True) or {})
    if error:
        return jsonify({'error': error}), 400

    bodies = [_build_channel_body(params, i) for i in range(params['channel_count'])]
    return jsonify({'count': len(bodies), 'request_bodies': bodies})


@txcore_bp.route('/channels', methods=['POST'])
def create_channels():
    """Start a background job that creates channels sequentially in TXCore."""
    sess = _get_session()
    if not _check_role(sess, ALLOWED_ROLES):
        return jsonify({'error': 'Forbidden'}), 403

    params, error = _parse_channel_params(request.get_json(force=True) or {})
    if error:
        return jsonify({'error': error}), 400

    if not params['dry_run'] and (not API_TOKEN or not API_URL_STB):
        return jsonify({'error': 'TXCore API is not configured on the server'}), 500

    job_id = uuid.uuid4().hex
    job = {
        'job_id': job_id,
        'status': 'queued',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'created_by': sess.get('username') if sess else None,
        'params': params,
        'progress': 0,
        'total': params['channel_count'],
        'results': [],
    }
    _write_job(job_id, job)

    thread = threading.Thread(target=_run_channel_job, args=(job_id, params), daemon=True)
    thread.start()

    return jsonify({'job_id': job_id, 'status': 'queued'}), 202


@txcore_bp.route('/channels/job/<job_id>', methods=['GET'])
def get_job(job_id):
    """Poll the status/progress of a channel creation job."""
    sess = _get_session()
    if not _check_role(sess, ALLOWED_ROLES):
        return jsonify({'error': 'Forbidden'}), 403

    job = _read_job(job_id)
    if job is None:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify(job)
