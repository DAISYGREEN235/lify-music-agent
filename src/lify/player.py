"""mpv 独立进程 + Windows 双向命名管道。事实事件与用户操作分开持久化。"""
import json
import math
import os
import queue
import subprocess
import threading
import time
import uuid


class PlaybackStats:
    """有效墙钟时长用于早跳；去重媒体区间用于完播，seek 不计入听过的内容。"""
    def __init__(self, duration):
        self.duration = duration
        self.intervals = []
        self.listened = 0.0
        self.previous = None
        self.gaps = 0

    def sample(self, position, now, active=True, speed=1):
        old = self.previous
        self.previous = (position, now, active, speed)
        if not old or not active or not old[2]:
            return
        delta_time, delta_media = now - old[1], position - old[0]
        if delta_time <= 0 or delta_media <= 0:
            return
        if delta_time > 3 or abs(delta_media - delta_time * speed) > max(.35, delta_time * .35):
            self.gaps += 1
            return
        self.listened += min(delta_time, delta_media / max(speed, .01))
        self.intervals.append((max(0, old[0]), min(self.duration, position)))

    def discontinuity(self):
        self.previous = None

    def result(self, reason, action=None):
        merged = []
        for start, end in sorted(self.intervals):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        coverage = sum(max(0, b - a) for a, b in merged) / self.duration if self.duration > 0 else 0
        return {'reason': reason, 'action': action, 'effective_seconds': self.listened,
                'coverage': min(1, coverage), 'complete': reason == 'eof' and coverage >= .8,
                'early_skip': action == 'next' and self.listened < 15,
                'gaps': self.gaps, 'intervals': merged}


class MPV:
    def __init__(self, executable, on_event, silent=False):
        if os.name != 'nt':
            raise RuntimeError('本版播放器适配器仅支持 Windows 命名管道')
        import win32con
        import win32file
        import win32job
        import win32api
        import pywintypes
        self.win32file, self.pywintypes = win32file, pywintypes
        self.callback, self.closed = on_event, False
        self.pending, self.counter, self.lock = {}, 0, threading.Lock()
        pipe = r'\\.\pipe\lify-' + uuid.uuid4().hex
        command = [str(executable), '--no-config', '--idle=yes', '--no-video', '--no-terminal',
                   '--pause=yes', '--input-ipc-server=' + pipe]
        if silent:
            command += ['--ao=null']
        # 内核持有的作业对象：即使 Python 被强制结束，本应用的 mpv 也随之停止。
        self.job = win32job.CreateJobObject(None, 'lify-' + uuid.uuid4().hex)
        try:
            self.process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.job.Close()
            raise
        try:
            info = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            process_handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                                 False, self.process.pid)
            try:
                win32job.AssignProcessToJobObject(self.job, process_handle)
            finally:
                process_handle.Close()
        except Exception:
            self.process.kill()
            self.process.wait()
            self.job.Close()
            raise RuntimeError('无法建立播放器进程退出保护，未开始播放') from None
        deadline = time.monotonic() + 10
        while True:
            try:
                self.handle = win32file.CreateFile(pipe, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                                   0, None, win32con.OPEN_EXISTING,
                                                   win32con.FILE_FLAG_OVERLAPPED, None)
                break
            except pywintypes.error:
                if time.monotonic() >= deadline or self.process.poll() is not None:
                    self.process.kill()
                    self.process.wait()
                    self.job.Close()
                    raise RuntimeError('mpv 命名管道连接失败') from None
                time.sleep(.05)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        import win32event
        buffer = b''
        try:
            while not self.closed:
                ov = self.pywintypes.OVERLAPPED()
                ov.hEvent = win32event.CreateEvent(None, True, False, None)
                try:
                    _, data = self.win32file.ReadFile(self.handle, 65536, ov)
                    count = self.win32file.GetOverlappedResult(self.handle, ov, True)
                    buffer += bytes(data[:count])
                finally:
                    ov.hEvent.Close()
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    request_id = message.get('request_id')
                    if request_id in self.pending:
                        self.pending[request_id].put(message)
                    elif 'event' in message:
                        self.callback(message)
        except Exception as exc:
            if not self.closed:
                self.callback({'event': 'ipc-disconnect', 'error_type': type(exc).__name__})

    def command(self, *args):
        import win32event
        with self.lock:
            self.counter += 1
            key = self.counter
            response = queue.Queue()
            self.pending[key] = response
            ov = self.pywintypes.OVERLAPPED()
            ov.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                data = (json.dumps({'command': args, 'request_id': key}, ensure_ascii=False) + '\n').encode()
                self.win32file.WriteFile(self.handle, data, ov)
                self.win32file.GetOverlappedResult(self.handle, ov, True)
                value = response.get(timeout=5)
                if value.get('error') != 'success':
                    raise RuntimeError('mpv 命令失败：' + str(value.get('error')))
                return value.get('data')
            finally:
                self.pending.pop(key, None)
                ov.hEvent.Close()

    def close(self):
        if self.closed:
            return
        try:
            self.command('quit')
        except Exception:
            pass
        self.closed = True
        try:
            self.win32file.CancelIoEx(self.handle, None)
        except Exception:
            pass
        self.handle.Close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.job.Close()


