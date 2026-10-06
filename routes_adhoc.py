"""
SO-Toolbox - ADHOC Manager
Blueprint that serves the ADHOC Manager page (adhoc_manager.html).

It combines two data sources:

  1. READ-ONLY: the local Dataminer snapshot maintained by routes_bte.py.
     Only Supplier Dynamic resources whose name starts with ADC_NAME_PREFIX
     (default "ADC_CH") are considered. ADHOC Manager never writes to Dataminer.
  2. READ/WRITE: its own file /opt/web/data/adhocs.json, keyed by Dataminer
     resource id:

        {
          "version": 1,
          "updated_at": "<UTC ISO>",
          "adhocs": {
            "<resource id>": {
              "name": "ADC_CH01",
              "competition": "...", "provider": "...",
              "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
              "jira_url": "https://...",
              "updated_at": "<UTC ISO>", "updated_by": "<username>"
            }
          }
        }

Availability rule (inferred, never stored): a channel is "in use" while its
end date is today (UTC) or later; otherwise (past end date, or no end date)
it is "available".

Environment variables (.env):
    ADC_LIST_URL_PRI   Base SRT URL for the PRIMARY listener endpoint,
                       e.g. srt://adhoc-pri.example.com
    ADC_LIST_URL_SEC   Base SRT URL for the SECONDARY listener endpoint.
                       Both accept an optional "{port}" placeholder; without
                       it, ":<port>" is appended. A missing scheme means srt://.
    ADC_NAME_PREFIX    Resource name prefix (default "ADC_CH").
    ADC_PROP_MODE / ADC_PROP_PORT / ADC_PROP_PORT_BACKUP /
    ADC_PROP_ADDR_MAIN / ADC_PROP_ADDR_BACKUP
                       Optional comma-separated lists of Dataminer property
                       names (case/punctuation-insensitive) that override the
                       built-in guesses used to read the connection mode,
                       port and pull addresses from each resource.

Security notes
    * Passphrases are NEVER part of the channel list. They are only returned
      by POST /channels/<id>/passphrase (admin/engineer), with Cache-Control:
      no-store, and every reveal is written to the audit file first (fail
      closed: no audit entry, no reveal).
    * Passphrase-like query parameters / credentials embedded in pull URLs are
      masked in every response.
    * Jira URLs are limited to http(s) so they are safe to render as links.
    * Write endpoints require a JSON content type (simple CSRF mitigation).
    * Audit entries store usernames (personal data under GDPR) for security
      accountability only; do not reuse them for other purposes.
"""

import fcntl
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

import routes_bte as bte

log = logging.getLogger('so-toolbox.adhoc')

adhoc_bp = Blueprint('adhoc', __name__, url_prefix='/api/adhoc')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NAME_PREFIX = bte._env('ADC_NAME_PREFIX') or 'ADC_CH'
LIST_URL_PRI = bte._env('ADC_LIST_URL_PRI')
LIST_URL_SEC = bte._env('ADC_LIST_URL_SEC')

ADHOC_FILE = os.path.join(bte.DATA_DIR, 'adhocs.json')
ADHOC_LOCK_FILE = ADHOC_FILE + '.lock'
AUDIT_FILE = os.path.join(bte.DATA_DIR, 'adhoc_audit.jsonl')

TEXT_FIELDS = ('competition', 'provider')
DATE_FIELDS = ('start_date', 'end_date')
FIELDS = TEXT_FIELDS + DATE_FIELDS + ('jira_url',)

MAX_TEXT = 200
MAX_URL = 500
MAX_BULK = 500

_ID_RE = re.compile(r'[0-9a-fA-F-]{8,64}')
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _norm(key):
    """Lower-case and strip punctuation so 'SRT Mode' == 'srt_mode'."""
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def _candidates(env_name, defaults):
    raw = bte._env(env_name)
    names = raw.split(',') if raw else defaults
    return [n for n in (_norm(x) for x in names) if n]


# Best-effort guesses for the Dataminer property names. Confirm them against
# the "Info" panel of a real ADC_CH channel and override through .env if needed.
PROP_MODE = _candidates('ADC_PROP_MODE', ['SRT Mode', 'Mode', 'Connection Mode', 'SRT Connection Mode'])
PROP_PORT = _candidates('ADC_PROP_PORT', ['Port', 'SRT Port', 'Listener Port', 'Main Port'])
PROP_PORT_BACKUP = _candidates('ADC_PROP_PORT_BACKUP', ['Backup Port', 'Secondary Port', 'Port Backup'])
PROP_ADDR_MAIN = _candidates('ADC_PROP_ADDR_MAIN', ['Main Address', 'Main URL', 'Primary Address',
                                                    'Primary URL', 'Main Source', 'Address', 'URL', 'Source URL'])
