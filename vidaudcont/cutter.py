"""Lossless removal of time ranges from audio/video files.

Nothing is re-encoded: the kept parts are stream-copied with ffmpeg's concat demuxer, so every
audio/video packet in the result is a byte-for-byte copy of a packet from the source. After cutting,
`verify()` checks exactly that by comparing packet checksums.

Precision: audio is cut on codec frame boundaries (~21-26 ms for AAC/MP3). Video can only start a
kept part on a keyframe, so in "video" mode each kept part is widened to the surrounding keyframes;
"audio" mode drops the picture and keeps the exact audio cut points.

Lossless audio codecs (FLAC, ALAC, WAV/PCM, WavPack ...) are cut to the exact sample instead:
decoded, trimmed and encoded again with the same lossless codec and bit depth. The decoded samples
of the result are then compared with the source's (SHA-256 of the PCM) - they must be identical.
"""
import bisect
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import resources
from .timecodes import keep_segments

# audio codec -> container for "audio only" export (packets are copied unchanged)
AUDIO_CONTAINER = {
    "aac": ".m4a", "alac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg", "flac": ".flac",
    "ac3": ".ac3", "eac3": ".eac3", "mp2": ".mp2", "pcm_s16le": ".wav", "pcm_s24le": ".wav",
    "pcm_s32le": ".wav", "pcm_f32le": ".wav", "pcm_u8": ".wav", "pcm_s16be": ".aiff", "pcm_s24be": ".aiff",
}
MP4_LIKE = {".mp4", ".m4a", ".m4v", ".mov", ".3gp", ".m4b"}
# ffmpeg >= 9 starts a new chained Ogg stream when the concat demuxer switches files (it attaches
# "new extradata" to the first packet of each part); joining through NUT first avoids that
OGG_LIKE = {".ogg", ".oga", ".ogv", ".opus", ".spx"}
LOSSLESS = {"flac", "alac", "wavpack", "tta", "ape", "mlp", "truehd", "shorten", "als"}
LOSSLESS_ENCODER = {"flac": ("flac", ".flac"), "alac": ("alac", ".m4a"), "wavpack": ("wavpack", ".wv"),
                    "tta": ("tta", ".tta")}  # anything else lossless -> FLAC


def is_lossless(codec):
    return codec.startswith("pcm_") or codec in LOSSLESS


class CutError(RuntimeError):
    pass


class NothingLeft(CutError):
    """The cuts take the whole file: there is nothing to save."""


def probe(path):
    out = resources.run([resources.tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format",
                         "-show_entries", "stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,"
                         "width,height,avg_frame_rate,sample_fmt,bits_per_raw_sample"
                         ":stream_disposition=attached_pic:format=duration,format_name",
                         path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise CutError(f"не удалось прочитать файл: {out.stderr.strip()[-300:]}")
    d = json.loads(out.stdout)
    streams = d.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    dur = float(d.get("format", {}).get("duration") or 0.0)
    return {"duration": dur, "format": d.get("format", {}).get("format_name", ""), "video": video, "audio": audio}


def keyframes(path):
    """Presentation times of video keyframes (reads packets only, no decoding)."""
    out = resources.run([resources.tool("ffprobe"), "-v", "error", "-select_streams", "V:0", "-show_entries",
                         "packet=pts_time,flags", "-of", "csv=p=0", path], capture_output=True, text=True)
    times = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            times.append(float(parts[0]))
    return sorted(set(times))


def audio_packet_starts(path):
    """Start times of the first audio stream's packets (reads packets only)."""
    out = resources.run([resources.tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
                         "packet=pts_time", "-of", "csv=p=0", path], capture_output=True, text=True)
    times = []
    for line in out.stdout.splitlines():
        v = line.split(",")[0].strip()
        if v not in ("", "N/A"):
            times.append(float(v))
    return sorted(times)


def snap_to_packets(keep, starts, duration):
    """Move every boundary onto an audio packet start, so the joined parts line up without overlap.

    The concat demuxer shifts each part by (outpoint - inpoint); if that is not a whole number of
    packets the last packet of one part and the first of the next get the same timestamp.
    """
    starts = [t for t in starts if t >= 0.0]
    if len(starts) < 2:
        return keep
    out = []
    for a, b in keep:
        i = bisect.bisect_left(starts, a - 1e-6)
        a2 = starts[i] if i < len(starts) else duration
        j = bisect.bisect_left(starts, b - 1e-6)
        b2 = starts[j] if j < len(starts) else duration
        if b2 - a2 > 0.05:
            out.append((a2, b2))
    return out


def snap_to_keyframes(keep, kf, duration):
    """Widen every kept part to start on a keyframe and end on the next one (whole GOPs only)."""
    if not kf:
        return keep
    out = []
    for a, b in keep:
        i = bisect.bisect_right(kf, a + 1e-3) - 1
        a2 = kf[i] if i >= 0 else 0.0
        j = bisect.bisect_left(kf, b - 1e-3)
        b2 = kf[j] if j < len(kf) else duration
        if out and a2 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b2))
        else:
            out.append((a2, b2))
    return out


