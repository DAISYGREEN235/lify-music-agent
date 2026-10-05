"""用户偏好与场景记忆：不改歌曲事实，不改模型；所有反馈保留原事件。"""
import json
import re

from .store import dumps

LOCAL_USER = 'local'
INTERPRETER_VERSION = 'explicit-text-v1'


def interpret(rating, reason):
    """仅解释明确措辞；无法确定的文字不伪装成长期偏好或事实标签。"""
    if not reason.strip():
        return {'rating': rating, 'source': 'rating', 'version': INTERPRETER_VERSION}
    negative = bool(re.search(r'不适合|不符合|不合适|不想听|不喜欢|太吵|太激烈|太悲伤|严重|不匹配', reason))
    partial = bool(re.search(r'部分适合|一般|有点|还行|勉强', reason))
    positive = bool(re.search(r'很适合|很符合|很合适|喜欢|符合场景|适合场景', reason))
    effective = 0 if negative else 1 if partial else 2 if positive else None
    return {'rating': effective, 'source': 'text' if effective is not None else 'unresolved_text',
            'version': INTERPRETER_VERSION, 'conflict': effective is not None and effective != rating}


class Preferences:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profiles(user_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scene_memory(user_id TEXT,scene TEXT,track_id TEXT,
                    dimension TEXT,event_id TEXT,ever_confirmed INTEGER DEFAULT 0,payload TEXT,
                    PRIMARY KEY(user_id,scene,track_id,dimension));
                CREATE TABLE IF NOT EXISTS user_exclusions(user_id TEXT,track_id TEXT,
                    PRIMARY KEY(user_id,track_id));
                CREATE INDEX IF NOT EXISTS events_session_kind ON events(session,kind,at);
                CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind,at);
            ''')
            # 旧全局排除仅属于本地默认用户；不复制给将来其他用户。
            db.execute('INSERT OR IGNORE INTO user_exclusions SELECT ?,track_id FROM exclusions', (LOCAL_USER,))
        # 可重复执行的旧网页反馈迁移；原事件不改，scene_memory只补尚不存在的记录。
        migrated = {}
        for event in store.events(kinds=['web_feedback']):
            value = event['payload']
            if not event['track_id'] or 'dimension' not in value:
                continue
            user_id = value.get('user_id', self.owner(event['session']))
            scene = value.get('scene') or store.load_session(event['session']).get('result', {}).get('intent', {}).get('scene', '')
            effective = value.get('effective') or interpret(value['rating'], value.get('reason', ''))
            key = (user_id, scene, event['track_id'], value['dimension'])
            ever = max(migrated.get(key, (None, 0, None))[1], int(effective['rating'] == 2))
            migrated[key] = (event['id'], ever, dumps({**value, 'effective': effective}))
        with store.connect() as db:
            for key, values in migrated.items():
                db.execute('INSERT OR IGNORE INTO scene_memory VALUES(?,?,?,?,?,?,?)', (*key, *values))

    def owner(self, session):
        return self.store.load_session(session).get('user_id', LOCAL_USER)

    def claim(self, session, user_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM sessions WHERE id=?', (session,)).fetchone()
            value = json.loads(row[0]) if row else {}
            if row and value.get('user_id', LOCAL_USER) != user_id:
                raise ValueError('任务不属于当前用户')
            value['user_id'] = user_id
            db.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (session, dumps(value)))

    def profile(self, user_id=LOCAL_USER):
        with self.store.connect() as db:
            row = db.execute('SELECT payload FROM profiles WHERE user_id=?', (user_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_profile(self, user_id, value):
        allowed = {'life_stage', 'common_scenes', 'stable_preferences'}
        if set(value) - allowed:
            raise ValueError('画像包含未知字段')
        with self.store.connect() as db:
            if value:
                db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?)', (user_id, dumps(value)))
            else:
                db.execute('DELETE FROM profiles WHERE user_id=?', (user_id,))
        self.store.event('profile_changed', {'user_id': user_id, 'profile': value})

    def feedback(self, session, track_id, rating, dimension, reason):
        user_id = self.owner(session)
        scene = self.store.load_session(session).get('result', {}).get('intent', {}).get('scene', '')
        effective = interpret(rating, reason)
        value = {'user_id': user_id, 'scene': scene, 'track_id': track_id, 'rating': rating,
                 'dimension': dimension, 'reason': reason, 'effective': effective, 'scope': 'current_task',
                 'source': 'user'}
        event_id = self.store.event('web_feedback', value, session, track_id)
        with self.store.connect() as db:
            db.execute('INSERT INTO scene_memory VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,scene,track_id,dimension) '
                       'DO UPDATE SET event_id=excluded.event_id,payload=excluded.payload,'
                       'ever_confirmed=max(scene_memory.ever_confirmed,excluded.ever_confirmed)',
                       (user_id, scene, track_id, dimension, event_id, int(effective['rating'] == 2), dumps(value)))
        return event_id, effective

    def latest(self, session):
        latest = {}
        for event in self.store.events(session, ['web_feedback']):
            payload = event['payload']
            latest[(event['track_id'], payload['dimension'])] = {
                **payload, 'effective': payload.get('effective') or interpret(payload['rating'], payload.get('reason', ''))}
        return list(latest.values())

    def revoke_feedback(self, session, event_id):
        events = self.store.events(session, ['web_feedback'])
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            raise ValueError('本任务没有该反馈事件')
        original = event['payload']
        latest = next(e for e in reversed(events) if e['track_id'] == event['track_id'] and
                      e['payload']['dimension'] == original['dimension'])
        if latest['id'] != event_id or original.get('revokes'):
            raise ValueError('这条反馈已有更新或已撤销；请撤销最新反馈')
        user_id = self.owner(session)
        # 撤销是新事件，原评分/原因仍可审计；不删除ever_confirmed候选记录。
        value = {**original, 'effective': {'rating': None, 'source': 'user_revoke', 'version': INTERPRETER_VERSION},
                 'revokes': event_id, 'reason': '用户撤销本条反馈', 'user_id': user_id}
        new_id = self.store.event('web_feedback', value, session, event['track_id'])
        with self.store.connect() as db:
            db.execute('UPDATE scene_memory SET event_id=?,payload=? WHERE user_id=? AND scene=? AND track_id=? AND dimension=?',
                       (new_id, dumps(value), user_id, original.get('scene', ''), event['track_id'], original['dimension']))
        return new_id

    def scene(self, user_id, scene):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM scene_memory WHERE user_id=? AND scene=?', (user_id, scene)).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    def recent(self, user_id):
        # 只读取推荐事件，避免每次排序反序列化四万条播放器原始采样。
        values = [e for e in self.store.events(kinds=['recommendation'])
                  if (e['payload']['user_id'] if 'user_id' in e['payload'] else self.owner(e['session'])) == user_id
                  and e['payload'].get('ids')]
        return {track_id for event in values[-3:] for track_id in event['payload']['ids']}

    def excluded(self, user_id):
        with self.store.connect() as db:
            rows = db.execute('SELECT track_id FROM user_exclusions WHERE user_id=?', (user_id,)).fetchall()
        return {row[0] for row in rows} | (self.store.excluded() if user_id == LOCAL_USER else set())

    def ban(self, user_id, track_id, active):
        with self.store.connect() as db:
            if active:
                db.execute('INSERT OR IGNORE INTO user_exclusions VALUES(?,?)', (user_id, track_id))
            else:
                db.execute('DELETE FROM user_exclusions WHERE user_id=? AND track_id=?', (user_id, track_id))
                if user_id == LOCAL_USER:
                    db.execute('DELETE FROM exclusions WHERE track_id=?', (track_id,))
        self.store.event('user_exclusion', {'user_id': user_id, 'active': active}, track_id=track_id)
