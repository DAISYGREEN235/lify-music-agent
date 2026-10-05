"""用本机Edge无声验证真实MP3解码和页面交互，不调用模型或改动正式会话。"""
import json
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from lify.config import Settings
from lify.labels import Labels
from lify.store import Store
from lify.web import create_app


def main():
    settings = Settings.load()
    source = Store(settings.data_dir / 'lify.sqlite')
    playable = [t for t in Labels(source).apply(source.tracks())
                if Path(t['path']).is_file() and t['duration'] > 40]
    # 有标签时连同实际索引一起验证展示；尚未建标签的曲库仍可验证播放。
    track = next((t for t in playable if t.get('audio_tag_profile')), playable[0])
    with tempfile.TemporaryDirectory(prefix='lify-web-test-') as directory:
        store = Store(Path(directory) / 'test.sqlite')
        store.put_track(track)
        result = {'items': [{'track': track, 'evidence': {}}],
            'intent': {'scene': '浏览器验证', 'count': 1}, 'total_seconds': track['duration'], 'warnings': []}
        store.save_session('default', {'result': result})
        class TestWorkflow:
            def run(self, session, **kwargs):
                # 验证真实网页轮询，故意延迟模拟供应商；不会发起付费调用。
                self.on_progress('parse')
                time.sleep(1.5)
                self.on_progress('choose')
                time.sleep(1.5)
                store.update_session(session, {'result': result})
                return result

            def close(self):
                pass
        app = create_app(settings, store, TestWorkflow)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18765, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.1)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:18765')
                page.get_by_role('button', name='试听', exact=True).wait_for()
                if track.get('audio_tag_profile'):
                    tag_text = page.locator('.reasons').text_content()
                    assert '音频标签估计' in tag_text and track['audio_tag_profile']['version'] in tag_text
                page.locator('#request').fill('browser test')
                page.get_by_role('button', name='新建推荐', exact=True).click()
                page.wait_for_function('document.querySelector("#status").textContent.includes("理解需求")')
                page.wait_for_function('document.querySelector("#status").textContent.includes("歌单已生成")')
                task = page.evaluate('localStorage.getItem("lify.task")')
                page.eval_on_selector('#audio', '(a)=>a.muted=true')
                page.get_by_role('button', name='试听', exact=True).click()
                page.wait_for_function('document.querySelector("#audio").currentTime>2')
                page.eval_on_selector('#audio', '(a)=>a.pause()')
                page.eval_on_selector('#audio', '(a)=>a.currentTime=30')
                page.wait_for_function('Math.abs(document.querySelector("#audio").currentTime-30)<.3')
                page.eval_on_selector('#audio', '(a)=>a.play()')
                page.wait_for_function('document.querySelector("#audio").currentTime>31')
                page.eval_on_selector('#audio', '(a)=>a.pause()')
                page.locator('.feedback select').nth(0).select_option('0')
                page.locator('.feedback select').nth(1).select_option('topic')
                page.locator('.feedback textarea').fill('题材不适合当前场景')
                page.get_by_role('button', name='保存反馈', exact=True).click()
                page.wait_for_function('document.querySelector("#history").textContent.includes("题材不适合")')
                page.wait_for_timeout(1000)
                page.reload()
                page.get_by_role('button', name='恢复上次位置').click()
                page.wait_for_function('document.querySelector("#audio").currentTime>30')
                assert page.eval_on_selector('#audio', '(a)=>a.paused')
                if track.get('audio_tag_profile'):
                    page.locator('.reasons summary').click()
                    page.get_by_text('音频标签估计（相对相似度）', exact=False).wait_for(state='visible')
                assert not errors, errors
                artifact = settings.data_dir / 'web-verification.png'
                page.screenshot(path=str(artifact), full_page=True)
                events = store.events(task)
                assert any(e['kind'] == 'web_feedback' for e in events)
                assert any(e['kind'] == 'browser_raw' and e['payload']['event'] == 'seek' for e in events)
                assert store.load_session(task)['browser_player']['position'] > 30
                print(json.dumps({'real_mp3': True, 'played': True, 'seek': True, 'feedback_saved': True,
                    'restored_paused': True, 'live_progress': True,
                    'audio_tags_displayed': bool(track.get('audio_tag_profile')),
                    'page_errors': errors, 'screenshot': str(artifact)}, ensure_ascii=False))
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    main()
