"""两种向量空间独立维护；每首歌处理后立即提交，重新运行只补缺失项。"""
import hashlib
import json
import subprocess
import uuid

import numpy as np


def normalize(vector):
    value = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(value)
    if value.ndim != 1 or not np.isfinite(value).all() or norm < 1e-10:
        raise ValueError('无效向量')
    return (value / norm).tolist()


def write_cache(file, value):
    temporary = file.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
    temporary.replace(file)


def chunks(text, max_bytes=2800):
    """按 UTF-8 字节保守分段，避免中英文字符数与 token 数混淆。"""
    result, current, size = [], [], 0
    for char in text:
        n = len(char.encode('utf-8'))
        if size + n > max_bytes:
            result.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += n
    if current:
        result.append(''.join(current))
    return result


class AudioEncoder:
    def __init__(self, settings):
        import torch
        from transformers import ClapModel, ClapProcessor
        self.torch = torch
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        torch.set_num_threads(min(4, torch.get_num_threads()))
        cache = str(settings.data_dir / 'models')
        snapshot = settings.data_dir / 'models' / ('models--' + settings.audio_model.replace('/', '--')) / 'snapshots' / settings.audio_revision
        local = (snapshot / 'pytorch_model.bin').is_file() and (snapshot / 'preprocessor_config.json').is_file()
        self.processor = ClapProcessor.from_pretrained(settings.audio_model, revision=settings.audio_revision,
                                                      cache_dir=cache, local_files_only=local)
        # 固定仓库版本，禁用自动寻找转换PR版本；torch>=2.6用受限的weights_only加载。
        self.model = ClapModel.from_pretrained(settings.audio_model, revision=settings.audio_revision,
                                              use_safetensors=False, cache_dir=cache,
                                              local_files_only=local).to(self.device).eval()
        self.revision = getattr(self.model.config, '_commit_hash', None)
        self.text_cache = {}
        # 冒烟对照只检测灾难性塌缩，不把此阈值当成情绪分类准确率。
        calm = self.text('gentle instrumental music for studying')
        intense = self.text('loud aggressive heavy metal with screaming')
        if float(np.dot(calm, intense)) > .99:
            raise RuntimeError('音频文本编码器对照塌缩；停止使用该权重，先核验模型版本')

    def text(self, query):
        if query in self.text_cache:
            return self.text_cache[query]
        inputs = self.processor(text=[query], return_tensors='pt', padding=True, truncation=True)
        with self.torch.inference_mode():
            vector = self.model.get_text_features(**inputs.to(self.device))[0].cpu().numpy()
        self.text_cache[query] = normalize(vector)
        return self.text_cache[query]

    def track(self, track):
        import imageio_ffmpeg
        import librosa
        # 短片段分别编码后聚合；不给长音频直接截前十秒。
        length = min(10.0, track['duration'])
        starts = sorted(set(round(max(0, track['duration'] - length) * f, 2) for f in (.2, .5, .8)))
        vectors, tempos, rms = [], [], []
        for start in starts:
            result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-ss', str(start),
                                     '-f', 'mp3', '-i', track['path'], '-t', str(length), '-vn',
                                     '-map', '0:a:0', '-f', 'f32le', '-ac', '1',
                                     '-ar', '48000', 'pipe:1'], capture_output=True, check=True, timeout=45)
            samples = np.frombuffer(result.stdout, dtype='<f4').copy()
            if len(samples) < 4800:
                raise ValueError('解码有效音频不足')
            inputs = self.processor(audio=samples, sampling_rate=48000, return_tensors='pt')
            with self.torch.inference_mode():
                vector = self.model.get_audio_features(**inputs.to(self.device))[0].cpu().numpy()
            vectors.append(normalize(vector))
            tempo, _ = librosa.beat.beat_track(y=samples[::2], sr=24000)
            tempos.append(float(np.asarray(tempo).reshape(-1)[0]))
            rms.append(float(np.sqrt(np.mean(samples ** 2))))
        mean = normalize(np.mean(vectors, axis=0))
        vocal_score = float(np.dot(mean, self.text('A pop song with a singer and vocals.')))
        instrumental_score = float(np.dot(mean, self.text('Instrumental music without singing.')))
        # 相似度不是校准概率；类型仅用于软混合，不能冒充硬约束事实。
        hint = 'vocal' if vocal_score > instrumental_score else 'instrumental'
        return mean, {'source': track['path'], 'model': self.model.config._name_or_path,
                      'revision': self.revision, 'segments': starts, 'segment_seconds': length,
                      'bpm_estimate': float(np.median(tempos)), 'rms': float(np.mean(rms)),
                      'type_hint': hint, 'type_margin': abs(vocal_score - instrumental_score),
                      'limitation': '片段级模型听感估计，不证明旋律走向、演唱语言或精确情绪'}


