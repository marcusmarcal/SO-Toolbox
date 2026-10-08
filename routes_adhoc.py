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

Dataminer inputs
    Each channel carries two properties, "Input Main" and "Input Backup", whose
    value is  "<srt url>|<Listener|Pull>|<state>", for example:

        Input Main    srt://1.1.1.1:1111|Pull|Resumed
        Input Backup  srt://:3842|Listener|Resumed

    * Pull:     the URL is used as assigned in Dataminer.
    * Listener: the host is empty in Dataminer; the address is completed with
                ADC_LIST_URL_PRI (main) / ADC_LIST_URL_SEC (backup) plus the
                port found in the Dataminer value.

Availability rule (inferred, never stored): a channel is "in use" while its
end date is today (UTC) or later; otherwise (past end date, or no end date)
it is "available".

Environment variables (.env):
    ADC_LIST_URL_PRI   Listener host/base URL for the MAIN input,
                       e.g. srt://adhoc-pri.example.com
    ADC_LIST_URL_SEC   Listener host/base URL for the BACKUP input.
                       Both accept an optional "{port}" placeholder; without
                       it, ":<port>" is appended. A missing scheme means srt://.
    ADC_NAME_PREFIX    Resource name prefix (default "ADC_CH").
    ADC_PROP_ADDR_MAIN / ADC_PROP_ADDR_BACKUP
                       Optional comma-separated Dataminer property names that
                       override "Input Main" / "Input Backup".

Security notes
    * Passphrases are NEVER part of the channel list. They are only returned by
      POST /channels/<id>/passphrase (single reveal) and POST /channels/share
      (copy for Outlook/Jira). Both require admin/engineer, send
      Cache-Control: no-store and are written to the audit file first
      (fail closed: no audit entry, no secret).
    * Every edit is audited with the username and the old/new values; the audit
      entry is written before the change is applied (fail closed).
    * Passphrase-like query parameters / credentials embedded in pull URLs are
      masked in every list/detail response.
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    """Lower-case and strip punctuation so 'Input Main' == 'input_main'."""
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def _candidates(env_name, defaults):
    raw = bte._env(env_name)
    names = raw.split(',') if raw else defaults
    return [n for n in (_norm(x) for x in names) if n]


PROP_ADDR_MAIN = _candidates('ADC_PROP_ADDR_MAIN', ['Input Main'])
PROP_ADDR_BACKUP = _candidates('ADC_PROP_ADDR_BACKUP', ['Input Backup'])

# (name, Dataminer property candidates, listener base URL, env variable name)
_INPUTS = (
    ('main', PROP_ADDR_MAIN, lambda: LIST_URL_PRI, 'ADC_LIST_URL_PRI'),
    ('backup', PROP_ADDR_BACKUP, lambda: LIST_URL_SEC, 'ADC_LIST_URL_SEC'),
)

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
    log.info('ADHOC Manager audit: %s', event)
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


def _classify(mode_text):
    value = (mode_text or '').lower()
    if 'listen' in value:
        return 'listener'
    if 'pull' in value or 'caller' in value:
        return 'pull'
    return 'unknown'  # flagged, never guessed


def _parse_input(value):
    """'srt://host:port|Listener|Resumed' -> {url, port, mode, state}."""
    parts = [p.strip() for p in _text(value).split('|')]
    url = parts[0] if parts else ''
    port = ''
    try:
        parsed_port = urlsplit(url).port
        port = str(parsed_port) if parsed_port else ''
    except ValueError:
        pass
    return {
        'url': url,
        'port': port,
        'mode': _classify(parts[1]) if len(parts) > 1 else 'unknown',
        'state': parts[2] if len(parts) > 2 else '',
    }


def _listener_url(template, port):
    if not template or not re.fullmatch(r'\d{1,5}', port or '') or not 0 < int(port) <= 65535:
        return ''
    url = template.strip()
    if '://' not in url:
        url = 'srt://' + url
    if '{port}' in url:
        return url.replace('{port}', port)
    return f'{url.rstrip("/")}:{port}'


