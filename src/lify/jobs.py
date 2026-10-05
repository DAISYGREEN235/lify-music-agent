"""持久后台任务：HTTP先受理，单工作线程串行执行；重启只标记中断，不自动收费。"""
import json
import queue
import threading
import time

from .store import dumps


class Jobs:
    def __init__(self, store, execute):
        self.store, self.execute = store, execute
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.queue = queue.Queue()
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id TEXT,session TEXT,'
                       'status TEXT,stage TEXT,created REAL,updated REAL,payload TEXT,result TEXT,error TEXT)')
            db.execute("UPDATE jobs SET status='interrupted',error='服务已重启；点击恢复任务继续',updated=? "
                       "WHERE status IN ('queued','running')", (time.time(),))
        self.worker = threading.Thread(target=self._work, daemon=True, name='lify-recommendations')
        self.worker.start()

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('任务状态不存在')
        value = dict(row)
        value['result'] = json.loads(value['result']) if value['result'] else None
        value.pop('payload')
        value['elapsed_seconds'] = round((value['updated'] if value['status'] not in ('running', 'queued')
                                          else time.time()) - value['created'], 1)
        return value

    def submit(self, job_id, user_id, session, payload):
        with self.lock, self.store.connect() as db:
            old = db.execute('SELECT user_id,session FROM jobs WHERE id=?', (job_id,)).fetchone()
            if old:
                if old['user_id'] != user_id:
                    raise ValueError('任务不属于当前用户')
                return self.get(job_id)
            if self.stop.is_set():
                raise RuntimeError('服务正在停止，任务未受理')
            active = db.execute("SELECT id FROM jobs WHERE session=? AND status IN ('queued','running')",
                                (session,)).fetchone()
            if active:
                raise ValueError('本推荐任务已在后台执行，请等待结果')
            now = time.time()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (job_id, user_id, session, 'queued', 'starting', now, now, dumps(payload), None, None))
        self.queue.put(job_id)
        return self.get(job_id)

    def update(self, job_id, **changes):
        changes['updated'] = time.time()
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET '+','.join(f'{name}=?' for name in changes)+' WHERE id=?',
                       (*changes.values(), job_id))

    def resume_payload(self, session):
        """仅恢复尚未进入工作流的请求；已进入节点的任务由检查点恢复。"""
        with self.store.connect() as db:
            rows = db.execute("SELECT stage,payload FROM jobs WHERE session=? AND status='interrupted' "
                              'ORDER BY created DESC', (session,)).fetchall()
        for row in rows:
            value = json.loads(row['payload'])
            if value.get('mode') == 'resume':
                continue
            return value if row['stage'] == 'starting' else None
        return None

    def _work(self):
        while not self.stop.is_set():
            try:
                job_id = self.queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                if self.stop.is_set():
                    break
                self.update(job_id, status='running')
                with self.store.connect() as db:
                    row = db.execute('SELECT payload,session FROM jobs WHERE id=?', (job_id,)).fetchone()

                def report(stage):
                    # 节点之间检查停止；已完成节点已由LangGraph提交检查点。
                    if self.stop.is_set():
                        raise RuntimeError('服务已停止；已完成阶段保留，重启后手动恢复')
                    self.update(job_id, stage=stage)

                value = self.execute(row['session'], json.loads(row['payload']), report)
                self.update(job_id, status='completed', result=dumps(value), error=None)
            except Exception as exc:
                status = 'interrupted' if self.stop.is_set() else 'failed'
                self.update(job_id, status=status, error=str(exc))
                session = self.get(job_id)['session']
                self.store.update_session(session, {'last_error': str(exc)})
                self.store.event('web_message', {'role': 'error', 'text': str(exc), 'job_id': job_id}, session)
            finally:
                self.queue.task_done()

    def close(self):
        self.stop.set()
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted',error='服务停止；请手动恢复',updated=? "
                       "WHERE status IN ('queued','running')", (time.time(),))
