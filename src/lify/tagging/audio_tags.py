"""复用已有 CLAP 音频向量生成弱标签，保存原始输出与受控标签两层。"""
import hashlib
import json

import numpy as np

from ..store import dumps
from ..features import normalize
from .audio_vocabulary import AUDIO_DESCRIPTORS


class AudioTagIndexer:
    """小接口 index(limit)：处理缺失项，每首提交；不调用文本付费接口。"""

    def __init__(self, store, features):
        self.store = store
        self.features = features
        vocabulary_hash = hashlib.sha256(dumps(AUDIO_DESCRIPTORS).encode()).hexdigest()[:16]
        self.version = f'{features.version("audio")}:tags-v1:{vocabulary_hash}'
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS audio_tag_profiles('
                       'track_id TEXT PRIMARY KEY,fingerprint TEXT,version TEXT,payload TEXT)')

    def _profile(self, track, audio_vector, audio_evidence, descriptor_vectors):
        raw_tags = []
        for (namespace, code, label, description), text_vector in zip(AUDIO_DESCRIPTORS, descriptor_vectors):
            # 同一 CLAP 空间的单位向量点积；分数可为负数，绝不是概率。
            score = float(np.dot(audio_vector, text_vector))
            if not np.isfinite(score):
                raise ValueError('音频标签包含非有限分数')
            raw_tags.append({'namespace': namespace, 'code': code, 'label': label,
                             'description': description, 'score': score, 'score_type': 'cosine_similarity'})
        product_tags = []
        for namespace in sorted({row['namespace'] for row in raw_tags}):
            ranked = sorted((row for row in raw_tags if row['namespace'] == namespace),
                            key=lambda row: row['score'], reverse=True)
            margin = ranked[0]['score'] - ranked[1]['score']
            # 仅选相对靠前描述用于展示；保留所有原始分数供试听审查。
            winner = ranked[0] if margin >= .005 else {**ranked[0], 'code': 'unknown', 'label': '不确定'}
            product_tags.append({**winner, 'method': 'model_estimate', 'confidence': None,
                                 'margin': margin, 'hard_constraint_eligible': False})
        return {'track_id': track['id'], 'fingerprint': track['fingerprint'], 'version': self.version,
                'source': track['path'], 'audio_feature_version': self.features.version('audio'),
                'segments': audio_evidence.get('segments', []), 'raw_tags': raw_tags,
                'product_tags': product_tags, 'calibrated': False,
                'limitation': '有限描述词间的相对听感估计；未校准，不证明语言、歌词题材、人声占比或场景适配'}

    def index(self, limit=None, progress=print):
        if limit is not None and limit < 1:
            raise ValueError('limit 必须大于0')
        audio_entries = self.store.vectors('audio', self.features.version('audio'))
        with self.store.connect() as db:
            cached = {row[0]: (row[1], row[2]) for row in db.execute(
                'SELECT track_id,fingerprint,version FROM audio_tag_profiles')}
        tracks = [track for track in self.store.tracks() if track['id'] in audio_entries and
                  cached.get(track['id']) != (track['fingerprint'], self.version)]
        tracks = tracks[:limit] if limit is not None else tracks
        if not tracks:
            return {'processed': 0, 'failed': 0, 'version': self.version}
        # 只编码词表一次，并复用查询缓存；初始化失败不对每首歌反复重试。
        descriptor_vectors = [np.asarray(normalize(self.features.query('audio', row[3])))
                              for row in AUDIO_DESCRIPTORS]
        done = failed = consecutive_failures = 0
        for track in tracks:
            try:
                vector, evidence = audio_entries[track['id']]
                profile = self._profile(track, np.asarray(normalize(vector)), evidence, descriptor_vectors)
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO audio_tag_profiles VALUES(?,?,?,?)',
                               (track['id'], track['fingerprint'], self.version, dumps(profile)))
                done += 1
                consecutive_failures = 0
                progress(f'音频标签 {done}：{track["title"]}')
            except (ValueError, TypeError) as exc:
                self.store.feature_error(track['id'], 'tags', type(exc).__name__)
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    break
        return {'processed': done, 'failed': failed, 'version': self.version}


def load_audio_tag_profiles(store):
    """只加载当前有效歌曲、当前文件指纹的标签；读取前不创建数据库表。"""
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='audio_tag_profiles'").fetchone()
        if not exists:
            return {}
        rows = db.execute('SELECT a.track_id,a.payload FROM audio_tag_profiles a JOIN tracks t '
                          'ON a.track_id=t.id WHERE t.available=1 AND a.fingerprint=t.fingerprint').fetchall()
    return {row[0]: json.loads(row[1]) for row in rows}