class Player:
    def __init__(self, settings, store, session, silent=False):
        self.store, self.session = store, session
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.current = None
        self.stats = None
        self.position = 0.0
        self.action = None
        self.stopping = threading.Event()
        self.playlist = []
        self.index = 0
        self.loaded = False
        saved = store.load_session(session).get('player', {})
        if saved.get('running'):
            store.event('collection_gap', {'cause': 'previous_unclean_exit', 'last_position': saved.get('position')}, session)
        self.mpv = MPV(settings.mpv_path, self.events.put, silent)
        for i, prop in enumerate(['time-pos', 'pause', 'seeking', 'speed', 'core-idle']):
            self.mpv.command('observe_property', i + 1, prop)
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def _save(self, running=True):
        snapshot = {'track_id': self.current['id'] if self.current else None,
                             'position': self.position, 'playlist': [t['id'] for t in self.playlist],
                             'index': self.index, 'running': running,
                             'stats': {'intervals': self.stats.intervals, 'listened': self.stats.listened,
                                       'gaps': self.stats.gaps} if self.stats else None}
        self.store.update_session(self.session, {'player': snapshot})

    def _loop(self):
        paused, seeking, speed, idle = True, False, 1.0, True
        last_save = 0
        while not self.stopping.is_set():
            try:
                event = self.events.get(timeout=.2)
            except queue.Empty:
                continue
            with self.lock:
                self.store.event('mpv_raw', event, self.session, self.current['id'] if self.current else None)
                kind = event['event']
                if kind == 'file-loaded':
                    self.loaded = True
                elif kind == 'property-change':
                    name, value = event.get('name'), event.get('data')
                    if name == 'pause':
                        paused = bool(value)
                        if self.stats:
                            self.stats.discontinuity()
                    elif name == 'seeking':
                        seeking = bool(value)
                        if self.stats:
                            self.stats.discontinuity()
                    elif name == 'speed' and value:
                        speed = float(value)
                        if self.stats:
                            self.stats.discontinuity()
                    elif name == 'core-idle':
                        idle = bool(value)
                        if self.stats:
                            self.stats.discontinuity()
                    elif name == 'time-pos' and value is not None and self.stats and self.loaded:
                        self.position = float(value)
                        self.stats.sample(self.position, time.monotonic(), not paused and not seeking and not idle, speed)
                        if time.monotonic() - last_save >= 1:
                            self._save()
                            last_save = time.monotonic()
                elif kind == 'seek' and self.stats:
                    self.stats.discontinuity()
                elif kind == 'end-file' and self.stats:
                    self.loaded = False
                    reason = event.get('reason', 'unknown')
                    self.store.event('playback_summary', self.stats.result(reason, self.action),
                                     self.session, self.current['id'])
                    self.stats = None
                    if reason == 'eof' and self.index + 1 < len(self.playlist) and not self.stopping.is_set():
                        self.index += 1
                        self._load(self.playlist[self.index], 0)
                elif kind == 'ipc-disconnect':
                    self.store.event('collection_gap', {'cause': 'ipc_disconnect'}, self.session)
                    self.stopping.set()

    def _load(self, track, position):
        from pathlib import Path
        if track['id'] in self.store.excluded() or not Path(track['path']).is_file():
            self.store.event('playback_blocked', {'cause': 'excluded_or_missing'}, self.session, track['id'])
            # 不能让永久排除歌曲从已经生成的队列重新进入播放。
            if self.index + 1 < len(self.playlist):
                self.index += 1
                return self._load(self.playlist[self.index], 0)
            self.stats = None
            return
        self.loaded = False
        self.current, self.position, self.action = track, position, None
        self.stats = PlaybackStats(track['duration'])
        self.mpv.command('loadfile', track['path'], 'replace', -1, {'start': str(position)})
        self.mpv.command('set_property', 'pause', False)
        self._save()

    def play(self, tracks, resume=False):
        with self.lock:
            self.playlist = tracks
            saved = self.store.load_session(self.session).get('player', {}) if resume else {}
            self.index = next((i for i, t in enumerate(tracks) if t['id'] == saved.get('track_id')), 0)
            if not tracks:
                raise ValueError('歌单为空')
            self._load(tracks[self.index], float(saved.get('position', 0)))
            if saved.get('stats') and saved.get('track_id') == self.current['id']:
                self.stats.intervals = saved['stats']['intervals']
                self.stats.listened = saved['stats']['listened']
                self.stats.gaps = saved['stats']['gaps'] + int(saved.get('running', False))

    def control(self, action, seconds=None):
        with self.lock:
            self.store.event('player_command', {'action': action, 'seconds': seconds}, self.session,
                             self.current['id'] if self.current else None)
            if action == 'pause':
                self.mpv.command('cycle', 'pause')
            elif action == 'seek':
                if not math.isfinite(float(seconds)) or not self.current:
                    raise ValueError('定位需要有效的秒数和正在播放的歌曲')
                seconds = min(self.current['duration'], max(0, float(seconds)))
                if self.stats:
                    self.stats.discontinuity()
                self.mpv.command('seek', seconds, 'absolute+exact')
            elif action == 'next':
                # 先记用户意图，再终止旧歌；等待旧 end-file 消费，避免归到新歌。
                self.action = 'next'
                self.mpv.command('stop')
            else:
                raise ValueError('未知播放器操作')
        if action == 'next':
            deadline = time.monotonic() + 2
            while self.stats is not None and time.monotonic() < deadline:
                time.sleep(.02)
            with self.lock:
                if self.stats is not None:
                    raise RuntimeError('播放器停止事件未确认，不切换歌曲')
                if self.index + 1 < len(self.playlist):
                    self.index += 1
                    self._load(self.playlist[self.index], 0)

    def close(self):
        with self.lock:
            self._save(running=False)
            self.action = 'exit'
            if self.stats and self.current:
                self.store.event('playback_summary', self.stats.result('quit', 'exit'), self.session, self.current['id'])
                self.stats = None
            self.stopping.set()
        self.mpv.close()
        self.worker.join(timeout=3)
