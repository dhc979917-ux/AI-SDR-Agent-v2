"""Customer ownership is the authorization boundary for calls and recordings."""
import json
import uuid

from .store import Conflict, NotFound, dumps, now


class Forbidden(Exception):
    pass


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
            password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            csrf TEXT NOT NULL, expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT NOT NULL, account TEXT NOT NULL, at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_time ON login_attempts(at);
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT REFERENCES users(id),
            contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS customer_owner ON customers(owner_id);
        CREATE TABLE IF NOT EXISTS workspace_events (
            id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), actor TEXT NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, before_value TEXT, after_value TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transcriptions (
            id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id),
            uploaded_by TEXT NOT NULL REFERENCES users(id), idem TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            duration REAL NOT NULL, data_mode TEXT NOT NULL, status TEXT NOT NULL,
            raw_text TEXT NOT NULL DEFAULT '', confirmed_text TEXT, confirmed_by TEXT,
            revision INTEGER NOT NULL DEFAULT 1, error TEXT, provider TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transcription_customer ON transcriptions(customer_id, created_at);
    """)
    # Rebuild without renaming the old table: references from analyses stay valid.
    audio_columns = {r['name']: r for r in db.execute('PRAGMA table_info(transcriptions)')}
    if audio_columns['customer_id']['notnull']:
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            db.execute('BEGIN IMMEDIATE')
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='transcriptions'").fetchone()[0]
            sql = 'CREATE TABLE transcriptions_upgrade ' + sql[sql.index('('):]
            sql = sql.replace('customer_id TEXT NOT NULL', 'customer_id TEXT', 1)
            db.execute(sql)
            db.execute('INSERT INTO transcriptions_upgrade SELECT * FROM transcriptions')
            db.execute('DROP TABLE transcriptions')
            db.execute('ALTER TABLE transcriptions_upgrade RENAME TO transcriptions')
            db.execute('CREATE INDEX transcription_customer ON transcriptions(customer_id, created_at)')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('录音表升级未通过关联完整性检查')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')
    for name, definition in {
        'customer_mode': "TEXT NOT NULL DEFAULT 'existing'",
        'identity_status': "TEXT NOT NULL DEFAULT 'not_requested'",
        'identity_result': "TEXT NOT NULL DEFAULT '{}'",
        'identity_error': 'TEXT',
        'model_actor': 'TEXT',
        'identity_model_actor': 'TEXT',
        'role_status': "TEXT NOT NULL DEFAULT 'not_requested'",
        'role_result': "TEXT NOT NULL DEFAULT '{}'",
        'role_error': 'TEXT',
        'role_model_actor': 'TEXT',
        'role_generation': 'INTEGER NOT NULL DEFAULT 0',
        'auto_analyze': 'INTEGER NOT NULL DEFAULT 0',
        'auto_analysis_status': 'TEXT',
        'auto_analysis_error': 'TEXT',
        'auto_analysis_id': 'TEXT',
        'auto_role_fallback': 'INTEGER NOT NULL DEFAULT 0',
    }.items():
        if name not in audio_columns:
            db.execute(f'ALTER TABLE transcriptions ADD COLUMN {name} {definition}')
    db.execute('''CREATE TABLE IF NOT EXISTS audio_segments (
        transcription_id TEXT NOT NULL REFERENCES transcriptions(id) ON DELETE CASCADE,
        segment_index INTEGER NOT NULL, start_frame INTEGER NOT NULL, end_frame INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', sha256 TEXT, text TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (transcription_id, segment_index)
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS model_preferences (
        user_id TEXT PRIMARY KEY REFERENCES users(id), text_config TEXT NOT NULL,
        audio_config TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
    )''')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(analyses)')}
    if 'customer_id' not in columns:
        db.execute('ALTER TABLE analyses ADD COLUMN customer_id TEXT REFERENCES customers(id)')
    if 'transcription_id' not in columns:
        db.execute('ALTER TABLE analyses ADD COLUMN transcription_id TEXT REFERENCES transcriptions(id)')
    # Existing records stay intact and enter the administrator's unassigned pool.
    for row in db.execute('SELECT * FROM analyses WHERE customer_id IS NULL').fetchall():
        request, result = json.loads(row['request']), json.loads(row['result'] or '{}')
        name = ((result.get('lead') or {}).get('company_name') or request['title'])[:100]
        cid = 'customer_' + uuid.uuid4().hex[:12]
        db.execute('INSERT INTO customers(id,name,created_at,updated_at) VALUES(?,?,?,?)',
                   (cid, name, row['created_at'], now()))
        db.execute('UPDATE analyses SET customer_id=? WHERE id=?', (cid, row['id']))
        event(db, 'system', 'legacy_import', '原有通话保留，等待管理员分配负责人', None, {'analysis_id': row['id']}, cid)


def event(db, actor, action, reason, before=None, after=None, customer_id=None):
    db.execute('INSERT INTO workspace_events VALUES(?,?,?,?,?,?,?,?)',
               (uuid.uuid4().hex, customer_id, actor, action, reason,
                dumps(before) if before is not None else None, dumps(after) if after is not None else None, now()))


def active_user(db, actor):
    user = db.execute('SELECT id,name,role,active FROM users WHERE id=? AND active=1', (actor,)).fetchone()
    if not user:
        raise Forbidden('账号不可用，请重新登录')
    return dict(user)


def require_admin(db, actor):
    user = active_user(db, actor)
    if user['role'] != 'admin':
        raise Forbidden('此操作仅限管理员')
    return user


def require_customer(db, cid, actor):
    user = active_user(db, actor)
    row = db.execute('SELECT * FROM customers WHERE id=?', (cid,)).fetchone()
    if not row or (user['role'] != 'admin' and row['owner_id'] != actor):
        raise NotFound('客户不存在或不属于当前账号')
    return dict(row)


def require_analysis(db, aid, actor):
    row = db.execute('SELECT customer_id FROM analyses WHERE id=?', (aid,)).fetchone()
    if not row:
        raise NotFound('通话不存在或不属于当前账号')
    require_customer(db, row['customer_id'], actor)


def require_audio(db, row, actor):
    if row['customer_id']:
        require_customer(db, row['customer_id'], actor)
    else:
        user = active_user(db, actor)
        if user['role'] != 'admin' and row['uploaded_by'] != actor:
            raise NotFound('录音不存在或不属于当前账号')


def new_customer(db, name, owner, actor, contact='', phone='', notes=''):
    if owner:
        active_user(db, owner)
    cid, stamp = 'customer_' + uuid.uuid4().hex[:12], now()
    db.execute('INSERT INTO customers(id,name,owner_id,contact,phone,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
               (cid, name, owner, contact, phone, notes, stamp, stamp))
    event(db, actor, 'customer_created', '创建客户档案', None, {'name': name, 'owner_id': owner}, cid)
    return dict(db.execute('SELECT * FROM customers WHERE id=?', (cid,)).fetchone())


class Workspace:
    def __init__(self, store):
        self.store = store

    def customers(self, actor):
        with self.store.connection() as db:
            user = active_user(db, actor)
            where, params = ('', ()) if user['role'] == 'admin' else ('WHERE c.owner_id=?', (actor,))
            return [dict(r) for r in db.execute(f'''SELECT c.*, u.name AS owner_name,
                (SELECT count(*) FROM analyses a WHERE a.customer_id=c.id) AS call_count,
                (SELECT count(*) FROM transcriptions t WHERE t.customer_id=c.id) AS audio_count
                FROM customers c LEFT JOIN users u ON u.id=c.owner_id {where}
                ORDER BY c.updated_at DESC, c.id''', params)]

    def create(self, body, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            user = active_user(db, actor)
            owner = body.owner_id if user['role'] == 'admin' else actor
            if user['role'] != 'admin' and body.owner_id not in (None, actor):
                raise Forbidden('员工只能为自己创建客户')
            return new_customer(db, body.name, owner, actor, body.contact, body.phone, body.notes)

    def update_customer(self, cid, body, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            before = require_customer(db, cid, actor)
            if before['revision'] != body.expected_revision:
                raise Conflict('客户档案已变化，请刷新后重试。')
            if not body.name.strip():
                raise Conflict('客户名称不能为空。')
            db.execute('UPDATE customers SET name=?,contact=?,phone=?,notes=?,revision=revision+1,updated_at=? WHERE id=?',
                       (body.name.strip(),body.contact.strip(),body.phone.strip(),body.notes.strip(),now(),cid))
            after = dict(db.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone())
            event(db, actor, 'customer_updated', '', before, after, cid)
            return after

    def delete_customer(self, cid, revision, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            before = require_customer(db, cid, actor)
            if before['revision'] != revision:
                raise Conflict('客户档案已变化，请刷新后重试。')
            if db.execute('SELECT 1 FROM analyses WHERE customer_id=? LIMIT 1',(cid,)).fetchone() or db.execute('SELECT 1 FROM transcriptions WHERE customer_id=? LIMIT 1',(cid,)).fetchone():
                raise Conflict('该客户仍有关联分析或录音，请先处理关联记录后再删除客户。')
            # Preserve administrative audit without a dangling customer foreign key.
            db.execute('UPDATE workspace_events SET customer_id=NULL WHERE customer_id=?',(cid,))
            event(db, actor, 'customer_deleted', '', before, {'deleted': True})
            db.execute('DELETE FROM customers WHERE id=?',(cid,))
            return {'deleted': True, 'id': cid}

    def assign(self, body, actor):
        if len({i.id for i in body.items}) != len(body.items):
            raise Conflict('不能重复选择同一客户')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            require_admin(db, actor)
            if body.owner_id:
                active_user(db, body.owner_id)
            rows = [require_customer(db, i.id, actor) for i in body.items]
            if any(r['revision'] != i.revision for r, i in zip(rows, body.items)):
                raise Conflict('客户归属已变化，请刷新后重新分配；本批次未修改')
            for row in rows:
                db.execute('UPDATE customers SET owner_id=?, revision=revision+1,updated_at=? WHERE id=?',
                           (body.owner_id, now(), row['id']))
                event(db, actor, 'customer_assigned', body.reason, {'owner_id': row['owner_id']},
                      {'owner_id': body.owner_id}, row['id'])
            return {'updated': len(rows)}

    def events(self, actor, cid=None):
        with self.store.connection() as db:
            if cid:
                require_customer(db, cid, actor)
                rows = db.execute('SELECT * FROM workspace_events WHERE customer_id=? ORDER BY created_at DESC,rowid DESC', (cid,))
            else:
                require_admin(db, actor)
                rows = db.execute('SELECT * FROM workspace_events ORDER BY created_at DESC,rowid DESC LIMIT 500')
            return [{**dict(r), 'before_value': json.loads(r['before_value'] or 'null'),
                     'after_value': json.loads(r['after_value'] or 'null')} for r in rows]
