"""Монтаж: плавное движение кадров, переходы по настроению, заставка, затемнение, звук."""
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import ffmpeg_exe

FPS = 60            # 60 кадров/с: плавные движения камеры, переходы и надписи (YouTube показывает до 60)
W, H = 1920, 1080
ZOOM = 0.08            # запас кадра под наезд/отъезд и боковой проезд камеры (8%)
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
    if max(bw / im.width, bh / im.height) > 1.25 and not blur_dark:
        # маленькое фото растянуто — мягкая резкость возвращает чёткость контуров (без «пережаренности»)
        base = base.filter(ImageFilter.UnsharpMask(radius=1.6, percent=55, threshold=3))
    if blur_dark:
        base = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(8))).enhance(0.45)
    return base


def _kb_frames(base, n, zoom_in=True, amp=ZOOM, pan=0):
    """Плавный зум без дрожания: каждый кадр считается с субпиксельной точностью,
    скорость мягко нарастает и затухает. pan = -1/1 — камера ещё и медленно проезжает вбок."""
    bw, bh = base.size
    last = max(n - 1, 1)
    full = 1 + ZOOM
    for k in range(n):
        p = k / last
        e = 0.5 * p + 0.5 * (p * p * (3 - 2 * p))      # мягкий старт и финиш
        z = 1 + amp * 0.6 + amp * 0.4 * (e if zoom_in else 1 - e) if pan else 1 + amp * (e if zoom_in else 1 - e)
        z = min(z, full)
        cw, ch = bw / z, bh / z
        x0, y0 = (bw - cw) / 2, (bh - ch) / 2
        if pan:                                           # проезд в пределах запаса кадра, без выхода за край
            x0 += pan * (e - 0.5) * (bw - cw) * 0.9
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
    pan = (-1, 0, 1)[sum(map(ord, str(src))) % 3] if total / FPS >= 3.0 else 0   # каждое третье фото — без проезда
    _pipe(_kb_frames(base, total, zoom_in, amp, pan), total, out, chain, extra)


def render_focus_shot(src, total, out, grade, boxes):
    """Фото в высоком разрешении: камера по очереди мягко приближается к целям и отъезжает в конце."""
    from . import fx
    canvas, conv = fx.focus_canvas(src)
    px = [conv(b) for b in boxes]
    at = fx.focus_views(canvas.size, px, total / FPS)
    chain = f"[0:v]{grade or 'null'},format=yuv420p[v]"

    def frames():
        for k in range(total):
            x0, y0, x1, y1 = at(k / FPS)
            yield canvas.resize((W, H), Image.BICUBIC, box=(x0, y0, x1, y1)).tobytes()
    _pipe(frames(), total, out, chain, [])


# ---------- «стопка фотографий», как в документальных фильмах ----------
def _old_print(im):
    """Отпечаток «того времени»: цвет почти уходит, тёплый сепийный тон, чуть выцветшие тени —
    даже современное цветное фото в стопке выглядит как старая карточка."""
    g = ImageOps.grayscale(im)
    sep = ImageOps.colorize(g, black=(34, 26, 20), white=(244, 232, 208), mid=(150, 126, 100))
    return Image.blend(sep, im, 0.18)


def _print_card(src, seed, max_w=1180, max_h=760):
    """Фото как старый отпечаток: белая рамка, лёгкий наклон, мягкая тень. RGBA и угол."""
    import random
    rnd = random.Random(str(seed))
    im = Image.open(src).convert("RGB")
    sc = min(max_w / im.width, max_h / im.height)
    im = im.resize((max(2, round(im.width * sc)), max(2, round(im.height * sc))), Image.LANCZOS)
    im = _old_print(im)
    bd = 18
    card = Image.new("RGB", (im.width + 2 * bd, im.height + 2 * bd + 10), (238, 232, 218))
    card.paste(im, (bd, bd))
    angle = rnd.uniform(-5.5, 5.5)
    rgba = card.convert("RGBA").rotate(angle, resample=Image.BICUBIC, expand=True)
    pad = 40
    sh = Image.new("RGBA", (rgba.width + 2 * pad, rgba.height + 2 * pad), (0, 0, 0, 0))
    alpha = rgba.split()[3].point(lambda a: 150 if a else 0)
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    black.putalpha(alpha)
    sh.alpha_composite(black, (pad + 10, pad + 14))
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    sh.alpha_composite(rgba, (pad, pad))
    dx, dy = rnd.randint(-150, 150), rnd.randint(-70, 70)
    return sh, (dx, dy)


