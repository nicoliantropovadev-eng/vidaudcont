"""A small log next to the program's settings: what happened, and why it closed if it crashed.

faulthandler writes the stack of every thread there if the process dies inside native code (the
case where no error window can be shown). A marker file tells the next start that the previous run
did not end normally.
"""
import faulthandler
import os
import platform
import sys
import threading
import time
import traceback

_file = None
_path = None
_marker = None


def setup(folder, version, max_bytes=2_000_000):
    """Start logging into <folder>/vidaudcont.log. Returns (log path, previous run crashed?)."""
    global _file, _path, _marker
    os.makedirs(folder, exist_ok=True)
    _path = os.path.join(folder, "vidaudcont.log")
    _marker = os.path.join(folder, "running.marker")
    crashed = os.path.exists(_marker)
    if os.path.exists(_path) and os.path.getsize(_path) > max_bytes:
        os.replace(_path, _path + ".old")
    _file = open(_path, "a", encoding="utf-8", buffering=1)
    log_event(f"=== запуск VidAudCont {version} | {platform.platform()} | Python {sys.version.split()[0]} | "
              f"потоков CPU: {os.cpu_count()}" + (" | ПРЕДЫДУЩИЙ ЗАПУСК ЗАВЕРШИЛСЯ АВАРИЙНО" if crashed else ""))
    faulthandler.enable(file=_file, all_threads=True)

    def hook(exc_type, exc, tb):
        log_event("необработанная ошибка:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    with open(_marker, "w") as f:
        f.write(str(os.getpid()))
    return _path, crashed


def log_event(msg):
    if _file is None:
        return
    try:
        _file.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg).rstrip() + "\n")
    except Exception:
        pass


def clean_exit():
    log_event("выход")
    if _marker and os.path.exists(_marker):
        os.remove(_marker)


def log_path():
    return _path