def plan(path, cuts, mode="auto"):
    """What will be kept. mode: 'auto' (video kept if present), 'video', 'audio'."""
    info = probe(path)
    if not info["audio"] and not info["video"]:
        raise CutError("в файле нет ни звука, ни видео")
    dur = info["duration"]
    keep = keep_segments(cuts, dur)
    if not keep:
        raise NothingLeft("после вырезания ничего не останется")
    has_video = bool(info["video"])
    if mode == "auto":
        mode = "video" if has_video else "audio"
    if mode == "video" and not has_video:
        mode = "audio"
    notes = []
    if mode == "video":
        snapped = snap_to_keyframes(keep, keyframes(path), dur)
        extra = sum(b - a for a, b in snapped) - sum(b - a for a, b in keep)
        if extra > 0.05:
            notes.append(f"видео режется только по ключевым кадрам: оставлено на {extra:.1f} с больше, чем в таймкодах")
        keep = snapped
    ext = os.path.splitext(path)[1].lower()
    method = "copy"
    if mode == "audio":
        if not info["audio"]:
            raise CutError("в файле нет звуковой дорожки")
        codec = info["audio"][0]["codec_name"]
        if has_video:
            ext = AUDIO_CONTAINER.get(codec, ".mka")
        if is_lossless(codec):
            method = "lossless"
            if not codec.startswith("pcm_"):
                enc, enc_ext = LOSSLESS_ENCODER.get(codec, ("flac", ".flac"))
                if enc_ext != ext and not (enc == "alac" and ext in MP4_LIKE):
                    ext = enc_ext
    return {"mode": mode, "method": method, "keep": keep, "duration": dur, "ext": ext, "info": info, "notes": notes}


def default_output(path, ext, suffix="_cut", out_dir=None):
    stem = os.path.splitext(os.path.basename(path))[0]
    folder = out_dir or os.path.dirname(os.path.abspath(path))
    cand = os.path.join(folder, f"{stem}{suffix}{ext}")
    n = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{stem}{suffix} ({n}){ext}")
        n += 1
    return cand


