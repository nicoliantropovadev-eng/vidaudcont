"""Hand-over to 4K Video Downloader+: a list of links to paste, and a folder to watch for the files.

4K Video Downloader+ has no command line. Its File → Import only reads its own export files, but
"Paste Link" takes many links from the clipboard at once and, in Smart Mode, saves them as M4A
into a chosen folder. The program copies the links from the sheet and picks up every file that
finishes downloading in the folder.
"""
import os
import re

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


def parse_rows(spec):
    """"80-120, 150; 200 – 250" -> {80, …, 120, 150, 200, …, 250}; an empty text -> None (every row)."""
    text = (spec or "").strip()
    if not text:
        return None
    out = set()
    for part in re.split(r"[,;\s]+(?![^\d]*[-–—]|\.\.)", re.sub(r"\s*(-|–|—|\.\.)\s*", "-", text)):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,6})(?:-(\d{1,6}))?", part)
        if not m:
            raise ValueError(f"не понимаю «{part}»: пишите номера строк и диапазоны, например 80-120, 150")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if b < a:
            a, b = b, a
        if b - a > 100000:
            raise ValueError(f"слишком большой диапазон: {part}")
        out.update(range(a, b + 1))
    return out


def select_text_links(rows, wanted=None, done_rows=(), have_ids=(), skip_red=True, stats=None):
    """Links still needed for the transcripts: one per video that has a row in `wanted` (None: every row), whose
    text is in none of its rows of the transcripts sheet (done_rows) and whose file is not in the program (have_ids:
    those are transcribed from the file). stats (a dict) receives the counts."""
    done_rows, have_ids = set(done_rows), set(have_ids)
    videos = {}
    for r in sorted(rows, key=lambda r: r["row"]):
        videos.setdefault(r["id"], []).append(r)
    out, counts = [], {"videos": 0, "done": 0, "have": 0, "red": 0}
    for vid, vrows in videos.items():
        if wanted is not None and not any(r["row"] in wanted for r in vrows):
            continue
        counts["videos"] += 1
        if any(r["row"] in done_rows for r in vrows):
            counts["done"] += 1
        elif vid in have_ids:
            counts["have"] += 1
        elif skip_red and row_marked_red(vrows[0]):
            counts["red"] += 1
        else:
            out.append((vrows[0]["row"], clean_link(vid)))
    if stats is not None:
        stats.update(counts)
    return sorted(out)


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
