from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY, idem TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
                    request TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, result TEXT, original TEXT,
                    error TEXT, trace TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, owner TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses(id),
                    actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                    before_value TEXT, after_value TEXT, revision INTEGER NOT NULL,
                    batch_id TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    actor TEXT NOT NULL, published_by TEXT, created_at TEXT NOT NULL,
                    published_at TEXT, validation TEXT
                );
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS events_analysis ON events(analysis_id, created_at);
                CREATE INDEX IF NOT EXISTS analyses_time ON analyses(created_at DESC);
            """)
            from .workspace import migrate
            migrate(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise NotFound("记录不存在")
        item = dict(row)
        for key in ("request", "result", "original", "trace"):
            if item.get(key) is not None:
                item[key] = json.loads(item[key])
        return item

    def create(self, request, fingerprint, actor, authorize=False):
        request = dict(request)
        identity = actor + ":" + request["idempotency_key"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM analyses WHERE idem=?", (identity,)).fetchone()
            if row:
                if authorize:
                    from .workspace import require_analysis
                    require_analysis(db, row['id'], actor)
                if row["request_hash"] != fingerprint:
                    raise Conflict("同一幂等键对应不同输入，请生成新的任务键。")
                return self.decode(row), False
            aid = "call_" + uuid.uuid4().hex[:12]
            from .workspace import active_user, new_customer, require_customer
            cid = request.get('customer_id')
            tid = request.get('transcription_id')
            if tid:
                audio = db.execute('SELECT * FROM transcriptions WHERE id=?', (tid,)).fetchone()
                if not audio:
                    raise NotFound('转写不存在')
                if not audio['customer_id']:
                    from .workspace import require_audio
                    if authorize:
                        require_audio(db, audio, actor)
                    raise Conflict('请先确认录音的归属客户，再进入分析')
                if authorize:
                    require_customer(db, audio['customer_id'], actor)
                if audio['status'] != 'confirmed' or audio['confirmed_text'] != request['transcript']:
                    raise Conflict('请先核对并保存转写文本，再使用该确认版本分析')
                if cid and cid != audio['customer_id'] or request['data_mode'] != audio['data_mode']:
                    raise Conflict('转写必须沿用原客户和数据类型')
                cid = audio['customer_id']
                request['audio_source'] = {'id':tid, 'revision':audio['revision'], 'sha256':audio['sha256'],
                                           'confirmed_by':audio['confirmed_by']}
            if cid:
                if authorize:
                    require_customer(db, cid, actor)
            else:
                if authorize:
                    active_user(db, actor)
                owner = actor if db.execute('SELECT 1 FROM users WHERE id=? AND active=1', (actor,)).fetchone() else None
                cid = new_customer(db, request['title'], owner, actor)['id']
            stamp = now()
            db.execute("INSERT INTO analyses(id,idem,request_hash,request,status,phase,created_at,updated_at,owner,customer_id,transcription_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, identity, fingerprint, dumps(request), "queued", "等待分析", stamp, stamp, actor, cid, tid))
            return self.decode(db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()), True

    def get(self, aid):
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone())

    def listing(self, limit=200, actor=None):
        if limit is None: limit = -1
        with self.connection() as db:
            if actor:
                from .workspace import active_user
                user = active_user(db, actor)
                if user['role'] != 'admin':
                    return [self.decode(r) for r in db.execute('''SELECT a.* FROM analyses a JOIN customers c ON c.id=a.customer_id
                        WHERE c.owner_id=? ORDER BY a.created_at DESC LIMIT ?''', (actor, limit))]
            return [self.decode(r) for r in db.execute("SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?", (limit,))]

    def delete(self, aid, revision, actor):
        from .workspace import require_analysis
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            require_analysis(db, aid, actor)
            row = db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
            if row['revision'] != revision:
                raise Conflict('记录已变化，请刷新后再删除。')
            if row['status'] in ('queued','running'):
                raise Conflict('分析正在进行，请完成后再删除。')
            db.execute("DELETE FROM feedback WHERE analysis_id=?", (aid,))
            db.execute("DELETE FROM events WHERE analysis_id=?", (aid,))
            db.execute("DELETE FROM analyses WHERE id=?", (aid,))
        return {'deleted': True, 'id': aid}

    def checkpoint(self, aid, phase, detail=None):
        with self.connection() as db:
            row = db.execute("SELECT trace FROM analyses WHERE id=?", (aid,)).fetchone()
            trace = json.loads(row[0])
            trace.append({"phase": phase, "at": now(), "detail": detail or ""})
            db.execute("UPDATE analyses SET status='running',phase=?,trace=?,updated_at=? WHERE id=? AND status!='cancelled'",
                       (phase, dumps(trace), now(), aid))

    def finish(self, aid, result, error=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status FROM analyses WHERE id=?", (aid,)).fetchone()
            if not current or current["status"] == "cancelled":
                return
            stamp = now()
            db.execute("UPDATE analyses SET status=?,phase=?,result=?,original=?,revision=1,error=?,updated_at=? WHERE id=? AND status!='cancelled'",
                       ("failed" if error else "completed", "分析失败 · 可人工处理" if error else "分析完成",
                        dumps(result), dumps(result), error, stamp, aid))
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, aid, "Agent", "analyze", error or "生成分析快照",
                       None, dumps(result), 1, None, stamp))

    def cancel(self, aid, actor="system"):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
            if not row:
                raise NotFound("记录不存在")
            if row["status"] not in ("queued", "running"):
                raise Conflict("只有排队中或运行中的分析可以停止")
            stamp = now()
            db.execute("UPDATE analyses SET status='cancelled',phase='分析已停止',error='用户停止分析，原文和检查点已保留。',updated_at=? WHERE id=? AND status IN ('queued','running')",
                       (stamp, aid))
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, aid, actor, "cancel", "用户停止分析", None, None, row["revision"], None, stamp))
            return self.decode(db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone())

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE analyses SET status='interrupted',phase='服务重启 · 请重试',error='分析中断，检查点已保留，请重新分析。',updated_at=? WHERE status IN ('running','queued')", (now(),))

    def mutate(self, aid, expected_revision, actor, action, reason, callback, batch_id=None, db=None, authorize=False):
        if db is None:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.mutate(aid, expected_revision, actor, action, reason, callback, batch_id, connection, authorize)
        if authorize:
            from .workspace import require_analysis
            require_analysis(db, aid, actor)
        record = self.decode(db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone())
        if record["revision"] != expected_revision:
            raise Conflict("记录已被修改，请刷新后核对差异再提交。")
        if record["status"] not in ("completed", "failed"):
            raise Conflict("当前任务尚未完成，请等待或重新分析。")
        before = record["result"]
        after, feedback = callback(record)
        revision = expected_revision + 1
        db.execute("UPDATE analyses SET result=?,revision=?,updated_at=? WHERE id=? AND revision=?",
                   (dumps(after), revision, now(), aid, expected_revision))
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, aid, actor, action, reason,
                   dumps(before), dumps(after), revision, batch_id, now()))
        if feedback:
            db.execute("INSERT INTO feedback(id,analysis_id,kind,payload,actor,created_at) VALUES(?,?,?,?,?,?)",
                       ("fb_" + uuid.uuid4().hex[:12], aid, feedback["kind"], dumps(feedback["payload"]), actor, now()))
        return self.decode(db.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone())

    def events(self, aid):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE analysis_id=? ORDER BY created_at, revision", (aid,))]
            for row in rows:
                for key in ("before_value", "after_value"):
                    row[key] = json.loads(row[key]) if row[key] else None
            return rows

    def feedback(self, actor=None):
        with self.connection() as db:
            from .workspace import active_user
            if actor and active_user(db, actor)['role'] != 'admin':
                query = '''SELECT f.* FROM feedback f JOIN analyses a ON a.id=f.analysis_id
                    JOIN customers c ON c.id=a.customer_id WHERE c.owner_id=? ORDER BY f.created_at DESC'''
                rows = [dict(r) for r in db.execute(query, (actor,))]
            else:
                rows = [dict(r) for r in db.execute("SELECT * FROM feedback ORDER BY created_at DESC")]
            for r in rows:
                r["payload"] = json.loads(r["payload"])
                r["validation"] = json.loads(r["validation"]) if r["validation"] else None
            return rows

    def learned(self, customer_id=None):
        rows = self.feedback()
        if customer_id:
            with self.connection() as db:
                ids = {r[0] for r in db.execute('SELECT id FROM analyses WHERE customer_id=?', (customer_id,))}
            rows = [r for r in rows if r['analysis_id'] in ids]
        return [r["payload"] for r in rows if r["status"] == "published" and r["kind"] == "entity_alias"]
