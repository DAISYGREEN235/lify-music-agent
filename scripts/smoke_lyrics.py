"""一次真实歌词路线 + 多轮精确条件验证；不播放。"""
import json
from lify.config import Settings
from lify.features import Features
from lify.providers import Providers
from lify.store import Store
from lify.workflow import Workflow


def main():
    settings = Settings.load()
    store = Store(settings.data_dir / 'lify.sqlite')
    providers = Providers(settings, store)
    workflow = Workflow(settings, store, providers, Features(settings, store, providers))
    try:
        first = workflow.run('smoke-lyrics-v1', request='我想读歌词里表达希望、继续生活的歌曲。'
                             '这里只按歌词主题推荐，不限制听感；恰好3首，总时长不超过15分钟。')
        if 'question' in first:
            print(json.dumps(first, ensure_ascii=False))
            return
        assert first['intent']['count'] == 3 and first['intent']['count_mode'] == 'exact'
        assert first['intent']['duration_mode'] == 'max' and first['total_seconds'] <= 900
        assert all('text' in x['evidence'] for x in first['items'])
        second = workflow.run('smoke-lyrics-v1', request='换成歌词表达友情的歌，其他条件保留。')
        assert second['intent']['count'] == 3 and second['intent']['count_mode'] == 'exact'
        assert second['intent']['minutes'] == 15 and second['total_seconds'] <= 900
        (settings.data_dir / 'smoke-lyrics.json').write_text(json.dumps(second, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'first_count': len(first['items']), 'second_count': len(second['items']),
                          'second_intent': second['intent'], 'budget': providers.budget.summary()}, ensure_ascii=False, indent=2))
    finally:
        workflow.close()


if __name__ == '__main__':
    main()