def _stack_background(first_src):
    bg = _cover(Image.open(first_src).convert("RGB"), W, H).filter(ImageFilter.GaussianBlur(30))
    return ImageEnhance.Brightness(bg).enhance(0.38).convert("RGBA")


def render_stack_shot(prev, src, total, out, grade, seed, zoom_in=True, label=None, flash_on=True):
    """Новое фото «ложится» поверх предыдущих отпечатков: вспышка, мягкое опускание, тень.
    prev — [(src, seed)] уже лежащих фото. Потом весь стол медленно приближается."""
    first = prev[0][0] if prev else src
    bg = _stack_background(first)
    for psrc, pseed in prev[-3:]:                      # уже лежащие (не больше трёх под новым)
        card, (dx, dy) = _print_card(psrc, pseed)
        bg.alpha_composite(card, ((W - card.width) // 2 + dx, (H - card.height) // 2 + dy))
    card, (dx, dy) = _print_card(src, seed)
    x0, y0 = (W - card.width) // 2 + dx, (H - card.height) // 2 + dy
    final = bg.copy()
    final.alpha_composite(card, (x0, y0))
    final_rgb = final.convert("RGB")
    anim = int(0.38 * FPS)
    flash = int(0.15 * FPS)
    amp = 0.035 * min(1.0, max(0.4, total / FPS / 5.0))
    big = final_rgb

    def frames():
        for k in range(total):
            if k < anim:                                 # фото опускается на стол: крупнее → на место
                p = k / anim
                e = 1 - (1 - p) ** 3
                sc = 1.12 - 0.12 * e
                cw, ch = round(card.width * sc), round(card.height * sc)
                c2 = card.resize((cw, ch), Image.BICUBIC)
                if e < 1:
                    a = c2.split()[3].point(lambda v, e=e: int(v * min(1.0, 0.25 + e)))
                    c2.putalpha(a)
                fr = bg.copy()
                fr.alpha_composite(c2, (x0 + (card.width - cw) // 2, y0 + (card.height - ch) // 2 - round(18 * (1 - e))))
                img = fr.convert("RGB")
                if flash_on and k < flash:               # вспышка фотоаппарата
                    img = Image.blend(img, Image.new("RGB", (W, H), (255, 252, 240)), 0.55 * (1 - k / flash))
                yield img.tobytes()
            else:                                        # медленный наезд на «стол»
                p = (k - anim) / max(1, total - anim - 1)
                e = 0.5 * p + 0.5 * (p * p * (3 - 2 * p))
                z = 1 + amp * e                          # только наезд: продолжает «падение» фото без скачка
                bw, bh = big.size
                cw, ch = min(bw, bw / z), min(bh, bh / z)
                xx, yy = max(0.0, (bw - cw) / 2), max(0.0, (bh - ch) / 2)
                yield big.resize((W, H), Image.BICUBIC, box=(xx, yy, xx + cw, yy + ch)).tobytes()
    extra, lf = _overlay_label(label, total / FPS, 1)
    chain = f"[0:v]{grade or 'null'},format=yuv420p[v0];{lf}"
    _pipe(frames(), total, out, chain, extra)


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


def render_video_shot(src, total, out, grade, label=None, clip_dur=0, offset_n=0, speed=1.0, clip_start=None):
    need = total / FPS
    slow = 1.0
    start = 0.0
    if clip_start is not None:                          # карта/лист, разрезанные на куски, — продолжение с того же места
        start = max(0.0, float(clip_start))
    elif clip_dur and clip_dur < need:
        slow = min(1.6, need / clip_dur)                # короткий ролик слегка замедляем, а не зацикливаем
    if speed and speed < 0.99:
        slow = min(2.0, max(slow, 1.0 / speed))         # быстрый ролик — проигрываем медленнее
    elif clip_start is not None:
        pass                                            # продолжение куска — начало не трогаем (раньше здесь
        #                                                 срабатывало «пропустить начало» и карта начиналась заново)
    elif clip_dur and offset_n:
        # повторное использование ролика — берём другой его участок
        free = max(clip_dur - need, 0)
        start = (offset_n * need) % free if free > 0.5 else 0.0
    elif clip_dur and clip_dur > need + 1.0:
        start = 0.4                                     # пропускаем самое начало ролика
    extra, lf = _overlay_label(label, need, 1)
    # продолжение карты/доски/листа: без -stream_loop. В новых ffmpeg (7.x, на Windows) -ss вместе с -stream_loop
    # переставал работать — каждый кусок карты начинался заново. Хвост, если не хватает, — последний кадр.
    loop = [] if clip_start is not None else ["-stream_loop", "-1"]
    pad = "tpad=stop_mode=clone:stop_duration=30," if clip_start is not None else ""
    chain = (f"[0:v]{pad}setpts={slow:.4f}*(PTS-STARTPTS),scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={W}:{H},setsar=1,fps={FPS},{grade or 'null'},format=yuv420p[v0];{lf}")
    _run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", *loop,
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
                                 s.get("offset_n", 0), s.get("speed", 1.0), s.get("clip_start"))
            elif s["kind"] == "image" and s.get("focus_boxes"):
                render_focus_shot(s.get("focus_src") or s["src"], n, tmp, s["grade"], s["focus_boxes"])
            elif s["kind"] == "image" and s["src"] and s.get("stack") is not None:
                render_stack_shot(s["stack"], s["src"], n, tmp, s["grade"], s.get("stack_seed", s["src"]),
                                  s.get("zoom_in", True), s.get("label"), not s.get("stack_solo"))
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
    if name == "flash":                                   # короткая белая вспышка между кадрами
        out = _mix(a, b, e)
        k = max(0.0, 1 - abs(1 - 2 * p) * 1.6)
        if k > 0:
            y = out[:W * H].astype(np.float32)
            out[:W * H] = np.clip(y + (255 - y) * k * 0.85, 0, 255).astype(np.uint8)
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
        sc = FPS / 30.0                                   # тайминги анимаций заданы для 30 кадров/с
        i, n = i / sc, self.frames / sc
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
DUCK = "sidechaincompress=threshold=0.012:ratio=14:attack=12:release=1000:makeup=1"
# Музыка тише и сильнее прижимается под голос; звуки (фото, листы, карта) тише, чтобы не перебивать диктора.
VOICE_NORM = "anull"      # выравнивание голоса (loudnorm) убрано: на ПК пользователя после него пропал звук
SFX_GAIN = 0.4


def _voice_lufs(narration):
    """Громкость голоса диктора (LUFS) — музыка ставится относительно неё, а не «вслепую»."""
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(narration), "-af", "ebur128",
                            "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, creationflags=_NO_WINDOW)
        import re as _re
        m = _re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", p.stderr)
        v = float(m[-1]) if m else None
        return v if v is not None and -60 < v < 0 else None
    except Exception:
        return None


MUSIC_BELOW_VOICE = 21.0       # дБ: музыка — фоном, слышна, но голос главный (под голосом прижимается ещё сильнее)


def _music_chain(music_volume, voice_i=None):
    """Музыка на MUSIC_BELOW_VOICE дБ тише голоса; ползунок 0.12 — это уровень по умолчанию,
    0.24 — на 6 дБ громче, 0.06 — на 6 дБ тише."""
    import math as _m
    rel = 20 * _m.log10(max(0.01, float(music_volume)) / 0.12)
    target = (voice_i if voice_i is not None else -20.0) - MUSIC_BELOW_VOICE + rel
    target = max(-60.0, min(-20.0, target))
    return ("aformat=sample_rates=48000:channel_layouts=stereo,"
            f"loudnorm=I={target:.1f}:TP=-6:LRA=11,equalizer=f=2500:t=q:w=1.2:g=-4")


def _sfx_track(sfx, total_sec, out, gain=1.0):
    """Звуковые эффекты (их может быть сотни на 30 минут) раскладываются в одну дорожку заранее:
    один вход для FFmpeg вместо сотен."""
    sr = 48000
    n = int((total_sec + 2) * sr)
    buf = np.zeros((n, 2), dtype=np.float32)
    cache = {}
    for e in sfx:
        f = e["file"]
        if f not in cache:
            p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", f, "-f", "f32le",
                                "-ac", "2", "-ar", str(sr), "-"], capture_output=True, timeout=120,
                               creationflags=_NO_WINDOW)
            cache[f] = np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, 2) if p.stdout else None
        src = cache[f]
        if src is None or not len(src):
            continue
        L = int(e["dur"] * sr)
        reps = int(np.ceil(L / len(src)))                 # короткий звук (машинка) повторяется на всю длину
        clip = np.tile(src, (max(1, reps), 1))[:L].copy()
        fi = int(min(L, e.get("fade_in", 0) * sr))
        if fi > 1:
            clip[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
        fd = int(min(L, e.get("fade", 0.4) * sr))
        if fd > 1:
            clip[-fd:] *= np.linspace(1, 0, fd, dtype=np.float32)[:, None]
        a = int(e["at"] * sr)
        if a >= n:
            continue
        clip = clip[: n - a]
        buf[a:a + len(clip)] += clip * float(e["vol"]) * SFX_GAIN * gain
    if not np.any(buf):
        return None
    import wave
    pcm = (np.clip(buf, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out


LOUD = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _run_loud(cmd, filt, out, total_sec):
    """Громкость всего ролика — ровно (линейно), в два прохода. Раньше loudnorm работал «на лету»: в паузах
    диктора он сам поднимал громкость — музыка и звуки между фразами становились громче голоса."""
    lin = None
    try:
        meas = [c if c != "error" else "info" for c in cmd]
        p = subprocess.run(meas + ["-filter_complex", filt.replace(LOUD, LOUD + ":print_format=json"),
                                   "-map", "[a]", "-t", f"{total_sec:.3f}", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                           creationflags=_NO_WINDOW)
        import json as _json
        import re as _re
        m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr)
        if m:
            d = _json.loads(m.group(0))
            lin = (f"loudnorm=I=-16:TP=-1.5:LRA=20:measured_I={d['input_i']}:measured_TP={d['input_tp']}:"
                   f"measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}:"
                   f"offset={d['target_offset']}:linear=true,alimiter=limit=0.89")
    except Exception:
        lin = None
    cmd = list(cmd) + ["-filter_complex", filt.replace(LOUD, lin) if lin else filt, "-map", "0:v", "-map", "[a]",
                       "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total_sec:.3f}",
                       "-movflags", "+faststart", str(out)]
    _run(cmd)


def _final_mix_sfx(cmd, music, out, total_sec, music_volume, sfx, fade_out_start):
    cmd = list(cmd)
    idx = 2
    parts = [f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,{VOICE_NORM},asplit=2[n1][n2]"]
    voice_i = _voice_lufs(cmd[cmd.index("-i", cmd.index("-i") + 1) + 1])
    sfx_gain = 10 ** (((voice_i if voice_i is not None else -20.0) + 20.0) / 20)   # звуки — относительно голоса
    mix = ["[n1]"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"[{idx}:a]{_music_chain(music_volume, voice_i)}[m]")
        parts.append(f"[m][n2]{DUCK}[duck]")
        mix.append("[duck]")
        idx += 1
    else:
        parts.append("[n2]anullsink")
    track = _sfx_track(sfx, total_sec, Path(out).with_suffix(".sfx.wav"), sfx_gain)   # все звуки — одной дорожкой
    if track:
        cmd += ["-i", str(track)]
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo[sx]")
        mix.append("[sx]")
        idx += 1
    parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,"
                 f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=1.5,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    _run_loud(cmd, ";".join(parts), out, total_sec)


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
        filt = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,{VOICE_NORM},asplit=2[n1][n2];"
                f"[2:a]{_music_chain(music_volume, _voice_lufs(narration))}[m];"
                f"[m][n2]{DUCK}[duck];"
                f"[n1][duck]amix=inputs=2:duration=first:normalize=0,"
                f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=1.5,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    else:
        filt = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={fade_out_start:.2f}:d=3[a]")
    _run_loud(cmd, filt, out, total_sec)


def audio_level(path):
    """Средняя громкость и пик звука готового файла в дБ (None, None — звуковой дорожки нет или ffmpeg не ответил)."""
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, creationflags=_NO_WINDOW)
    except Exception:
        return None, None
    import re as _re
    m = _re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", p.stderr)
    x = _re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", p.stderr)
    num = lambda g: (-99.0 if g.group(1) == "-inf" else float(g.group(1))) if g else None
    return num(m), num(x)
