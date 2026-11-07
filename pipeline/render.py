"""Монтаж: плавное движение кадров, переходы по настроению, заставка, затемнение, звук."""
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from .config import ffmpeg_exe

FPS = 30
W, H = 1920, 1080
ZOOM = 0.06            # насколько мягко камера приближается/отдаляется за кадр (6%)
_COMMON = ["-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", "15360", "-an"]
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", *_COMMON]
# Видеокарта NVIDIA: аппаратное кодирование, в разы быстрее процессора, качество сопоставимое
NVENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
         "-spatial-aq", "1", *_COMMON]
_nvenc_ok = None


def gpu_available():
    """Проверяет один раз, работает ли кодирование на видеокарте NVIDIA."""
    global _nvenc_ok
    if _nvenc_ok is None:
        try:
            p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                "-i", f"color=c=black:s={W}x{H}:r={FPS}", "-frames:v", "5", *NVENC,
                                "-f", "null", "-"], capture_output=True, timeout=60, creationflags=_NO_WINDOW)
            _nvenc_ok = p.returncode == 0
        except Exception:
            _nvenc_ok = False
    return _nvenc_ok


def disable_gpu():
    global _nvenc_ok
    _nvenc_ok = False


def enc():
    return NVENC if gpu_available() else X264

# для Windows: не показывать чёрные окна консоли при запуске ffmpeg
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class RenderError(Exception):
    pass


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=_NO_WINDOW)
    if p.returncode != 0:
        raise RenderError(p.stderr[-1500:])


# ---------------- подготовка изображения ----------------
def _cover(im, w, h):
    scale = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * scale)), max(h, round(im.height * scale))), Image.LANCZOS)
    left, top = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((left, top, left + w, top + h))


def prepare_base(path, blur_dark=False):
    """Кадр с запасом под зум. Вертикальные фото — целиком по центру на размытом фоне."""
    bw, bh = round(W * (1 + ZOOM)) + 2, round(H * (1 + ZOOM)) + 2
    im = Image.open(path).convert("RGB")
    if im.width / im.height >= 1.5:
        base = _cover(im, bw, bh)
    else:
        bg = _cover(im, bw, bh).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.75)
        fh = bh - 70
        fw = round(im.width * fh / im.height)
        if fw > bw - 70:
            fw, fh = bw - 70, round(im.height * (bw - 70) / im.width)
        fg = im.resize((fw, fh), Image.LANCZOS)
        bg.paste(fg, ((bw - fw) // 2, (bh - fh) // 2))
        base = bg
    if blur_dark:
        base = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(8))).enhance(0.45)
    return base


def _kb_frames(base, n, zoom_in=True, amp=ZOOM):
    """Плавный зум без дрожания: каждый кадр считается с субпиксельной точностью,
    скорость мягко нарастает и затухает. Камера не уезжает в сторону."""
    bw, bh = base.size
    last = max(n - 1, 1)
    for k in range(n):
        p = k / last
        e = 0.5 * p + 0.5 * (p * p * (3 - 2 * p))      # мягкий старт и финиш
        z = 1 + amp * (e if zoom_in else 1 - e)
        cw, ch = bw / z, bh / z
        x0, y0 = (bw - cw) / 2, (bh - ch) / 2
        # box с дробными координатами — субпиксельная точность, поэтому нет дрожания
        yield base.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)).tobytes()


def _overlay_label(label, dur, idx):
    """Всплывающая надпись: плавно проявляется и выезжает на 45 px, держится, исчезает."""
    if not label or dur < 3.0:
        return [], f"[v0]null[v]"
    st = 0.6
    end = min(dur - 0.4, st + 4.5)
    fo = max(st + 0.7, end - 0.6)
    filt = (f"[{idx}:v]format=rgba,fade=t=in:st={st}:d=0.6:alpha=1,"
            f"fade=t=out:st={fo:.2f}:d=0.6:alpha=1[lb];"
            f"[v0][lb]overlay=x='-45*pow(min(1,max(0,1-(t-{st})/0.8)),2)':y=0,format=yuv420p[v]")
    return ["-loop", "1", "-framerate", str(FPS), "-i", str(label)], filt


