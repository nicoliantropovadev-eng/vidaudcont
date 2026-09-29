"""Finds what to cut from a recording of a (British English, medical) conversation.

Pipeline for one file:
  audio (phase-aware mono) -> speech segments (Silero VAD) -> per-second AudioSet tags (EfficientAT)
  -> breaks: intro/outro, pauses > max_gap, music, quiet speech, short leftovers -> cut list
  + accent vote (CommonAccent ECAPA) + topic hint (Whisper excerpts + medical vocabulary).
The rules and thresholds were tuned against the user's manual cut lists (sheet rows 5-69).
"""
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np

from .. import resources
from ..timecodes import fmt_time, format_cuts
from .topic import topic_score

KIND_MUSIC = "музыка"
KIND_SILENCE = "тишина"
KIND_QUIET_SOUNDS = "тихие звуки"
KIND_OTHER = "шум/другое"
KIND_NO_SPEECH = "нет речи"
KIND_MUSIC_UNDER = "музыка под речью"
KIND_QUIET_SPEECH = "тихая речь"
KIND_LEFTOVER = "короткий кусок между вырезами"


@dataclass
class Settings:
    max_gap: float = 7.0        # a pause longer than this is cut
    edge_min: float = 3.0       # silence/music before the first / after the last words is cut from this length
    music_min: float = 2.0      # music (between or under speech) from this length is cut
    music_thr: float = 0.30     # AudioSet "Music" probability treated as music
    min_keep: float = 8.0       # speech left between two cuts shorter than this is cut as well
    quiet_enabled: bool = True  # cut stretches where people speak much more quietly than in the rest
    quiet_abs: float = -31.0    # ... speech level below this (dBFS, 90th pct of 30-ms speech frames) ...
    quiet_rel: float = -7.0     # ... and this many dB below the video's typical speech level
    quiet_win: float = 60.0     # ... measured over this window
    quiet_min: float = 20.0     # ... for at least this long
    blip_max: float = 1.5       # an isolated phrase shorter than this ...
    blip_isolation: float = 4.0  # ... with this much non-speech around it is not conversation
    event_db: float = 12.0      # a pause "has sounds" when its loud frames rise this far above the pause level
    prominent_db: float = 10.0  # ... and they are "prominent" when within this of the speech level
    other_min: float = 0.0      # cut prominent sounds from this length (0 = only via max_gap)
    short_gap: float = 5.0      # pauses short_gap..max_gap are listed as a hint (not cut)
    merge_gap: float = 1.0      # cuts closer than this are merged
    accent: bool = True
    topic: bool = True
    dialogue: bool = True       # tell a conversation (two voices taking turns) from a lecture or a voice-over
    only_suitable: bool = True  # "Cut all" cuts only what passes the checks turned on (accent, topic, conversation)
    threads: int = 0            # processor threads for analysis (0 = half of the logical cores)


ACCENT_NOTE = {"американский": "АМЕРИКАНСКИЙ АКЦЕНТ", "ирландский": "ИРЛАНДСКИЙ АКЦЕНТ",
               "австралийский": "АВСТРАЛИЙСКИЙ АКЦЕНТ", "индийский": "ИНДИЙСКИЙ АКЦЕНТ", "другой": "НЕ БРИТАНСКИЙ АКЦЕНТ"}
ACCENT_GROUP = {"england": "британский", "scotland": "британский", "wales": "британский",
                "ireland": "ирландский", "us": "американский", "canada": "американский",
                "australia": "австралийский", "newzealand": "австралийский", "indian": "индийский"}


