"""LangGraph 持久化受控流程。外部失败停在节点，重启可继续；不自动开始播放。"""
import sqlite3
import time
import re
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .models import Intent, merge_intent
from .recommend import compose, eligible, retrieve, validate_result
from .labels import Labels
from .preferences import LOCAL_USER, Preferences
from .suitability import audit_topics


class State(TypedDict, total=False):
    request: str
    intent: dict
    patch: dict
    clarification: str | None
    tool: str
    tool_reason: str
    candidates: list
    coverage: dict
    result: dict
    error: str
    attempts: int
    task_exclusions: list[str]
    user_id: str
    clarification_rounds: int
    context: str
    topic_audit: dict


class Workflow:
    def __init__(self, settings, store, providers, features, on_progress=None):
        self.store, self.providers, self.features = store, providers, features
        self.on_progress = on_progress
        self.timings = []
        self.preferences = Preferences(store)
        self.labels = Labels(store)
        self.connection = sqlite3.connect(settings.data_dir / 'checkpoints.sqlite', check_same_thread=False)
        graph = StateGraph(State)
        for name in ('parse', 'clarify', 'choose', 'retrieve', 'audit', 'compose'):
            graph.add_node(name, self._timed(getattr(self, name)))
        graph.add_edge(START, 'parse')
        graph.add_conditional_edges('parse', lambda s: 'clarify' if s.get('clarification') else
                                    'compose' if s.get('error') else 'choose')
        graph.add_edge('clarify', 'parse')
        graph.add_edge('choose', 'retrieve')
        graph.add_conditional_edges('retrieve', lambda s: 'choose' if s.get('error') and s['attempts'] < 2 else 'audit')
        graph.add_edge('audit', 'compose')
        graph.add_edge('compose', END)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.connection))

    def _timed(self, function):
        def node(state):
            start = time.perf_counter()
            if self.on_progress:
                self.on_progress(function.__name__)
            try:
                return function(state)
            finally:
                self.timings.append({'stage': function.__name__,
                                     'seconds': round(time.perf_counter() - start, 3)})
        return node

    def parse(self, state):
        patch = self.providers.parse(state['request'] + state.get('context', ''), state.get('intent', {}))
        if patch.clarification:
            # 明确已解析字段也保存，暂停时显示给用户；不能为生成歌单猜掉剩余硬条件。
            intent = merge_intent(state.get('intent', {}), patch)
            question = patch.clarification
            marks = list(re.finditer('[?？]', question))
            if len(marks) > 2:
                question = question[:marks[1].end()]
            return {'clarification': question, 'patch': patch.model_dump(), 'intent': intent.model_dump()}
        intent = merge_intent(state.get('intent', {}), patch)
        intent.exclude_tracks = sorted(set(intent.exclude_tracks) | set(state.get('task_exclusions', [])))
        tracks = self.labels.apply(self.store.tracks())
        excluded = self.preferences.excluded(state.get('user_id', LOCAL_USER))
        # 与检索共用硬过滤规则。已经没有候选时，换向量工具也无法补齐语言证据。
        if not any(eligible(track, intent, excluded) for track in tracks):
            reason = '当前曲库没有通过硬条件筛选的歌曲；已跳过检索模型和查询编码'
            if intent.language:
                reason += '。语言证据不足或其他硬条件排除，不代表曲库没有该语言歌曲'
            return {'intent': intent.model_dump(), 'clarification': None, 'attempts': 0,
                    'error': reason, 'candidates': [], 'coverage': {'catalog': len(tracks), 'eligible': 0},
                    'tool': 'none', 'tool_reason': '硬条件候选为空，换工具不能解决'}
        return {'intent': intent.model_dump(), 'clarification': None, 'attempts': 0, 'error': ''}

    def clarify(self, state):
        # interrupt 节点恢复时会重跑，所以模型调用在前一个节点完成，避免重复计费。
        rounds = state.get('clarification_rounds', 0)
        answer = interrupt({'question': None if rounds >= 2 else state['clarification'],
                            'ambiguity': state['clarification'], 'intent': state.get('intent', {}),
                            'clarification_rounds': min(rounds + 1, 2), 'waiting_for_input': rounds >= 2})
        return {'request': state['request'] + '\n用户澄清：' + str(answer), 'clarification': None,
                'clarification_rounds': min(rounds + 1, 2)}

    def choose(self, state):
        intent = Intent(**state['intent'])
        if not any((intent.audio_query, intent.lyrics_query, intent.negative_lyrics, intent.scene, intent.target_mood)):
            return {'tool': 'catalog', 'tool_reason': '仅按已确认语言与数量筛选，不调用向量模型', 'attempts': 1}
        available = self.features.available()
        if not available:
            raise RuntimeError('尚无可用特征；先运行 lify index audio 或 text')
        choice = self.providers.choose(state['intent'], available, state.get('error', ''))
        return {'tool': choice['tool'], 'tool_reason': choice.get('reason', ''),
                'attempts': state.get('attempts', 0) + 1}

    def retrieve(self, state):
        try:
            candidates, coverage = retrieve(self.store, self.features, Intent(**state['intent']), state['tool'],
                                            state.get('user_id', LOCAL_USER))
            return {'candidates': candidates, 'coverage': coverage,
                    'error': '' if candidates else '没有同时满足条件且具备所需特征的候选'}
        except RuntimeError:
            # 外部错误直接保留失败节点供重试，不让通用错误触发无限收费修复。
            raise

    def compose(self, state):
        result = compose(state.get('candidates', []), Intent(**state['intent']))
        result['coverage'] = state.get('coverage', {})
        result['tool'] = state['tool']
        result['tool_reason'] = state['tool_reason']
        result['topic_audit'] = state.get('topic_audit', {})
        if result['topic_audit'].get('required'):
            result['warnings'].append('明确禁止的题材已进行本轮歌词审核；证据不足的歌曲未补入，结果可能不足。')
        if state.get('error'):
            result['warnings'].append(state['error'])
        return {'result': validate_result(result, self.store, state.get('user_id', LOCAL_USER))}

    def audit(self, state):
        candidates, report = audit_topics(state.get('candidates', []),
                                         Intent(**state['intent']).negative_lyrics, self.providers)
        return {'candidates': candidates, 'topic_audit': report}

    def run(self, session, request=None, answer=None):
        self.timings = []
        start = time.perf_counter()
        config = {'configurable': {'thread_id': session}}
        snapshot = self.graph.get_state(config)
        user_id = self.preferences.owner(session)
        if answer is not None:
            value = Command(resume=answer)
        elif request is not None:
            # 页面关闭后仍可从结构化检查点继续。已显示的澄清计数不会因修改/刷新清零。
            value = {'request': request, 'intent': snapshot.values.get('intent', {}),
                     'candidates': [], 'result': {}, 'error': '', 'attempts': 0, 'topic_audit': {},
                     'user_id': user_id, 'clarification_rounds': snapshot.values.get('clarification_rounds', 0),
                     'context': '\n用户已确认的显式画像，仅作默认参考，当前请求优先：' +
                                str(self.preferences.profile(user_id))}
        else:
            if not snapshot.next:
                if snapshot.values.get('result'):
                    return snapshot.values['result']  # 已完成但回传前退出，不再收费重做。
                raise ValueError('当前没有待恢复的推荐任务；输入新请求或查看歌单')
            value = None
        state = self.graph.invoke(value, config)
        if state.get('__interrupt__'):
            pending = state['__interrupt__'][0].value
            self.store.update_session(session, {'pending_context': pending})
            return {**pending, 'timings': self.timings}
        state['result']['timings'] = self.timings
        state['result']['elapsed_seconds'] = round(time.perf_counter() - start, 3)
        self.store.update_session(session, {'result': state['result'], 'pending_context': None})
        self.store.event('recommendation', {'ids': [x['track']['id'] for x in state['result']['items']],
                                          'intent': state['intent'], 'user_id': user_id}, session)
        return state['result']

    def close(self):
        self.connection.close()

    def exclude_for_task(self, session, track_ids):
        config = {'configurable': {'thread_id': session}}
        state = self.graph.get_state(config).values
        intent = dict(state.get('intent', {}))
        # 新评分可撤回之前自动添加的任务排除；显式永久排除由Store单独管理。
        intent['exclude_tracks'] = sorted((set(intent.get('exclude_tracks', [])) -
                                          set(state.get('task_exclusions', []))) | set(track_ids))
        self.graph.update_state(config, {'intent': intent, 'task_exclusions': track_ids})