PROP_ADDR_BACKUP = _candidates('ADC_PROP_ADDR_BACKUP', ['Backup Address', 'Backup URL', 'Secondary Address',
                                                        'Secondary URL', 'Backup Source'])

# ---------------------------------------------------------------------------
# adhocs.json storage (file lock + atomic replace)
# ---------------------------------------------------------------------------

_PROC_LOCK = threading.RLock()


class StoreError(Exception):
    """adhocs.json exists but cannot be read — never overwrite it blindly."""


def _empty_store():
    return {'version': 1, 'updated_at': None, 'adhocs': {}}


def _read_store():
    try:
        with open(ADHOC_FILE, 'r') as f:
            store = json.load(f)
    except FileNotFoundError:
        return _empty_store()
    except (OSError, ValueError) as exc:
        log.error('ADHOC Manager: cannot read %s: %s', ADHOC_FILE, exc)
        raise StoreError('adhocs.json is unreadable — check the file and its permissions') from exc
    if not isinstance(store, dict) or not isinstance(store.get('adhocs'), dict):
        raise StoreError('adhocs.json has an unexpected structure')
    return store


def _write_store(store):
    tmp_path = ADHOC_FILE + '.tmp'
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(store, f, indent=1)
    os.replace(tmp_path, ADHOC_FILE)
    try:
        os.chmod(ADHOC_FILE, 0o600)
    except OSError:
        pass


@contextmanager
def _locked():
    """Serialise read-modify-write cycles across threads and gunicorn workers."""
    with _PROC_LOCK:
        lock_fd = open(ADHOC_LOCK_FILE, 'a+')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                lock_fd.close()


def _audit(event, username, **fields):
    """Append one JSON line to the audit file. Returns False if it could not be written."""
    entry = {'ts': bte._now_iso(), 'event': event, 'user': username}
    entry.update(fields)
    try:
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError:
        log.exception('ADHOC Manager: failed to write the audit entry (%s)', event)
        return False
    log.info('ADHOC Manager audit: %s', entry)
    return True


# ---------------------------------------------------------------------------
# Dataminer item helpers (read-only)
# ---------------------------------------------------------------------------

def _text(value):
    if value is None or isinstance(value, (dict, list)):
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _flat_props(item):
    """normalised key -> (original key, value). 'properties' win over 'capabilities'
    unless their value is empty."""
    flat = {}
    for section in ('capabilities', 'properties'):
        block = item.get(section)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            norm = _norm(key)
            if norm not in flat or _text(value):
                flat[norm] = (key, value)
    return flat


def _first(flat, candidates):
    for cand in candidates:
        entry = flat.get(cand)
        if entry and _text(entry[1]):
            return _text(entry[1])
    return ''


def _passphrases(flat):
    """[(original key, value)] for every non-empty passphrase-like property."""
    return sorted((orig, _text(val)) for norm, (orig, val) in flat.items()
                  if 'passphrase' in norm and _text(val))


def _classify(mode_value):
    value = (mode_value or '').lower()
    if 'listen' in value:
        return 'listener'
    if 'pull' in value or 'caller' in value:
        return 'pull'
    return 'unknown'  # flagged, never guessed


def _listener_url(template, port):
    if not template or not re.fullmatch(r'\d{1,5}', port or '') or not 0 < int(port) <= 65535:
        return ''
    url = template.strip()
    if '://' not in url:
        url = 'srt://' + url
    if '{port}' in url:
        return url.replace('{port}', port)
    return f'{url.rstrip("/")}:{port}'


_URL_SECRET_RE = re.compile(r'((?:passphrase|password|secret|token|api[-_]?key)=)[^&\s]+', re.IGNORECASE)
_URL_USERINFO_RE = re.compile(r'(://[^/:@\s]+:)[^@/\s]+@')


def _mask_url_secrets(url):
    url = _URL_SECRET_RE.sub(lambda m: m.group(1) + bte.REDACTED, url or '')
    return _URL_USERINFO_RE.sub(lambda m: m.group(1) + bte.REDACTED + '@', url)


def _adc_items(snapshot):
    """Raw Supplier Dynamic items whose name starts with the ADHOC prefix."""
    pool = ((snapshot or {}).get('pools') or {}).get('resources') or {}
    prefix = NAME_PREFIX.upper()
    return [i for i in pool.get('items') or [] if str(i.get('name') or '').upper().startswith(prefix)]


def _parse_date(value):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _today():
    return datetime.now(timezone.utc).date()


def _adhoc_view(meta, today):
    """Stored ADHOC fields plus the inferred availability."""
    end = _parse_date(meta.get('end_date'))
    in_use = bool(end and end >= today)
    adhoc = {f: meta.get(f, '') for f in FIELDS}
    adhoc['updated_at'] = meta.get('updated_at')
    adhoc['updated_by'] = meta.get('updated_by')
    return {
        'adhoc': adhoc,
        'status': 'in_use' if in_use else 'available',
        'days_left': (end - today).days if in_use else None,
    }


