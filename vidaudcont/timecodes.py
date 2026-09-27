"""Cut lists in the sheet's format: "start-0:13, 3:10-3:17, 6:35-end" or "good"."""
import math
import re

_DASH = r"\s*[-–—]\s*"
_TIME = r"(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?"
_ITEM = re.compile(rf"^(start|начало|{_TIME}){_DASH}(end|конец|{_TIME})$", re.IGNORECASE)


def fmt_time(t, floor=False):
    t = max(t, 0.0)
    t = int(math.floor(t)) if floor else int(round(t))
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(s):
    s = s.strip()
    if ":" not in s:
        return float(s)
    v = 0.0
    for part in s.split(":"):
        v = v * 60 + float(part)
    return v


def parse(text, duration=None):
    """Return [(start, end)] in seconds. 'end' becomes `duration` (or math.inf if unknown).

    Raises ValueError with a readable message for anything that is not a cut.
    """
    text = (text or "").strip()
    if not text or text.lower() in ("good", "ok", "хорошо"):
        return []
    cuts = []
    for raw in re.split(r"[,;\n]+", text):
        item = raw.strip()
        if not item:
            continue
        m = _ITEM.match(item)
        if not m:
            raise ValueError(f"не понимаю «{item}» — нужен вид 1:23-1:45, start-0:13 или 6:35-end")
        a, b = m.group(1).lower(), m.group(2).lower()
        start = 0.0 if a in ("start", "начало") else parse_time(a)
        end = (duration if duration is not None else math.inf) if b in ("end", "конец") else parse_time(b)
        if duration is not None:
            end = min(end, duration)
        if end <= start:
            raise ValueError(f"«{item}»: конец раньше начала")
        cuts.append((start, end))
    cuts.sort()
    merged = []
    for s, e in cuts:  # overlapping or touching cuts are one cut
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def format_cuts(cuts, duration):
    """[(start, end)] -> 'start-0:13, 3:10-3:17, 6:35-end' ('good' when empty)."""
    if not cuts:
        return "good"
    out = []
    for s, e in cuts:
        a = "start" if s <= 0.0 else fmt_time(s)
        b = "end" if e >= duration else fmt_time(e, floor=True)
        out.append(f"{a}-{b}")
    return ", ".join(out)


def keep_segments(cuts, duration):
    """Complement of the cuts inside [0, duration]."""
    keep, t = [], 0.0
    for s, e in sorted(cuts):
        if s > t:
            keep.append((t, s))
        t = max(t, e)
    if t < duration:
        keep.append((t, duration))
    return [(a, b) for a, b in keep if b - a > 0.05]