# ---------------------------------------------------------------- audio
def load_audio(path, sr, with_info=False):
    """Mono float32 audio with a phase-aware downmix.

    A plain (L+R)/2 downmix cancels speech where one channel is polarity-inverted (the voice
    drops by ~40 dB and only hum is left). Per 0.5-s block the right channel is flipped when
    L and R are strongly anti-correlated. Streamed, so long files do not need much memory.
    """
    cmd = [resources.tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "2",
           "-ar", str(sr), "-f", "f32le", "-"]
    blk = sr // 2
    ramp = int(0.02 * sr)
    chunks, sign, prev = [], [], 1.0
    with tempfile.TemporaryFile() as err:
        proc = resources.popen(cmd, stdout=subprocess.PIPE, stderr=err)
        while True:
            buf = proc.stdout.read(blk * 8)
            if not buf:
                break
            st = np.frombuffer(buf[: len(buf) // 8 * 8], dtype=np.float32).reshape(-1, 2)
            l, r = st[:, 0], st[:, 1]
            e_mid, e_side = float(np.sum((l + r) ** 2)), float(np.sum((l - r) ** 2))
            s = -1.0 if e_side > 4 * e_mid and e_side / max(len(l), 1) > 1e-7 else 1.0
            if s != prev and len(l) > ramp:
                g = np.full(len(l), s, np.float32)
                g[:ramp] = np.linspace(prev, s, ramp, dtype=np.float32)
                chunks.append((l + g * r) / 2)
            else:
                chunks.append((l + s * r) / 2)
            sign.append(s)
            prev = s
        proc.stdout.close()
        if proc.wait() != 0:
            err.seek(0)
            raise RuntimeError("ffmpeg не смог прочитать файл: " + err.read().decode(errors="replace")[-400:])
    mono = np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.zeros(0, np.float32)
    if with_info:
        sign = np.array(sign)
        return mono, {"phase_inverted_s": round(float((sign < 0).sum()) * 0.5, 1),
                      "phase_inverted_ranges": [[a * 0.5, b * 0.5] for a, b in runs(sign < 0)]}
    return mono


def runs(mask, min_len=1):
    """[start, end) index runs where mask is True."""
    res, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                res.append([start, i])
            start = None
    return res


def db(x):
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-9)) if len(x) else -120.0


def frame_db(x, frame=160):
    n = len(x) // frame
    if n == 0:
        return np.array([db(x)])
    return 20 * np.log10(np.sqrt(np.mean(np.square(x[: n * frame].reshape(n, frame)), axis=1)) + 1e-9)


def floor_db(x, frame=160):
    """Median level of 10-ms frames: ignores clicks that dominate a plain RMS."""
    return float(np.median(frame_db(x, frame)))


# ---------------------------------------------------------------- models
def _torch_amp_compat(torch):
    """speechbrain 1.1 decorates forward() with torch.amp.custom_fwd (torch >= 2.4). The Intel-Mac
    build has to stay on torch 2.2, where it does not exist; inference here runs in float32 without
    autocast, so a pass-through decorator behaves the same."""
    if hasattr(torch.amp, "custom_fwd"):
        return

    def custom_fwd(fwd=None, *, device_type=None, cast_inputs=None):
        return fwd if fwd is not None else (lambda f: f)

    def custom_bwd(bwd=None, *, device_type=None):
        return bwd if bwd is not None else (lambda f: f)

    torch.amp.custom_fwd, torch.amp.custom_bwd = custom_fwd, custom_bwd


def _load_state(torch, path):
    """torch.load through a Python file object (Unicode-safe on Windows)."""
    with open(path, "rb") as f:
        return torch.load(f, map_location="cpu")


def load_whisper(models_dir, threads):
    """Whisper base.en loaded from memory: CTranslate2 opens files by narrow path, which breaks on
    Windows when the program sits in a folder with non-Latin letters."""
    from faster_whisper import WhisperModel
    d = os.path.join(models_dir, "whisper-base.en")
    files = {}
    for name in ("model.bin", "config.json", "vocabulary.txt", "tokenizer.json"):
        with open(os.path.join(d, name), "rb") as f:
            files[name] = f.read()
    return WhisperModel("whisper-base.en", device="cpu", compute_type="int8", cpu_threads=threads, files=files)


