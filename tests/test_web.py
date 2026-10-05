"""本地网页的文件权限、任务隔离与反馈持久化；不调用付费模型。"""
import pytest
import time
from fastapi.testclient import TestClient

from lify.config import Settings
from lify.store import Store
from lify.web import PlaybackEvent, WebPlayback, create_app
from lify.workflow import Workflow
from lify.models import Intent, IntentPatch


@pytest.fixture
def web(tmp_path):
    store = Store(tmp_path / 'test.sqlite')
    path = tmp_path / 'track.mp3'
    path.write_bytes(b'0123456789')
    track = dict(id='one', path=str(path), fingerprint='v1', title='Test', artist='A', duration=100)
    store.put_track(track)
    result = {'items': [{'track': track}], 'intent': {'scene': '休息'}, 'total_seconds': 100, 'warnings': []}
    store.save_session('s', {'result': result})
    class FakeWorkflow:
        def run(self, session, request=None, answer=None):
            if request == 'fail':
                raise RuntimeError('故障')
            store.event('test_request', {'request': request, 'answer': answer}, session)
            if request == 'ask':
                return {'question': '听感还是歌词？'}
            store.update_session(session, {'result': result})
            return result

        def close(self):
            pass

    app = create_app(Settings(data_dir=tmp_path), store, FakeWorkflow)
    client = TestClient(app, base_url='http://127.0.0.1')
    token = client.get('/api/state?session=s').json()['token']
    return client, store, token, track


def post(web, path, data):
    return web[0].post(path, json=data, headers={'X-Lify-Token': web[2]})


def test_readonly_policy_endpoint_has_sources_and_no_paid_calls(web):
    client, store, _, _ = web
    with store.connect() as db:
        before = db.execute('SELECT count(*) FROM costs').fetchone()[0]
    response = client.get('/api/policies', params={'q': '语言'})
    assert response.status_code == 200
    assert response.json()['documents'][0]['document_id'] == 'tag_taxonomy'
    assert len(response.json()['documents'][0]['content_hash']) == 64
    assert client.get('/api/policies', params={'q': 'x' * 501}).status_code == 400
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM costs').fetchone()[0] == before


def settle(web, response):
    assert response.status_code == 202
    until = time.monotonic() + 3
    while time.monotonic() < until:
        value = web[0].get('/api/progress/'+response.json()['id']).json()
        if value['status'] not in ('queued', 'running'):
            return value
        time.sleep(.01)
    raise AssertionError('任务未结束')


