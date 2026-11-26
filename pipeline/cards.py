"""Информационные карточки в стиле познавательных каналов: большая цифра, дата и место, цитата с источником,
сравнение, заголовок с курсором, таймлайн, новость. Делаются сами — искать кадры не нужно.

Разметка в сценарии (одна строка = одна карточка под своё предложение):
  [цифра] 3.265 $ | Kosten für das ganze Haus
  [дата] 1937 | Madison, Wisconsin
  [цитата] Sicher und vollständig geprüft | Hersteller, Pressemitteilung 2004
  [сравнение] Holzrahmen | 6 Wochen | Paneelhaus | 1 Tag
  [заголовок] Was ist das?
  [таймлайн] 1935 Forschung beginnt ; 1937 Testhaus ; 2007 im Baurecht
  [новость] Reuters | 3.000 Kinder betroffen | Jan 2023
  [далее] Сладость, которая убивала неделями   — тизер «Далее ?» перед следующим пунктом (еда)
  [список] Гибель плода ; Развитие вне матки ; Защитный механизм — пункты появляются по очереди (страхи)
Тема «void» (страхи): чёрный фон, белый текст с засечками и мягким свечением.
"""
import math
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .extras import _font as _xfont, FONT_CANDIDATES

SERIF_FONTS = [r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\georgia.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
_TL = threading.local()                 # тема «void»: все шрифты карточки — с засечками


def _font(size, candidates=FONT_CANDIDATES):
    return _xfont(size, SERIF_FONTS if getattr(_TL, "serif", False) else candidates)

W, H = 1920, 1080
BODY_FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
TEXT_FONTS = [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
KINDS = {"цифра": "number", "число": "number", "дата": "date", "цитата": "quote", "сравнение": "versus",
         "заголовок": "title", "таймлайн": "timeline", "новость": "news", "далее": "next", "список": "list"}
ANIM = 1.1                      # секунды анимации появления; дальше карточка стоит с лёгким приближением

THEMES = {
    # стройка: светлый серый фон, тёмный текст, белые карточки со скруглением
    "light": {"bg1": (238, 240, 243), "bg2": (196, 200, 207), "text": (22, 26, 30), "muted": (92, 100, 108),
              "card": (255, 255, 255), "shadow": 70, "pill_text": (255, 255, 255)},
    # еда: тёмный графит с тёплым свечением, светлый текст
    "dark": {"bg1": (44, 22, 30), "bg2": (12, 12, 16), "text": (246, 244, 240), "muted": (178, 172, 168),
             "card": (250, 250, 248), "shadow": 140, "pill_text": (255, 255, 255)},
    # тайны: тёмно-синий, приглушённый
    "night": {"bg1": (30, 36, 48), "bg2": (8, 10, 14), "text": (236, 232, 222), "muted": (160, 160, 150),
              "card": (240, 236, 226), "shadow": 150, "pill_text": (255, 255, 255)},
    # страхи: чистый чёрный, белый текст с засечками и свечением
    "void": {"bg1": (6, 6, 8), "bg2": (0, 0, 0), "text": (242, 242, 248), "muted": (150, 150, 165),
             "card": (20, 20, 24), "shadow": 0, "pill_text": (255, 255, 255)},
}
YELLOW = (250, 204, 21)


def _ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _bg(theme, seed=""):
    th = THEMES.get(theme) or THEMES["dark"]
    if theme == "light":                                  # стройка: та же серебристая студия, что под карточками кадров
        from .looks import studio_bg
        return studio_bg().copy()
    if theme == "void":                                   # страхи: просто чёрный
        return Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.new("RGB", (W, H), th["bg2"])
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    h_ = abs(hash(seed)) % 400
    cx, cy = W // 2 + (h_ - 200), int(H * 0.42)
    for r in range(900, 0, -30):                          # мягкое радиальное свечение к центру
        gd.ellipse([cx - r * 1.4, cy - r, cx + r * 1.4, cy + r], fill=int(255 * (1 - r / 900) ** 1.6))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    top = Image.new("RGB", (W, H), th["bg1"])
    img = Image.composite(top, img, glow)
    if theme == "dark":                                   # тонкая сетка, как на тёмных инфо-каналах
        d = ImageDraw.Draw(img, "RGBA")
        for x in range(0, W, 64):
            d.line([x, 0, x, H], fill=(255, 255, 255, 9))
        for y in range(0, H, 64):
            d.line([0, y, W, y], fill=(255, 255, 255, 9))
    return img


def _fit(text, max_w, sizes, fonts, d):
    for s in sizes:
        f = _font(s, fonts)
        if d.textlength(text, font=f) <= max_w:
            return f
    return _font(sizes[-1], fonts)


def _wrap(text, f, max_w, d):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and d.textlength(t, font=f) > max_w:
            out.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        out.append(cur)
    return out


def _shadowed(base, box, radius, fill, alpha, offset=(0, 18)):
    x0, y0, x1, y1 = [int(v) for v in box]
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]], radius,
                                         fill=alpha)
    sh = sh.filter(ImageFilter.GaussianBlur(26))
    base.paste((0, 0, 0), (0, 0), sh)
    ImageDraw.Draw(base).rounded_rectangle([x0, y0, x1, y1], radius, fill=fill)


