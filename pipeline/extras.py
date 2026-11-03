"""Субтитры, описание с таймкодами, обложка, карточки глав."""
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
