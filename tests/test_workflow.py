from lify.config import Settings
from lify.models import IntentPatch
from lify.store import Store
from lify.workflow import Workflow
from test_core import track


class FakeProvider:
    def __init__(self):
        self.parses = 0

    def parse(self, text, previous):
        self.parses += 1
        if '澄清' not in text:
            return IntentPatch(clarification='听感还是歌词？')
        return IntentPatch(updates={'audio_query': 'hopeful soft music', 'scene': '夜晚',
                                    'count': 1, 'count_mode': 'exact'})

    def choose(self, intent, available, error):
        return {'tool': 'audio', 'reason': '听感请求'}


class FakeFeatures:
    def __init__(self, store):
        self.store = store

    def version(self, route):
        return 'test'

    def available(self):
        return ['audio']

    def query(self, route, text):
        return [1, 0]


def test_two_clarification_rounds_then_waits_without_third_question(tmp_path):
    class AlwaysAmbiguous:
        def parse(self, text, previous):
            return IntentPatch(updates={'scene': '学习'}, clarification='听感还是歌词？')
    store = Store(tmp_path/'test.sqlite')
    settings = Settings(data_dir=tmp_path)
    first = Workflow(settings, store, AlwaysAmbiguous(), None)
    assert first.run('s', request='不要悲伤')['clarification_rounds'] == 1
    assert first.run('s', answer='不确定')['clarification_rounds'] == 2
    first.close()
    resumed = Workflow(settings, store, AlwaysAmbiguous(), None)
    try:
        value = resumed.run('s', answer='还是不确定')
        assert value['waiting_for_input'] and value['question'] is None
        assert value['intent']['scene'] == '学习'
        assert not store.load_session('s').get('result')
    finally:
        resumed.close()


def test_language_only_uses_catalog_without_paid_choose_or_embeddings(tmp_path):
    class LanguageProvider:
        def parse(self, text, previous):
            return IntentPatch(updates={'language': '英文', 'count': 1, 'count_mode': 'exact'})
        def choose(self, *args):
            raise AssertionError('纯筛选不该收费选路')
    store = Store(tmp_path/'test.sqlite')
    t = {**track(tmp_path), 'language': 'eng'}
    store.put_track(t)
    workflow = Workflow(Settings(data_dir=tmp_path), store, LanguageProvider(), None)
    try:
        result = workflow.run('s', request='英文一首')
        assert result['tool'] == 'catalog' and len(result['items']) == 1
        assert result['items'][0]['evidence']['catalog']['source'] == 'ID3 TLAN'
    finally:
        workflow.close()


def test_interrupt_survives_process_restart_without_repeating_parse(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    t = track(tmp_path)
    store.put_track(t)
    store.put_vector(t, 'audio', 'test', [1, 0], {'source': t['path']})
    provider = FakeProvider()
    settings = Settings(data_dir=tmp_path)
    first = Workflow(settings, store, provider, FakeFeatures(store))
    assert first.run('test', request='不要悲伤')['question'] == '听感还是歌词？'
    assert provider.parses == 1
    first.close()
    second = Workflow(settings, store, provider, FakeFeatures(store))
    result = second.run('test', answer='听感')
    assert result['items'][0]['track']['id'] == t['id']
    assert result['complete'] and provider.parses == 2
    assert not store.load_session('test').get('player')
    second.close()


def test_failed_retrieval_resumes_without_reparsing_or_rechoosing(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    t = track(tmp_path)
    store.put_track(t)
    store.put_vector(t, 'audio', 'test', [1, 0], {'source': t['path']})
    class FailingFeatures(FakeFeatures):
        def query(self, route, text):
            raise RuntimeError('模拟断网')
    provider = FakeProvider()
    settings = Settings(data_dir=tmp_path)
    workflow = Workflow(settings, store, provider, FailingFeatures(store))
    import pytest
    with pytest.raises(RuntimeError, match='模拟断网'):
        workflow.run('failed', request='用户澄清：听感')
    assert provider.parses == 1
    workflow.close()
    resumed = Workflow(settings, store, provider, FakeFeatures(store))
    assert resumed.run('failed')['complete']
    assert provider.parses == 1
    resumed.close()


def test_scene_feedback_never_leaks_into_other_scenes(tmp_path):
    from lify.models import Intent
    from lify.recommend import retrieve
    store = Store(tmp_path / 'store.sqlite')
    for i, vector in enumerate(([1, 0], [.9, .1])):
        t = track(tmp_path, i)
        store.put_track(t)
        store.put_vector(t, 'audio', 'test', vector, {})
    features = FakeFeatures(store)
    store.feedback('s', '0', 'unsuitable', scene='运动')
    first, _ = retrieve(store, features, Intent(scene='夜间休息', audio_query='quiet'), 'audio')
    assert first[0]['track']['id'] == '0'
    second, _ = retrieve(store, features, Intent(scene='运动', audio_query='quiet'), 'audio')
    assert second[0]['track']['id'] == '1'
