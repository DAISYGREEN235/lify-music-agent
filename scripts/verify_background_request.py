"""显式真实API验证：费用写入正式账本，不播放、不写默认用户偏好。"""
import json
import time
import argparse

from fastapi.testclient import TestClient
from lify.config import Settings
from lify.store import Store
from lify.web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--language-only', action='store_true')
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.data_dir/'lify.sqlite')
    report = []
    with TestClient(create_app(settings, store), base_url='http://127.0.0.1') as client:
        token = client.get('/api/state?user_id=verification&session=verification').json()['token']
        requests = ['我喜欢英文歌曲，推荐15首', '日文歌曲，约10首',
                    '通勤想振作起来，节奏轻快但不要吵，大约30分钟，约10首。',
                    '现在是下午四点，我希望听一些流行乐鼓励我继续学习。',
                    '夜间疲惫，希望听有希望感的流行乐与轻音乐，不要太激烈。']
        if args.language_only:
            requests = requests[:2]
        for text in requests:
            start = time.perf_counter()
            response = client.post('/api/message', headers={'X-Lify-Token':token},
                                   json={'session':'verification', 'user_id':'verification', 'text':text})
            accepted = time.perf_counter()-start
            response.raise_for_status()
            job = response.json()
            until = time.monotonic()+180
            while job['status'] in ('queued','running') and time.monotonic()<until:
                time.sleep(.5)
                job = client.get('/api/progress/'+job['id']).json()
            result = job.get('result') or {}
            report.append({'request':text, 'accept_seconds':round(accepted, 3), 'status':job['status'],
                           'session':job['session'], 'count':len(result.get('items', [])),
                           'total_seconds':result.get('total_seconds'), 'tool':result.get('tool'),
                           'intent':result.get('intent'), 'question':result.get('question'), 'error':job.get('error'),
                           'warnings':result.get('warnings'), 'elapsed_seconds':job['elapsed_seconds']})
            print(json.dumps(report[-1], ensure_ascii=False), flush=True)
            assert accepted<15 and response.status_code==202
            assert job['status']=='completed', job.get('error')
    name = 'background-language-v1.2.json' if args.language_only else 'background-real-v1.2.json'
    (settings.data_dir/name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
