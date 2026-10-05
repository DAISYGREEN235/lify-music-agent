import concurrent.futures

import httpx
import pytest

from lify.catalog import parse_lrc
from lify.config import Settings
from lify.features import chunks
from lify.models import Intent, IntentPatch, merge_intent
from lify.player import PlaybackStats
from lify.providers import Budget, BudgetError, Providers
from lify.recommend import compose, eligible, validate_result
from lify.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'test.sqlite')


def track(tmp_path, i=0, duration=180):
    path = tmp_path / f'{i}.mp3'
    path.touch()
    return {'id': str(i), 'path': str(path), 'fingerprint': 'v1', 'title': f'歌曲{i}',
            'artist': f'艺人{i % 3}', 'duration': duration, 'language': None, 'vocal': None,
            'lyrics': {'text': '向着明天', 'source': 'test.lrc'}}


def test_lrc_keeps_translations_and_placeholders():
    parsed = parse_lrc('[00:01.20]hello\n[00:01.20]你好\n[ar:test]')
    assert [x['text'] for x in parsed['timed']] == ['hello', '你好']
    assert parse_lrc('[00:00.00]纯音乐，请欣赏')['text'] == ''
    assert parse_lrc('没有时间戳的歌词')['text']


def test_merge_preserves_and_clears_explicit_fields():
    old = Intent(minutes=30, duration_mode='max', exclude_artists=['A']).model_dump()
    new = merge_intent(old, IntentPatch(updates={'target_mood': '希望'}))
    assert new.minutes == 30 and new.exclude_artists == ['A']
    clear = merge_intent(old, IntentPatch(clear=['minutes']))
    assert clear.minutes is None and clear.duration_mode == 'none'
    with pytest.raises(ValueError):
        merge_intent(old, IntentPatch(updates={'invented': 3}))


def test_feedback_is_reversible_and_cache_invalidates(store, tmp_path):
    t = track(tmp_path)
    store.put_track(t)
    store.feedback('s', '0', 'ban')
    assert store.excluded() == {'0'}
    store.feedback('s', '0', 'unban')
    assert not store.excluded() and len(store.events()) == 2
    store.put_vector(t, 'audio', 'v1', [1, 0], {})
    assert store.vectors('audio', 'v1')
    store.put_track({**t, 'fingerprint': 'changed'})
    assert not store.vectors('audio', 'v1')


def test_budget_atomic_and_unconfirmed_holds(store):
    budget = Budget(store, 1)
    def reserve(_):
        try:
            return budget.reserve('x', .6, {})
        except BudgetError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(reserve, range(2)))
    assert len([i for i in ids if i]) == 1
    assert budget.summary()['held'] == .6
    budget.settle(next(i for i in ids if i), .1, {})
    assert budget.summary()['remaining'] == pytest.approx(.9)


def test_api_failure_retains_reservation_without_retry(store):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    settings = Settings(embedding_key='test', dimensions=2)
    provider = Providers(settings, store, httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match='503'):
        provider.embed(['歌词'])
    assert len(calls) == 1 and provider.budget.summary()['held'] > 0


def test_embedding_settles_actual_usage_and_validates_dimensions(store):
    def handler(request):
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}],
                                        'usage': {'prompt_tokens': 4, 'total_tokens': 4}})
    provider = Providers(Settings(embedding_key='test', dimensions=2), store, httpx.MockTransport(handler))
    assert provider.embed(['歌词']) == [[1, 0]]
    assert provider.budget.summary()['accounted'] == pytest.approx(.000002)
    assert provider.budget.summary()['held'] == 0


def test_chunks_conservative_bytes():
    text = '希望abc' * 2000
    parts = chunks(text)
    assert ''.join(parts) == text
    assert max(len(x.encode()) for x in parts) <= 2800


def test_exact_count_duration_and_missing_facts(store, tmp_path):
    candidates = []
    for i, length in enumerate([600, 100, 100, 100, 110]):
        t = track(tmp_path, i, length)
        store.put_track(t)
        candidates.append({'track': t, 'score': 1 / (i + 1), 'evidence': {}})
    intent = Intent(count=3, count_mode='exact', minutes=5, duration_mode='max')
    result = compose(candidates, intent)
    assert len(result['items']) == 3 and result['total_seconds'] == 300
    validate_result(result, store)
    assert not eligible(candidates[0]['track'], Intent(language='中文'), set())
    assert not eligible(candidates[0]['track'], Intent(vocal='instrumental'), set())
    store.feedback('s', result['items'][0]['track']['id'], 'ban')
    with pytest.raises(ValueError):
        validate_result(result, store)


def test_insufficient_results_do_not_relax_maximum(tmp_path):
    items = [{'track': track(tmp_path, i, 200), 'score': 1, 'evidence': {}} for i in range(4)]
    result = compose(items, Intent(count=4, count_mode='exact', minutes=5, duration_mode='max'))
    assert len(result['items']) == 1 and not result['complete']
    assert result['total_seconds'] <= 300


def test_playback_seek_pause_repeat_and_completion():
    stats = PlaybackStats(100)
    for second in range(11):
        stats.sample(second, second)
    stats.discontinuity()
    for second in range(10):
        stats.sample(90 + second, 11 + second)
    result = stats.result('eof')
    assert not result['complete'] and result['coverage'] == pytest.approx(.19)
    assert not result['early_skip']
    assert PlaybackStats(100).result('stop', 'next')['early_skip']
    assert not PlaybackStats(100).result('quit', 'exit')['early_skip']
    full = PlaybackStats(100)
    for second in range(82):
        full.sample(second, second)
    assert full.result('eof')['complete']
    full.discontinuity()
    for second in range(11):
        full.sample(second, second + 100)
    assert full.result('eof')['coverage'] == pytest.approx(.81)
    assert full.listened == pytest.approx(91)


def test_stalled_sampling_is_gap_not_listening():
    stats = PlaybackStats(200)
    stats.sample(0, 0)
    stats.sample(30, 30)
    assert stats.listened == 0 and stats.gaps == 1


def test_concurrent_session_writes_preserve_independent_state(store):
    def update(i):
        store.update_session('s', {str(i): {'value': i}})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(20)))
    assert len(store.load_session('s')) == 20


def test_changed_model_cannot_silently_reuse_old_prices(store):
    provider = Providers(Settings(embedding_model='unknown', embedding_key='test'), store)
    with pytest.raises(BudgetError, match='费率绑定'):
        provider.embed(['a'])
    assert provider.budget.summary()['requests'] == 0