def _pipe(frames_iter, total, out, chain, extra_inputs):
    """Отправляет кадры в ffmpeg через канал и кодирует с фильтрами."""
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           *extra_inputs, "-filter_complex", chain, "-map", "[v]", "-frames:v", str(total), *enc(), str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        for buf in frames_iter:
            p.stdin.write(buf)
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = p.stderr.read().decode("utf-8", "replace")
    if p.wait() != 0:
        raise RenderError(err[-1500:])


# ---------------- типы кадров ----------------
def render_image_shot(src, total, out, grade, zoom_in=True, label=None):
    base = prepare_base(src)
    extra, lf = _overlay_label(label, total / FPS, 1)
    chain = f"[0:v]{grade or 'null'},format=yuv420p[v0];{lf}"
    amp = ZOOM * min(1.0, max(0.4, total / FPS / 5.0))   # короткий кадр — движение медленнее и мягче
    _pipe(_kb_frames(base, total, zoom_in, amp), total, out, chain, extra)


def motion_score(path, seconds=8):
    """Насколько «быстрый» ролик: медиана разницы соседних кадров (8 кадров/с, 96×54, лёгкое размытие).
    Ориентиры: спокойный кадр 0–3, плавная панорама 3–6, быстрая камера/таймлапс 7 и выше."""
    import numpy as np
    w, h = 96, 54
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-t", str(seconds), "-i", str(path),
                            "-vf", f"fps=8,scale={w}:{h},gblur=sigma=1,format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=120, creationflags=_NO_WINDOW)
        a = np.frombuffer(p.stdout, dtype=np.uint8)
        n = a.size // (w * h)
        if n < 3:
            return 0.0
        f = a[: n * w * h].reshape(n, h, w).astype(np.int16)
        return float(np.median(np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))))
    except Exception:
        return 0.0


def render_video_shot(src, total, out, grade, label=None, clip_dur=0, offset_n=0, speed=1.0):
    need = total / FPS
    slow = 1.0
    start = 0.0
    if clip_dur and clip_dur < need:
        slow = min(1.6, need / clip_dur)                # короткий ролик слегка замедляем, а не зацикливаем
    if speed and speed < 0.99:
        slow = min(2.0, max(slow, 1.0 / speed))         # быстрый ролик — проигрываем медленнее
    elif clip_dur and offset_n:
        # повторное использование ролика — берём другой его участок
        free = max(clip_dur - need, 0)
        start = (offset_n * need) % free if free > 0.5 else 0.0
    elif clip_dur and clip_dur > need + 1.0:
        start = 0.4                                     # пропускаем самое начало ролика
    extra, lf = _overlay_label(label, need, 1)
    chain = (f"[0:v]setpts={slow:.4f}*(PTS-STARTPTS),scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={W}:{H},setsar=1,fps={FPS},{grade or 'null'},format=yuv420p[v0];{lf}")
    _run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-stream_loop", "-1",
          "-i", str(src), *extra, "-filter_complex", chain, "-map", "[v]", "-frames:v", str(total), *enc(), str(out)])


def render_card(src, total, out):
    base = prepare_base(src)
    _pipe(_kb_frames(base, total, True, 0.03), total, out, "[0:v]format=yuv420p[v]", [])


def render_title(bg, title_png, total, out):
    """Заставка: размытый затемнённый фон, название проявляется и мягко поднимается на место."""
    base = (prepare_base(bg, blur_dark=True) if bg else
            Image.new("RGB", (round(W * (1 + ZOOM)) + 2, round(H * (1 + ZOOM)) + 2), (10, 10, 12)))
    dur = total / FPS
    st, fo = 0.5, max(1.6, dur - 1.1)
    chain = (f"[0:v]format=yuv420p[bg];"
             f"[1:v]format=rgba,fade=t=in:st={st}:d=1.3:alpha=1,fade=t=out:st={fo:.2f}:d=0.9:alpha=1[tt];"
             f"[bg][tt]overlay=x=0:y='22*pow(max(0,1-(t-{st})/1.8),2)',format=yuv420p[v]")
    _pipe(_kb_frames(base, total, True, 0.04), total, out, chain,
          ["-loop", "1", "-framerate", str(FPS), "-i", str(title_png)])


def _black(total, out):
    _run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
          "-i", f"color=c=black:s={W}x{H}:r={FPS}", "-frames:v", str(total), *enc(), str(out)])


