"""Новые кадры «как в хорошей документалке»:
- географическая карта с рельефом и глубинами моря (по открытым данным высот), пунктир маршрута
  плавно прокладывается к следующей точке;
- радиопереговоры: осциллограф по настоящему звуку, время, позывной, фраза и перевод;
- доска расследования: фото на пробке, красная нить, камера подъезжает к новому человеку;
- точечные приближения на фото: камера переходит от одного человека/предмета к другому и отъезжает.
Все кадры рисуются в программе (Pillow + numpy) и кодируются ffmpeg."""
import hashlib
import io
import math
import os
import random
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .config import ROOT, ffmpeg_exe
from .extras import SEMI_FONTS, SERIF_BOLD, THIN_FONTS, TYPE_BOLD, TYPE_FONTS, _font, _wax_seal

W, H, FPS = 1920, 1080, 30
_NW = 0x08000000 if os.name == "nt" else 0
SERIF = [r"C:\Windows\Fonts\georgia.ttf", r"C:\Windows\Fonts\times.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
SERIF_IT = [r"C:\Windows\Fonts\georgiai.ttf", r"C:\Windows\Fonts\timesi.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-Italic.ttf"]


def _ease(p):
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


def _ease2(p):                               # мягче в начале и в конце (для камеры)
    p = min(1.0, max(0.0, p))
    return 0.5 - 0.5 * math.cos(math.pi * p)


class _Writer:
    """Кадры RGB → ffmpeg (libx264). with _Writer(out) as w: w.add(img)"""

    def __init__(self, out, crf=18):
        self.cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", str(crf), "-pix_fmt", "yuv420p", str(out)]

    def __enter__(self):
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NW)
        return self

    def add(self, img, times=1):
        b = img.tobytes()
        for _ in range(times):
            self.p.stdin.write(b)

    def __exit__(self, *a):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        err = self.p.stderr.read().decode("utf-8", "replace")
        if self.p.wait() != 0:
            raise RuntimeError(err[-800:])


def _text_halo(d, xy, text, font, fill, halo=(240, 232, 210), r=3):
    x, y = xy
    for dx in range(-r, r + 1, 1):
        for dy in range(-r, r + 1, 1):
            if dx * dx + dy * dy <= r * r:
                d.text((x + dx, y + dy), text, font=font, fill=halo)
    d.text((x, y), text, font=font, fill=fill)


# =====================================================================================
#                         ГЕОГРАФИЧЕСКАЯ КАРТА (рельеф, море, суша)
# =====================================================================================
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_DIR = ROOT / "cache" / "terrain"
_tile_hook = None                     # для проверки без сети: функция (z, x, y) -> массив высот 256×256


def _tile(z, x, y):
    n = 2 ** z
    x %= n
    if not (0 <= y < n):
        return np.full((256, 256), -4000.0, np.float32)
    if _tile_hook:
        return _tile_hook(z, x, y)
    f = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if not f.exists():
        import requests
        r = requests.get(TILE_URL.format(z=z, x=x, y=y), timeout=25,
                         headers={"User-Agent": "DocuStudio (documentary maps)"})
        r.raise_for_status()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(r.content)
    a = np.asarray(Image.open(f).convert("RGB"), np.float32)
    return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768


def _merc(lat, lon):
    """Меркатор в долях мира (0..1)."""
    lat = max(-85.0, min(85.0, lat))
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


_SEA = [(0, (176, 196, 190)), (-30, (160, 186, 184)), (-200, (134, 164, 170)), (-1000, (108, 138, 152)),
        (-4000, (84, 110, 128)), (-8000, (70, 92, 110))]
_LAND = [(0, (206, 196, 158)), (150, (196, 186, 146)), (500, (182, 168, 126)), (1200, (164, 144, 106)),
         (2500, (150, 132, 108)), (4000, (196, 188, 176)), (6000, (236, 232, 226))]


def _ramp(v, stops):
    xs = np.array([s[0] for s in stops], np.float32)
    cs = np.array([s[1] for s in stops], np.float32)
    order = np.argsort(xs)
    xs, cs = xs[order], cs[order]
    out = np.empty(v.shape + (3,), np.float32)
    for k in range(3):
        out[..., k] = np.interp(v, xs, cs[:, k])
    return out


def _dilate(mask, r):
    im = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.MaxFilter(r * 2 + 1))) > 127


