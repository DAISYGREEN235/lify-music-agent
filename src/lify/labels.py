"""可追溯歌曲标签：语言事实与歌词提示分开，人工试听纠正可撤销。"""
import json

from .store import dumps


def language_code(value):
    aliases = {'中文': 'zh', '汉语': 'zh', '国语': 'zh', '普通话': 'zh', 'zho': 'zh', 'chi': 'zh', 'cmn': 'zh',
               'chinese': 'zh', 'mandarin': 'zh', 'english': 'en', 'japanese': 'ja', 'korean': 'ko',
               '英文': 'en', '英语': 'en', 'eng': 'en', '日语': 'ja', '日文': 'ja', 'jpn': 'ja',
               '韩语': 'ko', '韩文': 'ko', 'kor': 'ko', '双语': 'mixed', '纯器乐': 'instrumental',
               '未知': 'unknown', '其他': 'unknown', 'und': 'unknown', 'mul': 'mixed', 'zxx': 'instrumental'}
    value = (value or '').strip().casefold()
    return aliases.get(value, value or 'unknown')


class Labels:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS track_labels(track_id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS language_estimates(track_id TEXT PRIMARY KEY,fingerprint TEXT,payload TEXT)')

    def apply(self, tracks):
        from .tagging.audio_tags import load_audio_tag_profiles
        audio_profiles = load_audio_tag_profiles(self.store)
        with self.store.connect() as db:
            overrides = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM track_labels')}
            estimates = {r[0]: (r[1], json.loads(r[2])) for r in db.execute('SELECT * FROM language_estimates')}
        result = []
        for track in tracks:
            estimate = estimates.get(track['id'])
            inferred = estimate[1] if estimate and estimate[0] == track['fingerprint'] else {}
            override = overrides.get(track['id'], {})
            if override.get('label_fingerprint', track['fingerprint']) != track['fingerprint']:
                override = {}
            # 人工试听 > 原始ID3 > 音频模型估计；译文不参与语言推断。
            value = {**track, **(inferred if not track.get('language') else {}), **override}
            # 弱标签只放入独立字段，绝不覆盖 language/vocal 等硬条件事实。
            if track['id'] in audio_profiles:
                value['audio_tag_profile'] = audio_profiles[track['id']]
            result.append(value)
        return result

    def save(self, track_id, language, source):
        code = language_code(language)
        if code not in {'zh', 'en', 'ja', 'ko', 'mixed', 'instrumental', 'unknown'}:
            raise ValueError('请选择支持的演唱语言或未知')
        if not source.strip():
            raise ValueError('必须注明实际试听或原始演唱语言来源')
        track = next((t for t in self.store.tracks() if t['id'] == track_id), None)
        if not track:
            raise ValueError('歌曲不存在')
        value = {'language': code, 'language_evidence': {'source': source, 'method': 'human_verified', 'version': 'v1'}}
        value['label_fingerprint'] = track['fingerprint']
        if code == 'instrumental':
            value['vocal'] = 'instrumental'
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO track_labels VALUES(?,?)', (track_id, dumps(value)))
        self.store.event('label_corrected', value, track_id=track_id)

    def revoke(self, track_id):
        with self.store.connect() as db:
            db.execute('DELETE FROM track_labels WHERE track_id=?', (track_id,))
        self.store.event('label_revoked', {}, track_id=track_id)

    def coverage(self):
        counts = {}
        for track in self.apply(self.store.tracks()):
            code = language_code(track.get('language'))
            counts[code] = counts.get(code, 0) + 1
        return counts


def __getattr__(name):
    """兼容旧调用 from lify.labels import LanguageIndexer，避免模型依赖提前加载。"""
    if name == 'LanguageIndexer':
        from .tagging.language_index import LanguageIndexer
        return LanguageIndexer
    raise AttributeError(name)
