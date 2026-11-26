"""Свой вид монтажа у каждой ниши (кроме тайн — у них прежний документальный вид).

стройка («studio»): светлый серебристый студийный фон, кадр парит на нём карточкой со скруглёнными углами
    и мягкой тенью, въезжает сбоку; люди — карточка слева, имя крупным жирным шрифтом справа;
    заставка главы — «KAPITEL 2» и название, которое печатается с курсором.
еда («polaroid»): кадр — наклонный снимок в белой рамке поверх размытой тёмной копии самого себя,
    падает на место; люди — снимок слева, имя на жёлтой плашке справа;
    заставка главы — красная косая панель «#2», кремовая половина с названием, жёлто-чёрная лента.
страхи («dark»): чёрный фон, небольшой кадр по центру в тонкой светящейся рамке (сиреневое свечение),
    иногда — «красный экран» с виньеткой; белые схемы на белом фоне становятся светлым контуром на чёрном;
    люди — портрет в овале, слева имя и годы шрифтом с засечками; заставка главы — белый текст со свечением.
Подписи источника фото — мелко внизу справа (как у познавательных каналов)."""
import random
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .config import ffmpeg_exe
from .extras import _font

W, H, FPS = 1920, 1080, 60
HEAD_FONTS = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\bahnschrift.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
BOLD_FONTS = [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
TEXT_FONTS = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
SERIF_FONTS = [r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\georgia.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
YELLOW, CREAM, RED = (250, 204, 21), (244, 236, 220), (214, 40, 40)
GLOW = (176, 168, 255)                       # сиреневое свечение рамки (страхи)
# страхи: лёгкое медленное мерцание яркости (как у старого экрана, без резких вспышек) и мягкая виньетка.
# Выключается настройкой ниши dark_fx: false (runner ставит DARK_FX = "").
DARK_FX = (",eq=brightness='0.018*sin(2*PI*t*0.9)+0.008*sin(2*PI*t*3.7)':eval=frame"
           ",vignette=angle=PI/5")
_NO_WINDOW = 0x08000000 if __import__("os").name == "nt" else 0


def _ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _fit(text, max_w, sizes, fonts):
    d = ImageDraw.Draw(Image.new("L", (4, 4)))
    for s in sizes:
        f = _font(s, fonts)
        if d.textlength(text, font=f) <= max_w:
            return f
    return _font(sizes[-1], fonts)


def _wrap(text, f, max_w, max_lines=3):
    d = ImageDraw.Draw(Image.new("L", (4, 4)))
    lines, cur = [], ""
    for w in (text or "").split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines]


# ---------------- фоны ----------------
@lru_cache(maxsize=2)
def studio_bg():
    """Серебристая студия: светлое пятно в центре, к краям мягко темнее."""
    img = Image.new("RGB", (W, H), (150, 154, 160))
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    cx, cy = W // 2, int(H * 0.45)
    for r in range(1100, 0, -25):
        gd.ellipse([cx - r * 1.35, cy - r, cx + r * 1.35, cy + r], fill=int(255 * (1 - r / 1100) ** 1.3))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    return Image.composite(Image.new("RGB", (W, H), (242, 243, 245)), img, glow)


def _round_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=255)
    return m


def _shadow(w, h, r, blur, alpha, angle=0.0):
    pad = blur * 3
    sh = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad, pad + w, pad + h], r, fill=(0, 0, 0, alpha))
    if angle:
        sh = sh.rotate(-angle, resample=Image.BICUBIC, expand=True)   # ffmpeg rotate: «+» — по часовой
    return sh.filter(ImageFilter.GaussianBlur(blur))


