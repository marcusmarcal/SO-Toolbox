"""
routes_auth.py — Authentication & User Management Blueprint
SO-Toolbox v2.24.0

Registers all /login, /logout, /me, /users/* routes (nginx strips /so-proxy prefix) routes.
User database lives in users.json (next to proxy.py) — NOT in Git.
"""

import json
import hashlib
import hmac
import secrets
import time
import os
from functools import wraps

from flask import Blueprint, request, jsonify, redirect

# ── Blueprint ─────────────────────────────────────────────────────────────
auth_bp = Blueprint('auth', __name__)

# ── Config ────────────────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
USERS_FILE  = os.path.join(_BASE_DIR, 'users.json')
SESSION_TTL = 8 * 3600   # 8 hours

# ── Session store (in-memory; resets on proxy restart) ───────────────────
_sessions: dict = {}   # token → { username, role, expires }


# ════════════════════════════════════════════════════════════════════════════
#  USER-DB HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: dict) -> None:
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)
    os.chmod(USERS_FILE, 0o600)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(_hash_password(password), hashed)


# ════════════════════════════════════════════════════════════════════════════
#  SESSION HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _create_session(username: str, role: str) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {
        'username': username,
        'role':     role,
        'expires':  time.time() + SESSION_TTL,
    }
    return token


def _get_session(token: str) -> dict | None:
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() > s['expires']:
        del _sessions[token]
        return None
    return s


def _invalidate_session(token: str) -> None:
    _sessions.pop(token, None)


def _token_from_request() -> str:
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.cookies.get('sotb-session', '')


# ════════════════════════════════════════════════════════════════════════════
#  DECORATORS  (importable by proxy.py for other routes)
# ════════════════════════════════════════════════════════════════════════════

def require_auth(f):
    """Require a valid session token (cookie or Bearer header)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        session = _get_session(_token_from_request())
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401
        request.session = session
        return f(*args, **kwargs)
    return decorated


def require_admin_role(f):
    """Require a valid session AND role == admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        session = _get_session(_token_from_request())
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'Forbidden — admin role required'}), 403
        request.session = session
        return f(*args, **kwargs)
    return decorated

def _get_admin_password() -> str:
    """Read ADMIN_PASSWORD from .env — same pattern as proxy.py."""
    env_path = os.path.join(_BASE_DIR, '.env')
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('ADMIN_PASSWORD='):
                    return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''


def require_admin(f):
    """Require ADMIN_PASSWORD in X-Admin-Password header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        admin_pw = _get_admin_password()
        if not admin_pw:
            return jsonify({'error': 'ADMIN_PASSWORD not configured'}), 500
        if not hmac.compare_digest(request.headers.get('X-Admin-Password', ''), admin_pw):
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


# ════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route('/login', methods=['POST'])
def login():
    data     = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not username or not password:
        return jsonify({'ok': False, 'error': 'Missing credentials'}), 400

    users = _load_users()
    user  = users.get(username)

    if not user or not _verify_password(password, user['password_hash']):
        time.sleep(0.4)   # constant-time penalty
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401

    token = _create_session(username, user.get('role', 'user'))

    resp = jsonify({'ok': True, 'token': token, 'role': user.get('role', 'user'), 'username': username})
    resp.set_cookie(
        'sotb-session', token,
        httponly=True, samesite='Strict', secure=False,
        max_age=SESSION_TTL,
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
    return jsonify({'ok': True, 'username': request.session['username'], 'role': request.session['role']})


# ════════════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT ROUTES
# ════════════════════════════════════════════════════════════════════════════

@auth_bp.route('/users', methods=['GET'])
@require_admin_role
def list_users():
    users = _load_users()
    safe  = sorted([
        {'username': u, 'role': d.get('role', 'user'), 'created_at': d.get('created_at', '')}
        for u, d in users.items()
    ], key=lambda x: x['username'])
    return jsonify({'ok': True, 'users': safe})


@auth_bp.route('/users', methods=['POST'])
@require_admin_role
def create_user():
    data     = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    role     = str(data.get('role', 'user'))

    if not username:
        return jsonify({'ok': False, 'error': 'username required'}), 400
    if not password:
        return jsonify({'ok': False, 'error': 'password required'}), 400
    if role not in ('user', 'admin'):
        return jsonify({'ok': False, 'error': 'role must be user or admin'}), 400

    users = _load_users()
    if username in users:
        return jsonify({'ok': False, 'error': f'User "{username}" already exists'}), 409

    users[username] = {
        'password_hash': _hash_password(password),
        'role':          role,
        'created_at':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    _save_users(users)
    return jsonify({'ok': True, 'username': username}), 201


@auth_bp.route('/users/<username>', methods=['PUT'])
@require_admin_role
def update_user(username):
    users = _load_users()
    if username not in users:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    data     = request.get_json(silent=True) or {}
    role     = data.get('role')
    password = data.get('password', '')

    if role and role not in ('user', 'admin'):
        return jsonify({'ok': False, 'error': 'role must be user or admin'}), 400
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

    del users[username]
    _save_users(users)

    # Kick any active sessions for this user
    for token in [t for t, s in _sessions.items() if s['username'] == username]:
        del _sessions[token]

    return jsonify({'ok': True, 'username': username})


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ════════════════════════════════════════════════════════════════════════════

def register_routes(app) -> None:
    """Call this from proxy.py exactly like the other blueprint modules."""
    app.register_blueprint(auth_bp)
