"""Ведущий в кадре (ниша «еда»): нарисованный персонаж говорит голосом диктора.

Рот открывается по громкости озвучки (каждый кадр), глаза моргают, голова слегка покачивается, брови
поднимаются на громких словах. Фон — тёмная комната с неоновой полосой, перед ведущим микрофон.

Свой ведущий: положите в папку программы `host/` картинки одного размера (PNG с прозрачностью):
  closed.png — рот закрыт, open.png — рот открыт, (необязательно) blink.png — глаза закрыты, bg.png — фон 1920x1080.
Тогда вместо нарисованного персонажа будут они (рот переключается по громкости голоса)."""
import math
import random
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .config import ffmpeg_exe, ROOT

W, H, FPS = 1920, 1080, 60
_NO_WINDOW = 0x08000000 if __import__("os").name == "nt" else 0
SS = 2                                   # рисуем в 2 раза крупнее и уменьшаем — гладкие края


def mouth_curve(wav_path, t0, length, fps=FPS):
    """Открытость рта 0..1 на каждый кадр участка озвучки [t0, t0+length]."""
    n = max(1, int(round(length * fps)))
    try:
        with wave.open(str(wav_path), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            w.setpos(min(w.getnframes(), int(t0 * sr)))
            raw = w.readframes(int(length * sr) + 1)
        a = np.frombuffer(raw, dtype=np.int16 if sw == 2 else np.uint8).astype(np.float32)
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        hop = sr / fps
        rms = np.array([np.sqrt(np.mean(a[int(i * hop):int(i * hop + 2 * hop)] ** 2) + 1e-9) for i in range(n)])
        ref = np.percentile(rms, 92) if rms.max() > 0 else 1.0
        v = np.clip((rms / max(ref, 1e-6) - 0.18) / 0.7, 0, 1)
    except Exception:
        v = np.zeros(n)
    out, prev = [], 0.0
    for x in v:                                     # быстро открывается, мягко закрывается
        prev = x if x > prev else prev * 0.55 + x * 0.45
        out.append(float(prev))
    return out


@lru_cache(maxsize=1)
def _room():
    """Фон: тёмная комната, слева фиолетовая неоновая полоса, справа тёплый свет, внизу стол."""
    if True:
        img = Image.new("RGB", (W, H), (34, 30, 44))
        glow = Image.new("RGB", (W, H), (0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([-300, -200, 900, 1200], fill=(110, 70, 190))
        gd.ellipse([1300, 100, 2300, 1100], fill=(150, 96, 70))
        glow = glow.filter(ImageFilter.GaussianBlur(220))
        img = Image.blend(img, glow, 0.55)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([340, 90, 362, 760], 10, fill=(190, 160, 255))           # неоновая полоса
        lamp = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(lamp).rounded_rectangle([300, 60, 402, 790], 50, fill=(120, 80, 220))
        img = Image.blend(img, Image.composite(lamp, img, lamp.convert("L").point(lambda v: 255 if v else 0))
                          .filter(ImageFilter.GaussianBlur(40)), 0.5)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([340, 90, 362, 760], 10, fill=(220, 205, 255))
        d.polygon([(1500, 520), (1560, 300), (1600, 520)], fill=(40, 70, 46))      # растение справа
        d.polygon([(1540, 520), (1640, 330), (1650, 520)], fill=(48, 84, 54))
        d.rectangle([1520, 520, 1640, 640], fill=(60, 56, 64))
        d.rectangle([0, 900, W, H], fill=(26, 24, 30))                               # стол
        d.rectangle([0, 900, W, 908], fill=(60, 56, 70))
        return img.filter(ImageFilter.GaussianBlur(3))


SKIN, SKIN_D, HAIR, HOODIE = (232, 190, 160), (205, 160, 132), (70, 50, 38), (30, 30, 34)


HX, HY, HW, HH = 350, 380, 700, 760                 # голова: центр лица внутри слоя головы и размер слоя
CX = W // 2 + 40                                     # центр ведущего по горизонтали


@lru_cache(maxsize=64)
def _head(open_, blink, brow):
    """Голова (RGBA) в одном положении рта/глаз/бровей — рисуется один раз и берётся из кэша."""
    s = SS
    head = Image.new("RGBA", (HW * s, HH * s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(head)
    hx, hy = HX * s, HY * s
    hd.ellipse([hx - 250 * s, hy - 140 * s, hx - 190 * s, hy + 10 * s], fill=SKIN_D)          # уши
    hd.ellipse([hx + 190 * s, hy - 140 * s, hx + 250 * s, hy + 10 * s], fill=SKIN_D)
    hd.rounded_rectangle([hx - 215 * s, hy - 300 * s, hx + 215 * s, hy + 230 * s], 200 * s, fill=SKIN)
    hd.chord([hx - 230 * s, hy - 360 * s, hx + 230 * s, hy - 40 * s], 180, 360, fill=HAIR)     # волосы
    hd.polygon([(hx - 225 * s, hy - 200 * s), (hx - 140 * s, hy - 300 * s), (hx + 60 * s, hy - 250 * s),
                (hx + 225 * s, hy - 210 * s), (hx + 225 * s, hy - 120 * s), (hx + 80 * s, hy - 190 * s),
                (hx - 225 * s, hy - 120 * s)], fill=HAIR)
    by = hy - 120 * s - int(brow * 18 * s)                                                     # брови
    hd.rounded_rectangle([hx - 150 * s, by, hx - 50 * s, by + 18 * s], 9 * s, fill=HAIR)
    hd.rounded_rectangle([hx + 50 * s, by, hx + 150 * s, by + 18 * s], 9 * s, fill=HAIR)
    for ex in (hx - 100 * s, hx + 100 * s):                                                    # глаза
        ey = hy - 50 * s
        if blink:
            hd.line([(ex - 34 * s, ey), (ex + 34 * s, ey)], fill=(60, 40, 34), width=7 * s)
        else:
            hd.ellipse([ex - 36 * s, ey - 26 * s, ex + 36 * s, ey + 26 * s], fill=(250, 250, 250))
            hd.ellipse([ex - 18 * s, ey - 20 * s, ex + 18 * s, ey + 20 * s], fill=(70, 96, 120))
            hd.ellipse([ex - 9 * s, ey - 10 * s, ex + 9 * s, ey + 10 * s], fill=(16, 16, 18))
            hd.ellipse([ex + 3 * s, ey - 12 * s, ex + 10 * s, ey - 5 * s], fill=(255, 255, 255))
    hd.arc([hx - 26 * s, hy - 10 * s, hx + 26 * s, hy + 50 * s], 20, 160, fill=SKIN_D, width=7 * s)   # нос
    my = hy + 120 * s                                                                          # рот
    mw = int((70 + 14 * open_) * s)
    mh = int((6 + 70 * open_) * s)
    if open_ < 0.08:
        hd.arc([hx - mw, my - 30 * s, hx + mw, my + 20 * s], 20, 160, fill=(120, 50, 50), width=8 * s)
    else:
        hd.rounded_rectangle([hx - mw, my - mh // 3, hx + mw, my + mh], int(min(mw, mh) * 0.8), fill=(90, 26, 32))
        hd.rectangle([hx - mw + 14 * s, my - mh // 3, hx + mw - 14 * s, my - mh // 3 + int(14 * s)], fill=(245, 245, 240))
        hd.ellipse([hx - mw // 2, my + mh // 3, hx + mw // 2, my + mh + 6 * s], fill=(200, 80, 90))
    return head.resize((HW, HH), Image.LANCZOS)


@lru_cache(maxsize=1)
def _body():
    """Туловище в худи и шея (RGBA, W x H)."""
    s = SS
    img = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = CX * s
    d.rectangle([cx - 70 * s, 640 * s, cx + 70 * s, 800 * s], fill=SKIN_D)                  # шея
    d.rounded_rectangle([cx - 430 * s, 780 * s, cx + 430 * s, 1400 * s], 200 * s, fill=HOODIE)
    d.polygon([(cx - 120 * s, 780 * s), (cx, 900 * s), (cx + 120 * s, 780 * s)], fill=(22, 22, 26))
    d.line([(cx - 60 * s, 820 * s), (cx - 70 * s, 960 * s)], fill=(200, 200, 205), width=6 * s)
    d.line([(cx + 60 * s, 820 * s), (cx + 70 * s, 960 * s)], fill=(200, 200, 205), width=6 * s)
    return img.resize((W, H), Image.LANCZOS)


@lru_cache(maxsize=1)
def _mic():
    """Подкаст-микрофон на стойке справа перед ведущим (RGBA, W x H)."""
    s = SS
    img = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mx, my = (CX + 250) * s, 760 * s
    d.line([(mx + 40 * s, my + 200 * s), (mx + 140 * s, 1080 * s)], fill=(18, 18, 20), width=24 * s)
    d.rounded_rectangle([mx - 60 * s, my - 70 * s, mx + 90 * s, my + 220 * s], 75 * s, fill=(26, 26, 28))
    for k in range(7):
        d.line([(mx - 48 * s, my + (-40 + k * 32) * s), (mx + 78 * s, my + (-40 + k * 32) * s)],
               fill=(52, 52, 56), width=4 * s)
    d.rounded_rectangle([mx - 70 * s, my + 60 * s, mx + 100 * s, my + 90 * s], 12 * s, fill=(40, 40, 44))
    return img.resize((W, H), Image.LANCZOS)


def _frame(bg, t, open_, blink, brow, sway):
    """Кадр: фон, туловище (дышит), голова (покачивается), микрофон."""
    fr = bg.convert("RGBA")
    breath = int(round(math.sin(t * 1.6) * 3))
    fr.alpha_composite(_body(), (0, breath))
    head = _head(open_, blink, brow).rotate(sway, resample=Image.BICUBIC, center=(HX, 620))
    fr.alpha_composite(head, (CX - HX, 190 + breath))
    fr.alpha_composite(_mic())
    return fr.convert("RGB")


def _custom():
    d = Path(ROOT) / "host"
    c, o = d / "closed.png", d / "open.png"
    if not (c.exists() and o.exists()):
        return None
    res = {"closed": Image.open(c).convert("RGBA"), "open": Image.open(o).convert("RGBA")}
    if (d / "blink.png").exists():
        res["blink"] = Image.open(d / "blink.png").convert("RGBA")
    res["bg"] = Image.open(d / "bg.png").convert("RGB").resize((W, H)) if (d / "bg.png").exists() else None
    return res


def make_host_clip(wav_path, t0, length, out, seed=0):
    """Видео (без звука) с говорящим ведущим на участке озвучки [t0, t0+length] секунд."""
    total = max(1, int(round(length * FPS)))
    mouth = mouth_curve(wav_path, t0, length)
    rnd = random.Random(seed)
    blinks, t = set(), rnd.uniform(0.6, 2.0)
    while t < length:                               # моргание раз в 2,5–5 с, 0,12 с
        for k in range(int(0.12 * FPS)):
            blinks.add(int(t * FPS) + k)
        t += rnd.uniform(2.5, 5.0)
    cust = _custom()
    bg = (cust or {}).get("bg") or _room()
    gen = 30                                        # рисуем 30 кадров/с, на выходе 60 (рот чаще и не нужен)
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(gen), "-i", "-", "-vf", f"fps={FPS}", "-frames:v", str(total),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        for k in range(int(math.ceil(total * gen / FPS))):
            tt = k / gen
            kk = int(tt * FPS)
            m = mouth[min(kk, len(mouth) - 1)]
            if cust:
                fig = cust["blink"] if kk in blinks and "blink" in cust else cust["open" if m > 0.25 else "closed"]
                frame = bg.copy()
                dx = int(math.sin(tt * 1.3) * 6)
                frame.paste(fig, ((W - fig.width) // 2 + dx, H - fig.height), fig)
            else:
                mq = round(m * 6) / 6                     # 7 положений рта — головы берутся из кэша
                sway = math.sin(tt * 0.9 + seed) * 2.2 + math.sin(tt * 2.3) * 0.6
                p.stdin.write(_frame(bg, tt, mq, kk in blinks, 1.0 if m > 0.85 else 0.0, sway).tobytes())
                continue
            p.stdin.write(frame.convert("RGB").tobytes())
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = p.stderr.read().decode("utf-8", "replace")
    if p.wait() != 0:
        raise RuntimeError(err[-800:])
    return out
