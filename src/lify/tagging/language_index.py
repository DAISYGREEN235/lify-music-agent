"""离线语言索引：解码、Whisper检测、证据持久化；汇总规则见 language_profile。"""
import json

from ..labels import Labels
from ..store import dumps
from .language_profile import segment_starts, summarize_language_samples


class LanguageIndexer:
    """离线音频语言估计；多片段一致才入候选，未知不能假扮英文/日文。"""
    VERSION = 'whisper-small@536b0662742c02347bc0e980a01041f333bce120:4x20-vocal-v4'

    def __init__(self, settings, store):
        from faster_whisper import WhisperModel
        self.store = store
        self.labels = Labels(store)
        from ..features import Features
        self.audio_evidence = store.vectors('audio', Features(settings, store, None).version('audio'))
        self.model = WhisperModel(str(settings.data_dir / 'models' / 'whisper-small'),
                                  device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)

    def detect(self, track):
        import subprocess
        import imageio_ffmpeg
        import numpy as np
        samples = []
        vocal = self.audio_evidence.get(track['id'], (None, {}))[1]
        has_vocal = vocal.get('type_hint') == 'vocal' and vocal.get('type_margin', 0) >= .05
        if not has_vocal or track.get('lyrics', {}).get('status') == 'instrumental_hint':
            return {'language': 'unknown', 'language_evidence': {'source': track['path'], 'method': 'audio_estimate',
                    'version': self.VERSION, 'samples': [], 'vocal_gate': vocal.get('type_hint'),
                    'limitation': '缺乏足够人声证据，不运行语言猜测；纯器乐需要试听确认'}}
        for start in segment_starts(track['duration']):
            raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-ss', str(start),
                                  '-i', track['path'], '-t', '20', '-vn', '-f', 'f32le', '-ac', '1',
                                  '-ar', '16000', 'pipe:1'], capture_output=True, check=True, timeout=45)
            audio = np.frombuffer(raw.stdout, dtype='<f4').copy()
            language, probability, _ = self.model.detect_language(audio=audio)
            samples.append({'start': start, 'language': language, 'probability': probability,
                            'seconds': min(20, max(0, track['duration'] - start))})
        summary = summarize_language_samples(samples, has_vocal)
        language = summary['language']
        return {'language': language, 'language_evidence': {'source': track['path'], 'method': 'audio_estimate',
                'version': self.VERSION, 'samples': samples, 'summary': summary, 'vocal_gate': {
                    'type_hint': vocal.get('type_hint'), 'margin': vocal.get('type_margin'), 'threshold': .05},
                'limitation': '最多四段音频语言token与CLAP人声估计，阈值为保守启发式；不是整首主语言保证，支持试听纠正'}}

    def index(self, limit=None, progress=print):
        with self.store.connect() as db:
            cached = {row[0]: (row[1], json.loads(row[2])) for row in db.execute('SELECT * FROM language_estimates')}
        done = failed = 0
        import re
        groups = [[], [], []]
        for track in self.store.tracks():
            text = track.get('lyrics', {}).get('text', '')
            # 仅决定小批次抽样顺序，避免只检查某一种字符的歌曲；绝不将字符推断写为语言标签。
            group = 0 if len(re.findall(r'[ぁ-ゟァ-ヿ]', text)) > 10 else 1 if len(re.findall('[a-zA-Z]', text)) > 100 else 2
            groups[group].append(track)
        tracks = [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
        for track in tracks:
            if track['id'] not in self.audio_evidence:
                continue  # 没有人声核验特征先不分类，下次补索引后可继续。
            old = cached.get(track['id'])
            if old and old[0] == track['fingerprint'] and old[1].get('language_evidence', {}).get('version') == self.VERSION:
                continue
            if limit is not None and done + failed >= limit:
                break
            try:
                value = self.detect(track)
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO language_estimates VALUES(?,?,?)',
                               (track['id'], track['fingerprint'], dumps(value)))
                done += 1
                progress(f'演唱语言 {done}：{track["title"]} -> {value["language"]}')
            except Exception as exc:
                failed += 1
                self.store.feature_error(track['id'], 'language', type(exc).__name__)
                progress(f'演唱语言检测失败：{type(exc).__name__}')
                if failed >= 3:
                    break
        return {'processed': done, 'failed': failed, 'coverage': self.labels.coverage()}