def render_shots(shots, workers, log=print, on_progress=lambda d, t: None):
    """shots: {kind, src, frames, tail, out, grade, label, zoom_in, clip_dur, title_png}."""
    todo = [s for s in shots if not (Path(s["out"]).exists() and Path(s["out"]).stat().st_size > 1000)]
    total = len(shots)
    done = [total - len(todo)]

    def work(s):
        n = s["frames"] + s["tail"]
        tmp = str(s["out"]) + ".part.mp4"
        try:
            if s["kind"] == "title":
                render_title(s["src"], s["title_png"], n, tmp)
            elif s["kind"] == "card":
                render_card(s["src"], n, tmp)
            elif s["kind"] == "video":
                render_video_shot(s["src"], n, tmp, s["grade"], s.get("label"), s.get("clip_dur", 0),
                                 s.get("offset_n", 0), s.get("speed", 1.0))
            elif s["kind"] == "image" and s["src"]:
                render_image_shot(s["src"], n, tmp, s["grade"], s.get("zoom_in", True), s.get("label"))
            else:
                _black(n, tmp)
            os.replace(tmp, s["out"])
        except RenderError as e:
            if gpu_available() and "nvenc" in str(e).lower():
                disable_gpu()
                log("  Видеокарта не справилась с кодированием — переключаюсь на процессор.")
                return work(s)
            log(f"  Кадр не собрался ({Path(s['src']).name if s['src'] else 'пусто'}), заменяю затемнением.")
            _black(n, s["out"])
        except Exception:
            log(f"  Кадр не собрался ({Path(s['src']).name if s['src'] else 'пусто'}), заменяю затемнением.")
            _black(n, s["out"])
        done[0] += 1
        on_progress(done[0], total)
        if done[0] % 10 == 0 or done[0] == total:
            log(f"  Смонтировано кадров: {done[0]}/{total}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))


# ---------------- склейка с переходами ----------------
# Переходы считаются в программе (numpy), а FFmpeg только читает готовые кадры и кодирует результат.
# Фильтр xfade не используется: в разных версиях FFmpeg он ведёт себя по-разному.
_FRAME = W * H * 3 // 2          # кадр yuv420p: яркость W*H + две цветовые плоскости по W*H/4
_BLACK = None


def _black_frame():
    global _BLACK
    if _BLACK is None:
        _BLACK = np.empty(_FRAME, np.uint8)
        _BLACK[:W * H] = 16          # чёрный в видеодиапазоне
        _BLACK[W * H:] = 128
    return _BLACK


class _Reader:
    """Читает кадры готового фрагмента. Если файл короче ожидаемого — повторяет последний кадр."""

    def __init__(self, path):
        self.err = tempfile.TemporaryFile()
        self.p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                                   "-vf", f"scale={W}:{H},format=yuv420p", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                                  stdout=subprocess.PIPE, stderr=self.err, creationflags=_NO_WINDOW)
        self.last = _black_frame()

    def read(self):
        buf = self.p.stdout.read(_FRAME) if self.p else b""
        if buf and len(buf) == _FRAME:
            self.last = np.frombuffer(buf, np.uint8)
        return self.last

    def close(self):
        if self.p:
            try:
                self.p.stdout.close()
            except OSError:
                pass
            try:
                self.p.kill()
            except OSError:
                pass
            self.p.wait()
            self.p = None
        self.err.close()


