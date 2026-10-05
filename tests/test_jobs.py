import threading
import time

from lify.jobs import Jobs
from lify.store import Store


def wait(jobs, job_id):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        value = jobs.get(job_id)
        if value["status"] not in ("queued", "running"):
            return value
        time.sleep(0.01)
    raise AssertionError("job did not settle")


def test_accepts_immediately_deduplicates_and_finishes_after_page_closes(tmp_path):
    gate = threading.Event()
    calls = []
    store = Store(tmp_path / "test.sqlite")

    def execute(session, payload, report):
        calls.append(session)
        report("parse")
        gate.wait(2)
        return {"items": []}

    jobs = Jobs(store, execute)
    try:
        start = time.monotonic()
        first = jobs.submit("id", "a", "session", {})
        assert time.monotonic() - start < 1 and first["status"] in ("queued", "running")
        assert jobs.submit("id", "a", "other", {})["session"] == "session"
        gate.set()
        assert wait(jobs, "id")["status"] == "completed"
        assert calls == ["session"]
    finally:
        jobs.close()


def test_restart_does_not_execute_unfinished_jobs(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    gate = threading.Event()

    def execute(session, payload, report):
        gate.wait(2)
        report("retrieve")
        return {}

    jobs = Jobs(store, execute)
    jobs.submit("id", "a", "s", {})
    time.sleep(0.03)
    jobs.close()
    gate.set()
    wait(jobs, "id")
    calls = []
    other = Jobs(store, lambda *args: calls.append(args))
    try:
        assert other.get("id")["status"] == "interrupted"
        assert calls == []
    finally:
        other.close()
