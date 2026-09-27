"""Where bundled files live (source checkout or PyInstaller bundle) and how to run ffmpeg quietly."""
import os
import shutil
import subprocess
import sys


def base_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # PyInstaller: the folder with bundled data
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def self_command():
    """How to start this program again (for helper processes)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "vidaudcont"]


def models_dir():
    return os.environ.get("VIDAUDCONT_MODELS") or os.path.join(base_dir(), "models")


def asset(name):
    return os.path.join(base_dir(), "assets", name)


def tool(name):
    """Path to ffmpeg / ffprobe: the bundled copy first, then PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = os.path.join(base_dir(), "bin", exe)
    if os.path.exists(bundled):
        return bundled
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found (expected {bundled})")


# no console window pops up for every ffmpeg call on Windows
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd, **kw):
    kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kw)


def popen(cmd, **kw):
    kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.Popen(cmd, **kw)
