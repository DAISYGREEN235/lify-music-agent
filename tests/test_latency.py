"""复现空候选仍调用选路模型与查询编码造成的无效等待。"""
from lify.config import Settings
from lify.models import IntentPatch
from lify.store import Store
from lify.workflow import Workflow


def test_empty_hard_filter_skips_paid_routing_and_query_encoding(tmp_path):
    store = Store(tmp_path / 'test.sqlite')
    path = tmp_path / 'song.mp3'
    path.touch()
    store.put_track({'id': 'unknown', 'title': 'A', 'artist': 'B', 'path': str(path),
                     'duration': 180, 'fingerprint': 'v1', 'language': None, 'vocal': None})
    calls = []
    class Providers:
        def parse(self, request, previous):
            return IntentPatch(updates={'language': 'zh', 'audio_query': 'relaxing pop'})

        def choose(self, *args):
            calls.append('paid_choose')
            return {'tool': 'audio', 'reason': 'test'}

    class Features:
        def available(self):
            return ['audio']

        def query(self, *args):
            calls.append('query_encoding')
            return [1.0]

        def version(self, route):
            return 'test'

    features = Features()
    features.store = store
    workflow = Workflow(Settings(data_dir=tmp_path), store, Providers(), features)
    try:
        result = workflow.run('s', request='relaxing Chinese pop')
        assert not result['items']
        assert calls == [], f'Hard filters already found zero tracks; wasted slow work: {calls}'
        assert any('硬条件' in w for w in result['warnings'])
    finally:
        workflow.close()
