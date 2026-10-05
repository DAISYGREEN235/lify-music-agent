from lify.preferences import Preferences, interpret
from lify.store import Store


def test_text_wins_pool_retained_and_users_isolated(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    p = Preferences(store)
    store.save_session("a", {"user_id": "a", "result": {"intent": {"scene": "学习"}}})
    store.save_session("b", {"user_id": "b", "result": {"intent": {"scene": "学习"}}})
    p.feedback("a", "song", 2, "sound", "很适合场景")
    _, effective = p.feedback("a", "song", 2, "sound", "严重不符合场景")
    assert effective["rating"] == 0 and effective["conflict"]
    assert p.scene("a", "学习")[0]["ever_confirmed"] == 1
    assert not p.scene("b", "学习")
    assert not p.scene("a", "休息")
    assert len(store.events("a", ["web_feedback"])) == 2
    event_id = store.events('a', ['web_feedback'])[-1]['id']
    p.revoke_feedback('a', event_id)
    assert p.latest('a')[0]['effective']['rating'] is None
    assert p.scene('a', '学习')[0]['ever_confirmed'] == 1
    assert interpret(2, "鼓点比较密")["rating"] is None
    p.ban("a", "song", True)
    assert p.excluded("a") == {"song"} and not p.excluded("b")
    p.ban("a", "song", False)
    assert not p.excluded("a")
    p.save_profile("a", {"life_stage": "学生"})
    assert not p.profile("b")
    p.save_profile("a", {})
    assert not p.profile("a")


def test_recent_exact_three_same_user_and_no_task_exclusions(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    p = Preferences(store)
    for n in range(5):
        store.event("recommendation", {"ids": [str(n)], "user_id": "a"}, str(n))
    store.event("recommendation", {"ids": ["b"], "user_id": "b"}, "b")
    assert p.recent("a") == {"2", "3", "4"}
    assert p.recent("b") == {"b"}
    assert not p.excluded("a")
