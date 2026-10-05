"""有标签才计算质量；外部播放器原始事件不等价于用户喜欢。"""
import math


def ranking_metrics(ranked_ids, relevance, k=10):
    """relevance 为固定评测池全量标签 {歌曲ID:0/1/2}，缺失标签不当成负例。"""
    top = ranked_ids[:k]
    if k < 1 or any(v not in (0, 1, 2) for v in relevance.values()):
        raise ValueError('K必须为正数，相关性标签必须为0/1/2')
    if not top or any(i not in relevance for i in top):
        raise ValueError('前K结果缺少标签；请补齐固定评测池标注')
    if len(set(ranked_ids)) != len(ranked_ids):
        raise ValueError('排序含重复ID')
    positives = sum(v > 0 for v in relevance.values())
    found = sum(relevance[i] > 0 for i in top)
    dcg = sum((2 ** relevance[i] - 1) / math.log2(p + 2) for p, i in enumerate(top))
    ideal = sum((2 ** v - 1) / math.log2(p + 2)
                for p, v in enumerate(sorted(relevance.values(), reverse=True)[:len(top)]))
    return {'precision_at_k': found / len(top), 'recall_within_labeled_pool': found / positives if positives else None,
            'ndcg_at_k': dcg / ideal if ideal else None, 'k_requested': k, 'k_actual': len(top),
            'label_pool_size': len(relevance)}


def event_metrics(events):
    summaries = [e['payload'] for e in events if e['kind'] == 'playback_summary']
    recs = [e['payload'] for e in events if e['kind'] == 'recommendation']
    return {'recommendation_runs': len(recs), 'playback_segments': len(summaries),
            'observed_complete_segments': sum(s['complete'] for s in summaries),
            'explicit_early_skips': sum(s['early_skip'] for s in summaries),
            'collection_gaps': sum(e['kind'] == 'collection_gap' for e in events),
            'feedback_events': sum(e['kind'] == 'feedback' for e in events),
            'note': '恢复播放可能拆成多个片段；这些是事件计数，不是无偏完播率或满意度。'}