class Features:
    def __init__(self, settings, store, providers):
        self.settings, self.store, self.providers = settings, store, providers
        self._audio = None

    @property
    def audio(self):
        if self._audio is None:
            self._audio = AudioEncoder(self.settings)
        return self._audio

    def version(self, route):
        s = self.settings
        return (f'{s.audio_model}@{s.audio_revision}:segments3x10-v1' if route == 'audio'
                else f'{s.embedding_model}:{s.dimensions}:lyrics2800bytes-v1')

    def available(self):
        routes = [r for r in ('audio', 'text') if self.store.vector_count(r, self.version(r))]
        return routes + (['both'] if len(routes) == 2 else [])

    def query(self, route, text):
        version = self.version(route)
        key = hashlib.sha256((version + text).encode()).hexdigest()
        folder = self.settings.data_dir / 'queries'
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / (key + '.json')
        if file.exists():
            return json.loads(file.read_text('utf-8'))
        if route == 'audio':
            vector = self.audio.text(text)
        else:
            vectors = self.providers.embed(chunks(text))
            vector = normalize(np.mean(vectors, axis=0))
        write_cache(file, vector)
        return vector

    def index(self, route, limit=None, progress=print):
        if route == 'text':
            return self._index_text(limit, progress)
        version = self.version(route)
        cached = self.store.vectors(route, version)
        done = failed = skipped = consecutive_failures = 0
        for track in self.store.tracks():
            if track['id'] in cached:
                skipped += 1
                continue
            if limit is not None and done + failed >= limit:
                break
            try:
                vector, evidence = self.audio.track(track)
                self.store.put_vector(track, route, version, vector, evidence)
                done += 1
                consecutive_failures = 0
                progress(f'{route} 已完成 {done}：{track["title"]}')
            except Exception as exc:
                from .providers import BudgetError
                if isinstance(exc, BudgetError):
                    raise
                # 初始化/供应商失败不要对整库反复发送请求。
                self.store.feature_error(track['id'], route, type(exc).__name__)
                failed += 1
                consecutive_failures += 1
                progress(f'{route} 失败：{type(exc).__name__}')
                if consecutive_failures >= 3:
                    break
        return {'processed': done, 'failed': failed, 'cached_or_no_lyrics': skipped,
                'indexed_total': len(self.store.vectors(route, version)), 'catalog_total': len(self.store.tracks())}

    def _index_text(self, limit, progress):
        version = self.version('text')
        cached = self.store.vectors('text', version)
        tracks = [t for t in self.store.tracks() if t['id'] not in cached and t['lyrics']['text'].strip()]
        tracks = tracks[:limit] if limit is not None else tracks
        folder = self.settings.data_dir / 'text_chunks'
        folder.mkdir(parents=True, exist_ok=True)
        parts_by_track, pending, vectors = {}, {}, {}
        for track in tracks:
            keys = []
            for part in chunks(track['lyrics']['text']):
                key = hashlib.sha256((version + part).encode()).hexdigest()
                keys.append(key)
                file = folder / (key + '.json')
                if file.exists():
                    vectors[key] = json.loads(file.read_text('utf-8'))
                else:
                    pending[key] = part
            parts_by_track[track['id']] = keys
        todo, done = list(pending), set()

        def commit_ready():
            for track in tracks:
                keys = parts_by_track[track['id']]
                if track['id'] in done or any(k not in vectors for k in keys):
                    continue
                self.store.put_vector(track, 'text', version, normalize(np.mean([vectors[k] for k in keys], axis=0)),
                                      {'source': track['lyrics'].get('source'), 'model': self.settings.embedding_model,
                                       'chunks': len(keys), 'limitation': '歌词主题相关性，不代表听感或演唱语言'})
                done.add(track['id'])
                progress(f'text 已完成 {len(done)}：{track["title"]}')

        commit_ready()
        for offset in range(0, len(todo), self.settings.batch_size):
            keys = todo[offset:offset + self.settings.batch_size]
            values = self.providers.embed([pending[k] for k in keys])
            for key, value in zip(keys, values):
                vectors[key] = value
                write_cache(folder / (key + '.json'), value)
            commit_ready()
        return {'processed': len(done), 'indexed_total': len(self.store.vectors('text', version)),
                'catalog_total': len(self.store.tracks())}
