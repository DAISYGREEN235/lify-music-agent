"""SQLite 是歌曲、事件和费用的事实源，连接按操作创建以隔离线程。"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, path TEXT UNIQUE, fingerprint TEXT,
                    payload TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS vectors(track_id TEXT, route TEXT, version TEXT, fingerprint TEXT,
                    vector TEXT, evidence TEXT, PRIMARY KEY(track_id,route,version));
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at REAL, session TEXT, kind TEXT,
                    track_id TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exclusions(track_id TEXT PRIMARY KEY, at REAL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS costs(id TEXT PRIMARY KEY, at REAL, provider TEXT, status TEXT,
                    reserved REAL, actual REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS feature_errors(track_id TEXT, route TEXT, error TEXT, at REAL,
                    PRIMARY KEY(track_id,route));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def tracks(self):
        with self.connect() as db:
            return [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM tracks WHERE available=1 ORDER BY id")]

    def put_track(self, track):
        with self.connect() as db:
            db.execute("INSERT INTO tracks VALUES(?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                       "path=excluded.path,fingerprint=excluded.fingerprint,payload=excluded.payload,available=1",
                       (track["id"], track["path"], track["fingerprint"], dumps(track)))

    def event(self, kind, payload, session="", track_id=None, event_id=None):
        event_id = event_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                       (event_id, time.time(), session, kind, track_id, dumps(payload)))
        return event_id

    def events(self, session=None, kinds=None):
        with self.connect() as db:
            conditions, params = [], []
            if session:
                conditions.append('session=?')
                params.append(session)
            if kinds:
                conditions.append('kind IN (' + ','.join('?' for _ in kinds) + ')')
                params.extend(kinds)
            rows = db.execute('SELECT * FROM events' +
                              (' WHERE ' + ' AND '.join(conditions) if conditions else '') + ' ORDER BY at,rowid',
                              params).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def backup(self):
        """SQLite backup包含已提交WAL，不能只复制正在使用的主文件。"""
        folder = self.path.parent / 'backups'
        folder.mkdir(exist_ok=True)
        target = folder / f'{self.path.stem}-{time.time_ns()}.sqlite'
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target

    def vector_count(self, route, version):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM vectors v JOIN tracks t ON t.id=v.track_id '
                              'WHERE v.route=? AND v.version=? AND v.fingerprint=t.fingerprint AND t.available=1',
                              (route, version)).fetchone()[0]

    def save_session(self, session, payload):
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (session, dumps(payload)))

    def load_session(self, session):
        with self.connect() as db:
            r = db.execute("SELECT payload FROM sessions WHERE id=?", (session,)).fetchone()
            return json.loads(r[0]) if r else {}

    def update_session(self, session, changes):
        """播放器与推荐流程可能同时保存，各自只修改自己的字段。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM sessions WHERE id=?', (session,)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(changes)
            db.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (session, dumps(value)))

    def feedback(self, session, track_id, action, scene="", rating=None):
        if action not in {"ban", "unban", "like", "dislike", "unsuitable", "rate"}:
            raise ValueError("未知反馈类型")
        if rating is not None and rating not in (0, 1, 2):
            raise ValueError("场景评分只能为0/1/2")
        if action == 'rate' and rating is None:
            raise ValueError('评分事件必须提供0/1/2评分')
        if track_id not in {t["id"] for t in self.tracks()}:
            raise ValueError("歌曲ID不在当前曲库")
        with self.connect() as db:
            if action == "ban":
                db.execute("INSERT OR REPLACE INTO exclusions VALUES(?,?)", (track_id, time.time()))
            elif action == "unban":
                db.execute("DELETE FROM exclusions WHERE track_id=?", (track_id,))
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, time.time(), session,
                       "feedback", track_id, dumps({"action": action, "scene": scene, "rating": rating})))

    def excluded(self):
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT track_id FROM exclusions")}

    def put_vector(self, track, route, version, vector, evidence):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?)",
                       (track["id"], route, version, track["fingerprint"], dumps(vector), dumps(evidence)))
            db.execute("DELETE FROM feature_errors WHERE track_id=? AND route=?", (track["id"], route))

    def vectors(self, route, version):
        with self.connect() as db:
            rows = db.execute("SELECT v.* FROM vectors v JOIN tracks t ON t.id=v.track_id "
                              "WHERE v.route=? AND v.version=? AND v.fingerprint=t.fingerprint AND t.available=1",
                              (route, version)).fetchall()
            return {r["track_id"]: (json.loads(r["vector"]), json.loads(r["evidence"])) for r in rows}

    def feature_error(self, track_id, route, error):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO feature_errors VALUES(?,?,?,?)",
                       (track_id, route, str(error)[:300], time.time()))
