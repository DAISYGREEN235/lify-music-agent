"""无需收费API的真实曲库编码对照；不把排序变化写成用户满意度。"""
import json
import time
import numpy as np

from lify.config import Settings
from lify.store import Store
from lify.features import Features
from lify.models import Intent
from lify.recommend import compose, retrieve, validate_result


def main():
    settings = Settings.load()
    store = Store(settings.data_dir/'lify.sqlite')
    features = Features(settings, store, None)
    queries = {
        '通勤': 'upbeat uplifting music with a light steady rhythm, moderate energy',
        '专注学习': 'encouraging hopeful pop music for studying with a steady rhythm',
        '夜间休息': 'uplifting hopeful gentle pop and instrumental music with a light steady rhythm',
        '专注写代码对照': 'instrumental music for focus and coding, steady rhythm, minimal distraction',
    }
    vectors, results = {}, {}
    started = time.perf_counter()
    for scene, query in queries.items():
        vectors[scene] = features.query('audio', query)
        intent = Intent(scene=scene, audio_query=query, count=10,
                        minutes=30 if scene == '通勤' else None,
                        duration_mode='approx' if scene == '通勤' else 'none',
                        mix_types=scene == '夜间休息')
        candidates, coverage = retrieve(store, features, intent, 'audio', user_id='encoding-verification')
        result = validate_result(compose(candidates, intent), store, 'encoding-verification')
        results[scene] = {'ids': [item['track']['id'] for item in result['items']],
                          'titles': [item['track']['title'] for item in result['items']],
                          'count': len(result['items']), 'seconds': result['total_seconds'],
                          'complete': result['complete'], 'coverage': coverage}
    pairs = []
    names = list(queries)
    for i, first in enumerate(names):
        for second in names[i+1:]:
            pairs.append({'first': first, 'second': second,
                          'query_cosine': float(np.dot(vectors[first], vectors[second])),
                          'shared_tracks': len(set(results[first]['ids']) & set(results[second]['ids']))})
    report = {'version': '1.2.0a1', 'model': settings.audio_model, 'revision': settings.audio_revision,
              'audio_coverage': store.vector_count('audio', features.version('audio')),
              'elapsed_seconds': round(time.perf_counter()-started, 2), 'pairs': pairs, 'scenes': results,
              'note': '固定描述的编码与硬约束检查；未调用LLM，不代表用户试听质量'}
    target = settings.data_dir/'scene-encoding-v1.2.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'scenes'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
