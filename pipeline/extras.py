"""Субтитры, описание с таймкодами, обложка, карточки глав."""
import math
import os
import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
TEXT_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\georgiab.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size, candidates=FONT_CANDIDATES):
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def fmt_srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def fmt_ts(t):
    t = int(t)
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def _sub_pieces(text, max_len=84):
    """Режет текст на фразы для субтитров (не длиннее max_len символов)."""
    pieces = []
    for sent in re.split(r"(?<=[.!?…])\s+", text.strip()):
        while len(sent) > max_len:
            cut = sent.rfind(",", 0, max_len)
            if cut < max_len // 3:
                cut = sent.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            pieces.append(sent[:cut + 1].strip())
            sent = sent[cut + 1:].strip()
        if sent:
            pieces.append(sent)
    return pieces


def write_srt(timeline, path):
    """timeline: список сегментов {text, start, end}."""
    n = 1
    out = []
    for seg in timeline:
        pieces = _sub_pieces(seg["text"])
        total = sum(len(p) for p in pieces) or 1
        t = seg["start"]
        dur = seg["end"] - seg["start"]
        for p in pieces:
            d = dur * len(p) / total
            lines = "\n".join(textwrap.wrap(p, 42)[:2])
            out.append(f"{n}\n{fmt_srt_time(t)} --> {fmt_srt_time(t + d)}\n{lines}\n")
            n += 1
            t += d
    Path(path).write_text("\n".join(out), encoding="utf-8")


def write_description(script, chapter_starts, credits, path, timecodes="Таймкоды:", sources="Использованные материалы:"):
    lines = [script.get("description", "").strip(), "", timecodes]
    for title, start in chapter_starts:
        lines.append(f"{fmt_ts(start)} {title}")
    if credits:
        lines += ["", sources, credits]
    tags = script.get("tags", [])
    text = "\n".join(lines)
    extra = ["", "=" * 40,
             "НАЗВАНИЕ: " + script.get("title", ""),
             "ДРУГИЕ ВАРИАНТЫ НАЗВАНИЯ:", *["  - " + t for t in script.get("title_options", [])],
             "ТЕГИ: " + ", ".join(tags)]
    Path(path).write_text(text + "\n" + "\n".join(extra), encoding="utf-8")


def write_facts(script, path):
    facts = script.get("facts_to_verify", [])
    body = ["ПРОВЕРЬ ЭТИ ФАКТЫ ПЕРЕД ПУБЛИКАЦИЕЙ.",
            "Нейросеть может ошибаться в датах, именах и числах. Исправления вносите в script.txt и пересобирайте видео.", ""]
    body += [f"{i + 1}. {f}" for i, f in enumerate(facts)] or ["(список не сформирован)"]
    Path(path).write_text("\n".join(body), encoding="utf-8")


