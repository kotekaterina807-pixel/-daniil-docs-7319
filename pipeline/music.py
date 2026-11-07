"""Музыка по настроению рассказа: спокойная глава — спокойный трек, тревожная — тревожный.
Программа сама «слушает» каждый трек в папке music (насколько он плотный, резкий, беспокойный)
и раскладывает треки на спокойные / загадочные / тревожные. Для каждой главы берётся трек её
настроения, между главами — плавный переход 3 секунды. Настроение главы: тег [музыка] в сценарии
(спокойная / загадочная / тревожная), иначе — по [переход] абзацев."""
import json
import os
import random
import re
import subprocess
from pathlib import Path

import numpy as np

from .config import MUSIC_DIR, ffmpeg_exe

EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac")
TENSE = re.compile(r"anxi|tens|suspens|horror|danger|chase|omin|scary|creep|thrill|fear|panic|dread|alarm|"
                   r"storm|battle|war|pursuit|nightmare|trevog|страх|тревог", re.I)
CALM = re.compile(r"calm|peace|soft|gentle|dream|sad|melanch|reflect|lullab|seren|quiet|tender|sorrow|"
                  r"memor|nostalg|piano|slow|спокой|грус", re.I)
MOODS = ("calm", "mystery", "tense")
TEXT = {"спокойная": "calm", "спокойно": "calm", "грустная": "calm", "загадочная": "mystery",
        "таинственная": "mystery", "мрачная": "mystery", "тревожная": "tense", "напряжённая": "tense",
        "напряженная": "tense", "страшная": "tense"}
_NW = 0x08000000 if os.name == "nt" else 0


def tracks(niche_id=""):
    for d in (MUSIC_DIR / niche_id, MUSIC_DIR) if niche_id else (MUSIC_DIR,):
        if d.exists():
            files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXT)
            if files:
                return files
    return []


def _features(path):
    sr = 11025
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", "15", "-t", "75", "-i", str(path),
                        "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, timeout=120,
                       creationflags=_NW)
    x = np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768
    if len(x) < sr * 5:
        return None
    n = 2048
    frames = x[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1)) + 1e-9
    freqs = np.fft.rfftfreq(n, 1 / sr)
    centroid = float(np.mean((spec * freqs).sum(1) / spec.sum(1)))
    logs = np.log(spec)
    flux = float(np.mean(np.maximum(0, np.diff(logs, axis=0)).mean(1)))      # как часто звук резко меняется
    rms = np.sqrt((frames ** 2).mean(1))
    dyn = float(np.std(rms) / (np.mean(rms) + 1e-9))
    return {"centroid": centroid, "flux": flux, "dyn": dyn}


def classify(files, log=print):
    """{путь: calm|mystery|tense}. Анализ кэшируется в music/.настроения.json."""
    cache_f = MUSIC_DIR / ".настроения.json"
    try:
        cache = json.loads(cache_f.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    feats = {}
    for f in files:
        key = f"{f.name}|{f.stat().st_size}"
        if key not in cache:
            try:
                cache[key] = _features(f)
            except Exception:
                cache[key] = None
        if cache[key]:
            feats[f] = cache[key]
    try:
        cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    out = {}
    if feats:
        arr = {k: np.array([v[k] for v in feats.values()]) for k in ("centroid", "flux", "dyn")}
        z = lambda k, v: (v - arr[k].mean()) / (arr[k].std() + 1e-9)
        score = {f: z("centroid", v["centroid"]) + 1.5 * z("flux", v["flux"]) + 0.7 * z("dyn", v["dyn"])
                 for f, v in feats.items()}
        ranked = sorted(score, key=score.get)
        for i, f in enumerate(ranked):
            out[f] = MOODS[min(2, i * 3 // max(1, len(ranked)))]
    for f in files:                                     # название трека подсказывает лучше любого анализа
        if TENSE.search(f.stem):
            out[f] = "tense"
        elif CALM.search(f.stem):
            out[f] = "calm"
        out.setdefault(f, "mystery")
    return out


def chapter_mood(ch):
    for s in ch.get("segments", []):
        m = TEXT.get(str(s.get("music", "")).lower().strip())
        if m:
            return m
    moods = [s.get("mood", "normal") for s in ch.get("segments", [])]
    if moods.count("climax") * 2 >= max(1, len(moods)):      # [переход] эффектный — напряжённая глава
        return "tense"
    return "mystery"


def build_bed(script, chapter_starts, total_sec, niche_id, out, log=print):
    """Музыкальная дорожка на всё видео: по треку на главу, плавные переходы. Возвращает (путь, описание) или (None, '')."""
    files = tracks(niche_id)
    if not files:
        return None, ""
    moods = classify(files, log)
    chs = script.get("chapters") or []
    starts = [t for _, t in chapter_starts] or [0.0]
    parts, used, desc = [], [], []
    for i, ch in enumerate(chs[: len(starts)]):
        a = starts[i]
        b = starts[i + 1] if i + 1 < len(starts) else total_sec
        want = chapter_mood(ch)
        if parts and parts[-1][3] == want:              # глава того же настроения — тот же трек продолжается
            parts[-1][2] = b
            continue
        pool = [f for f in files if moods.get(f) == want and f not in used] or \
               [f for f in files if moods.get(f) == want] or [f for f in files if f not in used] or files
        f = random.choice(pool)
        used.append(f)
        parts.append([f, a, b, want])
        desc.append(f"{ch.get('title', '')}: {f.stem} ({ {'calm': 'спокойная', 'mystery': 'загадочная', 'tense': 'тревожная'}[want]})")
    if len(parts) == 1:
        return parts[0][0], desc[0]
    X = 3.0                                             # переход между треками, с
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    flt = []
    for k, (f, a, b, _) in enumerate(parts):
        cmd += ["-stream_loop", "-1", "-i", str(f)]
        dur = (b - a) + (X if k + 1 < len(parts) else 1.0)
        flt.append(f"[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-24:TP=-4:LRA=11,"
                   f"atrim=0:{dur:.2f},asetpts=PTS-STARTPTS[t{k}]")
    cur = "[t0]"
    for k in range(1, len(parts)):
        flt.append(f"{cur}[t{k}]acrossfade=d={X}:c1=tri:c2=tri[x{k}]")
        cur = f"[x{k}]"
    cmd += ["-filter_complex", ";".join(flt), "-map", cur, "-ar", "48000", "-ac", "2", str(out)]
    p = subprocess.run(cmd, capture_output=True, timeout=1800, creationflags=_NW)
    if p.returncode != 0 or not Path(out).exists():
        log(f"  Музыка по главам не сложилась ({p.stderr.decode(errors='replace')[-160:]}) — один трек на всё видео.")
        return parts[0][0], desc[0]
    return Path(out), "; ".join(desc)