def _ffmpeg(args, progress=None, frac0=0.0, frac1=1.0, seconds=0.0, cancelled=None, text="вырезание"):
    """Run ffmpeg; progress is reported between frac0 and frac1 of the whole job."""
    cmd = [resources.tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error"] + args[:-1] + \
          ["-progress", "pipe:1", "-nostats", args[-1]]
    with tempfile.TemporaryFile() as err:
        proc = resources.popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        for line in proc.stdout:
            if cancelled and cancelled():
                proc.kill()
                proc.wait()
                raise InterruptedError("остановлено")
            m = re.match(r"out_time_us=(\d+)", line.strip())
            if m and progress and seconds > 0:
                progress(frac0 + (frac1 - frac0) * min(1.0, int(m.group(1)) / 1e6 / seconds), text)
        if proc.wait() != 0:
            err.seek(0)
            raise CutError("ffmpeg: " + err.read().decode(errors="replace").strip()[-500:])


def cut(path, cuts, out_path=None, mode="auto", progress=None, cancelled=None, verify_result=True, tags=None):
    """Remove `cuts` [(start, end)] from `path` without re-encoding. Returns a report dict.

    Each kept part is first copied into its own temporary file (so it gets correct timestamps of its
    own), then the parts are joined with the concat demuxer. Joining parts cut straight out of the
    source leaves overlapping timestamps at the joins (B-frames in video, Ogg granules in audio).
    """
    p = plan(path, cuts, mode)
    out_path = out_path or default_output(path, p["ext"])
    if os.path.abspath(out_path) == os.path.abspath(path):
        raise CutError("результат нельзя записать поверх исходного файла")
    out_ext = os.path.splitext(out_path)[1].lower()
    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix=".vidaudcont-", dir=folder)
    faststart = ["-movflags", "+faststart"] if out_ext in MP4_LIKE else []
    # container tags on top of the source's (e.g. the video's link): text next to the sound, which is not touched
    faststart += [x for k, v in (tags or {}).items() for x in ("-metadata", f"{k}={v}")]
    try:
        tmp_out = os.path.join(tmpdir, "result" + out_ext)
        if p["method"] == "lossless":
            codec = p["info"]["audio"][0]["codec_name"]
            enc = codec if codec.startswith("pcm_") else LOSSLESS_ENCODER.get(codec, ("flac",))[0]
            args = ["-i", path, "-filter_complex", _trim_graph(p), "-map", "[out]", "-map_metadata", "0",
                    "-map_chapters", "-1", "-c:a", enc] + (["-compression_level", "8"] if enc == "flac" else [])
            _ffmpeg(args + faststart + [tmp_out], progress, 0.0, 0.97, sum(b - a for a, b in p["keep"]), cancelled)
        else:
            work = os.path.abspath(path)
            maps = ["-map", "0:a", "-vn"] if p["mode"] == "audio" else ["-map", "0:V?", "-map", "0:a?"]
            if p["mode"] == "audio":
                if p["info"]["video"]:
                    # seeking in a video file lands on video keyframes: copy the audio track out first
                    work = os.path.join(tmpdir, "audio" + p["ext"])
                    _ffmpeg(["-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn", "-c", "copy", work],
                            progress, 0.0, 0.1, p["duration"], cancelled, "подготовка звука")
                p["keep"] = snap_to_packets(p["keep"], audio_packet_starts(work), probe(work)["duration"])
            total = sum(b - a for a, b in p["keep"])
            parts, done = [], 0.0
            for i, (a, b) in enumerate(p["keep"]):
                part = os.path.join(tmpdir, f"part{i:04d}{out_ext}")
                pre = max(0.0, a - 5.0)  # fast seek near the cut, then an exact trim from there
                _ffmpeg(["-ss", f"{pre:.6f}", "-i", work, "-ss", f"{a - pre:.6f}", "-t", f"{b - a:.6f}"] + maps +
                        ["-sn", "-dn", "-c", "copy", "-map_metadata", "-1", "-avoid_negative_ts", "make_zero", part],
                        progress, 0.1 + 0.8 * done / total, 0.1 + 0.8 * (done + b - a) / total, b - a, cancelled)
                parts.append(part)
                done += b - a
            lst = os.path.join(tmpdir, "parts.ffconcat")
            with open(lst, "w", encoding="utf-8") as f:
                f.write("ffconcat version 1.0\n" + "".join(
                    "file '" + x.replace("'", "'\\''") + "'\n" for x in parts))
            if out_ext in OGG_LIKE:
                joined = os.path.join(tmpdir, "joined.nut")
                _ffmpeg(["-f", "concat", "-safe", "0", "-auto_convert", "0", "-i", lst] + maps +
                        ["-sn", "-dn", "-c", "copy", joined], progress, 0.9, 0.94, total, cancelled, "склейка")
                _ffmpeg(["-i", joined, "-i", path, "-map_metadata", "1", "-map_chapters", "-1", "-map", "0",
                         "-c", "copy"] + faststart + [tmp_out], progress, 0.94, 0.97, total, cancelled, "склейка")
            else:
                _ffmpeg(["-f", "concat", "-safe", "0", "-auto_convert", "0", "-i", lst, "-i", path,
                         "-map_metadata", "1", "-map_chapters", "-1"] + maps +
                        ["-sn", "-dn", "-c", "copy"] + faststart + [tmp_out], progress, 0.9, 0.97, total, cancelled,
                        "склейка")
        os.replace(tmp_out, out_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    report = {"output": out_path, "mode": p["mode"], "method": p["method"], "keep": p["keep"], "notes": p["notes"],
              "source_duration": p["duration"], "expected_duration": sum(b - a for a, b in p["keep"]),
              "output_duration": probe(out_path)["duration"]}
    if verify_result:
        if progress:
            progress(0.98, "проверка качества")
        report["verification"] = (verify_pcm(path, out_path, p) if p["method"] == "lossless"
                                  else verify(path, out_path, p["mode"]))
    if progress:
        progress(1.0, "готово")
    return report


def _trim_graph(p):
    """Sample-exact trim of the kept parts of the first audio stream, joined back together."""
    sr = int(p["info"]["audio"][0].get("sample_rate") or 0)
    if sr <= 0:
        raise CutError("неизвестная частота дискретизации")
    parts, labels = [], []
    for i, (a, b) in enumerate(p["keep"]):
        parts.append(f"[0:a:0]atrim=start_sample={round(a * sr)}:end_sample={round(b * sr)},asetpts=PTS-STARTPTS[k{i}]")
        labels.append(f"[k{i}]")
    return ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"


def _pcm_hash(args):
    out = resources.run([resources.tool("ffmpeg"), "-v", "error", "-nostdin"] + args + ["-f", "hash", "-hash", "sha256", "-"],
                        capture_output=True, text=True)
    m = re.search(r"SHA256=([0-9a-f]+)", out.stdout)
    return m.group(1) if m else None


def verify_pcm(src, out, p):
    """Lossless codecs: the decoded samples of the result must equal the source's kept samples exactly."""
    fmt = p["info"]["audio"][0].get("sample_fmt", "")
    pcm = "pcm_f64le" if fmt.startswith(("flt", "dbl")) else "pcm_s32le"
    ref = _pcm_hash(["-i", src, "-filter_complex", _trim_graph(p), "-map", "[out]", "-c:a", pcm])
    got = _pcm_hash(["-i", out, "-map", "0:a:0", "-c:a", pcm])
    same = ref is not None and ref == got
    return {"lossless": same, "audio": {"method": "samples", "identical_samples": same}}


def _packets(path, selector):
    out = resources.run([resources.tool("ffprobe"), "-v", "error", "-select_streams", selector,
                         "-show_entries", "packet=pts_time,data_hash", "-show_data_hash", "CRC32",
                         "-of", "csv=p=0", path], capture_output=True, text=True)
    res = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        h = next((x for x in parts if x.startswith("CRC32:")), None)
        if h is None:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            t = float("nan")
        res.append((t, h))
    return res


def _match(src, dst):
    """How many dst packets are copies of src packets, taken in order (runs = continuous pieces)."""
    index = {}
    for i, (_, h) in enumerate(src):
        index.setdefault(h, []).append(i)
    pos, matched, runs, starts = 0, 0, 0, []
    for _, h in dst:
        if pos < len(src) and src[pos][1] == h:
            pos += 1
            matched += 1
            continue
        cand = index.get(h, [])
        k = bisect.bisect_left(cand, pos)
        if k < len(cand):
            pos = cand[k] + 1
            matched += 1
            runs += 1
            starts.append(src[cand[k]][0])
        # a packet that is not in the source at all stays unmatched
    return matched, runs, starts


def verify(src, out, mode):
    """Check that every audio (and video) packet of `out` is a bit-exact copy of a packet of `src`."""
    res = {}
    kinds = [("audio", "a:0")] + ([("video", "V:0")] if mode == "video" else [])
    ok = True
    for name, sel in kinds:
        s, d = _packets(src, sel), _packets(out, sel)
        if not d:
            if name == "video" and not s:
                continue
            ok = False
            res[name] = {"packets": 0, "identical": 0}
            continue
        matched, runs, _ = _match(s, d)
        res[name] = {"packets": len(d), "identical": matched, "pieces": runs + 1}
        ok = ok and matched == len(d)
    res["lossless"] = ok
    return res
