"""终端入口：显式播放、可恢复会话、只读曲库和可追溯反馈。"""
import argparse
import json
import shutil
import sys

from .catalog import import_catalog
from .config import Settings
from .features import Features
from .providers import Providers
from .store import Store


def show(result):
    if 'question' in result:
        print('等待主动补充：' + result.get('ambiguity', '') if result.get('waiting_for_input') else
              '需要澄清：' + (result['question'] or ''))
        return
    print(f'\n共 {len(result["items"])} 首，合计 {result["total_seconds"] / 60:.1f} 分钟')
    for i, item in enumerate(result['items'], 1):
        t = item['track']
        print(f'{i:2}. {t["title"]} — {t["artist"]} [{t["duration"] / 60:.1f}分钟] ID={t["id"]}')
        for route, evidence in item['evidence'].items():
            print(f'    {"听感" if route == "audio" else "歌词主题"}：与“{evidence["query"]}”模型相关度'
                  f' {evidence["similarity"]:.3f}；来源 {evidence.get("source")}')
        if item.get('scene_feedback'):
            print('    ' + item['scene_feedback'])
    if result.get('duration_deviation') is not None:
        print(f'时长偏差：{result["duration_deviation"]:+.1%}')
    for warning in result.get('warnings', []):
        print('说明：' + warning)
    print('尚未播放。输入“播放”；恢复上次位置请用“继续播放”。')


def chat(settings, store, providers, features, session):
    from .player import Player
    from .recommend import validate_result
    from .workflow import Workflow
    workflow = Workflow(settings, store, providers, features)
    player = None
    config = {'configurable': {'thread_id': session}}
    snapshot = workflow.graph.get_state(config)
    pending = bool(snapshot.interrupts)
    saved = store.load_session(session)
    result = saved.get('result')
    print(f'Lify V1.0 | 会话 {session} | 退出/恢复/歌单/播放/继续播放/暂停/下一首/定位 秒数')
    print('反馈：反馈 歌曲ID like|dislike|unsuitable|ban|unban；评分 歌曲ID 0|1|2')
    if pending:
        print('待澄清：' + (snapshot.interrupts[0].value['question'] or snapshot.interrupts[0].value.get('ambiguity', '')))
    elif snapshot.next:
        print('有未完成任务，输入“恢复”继续；也可输入新请求。')
    if result:
        show(result)
    try:
        while True:
            try:
                text = input('\nlify> ').strip()
                if not text:
                    continue
                if text in ('退出', 'exit', 'quit'):
                    break
                if text == '歌单':
                    show(result) if result else print('尚无歌单')
                elif text in ('播放', '继续播放'):
                    if not result:
                        print('请先生成歌单')
                        continue
                    validate_result(result, store)
                    if player:
                        player.close()
                    player = Player(settings, store, session)
                    player.play([x['track'] for x in result['items']], resume=text == '继续播放')
                elif text in ('暂停', '下一首') or text.startswith('定位 '):
                    if not player:
                        print('尚未播放')
                        continue
                    action = 'pause' if text == '暂停' else 'next' if text == '下一首' else 'seek'
                    player.control(action, float(text.split()[1]) if action == 'seek' else None)
                elif text.startswith(('反馈 ', '评分 ')):
                    parts = text.split()
                    if len(parts) != 3:
                        raise ValueError('格式：反馈 歌曲ID 动作，或 评分 歌曲ID 0|1|2')
                    scene = result['intent']['scene'] if result else ''
                    store.feedback(session, parts[1], parts[2] if parts[0] == '反馈' else 'rate',
                                   scene=scene, rating=int(parts[2]) if parts[0] == '评分' else None)
                    print('反馈已保存')
                else:
                    if pending:
                        value = workflow.run(session, answer=text)
                    else:
                        value = workflow.run(session, request=None if text == '恢复' else text)
                    pending = 'question' in value
                    if not pending:
                        result = value
                    show(value)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                print(f'操作未完成：{exc}')
                print('任务检查点已保留；可修复配置后输入“恢复”。')
    except (KeyboardInterrupt, EOFError):
        print('\n保存并退出。')
    finally:
        if player:
            player.close()
        workflow.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Lify V1.0 本机场景歌单与可恢复 Agent')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='检查配置与数据，不显示密钥')
    commands.add_parser('import', help='只读导入 MP3/LRC')
    index = commands.add_parser('index', help='构建特征，自动跳过有效缓存')
    index.add_argument('route', choices=['audio', 'text', 'language', 'tags'])
    index.add_argument('--limit', type=int)
    interactive = commands.add_parser('chat', help='终端会话')
    interactive.add_argument('--session', default='default')
    commands.add_parser('budget', help='费用及未确认预留款')
    commands.add_parser('evaluate', help='真实事件计数，不伪造未标注质量指标')
    history = commands.add_parser('history', help='历史事件')
    history.add_argument('--session', default='default')
    history.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.data_dir / 'lify.sqlite')
    providers = Providers(settings, store)
    features = Features(settings, store, providers)
    try:
        if args.command == 'doctor':
            print(json.dumps({'music_exists': settings.music_dir.is_dir(), 'lyrics_exists': settings.lyrics_dir.is_dir(),
                              'llm_key_configured': bool(settings.llm_key),
                              'embedding_key_configured': bool(settings.embedding_key),
                              'llm_model': settings.llm_model, 'embedding_model': settings.embedding_model,
                              'price_configured': settings.llm_input_price is not None and settings.llm_output_price is not None,
                              'mpv': shutil.which(settings.mpv_path), 'tracks': len(store.tracks()),
                              'indexed': {r: len(store.vectors(r, features.version(r))) for r in ('audio', 'text')},
                              'routes': features.available(), 'budget': providers.budget.summary()},
                             ensure_ascii=False, indent=2))
        elif args.command == 'import':
            print(json.dumps(import_catalog(store, settings.music_dir, settings.lyrics_dir), ensure_ascii=False))
        elif args.command == 'index':
            if args.limit is not None and args.limit < 1:
                raise ValueError('--limit 必须大于0')
            if args.route == 'language':
                from .tagging.language_index import LanguageIndexer
                print(LanguageIndexer(settings, store).index(args.limit))
            elif args.route == 'tags':
                from .tagging.audio_tags import AudioTagIndexer
                print(AudioTagIndexer(store, features).index(args.limit))
            else:
                print(features.index(args.route, args.limit))
        elif args.command == 'chat':
            chat(settings, store, providers, features, args.session)
        elif args.command == 'budget':
            print(json.dumps(providers.budget.summary(), ensure_ascii=False, indent=2))
        elif args.command == 'evaluate':
            from .evaluation import event_metrics
            print(json.dumps(event_metrics(store.events()), ensure_ascii=False, indent=2))
        elif args.command == 'history':
            print(json.dumps(store.events(args.session)[-args.limit:], ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'未完成：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('已停止；完成的索引和预算记录已保存，下次执行会继续。')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
