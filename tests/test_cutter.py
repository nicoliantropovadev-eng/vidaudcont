"""Lossless cutting across formats: result durations, packet-exact copies, clean decoding."""
import os
import subprocess

import pytest

from vidaudcont import cutter, resources

FF = resources.tool("ffmpeg")
DUR = 40.0
CUTS = [(0.0, 3.0), (12.0, 19.5), (33.0, DUR)]
KEPT = DUR - 3.0 - 7.5 - 7.0  # 22.5 s

AUDIO = ["-f", "lavfi", "-i", f"sine=frequency=330:duration={DUR}[s];anoisesrc=d={DUR}:c=pink:a=0.05[n];"
                             "[s][n]amix=inputs=2,aformat=channel_layouts=stereo[out0]"]
VIDEO = ["-f", "lavfi", "-i", f"testsrc2=duration={DUR}:size=320x240:rate=25"]

CASES = {
    # name: (extension, ffmpeg encoding args, mode, expected container duration tolerance)
    "m4a_aac": (".m4a", AUDIO + ["-c:a", "aac", "-b:a", "128k"], "auto", 0.1),
    "mp3": (".mp3", AUDIO + ["-c:a", "libmp3lame", "-b:a", "128k"], "auto", 0.1),
    "mp3_vbr": (".mp3", AUDIO + ["-c:a", "libmp3lame", "-q:a", "2"], "auto", 0.1),
    "opus": (".opus", AUDIO + ["-c:a", "libopus", "-b:a", "96k"], "auto", 0.1),
    "flac": (".flac", AUDIO + ["-c:a", "flac"], "auto", 0.01),
    "wav": (".wav", AUDIO + ["-c:a", "pcm_s16le"], "auto", 0.01),
    "wav24": (".wav", AUDIO + ["-c:a", "pcm_s24le"], "auto", 0.01),
    "flac24": (".flac", AUDIO + ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"], "auto", 0.01),
    "alac": (".m4a", AUDIO + ["-c:a", "alac"], "auto", 0.05),
    "ogg_vorbis": (".ogg", AUDIO + ["-c:a", "libvorbis"], "auto", 0.1),
    "mp4_h264_video": (".mp4", VIDEO + AUDIO + ["-c:v", "libx264", "-g", "50", "-bf", "2", "-pix_fmt", "yuv420p",
                                                 "-c:a", "aac", "-shortest"], "video", 4.1),
    "mp4_h264_audio_only": (".mp4", VIDEO + AUDIO + ["-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p",
                                                      "-c:a", "aac", "-shortest"], "audio", 0.1),
    "mkv_h264_opus": (".mkv", VIDEO + AUDIO + ["-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p",
                                                "-c:a", "libopus", "-shortest"], "video", 4.1),
    "ogv_theora_vorbis": (".ogv", VIDEO + AUDIO + ["-c:v", "libtheora", "-g", "25", "-c:a", "libvorbis", "-shortest"],
                          "video", 2.1),
    "mov_h264_aac": (".mov", VIDEO + AUDIO + ["-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p",
                                               "-c:a", "aac", "-shortest"], "video", 2.1),
}


_ENCODERS = subprocess.run([FF, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def need_encoders(args):
    for i, a in enumerate(args[:-1]):
        if a in ("-c:a", "-c:v") and args[i + 1] not in ("copy",) and f" {args[i + 1]} " not in _ENCODERS:
            pytest.skip(f"this ffmpeg build has no {args[i + 1]} encoder")


def make(path, args):
    subprocess.run([FF, "-v", "error", "-y"] + args + ["-map_metadata", "-1", path], check=True)


def decodes_cleanly(path):
    r = subprocess.run([FF, "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip(), r.stderr.strip()[:300]


@pytest.mark.parametrize("name", sorted(CASES))
def test_cut_is_lossless(tmp_path, name):
    ext, args, mode, tol = CASES[name]
    # awkward file name on purpose: spaces, apostrophe, Cyrillic
    src = str(tmp_path / f"тест 'файл' {name}{ext}")
    need_encoders(args)
    make(src, args)
    rep = cutter.cut(src, CUTS, mode=mode)
    out = rep["output"]
    assert os.path.exists(out) and os.path.getsize(out) > 0
    assert rep["verification"]["lossless"], rep["verification"]
    a = rep["verification"]["audio"]
    if rep["method"] == "copy":
        assert a["identical"] == a["packets"] and a["pieces"] >= 3, a
    else:
        assert a["identical_samples"], a
    if mode == "video":
        v = rep["verification"]["video"]
        assert v["identical"] == v["packets"], v
    got = cutter.probe(out)["duration"]
    assert abs(got - KEPT) <= tol, (got, KEPT, rep["keep"])
    ok, err = decodes_cleanly(out)
    assert ok, err
    # the source is never touched
    assert os.path.exists(src)


def test_cover_art_is_dropped_not_duplicated(tmp_path):
    src = str(tmp_path / "with_cover.m4a")
    cover = str(tmp_path / "cover.png")
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=64x64", "-frames:v", "1", cover], check=True)
    subprocess.run([FF, "-v", "error", "-y"] + AUDIO + ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:a", "aac",
                                                         "-c:v", "png", "-disposition:v", "attached_pic", src], check=True)
    rep = cutter.cut(src, CUTS)
    assert rep["mode"] == "audio" and rep["verification"]["lossless"]
    assert not cutter.probe(rep["output"])["video"]


def test_audio_only_extension_follows_codec(tmp_path):
    src = str(tmp_path / "v.mkv")
    make(src, VIDEO + AUDIO + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"])
    rep = cutter.cut(src, CUTS, mode="audio")
    assert rep["output"].endswith("_cut.m4a")


def test_refuses_to_overwrite_source(tmp_path):
    src = str(tmp_path / "a.m4a")
    make(src, AUDIO + ["-c:a", "aac"])
    with pytest.raises(cutter.CutError):
        cutter.cut(src, CUTS, out_path=src)


def test_nothing_left(tmp_path):
    src = str(tmp_path / "a.m4a")
    make(src, AUDIO + ["-c:a", "aac"])
    with pytest.raises(cutter.CutError):
        cutter.cut(src, [(0.0, DUR)])
