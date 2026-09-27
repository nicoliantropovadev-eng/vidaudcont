"""Builds assets/selftest.m4a: a 76-s clip with known structure for the app's self-test.

  0-5 s music | 5-30 s British speech | 30-40 s silence | 40-55 s speech | 55-60 s music
  | 60-72 s speech | 72-76 s silence            -> expected cuts: start-0:05, 0:30-0:40, 0:55-1:00, 1:12-end

Sources (not shipped except inside the clip): OpenSLR 83 "Crowdsourced high-quality UK and Ireland
English Dialect speech" (CC BY-SA 4.0), Kevin MacLeod "Tranquility" / "Carefree" (CC BY 4.0).
Run from the research workspace: python scripts/make_selftest_asset.py <testdata dir> <out.m4a>
"""
import io
import subprocess
import sys

import librosa
import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

SR = 44100
rng = np.random.default_rng(7)
src_dir, out = sys.argv[1], sys.argv[2]


def utts(parquet, n):
    res, by = [], {}
    for batch in pq.ParquetFile(parquet).iter_batches(batch_size=16, columns=["audio", "speaker_id"]):
        for r in batch.to_pylist():
            by.setdefault(r["speaker_id"], []).append(r["audio"]["bytes"])
        if sum(len(v) for v in by.values()) > 3 * n:
            break
    spk = max(by, key=lambda k: len(by[k]))
    for b in by[spk][:n]:
        w, sr = sf.read(io.BytesIO(b), dtype="float32")
        w = w.mean(1) if w.ndim > 1 else w
        w = librosa.resample(w, orig_sr=sr, target_sr=SR)
        w, _ = librosa.effects.trim(w, top_db=35)
        res.append(w / (np.abs(w).max() + 1e-9) * 0.5)
    return res


def speech(parts, dur):
    out, total, i = [], 0, 0
    while total < dur * SR:  # cycle through the utterances until the block is full
        w = parts[i % len(parts)]
        i += 1
        gap = np.zeros(int(rng.uniform(0.35, 0.8) * SR), np.float32)
        out += [w, gap]
        total += len(w) + len(gap)
    x = np.concatenate(out)[: int(dur * SR)]
    return np.pad(x, (0, int(dur * SR) - len(x)))


def music(path, offset, dur):
    x, _ = librosa.load(path, sr=SR, offset=offset, duration=dur)
    return (x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 0.1).astype(np.float32)


def silence(dur):
    return (rng.standard_normal(int(dur * SR)) * 10 ** (-66 / 20)).astype(np.float32)


a = utts(f"{src_dir}/southern_female.parquet", 12)
b = utts(f"{src_dir}/midlands_female.parquet", 12)
track = np.concatenate([music(f"{src_dir}/music2.wav", 30, 5), speech(a[: len(a) // 2 or 1], 25), silence(10), speech(b, 15),
                        music(f"{src_dir}/music1.wav", 200, 5), speech(a[len(a) // 2:] or a, 12), silence(4)])
sf.write("/tmp/selftest.wav", np.stack([track, track], 1), SR)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "/tmp/selftest.wav", "-c:a", "aac", "-b:a", "96k", out], check=True)
print(out, len(track) / SR, "s")
