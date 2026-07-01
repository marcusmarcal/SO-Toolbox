# ════════════════════════════════════════════════════════════════════════════
#  ROTATION LOGIC
# ════════════════════════════════════════════════════════════════════════════
import os
import re
import json
import uuid
import datetime

from flask import Blueprint, request, jsonify
from routes_auth import require_auth, require_admin_role

# ── Blueprint ─────────────────────────────────────────────────────────────
rota_bp = Blueprint('rota', __name__)

# ── Config ────────────────────────────────────────────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ROTA_DIR   = os.path.join(_BASE_DIR, 'rota')
LEAVE_FILE = os.path.join(ROTA_DIR, 'leave_requests.json')
USERS_FILE = os.path.join(_BASE_DIR, 'users.json')

# ── Data helpers ──────────────────────────────────────────────────────────
def _load_json(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[JSON ERROR] {path}: {e}")
        return {}

def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

# ── Role helpers ──────────────────────────────────────────────────────────
STAFF_ROLES = {'engineer', 'specialist'}
VALID_LEAVE_TYPES = {'Annual Leave', 'Parental Leave', 'Marital Leave'}

VALID_TRANSITIONS = {
    'Pending':            {'Confirmed', 'Rejected', 'Cancelled'},
    'Confirmed':          {'Withdrawal Pending'},
    'Withdrawal Pending': {'Withdrawn', 'Withdrawal Rejected', 'Cancelled'},
}

def _display_name_from_email(email: str) -> str:
    local = email.split('@')[0]
    parts = re.split(r'[.\-_]', local)
    if not parts:
        return email
    first = parts[0].capitalize()
    if len(parts) >= 2:
        return f"{first} {parts[1][0].upper()}"
    return first

def _get_rota_role(session: dict) -> str:
    role = session.get('role', '')
    if role == 'admin':
        return 'management'
    if role in STAFF_ROLES:
        return 'staff'
    return 'guest'

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'

# ── Rotation constants ────────────────────────────────────────────────────
from datetime import date, timedelta

ANCHOR_MONDAY = date(2026, 3, 2)

SPECIALIST_ROTATION = [
    "OFF","OFF","0700-1800","0700-1800","OFF","OFF","OFF",
    "0700-1800","0700-1800","OFF","OFF","0700-1800","0700-1800","0700-1800",
    "OFF","OFF","0900-2000","0900-2000","0900-2000","0900-2000","0900-2000",
    "OFF","OFF","1300-0000","1300-0000","OFF","OFF","OFF",
    "1300-0000","1300-0000","OFF","OFF","1300-0000","1300-0000","1300-0000",
    "1500-0200","1500-0200","OFF","OFF","OFF","1500-0200","1500-0200",
    "OFF","OFF","1500-0200","1500-0200","1500-0200","OFF","OFF",
    "OFF","OFF","2100-0700","2100-0700","2100-0700","2100-0700","2100-0700",
    "OFF","OFF","2100-0700","2100-0700","OFF","OFF","OFF",
    "OFF","OFF","0700-1800","0700-1800","OFF","OFF","OFF",
    "0700-1800","0700-1800","OFF","OFF","0700-1800","0700-1800","0700-1800",
    "OFF","OFF","0900-2000","0900-2000","0900-2000","0900-2000","0900-2000",
    "OFF","OFF","1300-0000","1300-0000","OFF","OFF","OFF",
    "1300-0000","1300-0000","OFF","OFF","1300-0000","1300-0000","1300-0000",
    "1500-0200","1500-0200","OFF","OFF","OFF","1500-0200","1500-0200",
    "OFF","OFF","1500-0200","1500-0200","1500-0200","OFF","OFF",
    "2100-0700","2100-0700","OFF","OFF","2100-0700","2100-0700","2100-0700",
    "2100-0700","2100-0700","OFF","OFF","OFF","OFF","OFF",
]

ENGINEERING_ROTATION = [
    "OFF","0900-1800","0900-1800","0900-1800","0900-1800","OFF","OFF",
    "0900-1800","1000-2000","OFF","OFF","1000-2000","1000-2000","1000-2000",
    "1000-2000","OFF","1000-2000","1000-2000","OFF","OFF","OFF",
]

SPECIALIST_OFFSETS = {
    "Sabina": 35, "Sergio": 119, "Tiago O": 77,
    "Vitor":  63, "Fernando": 21, "Marc":    7,
    "Gabriel":49, "Mario":    91, "Isaac":   105,
}
ENGINEERING_OFFSETS = {"Hugo": 0, "Goncalo": 14, "Nuno": 7}

MANAGEMENT_SHIFTS = {
    "Joao R":  "0930-1800",
    "Marcus":  "0900-1730",
    "Joao L":  "0800-1630",
    "Tiago C": "0900-1730",
}

# Maps each user's email to their exact display name as used in the rota
# tables above (MANAGEMENT_SHIFTS / ENGINEERING_OFFSETS / SPECIALIST_OFFSETS).
# Required because _display_name_from_email() always appends a last-initial,
# which only matches rota names for collision cases (e.g. "Tiago C", "Tiago O").
EMAIL_TO_ROTA_NAME = {
    "joao.rato@statsperform.com":   "Joao R",
    "marcus.marcal@statsperform.com":   "Marcus",
    "joao.lopes@statsperform.com":   "Joao L",
    "tiago.carvalho@statsperform.com":  "Tiago C",
    "hugo.carvalho@statsperform.com":     "Hugo",
    "goncalo.paiva@statsperform.com":  "Goncalo",
    "nuno.carvalho@statsperform.com":     "Nuno",
    "sabina.barros@statsperform.com":   "Sabina",
    "sergio.silva@statsperform.com":   "Sergio",
    "tiago.oliveira@statsperform.com":  "Tiago O",
    "vitor.cassama@statsperform.com":    "Vitor",
    "fernando.carvalho@statsperform.com": "Fernando",
    "marcmadeira.ribeiro@statsperform.com":     "Marc",
    "gabriel.ribeiro@statsperform.com":  "Gabriel",
    "mario.branco@statsperform.com":    "Mario",
    "isaac.santiago@statsperform.com":    "Isaac",
}

def _rota_display_name(email: str) -> str:
    """Returns the exact rota table name for a known team member,
    falling back to the generic email-derived name otherwise."""
    return EMAIL_TO_ROTA_NAME.get(email, _display_name_from_email(email))

def _rota_name_to_email_map() -> dict:
    """Reverse of EMAIL_TO_ROTA_NAME, for attributing draft al_toggle
    publishes to a real user account where possible."""
    return {v: k for k, v in EMAIL_TO_ROTA_NAME.items()}

def _email_for_rota_name(name: str):
    return _rota_name_to_email_map().get(name)

PUBLIC_HOLIDAYS = {
    date(2026,1,1),  date(2026,2,17), date(2026,4,3),
    date(2026,4,5),  date(2026,4,25), date(2026,5,1),
    date(2026,5,12), date(2026,6,4),  date(2026,6,10),
    date(2026,8,15), date(2026,10,5), date(2026,11,1),
    date(2026,12,1), date(2026,12,8), date(2026,12,25),
}

PARENTAL_LEAVE_TYPES = {"Parental Leave"}
MARITAL_LEAVE_TYPES  = {"Marital Leave"}

# Statuses that show as AL_APPROVED overlay on rota
AL_APPROVED_STATUSES = {'Confirmed', 'Withdrawal Pending', 'Withdrawal Rejected'}
# Statuses that show as AL_PENDING overlay on rota
AL_PENDING_STATUSES  = {'Pending'}
# Statuses that revert to base shift (no overlay)
AL_CLEAR_STATUSES    = {'Rejected', 'Withdrawn', 'Cancelled'}

# Shift types that require coverage when a Specialist takes them off (AL).
# Specialists only — Engineering and Management excluded from coverage logic.
COVERAGE_REQUIRED_SHIFTS = {'0700-1800', '1500-0200', '2100-0700'}
# Shifts a Specialist can be on that DON'T require coverage if they go on AL —
# these are the natural source shifts for a coverage swap.
COVERAGE_FREE_SHIFTS     = {'0900-2000', '1300-0000'}

def _base_shift(name: str, d: date) -> str:
    delta = (d - ANCHOR_MONDAY).days
    if name in SPECIALIST_OFFSETS:
        idx = (SPECIALIST_OFFSETS[name] + delta) % len(SPECIALIST_ROTATION)
        return SPECIALIST_ROTATION[idx]
    if name in ENGINEERING_OFFSETS:
        idx = (ENGINEERING_OFFSETS[name] + delta) % len(ENGINEERING_ROTATION)
        return ENGINEERING_ROTATION[idx]
    if d.weekday() >= 5 or d in PUBLIC_HOLIDAYS:
        return "OFF"
    return MANAGEMENT_SHIFTS.get(name, "OFF")

def _resolve_shift(name: str, d: date, leave_map: dict, override_map: dict = None) -> str:
    # Draft / published overrides take precedence over the computed
    # rotation and leave overlay when supplied.
    if override_map is not None:
        ov = override_map.get((name, d))
        if ov is not None:
            return ov['shift']

    leave = leave_map.get((name, d))
    if not leave:
        return _base_shift(name, d)
    lt     = leave["leave_type"]
    status = leave["status"]
    base   = _base_shift(name, d)

    if status in AL_CLEAR_STATUSES:
        return base
    if lt in PARENTAL_LEAVE_TYPES:
        return "PARENTAL"
    if lt in MARITAL_LEAVE_TYPES:
        return "MARITAL"
    if status in AL_APPROVED_STATUSES:
        return "AL_APPROVED" if base == "OFF" else f"AL_APPROVED|{base}"
    if status in AL_PENDING_STATUSES:
        return "AL_PENDING" if base == "OFF" else f"AL_PENDING|{base}"
    return base

def _build_leave_map(leave_list: list) -> dict:
    lmap = {}
    for r in leave_list:
        try:
            ds = date.fromisoformat(r["date_start"])
            de = date.fromisoformat(r["date_end"])
        except (KeyError, ValueError):
            continue
        d = ds
        while d <= de:
            lmap[(r["name"], d)] = {
                "leave_type": r["leave_type"],
                "status":     r["status"],
            }
            d += timedelta(days=1)
    return lmap

def _build_override_map(overrides: list) -> dict:
    """Keys: (person, date) → the override record for that cell."""
    omap = {}
    for o in overrides:
        try:
            d = date.fromisoformat(o['date'])
        except (KeyError, ValueError):
            continue
        omap[(o['person'], d)] = o
    return omap

# ════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════

@rota_bp.route('/rota/me', methods=['GET'])
@require_auth
def rota_me():
    session   = request.session
    rota_role = _get_rota_role(session)
    username  = session['username']
    name      = _rota_display_name(username)
    role      = session.get('role', '')
    team      = 'Engineering' if role == 'engineer' else \
                'Specialists' if role == 'specialist' else \
                'Management'  if role == 'admin' else None
    return jsonify({
        'ok':        True,
        'username':  username,
        'rota_role': rota_role,
        'name':      name,
        'team':      team,
    })


@rota_bp.route('/rota/members', methods=['GET'])
@require_auth
def rota_members():
    """All users — management gets full list for on-behalf dropdown."""
    users = _load_json(USERS_FILE)
    if not isinstance(users, dict):
        return jsonify({'ok': False, 'error': 'Could not load users'}), 500
    result = {}
    for email, info in users.items():
        role = info.get('role', '')
        team = 'Engineering' if role == 'engineer' else \
               'Specialists' if role == 'specialist' else \
               'Management'  if role == 'admin' else 'Other'
        result[email] = {
            'name': _rota_display_name(email),
            'team': team,
            'role': role,
        }
    return jsonify({'ok': True, 'members': result})


@rota_bp.route('/rota/schedule', methods=['GET'])
@require_auth
def rota_schedule():
    rota_role = _get_rota_role(request.session)
    try:
        date_from = date.fromisoformat(request.args.get('from', date.today().isoformat()))
        date_to   = date.fromisoformat(request.args.get('to', (date.today() + timedelta(weeks=5)).isoformat()))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    if rota_role != 'management':
        max_to = date.today() + timedelta(weeks=5)
        if date_to > max_to:
            date_to = max_to

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    # Published rota reflects committed shift overrides from past publishes
    # (e.g. weekend toggles, coverage swaps) — NOT the live draft.
    published_overrides = _load_json(PUBLISHED_OVERRIDES_FILE)
    if not isinstance(published_overrides, list):
        published_overrides = []
    override_map = _build_override_map(published_overrides)

    leave_map = _build_leave_map(leave_list)
    today = date.today()
    days  = []
    d     = date_from

    while d <= date_to:
        day = {
            'date':              d.isoformat(),
            'weekday':           d.strftime('%A'),
            'is_today':          d == today,
            'is_weekend':        d.weekday() >= 5,
            'is_public_holiday': d in PUBLIC_HOLIDAYS,
            'shifts':            {},
        }
        for name in MANAGEMENT_SHIFTS:
            day['shifts'][name] = {'team': 'Management',  'shift': _resolve_shift(name, d, leave_map, override_map)}
        for name in ENGINEERING_OFFSETS:
            day['shifts'][name] = {'team': 'Engineering', 'shift': _resolve_shift(name, d, leave_map, override_map)}
        for name in SPECIALIST_OFFSETS:
            day['shifts'][name] = {'team': 'Specialists', 'shift': _resolve_shift(name, d, leave_map, override_map)}
        days.append(day)
        d += timedelta(days=1)

    return jsonify({'ok': True, 'days': days})


@rota_bp.route('/rota/leave', methods=['GET'])
@require_auth
def rota_leave_get():
    session    = request.session
    rota_role  = _get_rota_role(session)
    username   = session['username']
    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    if rota_role == 'management':
        return jsonify({'ok': True, 'leave': leave_list})

    # Staff: own entries + entries submitted on their behalf
    my_leave = [
        r for r in leave_list
        if r.get('username') == username or
           (r.get('on_behalf') and r.get('username') == username)
    ]
    return jsonify({'ok': True, 'leave': my_leave})


@rota_bp.route('/rota/leave', methods=['POST'])
@require_auth
def rota_leave_post():
    session   = request.session
    rota_role = _get_rota_role(session)

    if rota_role == 'guest':
        return jsonify({'ok': False, 'error': 'Not authorised'}), 403

    data       = request.get_json(silent=True) or {}
    date_start = data.get('date_start', '').strip()
    date_end   = data.get('date_end', '').strip()
    leave_type = data.get('leave_type', '').strip()
    on_behalf  = data.get('on_behalf', False)
    target     = data.get('target_username', '').strip()

    if not date_start or not date_end or not leave_type:
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400
    if leave_type not in VALID_LEAVE_TYPES:
        return jsonify({'ok': False, 'error': 'Invalid leave type'}), 400
    if date_end < date_start:
        return jsonify({'ok': False, 'error': 'End date before start date'}), 400

    # ── Next-year AL blocker ──────────────────────────────────────────────
    bypass = bool(data.get('bypass_blocker', False))
    if not bypass:
        cfg        = _load_config()
        today      = date.today()
        next_year  = today.year + 1
        try:
            mm, dd     = cfg['next_year_open_from'].split('-')
            open_date  = date(today.year, int(mm), int(dd))
        except (ValueError, KeyError):
            open_date  = date(today.year, 11, 1)

        try:
            ds = date.fromisoformat(date_start)
        except ValueError:
            ds = None

        if ds and ds.year == next_year and today < open_date:
            return jsonify({
                'ok':    False,
                'error': f'Leave requests for {next_year} open on {open_date.strftime("%d-%m-%Y")}',
                'blocked': True,
                'open_date': open_date.isoformat(),
            }), 400

    # On-behalf only allowed for management
    if on_behalf:
        if rota_role != 'management':
            return jsonify({'ok': False, 'error': 'Not authorised for on-behalf requests'}), 403
        if not target:
            return jsonify({'ok': False, 'error': 'target_username required for on-behalf'}), 400
        username = target
    else:
        username = session['username']

    name = _rota_display_name(username)

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    leave_list.append({
        'id':          str(uuid.uuid4())[:8],
        'name':        name,
        'username':    username,
        'on_behalf':   on_behalf,
        'created_by':  session['username'],
        'created_at':  _now_iso(),
        'date_start':  date_start,
        'date_end':    date_end,
        'leave_type':  leave_type,
        'status':      'Pending',
        'actioned_by': None,
        'actioned_at': None,
        'history': [
            {
                'status': 'Pending',
                'by':     session['username'],
                'at':     _now_iso(),
            }
        ],
    })
    _save_json(LEAVE_FILE, leave_list)
    return jsonify({'ok': True})


@rota_bp.route('/rota/leave/<leave_id>', methods=['PUT'])
@require_auth
def rota_leave_put(leave_id):
    session   = request.session
    rota_role = _get_rota_role(session)
    data      = request.get_json(silent=True) or {}
    new_status = data.get('status', '').strip()

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        return jsonify({'ok': False, 'error': 'No data'}), 500

    idx = next((i for i, r in enumerate(leave_list) if r.get('id') == leave_id), None)
    if idx is None:
        return jsonify({'ok': False, 'error': 'Not found'}), 404

    entry          = leave_list[idx]
    current_status = entry.get('status', '')

    # Validate transition
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        return jsonify({'ok': False, 'error': f'Cannot transition from {current_status} to {new_status}'}), 400

    # Permission checks
    # Staff can only: request withdrawal on their own Confirmed entries,
    # or cancel their own Pending / Withdrawal Pending entries
    SELF_SERVICE = {'Withdrawal Pending', 'Cancelled'}
    if rota_role != 'management':
        if new_status not in SELF_SERVICE:
            return jsonify({'ok': False, 'error': 'Not authorised'}), 403
        if entry.get('username') != session['username']:
            return jsonify({'ok': False, 'error': 'Not authorised'}), 403

    now = _now_iso()
    entry['status']      = new_status
    entry['actioned_by'] = session['username']
    entry['actioned_at'] = now
    if 'history' not in entry:
        entry['history'] = []
    entry['history'].append({
        'status': new_status,
        'by':     session['username'],
        'at':     now,
    })
    leave_list[idx] = entry
    _save_json(LEAVE_FILE, leave_list)
    return jsonify({'ok': True})


# ── Config ────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(ROTA_DIR, 'config.json')

DEFAULT_CONFIG = {
    'next_year_open_from': '11-01',   # MM-DD, day next-year AL unlocks
}

def _load_config() -> dict:
    cfg = _load_json(CONFIG_FILE)
    if not isinstance(cfg, dict):
        cfg = {}
    return {**DEFAULT_CONFIG, **cfg}


@rota_bp.route('/rota/config', methods=['GET'])
@require_auth
def rota_config_get():
    return jsonify({'ok': True, 'config': _load_config()})


@rota_bp.route('/rota/config', methods=['PUT'])
@require_auth
def rota_config_put():
    if _get_rota_role(request.session) != 'management':
        return jsonify({'ok': False, 'error': 'Not authorised'}), 403
    data = request.get_json(silent=True) or {}
    cfg  = _load_config()
    # Only allow known keys
    if 'next_year_open_from' in data:
        val = data['next_year_open_from'].strip()
        # Validate MM-DD format
        try:
            datetime.datetime.strptime(val, '%m-%d')
        except ValueError:
            return jsonify({'ok': False, 'error': 'Invalid date format, use MM-DD'}), 400
        cfg['next_year_open_from'] = val
    _save_json(CONFIG_FILE, cfg)
    return jsonify({'ok': True, 'config': cfg})


# ════════════════════════════════════════════════════════════════════════════
#  DRAFT MODE
# ════════════════════════════════════════════════════════════════════════════
DRAFT_FILE                = os.path.join(ROTA_DIR, 'draft_overrides.json')
DRAFT_LOCK_FILE           = os.path.join(ROTA_DIR, 'draft_lock.json')
PUBLISHED_OVERRIDES_FILE  = os.path.join(ROTA_DIR, 'published_overrides.json')

DRAFT_LOCK_TIMEOUT_MIN = 240  # auto-expire a stale lock after 4h of inactivity


def _load_draft_overrides() -> list:
    data = _load_json(DRAFT_FILE)
    return data if isinstance(data, list) else []


def _save_draft_overrides(overrides: list) -> None:
    _save_json(DRAFT_FILE, overrides)


def _load_draft_lock():
    data = _load_json(DRAFT_LOCK_FILE)
    if not isinstance(data, dict) or not data.get('locked_by'):
        return None
    # Expire stale locks (e.g. someone forgot to exit draft mode)
    try:
        locked_at = datetime.datetime.fromisoformat(data['locked_at'].replace('Z', '+00:00'))
        age_min   = (datetime.datetime.now(datetime.timezone.utc) - locked_at).total_seconds() / 60
        if age_min > DRAFT_LOCK_TIMEOUT_MIN:
            return None
    except Exception:
        pass
    return data


def _save_draft_lock(username: str, name: str) -> dict:
    lock = {
        'locked_by':      username,
        'locked_by_name': name,
        'locked_at':      _now_iso(),
    }
    _save_json(DRAFT_LOCK_FILE, lock)
    return lock


def _clear_draft_lock() -> None:
    _save_json(DRAFT_LOCK_FILE, {})


def _require_management():
    """Returns an error response tuple if not management, else None."""
    if _get_rota_role(request.session) != 'management':
        return jsonify({'ok': False, 'error': 'Not authorised'}), 403
    return None


def _require_draft_lock_held_by_me():
    """Returns an error response tuple if the caller doesn't currently
    hold the draft lock, else None. Call after _require_management()."""
    session = request.session
    lock    = _load_draft_lock()
    if not lock or lock.get('locked_by') != session['username']:
        return jsonify({
            'ok': False,
            'error': 'You do not currently hold the draft lock. Enter draft mode first.',
        }), 409
    return None


@rota_bp.route('/rota/draft/status', methods=['GET'])
@require_auth
def rota_draft_status():
    err = _require_management()
    if err: return err
    lock = _load_draft_lock()
    return jsonify({'ok': True, 'lock': lock})


@rota_bp.route('/rota/draft/lock', methods=['POST'])
@require_auth
def rota_draft_lock():
    err = _require_management()
    if err: return err
    session = request.session
    lock    = _load_draft_lock()
    if lock and lock.get('locked_by') != session['username']:
        return jsonify({
            'ok': False,
            'error': f"Draft is currently locked by {lock.get('locked_by_name', lock.get('locked_by'))}",
            'lock': lock,
        }), 409
    name = _rota_display_name(session['username'])
    lock = _save_draft_lock(session['username'], name)
    return jsonify({'ok': True, 'lock': lock})


@rota_bp.route('/rota/draft/unlock', methods=['POST'])
@require_auth
def rota_draft_unlock():
    """Releases the draft lock. Management can release their own lock,
    or kick another user's lock by passing {"force": true}."""
    err = _require_management()
    if err: return err
    session = request.session
    data    = request.get_json(silent=True) or {}
    force   = bool(data.get('force', False))

    lock = _load_draft_lock()
    if not lock:
        return jsonify({'ok': True})  # already unlocked

    if lock.get('locked_by') != session['username'] and not force:
        return jsonify({'ok': False, 'error': 'Draft is locked by another user'}), 403

    _clear_draft_lock()
    return jsonify({'ok': True})


@rota_bp.route('/rota/draft', methods=['GET'])
@require_auth
def rota_draft_get():
    """Returns the current draft overrides plus computed schedule
    (rotation + leave + draft overrides layered on top), so the frontend
    can render draft mode with one call."""
    err = _require_management()
    if err: return err

    try:
        date_from = date.fromisoformat(request.args.get('from', date.today().isoformat()))
        date_to   = date.fromisoformat(request.args.get('to', (date.today() + timedelta(weeks=8)).isoformat()))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    overrides = _load_draft_overrides()

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []
    leave_map = _build_leave_map(leave_list)

    # Draft view layers: published overrides first (the committed baseline),
    # then draft overrides on top (so in-progress edits always win).
    published_overrides = _load_json(PUBLISHED_OVERRIDES_FILE)
    if not isinstance(published_overrides, list):
        published_overrides = []
    override_map = _build_override_map(published_overrides)
    override_map.update(_build_override_map(overrides))

    today = date.today()
    days  = []
    d     = date_from
    while d <= date_to:
        day = {
            'date':              d.isoformat(),
            'weekday':           d.strftime('%A'),
            'is_today':          d == today,
            'is_weekend':        d.weekday() >= 5,
            'is_public_holiday': d in PUBLIC_HOLIDAYS,
            'shifts':            {},
        }
        for name in MANAGEMENT_SHIFTS:
            day['shifts'][name] = {'team': 'Management',  'shift': _resolve_shift(name, d, leave_map, override_map)}
        for name in ENGINEERING_OFFSETS:
            day['shifts'][name] = {'team': 'Engineering', 'shift': _resolve_shift(name, d, leave_map, override_map)}
        for name in SPECIALIST_OFFSETS:
            day['shifts'][name] = {'team': 'Specialists', 'shift': _resolve_shift(name, d, leave_map, override_map)}
        days.append(day)
        d += timedelta(days=1)

    return jsonify({'ok': True, 'overrides': overrides, 'days': days})


@rota_bp.route('/rota/draft/override', methods=['PUT'])
@require_auth
def rota_draft_override_put():
    """Add or replace a single draft override for one person+date.
    Autosaves immediately — called on every cell edit."""
    err = _require_management()
    if err: return err
    err = _require_draft_lock_held_by_me()
    if err: return err

    session = request.session
    data    = request.get_json(silent=True) or {}

    person  = data.get('person', '').strip()
    date_s  = data.get('date', '').strip()
    shift   = data.get('shift', '').strip()
    note    = data.get('note', '').strip() if data.get('note') else None
    ov_type = data.get('type', 'shift_change').strip()

    if not person or not date_s or not shift:
        return jsonify({'ok': False, 'error': 'person, date and shift are required'}), 400
    try:
        d = date.fromisoformat(date_s)
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    overrides = _load_draft_overrides()

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []
    leave_map = _build_leave_map(leave_list)

    published_overrides = _load_json(PUBLISHED_OVERRIDES_FILE)
    if not isinstance(published_overrides, list):
        published_overrides = []
    published_map = _build_override_map(published_overrides)

    existing = next((o for o in overrides if o['person'] == person and o['date'] == date_s), None)
    if existing:
        # Keep the ORIGINAL previous_shift so reversal always restores the
        # true pre-draft state, even across multiple edits to the same cell.
        previous_shift = existing.get('previous_shift')
        existing.update({
            'shift':      shift,
            'note':       note,
            'type':       ov_type,
            'updated_by': session['username'],
            'updated_at': _now_iso(),
        })
    else:
        previous_shift = _resolve_shift(person, d, leave_map, published_map)
        overrides.append({
            'id':             str(uuid.uuid4())[:8],
            'person':         person,
            'date':           date_s,
            'shift':          shift,
            'previous_shift': previous_shift,
            'note':           note,
            'type':           ov_type,
            'created_by':     session['username'],
            'created_at':     _now_iso(),
        })

    _save_draft_overrides(overrides)
    return jsonify({'ok': True, 'overrides': overrides})


@rota_bp.route('/rota/draft/override/<override_id>', methods=['DELETE'])
@require_auth
def rota_draft_override_delete(override_id):
    """Removes a single draft override — e.g. reversing a weekend toggle,
    or undoing a manual shift edit back to its pre-draft value."""
    err = _require_management()
    if err: return err
    err = _require_draft_lock_held_by_me()
    if err: return err

    overrides = _load_draft_overrides()
    new_overrides = [o for o in overrides if o.get('id') != override_id]
    if len(new_overrides) == len(overrides):
        return jsonify({'ok': False, 'error': 'Override not found'}), 404

    _save_draft_overrides(new_overrides)
    return jsonify({'ok': True, 'overrides': new_overrides})


@rota_bp.route('/rota/draft/discard', methods=['POST'])
@require_auth
def rota_draft_discard():
    """Releases the draft lock without touching draft data. Because every
    edit autosaves immediately, 'discard' only relinquishes the current
    session's claim on editing — it reverts to the last SAVED draft state,
    not to a fully blank draft. A true wipe is a separate, more explicit
    action (not implemented here, by design — see conversation notes)."""
    err = _require_management()
    if err: return err
    session = request.session
    lock    = _load_draft_lock()
    if lock and lock.get('locked_by') == session['username']:
        _clear_draft_lock()
    return jsonify({'ok': True})


@rota_bp.route('/rota/draft/publish', methods=['POST'])
@require_auth
def rota_draft_publish():
    """Applies all draft overrides to the published rota and clears the
    draft. shift_change / weekend_toggle / coverage_swap overrides become
    permanent published overrides. al_toggle overrides create Confirmed
    leave entries instead, so they flow through the normal leave/history
    system and are visible in Leave Approvals history."""
    err = _require_management()
    if err: return err
    err = _require_draft_lock_held_by_me()
    if err: return err

    session   = request.session
    overrides = _load_draft_overrides()

    if not overrides:
        _clear_draft_lock()
        return jsonify({'ok': True, 'published': 0, 'al_created': 0, 'shift_applied': 0})

    published_overrides = _load_json(PUBLISHED_OVERRIDES_FILE)
    if not isinstance(published_overrides, list):
        published_overrides = []

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    al_created    = 0
    shift_applied = 0

    for ov in overrides:
        if ov.get('type') == 'al_toggle':
            leave_list.append({
                'id':          str(uuid.uuid4())[:8],
                'name':        ov['person'],
                'username':    _email_for_rota_name(ov['person']) or '',
                'on_behalf':   True,
                'created_by':  session['username'],
                'created_at':  _now_iso(),
                'date_start':  ov['date'],
                'date_end':    ov['date'],
                'leave_type':  'Annual Leave',
                'status':      'Confirmed',
                'actioned_by': session['username'],
                'actioned_at': _now_iso(),
                'history': [
                    {'status': 'Pending',   'by': session['username'], 'at': _now_iso()},
                    {'status': 'Confirmed', 'by': session['username'], 'at': _now_iso()},
                ],
            })
            al_created += 1
        else:
            # shift_change / weekend_toggle / coverage_swap — replace any
            # existing published override on the same person+date.
            published_overrides = [
                p for p in published_overrides
                if not (p['person'] == ov['person'] and p['date'] == ov['date'])
            ]
            published_overrides.append({
                'id':           str(uuid.uuid4())[:8],
                'person':       ov['person'],
                'date':         ov['date'],
                'shift':        ov['shift'],
                'note':         ov.get('note'),
                'type':         ov.get('type', 'shift_change'),
                'published_by': session['username'],
                'published_at': _now_iso(),
            })
            shift_applied += 1

    _save_json(LEAVE_FILE, leave_list)
    _save_json(PUBLISHED_OVERRIDES_FILE, published_overrides)
    _save_draft_overrides([])
    _clear_draft_lock()

    return jsonify({
        'ok': True,
        'published':      len(overrides),
        'al_created':      al_created,
        'shift_applied':   shift_applied,
    })


def register_routes(app) -> None:
    app.register_blueprint(rota_bp)