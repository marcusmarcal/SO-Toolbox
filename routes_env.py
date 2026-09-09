"""
routes_env.py — .env Manager Blueprint
SO-Toolbox

Admin-only management of the server-side ``.env`` file that drives the
toolbox (tool registry, presets, credentials). Consumed by the
"Environment" tab in ``so-toolbox-admin.html``.

Security model
--------------
- Every endpoint requires an authenticated session with role ``admin``
  (engineers are *not* allowed — the file contains credentials).
- Secret-looking keys (password, passphrase, token, bearer, auth, key…)
  are masked in list responses; the real value is only returned by the
  explicit ``/env/keys/<key>/reveal`` endpoint.
- Every write goes through ``_write_env``: a timestamped backup is taken
  first, the new content is written to a temp file with mode 0600 and
  atomically moved into place. The last ``MAX_BACKUPS`` backups are kept
  next to ``.env`` as ``.env.bak-YYYYmmdd-HHMMSS`` (already blocked by the
  nginx ``^/\\.env`` rule and git-ignored).
- Audit lines are printed to stdout (journald) with user + action + key,
  never the value.

Reload semantics
----------------
Some Blueprints read ``.env`` from disk on every request (``live``), others
copy it into ``os.environ`` at import time (``restart``). The
``KNOWN_KEYS`` registry below tells the UI which is which so it can show a
"restart proxy" hint after saving.

Endpoints (prefix ``/env``)
---------------------------
GET    /env                          parsed file (secrets masked) + metadata
GET    /env/raw                      full raw content (secrets included)
PUT    /env/raw                      replace full content (validated)
GET    /env/keys/<key>/reveal        real value of one key
POST   /env/keys                     add a variable
PUT    /env/keys/<key>               update a variable's value
PUT    /env/keys/<key>/rename        rename a variable
DELETE /env/keys/<key>               delete a variable
GET    /env/backups                  list backups
POST   /env/backups                  create a manual backup
GET    /env/backups/<name>/diff      unified diff backup → current (masked)
POST   /env/backups/<name>/restore   restore a backup (current is backed up)
DELETE /env/backups/<name>           delete a backup
"""

import difflib
import os
import re
import shutil
import threading
import time
from functools import wraps

from flask import Blueprint, jsonify, request

from routes_auth import _get_session, _token_from_request

env_bp = Blueprint('env', __name__, url_prefix='/env')

# ── Config ────────────────────────────────────────────────
_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
ENV_PATH       = os.path.join(_BASE_DIR, '.env')
BACKUP_PREFIX  = '.env.bak-'
MAX_BACKUPS    = 15
MAX_FILE_BYTES = 256 * 1024          # refuse absurd payloads on PUT /raw
MASK           = '••••••••'

KEY_RE     = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
BACKUP_RE  = re.compile(r'^\.env\.bak-\d{8}-\d{6}(-\d+)?$')
SECRET_RE  = re.compile(
    r'(PASS|PASSWORD|PASSPHRASE|SECRET|TOKEN|BEARER|CREDENTIAL|AUTH|_KEY$|^KEY_|APIKEY)',
    re.IGNORECASE,
)

# Serialises read-modify-write cycles inside this process.
_lock = threading.Lock()

