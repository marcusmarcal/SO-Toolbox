"""
routes_auth.py — Authentication & User Management Blueprint
SO-Toolbox

Supports roles:
- admin
- engineer
- analyst
- specialist
- user

Permissions:
- admin + engineer → full user management
"""

import json
import hashlib
import hmac
import bcrypt
import secrets
import time
import os
from functools import wraps

from flask import Blueprint, request, jsonify

# ── Blueprint ─────────────────────────────────────────────
auth_bp = Blueprint('auth', __name__)

# ── Config ────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
USERS_FILE  = os.path.join(_BASE_DIR, 'users.json')
SESSION_TTL = 8 * 3600  # 8 hours

# ✅ Roles
ALLOWED_ROLES = ('admin', 'engineer', 'analyst', 'specialist', 'user')
ADMIN_ROLES   = ('admin', 'engineer')

# ── Session store ─────────────────────────────────────────
_sessions = {}  # token → { username, role, expires }


# ══════════════════════════════════════════════════════════
# USER DB
# ══════════════════════════════════════════════════════════

def _load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)
    os.chmod(USERS_FILE, 0o600)


def _hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
# SESSION
# ══════════════════════════════════════════════════════════

def _create_session(username, role):
    token = secrets.token_hex(32)
    _sessions[token] = {
        'username': username,
        'role': role,
        'expires': time.time() + SESSION_TTL
    }
    return token


def _get_session(token):
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() > s['expires']:
        del _sessions[token]
        return None
    return s


def _invalidate_session(token):
    _sessions.pop(token, None)


def _token_from_request():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.cookies.get('sotb-session', '')


# ══════════════════════════════════════════════════════════
# DECORATORS
# ══════════════════════════════════════════════════════════

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        session = _get_session(_token_from_request())
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401
        request.session = session
        return f(*args, **kwargs)
    return decorated


def require_admin_role(f):
    """Allow admin + engineer"""
    @wraps(f)
    def decorated(*args, **kwargs):
        session = _get_session(_token_from_request())
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401

        if session.get('role') not in ADMIN_ROLES:
            return jsonify({'error': 'Forbidden — admin/engineer role required'}), 403

        request.session = session
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not username or not password:
        return jsonify({'ok': False, 'error': 'Missing credentials'}), 400

    users = _load_users()
    user = users.get(username)

    if not user or not _verify_password(password, user['password_hash']):
        time.sleep(0.4)
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401

    role = user.get('role', 'user')
    token = _create_session(username, role)

    resp = jsonify({
        'ok': True,
        'token': token,
        'role': role,
        'username': username
    })

    resp.set_cookie(
        'sotb-session',
        token,
        httponly=True,
        samesite='Lax',
        secure=False,
        max_age=SESSION_TTL
    )
    return resp


@auth_bp.route('/logout', methods=['POST'])
def logout():
    token = _token_from_request()
    _invalidate_session(token)

    resp = jsonify({'ok': True})
    resp.delete_cookie('sotb-session')
    return resp


@auth_bp.route('/me', methods=['GET'])
@require_auth
def me():
    return jsonify({
        'ok': True,
        'username': request.session['username'],
        'role': request.session['role']
    })


# ══════════════════════════════════════════════════════════
# USER MANAGEMENT
# ══════════════════════════════════════════════════════════

@auth_bp.route('/users', methods=['GET'])
@require_admin_role
def list_users():
    users = _load_users()

    safe = sorted([
        {
            'username': u,
            'role': d.get('role', 'user'),
            'created_at': d.get('created_at', '')
        }
        for u, d in users.items()
    ], key=lambda x: x['username'])

    return jsonify({'ok': True, 'users': safe})


@auth_bp.route('/users', methods=['POST'])
@require_admin_role
def create_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    role = str(data.get('role', 'user'))

    if not username:
        return jsonify({'ok': False, 'error': 'username required'}), 400
    if not password:
        return jsonify({'ok': False, 'error': 'password required'}), 400
    if role not in ALLOWED_ROLES:
        return jsonify({'ok': False, 'error': 'invalid role'}), 400

    users = _load_users()

    if username in users:
        return jsonify({'ok': False, 'error': f'user "{username}" exists'}), 409

    users[username] = {
        'password_hash': _hash_password(password),
        'role': role,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }

    _save_users(users)
    return jsonify({'ok': True, 'username': username}), 201


@auth_bp.route('/users/<username>', methods=['PUT'])
@require_admin_role
def update_user(username):
    users = _load_users()

    if username not in users:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    target_role = users[username].get('role', 'user')
    requester_role = request.session.get('role')
    if requester_role == 'engineer' and target_role == 'admin':
        return jsonify({'ok': False, 'error': 'Engineers cannot modify admin accounts'}), 403

    data = request.get_json(silent=True) or {}
    role = data.get('role')
    password = data.get('password', '')

    if role and role not in ALLOWED_ROLES:
        return jsonify({'ok': False, 'error': 'invalid role'}), 400

    if role:
        users[username]['role'] = role

    if password:
        users[username]['password_hash'] = _hash_password(password)

    _save_users(users)
    return jsonify({'ok': True, 'username': username})


@auth_bp.route('/users/<username>', methods=['DELETE'])
@require_admin_role
def delete_user(username):
    users = _load_users()

    if username not in users:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    target_role = users[username].get('role', 'user')
    requester_role = request.session.get('role')
    if requester_role == 'engineer' and target_role == 'admin':
        return jsonify({'ok': False, 'error': 'Engineers cannot delete admin accounts'}), 403

    del users[username]
    _save_users(users)

    # Invalidate sessions
    for token in [t for t, s in _sessions.items() if s['username'] == username]:
        del _sessions[token]

    return jsonify({'ok': True, 'username': username})


# ══════════════════════════════════════════════════════════
# REGISTER
# ══════════════════════════════════════════════════════════

def register_routes(app):
    app.register_blueprint(auth_bp)