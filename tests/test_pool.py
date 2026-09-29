"""Several analyses at once in separate processes: same results, "Остановить", one analysis crashing."""
import os
import shutil
import threading
import time
from dataclasses import asdict

import pytest

from vidaudcont import resources
from vidaudcont.engine.analyzer import Settings
from vidaudcont.engine.pool import AnalysisPool, plan

needs_models = pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")


def test_plan_keeps_the_total_number_of_threads():
    assert plan(8) == (4, 2)      # Ryzen 7 3700X by default: 8 of its 16 threads
    assert plan(16) == (4, 4)
    assert plan(6) == (3, 2)
    assert plan(3) == (1, 3)
    assert plan(1) == (1, 1)
    assert plan(16, ram_gb=8) == (2, 8)   # memory limits the number of processes
    assert plan(8, ram_gb=4) == (1, 8)


def test_queued_jobs_can_be_taken_back():
    pool = AnalysisPool(lambda *a: None, lambda *a: None, lambda *a: None)  # not configured: nothing takes the jobs
    for fid, kind in ((1, "analyze"), (2, "analyze"), (1, "transcribe"), (3, "dialogue")):
        pool.add(fid, "x.m4a", {}, kind=kind)
    assert pool.remove({1, 3}) == [(1, "analyze"), (1, "transcribe"), (3, "dialogue")]
    assert pool.pending == 1 and pool.remove(set()) == [] and pool.remove({9}) == []
    assert pool.cancel_all() == [(2, "analyze")] and pool.pending == 0


class Collector:
    def __init__(self):
        self.done, self.failed, self.progress = {}, {}, set()
        self.cond = threading.Condition()

    def on_progress(self, fid, frac, text):
        with self.cond:
            self.progress.add(fid)
            self.cond.notify_all()

    def on_done(self, fid, kind, res):
        with self.cond:
            self.done[fid] = res
            self.cond.notify_all()

    def on_failed(self, fid, kind, msg):
        with self.cond:
            self.failed[fid] = msg
            self.cond.notify_all()

    def wait(self, cond, timeout=600):
        with self.cond:
            assert self.cond.wait_for(cond, timeout), "timeout"


@pytest.fixture
def clip(tmp_path):
    p = tmp_path / "клип.m4a"
    shutil.copy(resources.asset("selftest.m4a"), p)
    return str(p)


@needs_models
def test_two_files_at_once_give_the_same_result(clip):
    c = Collector()
    pool = AnalysisPool(c.on_progress, c.on_done, c.on_failed)
    try:
        assert pool.configure(2, workers=2) == (2, 1)
        for fid in (1, 2):
            pool.add(fid, clip, asdict(Settings()))
        c.wait(lambda: len(c.done) + len(c.failed) == 2)
        assert not c.failed, c.failed
        assert c.done[1]["timecodes"] == c.done[2]["timecodes"]
        assert c.done[1]["timecodes"].startswith("start-0:0")
        assert c.progress == {1, 2}
        assert len({s.proc.pid for s in pool.slots if s.proc}) == 2  # two processes did the work
        assert pool.pending == 0
    finally:
        pool.stop()


@needs_models
def test_stop_and_a_crashed_analysis(clip):
    c = Collector()
    pool = AnalysisPool(c.on_progress, c.on_done, c.on_failed)
    try:
        pool.configure(2, workers=1)
        for fid in (1, 2, 3):
            pool.add(fid, clip, asdict(Settings()))
        c.wait(lambda: 1 in c.progress)
        assert sorted(pool.cancel_all()) == [(2, "analyze"), (3, "analyze")]   # the queue is dropped...
        c.wait(lambda: 1 in c.failed)
        assert c.failed[1] == "остановлено"                  # ...and the running analysis stopped
        t = time.time()
        while pool.pending and time.time() - t < 30:
            time.sleep(0.05)
        assert pool.pending == 0

        pool.add(4, clip, asdict(Settings()))
        c.wait(lambda: 4 in c.progress)
        pool.slots[0].proc.kill()                            # the analysis process dies on its own
        c.wait(lambda: 4 in c.failed)
        assert "аварийно" in c.failed[4]
        pool.add(5, clip, asdict(Settings()))                # the next file gets a new process
        c.wait(lambda: 5 in c.done or 5 in c.failed)
        assert 5 in c.done, c.failed
    finally:
        pool.stop()
