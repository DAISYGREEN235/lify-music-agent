"""候选检索、硬过滤、有限宽度组合搜索及最终校验。"""
from pathlib import Path
import numpy as np

from .models import Intent
from .labels import Labels, language_code
from .knowledge import attach_track_cards, validate_track_card
from .preferences import LOCAL_USER, Preferences


def eligible(track, intent, excluded):
    if track['id'] in excluded or not Path(track['path']).is_file():
        return False
    if track['duration'] <= 0:
        return False
    if any(x.casefold() in track['artist'].casefold() for x in intent.exclude_artists):
        return False
    if any(x == track['id'] or x.casefold() == track['title'].casefold() for x in intent.exclude_tracks):
        return False
    if intent.language and language_code(track.get('language')) != language_code(intent.language):
        return False
    if intent.vocal != 'any' and track.get('vocal') != intent.vocal:
        return False
    return True


def retrieve(store, features, intent: Intent, tool, user_id=LOCAL_USER):
    routes = ['audio', 'text'] if tool == 'both' else [tool]
    preferences = Preferences(store)
    excluded = preferences.excluded(user_id)
    tracks = {t['id']: t for t in Labels(store).apply(store.tracks()) if eligible(t, intent, excluded)}
    if not tracks:
        return [], {'eligible': 0}
    scored, coverage = {}, {'eligible': len(tracks)}
    if tool == 'catalog':
        # 纯语言/数量筛选不需要假造听感query，也不强求两类向量证据。
        scored = {track_id: {'track': track, 'score': 1., 'evidence': {'catalog': {
            'source': track.get('language_evidence', {}).get('source') or
                      ('ID3 TLAN' if track.get('language') else track['path']),
            'query': intent.language or '曲库筛选', 'similarity': 1., 'version': 'language-v1'}}}
            for track_id, track in tracks.items()}
        routes = []
    for route in routes:
        text = intent.audio_query if route == 'audio' else intent.lyrics_query
        if not text:
            text = intent.target_mood + ' ' + intent.scene
        if not text.strip() and route == 'text' and intent.negative_lyrics:
            text = '歌曲歌词'
        if not text.strip():
            continue
        query = np.asarray(features.query(route, text))
        entries = features.store.vectors(route, features.version(route))
        coverage[route] = len(entries)
        negatives = intent.negative_preferences if route == 'audio' else intent.negative_lyrics
        negative = [np.asarray(features.query(route, x)) for x in negatives]
        ranking = []
        for track_id, (vector, evidence) in entries.items():
            if track_id not in tracks:
                continue
            similarity = float(np.dot(query, vector))
            negative_score = max([float(np.dot(n, vector)) for n in negative], default=0)
            ranking.append((similarity - max(0, negative_score) * .4, track_id, similarity, evidence))
        ranking.sort(reverse=True, key=lambda r: r[0])
        for rank, (_, track_id, similarity, evidence) in enumerate(ranking, 1):
            item = scored.setdefault(track_id, {'track': tracks[track_id], 'score': 0.0, 'evidence': {}})
            weight = (2 if route == 'text' and intent.lyrics_query else 1)
            item['score'] += weight / (60 + rank)
            item['evidence'][route] = {**evidence, 'similarity': similarity,
                                       'version': features.version(route), 'query': text}
    # 两个明确要求并存时，候选必须同时有相应证据。
    required = ({'audio'} if intent.audio_query else set()) | (
        {'text'} if intent.lyrics_query or intent.negative_lyrics else set())
    candidates = [x for x in scored.values() if required <= x['evidence'].keys()]
    scene_feedback = {}
    for event in store.events(kinds=['feedback']):
        p = event['payload']
        if (preferences.owner(event['session']) == user_id and p.get('scene') == intent.scene and intent.scene):
            scene_feedback[event['track_id']] = p
    recent = preferences.recent(user_id)
    memory = {}
    for row in (preferences.scene(user_id, intent.scene) if intent.scene else []):
        effective = row['payload']['effective']['rating']
        if effective is not None:
            memory.setdefault(row['track_id'], []).append(effective)
    for item in candidates:
        feedback = scene_feedback.get(item['track']['id'], {})
        if feedback.get('action') in ('dislike', 'unsuitable') or feedback.get('rating') == 0:
            item['score'] *= .5
            item['scene_feedback'] = '同场景负反馈降权'
        elif feedback.get('action') == 'like' or feedback.get('rating') == 2:
            item['score'] *= 1.1
            item['scene_feedback'] = '同场景正反馈加权'
        ratings = memory.get(item['track']['id'], [])
        item['ranking_adjustments'] = []
        if ratings:
            factor = .5 if 0 in ratings else 1.1 if all(r == 2 for r in ratings) else 1.
            item['score'] *= factor
            item['ranking_adjustments'].append({'source': 'user_scene_feedback', 'factor': factor})
        if item['track']['id'] in recent:
            # 轻微软降权；硬约束/场景证据仍优先，不保证交集为0。
            item['score'] *= .9
            item['ranking_adjustments'].append({'source': 'recent_3_playlists', 'factor': .9})
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates, coverage


