"""Which sheet row does a downloaded file belong to?

Downloaders name files after the video title - often YouTube's automatic translation into the
viewer's language ("How Do You Partner…" -> "Как работать с немотивированным пациентом.m4a"),
with characters like "/" "|" "?" dropped and long titles cut short. So the program fetches, for
every video in the sheet, the title in the same language (and the original one) and compares
normalised names. The video id in the name wins. A number at the start of a name is usually part of
the title ("7 Tips for…"): it counts as a row only when the name is nothing but that number ("80.m4a").
"""
import concurrent.futures as cf
import difflib
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
ID_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
PREFIX_RE = re.compile(r"^\s*(\d{1,5})\s*(?:[.)_\-–—]|\s|$)")
ROW_ONLY_RE = re.compile(r"\s*(\d{1,5})\s*(?:\(\d{1,3}\))?\s*")


def normalize(s):
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return "".join(ch for ch in s if ch.isalnum())


def file_keys(path):
    """Normalised name, with and without a leading number ("80. Title" / "01.Complete History …")."""
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    stem = re.sub(r"(\.{3}|…)$", "", stem)  # "…социальной тревожнос..." (cut by the downloader)
    keys = [normalize(stem)]
    copy = re.match(r"^(.*\S)\s*\(\d{1,3}\)$", stem)  # "Title (1)": the downloader's second copy of a file
    if copy:
        keys.append(normalize(copy.group(1)))
    if PREFIX_RE.match(stem):
        keys.append(normalize(PREFIX_RE.sub("", stem, count=1)))
    return [k for k in dict.fromkeys(keys) if len(k) >= 4]


# ------------------------------------------------------------------ titles from YouTube
def _post_json(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "Content-Type": "application/json", "User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
        "Cookie": "SOCS=CAI"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def localized_title(video_id, lang="ru"):
    """Title as YouTube shows it to a viewer with this language (its automatic translation, if any)."""
    body = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20250925.01.00", "hl": lang, "gl": "NL"}},
            "videoId": video_id}
    d = _post_json("https://www.youtube.com/youtubei/v1/next?prettyPrint=false", body)
    for c in d.get("contents", {}).get("twoColumnWatchNextResults", {}).get("results", {}).get("results", {}) \
            .get("contents", []):
        if "videoPrimaryInfoRenderer" in c:
            return "".join(r.get("text", "") for r in c["videoPrimaryInfoRenderer"]["title"].get("runs", []))
    return ""


def original_title(video_id):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={video_id}", safe="")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("title", "")


class TitleCache:
    """Titles per video id, kept on disk so each video is looked up once."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def fetch(self, ids, lang="ru", progress=None, workers=8, cancelled=None):
        todo = [i for i in dict.fromkeys(ids) if not self.data.get(i, {}).get("ok")]

        def one(vid):
            rec = {"ok": False, "t": time.time()}
            try:
                rec["orig"] = original_title(vid)
            except Exception as e:  # removed / private videos have no oEmbed
                rec["orig_error"] = str(e)[:120]
            try:
                rec[lang] = localized_title(vid, lang)
            except Exception as e:
                rec["loc_error"] = str(e)[:120]
            rec["ok"] = bool(rec.get("orig") or rec.get(lang))
            return vid, rec

        done = 0
        with cf.ThreadPoolExecutor(workers) as ex:
            for vid, rec in ex.map(one, todo):
                self.data[vid] = rec
                done += 1
                if progress:
                    progress(done / max(len(todo), 1), f"названия видео из YouTube: {done} из {len(todo)}")
                if cancelled and cancelled():
                    break
        self.save()
        return len(todo)


# ------------------------------------------------------------------ matching
def match_files(paths, rows, titles, lang="ru"):
    """paths: files; rows: [{row, id, ...}] from the sheet; titles: {id: {"orig":…, lang:…}}.
    Returns {path: (row or None, how)} where how is 'id в имени', 'название', 'похоже', 'номер строки'."""
    by_row = {r["row"]: r for r in rows}
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], r)
    keys = []  # (normalised title, row, video id)
    for r in rows:
        t = titles.get(r["id"], {})
        for k in {normalize(t.get(lang, "")), normalize(t.get("orig", ""))}:
            if len(k) >= 4:
                keys.append((k, r["row"], r["id"]))
    exact = {}
    for k, row, _ in keys:
        exact.setdefault(k, []).append(row)
    first = {}
    for _, row, vid in keys:
        first[vid] = min(row, first.get(vid, row))

    result = {}
    for p in paths:
        base = os.path.splitext(os.path.basename(p))[0]
        ids = [m for m in ID_RE.findall(base) if m in by_id]
        if ids:
            result[p] = (by_id[ids[0]]["row"], "id в имени")
            continue
        row, how = _by_title(file_keys(p), keys, exact, first)
        m = ROW_ONLY_RE.fullmatch(base)
        if row is None and m and int(m.group(1)) in by_row:  # "80.m4a", "80 (1).m4a": named after the row
            row, how = int(m.group(1)), "номер строки"
        result[p] = (row, how)
    return result


SIMILAR = 0.9       # a name this close to exactly one title (by difflib's ratio) is that title...
SIMILAR_GAP = 0.03  # ...when no other video's title comes within this much
FLOOR = SIMILAR - SIMILAR_GAP  # a score below this cannot change the answer: not worth computing exactly


def _by_title(fks, keys, exact, first):
    """A video can stand in several rows: candidates are videos, the answer is the first row of that video."""
    for fk in fks:
        if fk in exact:
            return min(exact[fk]), "название"
    for fk in fks:  # cut-off names: the file name is the beginning of exactly one title
        pref = {vid for k, _, vid in keys if len(fk) >= 20 and k.startswith(fk)}
        if len(pref) == 1:
            return first[pref.pop()], "название"
    best = {}  # video -> its best score, only where it can matter (>= FLOOR)
    sm = difflib.SequenceMatcher(None)
    for fk in fks:
        sm.set_seq2(fk)  # the file name is analysed once, the titles are compared against it
        for k, _, vid in keys:
            sm.set_seq1(k)
            # cheap upper bounds first: most titles are far off in length or letters (100 names x 2000 titles
            # took 11 s with ratio() alone, holding up the window)
            if sm.real_quick_ratio() < FLOOR or sm.quick_ratio() < FLOOR:
                continue
            r = sm.ratio()
            if r > best.get(vid, 0.0):
                best[vid] = r
    if not best:
        return None, ""
    ranked = sorted(best.items(), key=lambda x: -x[1])
    top_vid, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= SIMILAR and top - runner_up >= SIMILAR_GAP:
        return first[top_vid], "похоже"
    return None, ""
