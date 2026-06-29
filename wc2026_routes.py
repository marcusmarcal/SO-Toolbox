"""
wc2026_routes.py  –  WC 2026 Engineering Rota · persistence endpoints
Register in proxy.py:
    from wc2026_routes import wc2026_bp
    app.register_blueprint(wc2026_bp)
"""

import json
import os
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

from routes_auth import require_admin_role, require_auth

wc2026_bp = Blueprint('wc2026', __name__, url_prefix='/wc2026')

ASSIGNMENTS_FILE = os.path.join(os.path.dirname(__file__), 'wc2026_assignments.json')

_EMPTY = {
    'assignments': {},
    'engNames': {'N': 'Nuno', 'G': 'Goncalo', 'H': 'Hugo', 'M': 'Marcus'},
    'updatedBy': None,
    'updatedAt': None,
}


def _load():
    try:
        with open(ASSIGNMENTS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict) and 'assignments' in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return dict(_EMPTY)


def _save(data):
    tmp = ASSIGNMENTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, ASSIGNMENTS_FILE)


@wc2026_bp.route('/assignments', methods=['GET'])
@require_auth
def get_assignments():
    """Return current assignments. Readable by any authenticated user."""
    return jsonify({'ok': True, 'data': _load()})


@wc2026_bp.route('/assignments', methods=['POST'])
@require_admin_role
def save_assignments():
    """Persist assignments, engineer names and scores. Admin only."""
    body = request.get_json(silent=True)
    if not body or 'assignments' not in body:
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400

    assignments     = body.get('assignments', {})
    eng_names       = body.get('engNames', {})
    incoming_scores = body.get('scores', {})

    clean_a = {}
    for k, v in assignments.items():
        if str(k).isdigit() and v in ('N', 'G', 'H', 'M', ''):
            clean_a[k] = v

    clean_n = {}
    for code in ('N', 'G', 'H', 'M'):
        raw = str(eng_names.get(code, code))[:40].strip()
        clean_n[code] = raw or code

    # Merge incoming scores onto existing ones (never discard previously synced results)
    existing = _load()
    merged_scores = existing.get('scores', {})
    if isinstance(incoming_scores, dict):
        merged_scores.update({str(k): v for k, v in incoming_scores.items()})

    data = {
        'assignments':     clean_a,
        'engNames':        clean_n,
        'scores':          merged_scores,
        'updatedBy':       request.session.get('username', '?'),
        'updatedAt':       datetime.now(timezone.utc).isoformat(),
        'scoresUpdatedAt': existing.get('scoresUpdatedAt'),
        'scoresUpdatedBy': existing.get('scoresUpdatedBy'),
    }
    _save(data)
    return jsonify({'ok': True, 'updatedBy': data['updatedBy'], 'updatedAt': data['updatedAt']})

# Adicionar a wc2026_routes.py
# Endpoint separado para scores — sem require_admin_role,
# qualquer utilizador autenticado pode fazer sync e persistir resultados.
# O GET /assignments já devolve scores no mesmo blob, por isso o load não precisa de mudar.

@wc2026_bp.route('/scores', methods=['POST'])
@require_auth
def save_scores():
    """Persist scores from openfootball sync. Any authenticated user."""
    payload = request.get_json(silent=True) or {}
    incoming_scores = payload.get('scores', {})
    if not isinstance(incoming_scores, dict):
        return jsonify({'ok': False, 'error': 'invalid payload'}), 400

    data = _load()
    existing = data.get('scores', {})
    existing.update({str(k): v for k, v in incoming_scores.items()})
    data['scores'] = existing
    data['scoresUpdatedAt'] = datetime.now(timezone.utc).isoformat()
    data['scoresUpdatedBy'] = request.session.get('username', '?')
    _save(data)
    return jsonify({'ok': True})