# ── Known keys registry ───────────────────────────────────
# (regex, consumer, description, reload)
#   reload: 'live'    → read from disk per request, effective immediately
#           'restart' → loaded into os.environ at import, needs proxy restart
#           'mixed'   → some consumers live, some need restart
#           'none'    → reference data, not read by any Blueprint
KNOWN_KEYS = [
    (r'^APP_TITLE$',            'index.html',
     'Application title shown in the sidebar and browser tab', 'live'),
    (r'^APP_VERSION$',          'index.html',
     'Legacy version string (the UI now reads the version from CHANGELOG.md)', 'live'),
    (r'^PROXY_URL$',            'index.html',
     'Optional proxy base URL override for the frontends', 'live'),
    (r'^TOOL_\d+$',             'index.html',
     'Tool registry entry — file.html|Name|Description|icon|Category|BADGE', 'live'),
    (r'^SRT_SERVER_\d+$',       'SRT URI Builder · Video Analyser · TXCore Manager',
     'SRT server preset — IP|Label', 'mixed'),
    (r'^SRT_LOCAL_\d+$',        'SRT URI Builder · Ingest Analyzer · Video Analyser',
     'Local interface preset — IP|Label', 'mixed'),
    (r'^SRT_PASSPHRASE$',       'SRT tools · TXCore Manager (fallback)',
     'Default SRT passphrase pre-filled in the SRT tools', 'mixed'),
    (r'^ADMIN_PASSWORD$',       'proxy.py · routes_gop.py',
     'Legacy X-Admin-Password guarding destructive MTR/GOP actions', 'live'),
    (r'^PRFAUTH$',              'id3as_routes.py',
     'id3as API bearer token', 'live'),
    (r'^ID3AS_HOST_(IX|EQ)$',   'id3as_routes.py',
     'id3as datacentre hostname used to build GUI deep-links', 'live'),
    (r'^BEARER_TOKEN_(STB|MAIN)$', 'routes_txcore.py',
     'TXCore API bearer token for the cluster', 'restart'),
    (r'^APIURL(STB|MAIN)$',     'routes_txcore.py',
     'TXCore API base URL for the cluster (no trailing slash)', 'restart'),
    (r'^(AVE|LMK|YER)GEOID$',   'routes_txcore.py',
     'TXCore geofence id for the site', 'restart'),
    (r'^INTERNALSRTPASSPHRASE$', 'routes_txcore.py',
     'SRT passphrase applied to MAIN cluster sources', 'restart'),
    (r'^TXEDGE_[A-Z0-9]+_ID$',  'reference',
     'TXEdge node id (reference data — not read by the proxy)', 'none'),
    (r'^[A-Z0-9]+_(INCOMING|OUTGOING)_(SRT|UDP|RTP)_IP$', 'reference',
     'Site interface address (reference data — not read by the proxy)', 'none'),
]
_KNOWN = [(re.compile(rx), c, d, r) for rx, c, d, r in KNOWN_KEYS]


def _key_info(key):
    for rx, consumer, desc, reload in _KNOWN:
        if rx.match(key):
            return {'consumer': consumer, 'description': desc, 'reload': reload, 'known': True}
    return {'consumer': '', 'description': 'Not referenced by any Blueprint', 'reload': 'unknown',
            'known': False}


def _is_secret(key):
    return bool(SECRET_RE.search(key))


# ══════════════════════════════════════════════════════════
# AUTH — admin only (stricter than routes_auth.require_admin_role)
# ══════════════════════════════════════════════════════════

def require_admin_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        session = _get_session(_token_from_request())
        if not session:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        if session.get('role') != 'admin':
            return jsonify({'ok': False, 'error': 'Forbidden — admin role required'}), 403
        request.session = session
        return f(*args, **kwargs)
    return decorated


def _audit(action, key=None, extra=''):
    user = getattr(request, 'session', {}).get('username', '?')
    msg = f'[env] user={user} action={action}'
    if key:
        msg += f' key={key}'
    if extra:
        msg += f' {extra}'
    print(msg, flush=True)


# ══════════════════════════════════════════════════════════
# FILE I/O
# ══════════════════════════════════════════════════════════

def _read_raw():
    """Return (content, mtime). Missing file → ('', 0)."""
    if not os.path.exists(ENV_PATH):
        return '', 0
    with open(ENV_PATH, 'r', encoding='utf-8', errors='replace', newline='') as f:
        content = f.read()
    return content, os.path.getmtime(ENV_PATH)


def _newline_of(content):
    return '\r\n' if '\r\n' in content else '\n'


def _split_lines(content):
    """Split preserving nothing but the text (line endings are re-applied on write)."""
    if not content:
        return []
    lines = content.replace('\r\n', '\n').split('\n')
    # A trailing newline yields an empty last element — drop it (re-added on write)
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def _join_lines(lines, newline):
    return newline.join(lines) + (newline if lines else '')