def _cover(img, w, h):
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((int(iw * scale) + 1, int(ih * scale) + 1), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def make_chapter_card(title, number, bg_image, out_path, accent=(200, 30, 30), word="ГЛАВА"):
    W, H = 1920, 1080
    if bg_image and os.path.exists(bg_image):
        try:
            bg = _cover(Image.open(bg_image).convert("RGB"), W, H)
            bg = bg.filter(ImageFilter.GaussianBlur(18))
            bg = ImageEnhance.Brightness(bg).enhance(0.3)
        except Exception:
            bg = Image.new("RGB", (W, H), (12, 12, 14))
    else:
        bg = Image.new("RGB", (W, H), (12, 12, 14))
    d = ImageDraw.Draw(bg)
    small = _font(46, TEXT_FONT_CANDIDATES)
    big = _font(96, TEXT_FONT_CANDIDATES)
    label = f"{word} {number}"
    lines = textwrap.wrap(title.upper(), 28)[:3]
    top = (H - (110 + len(lines) * 112)) / 2
    lw = d.textlength(label, font=small)
    d.text(((W - lw) / 2, top), label, font=small, fill=accent)
    d.rectangle([W / 2 - 60, top + 70, W / 2 + 60, top + 76], fill=accent)
    y = top + 110
    for ln in lines:
        tw = d.textlength(ln, font=big)
        d.text(((W - tw) / 2, y), ln, font=big, fill=(240, 240, 240))
        y += 112
    bg.save(out_path, quality=95)


def make_thumbnail(text, bg_image, out_path, accent=(230, 30, 30)):
    W, H = 1280, 720
    if bg_image and os.path.exists(bg_image):
        bg = _cover(Image.open(bg_image).convert("RGB"), W, H)
        bg = ImageEnhance.Contrast(bg).enhance(1.25)
        bg = ImageEnhance.Brightness(bg).enhance(0.8)
    else:
        bg = Image.new("RGB", (W, H), (20, 20, 20))
    # затемнение слева для читаемости текста
    grad = Image.new("L", (W, 1))
    for x in range(W):
        grad.putpixel((x, 0), int(210 * max(0, 1 - x / (W * 0.75))))
    grad = grad.resize((W, H))
    bg = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), bg, grad)
    d = ImageDraw.Draw(bg)
    words = (text or "").upper().split()
    lines = textwrap.wrap(" ".join(words), 12)[:3] or [""]
    size = 130 if len(lines) <= 2 else 110
    font = _font(size)
    y = (H - len(lines) * (size + 10)) / 2
    for i, ln in enumerate(lines):
        color = accent if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
        d.text((60, y), ln, font=font, fill=color, stroke_width=6, stroke_fill=(0, 0, 0))
        y += size + 10
    bg.save(out_path, quality=95)