def _planes(f):
    y = f[:W * H].reshape(H, W)
    u = f[W * H:W * H + W * H // 4].reshape(H // 2, W // 2)
    v = f[W * H + W * H // 4:].reshape(H // 2, W // 2)
    return y, u, v


def _join(y, u, v):
    return np.concatenate([y.ravel(), u.ravel(), v.ravel()]).astype(np.uint8)


def _mix(a, b, p):
    return (a.astype(np.float32) * (1 - p) + b.astype(np.float32) * p + 0.5).astype(np.uint8)


def _hblur(plane, r):
    """Горизонтальное размытие (скользящее среднее), r — радиус в пикселях."""
    if r < 1:
        return plane.astype(np.float32)
    x = np.pad(plane.astype(np.float32), ((0, 0), (r + 1, r)), mode="edge")
    c = np.cumsum(x, axis=1)
    return (c[:, 2 * r + 1:] - c[:, :-2 * r - 1]) / (2 * r + 1)


_CIRCLE = {}


def _circle_mask(p):
    if "d" not in _CIRCLE:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt((xx - W / 2) ** 2 + (yy - H / 2) ** 2)
        _CIRCLE["d"] = d
        _CIRCLE["dc"] = d[::2, ::2]
    rmax = float(np.sqrt((W / 2) ** 2 + (H / 2) ** 2)) + 60
    r = p * rmax
    m = np.clip((r - _CIRCLE["d"]) / 60 + 0.5, 0, 1)
    mc = np.clip((r - _CIRCLE["dc"]) / 60 + 0.5, 0, 1)
    return m, mc


def _transition(name, a, b, p):
    """p от 0 до 1: кадр A плавно сменяется кадром B."""
    e = p * p * (3 - 2 * p)                               # мягкое ускорение и замедление
    if name == "fadeblack":
        return _mix(a, _black_frame(), min(1, 2 * e)) if e < 0.5 else _mix(_black_frame(), b, 2 * e - 1)
    if name == "fadegrays":
        out = _mix(a, b, e)
        sat = abs(1 - 2 * e)                                  # в середине перехода — чёрно-белое
        out[W * H:] = (128 + (out[W * H:].astype(np.float32) - 128) * sat + 0.5).astype(np.uint8)
        return out
    if name == "circleopen":
        m, mc = _circle_mask(e)
        ya, ua, va = _planes(a)
        yb, ub, vb = _planes(b)
        return _join(ya * (1 - m) + yb * m + 0.5, ua * (1 - mc) + ub * mc + 0.5, va * (1 - mc) + vb * mc + 0.5)
    if name == "hblur":
        r = int(40 * (1 - abs(1 - 2 * e)))                   # размытие сильнее всего в середине
        ya, ua, va = _planes(a)
        yb, ub, vb = _planes(b)
        y = _hblur(ya, r) * (1 - e) + _hblur(yb, r) * e
        u = _hblur(ua, r // 2) * (1 - e) + _hblur(ub, r // 2) * e
        v = _hblur(va, r // 2) * (1 - e) + _hblur(vb, r // 2) * e
        return _join(y + 0.5, u + 0.5, v + 0.5)
    return _mix(a, b, e)                                    # fade и всё остальное — наплыв



# ---------------- надписи поверх видео (добавляются при склейке) ----------------
def _avg2(a):
    return (a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2]) * 0.25


class _Overlay:
    """Прозрачный PNG 1920x1080, переведённый в YUV (BT.601, видеодиапазон) — только его непустая часть."""

    def __init__(self, png, start, frames, anim):
        im = Image.open(png).convert("RGBA")
        if im.size != (W, H):
            im = im.resize((W, H), Image.LANCZOS)
        rgba = np.asarray(im).astype(np.float32) / 255.0
        a = rgba[..., 3]
        ys, xs = np.nonzero(a > 0.003)
        if len(xs) == 0:
            self.empty = True
            return
        self.empty = False
        pad = 80                                           # запас под выезд
        x0, x1 = max(0, xs.min() - 2) // 2 * 2, min(W, xs.max() + 4) // 2 * 2
        y0, y1 = max(0, ys.min() - pad // 2) // 2 * 2, min(H, ys.max() + pad // 2) // 2 * 2
        r, g, b = rgba[y0:y1, x0:x1, 0], rgba[y0:y1, x0:x1, 1], rgba[y0:y1, x0:x1, 2]
        a = a[y0:y1, x0:x1]
        yy = 16 + 65.481 * r + 128.553 * g + 24.966 * b
        uu = 128 - 37.797 * r - 74.203 * g + 112.0 * b
        vv = 128 + 112.0 * r - 93.786 * g - 18.214 * b
        self.x, self.y = x0, y0
        self.Y, self.A = yy * a, a
        self.Ac = _avg2(a)
        self.U, self.V = _avg2(uu * a), _avg2(vv * a)
        self.start, self.frames, self.anim = start, frames, anim

    def state(self, i):
        """(прозрачность 0..1, сдвиг x, сдвиг y) для кадра i от начала надписи."""
        n = self.frames
        if self.anim == "label":                          # выезд слева за 90 кадров, мягкое затухание
            p = min(1.0, i / 90.0)
            ease = 1 - (1 - p) ** 3
            k = min(1.0, i / 45.0)
            k = k * k * (3 - 2 * k)
            out = n - i
            if out < 24:
                k *= max(0.0, out / 24.0)
            return k, -70.0 * (1 - ease), 0.0
        if self.anim == "title":                          # проявление с лёгким подъёмом
            p = min(1.0, i / 50.0)
            ease = 1 - (1 - p) ** 3
            k = min(1.0, i / 36.0)
            out = n - i
            if out < 30:
                k *= max(0.0, out / 30.0)
            return k, 0.0, 24.0 * (1 - ease)
        k = min(1.0, i / 12.0, max(0.0, (n - i) / 12.0))  # fade — название главы
        return k, 0.0, 0.0


def _shift(a, dx, dy):
    """Сдвиг массива на дробное число пикселей (билинейно) — движение без ступенек."""
    ix, iy = int(np.floor(dx)), int(np.floor(dy))
    fx, fy = dx - ix, dy - iy
    if fx:
        a = a * (1 - fx) + np.pad(a, ((0, 0), (1, 0)))[:, :-1] * fx
    if fy:
        a = a * (1 - fy) + np.pad(a, ((1, 0), (0, 0)))[:-1, :] * fy
    return a, ix, iy


def _blend_plane(plane, pre, alpha, x, y, k):
    h, w = alpha.shape
    ph, pw = plane.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(pw, x + w), min(ph, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sl = (slice(y0 - y, y1 - y), slice(x0 - x, x1 - x))
    bg = plane[y0:y1, x0:x1].astype(np.float32)
    a = alpha[sl] * k
    plane[y0:y1, x0:x1] = np.clip(bg * (1 - a) + pre[sl] * k + 0.5, 0, 255).astype(np.uint8)


def _apply_overlay(f, ov, i):
    k, dx, dy = ov.state(i)
    if k <= 0.002:
        return f
    f = np.array(f, dtype=np.uint8, copy=True)
    y, u, v = _planes(f)
    Y, ix, iy = _shift(ov.Y, dx, dy)
    A, _, _ = _shift(ov.A, dx, dy)
    _blend_plane(y, Y, A, ov.x + ix, ov.y + iy, k)
    U, cx, cy = _shift(ov.U, dx / 2, dy / 2)
    V, _, _ = _shift(ov.V, dx / 2, dy / 2)
    Ac, _, _ = _shift(ov.Ac, dx / 2, dy / 2)
    _blend_plane(u, U, Ac, ov.x // 2 + cx, ov.y // 2 + cy, k)
    _blend_plane(v, V, Ac, ov.x // 2 + cx, ov.y // 2 + cy, k)
    return f


def _fade_black(f, k):
    """k=1 — кадр как есть, k=0 — чёрный."""
    return f if k >= 1 else _mix(_black_frame(), f, max(0.0, k))


def _assemble_once(shots, out, on_progress, overlays=(), fade_in=1.2, fade_out=2.0):
    n = len(shots)
    ovs = [o for o in (_Overlay(**x) for x in overlays) if not o.empty]
    total = sum(s["frames"] for s in shots) + shots[-1]["tail"]
    fi, fo = int(fade_in * FPS), int(fade_out * FPS)
    err = tempfile.TemporaryFile()
    enc_p = subprocess.Popen([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                              "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                              "-frames:v", str(total), *enc(), str(out)],
                             stdin=subprocess.PIPE, stderr=err, creationflags=_NO_WINDOW)
    written = [0]

    def put(f):
        i = written[0]
        for o in ovs:
            if o.start <= i < o.start + o.frames:
                f = _apply_overlay(f, o, i - o.start)
        if i < fi:
            f = _fade_black(f, i / fi)
        elif i >= total - fo:
            f = _fade_black(f, (total - 1 - i) / fo)
        enc_p.stdin.write(f.tobytes() if isinstance(f, np.ndarray) else f)
        written[0] += 1
        if written[0] % 300 == 0:
            on_progress(written[0], total)

    cur = _Reader(shots[0]["out"])
    t_in = 0
    try:
        for k, s in enumerate(shots):
            # часть кадра без переходов
            for _ in range(s["frames"] - t_in):
                put(cur.read())
            if k == n - 1:
                for _ in range(s["tail"]):
                    put(cur.read())
                break
            nxt = _Reader(shots[k + 1]["out"])
            t = min(s["tail"], shots[k + 1]["frames"])
            for j in range(t):
                put(_transition(s.get("trans", "fade"), cur.read(), nxt.read(), (j + 1) / (t + 1)))
            cur.close()
            cur, t_in = nxt, t
        enc_p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    finally:
        cur.close()
    code = enc_p.wait()
    err.seek(0)
    msg = err.read().decode("utf-8", "replace")
    err.close()
    if code != 0 or written[0] != total:
        raise RenderError(msg[-1500:] or f"склейка остановилась на кадре {written[0]} из {total}")
    on_progress(total, total)


def assemble(shots, out, workdir, log=print, group_size=20, on_progress=lambda d, t: None, overlays=()):
    """Склейка всех кадров с переходами, затемнение в начале (1,2 с) и в конце (2 с).
    Длительность каждого кадра не меняется, поэтому видео совпадает с озвучкой."""
    tmp = str(out) + ".part.mp4"
    try:
        _assemble_once(shots, tmp, on_progress, overlays)
    except RenderError as e:
        if gpu_available() and "nvenc" in str(e).lower():
            disable_gpu()
            log("  Видеокарта не справилась с кодированием — склеиваю на процессоре.")
            _assemble_once(shots, tmp, on_progress, overlays)
        else:
            raise
    os.replace(tmp, out)


# Музыка: каждый трек сначала выравнивается по громкости (тихий и громкий трек звучат одинаково),
# в ней чуть приглушается диапазон голоса (2–3 кГц), чтобы слова звучали чище, и она мягко уходит
# вниз, пока говорит диктор, и плавно возвращается в паузах. Итог: музыка на ~15–18 дБ тише голоса.
DUCK = "sidechaincompress=threshold=0.05:ratio=4:attack=40:release=900:makeup=1"


def _music_chain(music_volume):
    k = max(0.0, float(music_volume)) / 0.12 * 0.5          # ползунок 0.12 (по умолчанию) = нормальный уровень
    return ("aformat=sample_rates=48000:channel_layouts=stereo,loudnorm=I=-24:TP=-4:LRA=11,"
            f"equalizer=f=2500:t=q:w=1.2:g=-4,volume={k:.3f}")


def _final_mix_sfx(cmd, music, out, total_sec, music_volume, sfx, fade_out_start):
    cmd = list(cmd)
    idx = 2
    parts = ["[1:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[n1][n2]"]
    mix = ["[n1]"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"[{idx}:a]{_music_chain(music_volume)}[m]")
        parts.append(f"[m][n2]{DUCK}[duck]")
        mix.append("[duck]")
        idx += 1
    else:
        parts.append("[n2]anullsink")
    for k, e in enumerate(sfx):
        cmd += ["-stream_loop", "-1", "-i", e["file"]]
        ms = int(e["at"] * 1000)
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{e['dur']:.2f},"
                     f"afade=t=out:st={max(0.0, e['dur'] - 0.4):.2f}:d=0.4,volume={e['vol']},"
                     f"adelay={ms}|{ms}[s{k}]")
        mix.append(f"[s{k}]")
        idx += 1
    parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,"
                 f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:d=1.5,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total_sec:.3f}",
            "-movflags", "+faststart", str(out)]
    _run(cmd)


def final_mix(video, narration, music, out, total_sec, music_volume=0.12, sfx=None):
    fade_out_start = max(total_sec - 3, 0)
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(narration)]
    if sfx:
        try:
            return _final_mix_sfx(cmd, music, out, total_sec, music_volume, sfx, fade_out_start)
        except Exception:
            pass                                     # звуки не сложились — сводим как раньше
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        filt = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[n1][n2];"
                f"[2:a]{_music_chain(music_volume)}[m];"
                f"[m][n2]{DUCK}[duck];"
                f"[n1][duck]amix=inputs=2:duration=first:normalize=0,"
                f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:d=1.5,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    else:
        filt = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    cmd += ["-filter_complex", filt, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total_sec:.3f}",
            "-movflags", "+faststart", str(out)]
    _run(cmd)
