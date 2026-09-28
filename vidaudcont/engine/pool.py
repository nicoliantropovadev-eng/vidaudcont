"""Several analyses at once, each in its own process.

One analysis already runs on several threads, but parts of it (decoding, the work between the
models) use a single core. With the same total number of threads, measured on a 477-s recording
on 6 cores: one file on 6 threads 64 s; two at once on 3 threads each 45 s per file; three on 2
threads each 40 s per file, with identical timecodes. The processes share nothing, so if an
analysis crashes inside native code, only that file fails while the program and the other
analyses go on.
"""
import multiprocessing
import os
import queue
import sys
import threading
import traceback

MAX_WORKERS = 4
MIN_THREADS_EACH = 2
RAM_PER_WORKER_GB = 1.5  # measured peak of one analysis: 1.3 GB
RAM_RESERVE_GB = 4       # left for the system and everything else that runs


def total_ram_gb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("total", "avail", "total_page", "avail_page",
                                                             "total_virtual", "avail_virtual", "avail_ext")]
            m = MemoryStatus()
            m.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / 2 ** 30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except (OSError, ValueError, AttributeError):
        return None


def plan(threads, ram_gb=None):
    """(processes, threads each) for a budget of `threads` processor threads; the total stays within it."""
    n = max(1, min(MAX_WORKERS, threads // MIN_THREADS_EACH))
    if ram_gb:
        n = max(1, min(n, int((ram_gb - RAM_RESERVE_GB) // RAM_PER_WORKER_GB)))
    return n, max(1, threads // n)


def _worker_main(conn, threads):
    """The analysis process: loads the models once, then analyses the files sent through `conn`."""
    from .analyzer import Models, Settings, analyze, dialogue_structure, load_audio
    models = Models(threads=threads)
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        try:
            if job.get("kind") == "dialogue":  # only the conversation check, for files analysed before it existed
                res = {"dialogue": dialogue_structure(models, load_audio(job["path"], 16000), job["segments"])}
            else:
                res = analyze(job["path"], models, Settings(**job["settings"]),
                              progress=lambda f, t: conn.send(("progress", float(f), t)))
            conn.send(("done", res))
        except Exception as e:  # reported for this file; the process takes the next one
            conn.send(("error", f"{e}\n\n{traceback.format_exc(limit=3)}"))


class AnalysisPool:
    """A queue of analyses served by up to `workers` processes. The callbacks are called from the pool's
    threads: on_progress(fid, fraction, text), on_done(fid, kind, result), on_failed(fid, kind, message).
    kind is "analyze" (the whole analysis) or "dialogue" (only the conversation check)."""

    def __init__(self, on_progress, on_done, on_failed):
        self.on_progress, self.on_done, self.on_failed = on_progress, on_done, on_failed
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.pending = 0
        self.generation = 0  # raised by cancel_all: jobs queued before it are not analysed
        self.skip = set()    # files no longer wanted (a red row, a second copy): dropped when their turn comes
        self.workers, self.threads = 1, 1
        self.slots = []
        self.closing = False
        self.ctx = multiprocessing.get_context("spawn")

    def configure(self, threads, workers=None):
        """Share `threads` processor threads between the processes (their number is chosen from the
        threads and the memory unless given). Running analyses finish with the old values."""
        if workers:
            n, each = workers, max(1, threads // workers)
        else:
            n, each = plan(threads, total_ram_gb())
        with self.lock:
            self.workers, self.threads = n, each
            while len(self.slots) < n:
                slot = _Slot(self, len(self.slots))
                self.slots.append(slot)
                slot.start()
        return n, each

    def add(self, fid, path, settings, kind="analyze", extra=None):
        with self.lock:
            self.pending += 1
            gen = self.generation
        self.jobs.put((fid, path, dict(settings), gen, kind, dict(extra or {})))

    def drop(self, fid):
        """Do not analyse this file when its turn comes (an analysis already running finishes)."""
        with self.lock:
            self.skip.add(fid)

    def cancel_all(self):
        """Drop the queue (returns the dropped ids) and stop the running analyses: they report "остановлено"."""
        with self.lock:
            self.generation += 1
        dropped = []
        try:
            while True:
                dropped.append(self.jobs.get_nowait()[0])
                with self.lock:
                    self.pending -= 1
        except queue.Empty:
            pass
        for slot in self.slots:
            slot.kill_running()
        return dropped

    def stop(self):
        self.closing = True
        self.cancel_all()
        for slot in self.slots:
            slot.join(10)


class _Slot(threading.Thread):
    """Feeds one analysis process from the pool's queue; starts it again if it dies or the threads change."""

    def __init__(self, pool, index):
        super().__init__(daemon=True, name=f"analysis-slot-{index}")
        self.pool, self.index = pool, index
        self.proc = self.conn = None
        self.proc_threads = 0
        self.busy = False

    def run(self):
        pool = self.pool
        while not pool.closing:
            if self.index >= pool.workers:  # fewer processes wanted now
                self._close()
                threading.Event().wait(0.5)
                continue
            try:
                fid, path, settings, gen, kind, extra = pool.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self.busy = True
            try:
                if gen != pool.generation:  # taken from the queue just as it was stopped
                    pool.on_failed(fid, kind, "остановлено")
                elif fid in pool.skip:
                    pool.skip.discard(fid)
                    pool.on_failed(fid, kind, "пропущено")
                else:
                    self._analyse(fid, path, settings, gen, kind, extra)
            except Exception:  # never lose the slot: report and go on
                pool.on_failed(fid, kind, traceback.format_exc(limit=3))
            finally:
                self.busy = False
                with pool.lock:
                    pool.pending -= 1
        self._close()

    def _analyse(self, fid, path, settings, gen, kind, extra):
        pool = self.pool
        threads = pool.threads
        if self.proc is None or not self.proc.is_alive() or self.proc_threads != threads:
            self._close()
            parent, child = pool.ctx.Pipe()
            self.proc = pool.ctx.Process(target=_worker_main, args=(child, threads), daemon=True,
                                         name=f"VidAudCont-analysis-{self.index}")
            self.proc.start()
            child.close()
            self.conn, self.proc_threads = parent, threads
        if gen != pool.generation:
            pool.on_failed(fid, kind, "остановлено")
            return
        try:
            self.conn.send(dict(extra, path=path, kind=kind, settings=dict(settings, threads=threads)))
            while True:
                what, *rest = self.conn.recv()
                if what == "progress":
                    pool.on_progress(fid, *rest)
                elif what == "done":
                    pool.on_done(fid, kind, rest[0])
                    return
                else:
                    pool.on_failed(fid, kind, rest[0])
                    return
        except (EOFError, OSError):  # the process is gone: stopped by us, or it crashed
            self.proc.join(5)
            code = self.proc.exitcode
            self._close()
            pool.on_failed(fid, kind, "остановлено" if gen != pool.generation else
                           f"анализ прервался: процесс анализа завершился аварийно (код {code}). "
                           "Попробуйте проанализировать этот файл ещё раз.")

    def kill_running(self):
        proc = self.proc
        if self.busy and proc is not None and proc.is_alive():
            proc.kill()  # unblocks recv() in _analyse

    def _close(self):
        proc, conn = self.proc, self.conn
        self.proc = self.conn = None
        if proc is None:
            return
        try:
            conn.send(None)  # finish politely
        except (OSError, ValueError):
            pass
        proc.join(3)
        if proc.is_alive():
            proc.kill()
            proc.join(3)
        conn.close()
