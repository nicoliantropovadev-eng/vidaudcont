"""Do the row numbers in file names match the sheet?

A file is recognised by what is inside it, not by its name: the video's link, which the program
writes into every file it cuts, or the video's title, which 4K Video Downloader+ writes into the
files it downloads and which the cut files keep. Then the number at the start of the name is
compared with the row of that video in the sheet (the first row, if the video stands in several).
"""
import json
import os
import re

from . import resources
from .downloads import MEDIA_EXT, first_rows, norm_path
from .matching import match_files

NUM_RE = re.compile(r"^(\d{1,5})(?!\d)")
LINK_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
LINK_TAGS = ("comment", "description", "purl", "synopsis")


def file_tags(path):
    """The container's text tags (title, comment, …), keys in lower case."""
    out = resources.run([resources.tool("ffprobe"), "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        tags = json.loads(out.stdout or "{}").get("format", {}).get("tags", {})
    except ValueError:
        return {}
    return {k.lower(): v for k, v in tags.items()}


def identify(path, rows, titles, lang="ru", sources=None):
    """(video id, how it was recognised) or (None, why not)."""
    ids = {r["id"] for r in rows}
    by_row = {r["row"]: r["id"] for r in rows}
    tags = file_tags(path)
    for key in LINK_TAGS:
        m = LINK_ID_RE.search(tags.get(key, ""))
        if m and m.group(1) in ids:
            return m.group(1), "ссылка внутри файла"
    source = (sources or {}).get(norm_path(path))
    if source and os.path.exists(source):  # a file this program cut: what is its source?
        vid, how = identify(source, rows, titles, lang)
        if vid:
            return vid, f"{how} (исходный файл {os.path.basename(source)})"
    title = (tags.get("title") or "").strip()
    if len(title) >= 4:
        fake = re.sub(r"[\\/]", " ", title) + ".m4a"
        row, _ = match_files([fake], rows, titles, lang)[fake]
        if row:
            return by_row[row], f"название внутри файла: «{title}»"
    stem = os.path.splitext(os.path.basename(path))[0]
    if not NUM_RE.fullmatch(stem.strip()):  # a name that is only a number says nothing about the video
        row, _ = match_files([path], rows, titles, lang)[path]
        if row:
            return by_row[row], "название файла"
    return None, "не удалось узнать видео: нет ни ссылки, ни названия, которые есть в таблице"


def check_folder(folder, rows, titles, lang="ru", sources=None, progress=None):
    """Media files in `folder` whose name starts with a number: [{path, name, number, row, how, new}].
    row is None when the video was not recognised; new is None when nothing has to change."""
    first = first_rows(rows)
    names = sorted(n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in MEDIA_EXT
                   and not n.startswith(".") and NUM_RE.match(n) and os.path.isfile(os.path.join(folder, n)))
    out = []
    for i, name in enumerate(names):
        if progress:
            progress(i / max(len(names), 1), f"проверка имён: {i + 1} из {len(names)}")
        path = os.path.join(folder, name)
        vid, how = identify(path, rows, titles, lang, sources)
        row = first.get(vid) if vid else None
        number = int(NUM_RE.match(name).group(1))
        out.append({"path": path, "name": name, "number": number, "row": row, "how": how,
                    "new": NUM_RE.sub(str(row), name, count=1) if row and row != number else None})
    taken = {n.lower() for n in os.listdir(folder)} - {x["name"].lower() for x in out if x["new"]}
    for x in out:  # two files for one row, or a name already in use: "81 (2).m4a"
        if not x["new"]:
            continue
        base, ext = os.path.splitext(x["new"])
        k = 2
        while x["new"].lower() in taken:
            x["new"] = f"{base} ({k}){ext}"
            k += 1
        taken.add(x["new"].lower())
    return out


def rename(folder, plan):
    """Carries out the renames of check_folder (swaps like 80 <-> 81 included). Returns [(old path, new path)]."""
    moves = [(os.path.join(folder, x["name"]), os.path.join(folder, x["new"])) for x in plan if x.get("new")]
    staged = []
    for i, (old, new) in enumerate(moves):  # first out of the way, so that no name is overwritten
        tmp = os.path.join(folder, f".vidaudcont-rename-{i}-{os.path.basename(old)}")
        os.replace(old, tmp)
        staged.append((old, tmp, new))
    done = []
    for old, tmp, new in staged:
        base, ext = os.path.splitext(new)
        k = 2
        while os.path.exists(new):  # a file with that name appeared since the check: never overwrite it
            new = f"{base} ({k}){ext}"
            k += 1
        os.replace(tmp, new)
        done.append((old, new))
    return done