def _pill(d, cx, y, text, accent, th, size=40, alpha=255):
    f = _font(size, BODY_FONTS)
    t = text.upper()
    w = d.textlength(t, font=f)
    pad = 26
    x0 = cx - w / 2 - pad
    d.rounded_rectangle([x0, y, x0 + w + 2 * pad, y + size + 30], (size + 30) // 2, fill=tuple(accent) + (alpha,))
    d.text((x0 + pad, y + 13), t, font=f, fill=th["pill_text"] + (alpha,))
    return y + size + 30


def _count_text(big, k):
    """«3.265 $» при k=0.4 → «1.306 $»: число растёт, разделители тысяч сохраняются. Дроби не анимируются."""
    m = re.search(r"\d[\d.,\u202f ]*\d|\d", big)
    if not m or k >= 1:
        return big
    raw = m.group(0)
    groups = re.split(r"[.,\u202f ]", raw)
    sep = next((c for c in raw if not c.isdigit()), "")
    if sep and not all(len(g) == 3 for g in groups[1:]):
        return big                                     # дробное число — показываем сразу
    digits = "".join(groups)
    if len(digits) > 12:
        return big
    val = str(int(round(int(digits) * k)))
    if sep:
        val = f"{int(val):,}".replace(",", sep)
    return big[:m.start()] + val + big[m.end():]


def _frame(kind, parts, t, theme, accent, base):
    """Кадр карточки в момент t (секунды от начала)."""
    th = THEMES.get(theme) or THEMES["dark"]
    im = base.copy()
    d = ImageDraw.Draw(im, "RGBA")
    a = _ease(t / 0.6)                                    # общее появление
    dy = int((1 - a) * 60)
    al = int(255 * a)
    col = th["text"] + (al,)
    if kind == "number":
        big = parts[0]
        k = _ease(t / 0.9)
        shown = _count_text(big, k)
        f = _fit(big, W - 260, (260, 220, 190, 160, 130, 110), FONT_CANDIDATES, d)
        w = d.textlength(shown, font=f)
        y = int(H / 2 - f.size * 0.85) + dy
        d.text(((W - w) / 2, y), shown, font=f, fill=col)
        if len(parts) > 1 and parts[1]:
            b = _ease((t - 0.35) / 0.5)
            if b > 0:
                _pill(d, W / 2, y + f.size + 60 + int((1 - b) * 30), parts[1], accent, th, 42, int(255 * b))
    elif kind == "date":
        f = _fit(parts[0], W - 300, (200, 170, 140, 120, 100), FONT_CANDIDATES, d)
        w = d.textlength(parts[0], font=f)
        y = int(H / 2 - f.size * 0.9) + dy
        d.text(((W - w) / 2, y), parts[0], font=f, fill=tuple(accent) + (al,))
        if len(parts) > 1 and parts[1]:
            b = _ease((t - 0.3) / 0.5)
            if b > 0:
                _pill(d, W / 2, y + f.size + 50 + int((1 - b) * 30), parts[1], (34, 34, 38) if theme == "light"
                      else (240, 240, 236), dict(th, pill_text=(255, 255, 255) if theme == "light" else (20, 20, 24)),
                      44, int(255 * b))
    elif kind == "quote":
        text = parts[0].strip("«»\"„“ ")
        f = _font(66, BODY_FONTS)
        lines = _wrap(text, f, 1240, d)
        while len(lines) > 4 and f.size > 44:
            f = _font(f.size - 6, BODY_FONTS)
            lines = _wrap(text, f, 1240, d)
        lh = int(f.size * 1.3)
        y0 = H // 2 - (len(lines) * lh) // 2 - 30 + dy
        d.text((300, y0 - 150), "\u201C", font=_font(220, FONT_CANDIDATES), fill=tuple(accent) + (al,))
        for i, ln in enumerate(lines):
            b = _ease((t - 0.15 - i * 0.12) / 0.45)
            if b > 0:
                d.text((340 + int((1 - b) * 40), y0 + i * lh), ln, font=f, fill=th["text"] + (int(255 * b),))
        if len(parts) > 1 and parts[1]:
            b = _ease((t - 0.5) / 0.5)
            if b > 0:
                d.text((340, y0 + len(lines) * lh + 30), "— " + parts[1], font=_font(36, TEXT_FONTS),
                       fill=th["muted"] + (int(255 * b),))
    elif kind == "versus":
        p = parts + [""] * (4 - len(parts))
        for side in (0, 1):
            b = _ease((t - side * 0.35) / 0.55)
            if b <= 0:
                continue
            cx = W / 2 + (-1 if side == 0 else 1) * 430
            x0, x1 = cx - 360, cx + 360
            y0 = 300 + int((1 - b) * 70)
            _shadowed(im, (x0, y0, x1, y0 + 480), 34, th["card"] if theme == "light" else (28, 28, 34), th["shadow"])
            d = ImageDraw.Draw(im, "RGBA")
            lab, val = p[side * 2], p[side * 2 + 1]
            fl = _fit(lab.upper(), 640, (54, 48, 42, 36), BODY_FONTS, d)
            d.text((cx - d.textlength(lab.upper(), font=fl) / 2, y0 + 70), lab.upper(), font=fl,
                   fill=(th["muted"] if theme == "light" else (200, 196, 190)) + (255,))
            fv = _fit(val, 640, (150, 128, 110, 92, 76), FONT_CANDIDATES, d)
            vc = tuple(accent) if side == 1 else (th["text"] if theme == "light" else (246, 244, 240))
            d.text((cx - d.textlength(val, font=fv) / 2, y0 + 220), val, font=fv, fill=vc + (255,))
        b = _ease((t - 0.6) / 0.4)
        if b > 0:
            r = int(64 * b)
            d.ellipse([W / 2 - r, 540 - r, W / 2 + r, 540 + r], fill=tuple(accent) + (255,))
            fvs = _font(46, FONT_CANDIDATES)
            d.text((W / 2 - d.textlength("VS", font=fvs) / 2, 540 - 30), "VS", font=fvs, fill=(255, 255, 255, int(255 * b)))
    elif kind == "title":
        text = parts[0].upper()
        f = _fit(text + "_", W - 300, (150, 130, 110, 92, 80), FONT_CANDIDATES, d)
        n = int(len(text) * _ease(t / 0.8)) if t < 0.8 else len(text)
        shown = text[:n] + ("_" if int(t * 2.4) % 2 == 0 or n < len(text) else " ")
        w = d.textlength(text + "_", font=f)
        d.text(((W - w) / 2, H / 2 - f.size / 2 - 20), shown, font=f, fill=th["text"] + (255,))
    elif kind == "timeline":
        items = [x.strip() for x in parts[0].split(";") if x.strip()][:5]
        n = len(items)
        top = H / 2 - (n - 1) * 95
        x_line = 640
        prog = _ease(t / 0.9)
        d.line([x_line, top - 20, x_line, top - 20 + ((n - 1) * 190 + 40) * prog], fill=tuple(accent) + (230,), width=6)
        for i, it in enumerate(items):
            b = _ease((t - 0.15 - i * 0.22) / 0.4)
            if b <= 0:
                continue
            y = top + i * 190
            r = 16
            d.ellipse([x_line - r, y - r, x_line + r, y + r], fill=tuple(accent) + (int(255 * b),))
            m = re.match(r"^(\S+)\s+(.*)$", it)
            yr, rest = (m.group(1), m.group(2)) if m else (it, "")
            fy = _font(70, FONT_CANDIDATES)
            d.text((x_line + 50 + int((1 - b) * 40), y - 44), yr, font=fy, fill=tuple(accent) + (int(255 * b),))
            fr = _fit(rest, 900, (54, 48, 42, 36), BODY_FONTS, d)
            d.text((x_line + 70 + d.textlength(yr, font=fy) + int((1 - b) * 40), y - 32), rest, font=fr,
                   fill=th["text"] + (int(255 * b),))
    elif kind == "next":                                  # еда: тизер «Далее ?» — лента, знак вопроса, намёк
        from .looks import _hazard
        e = _ease(t / 0.35)
        band = _hazard(W + 300, 64, t * 120).rotate(4, expand=True, resample=Image.BICUBIC)
        im.paste(band, (int(-150 - (1 - e) * W), 120))
        d = ImageDraw.Draw(im, "RGBA")
        fl = _font(58, FONT_CANDIDATES)
        lab = (parts[1] if len(parts) > 1 and parts[1] else "ДАЛЕЕ").upper()
        if e > 0.6:
            tw = d.textlength(lab, font=fl)
            d.rectangle([W / 2 - tw / 2 - 24, 132, W / 2 + tw / 2 + 24, 212], fill=(14, 12, 14, 255))
            d.text((W / 2 - tw / 2, 140), lab, font=fl, fill=YELLOW + (255,))
        q = _ease((t - 0.25) / 0.35)
        if q > 0:
            r = int(150 * (0.7 + 0.3 * q))
            cy = 560
            d.rounded_rectangle([W / 2 - r, cy - r * 1.05, W / 2 + r, cy + r * 1.05], 40,
                                fill=(60, 18, 24, int(220 * q)))
            fq = _font(int(300 * (0.7 + 0.3 * q)), FONT_CANDIDATES)
            bb = d.textbbox((0, 0), "?", font=fq)
            d.text((W / 2 - (bb[0] + bb[2]) / 2, cy - (bb[1] + bb[3]) / 2), "?", font=fq,
                   fill=tuple(accent) + (int(255 * q),))
        b = _ease((t - 0.55) / 0.4)
        if b > 0 and parts[0]:
            ft = _fit(parts[0].upper(), W - 300, (70, 62, 54, 46, 40), FONT_CANDIDATES, d)
            ln = parts[0].upper()
            d.text(((W - d.textlength(ln, font=ft)) / 2 + 3, 860 + 4 + int((1 - b) * 30)), ln, font=ft, fill=(0, 0, 0, int(200 * b)))
            d.text(((W - d.textlength(ln, font=ft)) / 2, 860 + int((1 - b) * 30)), ln, font=ft, fill=(246, 244, 240, int(255 * b)))
    elif kind == "list":                                  # страхи: нумерованный список, пункты появляются по очереди
        items = [x.strip() for x in parts[0].split(";") if x.strip()][:5]
        f = _font(64, FONT_CANDIDATES)
        lh = 96
        y0 = H / 2 - len(items) * lh / 2
        x0 = 560
        for i, it in enumerate(items):
            b = _ease((t - 0.2 - i * 0.55) / 0.45)
            d.text((x0, y0 + i * lh), f"{i + 1}:", font=f, fill=th["text"] + (255,))
            if b > 0:
                d.text((x0 + 90, y0 + i * lh), it, font=f, fill=th["text"] + (int(255 * b),))
    elif kind == "news":
        p = parts + [""] * (3 - len(parts))
        b = _ease(t / 0.7)
        y0 = 340 + int((1 - b) * 120)
        card = Image.new("RGBA", (1180, 360), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle([0, 0, 1179, 359], 18, fill=(252, 252, 250, 255))
        cd.rectangle([0, 0, 1179, 70], fill=(240, 240, 238, 255))
        cd.text((34, 18), p[0].upper(), font=_font(34, BODY_FONTS), fill=(30, 40, 90, 255))
        if p[2]:
            fd = _font(30, TEXT_FONTS)
            cd.text((1150 - cd.textlength(p[2].upper(), font=fd), 22), p[2].upper(), font=fd, fill=(90, 90, 90, 255))
        fh = _font(54, BODY_FONTS)
        for i, ln in enumerate(_wrap(p[1], fh, 1100, cd)[:3]):
            cd.text((34, 100 + i * 70), ln, font=fh, fill=(20, 20, 20, 255))
        card = card.rotate(-1.6 * (1 - 0.3 * b), resample=Image.BICUBIC, expand=True)
        sh = Image.new("L", card.size, 0)
        sh.paste(card.split()[3].point(lambda v: int(v * th["shadow"] / 255)), (0, 0))
        sh = sh.filter(ImageFilter.GaussianBlur(24))
        x0 = (W - card.width) // 2
        im.paste((0, 0, 0), (x0, y0 + 20), sh)
        card.putalpha(card.split()[3].point(lambda v: int(v * b)))
        im.paste(card, (x0, y0), card)
    if theme == "void":                                   # мягкое свечение текста, как на тёмных каналах
        im = ImageChops.screen(im, im.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.8)))
    return im


