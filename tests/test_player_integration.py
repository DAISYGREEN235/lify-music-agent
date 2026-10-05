"""需显式 RUN_MPV_TEST=1；真 mpv、真 IPC、合成短音频、无声输出，无外部 API。"""
import os
import time
import wave

import pytest

from lify.config import Settings
from lify.player import Player
from lify.store import Store

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MPV_TEST') != '1', reason='显式启用真实无声播放器验证')


def wait_until(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('等待播放器事件超时')


def test_real_mpv_play_pause_seek_next_restore(tmp_path):
    settings = Settings.load()
    store = Store(tmp_path / 'player.sqlite')
    audio = tmp_path / 'tone.wav'
    with wave.open(str(audio), 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 8000 * 8)
    tracks = [{'id': str(i), 'path': str(audio), 'duration': 8} for i in range(2)]
    player = Player(settings, store, 'integration', silent=True)
    try:
        player.play(tracks)
        wait_until(lambda: player.position > 1)
        assert player.stats.listened > .5
        player.control('pause')
        time.sleep(.2)
        player.control('seek', 5)
        player.control('pause')
        wait_until(lambda: player.position > 5)
        player.control('next')
        wait_until(lambda: player.index == 1 and player.position > .5)
    finally:
        player.close()
    events = store.events('integration')
    summaries = [e['payload'] for e in events if e['kind'] == 'playback_summary']
    assert any(s['early_skip'] for s in summaries)
    assert not any(s['complete'] for s in summaries)
    assert not store.load_session('integration')['player']['running']
    saved_position = store.load_session('integration')['player']['position']
    restored = Player(settings, store, 'integration', silent=True)
    try:
        assert restored.current is None  # 创建播放器不会自动播放
        restored.play(tracks, resume=True)
        wait_until(lambda: restored.position >= saved_position)
        assert restored.current['id'] == '1'
    finally:
        restored.close()


def test_real_eof_requires_observed_coverage(tmp_path):
    settings = Settings.load()
    store = Store(tmp_path / 'eof.sqlite')
    audio = tmp_path / 'short.wav'
    with wave.open(str(audio), 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 8000 * 4)
    player = Player(settings, store, 'eof', silent=True)
    try:
        player.play([{'id': 'eof', 'path': str(audio), 'duration': 4}])
        wait_until(lambda: any(e['kind'] == 'playback_summary' for e in store.events('eof')))
        summary = next(e['payload'] for e in store.events('eof') if e['kind'] == 'playback_summary')
        assert summary['reason'] == 'eof' and summary['complete']
        assert .8 <= summary['coverage'] <= 1
    finally:
        player.close()


def test_abrupt_python_exit_does_not_leave_mpv_running(tmp_path):
    import subprocess
    import sys
    import win32api
    import win32event
    import win32con
    child = subprocess.Popen([sys.executable, '-c',
                              'import os; from lify.config import Settings; from lify.player import MPV; '
                              'p=MPV(Settings.load().mpv_path,lambda e:None,silent=True); '
                              'print(p.process.pid,flush=True); input(); os._exit(0)'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handle = None
    try:
        pid = int(child.stdout.readline().strip())
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        child.communicate('\n', timeout=10)
        assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
    finally:
        if handle:
            handle.Close()
        if child.poll() is None:
            child.kill()
            child.wait()
