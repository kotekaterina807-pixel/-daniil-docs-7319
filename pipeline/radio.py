"""Радиопереговоры: голоса пилотов «из эфира».

Строка сценария:  [радио] Кто говорит | Фраза в оригинале | Перевод
Каждая фраза озвучивается отдельным мужским голосом (бесплатные голоса Edge, у каждого говорящего свой),
затем звук делается «радийным»: узкая полоса частот, лёгкие искажения, шум эфира, щелчки
тангенты в начале и в конце. Если голоса Edge недоступны — фраза звучит голосом диктора (перевод)."""
import hashlib
import json
import os
import subprocess
import wave
from pathlib import Path

from .config import ffmpeg_exe
from . import tts

SR = tts.SAMPLE_RATE
GAP = 0.45                     # пауза эфира между фразами
_NW = 0x08000000 if os.name == "nt" else 0
VOICES = ["en-US-GuyNeural", "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-RogerNeural",
          "en-US-SteffanNeural", "en-US-AndrewNeural", "en-US-BrianNeural", "en-GB-RyanNeural"]
RADIO = ("highpass=f=380,lowpass=f=2900,acompressor=threshold=-22dB:ratio=6:attack=5:release=80,"
         "acrusher=bits=12:mode=log:aa=1:mix=0.18,volume=1.5")


def parse(val):
    """«Кто | фраза | перевод» → dict."""
    parts = [x.strip() for x in val.split("|")]
    return {"who": parts[0] if parts else "", "en": parts[1] if len(parts) > 1 else "",
            "de": parts[2] if len(parts) > 2 else ""}


def _speaker_key(who):
    """«15:40 · FT-28 · Lt. Charles Taylor» → «lt. charles taylor» (время и позывной не меняют голос)."""
    parts = [p.strip() for p in who.replace("—", "·").split("·") if p.strip()]
    return (parts[-1] if parts else who).lower()


def voice_map(lines):
    """Один и тот же человек — всегда один голос; разные люди — разные голоса."""
    out = {}
    for ln in lines:
        k = _speaker_key(ln.get("who", ""))
        if k not in out:
            h = int(hashlib.md5(k.encode()).hexdigest()[:6], 16)
            free = [v for v in VOICES if v not in out.values()] or VOICES
            out[k] = free[h % len(free)]
    return out


def key(lines, cfg):
    return hashlib.md5(json.dumps([lines, cfg.get("edge_voice", ""), "r2"], ensure_ascii=False).encode()).hexdigest()[:16]


def _dur(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _radioize(src, out, seed):
    d = _dur(src) + 0.5
    flt = (f"[0:a]aformat=sample_rates={SR}:channel_layouts=mono,adelay=180|180,apad=pad_dur=0.32,{RADIO}[v];"
           f"[1:a]highpass=f=600,lowpass=f=3600,"
           f"volume='if(lt(t,0.14),5.5,if(gt(t,{d - 0.2:.2f}),5.5,1))':eval=frame[n];"
           f"[v][n]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.92[a]")
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-f", "lavfi", "-t", f"{d:.2f}", "-i", f"anoisesrc=color=pink:amplitude=0.035:sample_rate={SR}:seed={seed}",
           "-filter_complex", flt, "-map", "[a]", "-ac", "1", "-ar", str(SR), str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=_NW)
    if p.returncode != 0:
        raise tts.TTSError(f"радиоэффект: {p.stderr[-300:]}")


def _silence(out, sec, seed):
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", f"{sec:.2f}",
           "-i", f"anoisesrc=color=pink:amplitude=0.02:sample_rate={SR}:seed={seed}",
           "-af", "highpass=f=600,lowpass=f=3600", "-ac", "1", "-ar", str(SR), str(out)]
    subprocess.run(cmd, capture_output=True, creationflags=_NW, check=True)


def make(cfg, niche, lines, out_wav, log=print):
    """Склеивает фразы в один WAV и пишет рядом <out>.json с временем каждой фразы. Возвращает список фраз с a/b."""
    out_wav = Path(out_wav)
    side = out_wav.with_suffix(".json")
    if out_wav.exists() and side.exists():
        return json.loads(side.read_text(encoding="utf-8"))
    vm = voice_map(lines)
    parts, timed, pos = [], [], 0.0
    tmp = out_wav.parent
    for i, ln in enumerate(lines):
        base = tmp / f"{out_wav.stem}_r{i}"
        raw_wav = base.with_suffix(".raw.wav")
        spoken = False
        if ln.get("en"):
            mp3 = base.with_suffix(".mp3")
            try:
                tts._edge(cfg, ln["en"], mp3, lambda m: None, voice=vm[_speaker_key(ln.get("who", ""))])
                tts.to_wav([mp3], raw_wav)
                spoken = True
            except Exception as e:
                log(f"  Голос пилота (Edge) недоступен ({str(e)[:80]}) — фразу прочитает диктор.")
            finally:
                mp3.unlink(missing_ok=True)
        if not spoken:
            tts.synthesize(cfg, niche, ln.get("de") or ln.get("en", ""), raw_wav, log=log)
        fx_wav = base.with_suffix(".fx.wav")
        _radioize(raw_wav, fx_wav, 11 + i)
        raw_wav.unlink(missing_ok=True)
        if parts:
            gap = base.with_suffix(".gap.wav")
            _silence(gap, GAP, 31 + i)
            parts.append(gap)
            pos += _dur(gap)
        d = _dur(fx_wav)
        timed.append(dict(ln, a=round(pos + 0.18, 3), b=round(pos + d - 0.3, 3)))
        parts.append(fx_wav)
        pos += d
    tts.to_wav(parts, out_wav)
    for p in parts:
        Path(p).unlink(missing_ok=True)
    side.write_text(json.dumps(timed, ensure_ascii=False, indent=1), encoding="utf-8")
    return timed
