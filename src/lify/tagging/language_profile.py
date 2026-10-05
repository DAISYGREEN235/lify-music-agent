"""语言证据汇总纯函数；不把歌词字符或片段数量当作整首语言真值。"""
from collections import Counter
import math


def segment_starts(duration, seconds=20):
    """最多四个不重叠片段；短曲减少采样数，避免同段人声重复投票。"""
    if not math.isfinite(duration) or duration < 0 or seconds <= 0:
        raise ValueError('duration 必须为非负有限数，seconds 必须大于0')
    count = min(4, max(1, int(duration // seconds)))
    # 每个等宽时间区间取中心20秒；片段间距至少20秒，短于20秒只取一次。
    interval = duration / count
    return [round(max(0., (index + .5) * interval - seconds / 2), 2) for index in range(count)]


def summarize_language_samples(samples, vocal_supported, threshold=.85):
    """输入带 start/seconds/language/probability 的列表，输出保守语言估计。

    probability 是模型 token 分数；这里只做启发式一致性判断，不是校准置信度。
    两种可靠语言才标 mixed；一种可靠语言需覆盖至少两段且至少3/4的片段。
    """
    reliable = []
    for sample in samples:
        score = sample.get('probability', 0)
        if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            continue
        if vocal_supported and score >= threshold and sample.get('language') in {'zh', 'en', 'ja', 'ko'}:
            reliable.append(sample['language'])
    counts = Counter(reliable)
    if len(counts) > 1:
        language = 'mixed'
    elif len(reliable) >= 2 and len(reliable) >= .75 * len(samples):
        language = next(iter(counts))
    else:
        language = 'unknown'
    return {'language': language, 'sample_votes': dict(counts), 'reliable_samples': len(reliable),
            'sample_count': len(samples), 'threshold': threshold, 'calibrated': False}