def default_threads():
    """Half of the logical cores: one per physical core on CPUs with SMT. Using every thread gains
    little for this work, but keeps the processor at 100% (heat, noise, a sluggish computer)."""
    return max(1, (os.cpu_count() or 2) // 2)


class Models:
    """Lazily loaded models, shared by all analyses in the process."""

    def __init__(self, models_dir=None, threads=0):
        import torch
        self.dir = models_dir or resources.models_dir()
        self.threads = threads or default_threads()
        torch.set_num_threads(self.threads)
        self._lock = threading.Lock()
        self._vad = self._tagger = self._accent = self._asr = self._speaker = None
        # On Intel Macs torch (LLVM OpenMP) and CTranslate2 (Intel OpenMP) cannot share a process:
        # the transcription runs in a separate process of this same program there.
        self.asr_in_subprocess = ((sys.platform == "darwin" and platform.machine() == "x86_64")
                                  or os.environ.get("VIDAUDCONT_ASR_SUBPROCESS") == "1")

    def set_threads(self, n):
        import torch
        n = n or default_threads()
        if n != self.threads:
            with self._lock:
                self.threads = n
                torch.set_num_threads(n)
                self._asr = None  # CTranslate2 takes its thread count at load time

    def vad(self):
        with self._lock:
            if self._vad is None:
                import io

                import silero_vad
                import torch
                path = os.path.join(os.path.dirname(silero_vad.__file__), "data", "silero_vad.jit")
                with open(path, "rb") as f:  # torch.jit.load(path) fails on Windows for D:\Загрузки\...
                    self._vad = torch.jit.load(io.BytesIO(f.read()), map_location="cpu").eval()
            return self._vad

    def tagger(self):
        with self._lock:
            if self._tagger is None:
                import torch
                from .efficientat import load_labels
                from .efficientat.mn.model import get_model
                from .efficientat.mn.utils import NAME_TO_WIDTH
                from .efficientat.preprocess import AugmentMelSTFT
                model = get_model(width_mult=NAME_TO_WIDTH("mn10_as"), pretrained_name=None)
                model.load_state_dict(_load_state(torch, os.path.join(self.dir, "efficientat", "mn10_as_mAP_471.pt")))
                mel = AugmentMelSTFT(n_mels=128, sr=32000, win_length=800, hopsize=320)
                self._tagger = (model.eval(), mel.eval(), load_labels())
            return self._tagger

    def accent(self):
        """CommonAccent ECAPA built directly from its checkpoints (no hyperpyyaml / symlinks)."""
        with self._lock:
            if self._accent is None:
                import torch
                _torch_amp_compat(torch)
                from speechbrain.lobes.features import Fbank
                from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN, Classifier
                from speechbrain.processing.features import InputNormalization
                d = os.path.join(self.dir, "accent-ecapa")
                feats = Fbank(n_mels=80)
                norm = InputNormalization(norm_type="sentence", std_norm=False)
                emb = ECAPA_TDNN(input_size=80, activation=torch.nn.LeakyReLU, channels=[1024, 1024, 1024, 1024, 3072],
                                 kernel_sizes=[5, 3, 3, 3, 1], dilations=[1, 2, 3, 4, 1], attention_channels=128,
                                 lin_neurons=192)
                clf = Classifier(input_size=192, out_neurons=16)
                emb.load_state_dict(_load_state(torch, os.path.join(d, "embedding_model.ckpt")))
                clf.load_state_dict(_load_state(torch, os.path.join(d, "classifier.ckpt")))
                labels = {}
                with open(os.path.join(d, "accent_encoder.txt"), encoding="utf-8") as f:
                    for line in f:
                        if "=>" in line and not line.startswith("'starting_index'"):
                            name, idx = line.split("=>")
                            labels[int(idx)] = name.strip().strip("'")
                self._accent = (feats.eval(), norm.eval(), emb.eval(), clf.eval(), [labels[i] for i in range(len(labels))])
            return self._accent

    def speaker(self):
        """x-vector speaker embeddings (SpeechBrain, VoxCeleb). Tells voices apart well enough to see two
        people taking turns, at a fraction of the cost of ECAPA (10 s instead of 3 min per recording)."""
        with self._lock:
            if self._speaker is None:
                import torch
                _torch_amp_compat(torch)
                from speechbrain.lobes.features import Fbank
                from speechbrain.lobes.models.Xvector import Xvector
                from speechbrain.processing.features import InputNormalization
                emb = Xvector(in_channels=24, activation=torch.nn.LeakyReLU, tdnn_blocks=5,
                              tdnn_channels=[512, 512, 512, 512, 1500], tdnn_kernel_sizes=[5, 3, 3, 1, 1],
                              tdnn_dilations=[1, 2, 3, 1, 1], lin_neurons=512)
                emb.load_state_dict(_load_state(torch, os.path.join(self.dir, "speaker-xvect", "embedding_model.ckpt")))
                self._speaker = (Fbank(n_mels=24).eval(), InputNormalization(norm_type="sentence", std_norm=False).eval(),
                                 emb.eval())
            return self._speaker

    def speaker_embeddings(self, wav16, wins, batch=64):
        """One 512-d embedding per (start, end) piece of speech (at most 1.5 s)."""
        import torch
        feats, norm, emb = self.speaker()
        n = int(SPEAKER_WIN * 16000)
        out = []
        with torch.no_grad():
            for i in range(0, len(wins), batch):
                chunk = wins[i:i + batch]
                x = np.zeros((len(chunk), n), np.float32)
                lens = np.zeros(len(chunk), np.float32)
                for j, (s, e) in enumerate(chunk):
                    a = wav16[int(s * 16000): int(s * 16000) + min(n, int((e - s) * 16000))]
                    x[j, :len(a)] = a
                    lens[j] = max(len(a), 1) / n
                xt, lt = torch.from_numpy(x), torch.from_numpy(lens)
                out.append(emb(norm(feats(xt), lt), lt).squeeze(1).numpy())
        return np.concatenate(out)

    def asr(self):
        with self._lock:
            if self._asr is None:
                self._asr = load_whisper(self.dir, self.threads)
            return self._asr


# ---------------------------------------------------------------- analysis steps
def speech_segments(models, wav16):
    import torch
    from silero_vad import get_speech_timestamps
    ts = get_speech_timestamps(torch.from_numpy(wav16), models.vad(), sampling_rate=16000, threshold=0.5,
                               min_speech_duration_ms=250, min_silence_duration_ms=400, speech_pad_ms=100,
                               return_seconds=True)
    segs = []
    for t in ts:
        if segs and t["start"] - segs[-1][1] < 0.3:
            segs[-1][1] = t["end"]
        else:
            segs.append([t["start"], t["end"]])
    return segs


def tag_seconds(models, wav32, win_s=6.0, batch=32, progress=None):
    """AudioSet probabilities for each second t (window of win_s centred on t+0.5)."""
    import torch
    model, mel, labels = models.tagger()
    sr = 32000
    n_sec = int(np.ceil(len(wav32) / sr))
    half = int(win_s * sr / 2)
    padded = np.concatenate([np.zeros(half, np.float32), wav32, np.zeros(half + sr, np.float32)])
    out = np.zeros((n_sec, len(labels)), np.float32)
    with torch.no_grad():
        for b0 in range(0, n_sec, batch):
            idx = range(b0, min(b0 + batch, n_sec))
            x = np.stack([padded[t * sr + sr // 2: t * sr + sr // 2 + 2 * half] for t in idx])
            logits, _ = model(mel(torch.from_numpy(x)).unsqueeze(1))
            out[b0:b0 + len(idx)] = torch.sigmoid(logits).numpy()
            if progress:
                progress(min(1.0, (b0 + len(idx)) / max(n_sec, 1)))
    return out, labels


def classify_gap(wav16, s, e, music_sec, bg_db, speech_db, st):
    seg = wav16[int(s * 16000):int(e * 16000)]
    fr = frame_db(seg)
    lvl, peak = float(np.median(fr)), float(np.percentile(fr, 95))
    secs = music_sec[int(s):max(int(np.ceil(e)), int(s) + 1)]
    music_frac = float(np.mean(secs >= st.music_thr)) if len(secs) else 0.0
    if music_frac >= 0.5:
        kind = KIND_MUSIC
    elif peak < -50 or peak < bg_db + st.event_db:
        kind = KIND_SILENCE
    elif db(seg) < speech_db - st.prominent_db:
        kind = KIND_QUIET_SOUNDS
    else:
        kind = KIND_OTHER
    return kind, lvl, music_frac


def drop_blips(segs, dur, st):
    """Remove an isolated short phrase (a knock and 'come in' behind the door) - not the conversation."""
    out = []
    for i, (s, e) in enumerate(segs):
        prev_end = segs[i - 1][1] if i > 0 else 0.0
        next_start = segs[i + 1][0] if i + 1 < len(segs) else dur
        left, right = s - prev_end, next_start - e
        if e - s < st.blip_max and min(left, right) >= 1.5 and left + right >= st.blip_isolation:
            continue
        out.append([s, e])
    return out


def quiet_speech(wav16, segs, dur, st):
    """Stretches where people speak much more quietly than in the rest of the recording."""
    if not segs or not st.quiet_enabled:
        return []
    fr = frame_db(wav16, 480)
    fps = 16000 / 480
    t = np.arange(len(fr)) / fps
    sp = np.zeros(len(fr), bool)
    for s, e in segs:
        sp[(t >= s) & (t < e)] = True
    centers = np.arange(0, dur, 5.0)
    lvl = np.full(len(centers), np.nan)
    for i, c in enumerate(centers):
        m = sp & (t >= c - st.quiet_win / 2) & (t < c + st.quiet_win / 2)
        if m.sum() / fps >= st.quiet_win * 0.25:
            lvl[i] = np.percentile(fr[m], 90)
    if np.all(np.isnan(lvl)):
        return []
    ref = float(np.nanpercentile(lvl, 80))
    with np.errstate(invalid="ignore"):
        quiet = (lvl < st.quiet_abs) & (lvl - ref < st.quiet_rel)
    thr = min(st.quiet_abs, ref + st.quiet_rel)
    seg_lvl = [(ss, ee, float(np.percentile(fr[(t >= ss) & (t < ee)], 90)) if ee - ss > 0.1 else -120.0)
               for ss, ee in segs]
    out = []
    for a, b in runs(quiet):
        s, e = centers[a], min(centers[b - 1] + 5.0, dur)
        inside = [i for i, (ss, ee, _) in enumerate(seg_lvl) if ee > s and ss < e]
        if not inside:
            continue
        i0, i1 = inside[0], inside[-1]
        while i0 > 0 and seg_lvl[i0 - 1][2] < thr + 3:
            i0 -= 1
        while i1 + 1 < len(seg_lvl) and seg_lvl[i1 + 1][2] < thr + 3:
            i1 += 1
        while i0 < i1 and seg_lvl[i0][2] >= thr + 3:
            i0 += 1
        while i1 > i0 and seg_lvl[i1][2] >= thr + 3:
            i1 -= 1
        s, e = seg_lvl[i0][0], seg_lvl[i1][1]
        if e - s >= st.quiet_min:
            out.append((s, e, float(np.nanmedian(lvl[a:b])), ref))
    return out


def find_cuts(wav16, dur, segs, P, labels, st):
    """The rules. Returns (cuts, events, hint pauses, speech level, per-second music probability)."""
    li = {l: i for i, l in enumerate(labels)}
    music_sec = P[:, li["Music"]]
    segs = drop_blips(segs, dur, st)
    speech_db = float(np.median([db(wav16[int(s * 16000):int(e * 16000)]) for s, e in segs])) if segs else -30.0
    pauses = [floor_db(wav16[int(a * 16000):int(b * 16000)]) for (_, a), (b, _) in zip(segs[:-1], segs[1:])
              if b - a >= 0.3]
    bg_db = float(np.median(pauses)) if pauses else float(np.percentile(frame_db(wav16), 10))

    events = []
    bounds = [0.0] + [x for s in segs for x in s] + [dur]
    for i in range(0, len(bounds), 2):
        s, e = bounds[i], bounds[i + 1]
        if e - s < 0.5:
            continue
        where = "intro" if i == 0 else ("outro" if i == len(bounds) - 2 else "inside")
        kind, lvl, mf = classify_gap(wav16, s, e, music_sec, bg_db, speech_db, st)
        events.append({"start": s, "end": e, "dur": e - s, "where": where, "kind": kind})
    if not segs:
        events = [{"start": 0.0, "end": dur, "dur": dur, "where": "all", "kind": KIND_NO_SPEECH}]

    # music under speech (the 6-s tagger window smears music ~3 s into neighbouring speech:
    # keep only seconds where all windows within +-2 s agree, then grow back)
    raw = music_sec >= st.music_thr
    n = len(raw)
    core = np.array([raw[max(0, t - 2):t + 3].all() for t in range(n)], bool)
    opened = np.array([core[max(0, t - 2):t + 3].any() for t in range(n)], bool) & raw
    in_speech = np.zeros(n, bool)
    for s, e in segs:
        in_speech[int(s):int(np.ceil(e))] = True
    m = opened & in_speech
    for i in range(1, len(m) - 1):
        if not m[i] and m[i - 1] and m[i + 1]:
            m[i] = True
    bad = []
    music_gaps = [ev for ev in events if ev["kind"] == KIND_MUSIC]
    for a, b in runs(m, min_len=int(st.music_min)):
        b = min(b, dur)
        touches = any(abs(a - ev["end"]) <= 1.0 or abs(ev["start"] - b) <= 1.0 for ev in music_gaps)
        if touches and b - a <= 4.0:
            continue
        bad.append({"start": float(a), "end": float(b), "kind": KIND_MUSIC_UNDER})

    for ev in events:
        if ev["where"] in ("intro", "outro", "all") and ev["dur"] >= st.edge_min:
            bad.append(ev)
        elif ev["where"] == "inside" and (ev["dur"] > st.max_gap
                                          or (ev["kind"] == KIND_MUSIC and ev["dur"] >= st.music_min)
                                          or (st.other_min > 0 and ev["kind"] == KIND_OTHER
                                              and ev["dur"] >= st.other_min)):
            bad.append(ev)
    for s, e, lv, ref in quiet_speech(wav16, segs, dur, st):
        bad.append({"start": s, "end": e, "kind": KIND_QUIET_SPEECH, "level_db": round(lv, 1)})
    bad.sort(key=lambda x: x["start"])

    merged = []
    for ev in bad:
        if merged and ev["start"] - merged[-1]["end"] <= st.merge_gap:
            merged[-1]["end"] = max(merged[-1]["end"], ev["end"])
            merged[-1]["kinds"].add(ev["kind"])
        else:
            merged.append({"start": ev["start"], "end": ev["end"], "kinds": {ev["kind"]}})
    merged2 = []
    for mg in merged:  # a short piece of speech left between two cuts is not a usable conversation
        if merged2 and mg["start"] - merged2[-1]["end"] < st.min_keep:
            merged2[-1]["end"] = max(merged2[-1]["end"], mg["end"])
            merged2[-1]["kinds"] |= mg["kinds"] | {KIND_LEFTOVER}
        else:
            merged2.append(mg)
    cuts = []
    for mg in merged2:
        a = 0.0 if mg["start"] <= 1.0 else float(round(mg["start"]))
        b = dur if mg["end"] >= dur - 1.0 else float(math.floor(mg["end"]))  # never eat the next syllable
        if b > a:
            cuts.append({"start": a, "end": b, "reasons": sorted(mg["kinds"])})
    hints = [ev for ev in events if ev["where"] == "inside" and st.short_gap <= ev["dur"] <= st.max_gap
             and not any(c["start"] <= ev["start"] and ev["end"] <= c["end"] for c in cuts)]
    return cuts, events, hints, speech_db, music_sec


def speech_chunks(segs, music_sec, st, max_len=8.0, min_len=3.0):
    chunks = []
    for s, e in segs:
        t = s
        while e - t >= min_len:
            b = min(t + max_len, e)
            if e - b < min_len:
                b = e if e - t <= max_len * 1.5 else b
            if float(np.mean(music_sec[int(t):max(int(np.ceil(b)), int(t) + 1)] >= st.music_thr)) < 0.3:
                chunks.append((t, b))
            t = b
    return chunks


def accent_vote(models, wav16, chunks, max_chunks=80):
    if not chunks:
        return None
    import torch
    feats, norm, emb, clf, labels = models.accent()
    if len(chunks) > max_chunks:
        pick = np.linspace(0, len(chunks) - 1, max_chunks).round().astype(int)
        chunks = [chunks[i] for i in pick]
    votes, timeline = Counter(), []
    with torch.no_grad():
        for s, e in chunks:
            x = torch.from_numpy(wav16[int(s * 16000):int(e * 16000)])[None].float()
            lens = torch.ones(1)
            f = norm(feats(x), lens)
            out = clf(emb(f, lens)).squeeze(1)
            lab = labels[int(out.argmax(dim=-1)[0])]
            votes[lab] += 1
            timeline.append((s, e, lab))
    n = sum(votes.values())
    groups = Counter()
    for k, v in votes.items():
        groups[ACCENT_GROUP.get(k, "другой")] += v
    other = []
    for s, e, lab in timeline:
        g = ACCENT_GROUP.get(lab, "другой")
        if other and other[-1]["group"] == g and s - other[-1]["end"] <= 15:
            other[-1]["end"], other[-1]["n"] = e, other[-1]["n"] + 1
        else:
            other.append({"group": g, "start": s, "end": e, "n": 1})
    other = [o for o in other if o["group"] != "британский" and o["n"] >= 2]
    return {"n": n, "labels": {k: round(v / n, 2) for k, v in votes.most_common()},
            "groups": {k: round(v / n, 2) for k, v in groups.most_common()},
            "top": groups.most_common(1)[0][0], "non_british": other}


def transcribe(model, clips, progress=None):
    """Whisper text of each clip (16 kHz float32 arrays)."""
    out = []
    for i, clip in enumerate(clips):
        seg_iter, _ = model.transcribe(clip, language="en", beam_size=1, vad_filter=False,
                                       condition_on_previous_text=False)
        out.append(" ".join(x.text.strip() for x in seg_iter))
        if progress:
            progress((i + 1) / len(clips))
    return out


def _transcribe_in_subprocess(models, clips):
    with tempfile.TemporaryDirectory(prefix="vidaudcont-asr-") as d:
        src, dst = os.path.join(d, "clips.npz"), os.path.join(d, "texts.json")
        np.savez(src, *clips)
        cmd = resources.self_command() + ["--transcribe-clips", src, dst, "--threads", str(models.threads)]
        r = resources.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError(f"расшифровка не удалась (код {r.returncode}): {(r.stderr or '')[-300:]}")
        with open(dst, encoding="utf-8") as f:
            return json.load(f)


def transcribe_excerpts(models, wav16, dur, segs, n_excerpts=4, excerpt=45.0):
    if not segs:
        return ""
    starts, clips = [], []
    for k in range(n_excerpts):
        c = dur * (k + 0.5) / n_excerpts
        s = max(0.0, c - excerpt / 2)
        e = min(dur, s + excerpt)
        starts.append(s)
        clips.append(np.ascontiguousarray(wav16[int(s * 16000):int(e * 16000)], dtype=np.float32))
    texts = _transcribe_in_subprocess(models, clips) if models.asr_in_subprocess else transcribe(models.asr(), clips)
    return "\n".join(f"[{fmt_time(s)}] {t}" for s, t in zip(starts, texts))


def speech_chunks_for_asr(segs, max_len=28.0, join_gap=1.5):
    """Speech segments grouped into pieces Whisper takes at once (it hears 30 s at a time)."""
    out = []
    for s, e in segs:
        while e - s > max_len:  # a very long stretch of speech: cut it into Whisper-sized pieces
            out.append([s, s + max_len])
            s += max_len
        if out and s - out[-1][1] <= join_gap and e - out[-1][0] <= max_len:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(a, b) for a, b in out if b - a >= 0.3]


def transcribe_full(models, wav16, segs, paragraph_gap=3.0, progress=None):
    """The whole recording as text: every stretch of speech, a new paragraph after a longer pause."""
    chunks = speech_chunks_for_asr(segs)
    if not chunks:
        return ""
    clips = [np.ascontiguousarray(wav16[int(a * 16000):int(b * 16000)], dtype=np.float32) for a, b in chunks]
    texts = (_transcribe_in_subprocess(models, clips) if models.asr_in_subprocess
             else transcribe(models.asr(), clips, progress))
    out, prev_end = [], None
    for (a, b), t in zip(chunks, texts):
        t = t.strip()
        if not t:
            continue
        if out:
            out.append("\n" if a - prev_end >= paragraph_gap else " ")
        out.append(t)
        prev_end = b
    return "".join(out)


# ---------------------------------------------------------------- conversation or not
SPEAKER_WIN = 1.5           # pieces of speech of this length get a speaker embedding each
DIALOGUE_SEPARATION = 0.15  # two groups of voices at least this far apart (silhouette) = two people ...
DIALOGUE_MINOR = 0.15       # ... the second one speaking at least this share of the time ...
DIALOGUE_TURNS = 1.8        # ... and taking turns at least this often per minute of speech


def speaker_windows(segs, min_len=0.8):
    """Speech cut into 1.5-s pieces; a short reply ("Yes", "Same") is a piece of its own."""
    out = []
    for s, e in segs:
        if e - s < min_len:
            continue
        t, first = s, len(out)
        while t + SPEAKER_WIN <= e + 0.25:
            out.append((t, min(t + SPEAKER_WIN, e)))
            t += SPEAKER_WIN
        if len(out) == first:
            out.append((s, e))
    return out


def dialogue_structure(models, wav16, segs):
    """Two people taking turns? The pieces of speech are split into two groups by voice. A conversation has
    two distinct voices, each with a fair share, alternating through the recording. A lecture, a voice-over or
    an examination commented by one person has one voice, or two voices in long blocks (narrator, then scene)."""
    from scipy.cluster.hierarchy import fcluster, linkage
    wins = speaker_windows(segs)
    if len(wins) < 20:
        return {"pieces": len(wins), "conversation": None, "reason": "слишком мало речи, чтобы судить"}
    if len(wins) > 1500:  # hours of speech: every n-th piece is plenty and keeps memory in check
        wins = wins[::len(wins) // 1500 + 1]
    E = models.speaker_embeddings(wav16, wins)
    E = E - E.mean(axis=0)  # what every piece shares (room, microphone) says nothing about who is speaking
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    lab = fcluster(linkage(E, method="ward"), t=2, criterion="maxclust")
    S = E @ E.T
    same = lab[:, None] == lab[None, :]
    inside = np.where(same, S, 0).sum(1) / np.maximum(same.sum(1), 1)
    across = np.where(~same, S, 0).sum(1) / np.maximum((~same).sum(1), 1)
    separation = float(np.mean(inside - across))
    minor = float(min(np.mean(lab == 1), np.mean(lab == 2)))
    smooth = [np.bincount(lab[max(0, i - 2): i + 3]).argmax() for i in range(len(lab))]
    turns = sum(1 for x, y in zip(smooth, smooth[1:]) if x != y)
    per_min = turns / max(sum(e - s for s, e in wins) / 60, 1e-6)
    two = separation >= DIALOGUE_SEPARATION and minor >= DIALOGUE_MINOR
    conversation = two and per_min >= DIALOGUE_TURNS
    reason = "" if conversation else ("говорит в основном один человек" if not two
                                      else "голоса звучат длинными блоками, а не чередуются")
    return {"pieces": len(wins), "separation": round(separation, 3), "minor_share": round(minor, 2),
            "turns_per_min": round(per_min, 1), "conversation": bool(conversation), "reason": reason}


ENCOUNTER_MIN = 1.0  # clinician-to-patient phrases per 100 words: examinations 1.4-10.8, a lecture 0


def encounter(res):
    """One voice most of the time, but a clinician speaking to a patient who is there (an examination:
    "can you look at my finger", "take a deep breath", "thank you"), not to an audience."""
    from .topic import encounter_score
    e = encounter_score(res.get("transcript") or "")
    return e["encounter"] >= ENCOUNTER_MIN and e["encounter"] > e["lecture"]


def suitability(res, check_dialogue=True):
    """Why a recording is not a medical conversation (or examination) with a British accent; [] = it is one.
    None when the conversation check has not been done for it yet, or nothing was checked at all.
    check_dialogue=False (the check turned off in the settings): only the accent and the topic count."""
    d, t, a = res.get("dialogue"), res.get("topic"), res.get("accent")
    if check_dialogue and d is None:
        return None
    why = []
    if check_dialogue and d.get("conversation") is False and not encounter(res):
        why.append("НЕ РАЗГОВОР")
    if t and t.get("medical") is False:
        why.append("НЕ МЕДИЦИНСКАЯ ТЕМА")
    if a and a.get("top") and a["top"] != "британский":
        why.append(ACCENT_NOTE.get(a["top"], "НЕ БРИТАНСКИЙ АКЦЕНТ"))
    if not why and not check_dialogue and not t and not (a and a.get("top")):
        return None
    return why


def analyze(path, models, settings=None, progress=None, cancelled=None):
    """Analyse one audio/video file. progress(fraction, text) is called along the way."""
    st = settings or Settings()

    def step(frac, text):
        if cancelled and cancelled():
            raise InterruptedError("остановлено")
        if progress:
            progress(frac, text)

    step(0.0, "чтение звука")
    wav16, phase = load_audio(path, 16000, with_info=True)
    dur = len(wav16) / 16000
    if dur < 1.0:
        raise ValueError("в файле нет звука или он короче секунды")
    step(0.08, "поиск речи")
    segs = speech_segments(models, wav16)
    step(0.15, "поиск музыки и шумов")
    wav32 = load_audio(path, 32000)
    P, labels = tag_seconds(models, wav32, progress=lambda f: step(0.15 + 0.45 * f, "поиск музыки и шумов"))
    del wav32
    step(0.6, "правила вырезки")
    cuts, events, hints, speech_db, music_sec = find_cuts(wav16, dur, segs, P, labels, st)
    accent = None
    if st.accent:
        step(0.62, "определение акцента")
        accent = accent_vote(models, wav16, speech_chunks(segs, music_sec, st))
    dialogue = None
    if st.dialogue:
        step(0.7, "разговор или лекция")
        dialogue = dialogue_structure(models, wav16, segs)
    transcript, topic = "", None
    if st.topic:
        step(0.8, "расшифровка фрагментов для темы")
        transcript = transcribe_excerpts(models, wav16, dur, segs)
        topic = topic_score(transcript)
    step(1.0, "готово")
    return {
        "file": os.path.abspath(path),
        "duration": dur,
        "speech_ratio": round(sum(e - s for s, e in segs) / max(dur, 1e-9), 3),
        "speech_db": round(speech_db, 1),
        "cuts": cuts,
        "timecodes": format_cuts([(c["start"], c["end"]) for c in cuts], dur),
        "hints": [{"start": h["start"], "end": h["end"], "kind": h["kind"]} for h in hints],
        "accent": accent,
        "topic": topic,
        "transcript": transcript,
        "phase": phase,
        "dialogue": dialogue,
        "segments": segs,
        "music_per_second": [round(float(x), 3) for x in music_sec],
        "settings": asdict(st),
    }
