"""真实推荐计时：显式付费测试，费用进入正式预算；不播放、不输出密钥。"""
import argparse
import json
import time
import uuid

from lify.config import Settings
from lify.features import Features
from lify.providers import Providers
from lify.store import Store
from lify.workflow import Workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.data_dir / 'lify.sqlite')
    providers = Providers(settings, store)
    features = Features(settings, store, providers)
    stages = []
    def wrap(obj, name):
        original = getattr(obj, name)
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                row = {'stage': name, 'seconds': round(time.perf_counter() - start, 3)}
                stages.append(row)
                print(json.dumps(row), flush=True)
        setattr(obj, name, timed)
    for obj, name in ((providers, 'parse'), (providers, 'choose'), (features, 'available'),
                      (features, 'query')):
        wrap(obj, name)
    workflow = Workflow(settings, store, providers, features)
    start = time.perf_counter()
    before = providers.budget.summary()['accounted']
    try:
        result = workflow.run('diagnostic-' + uuid.uuid4().hex, request=args.request)
        report = {'seconds': round(time.perf_counter() - start, 3), 'count': len(result.get('items', [])),
                  'stages': stages, 'cost_estimate': providers.budget.summary()['accounted'] - before}
        (settings.data_dir / 'recommend-performance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    finally:
        workflow.close()


if __name__ == '__main__':
    main()
