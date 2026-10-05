import pytest
from types import SimpleNamespace

from lify.labels import Labels
from lify.knowledge import build_track_card, validate_track_card
from lify.models import Intent
from lify.recommend import eligible
from lify.store import Store
from lify.tagging.audio_tags import AudioTagIndexer, load_audio_tag_profiles
from lify.tagging.language_profile import segment_starts, summarize_language_samples
from test_core import track


class LocalFeatures:
    """模拟本地CLAP接口；没有供应商依赖，测试不会发收费请求。"""
    def __init__(self, store):
        self.store = store
        self.calls = 0

    def version(self, route):
        return 'clap-test'

    def query(self, route, description):
        assert route == 'audio'
        self.calls += 1
        return [1., 0.]


def test_audio_tag_cache_resumes_and_invalidates_on_fingerprint(tmp_path, monkeypatch):
    store = Store(tmp_path / 'tags.sqlite')
    song = track(tmp_path)
    store.put_track(song)
    store.put_vector(song, 'audio', 'clap-test', [1., 0.], {'segments': [10, 50, 90]})
    features = LocalFeatures(store)
    indexer = AudioTagIndexer(store, features)
    assert indexer.index(progress=lambda _: None)['processed'] == 1
    profile = load_audio_tag_profiles(store)[song['id']]
    assert len(profile['raw_tags']) == 16
    assert not profile['calibrated']
    assert all(tag['confidence'] is None for tag in profile['product_tags'])
    assert all(tag['code'] == 'unknown' for tag in profile['product_tags'])  # 描述分数相同不硬选标签。
    calls = features.calls
    assert indexer.index(progress=lambda _: None)['processed'] == 0
    assert features.calls == calls
    from lify.tagging.audio_vocabulary import AUDIO_DESCRIPTORS
    first = AUDIO_DESCRIPTORS[0]
    monkeypatch.setattr('lify.tagging.audio_tags.AUDIO_DESCRIPTORS',
                        ((*first[:3], first[3] + ' Updated description.'), *AUDIO_DESCRIPTORS[1:]))
    revised = AudioTagIndexer(store, features)
    assert revised.version != indexer.version
    assert revised.index(progress=lambda _: None)['processed'] == 1  # 词表改变必须重建。
    applied = Labels(store).apply([song])[0]
    assert 'audio_tag_profile' in applied
    assert not eligible(applied, Intent(vocal='instrumental'), set())
    assert not eligible(applied, Intent(language='en'), set())
    store.put_track({**song, 'fingerprint': 'new'})
    assert load_audio_tag_profiles(store) == {}
    assert 'audio_tag_profile' not in Labels(store).apply([{**song, 'fingerprint': 'new'}])[0]


def test_audio_tags_without_audio_vectors_do_not_load_encoder(tmp_path):
    store = Store(tmp_path / 'tags.sqlite')
    store.put_track(track(tmp_path))
    features = LocalFeatures(store)
    assert AudioTagIndexer(store, features).index()['processed'] == 0
    assert features.calls == 0


def samples(languages, score=.95):
    return [{'start': i * 30, 'seconds': 20, 'language': language, 'probability': score}
            for i, language in enumerate(languages)]


def test_language_profile_preserves_mixed_and_uncertain_audio():
    assert summarize_language_samples(samples(['en'] * 4), True)['language'] == 'en'
    assert summarize_language_samples(samples(['en', 'en', 'en', 'unknown']), True)['language'] == 'en'
    assert summarize_language_samples(samples(['en', 'ja', 'en', 'en']), True)['language'] == 'mixed'
    assert summarize_language_samples(samples(['en', 'en', 'unknown', 'unknown']), True)['language'] == 'unknown'
    assert summarize_language_samples(samples(['en'] * 4), False)['language'] == 'unknown'
    assert summarize_language_samples(samples(['en'] * 4, .5), True)['language'] == 'unknown'
    assert summarize_language_samples(samples(['en'] * 4, float('nan')), True)['language'] == 'unknown'


def test_short_song_cannot_repeat_one_segment_to_fake_language_agreement():
    assert segment_starts(10) == [0.]
    assert summarize_language_samples(samples(['en']), True)['language'] == 'unknown'
    starts = segment_starts(180)
    assert len(starts) == 4 and len(set(starts)) == 4
    assert segment_starts(30) == [5.]
    assert all(b - a >= 20 for a, b in zip(segment_starts(60), segment_starts(60)[1:]))


def test_legacy_language_import_and_new_module_are_same_class():
    from lify.labels import LanguageIndexer
    from lify.tagging.language_index import LanguageIndexer as NewIndexer
    assert LanguageIndexer is NewIndexer


def test_tag_index_rejects_invalid_batch_limit(tmp_path):
    store = Store(tmp_path / 'tags.sqlite')
    with pytest.raises(ValueError):
        AudioTagIndexer(store, LocalFeatures(store)).index(limit=0)


def test_language_detector_decodes_distinct_samples_and_respects_vocal_gate(monkeypatch):
    import numpy as np
    from lify.tagging.language_index import LanguageIndexer

    decoded_starts = []
    def decode(command, **kwargs):
        decoded_starts.append(float(command[command.index('-ss') + 1]))
        return SimpleNamespace(stdout=np.zeros(16000, dtype='<f4').tobytes())

    monkeypatch.setattr('subprocess.run', decode)
    indexer = LanguageIndexer.__new__(LanguageIndexer)
    indexer.audio_evidence = {'song': ([], {'type_hint': 'vocal', 'type_margin': .1})}
    indexer.model = SimpleNamespace(detect_language=lambda **_: ('en', .95, []))
    song = {'id': 'song', 'path': 'song.mp3', 'duration': 180, 'lyrics': {}}
    detected = indexer.detect(song)
    assert detected['language'] == 'en'
    assert len(decoded_starts) == 4
    assert all(b - a >= 20 for a, b in zip(decoded_starts, decoded_starts[1:]))
    assert detected['language_evidence']['summary']['reliable_samples'] == 4
    indexer.audio_evidence = {}
    assert indexer.detect(song)['language'] == 'unknown'
    assert len(decoded_starts) == 4  # 缺乏人声证据时没有新解码或模型猜测。


def test_audio_tag_card_keeps_estimates_separate_and_rejects_tampering(tmp_path):
    store = Store(tmp_path / 'tags.sqlite')
    song = track(tmp_path)
    store.put_track(song)
    store.put_vector(song, 'audio', 'clap-test', [1., 0.], {})
    AudioTagIndexer(store, LocalFeatures(store)).index(progress=lambda _: None)
    item = {'track': Labels(store).apply([song])[0], 'evidence': {}}
    card = build_track_card(item)
    assert card['facts']['vocal'] == (song.get('vocal') or 'unknown')
    assert card['tag_estimates']['source'] == song['path']
    validate_track_card(card, item)
    card['tag_estimates']['raw_tags'][0]['score'] = .123
    with pytest.raises(ValueError):
        validate_track_card(card, item)