def terrain_base(points, w, h, pad=0.42, min_span=(2.4, 3.6), log=None):
    """Карта-подложка с рельефом. Возвращает (картинка RGB w×h, функция (lat, lon) -> (x, y)) или (None, None)."""
    lats = [p[1] for p in points]
    lons = [p[2] for p in points]
    la0, la1, lo0, lo1 = min(lats), max(lats), min(lons), max(lons)
    cla, clo = (la0 + la1) / 2, (lo0 + lo1) / 2
    sla = max(min_span[0], (la1 - la0) * (1 + 2 * pad))
    slo = max(min_span[1], (lo1 - lo0) * (1 + 2 * pad))
    x0, y1 = _merc(cla - sla / 2, clo - slo / 2)
    x1, y0 = _merc(cla + sla / 2, clo + slo / 2)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dw, dh = x1 - x0, y1 - y0
    if dw / dh < w / h:                      # под пропорции кадра
        dw = dh * w / h
    else:
        dh = dw * h / w
    x0, x1, y0, y1 = cx - dw / 2, cx + dw / 2, cy - dh / 2, cy + dh / 2
    z = int(max(2, min(11, math.ceil(math.log2(w * 1.05 / (dw * 256))))))
    while True:                                   # не больше ~150 плиток: при большой площади — уровень грубее
        n = 256 * 2 ** z
        px0, px1, py0, py1 = x0 * n, x1 * n, y0 * n, y1 * n
        tx0, tx1, ty0, ty1 = int(px0 // 256), int(px1 // 256), int(py0 // 256), int(py1 // 256)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 150 or z <= 2:
            break
        z -= 1
    rows = []
    try:
        for ty in range(ty0, ty1 + 1):
            rows.append(np.concatenate([_tile(z, tx, ty) for tx in range(tx0, tx1 + 1)], axis=1))
    except Exception as e:
        if log:
            log(f"  Рельеф для карты не загрузился ({str(e)[:80]}) — будет старая бумажная карта.")
        return None, None
    mos = np.concatenate(rows, axis=0)
    ox, oy = px0 - tx0 * 256, py0 - ty0 * 256
    crop = mos[int(oy):int(oy + (py1 - py0)), int(ox):int(ox + (px1 - px0))]
    elev = np.asarray(Image.fromarray(crop.astype(np.float32), mode="F").resize((w, h), Image.BILINEAR))
    # тени рельефа (свет с северо-запада), масштаб — метры на пиксель
    mpp = 156543.0 * math.cos(math.radians(cla)) / 2 ** z * ((px1 - px0) / w)
    land = elev > 0
    e2 = np.where(land, elev, elev * 0.12)
    gy, gx = np.gradient(e2 / max(mpp, 1.0) * 2.2)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(315), math.radians(42)
    shade = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1)
    col = np.where(land[..., None], _ramp(elev, _LAND), _ramp(elev, _SEA))
    mul = np.where(land, 0.70 + 0.45 * shade, 0.93 + 0.10 * shade)
    col *= mul[..., None]
    # береговая линия и «волны» вдоль берега, как на старых атласах
    coast = _dilate(land, 1) & ~land
    for r_, a_ in ((7, 0.20), (14, 0.12), (22, 0.07)):
        ring = _dilate(land, r_) & ~_dilate(land, r_ - 1)
        col[ring] = col[ring] * (1 - a_) + np.array([70, 90, 96], np.float32) * a_
    col[coast] = (74, 62, 48)
    # бумага: зерно, пятна, затемнённые края
    rs = np.random.RandomState(int(hashlib.md5(str(points).encode()).hexdigest()[:8], 16))
    noise = rs.normal(0, 1, (h // 4 + 1, w // 4 + 1)).astype(np.float32)
    noise = np.asarray(Image.fromarray(noise, mode="F").resize((w, h), Image.BICUBIC))
    col *= (1 + 0.035 * noise)[..., None]
    col += rs.normal(0, 3.0, (h, w, 1)).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    vig = ((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2
    col *= (1 - 0.22 * np.clip(vig - 0.35, 0, 1))[..., None]
    sepia = np.array([1.03, 1.0, 0.92], np.float32)
    col = col * sepia
    img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8))

    def xy(lat, lon):
        mx, my = _merc(lat, lon)
        return (mx * n - px0) / (px1 - px0) * w, (my * n - py0) / (py1 - py0) * h

    def ll(x, y):                               # пиксель -> (широта, долгота) — для сетки
        mx = (px0 + x / w * (px1 - px0)) / n
        my = (py0 + y / h * (py1 - py0)) / n
        lon = mx * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * my))))
        return lat, lon
    xy.ll = ll
    return img, xy


def dash_secs(dt):
    """Сколько секунд пунктир «ложится» от точки к точке: неторопливо, но успевает до следующего места."""
    return min(4.0, max(1.2, 0.85 * dt))


