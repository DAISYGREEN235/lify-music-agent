from lify.store import Store
from lify.preferences import Preferences
from lify.models import Intent
from lify.recommend import retrieve
from test_core import track
from test_workflow import FakeFeatures


def test_recent_penalty_is_soft_and_user_scene_context_isolated(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    for i, vector in enumerate(([1, 0], [.99, .01])):
        t = track(tmp_path, i)
        store.put_track(t)
        store.put_vector(t, 'audio', 'test', vector, {})
    store.event('recommendation', {'ids':['0'], 'user_id':'a'}, 'prior')
    preferences = Preferences(store)
    store.save_session('s', {'user_id':'a', 'result':{'intent':{'scene':'学习'}}})
    preferences.feedback('s', '1', 2, 'sound', '严重不适合这个场景')
    intent = Intent(scene='休息', audio_query='quiet')
    first, _ = retrieve(store, FakeFeatures(store), intent, 'audio', 'a')
    assert first[0]['track']['id'] == '1'
    repeated = next(x for x in first if x['track']['id'] == '0')
    assert repeated['ranking_adjustments'][0]['source'] == 'recent_3_playlists'
    assert len(first) == 2  # 降权不删除候选
    other, _ = retrieve(store, FakeFeatures(store), intent, 'audio', 'b')
    assert other[0]['track']['id'] == '0'
    same_scene, _ = retrieve(store, FakeFeatures(store), Intent(scene='学习', audio_query='quiet'), 'audio', 'a')
    assert same_scene[0]['track']['id'] == '0'


def test_blank_scene_feedback_does_not_become_general_preference(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    for i, vector in enumerate(([1, 0], [.99, .01])):
        t = track(tmp_path, i)
        store.put_track(t)
        store.put_vector(t, 'audio', 'test', vector, {})
    store.save_session('s', {'user_id':'a', 'result':{'intent':{'scene':''}}})
    Preferences(store).feedback('s', '0', 0, 'sound', '不适合')
    result, _ = retrieve(store, FakeFeatures(store), Intent(audio_query='music'), 'audio', 'a')
    assert result[0]['track']['id'] == '0'
