from lify.config import Settings
from lify.providers import Providers
from lify.models import merge_intent
from lify.store import Store


def test_plain_pop_does_not_gain_hard_vocals_or_lyrics(tmp_path):
    provider = Providers(Settings(data_dir=tmp_path), Store(tmp_path/'test.sqlite'))
    provider.json = lambda *args: {'updates':{'lyrics_query':'励志', 'vocal':'vocal', 'audio_query':'uplifting pop'}}
    value = merge_intent({}, provider.parse('流行乐鼓励我继续学习', {}))
    assert value.vocal == 'any' and not value.lyrics_query
    assert value.audio_query == 'uplifting pop'


def test_explicit_count_duration_semantics_override_wrong_model(tmp_path):
    provider = Providers(Settings(data_dir=tmp_path), Store(tmp_path/'test.sqlite'))
    provider.json = lambda *args: {'updates':{'count':10, 'count_mode':'approx','minutes':30,'duration_mode':'max'}}
    value = merge_intent({}, provider.parse('恰好10首，大约30分钟', {}))
    assert value.count_mode == 'exact' and value.duration_mode == 'approx'
    value = merge_intent({}, provider.parse('约10首，不超过30分钟', {}))
    assert value.count_mode == 'approx' and value.duration_mode == 'max'
    patch = provider.parse('10首，30分钟', {})
    assert patch.clarification
    assert merge_intent({}, patch).minutes == 30
