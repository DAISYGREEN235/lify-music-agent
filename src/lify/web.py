"""V1.1 网页内试听试用版：127.0.0.1 服务，浏览器播放，不启动隐藏 mpv。"""
import argparse
import json
import secrets
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .features import Features
from .player import PlaybackStats
from .providers import Providers
from .store import Store
from .preferences import LOCAL_USER, Preferences
from .labels import Labels
from .jobs import Jobs
from .history import History


class Message(BaseModel):
    user_id: str = Field(default=LOCAL_USER, min_length=1, max_length=100)
    session: str = Field(min_length=1, max_length=100)
    text: str = Field(default='', max_length=8000)
    mode: Literal['new', 'modify', 'answer', 'resume', 'adjust'] = 'new'
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)


class Feedback(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    track_id: str
    rating: Literal[0, 1, 2]
    dimension: Literal['overall', 'topic', 'sound'] = 'overall'
    reason: str = Field(default='', max_length=2000)


class Profile(BaseModel):
    user_id: str = Field(default=LOCAL_USER, min_length=1, max_length=100)
    life_stage: str = Field(default='', max_length=200)
    common_scenes: list[str] = Field(default_factory=list, max_length=30)
    stable_preferences: list[str] = Field(default_factory=list, max_length=30)
    delete: bool = False


class Report(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


class Label(BaseModel):
    track_id: str
    language: str = 'unknown'
    source: str = Field(default='', max_length=1000)
    revoke: bool = False


class Exclusion(BaseModel):
    user_id: str = LOCAL_USER
    track_id: str
    active: bool


class RevokeFeedback(BaseModel):
    session: str
    event_id: str


class PlaybackEvent(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    client: str = Field(min_length=1, max_length=100)
    track_id: str
    event: Literal['start', 'sample', 'pause', 'seek', 'ended', 'next', 'select', 'close', 'error']
    position: float = Field(ge=0, allow_inf_nan=False)
    active: bool = False
    speed: float = Field(default=1, gt=0, le=16, allow_inf_nan=False)


class WebPlayback:
    def __init__(self, store):
        self.store, self.entries, self.lock = store, {}, threading.RLock()

    def record(self, event, track):
        key = (event.session, event.client)
        with self.lock:
            now = time.monotonic()
            entry = self.entries.get(key)
            if event.event == 'start':
                if entry:
                    self._finish(event.session, entry, 'replaced')
                stats = PlaybackStats(track['duration'])
                saved = self.store.load_session(event.session).get('browser_player', {})
                if saved.get('track_id') == event.track_id and abs(saved.get('position', 0) - event.position) < 3:
                    previous = saved.get('stats') or {}
                    stats.intervals = previous.get('intervals', [])
                    stats.listened = previous.get('listened', 0)
                    stats.gaps = previous.get('gaps', 0)
                    if saved.get('running'):
                        stats.gaps += 1
                        self.store.event('collection_gap', {'source': 'browser', 'cause': 'page_or_service_restart'},
                                         event.session, event.track_id)
                entry = {'track_id': event.track_id, 'stats': stats, 'last': now}
                self.entries[key] = entry
            if not entry or entry['track_id'] != event.track_id:
                raise ValueError('播放记录尚未开始或歌曲已切换')
            stats = entry['stats']
            if now - entry['last'] > 5:
                stats.discontinuity()
                stats.gaps += 1
                self.store.event('collection_gap', {'source': 'browser', 'cause': 'heartbeat_gap'},
                                 event.session, event.track_id)
            entry['last'] = now
            self.store.event('browser_raw', event.model_dump(), event.session, event.track_id)
            if event.event == 'seek':
                stats.discontinuity()
            else:
                stats.sample(min(event.position, track['duration']), now, event.active, event.speed)
            stopped = event.event in ('ended', 'next', 'select', 'close', 'error')
            if stopped:
                reason = {'ended': 'eof', 'next': 'stop', 'select': 'stop', 'close': 'quit', 'error': 'error'}[event.event]
                self._finish(event.session, entry, reason, 'next' if event.event == 'next' else event.event)
                self.entries.pop(key, None)
            self.store.update_session(event.session, {'browser_player': {
                'track_id': event.track_id, 'position': event.position, 'running': not stopped,
                'stats': {'intervals': stats.intervals, 'listened': stats.listened, 'gaps': stats.gaps}}})
        return {'saved': True}

    def _finish(self, session, entry, reason, action=None):
        self.store.event('playback_summary', {**entry['stats'].result(reason, action), 'source': 'browser'},
                         session, entry['track_id'])


def create_app(settings=None, store=None, workflow_factory=None):
    settings = settings or Settings.load()
    store = store or Store(settings.data_dir / 'lify.sqlite')
    providers = Providers(settings, store)
    features = Features(settings, store, providers)
    playback = WebPlayback(store)
    token = secrets.token_urlsafe(32)
    submission_lock = threading.RLock()
    preferences, labels = Preferences(store), Labels(store)
    history = History(store, preferences)

    def execute(session, payload, report):
        body = Message.model_validate(payload)
        if body.mode == 'resume':
            original = jobs.resume_payload(session)
            if original:
                body = Message.model_validate({**original, 'session': session,
                                               'user_id': body.user_id, 'request_id': body.request_id})
        workflow = None
        try:
            if workflow_factory:
                workflow = workflow_factory()
            else:
                from .workflow import Workflow
                workflow = Workflow(settings, store, providers, features)
            workflow.on_progress = report
            text = body.text
            if body.mode == 'adjust':
                latest = preferences.latest(session)
                if not latest:
                    raise ValueError('本任务尚未保存反馈')
                text += '\n本任务反馈：文字解释优先，题材与听感分开；保留其他条件，不设永久排除：\n'
                text += json.dumps([p for p in latest if not p.get('revokes')], ensure_ascii=False)
                if hasattr(workflow, 'exclude_for_task'):
                    workflow.exclude_for_task(session, [p['track_id'] for p in latest
                                                       if p['effective']['rating'] == 0])
            if body.mode == 'answer':
                result = workflow.run(session, answer=body.text)
            else:
                result = workflow.run(session, request=None if body.mode == 'resume' else text)
            store.update_session(session, {'pending_question': result.get('question'), 'last_error': None})
            store.event('web_message', {'role': 'assistant', 'text': result.get('question') or
                        ('仍有歧义，等待你主动修改条件' if result.get('waiting_for_input') else
                         f'已生成 {len(result.get("items", []))} 首')}, session)
            return result
        finally:
            if workflow:
                workflow.close()

    jobs = Jobs(store, execute)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

    app = FastAPI(title='Lify 网页内试听', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def protect(request: Request, call_next):
        origin = request.headers.get('origin')
        expected = str(request.base_url).rstrip('/')
        if origin and origin != expected:
            return JSONResponse({'detail': '仅允许当前本地页面访问'}, status_code=403)
        if request.method == 'POST':
            if request.headers.get('x-lify-token') != token:
                return JSONResponse({'detail': '页面已失效，请刷新后重试'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def failed_operation(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    def allowed_track(session, track_id):
        result = store.load_session(session).get('result') or {}
        item = next((x for x in result.get('items', []) if x['track']['id'] == track_id), None)
        current = next((t for t in store.tracks() if t['id'] == track_id), None)
        if not item or not current or current['fingerprint'] != item['track']['fingerprint']:
            raise HTTPException(404, '歌曲不在当前有效歌单，或文件状态已变化')
        if track_id in preferences.excluded(preferences.owner(session)) or not Path(current['path']).is_file():
            raise HTTPException(404, '歌曲已排除或文件不可用')
        return current

    @app.get('/')
    def page():
        return FileResponse(Path(__file__).parent / 'static' / 'index.html', media_type='text/html')

    @app.get('/ping')
    def ping():
        return {'app': 'lify-web-v1.1', 'version': '1.2.0a1'}

    @app.post('/api/shutdown')
    def shutdown():
        if not hasattr(app.state, 'stop_server'):
            raise HTTPException(409, '当前启动方式不支持网页停止服务')
        jobs.close()
        app.state.stop_server()
        return {'stopped': True}

    @app.get('/app.js')
    def javascript():
        return FileResponse(Path(__file__).parent / 'static' / 'app.js', media_type='text/javascript')

    @app.get('/api/state')
    def state(session: str = 'default', user_id: str = LOCAL_USER):
        if store.load_session(session) and preferences.owner(session) != user_id:
            raise ValueError('任务不属于当前用户')
        with store.connect() as db:
            rows = db.execute('SELECT id,payload FROM sessions ORDER BY rowid DESC LIMIT 50').fetchall()
        tasks = []
        for row in rows:
            payload = json.loads(row['payload'])
            if payload.get('user_id', LOCAL_USER) != user_id:
                continue
            result = payload.get('result') or {}
            pending = payload.get('pending_question') or payload.get('pending_context')
            if result or pending or payload.get('last_error') or payload.get('job_id'):
                tasks.append({'id': row['id'], 'scene': result.get('intent', {}).get('scene') or '已有歌单',
                              'count': len(result.get('items', []))})
        value = store.load_session(session)
        events = history.page(session)['events']
        job = jobs.get(value['job_id']) if value.get('job_id') else None
        return {'session': session, 'result': value.get('result'), 'player': value.get('browser_player'),
                'question': value.get('pending_question'),
                'pending_context': value.get('pending_context'), 'job': job,
                'tasks': tasks, 'events': events, 'token': token, 'budget': providers.budget.summary(),
                'profile': preferences.profile(user_id), 'language_coverage': labels.coverage(),
                'exclusions': sorted(preferences.excluded(user_id))}

    @app.get('/api/policies')
    def policies(q: str = ''):
        from .policies import retrieve_policies
        if len(q) > 500:
            raise ValueError('策略查询最多500字')
        return retrieve_policies(q)

    @app.get('/api/progress/{request_id}')
    def progress(request_id: str):
        return jobs.get(request_id)

    @app.get('/audio/{track_id}')
    def audio(track_id: str, session: str, key: str):
        if not secrets.compare_digest(key, token):
            raise HTTPException(403, '请从本地歌单页面播放')
        track = allowed_track(session, track_id)
        # FileResponse支持Range请求，浏览器拖动进度无需下载完整MP3。
        return FileResponse(track['path'], media_type='audio/mpeg')

    @app.post('/api/message', status_code=202)
    def message(body: Message):
        if not body.text.strip() and body.mode not in ('resume', 'adjust'):
            raise ValueError('请填写需求或澄清回答')
        with submission_lock:
            try:
                old = jobs.get(body.request_id)
            except ValueError:
                old = None
            if old:
                if old['user_id'] != body.user_id:
                    raise ValueError('任务不属于当前用户')
                return old
            session = 'web-' + uuid.uuid4().hex if body.mode == 'new' else body.session
            preferences.claim(session, body.user_id)
            store.event('web_message', {'role': 'user', 'text': body.text, 'mode': body.mode,
                                      'user_id': body.user_id}, session, event_id='message-'+body.request_id)
            try:
                job = jobs.submit(body.request_id, body.user_id, session, body.model_dump())
            except (ValueError, RuntimeError) as exc:
                store.event('web_message', {'role': 'error', 'text': str(exc)}, session)
                raise
            store.update_session(session, {'job_id': body.request_id})
            return job

    @app.post('/api/profile')
    def profile(body: Profile):
        preferences.save_profile(body.user_id, {} if body.delete else body.model_dump(exclude={'user_id', 'delete'}))
        return {'saved': True, 'profile': preferences.profile(body.user_id)}

    @app.post('/api/exclusion')
    def exclusion(body: Exclusion):
        if body.track_id not in {t['id'] for t in store.tracks()}:
            raise ValueError('歌曲不在曲库')
        preferences.ban(body.user_id, body.track_id, body.active)
        return {'saved': True}

    @app.post('/api/label')
    def label(body: Label):
        if body.revoke:
            labels.revoke(body.track_id)
        else:
            labels.save(body.track_id, body.language, body.source)
        return {'saved': True, 'coverage': labels.coverage()}

    @app.get('/api/history')
    def task_history(session: str, user_id: str = LOCAL_USER, before: int | None = None):
        if preferences.owner(session) != user_id:
            raise ValueError('任务不属于当前用户')
        return history.page(session, before)

    @app.get('/api/export')
    def export(session: str, user_id: str = LOCAL_USER):
        if preferences.owner(session) != user_id:
            raise ValueError('任务不属于当前用户')
        return JSONResponse(history.export(session), headers={'Content-Disposition': 'attachment; filename="lify-task.json"'})

    @app.post('/api/report')
    def report_issue(body: Report):
        return {'saved': True, 'path': history.report(body.session, body.text)}

    @app.post('/api/feedback')
    def feedback(body: Feedback):
        allowed_track(body.session, body.track_id)
        event_id, effective = preferences.feedback(body.session, body.track_id, body.rating, body.dimension, body.reason)
        return {'saved': True, 'event_id': event_id, 'effective': effective}

    @app.post('/api/feedback/revoke')
    def revoke_feedback(body: RevokeFeedback):
        return {'saved': True, 'event_id': preferences.revoke_feedback(body.session, body.event_id)}

    @app.post('/api/playback')
    def observed(body: PlaybackEvent):
        return playback.record(body, allowed_track(body.session, body.track_id))

    return app


def main():
    parser = argparse.ArgumentParser(description='Lify V1.1 网页内试听试用版')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError('端口应在1024至65535之间')
    import uvicorn
    import sys
    import httpx
    url = f'http://127.0.0.1:{args.port}'
    try:
        if httpx.get(url + '/ping', timeout=1).json().get('app') == 'lify-web-v1.1':
            if args.open:
                webbrowser.open(url)
            return
    except (httpx.HTTPError, ValueError):
        pass
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    # pythonw没有控制台；日志另由启动器捕获，避免默认控制台日志器失败。
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
        log_config=None if sys.stdout is None else uvicorn.config.LOGGING_CONFIG, access_log=False))
    app.state.stop_server = lambda: setattr(server, 'should_exit', True)
    server.run()


if __name__ == '__main__':
    main()