def _build_channel(item, meta, today):
    flat = _flat_props(item)
    kind = _classify(_first(flat, PROP_MODE))
    port = _first(flat, PROP_PORT)
    if kind == 'listener':
        main = _listener_url(LIST_URL_PRI, port)
        backup = _listener_url(LIST_URL_SEC, _first(flat, PROP_PORT_BACKUP) or port)
    else:
        main = _mask_url_secrets(_first(flat, PROP_ADDR_MAIN))
        backup = _mask_url_secrets(_first(flat, PROP_ADDR_BACKUP))

    redacted = bte._redact_item(item)
    channel = {
        'id': item.get('id'),
        'name': item.get('name'),
        'kind': kind,
        'supplier': bte._item_type(item),
        'edge': bte._item_edge(item),
        'dm_mode': item.get('mode'),
        'port': port or None,
        'address_main': main or None,
        'address_backup': backup or None,
        'passphrase_count': len(_passphrases(flat)),
        'properties': redacted.get('properties') or {},
        'capabilities': redacted.get('capabilities') or {},
    }
    channel.update(_adhoc_view(meta, today))
    return channel


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _json_body():
    data = request.get_json(silent=True)  # requires a JSON content type
    return data if isinstance(data, dict) else None


def _clean_fields(raw):
    """Validate the submitted subset of editable fields -> (clean, error)."""
    if not isinstance(raw, dict) or not raw:
        return None, 'No fields supplied'
    unknown = [k for k in raw if k not in FIELDS]
    if unknown:
        return None, f'Unknown field(s): {", ".join(map(str, unknown))}'
    clean = {}
    for key, value in raw.items():
        if value is None:
            value = ''
        if not isinstance(value, str):
            return None, f'"{key}" must be a string'
        value = _CTRL_RE.sub('', value).strip()
        if key in TEXT_FIELDS and len(value) > MAX_TEXT:
            return None, f'"{key}" is longer than {MAX_TEXT} characters'
        if key in DATE_FIELDS and value and _parse_date(value) is None:
            return None, f'"{key}" must be a valid date (YYYY-MM-DD)'
        if key == 'jira_url' and value:
            parsed = urlparse(value)
            if len(value) > MAX_URL:
                return None, f'"jira_url" is longer than {MAX_URL} characters'
            if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
                return None, '"jira_url" must be a full http(s) URL'
        clean[key] = value
    return clean, None


def _range_error(meta):
    start, end = _parse_date(meta.get('start_date')), _parse_date(meta.get('end_date'))
    if start and end and end < start:
        return 'end date cannot be earlier than start date'
    return None


def _apply_update(items_by_id, clean, username):
    """Merge ``clean`` into every record in one locked, all-or-nothing write.
    Returns (staged records, error)."""
    with _locked():
        store = _read_store()
        records = store['adhocs']
        now = bte._now_iso()
        staged = {}
        for rid, item in items_by_id.items():
            record = dict(records.get(rid) or {})
            record.update(clean)
            problem = _range_error(record)
            if problem:
                return None, f"{item.get('name')}: {problem}"
            record['name'] = item.get('name')
            record['updated_at'] = now
            record['updated_by'] = username
            staged[rid] = record
        records.update(staged)
        store['updated_at'] = now
        _write_store(store)
    return staged, None


def _items_by_id():
    return {i.get('id'): i for i in _adc_items(bte._current_snapshot())}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@adhoc_bp.route('/channels', methods=['GET'])
