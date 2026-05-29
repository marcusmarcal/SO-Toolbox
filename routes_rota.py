"""
routes_rota.py — Rota App Blueprint
SO-Toolbox v2.24.0

Serves the Rota App under /so-proxy/rota/*.
Auth is delegated to the existing SO-Toolbox session (routes_auth.py).
Members metadata (team, rotation) lives in rota/data/members.json.
"""

import json
import os
from flask import Blueprint, jsonify, send_from_directory, request
from routes_auth import require_auth, require_admin_role

rota_bp = Blueprint('rota', __name__)

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ROTA_DIR    = os.path.join(_BASE_DIR, 'rota')          # /opt/so-toolbox/rota/
MEMBERS_FILE = os.path.join(ROTA_DIR, 'data', 'members.json')
LEAVE_FILE   = os.path.join(ROTA_DIR, 'data', 'leave_requests.json')


# ════════════════════════════════════════════════════════════════════════════
#  DATA HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _load_json(path: str) -> dict | list:
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)


def _save_json(path: str, data: dict | list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _get_member(username: str) -> dict | None:
    """Return the member record matching the SO-Toolbox username, or None."""
    members = _load_json(MEMBERS_FILE)
    return members.get(username)


def _is_management(session: dict) -> bool:
    member = _get_member(session['username'])
    if not member:
        return False
    return member.get('team') == 'Management' or session.get('role') == 'admin'


# ════════════════════════════════════════════════════════════════════════════
#  STATIC / APP SHELL
# ════════════════════════════════════════════════════════════════════════════

@rota_bp.route('/rota/', methods=['GET'])
@rota_bp.route('/rota', methods=['GET'])
@require_auth
def rota_index():
    """Serve the main app shell. Auth already confirmed by decorator."""
    return send_from_directory(os.path.join(ROTA_DIR, 'static'), 'index.html')


@rota_bp.route('/rota/guest', methods=['GET'])
def rota_guest():
    """Fully public guest view — no auth required."""
    return send_from_directory(os.path.join(ROTA_DIR, 'static'), 'guest.html')


@rota_bp.route('/rota/static/<path:filename>', methods=['GET'])
def rota_static(filename):
    return send_from_directory(os.path.join(ROTA_DIR, 'static'), filename)


# ════════════════════════════════════════════════════════════════════════════
#  SESSION / IDENTITY
# ════════════════════════════════════════════════════════════════════════════

@rota_bp.route('/rota/me', methods=['GET'])
@require_auth
def rota_me():
    """
    Returns the caller's rota identity — SO-Toolbox session merged with
    their member record. Frontend uses this to decide which tabs to show.

    Response:
      { ok, username, role, rota_role, team, member } 
      rota_role: "management" | "staff" | "guest"
    """
    session = request.session
    member  = _get_member(session['username'])

    if not member:
        return jsonify({
            'ok':       True,
            'username': session['username'],
            'role':     session['role'],
            'rota_role': 'guest',
            'team':     None,
            'member':   None,
        })

    rota_role = 'management' if _is_management(session) else 'staff'

    return jsonify({
        'ok':        True,
        'username':  session['username'],
        'role':      session['role'],
        'rota_role': rota_role,
        'team':      member.get('team'),
        'member':    {
            'name':  member.get('name'),
            'email': member.get('email'),
            'team':  member.get('team'),
        },
    })


# ════════════════════════════════════════════════════════════════════════════
#  ROTA DATA
# ════════════════════════════════════════════════════════════════════════════

@rota_bp.route('/rota/members', methods=['GET'])
@require_auth
def rota_members():
    """Returns all active members. Management gets full list; staff gets own team only."""
    session = request.session
    members = _load_json(MEMBERS_FILE)

    if _is_management(session):
        safe = {u: {k: v for k, v in d.items()} for u, d in members.items()}
    else:
        member = _get_member(session['username'])
        my_team = member.get('team') if member else None
        safe = {
            u: d for u, d in members.items()
            if d.get('team') == my_team
        }

    return jsonify({'ok': True, 'members': safe})


@rota_bp.route('/rota/leave', methods=['GET'])
@require_auth
def get_leave():
    """
    Management: all leave requests.
    Staff: only their own.
    """
    session  = request.session
    requests_ = _load_json(LEAVE_FILE)

    if not isinstance(requests_, list):
        requests_ = []

    if _is_management(session):
        return jsonify({'ok': True, 'leave': requests_})

    own = [r for r in requests_ if r.get('username') == session['username']]
    return jsonify({'ok': True, 'leave': own})


@rota_bp.route('/rota/leave', methods=['POST'])
@require_auth
def submit_leave():
    """Staff submit a leave request."""
    session = request.session
    data    = request.get_json(silent=True) or {}

    required = ('date_start', 'date_end', 'leave_type')
    if not all(data.get(k) for k in required):
        return jsonify({'ok': False, 'error': 'date_start, date_end, leave_type required'}), 400

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list):
        leave_list = []

    import time
    entry = {
        'username':   session['username'],
        'date_start': data['date_start'],
        'date_end':   data['date_end'],
        'leave_type': data['leave_type'],
        'status':     'Pending',
        'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    leave_list.append(entry)
    _save_json(LEAVE_FILE, leave_list)

    return jsonify({'ok': True, 'entry': entry}), 201


@rota_bp.route('/rota/leave/<int:index>', methods=['PUT'])
@require_admin_role
def update_leave(index):
    """Management approve/reject a leave request by list index."""
    data       = request.get_json(silent=True) or {}
    new_status = data.get('status')

    if new_status not in ('Approved', 'Rejected'):
        return jsonify({'ok': False, 'error': 'status must be Approved or Rejected'}), 400

    leave_list = _load_json(LEAVE_FILE)
    if not isinstance(leave_list, list) or index >= len(leave_list):
        return jsonify({'ok': False, 'error': 'Not found'}), 404

    leave_list[index]['status'] = new_status
    _save_json(LEAVE_FILE, leave_list)

    return jsonify({'ok': True, 'entry': leave_list[index]})


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_routes(app) -> None:
    app.register_blueprint(rota_bp)