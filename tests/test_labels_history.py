import json

from lify.labels import Labels, language_code
from lify.history import History
from lify.preferences import Preferences
from lify.store import Store
from test_core import track


def test_lyrics_translation_instrumental_unknown_not_english(tmp_path):
    from lify.models import Intent
    from lify.recommend import eligible
    store = Store(tmp_path/'test.sqlite')
    t = {**track(tmp_path), 'language': None, 'lyrics': {'text': 'English translation of Japanese lyrics'}}
    store.put_track(t)
    labels = Labels(store)
    assert not eligible(labels.apply([t])[0], Intent(language='en'), set())
    assert language_code('国语') == 'zh'
    assert language_code('English') == 'en'
    assert language_code('Japanese') == 'ja'
    labels.save(t['id'], 'ja', '已试听主要为日文演唱')
    assert eligible(labels.apply([t])[0], Intent(language='ja'), set())
    labels.save(t['id'], 'mixed', '已试听明显双语')
    assert not eligible(labels.apply([t])[0], Intent(language='en'), set())
    labels.save(t['id'], 'instrumental', '已试听纯器乐')
    assert not eligible(labels.apply([t])[0], Intent(language='en'), set())
    labels.revoke(t['id'])
    assert labels.apply([t])[0]['language'] is None
    labels.save(t['id'], 'en', '已试听')
    assert Labels(store).apply([{**t, 'fingerprint':'new'}])[0]['language'] is None


def test_paginated_history_export_and_consistent_backup(tmp_path):
    store = Store(tmp_path/'test.sqlite')
    preferences = Preferences(store)
    history = History(store, preferences)
    for i in range(45):
        store.event('web_message', {'role':'user','text':str(i)}, 's')
    store.event('web_message', {'text':'other'}, 'other')
    page = history.page('s')
    assert len(page['events']) == 40
    older = history.page('s', page['next_cursor'])
    assert len(older['events']) == 5 and older['next_cursor'] is None
    ids = [e['id'] for e in older['events']+page['events']]
    assert len(ids) == len(set(ids)) == 45
    assert 'other' not in json.dumps(history.export('s'))
    copy = Store(store.backup())
    assert len(copy.events('s')) == 45
