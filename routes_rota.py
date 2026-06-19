# ════════════════════════════════════════════════════════════════════════════
#  ROTATION LOGIC
# ════════════════════════════════════════════════════════════════════════════
import os
import re
import json
import uuid
import datetime
import subprocess
import threading
import tempfile
import shutil

from flask import Blueprint, request, jsonify, send_from_directory

from routes_auth import _get_session, _token_from_request
from routes_auth import require_auth, require_admin_role

# ── Blueprint ─────────────────────────────────────────────────────────────
rota_bp = Blueprint('rota', __name__)

# ── Config ────────────────────────────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ROTA_DIR     = os.path.join(_BASE_DIR, 'rota')
MEMBERS_FILE = os.path.join(ROTA_DIR, 'members.json')
LEAVE_FILE   = os.path.join(ROTA_DIR, 'leave_requests.json')


# ── Data helpers ──────────────────────────────────────────────────────────
def _load_json(path: str):
    print(f"[DEBUG] Loading JSON from: {path}")   # 👈 shows actual file used

    if not os.path.exists(path):
        print(f"[DEBUG] File does not exist: {path}")
        return {}

    try:
        with open(path, 'r') as f:
            data = json.load(f)
            print(f"[DEBUG] Successfully loaded: {path}")
            return data
    except Exception as e:
        print(f"[JSON ERROR] {path}: {e}")
        return {}



def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _get_member(username: str) -> dict | None:
    members = _load_json(MEMBERS_FILE)
    return members.get(username)


def _get_rota_role(session: dict) -> str:
    if session.get('role') == 'admin':
        return 'management'
    if _get_member(session['username']):
        return 'staff'
    return 'guest'

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
    "Sabina":   35, "Sergio": 119, "Tiago O": 77,
    "Vitor":    63, "Fernando": 21, "Marc":    7,
    "Gabriel":  49, "Mario":   91, "Isaac":   105,
}
ENGINEERING_OFFSETS = {"Hugo": 0, "Goncalo": 14, "Nuno": 7}

MANAGEMENT_SHIFTS = {
    "Joao R":  "0930-1800",
    "Marcus":  "0900-1730",
    "Joao L":  "0800-1630",
    "Tiago C": "0900-1730",
}

PUBLIC_HOLIDAYS = {
    date(2026,1,1),  date(2026,2,17), date(2026,4,3),
    date(2026,4,5),  date(2026,4,25), date(2026,5,1),
    date(2026,5,12), date(2026,6,4),  date(2026,6,10),
    date(2026,8,15), date(2026,10,5), date(2026,11,1),
    date(2026,12,1), date(2026,12,8), date(2026,12,25),
}

PARENTAL_LEAVE_TYPES = {"Parental Leave"}
MARITAL_LEAVE_TYPES  = {"Marital Leave"}


def _base_shift(name: str, d: date) -> str:
    delta = (d - ANCHOR_MONDAY).days
    if name in SPECIALIST_OFFSETS:
        idx = (SPECIALIST_OFFSETS[name] + delta) % len(SPECIALIST_ROTATION)
        return SPECIALIST_ROTATION[idx]
    if name in ENGINEERING_OFFSETS:
        idx = (ENGINEERING_OFFSETS[name] + delta) % len(ENGINEERING_ROTATION)
        return ENGINEERING_ROTATION[idx]
    # Management
    if d.weekday() >= 5 or d in PUBLIC_HOLIDAYS:
        return "OFF"
    return MANAGEMENT_SHIFTS.get(name, "OFF")


def _resolve_shift(name: str, d: date, leave_map: dict) -> str:
    """Apply leave overlay on top of base shift."""
    leave = leave_map.get((name, d))
    if not leave:
        return _base_shift(name, d)
    lt     = leave["leave_type"]
    status = leave["status"]
    base   = _base_shift(name, d)
    if lt in PARENTAL_LEAVE_TYPES:
        return "PARENTAL"
    if lt in MARITAL_LEAVE_TYPES:
        return "MARITAL"
    code = "AL_APPROVED" if status == "Approved" else "AL_PENDING"
    return code if base == "OFF" else f"{code}|{base}"


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
            lmap[(r["name"], d)] = {   # ← name, not username
                "leave_type": r["leave_type"],
                "status":     r["status"],
            }
            d += timedelta(days=1)
    return lmap

@rota_bp.route('/rota/me', methods=['GET'])
@require_auth
def rota_me():
    session   = request.session
    rota_role = _get_rota_role(session)
    member    = _get_member(session['username'])
    return jsonify({
        'ok':        True,
        'username':  session['username'],
        'rota_role': rota_role,
        'name':      member['name'] if member else session['username'],
        'team':      member['team'] if member else None,
    })


def register_routes(app) -> None:
    app.register_blueprint(rota_bp)

# ════════════════════════════════════════════════════════════════════════════
#  SCHEDULE ENDPOINT
# ════════════════════════════════════════════════════════════════════════════

@rota_bp.route('/rota/schedule', methods=['GET'])
@require_auth
def rota_schedule():
    """
    GET /so-proxy/rota/schedule?from=2026-01-01&to=2026-12-31

    Returns a day-by-day schedule for all active members.
    Management sees everyone; staff sees all teams (read-only rota).
    """
    try:
        date_from = date.fromisoformat(request.args.get('from', date.today().isoformat()))
        date_to   = date.fromisoformat(request.args.get('to',   (date.today() + timedelta(weeks=5)).isoformat()))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    members  = _load_json(MEMBERS_FILE)
    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    # Build name-keyed member map for the schedule
    # members.json uses username (email) as key; name is the display value
    name_to_team = {v['name']: v['team'] for v in members.values()}
    leave_map    = _build_leave_map(leave_list)

    today = date.today()
    days  = []
    d     = date_from

    while d <= date_to:
        day = {
            'date':               d.isoformat(),
            'weekday':            d.strftime('%A'),
            'is_today':           d == today,
            'is_weekend':         d.weekday() >= 5,
            'is_public_holiday':  d in PUBLIC_HOLIDAYS,
            'shifts':             {},
        }

        # Management
        for name, shift in MANAGEMENT_SHIFTS.items():
            day['shifts'][name] = {
                'team':  'Management',
                'shift': _resolve_shift(name, d, leave_map),
            }

        # Engineering
        for name in ENGINEERING_OFFSETS:
            day['shifts'][name] = {
                'team':  'Engineering',
                'shift': _resolve_shift(name, d, leave_map),
            }

        # Specialists
        for name in SPECIALIST_OFFSETS:
            day['shifts'][name] = {
                'team':  'Specialists',
                'shift': _resolve_shift(name, d, leave_map),
            }

        days.append(day)
        d += timedelta(days=1)

    return jsonify({'ok': True, 'days': days})