def test_audio_ranges_and_access_boundary(web):
    client, store, token, track = web
    assert '<audio' in client.get('/').text
    url = f'/audio/one?session=s&key={token}'
    response = client.get(url, headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206 and response.content == b'2345'
    assert client.get('/audio/one?session=another&key='+token).status_code == 404
    assert client.get('/audio/one?session=s&key=wrong').status_code == 403
    store.put_track({**track, 'fingerprint': 'changed'})
    assert client.get(url).status_code == 404
    assert client.post('/api/feedback', json={}).status_code == 403
    assert client.get('/api/state', headers={'Origin': 'https://other.example'}).status_code == 403


def test_feedback_does_not_ban_or_leak_to_other_tasks(web):
    data = dict(session='s', track_id='one', rating=0, dimension='topic', reason='题材不适合休息')
    assert post(web, '/api/feedback', data).status_code == 200
    assert not web[1].excluded()
    assert not web[1].events('another')
    assert settle(web, post(web, '/api/message', dict(session='s', mode='adjust')))['status'] == 'completed'
    request = [e for e in web[1].events('s') if e['kind'] == 'test_request'][-1]['payload']['request']
    assert '题材不适合休息' in request and 'topic' in request
    assert len([e for e in web[1].events('s') if e['kind'] == 'web_feedback']) == 1


def test_new_task_failure_and_clarification_can_be_restored(web):
    failed = post(web, '/api/message', dict(session='s', mode='new', text='fail'))
    assert settle(web, failed)['status'] == 'failed' and failed.json()['session'] != 's'
    response = settle(web, post(web, '/api/message', dict(session='s', mode='new', text='ask')))
    assert response['session'] != 's'
    state = web[0].get('/api/state?session='+response['session']).json()
    assert state['question'] == '听感还是歌词？'
    assert any(t['id'] == response['session'] for t in state['tasks'])


def test_request_progress_records_completion_and_failure(web):
    settle(web, post(web, '/api/message', dict(session='s', mode='new', text='ok', request_id='ok')))
    value = web[0].get('/api/progress/ok').json()
    assert value['status'] == 'completed' and value['elapsed_seconds'] >= 0
    assert settle(web, post(web, '/api/message', dict(session='s', mode='new', text='fail', request_id='bad')))['status'] == 'failed'
    assert web[0].get('/api/progress/bad').json()['status'] == 'failed'


def test_profiles_and_task_states_are_user_scoped(web):
    assert post(web, '/api/profile', {'user_id':'a', 'life_stage':'学生'}).status_code == 200
    assert not web[0].get('/api/state?user_id=b').json()['profile']
    response = post(web, '/api/message', dict(session='s', user_id='a', mode='new', text='ok', request_id='user-a'))
    task = settle(web, response)['session']
    assert web[0].get('/api/state?session='+task+'&user_id=b').status_code == 400
    assert not any(t['id'] == task for t in web[0].get('/api/state?user_id=b').json()['tasks'])
    assert post(web, '/api/message', dict(session=task, user_id='b', mode='modify', text='ok')).status_code == 400
    duplicate = post(web, '/api/message', dict(session='other', user_id='a', text='ok', request_id='user-a'))
    assert duplicate.json()['session'] == task


def test_concurrent_duplicate_submission_creates_one_task(web):
    from concurrent.futures import ThreadPoolExecutor
    payload = dict(session='s', mode='new', text='ok', request_id='duplicate')
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(web, '/api/message', payload), range(2)))
    assert responses[0].json()['session'] == responses[1].json()['session']
    settle(web, responses[0])
    assert len(web[1].events(kinds=['test_request'])) == 1
    with web[1].connect() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 2


def test_browser_seek_does_not_fake_completion(web, monkeypatch):
    player = WebPlayback(web[1])
    now = [0.0]
    monkeypatch.setattr('lify.web.time.monotonic', lambda: now[0])
    def event(name, position, active=True):
        player.record(PlaybackEvent(session='s', client='tab', track_id='one',
            event=name, position=position, active=active), web[3])
    event('start', 0)
    now[0] = 1
    event('sample', 1)
    event('seek', 99, False)
    now[0] = 2
    event('ended', 100, False)
    summary = web[1].events('s')[-1]['payload']
    assert summary['source'] == 'browser' and not summary['complete']
    assert summary['coverage'] == .01
    assert web[1].load_session('s')['browser_player']['position'] == 100


def test_task_exclusion_is_persistent_reversible_and_separate(tmp_path):
    store = Store(tmp_path / 'test.sqlite')
    class Provider:
        def parse(self, request, intent):
            return IntentPatch(clear=['exclude_tracks'])
    workflow = Workflow(Settings(data_dir=tmp_path), store, Provider(), None)
    config = {'configurable': {'thread_id': 'one'}}
    try:
        workflow.graph.update_state(config, {'intent': Intent().model_dump()}, as_node='compose')
        workflow.exclude_for_task('one', ['bad'])
        state = workflow.graph.get_state(config).values
        assert workflow.parse({**state, 'request': '调整'})['intent']['exclude_tracks'] == ['bad']
        assert not workflow.graph.get_state({'configurable': {'thread_id': 'two'}}).values
        workflow.exclude_for_task('one', [])
        assert not workflow.graph.get_state(config).values['intent']['exclude_tracks']
        assert not store.excluded()
    finally:
        workflow.close()