def make_info_clip(kind_ru, text, length, out_mp4, theme="dark", accent=(200, 30, 30), fps=60):
    """Видео-карточка: появление с анимацией (~1 с), затем стоит с мягким приближением до конца фразы."""
    from .config import ffmpeg_exe
    kind = KINDS.get(str(kind_ru).lower(), "title")
    parts = [x.strip() for x in str(text).split("|")]
    _TL.serif = theme == "void"
    try:
        return _make_info_clip(kind, parts, text, length, out_mp4, theme, accent, fps)
    finally:
        _TL.serif = False


def _make_info_clip(kind, parts, text, length, out_mp4, theme, accent, fps):
    from .config import ffmpeg_exe
    base = _bg(theme, text)
    tmp = Path(tempfile.mkdtemp())
    length = max(1.6, float(length))
    anim = min({"timeline": 1.6, "versus": 1.6, "list": 3.2, "next": 1.3}.get(kind, ANIM), length - 0.3)
    step = 1 / 30                                         # 30 опорных кадров в секунду анимации, дальше — один кадр
    times = [i * step for i in range(int(anim / step) + 1)]
    if kind == "title":                                   # мигающий курсор — несколько кадров до конца
        times += [anim + i * 0.42 for i in range(1, int((length - anim) / 0.42))]
    frames = []
    for k, t in enumerate(times):
        p = tmp / f"{k:04d}.jpg"
        _frame(kind, parts, t, theme, accent, base).save(p, quality=92)
        frames.append((p, t))
    lst = tmp / "list.txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for k, (p, t) in enumerate(frames):
            nxt = frames[k + 1][1] if k + 1 < len(frames) else length
            fh.write(f"file '{p.as_posix()}'\nduration {max(0.034, nxt - t):.3f}\n")
        fh.write(f"file '{frames[-1][0].as_posix()}'\n")
    zf = max(1, int(length * fps))
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-vf", f"fps={fps},scale=3840:2160:flags=lanczos,"
                    f"zoompan=z='1+{0.012 if theme == 'void' else 0.035}*on/{zf}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps={fps},"
                    "format=yuv420p", "-t", f"{length:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    str(out_mp4)], check=True, timeout=900, creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        for p, _ in frames:
            p.unlink()
        lst.unlink()
        tmp.rmdir()
    except Exception:
        pass
    return out_mp4
