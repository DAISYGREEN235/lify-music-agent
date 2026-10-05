"""显式运行一次真实推荐验证；不启动播放器，费用受统一账本控制。"""
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
        result = workflow.run('smoke-real-v1', request='现在是晚上9点，lify我好累啊，但是我希望听一些'
                              '有希望感的流行乐、轻音乐，不要太激烈的。约10首。')
        output = settings.data_dir / 'smoke-result.json'
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'question': result.get('question'), 'count': len(result.get('items', [])),
                          'total_seconds': result.get('total_seconds'), 'intent': result.get('intent'),
                          'warnings': result.get('warnings'), 'budget': providers.budget.summary()},
                         ensure_ascii=False, indent=2))
    finally:
        workflow.close()


if __name__ == '__main__':
    main()
