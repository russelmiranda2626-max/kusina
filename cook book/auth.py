"""
auth.py - Authentication & User Session Management Module
"""
import re
from functools import wraps
from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError

# Lumikha ng Flask Blueprint para sa Auth API routes
auth_bp = Blueprint('auth', __name__)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Tatanggapin ang database instance at User model mula sa app.py
db = None
User = None

def init_auth(db_instance, user_model):
    global db, User
    db = db_instance
    User = user_model

def current_user():
    uid = session.get('uid')
    return db.session.get(User, uid) if uid and db and User else None

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({'error': 'Mag-login muna.'}), 401
        return fn(u, *a, **kw)
    return wrapper

def json_body():
    return request.get_json(silent=True) or {} if request.is_json else None

@auth_bp.get('/api/me')
def api_me():
    u = current_user()
    return jsonify({'user': {'id': u.id, 'name': u.name, 'email': u.email} if u else None})

@auth_bp.post('/api/signup')
def api_signup():
    body = json_body()
    name = str(body.get('name', '')).strip()
    email = str(body.get('email', '')).strip().lower()
    password = str(body.get('password', ''))

    if not name or not email or not password:
        return jsonify({'error': 'Punan ang lahat ng fields.'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Invalid email format.'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Minimum na 6 characters sa password.'}), 400

    if db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none():
        return jsonify({'error': 'May account na sa email na ito.'}), 400

    u = User(name=name, email=email, password_hash=generate_password_hash(password))
    db.session.add(u)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'May error sa pag-save ng account.'}), 400

    session['uid'] = u.id
    return jsonify({'ok': True, 'user': {'id': u.id, 'name': u.name, 'email': u.email}})

@auth_bp.post('/api/login')
def api_login():
    body = json_body()
    email = str(body.get('email', '')).strip().lower()
    password = str(body.get('password', ''))

    u = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
    if not u or not check_password_hash(u.password_hash, password):
        return jsonify({'error': 'Maling email o password.'}), 401

    session['uid'] = u.id
    return jsonify({'ok': True, 'user': {'id': u.id, 'name': u.name, 'email': u.email}})

@auth_bp.post('/api/logout')
def api_logout():
    session.pop('uid', None)
    return jsonify({'ok': True})