def make_geo_map_clip(title, points, times, length, out_mp4, seed="", log=None):
    """Географическая карта: точки появляются, когда диктор называет место; пунктир плавно
    «прокладывается» к следующей точке (2–3 с, мягкий старт и финиш), камера чуть приближается.
    Возвращает out_mp4 или None, если рельеф не загрузился (тогда рисуется бумажная карта)."""
    M = 1.08                                         # запас под движение камеры
    BW, BH = int(W * M), int(H * M)
    base, xy = terrain_base(points, BW, BH, log=log)
    if base is None:
        return None
    d0 = ImageDraw.Draw(base)
    ink, red = (52, 40, 30), (158, 26, 22)
    # сетка градусов
    lat_t, lon_l = xy.ll(0, 0)
    lat_b, lon_r = xy.ll(BW, BH)
    step = 1 if (lat_t - lat_b) < 8 else 2 if (lat_t - lat_b) < 16 else 5
    fs = _font(22, SERIF)
    for lat in range(int(math.floor(lat_b)), int(math.ceil(lat_t)) + 1):
        if lat % step:
            continue
        _, y = xy(lat, lon_l)
        d0.line([(0, y), (BW, y)], fill=(96, 84, 66), width=1)
        d0.text((BW * 0.04 + 6, y - 26), f"{abs(lat)}°{'N' if lat >= 0 else 'S'}", font=fs, fill=(80, 66, 50))
    for lon in range(int(math.floor(lon_l)), int(math.ceil(lon_r)) + 1):
        if lon % step:
            continue
        x, _ = xy(lat_t, lon)
        d0.line([(x, 0), (x, BH)], fill=(96, 84, 66), width=1)
        d0.text((x + 6, BH - BW * 0.035 - 40), f"{abs(lon)}°{'W' if lon < 0 else 'E'}", font=fs, fill=(80, 66, 50))
    # рамка атласа
    m = int(BW * 0.035)
    d0.rectangle([m, m, BW - m, BH - m], outline=ink, width=3)
    d0.rectangle([m + 8, m + 8, BW - m - 8, BH - m - 8], outline=ink, width=1)
    if title:
        ft = _font(40, SERIF_BOLD)
        tw = d0.textlength(title.upper(), font=ft)
        bx, by = m + 40, m + 34
        d0.rectangle([bx - 22, by - 16, bx + tw + 22, by + 62], fill=(232, 222, 196), outline=ink, width=2)
        d0.text((bx, by), title.upper(), font=ft, fill=(110, 30, 24))
    # роза ветров
    rx, ry = BW - m - 120, BH - m - 140
    d0.ellipse([rx - 58, ry - 58, rx + 58, ry + 58], outline=ink, width=2)
    d0.polygon([(rx, ry - 70), (rx - 13, ry), (rx + 13, ry)], fill=ink)
    d0.polygon([(rx, ry + 70), (rx - 13, ry), (rx + 13, ry)], outline=ink)
    d0.polygon([(rx - 70, ry), (rx, ry - 11), (rx, ry + 11)], outline=ink)
    d0.polygon([(rx + 70, ry), (rx, ry - 11), (rx, ry + 11)], outline=ink)
    d0.text((rx - 11, ry - 112), "N", font=_font(36, SERIF_BOLD), fill=ink)

    P = [xy(p[1], p[2]) for p in points]
    times = list(times) + [length - 1.0] * (len(points) - len(times))
    seg = [0.0] + [dash_secs(times[i] - times[i - 1]) for i in range(1, len(points))]
    fl = _font(38, SERIF_BOLD)
    n = max(1, int(round(length * FPS)))
    # подписи мест рядом с точкой, но не на пунктире, не на других подписях и точках
    from .extras import place_labels
    lab = place_labels(d0, [p[0] for p in points], P, fl, BW, BH, line_h=48)
    focus = np.array(P, np.float32).mean(axis=0)

    def dashed(d, a, b, frac):
        (xa, ya), (xb, yb) = a, b
        L = math.hypot(xb - xa, yb - ya)
        if L < 1:
            return a
        ux, uy = (xb - xa) / L, (yb - ya) / L
        s, dash, gap, end = 0.0, 20.0, 13.0, L * frac
        while s < end:
            e = min(s + dash, end)
            d.line([(xa + ux * s, ya + uy * s), (xa + ux * e, ya + uy * e)], fill=red, width=6)
            s += dash + gap
        return xa + ux * end, ya + uy * end

    with _Writer(out_mp4) as wr:
        prev_key, prev_img = None, None
        for f in range(n):
            t = f / FPS
            prog = []
            for i in range(len(points)):
                if i == 0:
                    prog.append(1.0 if t >= times[0] else 0.0)
                else:
                    prog.append(_ease((t - (times[i] - seg[i])) / seg[i]))
            pulses = [max(0.0, min(1.0, (t - times[i]) / 0.7)) if t >= times[i] else -1 for i in range(len(points))]
            # камера: медленный наезд (4%) к центру маршрута
            k = _ease2(f / max(1, n - 1))
            z = 1.0 + 0.04 * k
            cw, ch = BW / z, BH / z
            cx = BW / 2 + (focus[0] - BW / 2) * 0.25 * k
            cy = BH / 2 + (focus[1] - BH / 2) * 0.25 * k
            bx0 = min(max(0.0, cx - cw / 2), BW - cw)
            by0 = min(max(0.0, cy - ch / 2), BH - ch)
            key = (tuple(round(p, 3) for p in prog), tuple(round(p, 2) for p in pulses), round(bx0, 1),
                   round(by0, 1), round(z, 4))
            if key == prev_key:
                wr.add(prev_img)
                continue
            im = base.copy()
            d = ImageDraw.Draw(im, "RGBA")
            for i in range(1, len(points)):
                if prog[i] > 0:
                    head = dashed(d, P[i - 1], P[i], prog[i])
                    if prog[i] < 1:
                        d.ellipse([head[0] - 9, head[1] - 9, head[0] + 9, head[1] + 9], fill=red)
            for i, (name, _, _) in enumerate(points):
                if pulses[i] < 0:
                    continue
                x, y = P[i]
                if pulses[i] < 1:                     # круг расходится, как сигнал
                    r = 14 + 34 * pulses[i]
                    d.ellipse([x - r, y - r, x + r, y + r], outline=(158, 26, 22, int(220 * (1 - pulses[i]))), width=4)
                d.ellipse([x - 14, y - 14, x + 14, y + 14], outline=red, width=4)
                d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=red)
                a = min(1.0, (t - times[i]) / 0.4)
                if lab[i] is not None:
                    _text_halo(d, lab[i], name, fl, (ink[0], ink[1], ink[2], int(255 * a)),
                               halo=(236, 228, 204, int(230 * a)))
            fr = im.resize((W, H), Image.BICUBIC, box=(bx0, by0, bx0 + cw, by0 + ch))
            prev_key, prev_img = key, fr
            wr.add(fr)
    return out_mp4