def _list_backups():
    out = []
    try:
        for name in os.listdir(_BASE_DIR):
            if not BACKUP_RE.match(name):
                continue
            p = os.path.join(_BASE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({'name': name, 'size': st.st_size, 'mtime': st.st_mtime})
    except OSError:
        pass
    out.sort(key=lambda b: b['name'], reverse=True)
    return out


def _prune_backups():
    for b in _list_backups()[MAX_BACKUPS:]:
        try:
            os.remove(os.path.join(_BASE_DIR, b['name']))
        except OSError:
            pass


def _backup_now():
    """Copy the current .env to a timestamped backup (mode 0600). Returns the name or None."""
    if not os.path.exists(ENV_PATH):
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = f'{BACKUP_PREFIX}{stamp}'
    dest = os.path.join(_BASE_DIR, name)
    n = 1
    while os.path.exists(dest):          # several writes within one second
        name = f'{BACKUP_PREFIX}{stamp}-{n}'
        dest = os.path.join(_BASE_DIR, name)
        n += 1
    shutil.copy2(ENV_PATH, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    _prune_backups()
    return name


def _write_env(content):
    """Backup, then atomically replace .env with ``content`` (mode 0600)."""
    backup = _backup_now()
    tmp = ENV_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ENV_PATH)
    return backup


# ══════════════════════════════════════════════════════════
# PARSING
# ══════════════════════════════════════════════════════════

_VAR_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')


def _parse_line(text):
    """Classify one line → dict(type, key, value). Value is kept verbatim
    (including any surrounding quotes) so a round-trip never alters it."""
    stripped = text.strip()
    if stripped == '':
        return {'type': 'blank'}
    if stripped.startswith('#'):
        return {'type': 'comment'}
    m = _VAR_RE.match(text)
    if m:
        return {'type': 'var', 'key': m.group(1), 'value': m.group(2).strip()}
    return {'type': 'invalid'}


def _parse(lines):
    """Return the list of line objects sent to the UI (secrets masked)."""
    out, seen, duplicates = [], set(), []
    for i, text in enumerate(lines):
        p = _parse_line(text)
        obj = {'n': i + 1, 'type': p['type'], 'text': text}
        if p['type'] == 'var':
            key, value = p['key'], p['value']
            secret = _is_secret(key)
            obj.update({
                'key': key,
                'secret': secret,
                'has_value': bool(value),
                'value': MASK if (secret and value) else value,
                'length': len(value),
                'duplicate': key in seen,
            })
            obj.update(_key_info(key))
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        out.append(obj)
    return out, duplicates


def _find_key(lines, key):
    """Index of the first line defining ``key`` or -1."""
    for i, text in enumerate(lines):
        p = _parse_line(text)
        if p['type'] == 'var' and p['key'] == key:
            return i
    return -1


def _validate_key(key):
    if not key or not KEY_RE.match(key):
        return 'Key must match [A-Za-z_][A-Za-z0-9_]* (letters, digits, underscore)'
    return None


def _validate_value(value):
    if value is None:
        return 'value required'
    if not isinstance(value, str):
        return 'value must be a string'
    if '\n' in value or '\r' in value:
        return 'value must be a single line'
    if len(value) > 8192:
        return 'value too long (max 8192 characters)'
    return None


def _validate_content(content):
    """Reject raw content that would confuse the .env parsers used by the
    toolbox: every non-blank, non-comment line must be KEY=VALUE."""
    problems = []
    for i, text in enumerate(_split_lines(content)):
        if _parse_line(text)['type'] == 'invalid':
            problems.append({'line': i + 1, 'text': text[:120]})
    return problems


def _meta(content, mtime):
    return {
        'file': os.path.basename(ENV_PATH),
        'exists': os.path.exists(ENV_PATH),
        'size': len(content.encode('utf-8')),
        'mtime': mtime,
        'newline': 'CRLF' if _newline_of(content) == '\r\n' else 'LF',
        'backups': len(_list_backups()),
        'max_backups': MAX_BACKUPS,
    }


# ══════════════════════════════════════════════════════════
# ROUTES — read
# ══════════════════════════════════════════════════════════

@env_bp.route('', methods=['GET'])
@require_admin_only
def get_env():
    with _lock:
        content, mtime = _read_raw()
    lines, duplicates = _parse(_split_lines(content))
    vars_ = [l for l in lines if l['type'] == 'var']
    return jsonify({
        'ok': True,
        'meta': _meta(content, mtime),
        'lines': lines,
        'stats': {
            'vars': len(vars_),
            'secrets': sum(1 for v in vars_ if v['secret']),
            'empty': sum(1 for v in vars_ if not v['has_value']),
            'unknown': sum(1 for v in vars_ if not v['known']),
            'comments': sum(1 for l in lines if l['type'] == 'comment'),
            'invalid': sum(1 for l in lines if l['type'] == 'invalid'),
        },
        'duplicates': sorted(set(duplicates)),
    })


@env_bp.route('/keys/<key>/reveal', methods=['GET'])
@require_admin_only
def reveal_key(key):
    err = _validate_key(key)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    with _lock:
        content, _ = _read_raw()
    lines = _split_lines(content)
    idx = _find_key(lines, key)
    if idx < 0:
        return jsonify({'ok': False, 'error': 'Key not found'}), 404
    _audit('reveal', key)
    return jsonify({'ok': True, 'key': key, 'value': _parse_line(lines[idx])['value']})


@env_bp.route('/raw', methods=['GET'])
@require_admin_only
def get_raw():
    with _lock:
        content, mtime = _read_raw()
    _audit('raw-read')
    return jsonify({'ok': True, 'content': content, 'meta': _meta(content, mtime)})


# ══════════════════════════════════════════════════════════
# ROUTES — write
# ══════════════════════════════════════════════════════════

@env_bp.route('/raw', methods=['PUT'])
@require_admin_only
def put_raw():
    data = request.get_json(silent=True) or {}
    content = data.get('content')
    if not isinstance(content, str):
        return jsonify({'ok': False, 'error': 'content (string) required'}), 400
    if len(content.encode('utf-8')) > MAX_FILE_BYTES:
        return jsonify({'ok': False, 'error': f'content exceeds {MAX_FILE_BYTES} bytes'}), 413

    problems = _validate_content(content)
    if problems and not data.get('allow_invalid'):
        return jsonify({'ok': False, 'error': 'Invalid lines — every non-comment line must be KEY=VALUE',
                        'problems': problems}), 400

    with _lock:
        current, mtime = _read_raw()
        # Optimistic concurrency: refuse if someone else changed the file since it was loaded
        expected = data.get('mtime')
        if expected is not None and not data.get('force'):
            try:
                if abs(float(expected) - float(mtime)) > 0.001:
                    return jsonify({'ok': False, 'error': 'File changed on disk since it was loaded',
                                    'code': 'conflict', 'mtime': mtime}), 409
            except (TypeError, ValueError):
                pass
        if content == current:
            return jsonify({'ok': True, 'unchanged': True, 'meta': _meta(current, mtime)})
        # Normalise: keep the file's newline style and ensure a trailing newline
        nl = _newline_of(current) if current else _newline_of(content)
        normalised = _join_lines(_split_lines(content), nl)
        backup = _write_env(normalised)
        new_content, new_mtime = _read_raw()
    _audit('raw-write', extra=f'backup={backup}')
    return jsonify({'ok': True, 'backup': backup, 'meta': _meta(new_content, new_mtime)})


@env_bp.route('/keys', methods=['POST'])
@require_admin_only
def add_key():
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', '')).strip()
    value = data.get('value', '')
    after = str(data.get('after', '') or '').strip()      # insert after this key ('' → end)
    comment = str(data.get('comment', '') or '').strip()  # optional single-line comment above

    err = _validate_key(key) or _validate_value(value)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    if '\n' in comment or '\r' in comment or len(comment) > 200:
        return jsonify({'ok': False, 'error': 'comment must be a single line (max 200 chars)'}), 400
    if after and _validate_key(after):
        return jsonify({'ok': False, 'error': 'invalid "after" key'}), 400

    with _lock:
        content, mtime = _read_raw()
        nl = _newline_of(content)
        lines = _split_lines(content)
        if _find_key(lines, key) >= 0:
            return jsonify({'ok': False, 'error': f'Key "{key}" already exists — use PUT to update'}), 409

        new_lines = []
        if comment:
            new_lines.append('# ' + comment.lstrip('#').strip())
        new_lines.append(f'{key}={value}')

        if after:
            pos = _find_key(lines, after)
            if pos < 0:
                return jsonify({'ok': False, 'error': f'"after" key "{after}" not found'}), 404
            lines[pos + 1:pos + 1] = new_lines
        else:
            if lines and lines[-1].strip() != '':
                lines.append('')
            lines.extend(new_lines)

        backup = _write_env(_join_lines(lines, nl))
        os.environ[key] = value
    _audit('add', key, f'backup={backup}')
    return jsonify({'ok': True, 'key': key, 'backup': backup, **_key_info(key)}), 201


@env_bp.route('/keys/<key>', methods=['PUT'])
@require_admin_only
def update_key(key):
    data = request.get_json(silent=True) or {}
    value = data.get('value')
    err = _validate_key(key) or _validate_value(value)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    with _lock:
        content, _ = _read_raw()
        nl = _newline_of(content)
        lines = _split_lines(content)
        idx = _find_key(lines, key)
        if idx < 0:
            return jsonify({'ok': False, 'error': 'Key not found — use POST to add'}), 404
        current = _parse_line(lines[idx])['value']
        if current == value:
            return jsonify({'ok': True, 'key': key, 'unchanged': True, **_key_info(key)})
        # Preserve an "export " prefix if the line had one
        prefix = 'export ' if lines[idx].lstrip().startswith('export ') else ''
        lines[idx] = f'{prefix}{key}={value}'
        backup = _write_env(_join_lines(lines, nl))
        os.environ[key] = value
    _audit('update', key, f'backup={backup}')
    return jsonify({'ok': True, 'key': key, 'backup': backup, **_key_info(key)})


@env_bp.route('/keys/<key>/rename', methods=['PUT'])
@require_admin_only
def rename_key(key):
    data = request.get_json(silent=True) or {}
    new_key = str(data.get('new_key', '')).strip()
    err = _validate_key(key) or _validate_key(new_key)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    if new_key == key:
        return jsonify({'ok': True, 'key': key, 'unchanged': True})

    with _lock:
        content, _ = _read_raw()
        nl = _newline_of(content)
        lines = _split_lines(content)
        idx = _find_key(lines, key)
        if idx < 0:
            return jsonify({'ok': False, 'error': 'Key not found'}), 404
        if _find_key(lines, new_key) >= 0:
            return jsonify({'ok': False, 'error': f'Key "{new_key}" already exists'}), 409
        value = _parse_line(lines[idx])['value']
        prefix = 'export ' if lines[idx].lstrip().startswith('export ') else ''
        lines[idx] = f'{prefix}{new_key}={value}'
        backup = _write_env(_join_lines(lines, nl))
        os.environ.pop(key, None)
        os.environ[new_key] = value
    _audit('rename', key, f'new_key={new_key} backup={backup}')
    return jsonify({'ok': True, 'key': new_key, 'old_key': key, 'backup': backup, **_key_info(new_key)})


@env_bp.route('/keys/<key>', methods=['DELETE'])
@require_admin_only
def delete_key(key):
    err = _validate_key(key)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    remove_all = request.args.get('all') == '1'

    with _lock:
        content, _ = _read_raw()
        nl = _newline_of(content)
        lines = _split_lines(content)
        idx = _find_key(lines, key)
        if idx < 0:
            return jsonify({'ok': False, 'error': 'Key not found'}), 404
        removed = 0
        if remove_all:
            kept = []
            for text in lines:
                p = _parse_line(text)
                if p['type'] == 'var' and p['key'] == key:
                    removed += 1
                    continue
                kept.append(text)
            lines = kept
        else:
            del lines[idx]
            removed = 1
        backup = _write_env(_join_lines(lines, nl))
        os.environ.pop(key, None)
    _audit('delete', key, f'removed={removed} backup={backup}')
    return jsonify({'ok': True, 'key': key, 'removed': removed, 'backup': backup})


# ══════════════════════════════════════════════════════════
# ROUTES — backups
# ══════════════════════════════════════════════════════════

def _safe_backup_path(name):
    """Resolve a backup name to a path, refusing anything that is not one of ours."""
    if not BACKUP_RE.match(name or ''):
        return None
    p = os.path.join(_BASE_DIR, name)
    if os.path.dirname(os.path.abspath(p)) != _BASE_DIR or not os.path.isfile(p):
        return None
    return p


@env_bp.route('/backups', methods=['GET'])
@require_admin_only
def list_backups():
    return jsonify({'ok': True, 'backups': _list_backups(), 'max_backups': MAX_BACKUPS})


@env_bp.route('/backups', methods=['POST'])
@require_admin_only
def create_backup():
    with _lock:
        name = _backup_now()
    if not name:
        return jsonify({'ok': False, 'error': '.env not found'}), 404
    _audit('backup', extra=f'name={name}')
    return jsonify({'ok': True, 'name': name, 'backups': _list_backups()}), 201


def _mask_line(text):
    p = _parse_line(text)
    if p['type'] == 'var' and _is_secret(p['key']) and p['value']:
        return f"{p['key']}={MASK}"
    return text


@env_bp.route('/backups/<name>/diff', methods=['GET'])
@require_admin_only
def diff_backup(name):
    p = _safe_backup_path(name)
    if not p:
        return jsonify({'ok': False, 'error': 'Backup not found'}), 404
    with _lock:
        current, _ = _read_raw()
        with open(p, 'r', encoding='utf-8', errors='replace', newline='') as f:
            old = f.read()
    a = [_mask_line(l) for l in _split_lines(old)]
    b = [_mask_line(l) for l in _split_lines(current)]
    diff = list(difflib.unified_diff(a, b, fromfile=name, tofile='.env (current)', lineterm='', n=2))
    return jsonify({'ok': True, 'name': name, 'diff': diff, 'identical': not diff})


@env_bp.route('/backups/<name>/restore', methods=['POST'])
@require_admin_only
def restore_backup(name):
    p = _safe_backup_path(name)
    if not p:
        return jsonify({'ok': False, 'error': 'Backup not found'}), 404
    with _lock:
        with open(p, 'r', encoding='utf-8', errors='replace', newline='') as f:
            content = f.read()
        backup = _write_env(content)   # current state is preserved as a new backup
        new_content, new_mtime = _read_raw()
    _audit('restore', extra=f'from={name} backup={backup}')
    return jsonify({'ok': True, 'restored': name, 'backup': backup,
                    'meta': _meta(new_content, new_mtime),
                    'note': 'Restart the proxy so Blueprints that cache values pick up the restored file.'})


@env_bp.route('/backups/<name>', methods=['DELETE'])
@require_admin_only
def delete_backup(name):
    p = _safe_backup_path(name)
    if not p:
        return jsonify({'ok': False, 'error': 'Backup not found'}), 404
    with _lock:
        os.remove(p)
    _audit('backup-delete', extra=f'name={name}')
    return jsonify({'ok': True, 'deleted': name, 'backups': _list_backups()})


# ══════════════════════════════════════════════════════════
# REGISTER
# ══════════════════════════════════════════════════════════

def register_routes(app):
    app.register_blueprint(env_bp)
