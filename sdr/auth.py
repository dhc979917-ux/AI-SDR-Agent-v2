"""Password accounts and revocable server-side sessions. No browser token storage."""
import hashlib
import hmac
import secrets
import sqlite3
import time

from .store import Conflict, NotFound, now
from .workspace import Forbidden, active_user, event, require_admin

COOKIE = 'sdr_session'
ITERATIONS = 600_000


class LoginError(Exception):
    def __init__(self, message='账号或密码错误', status=401):
        self.status = status
        super().__init__(message)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def verify_password(password, stored):
    _, rounds, salt, digest = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(actual, digest)


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(row):
    return {k: row[k] for k in ('id', 'name', 'role', 'active', 'created_at')}


class Auth:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.dummy = password_hash(secrets.token_urlsafe(24))

    def create_user(self, body, actor=None):
        hashed = password_hash(body.password)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if actor:
                require_admin(db, actor)
            elif db.execute('SELECT count(*) FROM users').fetchone()[0] or body.role != 'admin':
                raise Forbidden('初始管理员只能在空账号库中创建')
            try:
                db.execute('INSERT INTO users(id,name,role,password_hash,created_at) VALUES(?,?,?,?,?)',
                           (body.username, body.name, body.role, hashed, now()))
            except sqlite3.IntegrityError:
                raise Conflict('该登录账号已存在') from None
            event(db, actor or 'bootstrap', 'account_created', '创建账号', after={'id': body.username, 'name': body.name, 'role': body.role})
            return public_user(db.execute('SELECT * FROM users WHERE id=?', (body.username,)).fetchone())

    def login(self, username, password, ip):
        stamp = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_attempts WHERE at<?', (stamp - 900,))
            attempts = db.execute('SELECT count(*) FROM login_attempts WHERE ip=?', (ip,)).fetchone()[0]
            account_attempts = db.execute('SELECT count(*) FROM login_attempts WHERE ip=? AND account=?', (ip, username)).fetchone()[0]
            if attempts >= 30 or account_attempts >= 5:
                raise LoginError('尝试次数过多，请15分钟后重试', 429)
            # Reserve before hashing so concurrent failed attempts cannot bypass the limit.
            db.execute('INSERT INTO login_attempts VALUES(?,?,?)', (ip, username, stamp))
            row = db.execute('SELECT * FROM users WHERE id=?', (username,)).fetchone()
        valid = verify_password(password, row['password_hash'] if row else self.dummy)
        if not row or not valid or not row['active']:
            raise LoginError()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM users WHERE id=?', (username,)).fetchone()
            if not current['active'] or current['password_hash'] != row['password_hash']:
                raise LoginError()
            db.execute('DELETE FROM login_attempts WHERE ip=? AND account=?', (ip, username))
            db.execute('DELETE FROM sessions WHERE expires_at<?', (stamp,))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest_token(token), username, csrf, stamp+self.settings.session_hours*3600))
        return token, {'user': public_user(current), 'csrf': csrf}

    def session(self, token):
        with self.store.connection() as db:
            row = db.execute('''SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE token_hash=? AND expires_at>? AND u.active=1''', (digest_token(token or ''), time.time())).fetchone()
            if not row:
                raise LoginError('请登录员工账号')
            return {'user': public_user(row), 'csrf': row['csrf']}

    def logout(self, token):
        with self.store.connection() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest_token(token or ''),))

    def users(self, actor):
        with self.store.connection() as db:
            require_admin(db, actor)
            return [public_user(r) for r in db.execute('SELECT * FROM users ORDER BY created_at,id')]

    def update_user(self, uid, body, actor):
        hashed = password_hash(body.password) if body.password else None
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            require_admin(db, actor)
            old = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not old:
                raise NotFound('账号不存在')
            if uid == actor and body.active is False:
                raise Conflict('不能停用当前登录账号')
            if old['role'] == 'admin' and body.active is False and db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                raise Conflict('至少保留一名启用的管理员')
            if body.active is not None:
                db.execute('UPDATE users SET active=? WHERE id=?', (int(body.active), uid))
            if hashed:
                db.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, uid))
            if hashed or body.active is False:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            event(db, actor, 'account_updated', body.reason, after={'id': uid, 'active': body.active, 'password_reset': bool(hashed)})
            return public_user(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())

    def change_password(self, actor, current_password, new_password):
        hashed = password_hash(new_password)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            active_user(db, actor)
            old = db.execute('SELECT password_hash FROM users WHERE id=?', (actor,)).fetchone()[0]
            if not verify_password(current_password, old):
                raise LoginError('原密码不正确', 422)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, actor))
            db.execute('DELETE FROM sessions WHERE user_id=?', (actor,))
            event(db, actor, 'password_changed', '本人修改密码，撤销全部会话')