# ---------------- геометрия кадра ----------------
def geometry(look, seed, side=False):
    rnd = random.Random(str(seed))
    if look == "studio":
        if side:
            cw, ch = 760, 820
            return {"cw": cw, "ch": ch, "x": 150, "y": (H - ch) // 2, "dx": -1100, "dy": 0, "r": 30, "angle": 0.0}
        cw, ch = 1280, 720
        off = rnd.choice((-110, 0, 110))
        dx = 1500 if rnd.random() < 0.5 else -1500
        return {"cw": cw, "ch": ch, "x": (W - cw) // 2 + off, "y": (H - ch) // 2 + 30, "dx": dx, "dy": 0,
                "r": 30, "angle": 0.0}
    if look == "dark":
        if side:                                         # портрет в овале справа от центра, имя слева
            return {"cw": 480, "ch": 600, "x": 1000, "y": (H - 600) // 2 - 10, "var": "oval"}
        var = "glow" if rnd.random() < 0.7 else "red"     # как в примерах: сиреневое свечение или красный экран
        cw, ch = (1000, 564) if var == "red" else (1240, 698)
        return {"cw": cw, "ch": ch, "x": (W - cw) // 2, "y": (H - ch) // 2 - 10, "var": var}
    # polaroid
    ang = rnd.uniform(1.6, 4.2) * rnd.choice((-1, 1))
    if side:
        cw, ch = 660, 760
        return {"cw": cw, "ch": ch, "x": 170, "y": (H - ch) // 2 - 10, "dx": 0, "dy": 90, "b": 20, "bb": 20,
                "angle": ang}
    cw, ch = 1120, 630
    return {"cw": cw, "ch": ch, "x": (W - cw) // 2 + rnd.randint(-40, 40), "y": (H - ch) // 2 - 20 + rnd.randint(-15, 15),
            "dx": 0, "dy": 90, "b": 20, "bb": 20, "angle": ang}


def _credit(d, credit, look):
    if not credit:
        return
    f = _font(20, TEXT_FONTS)
    t = credit[:110]
    tw = d.textlength(t, font=f)
    col = (70, 74, 80, 200) if look == "studio" else (235, 235, 235, 170)
    d.text((W - 36 - tw, H - 40), t, font=f, fill=col)


def deco_png(look, out, caption="", credit="", side=False):
    """Всё, что стоит поверх кадра: имя человека справа (side) и подпись источника. Прозрачный PNG."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    main, _, sub = (caption or "").partition("|")
    main, sub = main.strip(), sub.strip()
    if side and main and look == "dark":                 # имя и годы слева от овала, шрифт с засечками
        f = _fit(main, 820, (64, 56, 50, 44), SERIF_FONTS)
        fs = _font(34, SERIF_FONTS)
        tw = d.textlength(main, font=f)
        x1 = 960
        y = H // 2 - 50
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((x1 - tw, y), main, font=f, fill=GLOW + (255,))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(10)))
        d = ImageDraw.Draw(img)
        d.text((x1 - tw, y), main, font=f, fill=(244, 244, 250, 255))
        if sub:
            d.text((x1 - tw + 4, y + f.size + 8), sub, font=fs, fill=(180, 180, 196, 255))
        main = ""
    if side and main:
        x0, max_w = 1020, 800
        if look == "studio":
            f = _fit(main.upper(), max_w, (110, 96, 84, 72, 62), HEAD_FONTS)
            lines = _wrap(main.upper(), f, max_w, 2)
            y = H // 2 - (len(lines) * int(f.size * 1.05) + (70 if sub else 0)) // 2
            for ln in lines:
                d.text((x0, y), ln, font=f, fill=(22, 24, 28, 255))
                y += int(f.size * 1.05)
            if sub:
                fs = _font(38, BOLD_FONTS)
                for ln in _wrap(sub, fs, max_w, 2):
                    d.text((x0 + 4, y + 16), ln, font=fs, fill=(90, 96, 104, 255))
                    y += 48
        else:
            f = _fit(main.upper(), max_w, (84, 74, 64, 56, 48), HEAD_FONTS)
            lines = _wrap(main.upper(), f, max_w, 2)
            y = H // 2 - 90
            for ln in lines:
                d.text((x0 + 3, y + 4), ln, font=f, fill=(0, 0, 0, 160))
                d.text((x0, y), ln, font=f, fill=(250, 250, 248, 255))
                y += int(f.size * 1.08)
            if sub:
                _pill(d, x0, y + 18, sub.upper(), YELLOW, (20, 20, 20), 34, left=True)
    _credit(d, credit, look)
    img.save(out)


def dark_bg(g, out):
    """Фон кадра «страхов»: чёрный, рамка со свечением (glow), красный экран (red), овал (oval) или ничего."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    x, y, cw, ch, var = g["x"], g["y"], g["cw"], g["ch"], g["var"]
    if var == "red":                                 # красный «экран»: светящийся прямоугольник, к краям темнее
        rw, rh = 1420, 880
        rx, ry = (W - rw) // 2, (H - rh) // 2 - 20
        lay = Image.new("L", (W, H), 0)
        ImageDraw.Draw(lay).rounded_rectangle([rx, ry, rx + rw, ry + rh], 90, fill=255)
        lay = lay.filter(ImageFilter.GaussianBlur(40))
        core = Image.new("L", (W, H), 0)
        cd = ImageDraw.Draw(core)
        for k in range(30, 0, -1):                   # к центру ярче
            cd.ellipse([W / 2 - rw * k / 50, H / 2 - 20 - rh * k / 50, W / 2 + rw * k / 50, H / 2 - 20 + rh * k / 50],
                       fill=int(255 * (1 - k / 30) ** 0.6))
        core = core.filter(ImageFilter.GaussianBlur(60))
        red = Image.composite(Image.new("RGB", (W, H), (236, 40, 40)), Image.new("RGB", (W, H), (120, 0, 6)), core)
        img = Image.composite(red, img, lay)
    elif var in ("glow", "oval"):
        lay = Image.new("RGB", (W, H), (0, 0, 0))
        ld = ImageDraw.Draw(lay)
        pad = 26
        if var == "oval":
            ld.ellipse([x - pad, y - pad, x + cw + pad, y + ch + pad], fill=tuple(int(c * 0.55) for c in GLOW))
        else:
            ld.rounded_rectangle([x - pad, y - pad, x + cw + pad, y + ch + pad], 30,
                                 fill=tuple(int(c * 0.55) for c in GLOW))
        img = lay.filter(ImageFilter.GaussianBlur(38))
        d = ImageDraw.Draw(img)
        if var == "oval":
            d.ellipse([x - 5, y - 5, x + cw + 5, y + ch + 5], outline=(236, 234, 255), width=4)
        else:
            d.rounded_rectangle([x - 5, y - 5, x + cw + 5, y + ch + 5], 16, outline=(236, 234, 255), width=4)
    img.save(out)


def _is_diagram(src):
    """Схема/рисунок на белом фоне (органы, клетки) — в «страхах» становится светлым контуром на чёрном."""
    try:
        from .render import open_photo
        im = open_photo(src).convert("RGB").resize((96, 96))
        px = list(im.getdata())
        white = sum(1 for r, g, b in px if r > 225 and g > 225 and b > 225) / len(px)
        return white > 0.55
    except Exception:
        return False


def _pill(d, x, y, text, bg, fg, size=36, left=False):
    f = _font(size, BOLD_FONTS)
    tw = d.textlength(text, font=f)
    x0 = x if left else x - tw / 2 - 26
    d.rounded_rectangle([x0, y, x0 + tw + 52, y + size + 26], (size + 26) // 2, fill=bg + (255,))
    d.text((x0 + 26, y + 10), text, font=f, fill=fg + (255,))


def _kb_card(src, n, cw, ch, zoom_in=True, amp=0.06):
    """Фото внутри карточки: медленный наезд/отъезд с субпиксельной точностью. Кадры размера карточки."""
    from .render import open_photo
    im = open_photo(src)
    bw, bh = round(cw * (1 + amp)), round(ch * (1 + amp))
    sc = max(bw / im.width, bh / im.height)
    im = im.resize((max(bw, round(im.width * sc)), max(bh, round(im.height * sc))), Image.LANCZOS)
    l, t = (im.width - bw) // 2, (im.height - bh) // 2
    base = im.crop((l, t, l + bw, t + bh))
    last = max(1, n - 1)
    for k in range(n):
        p = k / last
        e = 0.5 * p + 0.5 * (p * p * (3 - 2 * p))
        z = 1 + amp * (e if zoom_in else 1 - e)
        w_, h_ = bw / z, bh / z
        x0, y0 = (bw - w_) / 2, (bh - h_) / 2
        yield base.resize((cw, ch), Image.BICUBIC, box=(x0, y0, x0 + w_, y0 + h_)).tobytes()


def render_look_shot(s, total, out, workdir):
    """Кадр ниши: s — словарь кадра (kind, src, grade, look, side, caption, credit, ...)."""
    look, side = s["look"], bool(s.get("side"))
    g = geometry(look, str(s.get("src")) + str(s.get("s", "")), side)
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    tag = f"{look}_{int(side)}_{abs(hash((str(s.get('src')), s.get('s'), s.get('caption'), s.get('credit')))) % 10**10}"
    deco = wd / f"deco_{tag}.png"
    deco_png(look, deco, s.get("caption", ""), s.get("credit", ""), side)
    grade = s.get("grade") or "null"
    a = 0.55                                                # время въезда/падения карточки
    mv = f"pow(1-min(1,t/{a}),3)"
    is_img = s["kind"] == "image"
    cw, ch = g["cw"], g["ch"]
    if is_img:
        inp = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{cw}x{ch}", "-r", str(FPS), "-i", "-"]
        pre = ""
    else:
        inp, pre = _video_input(s, total)
    if look == "dark":
        bgp = wd / f"dark_{g['var']}_{cw}x{ch}_{g['x']}_{g['y']}.png"
        if not bgp.exists():
            dark_bg(g, bgp)
        inv = ",negate,hue=s=0,eq=contrast=1.15:brightness=-0.03" if is_img and _is_diagram(s["src"]) else ""
        fit = ("scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch}"
               .format(cw=cw, ch=ch))
        if g["var"] == "oval":
            mask = wd / f"oval_{cw}x{ch}.png"
            if not mask.exists():
                m = Image.new("L", (cw, ch), 0)
                ImageDraw.Draw(m).ellipse([0, 0, cw - 1, ch - 1], fill=255)
                m.save(mask)
            chain = (f"[0:v]{pre}{fit},setsar=1,fps={FPS},{grade}{inv},format=rgba[m];[2:v]format=gray[mk];"
                     f"[m][mk]alphamerge[c];[1:v]format=rgba[bg];[bg][c]overlay=x={g['x']}:y={g['y']}[b2];"
                     f"[3:v]format=rgba[dc];[b2][dc]overlay=x=0:y=0{DARK_FX},format=yuv420p[v]")
            extra = ["-loop", "1", "-framerate", str(FPS), "-i", str(bgp),
                     "-loop", "1", "-framerate", str(FPS), "-i", str(mask),
                     "-loop", "1", "-framerate", str(FPS), "-i", str(deco)]
        else:
            soft = ""
            chain = (f"[0:v]{pre}{fit},setsar=1,fps={FPS},{grade}{inv}{soft}[c];"
                     f"[1:v][c]overlay=x={g['x']}:y={g['y']}[b2];"
                     f"[2:v]format=rgba[dc];[b2][dc]overlay=x=0:y=0{DARK_FX},format=yuv420p[v]")
            extra = ["-loop", "1", "-framerate", str(FPS), "-i", str(bgp),
                     "-loop", "1", "-framerate", str(FPS), "-i", str(deco)]
    elif look == "studio":
        bg = wd / "studio_bg.png"
        if not bg.exists():
            studio_bg().save(bg)
        mask = wd / f"mask_{cw}x{ch}.png"
        if not mask.exists():
            _round_mask(cw, ch, g["r"]).save(mask)
        shp = wd / f"shadow_{cw}x{ch}.png"
        if not shp.exists():
            _shadow(cw, ch, g["r"], 22, 120).save(shp)
        pad = 22 * 3
        chain = (f"[0:v]{pre}scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch},"
                 f"setsar=1,fps={FPS},{grade},format=rgba[m];[2:v]format=gray[mk];[m][mk]alphamerge[c];"
                 f"[1:v]format=rgba[bg];"
                 f"[bg][3:v]overlay=x='{g['x'] - pad + 8}+{g['dx']}*{mv}':y={g['y'] - pad + 26}[b1];"
                 f"[b1][c]overlay=x='{g['x']}+{g['dx']}*{mv}':y={g['y']}[b2];"
                 f"[4:v]format=rgba,fade=t=in:st=0.35:d=0.5:alpha=1[dc];"
                 f"[b2][dc]overlay=x='60*pow(1-min(1,max(0,t-0.35)/0.6),3)':y=0,format=yuv420p[v]")
        extra = ["-loop", "1", "-framerate", str(FPS), "-i", str(bg),
                 "-loop", "1", "-framerate", str(FPS), "-i", str(mask),
                 "-loop", "1", "-framerate", str(FPS), "-i", str(shp),
                 "-loop", "1", "-framerate", str(FPS), "-i", str(deco)]
    else:
        b, bb, ang = g["b"], g["bb"], g["angle"]
        pw, ph = cw + 2 * b, ch + b + bb
        import math
        ra = math.radians(ang)
        rw = int(abs(pw * math.cos(ra)) + abs(ph * math.sin(ra))) + 2
        rh = int(abs(pw * math.sin(ra)) + abs(ph * math.cos(ra))) + 2
        x0, y0 = g["x"] + cw // 2 - rw // 2, g["y"] + ch // 2 - rh // 2
        shp = wd / f"pshadow_{pw}x{ph}_{ang:.2f}.png"
        if not shp.exists():
            _shadow(pw, ph, 4, 18, 170, ang).save(shp)
        sh_img = Image.open(shp)
        sx, sy = g["x"] + cw // 2 - sh_img.width // 2 + 10, g["y"] + ch // 2 - sh_img.height // 2 + 22
        chain = (f"[0:v]{pre}fps={FPS},split=2[a][b];"
                 f"[a]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=10:2,"
                 f"scale={W}:{H},eq=brightness=-0.22:saturation=0.7,format=rgba[bg];"
                 f"[b]scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch},setsar=1,"
                 f"{grade},pad={pw}:{ph}:{b}:{b}:color=0xF4F1EA,format=rgba,"
                 f"rotate={ra:.5f}:ow={rw}:oh={rh}:c=none[c];"
                 f"[bg][1:v]overlay=x={sx}:y='{sy}+{g['dy']}*{mv}'[b1];"
                 f"[b1][c]overlay=x={x0}:y='{y0}+{g['dy']}*{mv}'[b2];"
                 f"[2:v]format=rgba,fade=t=in:st=0.3:d=0.4:alpha=1[dc];"
                 f"[b2][dc]overlay=x=0:y=0,format=yuv420p[v]")
        extra = ["-loop", "1", "-framerate", str(FPS), "-i", str(shp),
                 "-loop", "1", "-framerate", str(FPS), "-i", str(deco)]
    from . import render
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *inp, *extra, "-filter_complex", chain,
           "-map", "[v]", "-frames:v", str(total), *render.enc(), str(out)]
    if is_img:
        amp_ = 0.015 if look == "dark" else 0.06 * min(1.0, max(0.4, total / FPS / 5))   # страхи: кадр почти стоит
        frames = _kb_card(s["src"], total, cw, ch, s.get("zoom_in", True), amp_)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        try:
            for buf in frames:
                p.stdin.write(buf)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise render.RenderError(err[-1500:])
    else:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise render.RenderError(r.stderr[-1500:])


def _video_input(s, total):
    """Как в render_video_shot: участок ролика, лёгкое замедление короткого, без повтора начала."""
    need = total / FPS
    clip_dur, offset_n, speed = s.get("clip_dur", 0) or 0, s.get("offset_n", 0) or 0, s.get("speed", 1.0) or 1.0
    slow, start = 1.0, 0.0
    if clip_dur and clip_dur < need:
        slow = min(1.6, need / clip_dur)
    if speed < 0.99:
        slow = min(2.0, max(slow, 1.0 / speed))
    elif clip_dur and offset_n:
        free = max(clip_dur - need, 0)
        start = (offset_n * need) % free if free > 0.5 else 0.0
    elif clip_dur and clip_dur > need + 1.0:
        start = 0.4
    return (["-ss", f"{start:.2f}", "-stream_loop", "-1", "-i", str(s["src"])],
            f"setpts={slow:.4f}*(PTS-STARTPTS),")


# ---------------- заставки глав ----------------
def make_chapter_clip(look, number, title, length, out, word="KAPITEL", accent=(34, 110, 90)):
    """Анимированная заставка главы (mp4 без звука), длина length секунд."""
    total = max(1, int(round(length * FPS)))
    if look == "studio":
        frames = _studio_chapter(number, title, total, word, accent)
    elif look == "dark":
        frames = _dark_chapter(title, total)
    else:
        frames = _food_chapter(number, title, total)
    from . import render
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-frames:v", str(total), *render.enc(), str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        for fr in frames:
            p.stdin.write(fr.tobytes())
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = p.stderr.read().decode("utf-8", "replace")
    if p.wait() != 0:
        raise render.RenderError(err[-1500:])


def _studio_chapter(number, title, total, word, accent):
    bg = studio_bg()
    t_up = (title or "").upper()
    f = _fit(t_up, 1500, (150, 130, 112, 96, 84, 72), HEAD_FONTS)
    lines = _wrap(t_up, f, 1500, 2)
    lh = int(f.size * 1.08)
    y0 = H // 2 - (len(lines) * lh) // 2 + 30
    nchar = sum(len(x) for x in lines)
    lab = f"{word} {number}".upper()
    fl = _font(40, BOLD_FONTS)
    for k in range(total):
        t = k / FPS
        img = bg.copy()
        d = ImageDraw.Draw(img, "RGBA")
        e = _ease((t - 0.1) / 0.4)
        if e > 0:                                       # плашка «KAPITEL 2» выезжает сверху
            tw = d.textlength(lab, font=fl)
            x = W / 2 - tw / 2 - 30
            y = y0 - 120 - int((1 - e) * 40)
            d.rounded_rectangle([x, y, x + tw + 60, y + 70], 35, fill=tuple(accent) + (int(255 * e),))
            d.text((x + 30, y + 12), lab, font=fl, fill=(255, 255, 255, int(255 * e)))
        shown = int(nchar * min(1.0, max(0.0, (t - 0.35) / 0.9)))     # название печатается
        y, left, cur_done = y0, shown, False
        for ln in lines:
            part = ln[:max(0, left)]
            xs = W / 2 - d.textlength(ln, font=f) / 2
            if part:
                d.text((xs, y), part, font=f, fill=(22, 24, 28, 255))
            if not cur_done and (left < len(ln) or ln is lines[-1]):
                cur_done = True
                if shown < nchar or int(t * 2.2) % 2 == 0:            # курсор «_» мигает после печати
                    cx = xs + (d.textlength(part, font=f) if part else 0) + 6
                    d.rectangle([cx, y + f.size * 0.82, cx + f.size * 0.45, y + f.size * 0.9], fill=(22, 24, 28, 255))
            left -= len(ln)
            y += lh
        el = _ease((t - 1.2) / 0.6)
        if el > 0:                                      # линия-акцент растёт под названием
            yl = y0 + len(lines) * lh + 30
            d.rectangle([W / 2 - 160 * el, yl, W / 2 + 160 * el, yl + 6], fill=tuple(accent) + (255,))
        yield img


def _dark_chapter(title, total):
    """Страхи: на чёрном проявляется название (белый шрифт с засечками, мягкое свечение) и гаснет."""
    f = _fit(title or "", 1500, (96, 84, 72, 62, 54), SERIF_FONTS)
    lines = _wrap(title or "", f, 1500, 2)
    lh = int(f.size * 1.2)
    base = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(base)
    y = H // 2 - len(lines) * lh // 2 - 10
    for ln in lines:
        d.text(((W - d.textlength(ln, font=f)) / 2, y), ln, font=f, fill=(244, 244, 250))
        y += lh
    from PIL import ImageChops
    lit = ImageChops.screen(base, base.filter(ImageFilter.GaussianBlur(16)))
    for k in range(total):
        t, rest = k / FPS, (total - k) / FPS
        a = min(1.0, t / 0.6, rest / 0.4)
        yield Image.blend(base.point(lambda v: 0), lit, max(0.0, a))


def _hazard(w, h, off):
    band = Image.new("RGB", (w, h), (20, 20, 20))
    d = ImageDraw.Draw(band)
    step = 70
    for x in range(-2 * step, w + 2 * step, step):
        xx = x + int(off) % step
        d.polygon([(xx, 0), (xx + step // 2, 0), (xx + step // 2 - h, h), (xx - h, h)], fill=YELLOW)
    return band


def _food_chapter(number, title, total):
    split = 760                                          # ширина красной панели (по верхнему краю)
    num = f"#{number}"
    fn = _fit(num, 560, (300, 260, 220, 190), HEAD_FONTS)
    import re as _re
    ym = _re.search(r"\s*\(([^()]{2,24})\)\s*$", title or "")    # «Название (2003)» — год плашкой под названием
    year = ym.group(1) if ym else ""
    if ym:
        title = title[:ym.start()]
    words = (title or "").upper().split()
    half = max(1, (len(words) + 1) // 2)
    l1, l2 = " ".join(words[:half]), " ".join(words[half:])
    f1 = _fit(l1, 900, (96, 84, 72, 62, 54), HEAD_FONTS)
    f2 = _fit(l2 or " ", 900, (110, 96, 84, 72, 62), HEAD_FONTS)
    for k in range(total):
        t = k / FPS
        img = Image.new("RGB", (W, H), CREAM)
        d = ImageDraw.Draw(img, "RGBA")
        e = _ease(t / 0.35)
        sx = int(-split - 200 + (split + 200) * e)       # красная косая панель въезжает слева
        d.polygon([(sx - 400, 0), (sx + split, 0), (sx + split - 240, H), (sx - 400, H)], fill=RED)
        en = _ease((t - 0.2) / 0.35)
        if en > 0:                                       # «#2» — крупно, с тенью
            tw = d.textlength(num, font=fn)
            x = (split - 240) / 2 + 60 - tw / 2
            y = H / 2 - fn.size * 0.62 - int((1 - en) * 60)
            d.text((x + 10, y + 12), num, font=fn, fill=(0, 0, 0, int(200 * en)))
            d.text((x, y), num, font=fn, fill=(255, 255, 255, int(255 * en)))
        et = _ease((t - 0.35) / 0.4)
        if et > 0:                                       # название выезжает справа: первая строка чёрная, вторая красная
            x = split + 40 + int((1 - et) * 160)
            y = H / 2 - 120
            d.text((x, y), l1, font=f1, fill=(18, 18, 18, int(255 * et)))
            d.rectangle([x, y + f1.size + 14, x + 220, y + f1.size + 22], fill=YELLOW + (int(255 * et),))
            if l2:
                d.text((x, y + f1.size + 40), l2, font=f2, fill=RED + (int(255 * et),))
            if year:
                fy = _font(40, BOLD_FONTS)
                yy = y + f1.size + 40 + (f2.size + 30 if l2 else 0)
                tw = d.textlength(year, font=fy)
                d.rounded_rectangle([x, yy, x + tw + 44, yy + 62], 31, fill=(18, 18, 18, int(255 * et)))
                d.text((x + 22, yy + 9), year, font=fy, fill=(255, 255, 255, int(255 * et)))
        img.paste(_hazard(W + 10, 54, t * 90), (0, H - 54))   # жёлто-чёрная лента внизу медленно едет
        yield img


# ---------------- подписи поверх видео ----------------
def make_label(style, text, out_path, accent=(34, 110, 90)):
    """Подпись абзаца/места/человека в стиле ниши. Прозрачный PNG 1920x1080."""
    main, _, sub = (text or "").partition("|")
    main, sub = main.strip(), sub.strip()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if style == "studio":                                # белая плашка слева сверху, жирный узкий шрифт
        f = _fit(main.upper(), 1100, (54, 48, 42, 36), HEAD_FONTS)
        fs = _font(28, BOLD_FONTS)
        tw = max(d.textlength(main.upper(), font=f), d.textlength(sub, font=fs) if sub else 0)
        x, y = 90, 80
        h = f.size + 40 + (44 if sub else 0)
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([x + 6, y + 12, x + tw + 86, y + h + 12], 22, fill=(0, 0, 0, 90))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(14)))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([x, y, x + tw + 80, y + h], 22, fill=(255, 255, 255, 245))
        d.rectangle([x + 22, y + 22, x + 28, y + h - 22], fill=tuple(accent) + (255,))
        d.text((x + 46, y + 16), main.upper(), font=f, fill=(22, 24, 28, 255))
        if sub:
            d.text((x + 48, y + 22 + f.size), sub, font=fs, fill=(92, 98, 106, 255))
    elif style == "dark":                                # страхи: белый текст с засечками и свечением снизу по центру
        f = _fit(main, 1400, (46, 42, 38, 34), SERIF_FONTS)
        tw = d.textlength(main, font=f)
        y = H - 190 if sub else H - 150
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).text((W / 2 - tw / 2, y), main, font=f, fill=GLOW + (255,))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(9)))
        d = ImageDraw.Draw(img)
        d.text((W / 2 - tw / 2, y), main, font=f, fill=(246, 246, 252, 255))
        if sub:
            fs = _font(30, SERIF_FONTS)
            d.text((W / 2 - d.textlength(sub, font=fs) / 2, y + f.size + 12), sub, font=fs, fill=(170, 170, 186, 255))
    else:                                                # еда: жёлтая плашка по центру снизу, ниже белая
        f = _fit(main.upper(), 1300, (46, 42, 38, 34), BOLD_FONTS)
        tw = d.textlength(main.upper(), font=f)
        y = H - 210 if sub else H - 160
        d.rounded_rectangle([W / 2 - tw / 2 - 30, y + 6, W / 2 + tw / 2 + 30, y + f.size + 34], 30, fill=(0, 0, 0, 110))
        _pill(d, W / 2, y, main.upper(), YELLOW, (18, 18, 18), f.size)
        if sub:
            _pill(d, W / 2, y + f.size + 40, sub.upper(), (250, 250, 248), (18, 18, 18), 30)
    img.save(out_path)


def warning_png(text, out):
    """Страхи: предупреждение в начале — светящийся голубовато-белый текст и красная строка сверху."""
    lines = [x.strip() for x in (text or "").split("|") if x.strip()]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    y = 110
    for i, ln in enumerate(lines):
        red = i == len(lines) - 1 and len(lines) > 1
        f = _fit(ln.upper(), 1500, (40, 36, 32, 28), BOLD_FONTS)
        col = (255, 60, 60, 255) if red else (215, 230, 255, 255)
        d.text(((W - d.textlength(ln.upper(), font=f)) / 2, y), ln.upper(), font=f, fill=col)
        y += f.size + 18
    img = Image.alpha_composite(img, lay.filter(ImageFilter.GaussianBlur(8)))
    img = Image.alpha_composite(img, lay)
    img.save(out)


def intro_png(title, warning, out, sub=""):
    """Страхи: заставка в начале — на чёрном предупреждение сверху (голубое и красное свечение, 2–3 строки),
    крупное название посередине (белый шрифт с засечками, сиреневое свечение) и подзаголовок под ним."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    lines = [x.strip() for x in (warning or "").split("|") if x.strip()]
    y = 120
    for i, ln in enumerate(lines):
        red = i == len(lines) - 1 and len(lines) > 1
        f = _fit(ln.upper(), 1600, (46, 42, 38, 34), BOLD_FONTS)
        d.text(((W - d.textlength(ln.upper(), font=f)) / 2, y), ln.upper(), font=f,
               fill=(255, 64, 64, 255) if red else (214, 228, 255, 255))
        y += f.size + 20
    f = _fit(title or "", 1600, (150, 132, 116, 100, 86), SERIF_FONTS)
    tl = _wrap(title or "", f, 1600, 2)
    lh = int(f.size * 1.12)
    ty = H // 2 - len(tl) * lh // 2 + 40
    for ln in tl:
        d.text(((W - d.textlength(ln, font=f)) / 2, ty), ln, font=f, fill=(246, 246, 252, 255))
        ty += lh
    if sub:
        fs = _fit(sub, 1500, (52, 46, 40, 36), SERIF_FONTS)
        d.text(((W - d.textlength(sub, font=fs)) / 2, ty + 24), sub, font=fs, fill=(196, 194, 214, 255))
    glow = lay.copy()
    gpx = glow.split()[3]
    tint = Image.new("RGBA", (W, H), GLOW + (255,))
    tint.putalpha(gpx)
    img = Image.alpha_composite(img, tint.filter(ImageFilter.GaussianBlur(18)))
    img = Image.alpha_composite(img, lay.filter(ImageFilter.GaussianBlur(6)))
    img = Image.alpha_composite(img, lay)
    img.save(out)


def unknown_person(name, role, out):
    """Страхи: человек без свободного фото — рамка со знаком «?» и имя под ней (как в примерах). JPG 1920x1080."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    lay = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(lay)
    bw = 300
    x0, y0 = (W - bw) // 2, H // 2 - bw // 2 - 70
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bw], 36, outline=(236, 234, 255), width=7)
    fq = _font(230, SERIF_FONTS)
    bb = d.textbbox((0, 0), "?", font=fq)
    d.text((W / 2 - (bb[0] + bb[2]) / 2, y0 + bw / 2 - (bb[1] + bb[3]) / 2), "?", font=fq, fill=(244, 244, 250))
    f = _fit(name or "", 1400, (64, 56, 48), SERIF_FONTS)
    d.text(((W - d.textlength(name, font=f)) / 2, y0 + bw + 30), name, font=f, fill=(244, 244, 250))
    if role:
        fr = _font(34, SERIF_FONTS)
        d.text(((W - d.textlength(role, font=fr)) / 2, y0 + bw + 40 + f.size), role, font=fr, fill=(170, 170, 186))
    from PIL import ImageChops
    glow = lay.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.9))
    img = ImageChops.screen(lay, Image.composite(Image.new("RGB", (W, H), GLOW), Image.new("RGB", (W, H), (0, 0, 0)),
                                                glow.convert("L")))
    img.save(out, quality=94)
