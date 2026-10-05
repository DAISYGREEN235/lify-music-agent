"""分页任务历史、可携带导出和显式问题报告，与歌曲反馈分开。"""
import json
import re
import time
import uuid
from pathlib import Path


class History:
    def __init__(self, store, preferences):
        self.store, self.preferences = store, preferences

    def page(self, session, before=None, limit=40):
        params = [session]
        condition = 'session=? AND kind IN (\'web_message\',\'web_feedback\',\'issue_report\')'
        if before is not None:
            condition += ' AND rowid<?'
            params.append(before)
        params.append(min(100, max(1, limit)))
        with self.store.connect() as db:
            rows = db.execute('SELECT rowid AS cursor,* FROM events WHERE '+condition+
                              ' ORDER BY rowid DESC LIMIT ?', params).fetchall()
        events = [{**dict(row), 'payload': json.loads(row['payload'])} for row in reversed(rows)]
        return {'events': events, 'next_cursor': events[0]['cursor'] if len(rows) == params[-1] else None}

    def export(self, session):
        value = self.store.load_session(session)
        # 显式白名单，避免未来配置/凭据字段意外进入导出。
        return {'version': '1.2.0a1', 'session': session, 'user_id': self.preferences.owner(session),
                'result': value.get('result'), 'pending_context': value.get('pending_context'),
                'events': self.store.events(session, ['web_message', 'web_feedback', 'issue_report', 'recommendation'])}

    def report(self, session, text):
        if not text.strip():
            raise ValueError('请填写问题现象')
        # 防止误贴常见密钥进入可提交的.scratch文档；不改变歌曲反馈。
        text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[已隐藏密钥]', text)
        folder = Path('.scratch/lify-v1-1/user-reports')
        folder.mkdir(parents=True, exist_ok=True)
        name = f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}.md'
        path = folder / name
        path.write_text(f'# 用户报告问题\n\nStatus: needs-triage\n\n任务：{session}\n\n'
                        f'## 现象\n\n{text}\n\n## Comments\n\n等待诊断。\n', encoding='utf-8')
        self.store.event('issue_report', {'text': text, 'path': str(path)}, session)
        return str(path)