def channels_list():
    """ADHOC channels from the Dataminer snapshot merged with adhocs.json."""
    if bte._get_role() not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    snapshot = bte._current_snapshot()
    if not snapshot or not (snapshot.get('pools') or {}).get('resources'):
        return jsonify({'available': False, 'channels': [], 'counts': {}, 'warnings': [],
                        'hint': 'No Dataminer snapshot yet — refresh it from BTE or wait for the hourly refresh'})
    try:
        store = _read_store()
    except StoreError as exc:
        return jsonify({'error': str(exc)}), 500

    today = _today()
    channels = [_build_channel(i, store['adhocs'].get(i.get('id')) or {}, today) for i in _adc_items(snapshot)]
    channels.sort(key=lambda c: str(c['name'] or '').lower())

    warnings = []
    listeners = [c for c in channels if c['kind'] == 'listener']
    if listeners and not LIST_URL_PRI:
        warnings.append('ADC_LIST_URL_PRI is not set — primary listener addresses cannot be built.')
    if listeners and not LIST_URL_SEC:
        warnings.append('ADC_LIST_URL_SEC is not set — secondary listener addresses cannot be built.')
    no_port = sum(1 for c in listeners if not c['port'])
    if no_port:
        warnings.append(f'{no_port} listener channel(s) have no usable port in Dataminer.')
    unknown = sum(1 for c in channels if c['kind'] == 'unknown')
    if unknown:
        warnings.append(f'{unknown} channel(s) have an undetermined connection mode. Check their Info panel '
                        'and set ADC_PROP_MODE in .env if the Dataminer property name differs.')

    meta = bte._snapshot_meta(snapshot)
    return jsonify({
        'available': True,
        'prefix': NAME_PREFIX,
        'today': today.isoformat(),
        'snapshot': {'fetched_at': meta.get('fetched_at'), 'age_seconds': meta.get('age_seconds'),
                     'errors': meta.get('errors') or {}},
        'counts': {
            'total': len(channels),
            'available': sum(1 for c in channels if c['status'] == 'available'),
            'in_use': sum(1 for c in channels if c['status'] == 'in_use'),
        },
        'warnings': warnings,
        'channels': channels,
    })


@adhoc_bp.route('/channels/<resource_id>', methods=['PATCH'])
def channel_update(resource_id):
    """Update ADHOC fields of one channel. Body: any subset of the editable fields."""
    username, role = bte._get_user_and_role()
    if role not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    items = _items_by_id()
    if not _ID_RE.fullmatch(resource_id) or resource_id not in items:
        return jsonify({'error': 'ADHOC channel not found'}), 404
    data = _json_body()
    if data is None:
        return jsonify({'error': 'A JSON object body is required'}), 400
    clean, error = _clean_fields(data)
    if error:
        return jsonify({'error': error}), 400
    try:
        staged, error = _apply_update({resource_id: items[resource_id]}, clean, username)
    except (StoreError, OSError) as exc:
        log.exception('ADHOC Manager: update failed')
        return jsonify({'error': str(exc)}), 500
    if error:
        return jsonify({'error': error}), 400
    _audit('meta_updated', username, resource_ids=[resource_id], fields=sorted(clean))
    return jsonify(_adhoc_view(staged[resource_id], _today()))


@adhoc_bp.route('/channels/bulk', methods=['POST'])
def channels_bulk():
    """Apply the same field values to many channels (all-or-nothing).

    Body: {"ids": ["..."], "fields": {"competition": "...", "end_date": ""}}
    Only the fields present are changed; an empty string clears the field.
    """
    username, role = bte._get_user_and_role()
    if role not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    data = _json_body()
    if data is None:
        return jsonify({'error': 'A JSON object body is required'}), 400
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': '"ids" must be a non-empty list'}), 400
    ids = list(dict.fromkeys(str(i) for i in ids))
    if len(ids) > MAX_BULK:
        return jsonify({'error': f'At most {MAX_BULK} channels per request'}), 400
    items = _items_by_id()
    unknown = [i for i in ids if i not in items]
    if unknown:
        return jsonify({'error': f'{len(unknown)} channel(s) not found in the ADHOC list'}), 404
    clean, error = _clean_fields(data.get('fields'))
    if error:
        return jsonify({'error': error}), 400
    try:
        staged, error = _apply_update({i: items[i] for i in ids}, clean, username)
    except (StoreError, OSError) as exc:
        log.exception('ADHOC Manager: bulk update failed')
        return jsonify({'error': str(exc)}), 500
    if error:
        return jsonify({'error': error}), 400
    _audit('meta_bulk_updated', username, resource_ids=ids, fields=sorted(clean))
    today = _today()
    return jsonify({'updated': len(staged), 'channels': {rid: _adhoc_view(rec, today) for rid, rec in staged.items()}})


@adhoc_bp.route('/channels/<resource_id>/passphrase', methods=['POST'])
def channel_passphrase(resource_id):
    """Reveal the passphrase(s) of one channel. Audited; fails closed if the audit write fails."""
    username, role = bte._get_user_and_role()
    if role not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    items = _items_by_id()
    if not _ID_RE.fullmatch(resource_id) or resource_id not in items:
        return jsonify({'error': 'ADHOC channel not found'}), 404
    if _json_body() is None:
        return jsonify({'error': 'A JSON object body is required'}), 400
    item = items[resource_id]
    entries = _passphrases(_flat_props(item))
    if not entries:
        return jsonify({'error': 'This channel has no passphrase'}), 404
    if not _audit('passphrase_revealed', username, resource_id=resource_id, resource_name=item.get('name')):
        return jsonify({'error': 'Audit log unavailable — passphrase not revealed'}), 500
    response = jsonify({'passphrases': [{'label': k, 'value': v} for k, v in entries]})
    response.headers['Cache-Control'] = 'no-store'
    return response