def compose(candidates, intent: Intent):
    count_min = intent.count
    count_max = intent.count if intent.count_mode == 'exact' else min(100, intent.count + 5)
    target_seconds = intent.minutes * 60 if intent.minutes else None
    upper = (target_seconds * (1.1 if intent.duration_mode == 'approx' else 1)
             if target_seconds else float('inf'))
    lower = target_seconds * .9 if intent.duration_mode == 'approx' else 0
    # 每个状态：(已选候选下标, 时长, 分数)。按数量及分钟桶保留多条路径，避免纯贪心卡住。
    beams = {0: [((), 0.0, 0.0)]}
    for index, item in enumerate(candidates[:200]):
        duration = item['track']['duration']
        for count in range(min(count_max - 1, index), -1, -1):
            additions = [(ids + (index,), seconds + duration, score + item['score'])
                         for ids, seconds, score in beams.get(count, []) if seconds + duration <= upper]
            pool = beams.get(count + 1, []) + additions
            buckets = {}
            for state in sorted(pool, key=lambda x: -x[2]):
                bucket = int(state[1] / 15) if target_seconds else 0
                if len(buckets.setdefault(bucket, [])) < 3:
                    buckets[bucket].append(state)
            beams[count + 1] = sorted([s for b in buckets.values() for s in b],
                                     key=lambda s: (abs(s[1] - target_seconds) if target_seconds else 0,
                                                    -s[2]))[:96]
    valid = [state for count, states in beams.items() if count_min <= count <= count_max
             for state in states if state[1] >= lower]
    complete = bool(valid)
    if valid:
        chosen = min(valid, key=lambda s: (abs(s[1] - target_seconds) if target_seconds else len(s[0]), -s[2]))
    else:
        pool = [s for states in beams.values() for s in states]
        chosen = min(pool, key=lambda s: (-len(s[0]), abs(s[1] - target_seconds) if target_seconds else -s[2]))
    selected = [candidates[i] for i in chosen[0]]
    warnings = []
    if not complete:
        if len(candidates) < count_min:
            warnings.append(f'满足已确认条件且具备所需证据的候选仅{len(candidates)}首，少于请求的{count_min}首；'
                            '返回部分结果，没有把未知或不合适的歌曲补入。')
        else:
            warnings.append('有限组合搜索未找到同时满足数量和时长的歌单；返回部分结果，未放宽时长上限。')
    if intent.mix_types:
        types = {x['evidence'].get('audio', {}).get('type_hint') for x in selected}
        if len(selected) >= 2 and len(types & {'vocal', 'instrumental'}) == 1:
            missing = ({'vocal', 'instrumental'} - types).pop()
            selected_ids = {x['track']['id'] for x in selected}
            # 只在同一组场景候选中尝试近分替换；不能为凑类型突破数量、时长。
            for candidate in candidates:
                if (candidate['track']['id'] in selected_ids
                        or candidate['evidence'].get('audio', {}).get('type_hint') != missing):
                    continue
                replace = min(range(len(selected)), key=lambda i: selected[i]['score'])
                old = selected[replace]
                seconds = sum(x['track']['duration'] for x in selected) - old['track']['duration'] + candidate['track']['duration']
                if candidate['score'] >= old['score'] * .9 and lower <= seconds <= upper:
                    selected[replace] = candidate
                    types.add(missing)
                    break
        if not {'vocal', 'instrumental'} <= types:
            warnings.append('当前结果未覆盖人声与器乐两类；类型仅为模型估计，未牺牲场景排序强行补歌。')
    # 相邻同艺人优先错开，不改变集合、时长和硬约束。
    ordered = []
    while selected:
        pos = next((i for i, x in enumerate(selected)
                    if not ordered or x['track']['artist'] != ordered[-1]['track']['artist']), 0)
        ordered.append(selected.pop(pos))
    if any(a['track']['artist'] == b['track']['artist'] for a, b in zip(ordered, ordered[1:])):
        warnings.append('候选艺人分布有限，仍有同艺人相邻。')
    if intent.negative_preferences:
        warnings.append('听感负向条件通过模型相似度降权；尚未完成听感标注验证，不保证稳定满足。')
    if intent.negative_lyrics:
        warnings.append('歌词负向主题通过文本相似度降权，尚未形成可保证排除的主题分类器。')
    total = sum(x['track']['duration'] for x in ordered)
    return attach_track_cards({'items': ordered, 'total_seconds': total, 'complete': complete,
            'warnings': warnings, 'duration_deviation': total / target_seconds - 1 if target_seconds else None,
            'intent': intent.model_dump()})


def validate_result(result, store, user_id=LOCAL_USER):
    intent = Intent.model_validate(result['intent'])
    current = {t['id']: t for t in Labels(store).apply(store.tracks())}
    ids = [x['track']['id'] for x in result['items']]
    exclusions = Preferences(store).excluded(user_id)
    if len(ids) != len(set(ids)):
        raise ValueError('歌单包含重复歌曲')
    for item in result['items']:
        track = current.get(item['track']['id'])
        if (not track or track['fingerprint'] != item['track']['fingerprint'] or
                not eligible(track, intent, exclusions)):
            raise ValueError('歌曲状态已变化或违反硬约束，需要重新生成')
        validate_track_card(item.get('knowledge_card', {}), item)
    total = sum(current[i]['duration'] for i in ids)
    upper = intent.minutes * 60 * (1.1 if intent.duration_mode == 'approx' else 1) if intent.minutes else None
    if upper is not None and total > upper + 1e-6:
        raise ValueError('超过时长上限')
    maximum = intent.count + (5 if intent.count_mode == 'approx' else 0)
    if len(ids) > maximum:
        raise ValueError('超过数量上限')
    if result['complete'] and len(ids) < intent.count:
        raise ValueError('不能把数量不足标记为完整结果')
    if result['complete'] and intent.duration_mode == 'approx' and total < intent.minutes * 54:
        raise ValueError('不能把低于时长容差的结果标记为完整')
    return result