def _split_url_passphrase(url):
    """Remove an embedded ?passphrase=... from a URL -> (clean url, passphrase)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, ''
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    embedded = next((v for k, v in pairs if k.lower() == 'passphrase'), '')
    if not embedded:
        return url, ''
    rest = [(k, v) for k, v in pairs if k.lower() != 'passphrase']
    return urlunsplit(parts._replace(query=urlencode(rest, safe=':/,'))), embedded


_URL_SECRET_RE = re.compile(r'((?:passphrase|password|secret|token|api[-_]?key)=)[^&|\s]+', re.IGNORECASE)
_URL_USERINFO_RE = re.compile(r'(://[^/:@\s]+:)[^@/\s]+@')


def _mask_url_secrets(text):
    text = _URL_SECRET_RE.sub(lambda m: m.group(1) + bte.REDACTED, text or '')
    return _URL_USERINFO_RE.sub(lambda m: m.group(1) + bte.REDACTED + '@', text)


def _mask_block(block):
    """Mask secrets embedded in string values (key-based redaction is done by routes_bte)."""
    if not isinstance(block, dict):
        return {}
    return {k: _mask_url_secrets(v) if isinstance(v, str) else v for k, v in block.items()}


def _secret_entries(flat):
    """[(label, passphrase)] from passphrase properties and from ?passphrase= in the input URLs."""
    entries = sorted((orig, _text(val)) for norm, (orig, val) in flat.items()
                     if 'passphrase' in norm and _text(val))
    for label, cands in (('Input Main URL', PROP_ADDR_MAIN), ('Input Backup URL', PROP_ADDR_BACKUP)):
        _, embedded = _split_url_passphrase(_parse_input(_first(flat, cands))['url'])
        if embedded:
            entries.append((label, embedded))
    return entries


def _passphrase_for(entries, which):
    """Passphrase for 'main' or 'backup'. None when it cannot be decided safely."""
    values = {v for _, v in entries}
    if len(values) == 1:
        return next(iter(values))
    hints = {'main': ('main', 'primary', 'pri'), 'backup': ('backup', 'secondary', 'sec')}[which]
    matches = {v for k, v in entries if any(h in _norm(k) for h in hints)}
    return next(iter(matches)) if len(matches) == 1 else None


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
    parsed, addresses = {}, {}
    for which, cands, template, _env_name in _INPUTS:
        p = _parse_input(_first(flat, cands))
        parsed[which] = p
        if not p['url']:
            addresses[which] = ''
        elif p['mode'] == 'listener':
            addresses[which] = _listener_url(template(), p['port'])
        else:
            addresses[which] = _mask_url_secrets(p['url'])

    modes = {p['mode'] for p in parsed.values() if p['url']}
    kind = next(iter(modes)) if len(modes) == 1 else ('mixed' if modes else 'unknown')

    redacted = bte._redact_item(item)
    channel = {
        'id': item.get('id'),
        'name': item.get('name'),
        'kind': kind,
        'supplier': bte._item_type(item),
        'edge': bte._item_edge(item),
        'dm_mode': item.get('mode'),
        'mode_main': parsed['main']['url'] and parsed['main']['mode'],
        'mode_backup': parsed['backup']['url'] and parsed['backup']['mode'],
        'port_main': parsed['main']['port'] or None,
        'port_backup': parsed['backup']['port'] or None,
        'state_main': parsed['main']['state'] or None,
        'state_backup': parsed['backup']['state'] or None,
        'address_main': addresses['main'] or None,
        'address_backup': addresses['backup'] or None,
        'passphrase_count': len({v for _, v in _secret_entries(flat)}),
        'properties': _mask_block(redacted.get('properties')),
        'capabilities': _mask_block(redacted.get('capabilities')),
    }
    channel.update(_adhoc_view(meta, today))
    return channel


def _share_entry(item):
    """Unmasked data for the 'copy for Outlook/Jira' feature of one channel."""
    flat = _flat_props(item)
    entries = _secret_entries(flat)
    out = {'id': item.get('id'), 'name': item.get('name'), 'main': None, 'backup': None, 'warnings': []}
    kinds = set()
    for which, cands, template, env_name in _INPUTS:
        p = _parse_input(_first(flat, cands))
        if not p['url']:
            continue
        kinds.add(p['mode'])
        embedded = ''
        if p['mode'] == 'listener':
            url = _listener_url(template(), p['port'])
            if not url:
                out['warnings'].append(f'{which}: listener address unavailable ({env_name} or port missing)')
                continue
        else:
            url, embedded = _split_url_passphrase(p['url'])
        secret = ''
        if entries:
            secret = _passphrase_for(entries, which)
            if secret is None:
                out['warnings'].append(f'{which}: more than one passphrase found, none could be matched — left out')
                secret = ''
        out[which] = {'url': url, 'passphrase': secret or embedded}
    out['kind'] = next(iter(kinds)) if len(kinds) == 1 else ('mixed' if kinds else 'unknown')
    return out


# ---------------------------------------------------------------------------
# Validation and updates
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
            try:
                parts = urlsplit(value)
            except ValueError:
                return None, '"jira_url" is not a valid URL'
            if len(value) > MAX_URL:
                return None, f'"jira_url" is longer than {MAX_URL} characters'
            if parts.scheme.lower() not in ('http', 'https') or not parts.netloc:
                return None, '"jira_url" must be a full http(s) URL'
        clean[key] = value
    return clean, None


def _range_error(meta):
    start, end = _parse_date(meta.get('start_date')), _parse_date(meta.get('end_date'))
    if start and end and end < start:
        return 'end date cannot be earlier than start date'
    return None


def _apply_update(items_by_id, clean, username, event):
    """Merge ``clean`` into every record: one locked, all-or-nothing operation.

    The audit entry (who, what, old -> new) is written BEFORE the file, and the
    change is abandoned if it cannot be recorded. Returns (records, changes, error).
    """
    with _locked():
        store = _read_store()
        records = store['adhocs']
        now = bte._now_iso()
        views, staged, changes = {}, {}, {}
        for rid, item in items_by_id.items():
            old = records.get(rid) or {}
            record = dict(old)
            record.update(clean)
            problem = _range_error(record)
            if problem:
                return None, None, f"{item.get('name')}: {problem}"
            diff = {f: [old.get(f, ''), record[f]] for f in clean if old.get(f, '') != record[f]}
            if diff:
                record.update({'name': item.get('name'), 'updated_at': now, 'updated_by': username})
                staged[rid] = record
                changes[rid] = {'name': item.get('name'), 'fields': diff}
            views[rid] = record
        if staged:
            if not _audit(event, username, changes=changes):
                return None, None, 'Audit log unavailable — nothing was changed'
            records.update(staged)
            store['updated_at'] = now
            _write_store(store)
    return views, changes, None


def _items_by_id():
    return {i.get('id'): i for i in _adc_items(bte._current_snapshot())}


def _ids_from_body(data):
    """Validated, de-duplicated id list -> (ids, error response)."""
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return None, (jsonify({'error': '"ids" must be a non-empty list'}), 400)
    ids = list(dict.fromkeys(str(i) for i in ids))
    if len(ids) > MAX_BULK:
        return None, (jsonify({'error': f'At most {MAX_BULK} channels per request'}), 400)
    return ids, None


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@adhoc_bp.route('/channels', methods=['GET'])
def channels_list():
    """ADHOC channels from the Dataminer snapshot merged with adhocs.json."""
    username, role = bte._get_user_and_role()
    if role not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    snapshot = bte._current_snapshot()
    if not snapshot or not (snapshot.get('pools') or {}).get('resources'):
        return jsonify({'available': False, 'channels': [], 'counts': {}, 'warnings': [], 'me': username,
                        'hint': 'No Dataminer snapshot yet — refresh it from BTE or wait for the hourly refresh'})
    try:
        store = _read_store()
    except StoreError as exc:
        return jsonify({'error': str(exc)}), 500

    today = _today()
    channels = [_build_channel(i, store['adhocs'].get(i.get('id')) or {}, today) for i in _adc_items(snapshot)]
    channels.sort(key=lambda c: str(c['name'] or '').lower())

    warnings = []
    if any(c['mode_main'] == 'listener' for c in channels) and not LIST_URL_PRI:
        warnings.append('ADC_LIST_URL_PRI is not set — main listener addresses cannot be built.')
    if any(c['mode_backup'] == 'listener' for c in channels) and not LIST_URL_SEC:
        warnings.append('ADC_LIST_URL_SEC is not set — backup listener addresses cannot be built.')
    no_input = sum(1 for c in channels if not c['mode_main'] and not c['mode_backup'])
    if no_input:
        warnings.append(f'{no_input} channel(s) have no "Input Main" / "Input Backup" value — check their Details '
                        'and set ADC_PROP_ADDR_MAIN / ADC_PROP_ADDR_BACKUP if the property names differ.')
    undetermined = sum(1 for c in channels if c['kind'] in ('unknown', 'mixed') and (c['mode_main'] or c['mode_backup']))
    if undetermined:
        warnings.append(f'{undetermined} channel(s) have an input whose mode is not Listener/Pull, or inputs with '
                        'different modes — their addresses are shown as assigned in Dataminer.')

    meta = bte._snapshot_meta(snapshot)
    return jsonify({
        'available': True,
        'me': username,
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
        views, changes, error = _apply_update({resource_id: items[resource_id]}, clean, username, 'meta_updated')
    except (StoreError, OSError) as exc:
        log.exception('ADHOC Manager: update failed')
        return jsonify({'error': str(exc)}), 500
    if error:
        return jsonify({'error': error}), 400
    result = _adhoc_view(views[resource_id], _today())
    result['changed'] = len(changes)
    return jsonify(result)


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
    ids, error_response = _ids_from_body(data)
    if error_response:
        return error_response
    items = _items_by_id()
    if any(i not in items for i in ids):
        return jsonify({'error': 'One or more channels were not found in the ADHOC list'}), 404
    clean, error = _clean_fields(data.get('fields'))
    if error:
        return jsonify({'error': error}), 400
    try:
        views, changes, error = _apply_update({i: items[i] for i in ids}, clean, username, 'meta_bulk_updated')
    except (StoreError, OSError) as exc:
        log.exception('ADHOC Manager: bulk update failed')
        return jsonify({'error': str(exc)}), 500
    if error:
        return jsonify({'error': error}), 400
    today = _today()
    return jsonify({'updated': len(views), 'changed': len(changes),
                    'channels': {rid: _adhoc_view(rec, today) for rid, rec in views.items()}})


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
    seen, passphrases = set(), []
    for label, value in _secret_entries(_flat_props(item)):
        if value not in seen:
            seen.add(value)
            passphrases.append({'label': label, 'value': value})
    if not passphrases:
        return jsonify({'error': 'This channel has no passphrase'}), 404
    if not _audit('passphrase_revealed', username, resource_id=resource_id, resource_name=item.get('name')):
        return jsonify({'error': 'Audit log unavailable — passphrase not revealed'}), 500
    return _no_store(jsonify({'passphrases': passphrases}))


@adhoc_bp.route('/channels/share', methods=['POST'])
def channels_share():
    """Complete addresses and passphrases of the selected channels, for the
    'copy for Outlook/Jira' button. Audited; fails closed like the single reveal.

    Body: {"ids": ["..."]}
    """
    username, role = bte._get_user_and_role()
    if role not in bte.ALLOWED_ROLES:
        return bte._forbidden()
    data = _json_body()
    if data is None:
        return jsonify({'error': 'A JSON object body is required'}), 400
    ids, error_response = _ids_from_body(data)
    if error_response:
        return error_response
    items = _items_by_id()
    if any(i not in items for i in ids):
        return jsonify({'error': 'One or more channels were not found in the ADHOC list'}), 404
    entries = [_share_entry(items[i]) for i in ids]
    names = [items[i].get('name') for i in ids]
    if not _audit('share_copied', username, resource_ids=ids, resource_names=names):
        return jsonify({'error': 'Audit log unavailable — nothing was copied'}), 500
    return _no_store(jsonify({'channels': entries}))