# =====================================================================================
#                                РАДИОПЕРЕГОВОРЫ
# =====================================================================================
def _wav_mono(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    return x, sr


def _wrap(d, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if d.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def make_radio_clip(lines, audio_wav, length, out_mp4, header="FUNKVERKEHR", seed=""):
    """lines: [{"who", "en", "de", "a", "b"}] (секунды от начала клипа). Звук — тот же, что звучит в ролике:
    осциллограф рисуется по нему, текст фразы печатается, пока она звучит, перевод виден сразу."""
    x, sr = _wav_mono(audio_wav)
    n = max(1, int(round(length * FPS)))
    bg = Image.new("RGB", (W, H), (14, 16, 14))
    bd = ImageDraw.Draw(bg)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((xx - W / 2) / (W * 0.55)) ** 2 + ((yy - H * 0.42) / (H * 0.5)) ** 2))
    arr = np.asarray(bg, np.float32) + glow[..., None] * np.array([12, 22, 14], np.float32)
    arr[::3] *= 0.93                                     # строки развёртки
    bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    bd = ImageDraw.Draw(bg)
    amber, green, grey, white = (226, 170, 80), (120, 236, 140), (176, 180, 170), (236, 236, 228)
    fh = _font(30, TYPE_BOLD)
    bd.text((110, 78), header.upper(), font=fh, fill=amber)
    bd.line([110, 124, W - 110, 124], fill=(70, 66, 50), width=2)
    # рамка осциллографа
    ox0, oy0, ox1, oy1 = 160, 200, W - 160, 520
    bd.rectangle([ox0, oy0, ox1, oy1], outline=(54, 70, 56), width=2)
    for k in range(1, 8):
        gx = ox0 + (ox1 - ox0) * k / 8
        bd.line([gx, oy0, gx, oy1], fill=(30, 42, 32), width=1)
    for k in range(1, 4):
        gy = oy0 + (oy1 - oy0) * k / 4
        bd.line([ox0, gy, ox1, gy], fill=(30, 42, 32), width=1)
    f_who = _font(36, TYPE_BOLD)
    f_en = _font(46, TYPE_FONTS)
    f_de = _font(42, SERIF_IT)
    f_rec = _font(26, TYPE_BOLD)
    win = int(sr * 0.06)
    mid = (oy0 + oy1) / 2
    amp = (oy1 - oy0) * 0.42
    with _Writer(out_mp4) as wr:
        for f in range(n):
            t = f / FPS
            im = bg.copy()
            d = ImageDraw.Draw(im)
            if (f // 15) % 2 == 0:                          # мигающая точка «запись»
                d.ellipse([W - 300, 86, W - 280, 106], fill=(210, 40, 36))
            d.text((W - 266, 80), "REC", font=f_rec, fill=(210, 200, 190))
            # осциллограмма текущего звука
            c = int(t * sr)
            seg = x[max(0, c - win // 2): c + win // 2]
            if len(seg) < win:
                seg = np.pad(seg, (0, win - len(seg)))
            idx = np.linspace(0, len(seg) - 1, ox1 - ox0 - 20).astype(int)
            ys = mid - np.clip(seg[idx] * 3.2, -1, 1) * amp
            pts = list(zip(range(ox0 + 10, ox1 - 10), ys.tolist()))
            d.line(pts, fill=(40, 110, 56), width=7)
            d.line(pts, fill=green, width=3)
            cur = None
            for ln in lines:
                if ln["a"] - 0.25 <= t:
                    cur = ln
            if cur:
                al = min(1.0, (t - (cur["a"] - 0.25)) / 0.35)
                who = cur.get("who", "")
                d.text((160, 590), who.upper(), font=f_who,
                       fill=tuple(int(v * al + 14 * (1 - al)) for v in amber))
                en = cur.get("en", "")
                if en:
                    span = max(0.6, (cur["b"] - cur["a"]) * 0.92)
                    k_ = int(len(en) * min(1.0, max(0.0, (t - cur["a"]) / span)))
                    shown = "„" + en[:k_] + ("“" if k_ >= len(en) else "")
                    yy0 = 660
                    for row in _wrap(d, "„" + en + "“", f_en, W - 320)[:3]:
                        part = shown[:len(row)]
                        shown = shown[len(row):].lstrip()
                        d.text((160, yy0), part, font=f_en, fill=white)
                        yy0 += 62
                    de_y = yy0 + 22
                else:
                    de_y = 670
                de = cur.get("de", "")
                if de:
                    for row in _wrap(d, de, f_de, W - 320)[:3]:
                        d.text((160, de_y), row, font=f_de,
                               fill=tuple(int(v * al + 14 * (1 - al)) for v in grey))
                        de_y += 56
            wr.add(im)
    return out_mp4


def _bg_frames(bg_video, start, length):
    """Кадры фонового ролика (RGB, W×H) по порядку; ролик короче — идёт по кругу."""
    while True:
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.2f}", "-i", str(bg_video),
               "-t", f"{length + 1:.2f}", "-an", "-vf",
               f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NW)
        got = 0
        while True:
            b = p.stdout.read(W * H * 3)
            if len(b) < W * H * 3:
                break
            got += 1
            yield Image.frombytes("RGB", (W, H), b)
        p.stdout.close()
        p.wait()
        if got == 0:
            if start > 0:
                start = 0.0
                continue
            return
        start = 0.0


def make_radio_overlay_clip(lines, audio_wav, length, out_mp4, bg_video, bg_start=0.0, label="FUNKSPRUCH"):
    """Радио в прологе: хроника идёт дальше, сбоку — «голосовое сообщение»: кто говорит (или «неизвестный»),
    полоски звука, которые закрашиваются по мере прослушивания, фраза в оригинале и перевод."""
    x, sr = _wav_mono(audio_wav)
    n = max(1, int(round(length * FPS)))
    NB = 46
    bars = []                                            # полоски громкости для каждой фразы
    for ln in lines:
        a_, b_ = int(ln["a"] * sr), int(ln["b"] * sr)
        seg = x[a_:max(a_ + 1, b_)]
        ch = np.array_split(seg, NB) if len(seg) >= NB else [seg] * NB
        rms = np.array([float(np.sqrt(np.mean(c ** 2))) if len(c) else 0.0 for c in ch])
        rms = rms / (rms.max() + 1e-6)
        bars.append(0.18 + 0.82 * rms)
    f_lab = _font(24, TYPE_BOLD)
    f_who = _font(34, TYPE_BOLD)
    f_en = _font(36, TYPE_FONTS)
    f_de = _font(31, SERIF_IT)
    f_t = _font(24, TYPE_FONTS)
    amber, white, grey, red = (232, 178, 84), (240, 238, 230), (190, 190, 182), (214, 46, 40)
    PX, PW = 80, 900
    frames = _bg_frames(bg_video, bg_start, length) if bg_video else None
    last_bg = Image.new("RGB", (W, H), (16, 16, 16))
    with _Writer(out_mp4) as wr:
        for f in range(n):
            t = f / FPS
            if frames is not None:
                try:
                    last_bg = next(frames)
                except StopIteration:
                    frames = None
            im = Image.blend(last_bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.22)
            ci = None
            for i, ln in enumerate(lines):
                if ln["a"] - 0.3 <= t:
                    ci = i
            if ci is None:
                wr.add(im)
                continue
            ln = lines[ci]
            appear = min(1.0, max(0.0, (t - (lines[0]["a"] - 0.3)) / 0.35))
            d0 = ImageDraw.Draw(im)
            en, de = ln.get("en", ""), ln.get("de", "")
            en_rows = _wrap(d0, "„" + en + "“", f_en, PW - 80)[:3] if en else []
            de_rows = _wrap(d0, de, f_de, PW - 80)[:3] if de else []
            ph = 40 + 34 + 52 + 74 + len(en_rows) * 46 + (14 if de_rows else 0) + len(de_rows) * 40 + 30
            y0 = H - 70 - ph + int((1 - appear) * 40)
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            d.rounded_rectangle([PX, y0, PX + PW, y0 + ph], radius=26, fill=(10, 10, 10, int(205 * appear)),
                                outline=(90, 84, 70, int(220 * appear)), width=2)
            a8 = int(255 * appear)
            cy = y0 + 30
            if (f // 15) % 2 == 0:
                d.ellipse([PX + 40, cy + 6, PX + 56, cy + 22], fill=red + (a8,))
            d.text((PX + 70, cy), label.upper(), font=f_lab, fill=grey + (a8,))
            el = max(0.0, min(ln["b"], t) - ln["a"])
            tt = f"{int(el) // 60}:{int(el) % 60:02d} / {int(ln['b'] - ln['a'] + 0.99) // 60}:{int(ln['b'] - ln['a'] + 0.99) % 60:02d}"
            d.text((PX + PW - 40 - d.textlength(tt, font=f_t), cy), tt, font=f_t, fill=grey + (a8,))
            cy += 44
            d.text((PX + 40, cy), ln.get("who", "").upper(), font=f_who, fill=amber + (a8,))
            cy += 58
            # кнопка и полоски «голосового»
            bx, bmid = PX + 40, cy + 30
            d.ellipse([bx, bmid - 28, bx + 56, bmid + 28], fill=amber + (a8,))
            d.polygon([(bx + 22, bmid - 14), (bx + 22, bmid + 14), (bx + 42, bmid)], fill=(16, 16, 16, a8))
            prog = min(1.0, max(0.0, (t - ln["a"]) / max(0.3, ln["b"] - ln["a"])))
            gx0, gx1 = bx + 80, PX + PW - 40
            step = (gx1 - gx0) / NB
            for k in range(NB):
                hh = 6 + 46 * float(bars[ci][k])
                if prog * NB > k and ln["a"] <= t <= ln["b"]:   # сейчас звучит — полоска «живая»
                    c = int(t * sr)
                    w_ = x[max(0, c - 800): c + 800]
                    live = float(np.sqrt(np.mean(w_ ** 2))) * 6 if len(w_) else 0
                    hh = hh * (0.8 + 0.4 * min(1.0, live)) if k >= prog * NB - 3 else hh
                col = white if prog * NB > k else (110, 108, 100)
                xx = gx0 + k * step
                d.rounded_rectangle([xx, bmid - hh / 2, xx + step * 0.55, bmid + hh / 2], radius=3, fill=col + (a8,))
            cy += 74
            if en_rows:                                   # оригинал печатается по мере звучания
                span = max(0.6, (ln["b"] - ln["a"]) * 0.92)
                full = "„" + en + "“"
                k_ = int(len(full) * min(1.0, max(0.0, (t - ln["a"]) / span)))
                shown = full[:k_]
                for row in en_rows:
                    part = shown[:len(row)]
                    shown = shown[len(row):].lstrip()
                    d.text((PX + 40, cy), part, font=f_en, fill=white + (a8,))
                    cy += 46
                cy += 14
            for row in de_rows:
                d.text((PX + 40, cy), row, font=f_de, fill=grey + (a8,))
                cy += 40
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
            wr.add(im)
    return out_mp4


# =====================================================================================
#                          ДОСКА РАССЛЕДОВАНИЯ (пробка, фото, красная нить)
# =====================================================================================
BW2, BH2 = 3840, 2160
_SLOTS = [(1100, 430), (2740, 430), (1100, 1730), (2740, 1730), (400, 1080), (3440, 1080), (1920, 400),
          (1920, 1760), (400, 400), (3440, 400), (400, 1760), (3440, 1760)]     # центры фото на доске 3840×2160
PIN_DY = 255                                                                    # булавка — у верхнего края фото


def _cork(seed):
    rs = np.random.RandomState(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    a = np.zeros((BH2 // 2, BW2 // 2, 3), np.float32)
    a[:] = (146, 108, 70)
    a += rs.normal(0, 14, (BH2 // 2, BW2 // 2, 1))
    dots = rs.rand(BH2 // 2, BW2 // 2) > 0.985
    a[dots] *= 0.62
    yy, xx = np.mgrid[0:BH2 // 2, 0:BW2 // 2].astype(np.float32)
    vig = ((xx - BW2 / 4) / (BW2 / 4)) ** 2 + ((yy - BH2 / 4) / (BH2 / 4)) ** 2
    a *= (1 - 0.35 * np.clip(vig - 0.3, 0, 1))[..., None]
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((BW2, BH2), Image.BICUBIC)
    return im.filter(ImageFilter.GaussianBlur(1.2))


def _silhouette(size):
    im = Image.new("RGB", (size, size), (70, 70, 72))
    d = ImageDraw.Draw(im)
    d.ellipse([size * 0.33, size * 0.16, size * 0.67, size * 0.56], fill=(32, 32, 34))
    d.ellipse([size * 0.14, size * 0.58, size * 0.86, size * 1.25], fill=(32, 32, 34))
    f = _font(int(size * 0.26), SERIF_BOLD)
    tw = d.textlength("?", font=f)
    d.text((size / 2 - tw / 2, size * 0.24), "?", font=f, fill=(150, 150, 150))
    return im


def _polaroid(photo_path, name, role, seed):
    pw, ph, ps = 500, 600, 430
    card = Image.new("RGBA", (pw, ph), (238, 234, 222, 255))
    try:
        im = Image.open(photo_path).convert("RGB") if photo_path else None
    except Exception:
        im = None
    if im is not None:
        s = min(im.width, im.height)
        left = (im.width - s) // 2
        top = 0 if im.height > im.width else (im.height - s) // 2
        im = im.crop((left, top, left + s, top + s)).resize((ps, ps), Image.LANCZOS)
        im = ImageOps.grayscale(im).convert("RGB")
        im = Image.blend(im, Image.new("RGB", im.size, (112, 92, 66)), 0.16)
    else:
        im = _silhouette(ps)
    card.paste(im, ((pw - ps) // 2, 40))
    d = ImageDraw.Draw(card)
    f1 = _font(36, SEMI_FONTS)
    f2 = _font(26, SEMI_FONTS)
    for txt, f, y, col in ((name, f1, 40 + ps + 18, (40, 34, 30)), (role, f2, 40 + ps + 70, (110, 40, 34))):
        if not txt:
            continue
        while d.textlength(txt, font=f) > pw - 40 and f.size > 18:
            f = _font(f.size - 2, SEMI_FONTS)
        d.text(((pw - d.textlength(txt, font=f)) / 2, y), txt, font=f, fill=col)
    rot = random.Random(seed).uniform(-5, 5)
    shadow = Image.new("RGBA", (pw + 60, ph + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([36, 40, pw + 36, ph + 40], fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    shadow.paste(card, (30, 30), card)
    return shadow.rotate(rot, resample=Image.BICUBIC, expand=True)


def _pin(d, x, y, r=20):
    d.ellipse([x - r + 4, y - r + 6, x + r + 4, y + r + 6], fill=(0, 0, 0, 90))
    d.ellipse([x - r, y - r, x + r, y + r], fill=(178, 24, 22, 255))
    d.ellipse([x - r * 0.45, y - r * 0.6, x, y - r * 0.15], fill=(240, 140, 130, 255))


def _string(d, a, b, frac=1.0, sag=50):
    (xa, ya), (xb, yb) = a, b
    pts = []
    steps = 40
    for k in range(int(steps * frac) + 1):
        u = k / steps
        x = xa + (xb - xa) * u
        y = ya + (yb - ya) * u + sag * 4 * u * (1 - u)
        pts.append((x, y))
    if len(pts) > 1:
        d.line([(x + 3, y + 5) for x, y in pts], fill=(0, 0, 0, 80), width=7)
        d.line(pts, fill=(176, 22, 24, 255), width=7)


def _case_card(title, sub, seed):
    cw, ch = 1000, 560
    im = Image.new("RGBA", (cw, ch), (226, 214, 184, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([14, 14, cw - 14, ch - 14], outline=(90, 70, 50, 255), width=3)
    f1 = _font(74, TYPE_BOLD)
    f2 = _font(40, TYPE_FONTS)
    f3 = _font(30, TYPE_BOLD)
    d.text((60, 60), "AKTE", font=f3, fill=(140, 36, 30, 255))
    y = 130
    for row in _wrap(d, title.upper(), f1, cw - 120)[:2]:
        d.text((60, y), row, font=f1, fill=(36, 30, 26, 255))
        y += 86
    for row in _wrap(d, sub, f2, cw - 120)[:2]:
        d.text((60, y + 16), row, font=f2, fill=(70, 60, 50, 255))
        y += 52
    _wax_seal(im, cw - 120, ch - 110, 64, seed)
    return im.rotate(random.Random(seed).uniform(-2, 2), resample=Image.BICUBIC, expand=True)


_CHAIN = [0, 1, 5, 3, 2, 4, 6, 7, 8, 9, 10, 11]      # места по кругу вокруг карточки дела — нить идёт от лица к лицу


def make_board_clip(title, sub, pins, new_names, length, out_mp4, seed="", times=None, focus_names=None):
    """Доска расследования, которая растёт по ходу фильма.
    pins: [{"name", "role", "photo"}] — все люди на доске по порядку появления; new_names — кто добавляется сейчас.
    Каждый новый человек: камера стоит на предыдущем, красная нить тянется от него к новому месту,
    камера плавно едет вдоль нити, фото прикалывается, камера держится на лице, в конце — мягкий отъезд
    на всю цепочку. Первый человек — нить от карточки дела. Без новых (focus_names) — возвращение
    к доске: общий план, потом подъезд к названным."""
    board = _cork(seed or title).convert("RGBA")
    cxy = (BW2 / 2, BH2 / 2)
    cc = _case_card(title, sub, seed)
    case_pin = (cxy[0], cxy[1] - cc.height / 2 + 30)
    order = [p for p in pins if p.get("name")][:len(_CHAIN)]
    pos, cards = {}, {}
    for i, p in enumerate(order):
        pos[p["name"]] = _SLOTS[_CHAIN[i]]
        cards[p["name"]] = _polaroid(p.get("photo"), p["name"], p.get("role", ""), seed + p["name"])
    names = [p["name"] for p in order]
    pin_of = lambda nm: (pos[nm][0], pos[nm][1] - PIN_DY)
    prev_of = {nm: (pin_of(names[i - 1]) if i else case_pin) for i, nm in enumerate(names)}
    olds = [nm for nm in names if nm not in new_names]
    news = [nm for nm in names if nm in new_names]

    under = board.copy()                           # пробка и старые нити
    du = ImageDraw.Draw(under)
    for nm in olds:
        _string(du, prev_of[nm], pin_of(nm))
    top = Image.new("RGBA", board.size, (0, 0, 0, 0))
    top.alpha_composite(cc, (int(cxy[0] - cc.width / 2), int(cxy[1] - cc.height / 2)))
    dt = ImageDraw.Draw(top)
    for nm in olds:
        x, y = pos[nm]
        c = cards[nm]
        top.alpha_composite(c, (int(x - c.width / 2), int(y - c.height / 2)))
        _pin(dt, *pin_of(nm))
    _pin(dt, *case_pin)
    static = under.copy()
    static.alpha_composite(top)

    n = max(1, int(round(length * FPS)))
    T = n / FPS
    DRAW = 1.7                                     # сколько тянется нить
    if times and len(times) >= len(news) and all(x is not None for x in times[:len(news)]):
        t_new = [max(DRAW + 0.5, min(T - 1.6, float(x))) for x in times[:len(news)]]
    else:
        t_new = [min(T - 1.6, DRAW + 0.6 + k * (DRAW + 2.4)) for k in range(len(news))]
    for k in range(1, len(t_new)):
        t_new[k] = max(t_new[k], t_new[k - 1] + DRAW + 1.6)

    def close_on(pts, pad=1150.0):
        xs = [p_[0] for p_ in pts]
        ys = [p_[1] for p_ in pts]
        fx_, fy_ = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        vh = min(float(BH2), max(1300.0, ((max(xs) - min(xs)) + pad) * 9 / 16, (max(ys) - min(ys)) + pad))
        vw = vh * 16 / 9
        x0 = min(max(0.0, fx_ - vw / 2), BW2 - vw)
        y0 = min(max(0.0, fy_ - vh / 2), BH2 - vh)
        return (x0, y0, x0 + vw, y0 + vh)

    face = lambda nm: (pos[nm][0], pos[nm][1] - 40)
    chain_view = close_on([pos[nm] for nm in names] + [cxy], pad=1500.0)
    keys = []
    if news:
        first_from = names[names.index(news[0]) - 1] if names.index(news[0]) else None
        start = close_on([face(first_from)]) if first_from else close_on([cxy], pad=1600.0)
        keys.append((0.0, start))
        for nm, tn in zip(news, t_new):
            a_ = tn - DRAW                         # нить начинает тянуться
            keys.append((max(keys[-1][0] + 0.2, a_), keys[-1][1]))
            keys.append((tn + 0.5, close_on([face(nm)])))   # камера доезжает вдоль нити до нового лица
        if T - keys[-1][0] > 2.2:                   # мягкий отъезд на всю цепочку
            keys.append((T - 1.5, keys[-1][1]))
            keys.append((T, chain_view))
    else:
        tgt = [face(nm) for nm in (focus_names or []) if nm in pos]
        keys += [(0.0, chain_view), (0.6, chain_view)]
        if tgt:
            keys.append((min(T - 0.4, 2.8), close_on(tgt)))

    def view(t):
        for (ta, va), (tb, vb) in zip(keys, keys[1:]):
            if t <= tb:
                k = _ease2((t - ta) / max(1e-6, tb - ta))
                return tuple(a_ + (b_ - a_) * k for a_, b_ in zip(va, vb))
        return keys[-1][1]

    with _Writer(out_mp4) as wr:
        prev_key, prev_img = None, None
        for f in range(n):
            t = f / FPS
            drawing = [(nm, tn) for nm, tn in zip(news, t_new) if t >= tn - DRAW]
            v = view(t)
            key = (tuple(round(min(1.0, (t - tn + DRAW) / DRAW), 3) for nm, tn in drawing),
                   tuple(round(min(1.0, max(0.0, t - tn) / 0.45), 3) for nm, tn in drawing),
                   tuple(round(x, 1) for x in v))
            if key == prev_key:
                wr.add(prev_img)
                continue
            if not drawing:
                im = static.copy()
            else:
                im = under.copy()
                d = ImageDraw.Draw(im)
                for nm, tn in drawing:             # нить тянется от предыдущего к новому
                    sq = _ease((t - (tn - DRAW)) / DRAW)
                    _string(d, prev_of[nm], pin_of(nm), max(0.02, sq))
                im.alpha_composite(top)
                d = ImageDraw.Draw(im)
                for nm, tn in drawing:
                    if t < tn:
                        continue
                    x, y = pos[nm]
                    q = min(1.0, (t - tn) / 0.45)  # фото «прикалывают»: чуть крупнее и тень, потом садится
                    c = cards[nm]
                    s_ = 1.10 - 0.10 * _ease(q)
                    cc_ = c.resize((int(c.width * s_), int(c.height * s_)), Image.BICUBIC)
                    if q < 1:
                        cc_.putalpha(cc_.getchannel("A").point(lambda v_, q=q: int(v_ * min(1.0, q * 2))))
                    im.alpha_composite(cc_, (int(x - cc_.width / 2), int(y - cc_.height / 2 - 30 * (1 - _ease(q)))))
                    _pin(d, *pin_of(nm))
            fr = im.convert("RGB").resize((W, H), Image.BICUBIC, box=v)
            prev_key, prev_img = key, fr
            wr.add(fr)
    return out_mp4


# =====================================================================================
#                        ТОЧЕЧНЫЕ ПРИБЛИЖЕНИЯ НА ФОТО
# =====================================================================================
def focus_canvas(path, max_w=3840):
    """Фото в кадре 16:9 в высоком разрешении (вертикальное — по центру на размытом фоне).
    Возвращает (холст, функция перевода долей исходного фото в пиксели холста)."""
    im = Image.open(path).convert("RGB")
    iw, ih = im.size
    if iw / ih >= 1.6:
        cw = min(max_w, iw)
        chh = int(cw * 9 / 16)
        sc = max(cw / iw, chh / ih)
        rs = im.resize((int(iw * sc), int(ih * sc)), Image.LANCZOS)
        ox, oy = (rs.width - cw) // 2, (rs.height - chh) // 2
        canvas = rs.crop((ox, oy, ox + cw, oy + chh))

        def conv(bx):
            return (bx[0] * iw * sc - ox, bx[1] * ih * sc - oy, bx[2] * iw * sc - ox, bx[3] * ih * sc - oy)
    else:
        chh = min(int(max_w * 9 / 16), max(ih, 1080))
        cw = int(chh * 16 / 9)
        bg = ImageOps.fit(im, (cw, chh)).filter(ImageFilter.GaussianBlur(40))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.3)
        sc = (chh - 60) / ih
        fg = im.resize((int(iw * sc), int(ih * sc)), Image.LANCZOS)
        ox, oy = (cw - fg.width) // 2, (chh - fg.height) // 2
        bg.paste(fg, (ox, oy))
        canvas = bg

        def conv(bx):
            return (ox + bx[0] * iw * sc, oy + bx[1] * ih * sc, ox + bx[2] * iw * sc, oy + bx[3] * ih * sc)
    return canvas, conv


def focus_views(canvas_size, boxes_px, total_sec):
    """Камера по фото, как в документальном кино: общий план → мягкий наезд на первую цель →
    плавное смещение ко второй, к третьей (на каждой — едва заметный дрейф, кадр «живой») →
    мягкий отъезд на весь снимок. Возвращает функцию t → (x0, y0, x1, y1)."""
    cw, chh = canvas_size
    full = (0.0, 0.0, float(cw), float(chh))
    max_zoom = min(2.8, chh / (H * 0.5))             # не сильнее, чем позволяет разрешение фото
    views = []
    for (x0, y0, x1, y1) in boxes_px:
        bw, bh = max(8.0, x1 - x0), max(8.0, y1 - y0)
        vh = max(bh * 1.7, bw * 1.7 * 9 / 16, chh / max(1.0, max_zoom))
        vh = min(vh, chh * 0.8)                       # приближение всегда заметно
        vw = vh * 16 / 9
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        vx = min(max(0.0, cx - vw / 2), cw - vw)
        vy = min(max(0.0, cy - vh / 2), chh - vh)
        v = (vx, vy, vx + vw, vy + vh)
        # та же цель второй раз (почти та же рамка) — не нужна: камера стояла бы на месте
        if any(abs(v[0] - u[0]) < vw * 0.25 and abs(v[1] - u[1]) < vh * 0.25 for u in views):
            continue
        views.append(v)
    if not views:
        return lambda t: full
    T = total_sec
    intro = min(1.0, T * 0.12)
    outro = min(2.0, max(0.0, T * 0.16)) if T > 4.5 else 0.0
    body = max(0.1, T - intro - outro)
    per = body / len(views)
    move = min(1.9, per * 0.55)                      # переезд — неторопливый

    def drift(v, k):                                  # медленный наезд на 5% за время стоянки
        x0, y0, x1, y1 = v
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        z = 1 - 0.05 * k
        return (cx - (x1 - x0) / 2 * z, cy - (y1 - y0) / 2 * z, cx + (x1 - x0) / 2 * z, cy + (y1 - y0) / 2 * z)

    keys = [(0.0, full), (intro, full)]
    for i, v in enumerate(views):
        st = intro + i * per
        keys.append((st + move, ("hold", v, st + move, st + per)))
        keys.append((st + per, ("hold", v, st + move, st + per)))
    if outro:
        keys.append((T, full))

    def val(key, t):
        if isinstance(key[1], tuple) and key[1] and key[1][0] == "hold":
            _, v, a_, b_ = key[1]
            return drift(v, max(0.0, min(1.0, (t - a_) / max(1e-6, b_ - a_))))
        return key[1]

    def at(t):
        for ka, kb in zip(keys, keys[1:]):
            if t <= kb[0]:
                va, vb = val(ka, min(t, ka[0]) if False else ka[0]), val(kb, kb[0])
                if isinstance(ka[1], tuple) and ka[1] and ka[1][0] == "hold" and \
                        isinstance(kb[1], tuple) and kb[1] and kb[1][0] == "hold" and ka[1][1] == kb[1][1]:
                    return val(ka, t)                 # стоянка на цели — дрейф
                k = _ease2((t - ka[0]) / max(1e-6, kb[0] - ka[0]))
                return tuple(a + (b - a) * k for a, b in zip(va, vb))
        return val(keys[-1], T)
    return at
