import pytest

from lify.knowledge import attach_track_cards, build_track_card, validate_track_card
from lify.models import Intent
from lify.recommend import compose
from test_core import track


def candidate(tmp_path):
    return {
        'track': track(tmp_path),
        'score': 1,
        'evidence': {
            'audio': {
                'source': '0.mp3', 'version': 'clap-v1', 'query': '安静学习',
                'similarity': .73, 'limitation': '听感估计，不是情绪概率',
            },
            'topic_audit': {
                'source': 'test.lrc', 'version': 'topic-v1', 'query': '不要绝望',
                'quote': '向着明天',
            },
        },
        'ranking_adjustments': [{'source': 'recent_3_playlists', 'factor': .9}],
    }


def test_track_card_claims_are_bound_to_source_version_and_track(tmp_path):
    item = candidate(tmp_path)
    card = build_track_card(item)
    assert card['track_id'] == '0'
    assert card['claims'][0]['evidence_ref'] == 'track:0:audio:clap-v1'
    assert card['ranking_adjustments'][0]['factor'] == .9
    assert validate_track_card(card, item) is card


def test_track_card_rejects_missing_source_and_invented_lyrics_quote(tmp_path):
    item = candidate(tmp_path)
    item['evidence']['audio']['source'] = ''
    with pytest.raises(ValueError, match='缺少来源或版本'):
        build_track_card(item)
    item = candidate(tmp_path)
    card = build_track_card(item)
    card['claims'][1]['quote'] = '歌词中不存在的句子'
    with pytest.raises(ValueError, match='歌词原文'):
        validate_track_card(card, item)


def test_compose_attaches_validated_track_cards(tmp_path):
    result = compose([candidate(tmp_path)], Intent(count=1, count_mode='exact'))
    card = result['items'][0]['knowledge_card']
    assert card['document_id'] == 'track_card:0:v1'
    assert card['facts']['title'] == '歌曲0'
    attach_track_cards(result)  # 幂等重建，不叠加旧内容。
    assert len(result['items'][0]['knowledge_card']['claims']) == 2


@pytest.mark.parametrize('field', ['facts', 'similarity', 'query', 'claims', 'ranking_adjustments', 'limitations'])
def test_card_rejects_tampered_or_removed_explanation(tmp_path, field):
    item = candidate(tmp_path)
    card = build_track_card(item)
    if field == 'facts':
        card['facts']['language'] = 'en'
    elif field in {'similarity', 'query'}:
        card['claims'][0][field] = .99 if field == 'similarity' else '另一条需求'
    else:
        card[field] = []
    with pytest.raises(ValueError, match='不一致'):
        validate_track_card(card, item)


@pytest.mark.parametrize('score', [float('nan'), float('inf'), float('-inf')])
def test_card_rejects_non_finite_similarity(tmp_path, score):
    item = candidate(tmp_path)
    item['evidence']['audio']['similarity'] = score
    with pytest.raises(ValueError, match='有限数值'):
        build_track_card(item)