# Современные тонкие шрифты Windows 10/11 (Segoe UI Light / Semilight), запасные — Arial и DejaVu
THIN_FONTS = [
    r"C:\Windows\Fonts\segoeuil.ttf",
    r"C:\Windows\Fonts\segoeuisl.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf",
]
SEMI_FONTS = [
    r"C:\Windows\Fonts\segoeuisl.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
SMALL_CAPS_FONTS = [
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
# совместимость со старым кодом
LABEL_MAIN_FONTS = THIN_FONTS
LABEL_SUB_FONTS = SEMI_FONTS
TITLE_FONTS = THIN_FONTS


def _tracked_width(d, text, font, tracking):
    return sum(d.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)


def _draw_tracked(d, xy, text, font, fill, tracking):
    """Текст с разрядкой (расстоянием между буквами) — так выглядят современные титры."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking


def _soft_shadow(img, radius=10, alpha=150):
    """Мягкая тень под текстом (а не плашка) — читается на любом фоне."""
    a = img.split()[3].filter(ImageFilter.GaussianBlur(radius))
    a = a.point(lambda v: min(255, int(v * alpha / 255 * 2.2)))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow.putalpha(a)
    return Image.alpha_composite(shadow, img)


def make_label(text, out_path, accent=(200, 30, 30)):
    """Подпись внизу слева: тонкий шрифт, тонкая акцентная линия, мягкая тень. Прозрачный PNG 1920x1080.
    text: «Главное | пояснение» (пояснение необязательно)."""
    W, H = 1920, 1080
    main, _, sub = (text or "").partition("|")
    main, sub = main.strip(), sub.strip()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f_main = _font(58, THIN_FONTS)
    f_sub = _font(30, SEMI_FONTS)
    x0, bottom = 120, H - 140
    block_h = 72 + (46 if sub else 0)
    top = bottom - block_h
    d.rectangle([x0 - 26, top + 8, x0 - 23, bottom - 4], fill=tuple(accent) + (255,))
    d.text((x0, top), main, font=f_main, fill=(250, 250, 250, 255))
    if sub:
        _draw_tracked(d, (x0 + 2, top + 76), sub.upper(), f_sub, (215, 215, 215, 255), 3)
    _soft_shadow(img, 14, 140).save(out_path)


def make_title_png(title, eyebrow, out_path, accent=(200, 30, 30)):
    """Название ролика: тонкий шрифт с разрядкой, маленькая подпись ниши сверху. Прозрачный PNG 1920x1080."""
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    words = (title or "").upper()
    size = 92
    while True:
        font = _font(size, THIN_FONTS)
        tracking = max(2, size // 12)
        lines = textwrap.wrap(words, 26)[:3] or [""]
        if all(_tracked_width(d, ln, font, tracking) < 1680 for ln in lines) or size <= 52:
            break
        size -= 4
    line_h = int(size * 1.28)
    top = (H - len(lines) * line_h) / 2 + 10
    small = _font(26, SMALL_CAPS_FONTS)
    eb = (eyebrow or "").upper()
    if eb:
        ew = _tracked_width(d, eb, small, 9)
        _draw_tracked(d, ((W - ew) / 2, top - 70), eb, small, tuple(accent) + (255,), 9)
    y = top
    for ln in lines:
        tw = _tracked_width(d, ln, font, tracking)
        _draw_tracked(d, ((W - tw) / 2, y), ln, font, (248, 248, 248, 255), tracking)
        y += line_h
    d.rectangle([W / 2 - 40, y + 26, W / 2 + 40, y + 28], fill=tuple(accent) + (255,))
    _soft_shadow(img, 16, 150).save(out_path)


def make_chapter_png(title, number, out_path, accent=(200, 30, 30), word="ГЛАВА"):
    """Название главы поверх видео: «ГЛАВА 2» мелко с разрядкой, название тонким шрифтом. Прозрачный PNG."""
    W, H = 1920, 1080
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    small = _font(26, SMALL_CAPS_FONTS)
    big = _font(80, THIN_FONTS)
    label = f"{word} {number}".upper()
    lines = textwrap.wrap((title or "").upper(), 30)[:2] or [""]
    top = (H - (70 + len(lines) * 100)) / 2
    lw = _tracked_width(d, label, small, 10)
    _draw_tracked(d, ((W - lw) / 2, top), label, small, tuple(accent) + (255,), 10)
    y = top + 62
    for ln in lines:
        tw = _tracked_width(d, ln, big, 6)
        _draw_tracked(d, ((W - tw) / 2, y), ln, big, (248, 248, 248, 255), 6)
        y += 100
    _soft_shadow(img, 16, 150).save(out_path)


def make_person_card(name, out_path, accent=(200, 30, 30), sub="", variant=0):
    """Карточка человека, если его фото нет в свободных источниках: монограмма с инициалами,
    имя крупно, тёмный фон с мягкой виньеткой. JPG 1920x1080 (по ней идёт плавное приближение)."""
    import random
    W, H = 1920, 1080
    img = Image.new("RGB", (W, H), (10, 13, 20))
    px = img.load()
    rnd = random.Random(name)
    for y in range(0, H, 2):                       # градиент + лёгкое зерно
        for x in range(0, W, 2):
            dx, dy = (x - W / 2) / W, (y - H / 2) / H
            v = max(0.0, 1 - (dx * dx + dy * dy) * 2.2)
            g = int(10 + 26 * v + rnd.randint(-3, 3))
            c = (g, g + 3, g + 9)
            px[x, y] = c
            if x + 1 < W:
                px[x + 1, y] = c
            if y + 1 < H:
                px[x, y + 1] = c
                if x + 1 < W:
                    px[x + 1, y + 1] = c
    d = ImageDraw.Draw(img)
    words = [w for w in re.split(r"[\s.]+", name.strip()) if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    if variant % 2 == 1:                           # второй вариант: монограмма слева, имя справа
        r = 150
        cx, cy = 560, H // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=tuple(accent), width=4)
        d.ellipse([cx - r + 14, cy - r + 14, cx + r - 14, cy + r - 14], outline=(70, 76, 92), width=2)
        f_ini = _font(150, THIN_FONTS)
        tw = d.textlength(initials, font=f_ini)
        d.text((cx - tw / 2, cy - 98), initials, font=f_ini, fill=(235, 235, 240))
        x0 = cx + r + 90
        d.rectangle([x0, cy - 70, x0 + 4, cy + 70], fill=tuple(accent))
        _draw_tracked(d, (x0 + 40, cy - 70), name.strip().upper(), _font(78, THIN_FONTS), (245, 245, 248), 5)
        if sub:
            _draw_tracked(d, (x0 + 42, cy + 30), sub.upper(), _font(32, SEMI_FONTS), (190, 196, 210), 4)
        img.save(out_path, quality=92)
        return
    cx, cy, r = W // 2, 400, 150
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=tuple(accent), width=4)
    d.ellipse([cx - r + 14, cy - r + 14, cx + r - 14, cy + r - 14], outline=(70, 76, 92), width=2)
    f_ini = _font(150, THIN_FONTS)
    tw = d.textlength(initials, font=f_ini)
    d.text((cx - tw / 2, cy - 98), initials, font=f_ini, fill=(235, 235, 240))
    f_name = _font(84, THIN_FONTS)
    nm = name.strip().upper()
    tracking = 6
    total = sum(d.textlength(ch, font=f_name) + tracking for ch in nm) - tracking
    _draw_tracked(d, (cx - total / 2, 610), nm, f_name, (245, 245, 248), tracking)
    if sub:
        f_sub = _font(34, SEMI_FONTS)
        st = sub.upper()
        total = sum(d.textlength(ch, font=f_sub) + 4 for ch in st) - 4
        _draw_tracked(d, (cx - total / 2, 725), st, f_sub, (190, 196, 210), 4)
    d.rectangle([cx - 60, 710 if not sub else 790, cx + 60, 713 if not sub else 793], fill=tuple(accent))
    img.save(out_path, quality=92)


TYPE_FONTS = [
    r"C:\Windows\Fonts\cour.ttf",                     # Courier New — пишущая машинка, есть кириллица
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]
TYPE_BOLD = [r"C:\Windows\Fonts\courbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]


def _paper(W, H, seed):
    """Старый лист бумаги на тёмном столе: желтоватый, с зерном, пятнами и потемневшими краями."""
    import random
    import numpy as np
    rnd = random.Random(seed)
    rs = np.random.RandomState(abs(hash(seed)) % (2 ** 31))
    desk = np.zeros((H, W, 3), np.float32)
    desk[:] = (28, 22, 18)
    desk += rs.normal(0, 6, (H, W, 1))
    pw, ph = int(W * 0.62), int(H * 0.86)
    px, py = (W - pw) // 2, (H - ph) // 2
    paper = np.zeros((ph, pw, 3), np.float32)
    paper[:] = (222, 208, 176)
    paper += rs.normal(0, 7, (ph, pw, 1))
    yy, xx = np.mgrid[0:ph, 0:pw]
    edge = np.minimum.reduce([xx, yy, pw - 1 - xx, ph - 1 - yy]).astype(np.float32)
    paper *= (0.78 + 0.22 * np.clip(edge / 90.0, 0, 1))[..., None]
    for _ in range(3):                                  # пятна
        cx, cy, r = rnd.randint(0, pw), rnd.randint(0, ph), rnd.randint(60, 180)
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        paper *= (1 - 0.08 * np.clip(1 - d / r, 0, 1))[..., None]
    fold = int(ph * rnd.uniform(0.45, 0.55))           # сгиб листа
    paper[fold - 2:fold + 2] *= 0.86
    desk[py:py + ph, px:px + pw] = paper
    img = Image.fromarray(np.clip(desk, 0, 255).astype("uint8"))
    return img, (px, py, pw, ph)


SERIF_BOLD = [r"C:\Windows\Fonts\georgiab.ttf", r"C:\Windows\Fonts\timesbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]


def _wax_seal(img, cx, cy, r, seed=""):
    """Сургучная печать с буквой «U» — знак канала. Неровный край, блик, лёгкий наклон."""
    import math
    import random as _r
    rnd = _r.Random(str(seed) + "seal")
    size = int(r * 2.6)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    pts = []
    for k in range(48):
        a = 2 * math.pi * k / 48
        rr = r * (1.0 + 0.07 * math.sin(a * 7 + rnd.random()) + rnd.uniform(-0.03, 0.05))
        pts.append((c + rr * math.cos(a), c + rr * math.sin(a)))
    d.polygon([(x + 3, y + 4) for x, y in pts], fill=(30, 8, 6, 90))          # тень
    d.polygon(pts, fill=(132, 22, 26, 255))
    d.ellipse([c - r * 0.74, c - r * 0.74, c + r * 0.74, c + r * 0.74], outline=(92, 12, 16, 255), width=4)
    d.ellipse([c - r * 0.66, c - r * 0.66, c + r * 0.66, c + r * 0.66], fill=(150, 30, 32, 255))
    f = _font(int(r * 0.95), SERIF_BOLD)
    tw = d.textlength("U", font=f)
    d.text((c - tw / 2 + 2, c - r * 0.58 + 2), "U", font=f, fill=(80, 10, 12, 255))   # тиснение
    d.text((c - tw / 2, c - r * 0.58), "U", font=f, fill=(196, 70, 60, 255))
    d.arc([c - r * 0.9, c - r * 0.9, c + r * 0.9, c + r * 0.9], 200, 250, fill=(220, 120, 110, 200), width=3)  # блик
    layer = layer.rotate(rnd.uniform(-14, 14), resample=Image.BICUBIC)
    img.paste(layer, (int(cx - size / 2), int(cy - size / 2)), layer)


def make_document_clip(text, head, reveal, length, out_mp4, seed=""):
    """Видео: старый документ, на котором текст печатается по буквам ровно под голос диктора.
    reveal — время (с) появления каждого слова от начала кадра."""
    import subprocess
    import tempfile
    from .config import ffmpeg_exe
    W, H = 1920, 1080
    base, (px, py, pw, ph) = _paper(W, H, seed or text)
    _wax_seal(base, px + pw - 112, py + ph - 108, 50, seed or text)   # печать канала «U» в углу листа
    words = text.split()
    margin = 90
    d0 = ImageDraw.Draw(base)
    inner = pw - 2 * margin
    # шапка: всегда внутри листа — уменьшаем шрифт, а если и так не влезает, переносим на 2 строки
    head_lines, fh = [], None
    if head:
        H_ = head.upper()
        for size in (34, 30, 27, 24):
            fh = _font(size, TYPE_BOLD)
            if d0.textlength(H_, font=fh) <= inner:
                head_lines = [H_]
                break
        if not head_lines:
            fh = _font(26, TYPE_BOLD)
            cur_ = ""
            for w in H_.split():
                t_ = (cur_ + " " + w).strip()
                if cur_ and d0.textlength(t_, font=fh) > inner:
                    head_lines.append(cur_)
                    cur_ = w
                else:
                    cur_ = t_
            head_lines.append(cur_)
            if len(head_lines) > 2:
                head_lines = [head_lines[0], " ".join(head_lines[1:])]
            last = head_lines[-1]
            while d0.textlength(last, font=fh) > inner and len(last) > 4:
                last = last[:-2].rstrip(" ,.;:") + "…"
            head_lines[-1] = last
    head_h = (len(head_lines) * 40 + 50) if head_lines else 0
    # текст: если строк больше, чем помещается на листе, — шрифт меньше
    for size, lh in ((52, 74), (46, 66), (40, 58), (35, 50), (30, 44)):
        f = _font(size, TYPE_FONTS)
        lines, cur = [], []
        for i, w in enumerate(words):
            test = " ".join(x for _, x in cur + [(i, w)])
            if cur and d0.textlength(test, font=f) > inner:
                lines.append(cur)
                cur = []
            cur.append((i, w))
        if cur:
            lines.append(cur)
        if len(lines) * lh + head_h <= ph - 120 and all(d0.textlength(w, font=f) <= inner for w in words):
            break
    top = py + (ph - len(lines) * lh - head_h) // 2 + head_h
    pos = {}
    for li, ln in enumerate(lines):
        x = px + margin
        for i, w in ln:
            pos[i] = (x, top + li * lh)
            x += d0.textlength(w + " ", font=f)
    if head_lines:
        y0 = top - head_h
        for k_, hl in enumerate(head_lines):
            d0.text((px + margin, y0 + k_ * 40), hl, font=fh, fill=(120, 40, 30))
        ly = y0 + len(head_lines) * 40 + 6
        d0.line([px + margin, ly, px + pw - margin, ly], fill=(120, 40, 30), width=2)
    tmp = Path(tempfile.mkdtemp())
    reveal = list(reveal) + [length] * (len(words) - len(reveal))
    # печать ПО БУКВАМ: буквы слова появляются в темпе речи — от момента, когда диктор начал слово,
    # до начала следующего слова (но не медленнее 0,11 с и не быстрее 0,035 с на букву)
    events = []                                           # (время, номер слова, номер буквы)
    for i, w in enumerate(words):
        t0 = max(0.0, min(length - 0.05, reveal[i]))
        nxt = reveal[i + 1] if i + 1 < len(words) else t0 + 0.08 * len(w) + 0.2
        span = max(0.05, min(nxt, length - 0.05) - t0)
        per = max(0.035, min(0.11, span / (len(w) + 0.5)))
        for c in range(len(w)):
            events.append((min(length - 0.05, t0 + c * per), i, c))
    events.sort()
    im = base.copy()
    d = ImageDraw.Draw(im)
    frames = [(tmp / "0000.jpg", 0.0)]
    im.save(frames[0][0], quality=93)
    k = 0
    while k < len(events):
        # все буквы, у которых время совпадает (в пределах кадра 1/30 с), рисуем в один кадр
        group = [e for e in events[k:] if e[0] - events[k][0] < 1 / 30]
        for _, i, c in group:
            x, y = pos[i]
            w = words[i]
            d.text((x + d0.textlength(w[:c], font=f), y), w[c], font=f, fill=(38, 30, 24))
        k += len(group)
        p_ = tmp / f"{k:04d}.jpg"
        im.save(p_, quality=93)
        frames.append((p_, events[k - 1][0]))
    lst = tmp / "list.txt"
    with open(lst, "w", encoding="utf-8") as fh_:
        for k, (p, t) in enumerate(frames):
            nxt = frames[k + 1][1] if k + 1 < len(frames) else length
            fh_.write(f"file '{p.as_posix()}'\nduration {max(0.034, nxt - t):.3f}\n")
        fh_.write(f"file '{frames[-1][0].as_posix()}'\n")
    # плавное медленное приближение без дрожи: сначала увеличиваем кадр в 2 раза (тогда сдвиги
    # zoompan меньше пикселя и не «прыгают»), приближение очень мягкое — до 3% за весь кадр
    zf = max(1, int(length * 30))
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-vf", f"fps=30,scale=3840:2160:flags=lanczos,"
                    f"zoompan=z='1+0.03*on/{zf}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,"
                    "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", str(out_mp4)], check=True, timeout=900,
                   creationflags=0x08000000 if os.name == "nt" else 0)
    return out_mp4


def place_labels(d, names, P, font, w, h, line_h=48):
    """Где писать названия мест: рядом с точкой (сверху, снизу, сбоку), не на пунктире, не на других
    подписях и точках. Повторное место (возвращение) — без второй подписи (None)."""
    taken = [(x - 18, y - 18, x + 18, y + 18) for x, y in P]
    path_pts = []
    for i in range(1, len(P)):
        (xa, ya), (xb, yb) = P[i - 1], P[i]
        L_ = max(1.0, math.hypot(xb - xa, yb - ya))
        for q in range(int(L_ // 8) + 1):
            path_pts.append((xa + (xb - xa) * q * 8 / L_, ya + (yb - ya) * q * 8 / L_))

    def hits(r_):
        return sum(1 for px, py in path_pts if r_[0] - 10 <= px <= r_[2] + 10 and r_[1] - 10 <= py <= r_[3] + 10)

    out = []
    for k_, (name, (x, y)) in enumerate(zip(names, P)):
        if name in names[:k_]:
            out.append(None)
            continue
        tw = d.textlength(name, font=font)
        up, dn = line_h + 32, 28
        cands = [(x - tw / 2, y - up), (x - tw / 2, y + dn), (x + 30, y - line_h / 2), (x - 30 - tw, y - line_h / 2),
                 (x + 30, y - up + 10), (x - 30 - tw, y - up + 10), (x + 30, y + dn - 10), (x - 30 - tw, y + dn - 10),
                 (x - tw / 2, y - up - 40), (x - tw / 2, y + dn + 40), (x + 40, y - up - 30), (x - 40 - tw, y + dn + 30)]
        best, best_score = None, None
        for cx_, cy_ in cands:
            r_ = (cx_ - 6, cy_ - 4, cx_ + tw + 6, cy_ + line_h)
            if r_[0] < w * 0.04 or r_[2] > w * 0.96 or r_[1] < h * 0.04 or r_[3] > h * 0.96:
                continue
            over = sum(1 for a in taken if not (r_[2] < a[0] or r_[0] > a[2] or r_[3] < a[1] or r_[1] > a[3]))
            score = over * 1000 + hits(r_)
            if best_score is None or score < best_score:
                best, best_score = (cx_, cy_), score
            if score == 0:
                break
        best = best or cands[0]
        taken.append((best[0] - 6, best[1] - 4, best[0] + tw + 6, best[1] + line_h))
        out.append(best)
    return out


def make_map_clip(title, points, times, length, out_mp4, seed=""):
    """Старая карта: точки появляются, когда диктор называет место, а пунктир маршрута
    «прокладывается» к каждой новой точке ровно к моменту, когда звучит её название.
    points: [(название, широта, долгота)], times: [секунда появления каждой точки]."""
    import math
    import subprocess
    import tempfile
    from .config import ffmpeg_exe
    W, H = 1920, 1080
    base, (px, py, pw, ph) = _paper(W, H, seed or title or "map")
    d0 = ImageDraw.Draw(base)
    ink, red = (70, 52, 36), (150, 28, 22)
    fh = _font(34, TYPE_BOLD)
    fl = _font(36, TYPE_FONTS)
    fs = _font(22, TYPE_FONTS)
    m = 110
    ax0, ay0, ax1, ay1 = px + m, py + m + 40, px + pw - m, py + ph - m + 10
    lats = [p[1] for p in points]
    lons = [p[2] for p in points]
    la0, la1 = min(lats), max(lats)
    lo0, lo1 = min(lons), max(lons)
    k = math.cos(math.radians((la0 + la1) / 2))
    span_x = max(0.5, (lo1 - lo0) * k)
    span_y = max(0.5, la1 - la0)
    sc = min((ax1 - ax0) / span_x, (ay1 - ay0) / span_y) * 0.92
    cx, cy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
    mx, my = (lo0 + lo1) / 2, (la0 + la1) / 2

    def xy(lat, lon):
        return cx + (lon - mx) * k * sc, cy - (lat - my) * sc
    # сетка градусов (тонко, как на старых картах)
    for lat in range(int(math.floor(la0 - 2)), int(math.ceil(la1 + 2)) + 1):
        _, y = xy(lat, mx)
        if ay0 - 30 < y < ay1 + 30:
            d0.line([(px + 40, y), (px + pw - 40, y)], fill=(170, 150, 118), width=1)
            d0.text((px + 46, y - 26), f"{lat}°", font=fs, fill=(150, 128, 98))
    for lon in range(int(math.floor(lo0 - 4)), int(math.ceil(lo1 + 4)) + 1):
        x, _ = xy(my, lon)
        if px + 40 < x < px + pw - 40:
            d0.line([(x, py + 90), (x, py + ph - 40)], fill=(170, 150, 118), width=1)
    if title:
        d0.text((px + 60, py + 34), title.upper(), font=fh, fill=(120, 40, 30))
        d0.line([px + 60, py + 82, px + pw - 60, py + 82], fill=(120, 40, 30), width=2)
    # роза ветров
    rx, ry = px + pw - 120, py + ph - 130
    d0.polygon([(rx, ry - 52), (rx - 12, ry), (rx + 12, ry)], fill=ink)
    d0.polygon([(rx, ry + 52), (rx - 12, ry), (rx + 12, ry)], outline=ink)
    d0.text((rx - 9, ry - 92), "N", font=fl, fill=ink)
    P = [xy(p[1], p[2]) for p in points]
    times = list(times) + [length - 1.0] * (len(points) - len(times))
    seg_d = 1.1                                           # пунктир доходит до точки за 1,1 с
    tmp = Path(tempfile.mkdtemp())
    fps = 30
    n = max(1, int(length * fps))

    def dashed(d, a, b, frac):
        (x0, y0), (x1, y1) = a, b
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 1:
            return
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        s, dash, gap = 0.0, 18.0, 12.0
        end = L * frac
        while s < end:
            e = min(s + dash, end)
            d.line([(x0 + ux * s, y0 + uy * s), (x0 + ux * e, y0 + uy * e)], fill=red, width=6)
            s += dash + gap

    lab = place_labels(ImageDraw.Draw(base), [p[0] for p in points], P, fl, base.width, base.height,
                       line_h=int(fl.size * 1.25) if hasattr(fl, "size") else 40)
    lst = tmp / "list.txt"
    prev_state = None
    frames = []
    for f in range(n):
        t = f / fps
        state = []
        for i in range(len(points)):
            if i == 0:
                state.append(1.0 if t >= times[0] else 0.0)
            else:
                st = times[i] - seg_d
                state.append(max(0.0, min(1.0, (t - st) / seg_d)) if t >= st else 0.0)
        state = [round(x, 3) for x in state]            # одинаково и для сравнения, и для рисования
        key = tuple(state)
        if key == prev_state and frames:
            frames[-1][1] += 1
            continue
        prev_state = key
        im = base.copy()
        d = ImageDraw.Draw(im)
        for i in range(1, len(points)):
            if state[i] > 0:
                dashed(d, P[i - 1], P[i], state[i])
        for i, (name, _, _) in enumerate(points):
            if state[i] >= 1.0 or (i == 0 and state[0] > 0):
                x, y = P[i]
                d.ellipse([x - 13, y - 13, x + 13, y + 13], outline=red, width=4)
                d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=red)
                if lab[i] is not None:
                    d.text(lab[i], name, font=fl, fill=ink)
        p_ = tmp / f"{len(frames):05d}.jpg"
        im.save(p_, quality=92)
        frames.append([p_, 1])
    with open(lst, "w", encoding="utf-8") as fh_:
        for p_, cnt in frames:
            fh_.write(f"file '{p_.as_posix()}'\nduration {cnt / fps:.4f}\n")
        fh_.write(f"file '{frames[-1][0].as_posix()}'\n")
    zf = max(1, n)
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-vf", f"fps=30,scale=3840:2160:flags=lanczos,"
                    f"zoompan=z='1+0.025*on/{zf}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,"
                    "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out_mp4)],
                   check=True, timeout=1200, creationflags=0x08000000 if os.name == "nt" else 0)
    return out_mp4
