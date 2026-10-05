from lify.suitability import audit_topics


def test_unknown_missing_lyrics_and_invented_citations_are_not_filled():
    candidates = [{'track': {'id': str(i), 'lyrics': {'text': '真实歌词'}}, 'evidence': {}} for i in range(3)]
    class Provider:
        def check_topics(self, forbidden, tracks):
            return {'decisions': [{'id':'0', 'verdict':'allowed', 'quote':'真实'},
                                  {'id':'1', 'verdict':'allowed', 'quote':'编造'},
                                  {'id':'2', 'verdict':'blocked', 'quote':'真实歌词'}]}
    result, report = audit_topics(candidates, ['禁止题材'], Provider())
    assert [item['track']['id'] for item in result] == ['0']
    assert report['audited'] == 3 and report['accepted'] == 1
    assert not report['decisions'][1]['accepted']


def test_verified_instrumental_does_not_carry_invented_lyrics(tmp_path):
    from test_core import track
    from lify.knowledge import attach_track_cards
    t = {**track(tmp_path), 'vocal': 'instrumental', 'lyrics': {'text': ''},
         'language_evidence': {'source': '人工试听整首'}}
    class Provider:
        def check_topics(self, *args):
            return {'decisions': [{'id': t['id'], 'verdict': 'allowed', 'quote': '编造的歌词'}]}
    items, _ = audit_topics([{'track': t, 'evidence': {}}], ['悲伤'], Provider())
    assert items[0]['evidence']['topic_audit']['quote'] == ''
    assert items[0]['evidence']['topic_audit']['source'] == '人工试听整首'
    attach_track_cards({'items': items})
