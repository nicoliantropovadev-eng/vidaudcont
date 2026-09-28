"""Hand-over to 4K Video Downloader+: a list of links to paste, and a folder to watch for the files.

4K Video Downloader+ has no command line. Its File → Import only reads its own export files, but
"Paste Link" takes many links from the clipboard at once and, in Smart Mode, saves them as M4A
into a chosen folder. The program copies the links from the sheet and picks up every file that
finishes downloading in the folder.
"""
import os

from . import cutter
from .sheet import row_marked_red

MEDIA_EXT = {".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif", ".alac",
             ".ape", ".wv", ".amr", ".ac3", ".eac3", ".mka", ".caf", ".m4b", ".mp2", ".mp4", ".m4v", ".mov", ".mkv",
             ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".3g2", ".ogv",
             ".vob", ".asf", ".dv", ".mxf", ".f4v", ".rm", ".rmvb"}


def norm_path(path):
    """One spelling per file. Windows takes "D:/Загрузки\\a.m4a" and "d:\\загрузки\\A.m4a" for the same
    file, and folder dialogs give forward slashes while the file list keeps backslashes."""
    return os.path.normcase(os.path.abspath(path))


def clean_link(video_id):
    """Plain video link: a "&list=…" part would make the downloader fetch the whole playlist."""
    return f"https://www.youtube.com/watch?v={video_id}"


def first_rows(rows):
    """video id -> the first row it appears in. Later rows with the same video are repeats."""
    first = {}
    for r in sorted(rows, key=lambda r: r["row"]):
        first.setdefault(r["id"], r["row"])
    return first


def repeats(rows):
    """{row: first row of the same video} for every repeated link in the whole table."""
    first = first_rows(rows)
    return {r["row"]: first[r["id"]] for r in rows if first[r["id"]] != r["row"]}


def select_links(rows, first, last, skip_filled=True, skip_rows=(), stats=None):
    """rows: [{row, id, cuts, note, bg, fg}] from the sheet -> [(row, link)] in table order, one per video.
    Left out: rows marked red, repeats of a video from an earlier row anywhere in the table (even one already
    done), rows with timecodes or a note. stats (a dict) receives how many were left out and why."""
    out, rep = [], repeats(rows)
    skip_rows = set(skip_rows)
    counts = {"red": 0, "repeat": 0, "filled": 0, "have": 0}
    for r in sorted(rows, key=lambda r: r["row"]):
        if not first <= r["row"] <= last:
            continue
        if row_marked_red(r):
            counts["red"] += 1
        elif r["row"] in rep:
            counts["repeat"] += 1
        elif skip_filled and ((r.get("cuts") or "").strip() or (r.get("note") or "").strip()):
            counts["filled"] += 1  # done, or marked as not fitting / not downloadable
        elif r["row"] in skip_rows:
            counts["have"] += 1
        else:
            out.append((r["row"], clean_link(r["id"])))
    if stats is not None:
        stats.update(counts)
    return out


def write_links(path, links):
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(link for _, link in links) + "\n")


class FolderWatcher:
    """Finds files that have finished downloading: size unchanged between two looks and readable.
    scan() may be slow (it opens each new file) and is meant to run outside the window's thread."""

    def __init__(self, folder):
        self.folder = folder
        self.seen = {}      # norm_path -> (size, mtime) at the previous look
        self.done = set()   # norm_paths already handed over

    def scan(self, known=()):
        if not self.folder or not os.path.isdir(self.folder):
            return []
        known = {norm_path(p) for p in known}
        ready = []
        for name in sorted(os.listdir(self.folder)):
            path = os.path.abspath(os.path.join(self.folder, name))
            key = norm_path(path)
            if (name.startswith(".") or os.path.splitext(name)[1].lower() not in MEDIA_EXT
                    or key in self.done or key in known or not os.path.isfile(path)):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            prev, self.seen[key] = self.seen.get(key), sig
            if prev != sig or st.st_size == 0:
                continue  # new or still growing: look again next time
            try:
                complete = cutter.probe(path)["duration"] > 0  # an unfinished .m4a has no index yet
            except Exception:
                complete = False
            if complete:
                self.done.add(key)
                ready.append(path)
        return ready
