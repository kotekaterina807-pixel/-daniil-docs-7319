"""Свои звуки, сделанные программой (никаких чужих файлов — никаких претензий по правам):
машинка на листах, «свуш» перед главой, вспышка и шлепок фото в стопке, шорох листа,
скрип пера и «тук» точки на карте, низкий удар при появлении человека.
Если в папке sfx лежат свои файлы typewriter*/whoosh*, берутся они."""
import random
import wave
from pathlib import Path

import numpy as np

SR = 48000


def _save(path, x):
    x = np.clip(x / (np.max(np.abs(x)) + 1e-9) * 0.9, -1, 1)
    st = np.stack([x, x], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def _onepole(x, a):
    """Простой фильтр низких частот; a — массив или число (0..1, больше — мягче)."""
    y = np.empty_like(x)
    prev = 0.0
    a = np.broadcast_to(a, x.shape)
    for i in range(len(x)):
        prev = a[i] * prev + (1 - a[i]) * x[i]
        y[i] = prev
    return y


def typewriter(path, seconds=12.0, seed=7):
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n)
    t = 0.15
    k = 0
    while t < seconds - 0.3:
        L = int(0.045 * SR)
        tt = np.arange(L) / SR
        noise = rng.standard_normal(L)
        click = np.diff(np.concatenate([[0], noise])) * np.exp(-tt / 0.004)          # сухой щелчок рычага
        body = np.sin(2 * np.pi * rng.uniform(170, 230) * tt) * np.exp(-tt / 0.018)   # удар по валику
        clack = noise * np.exp(-tt / 0.012) * 0.35
        hit = (click * 0.9 + body * 0.55 + clack) * rng.uniform(0.6, 1.0)
        i = int(t * SR)
        out[i:i + L] += hit[: n - i]
        k += 1
        if k % rng.integers(5, 9) == 0:                     # пробел/пауза между словами
            t += rng.uniform(0.18, 0.32)
        else:
            t += rng.uniform(0.075, 0.15)
    room = np.convolve(out, np.exp(-np.arange(int(0.03 * SR)) / (0.008 * SR)) * 0.12)[:n]
    _save(path, out + room)


def whoosh(path, seconds=1.4, seed=3):
    """Современный «свуш» как в новых роликах: воздух быстро нарастает, проносится слева направо
    и обрывается мягким низким ударом."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    peak = 0.62
    rise = np.clip(t / peak, 0, 1) ** 3                                 # ускоряющееся нарастание
    fall = np.exp(-np.clip(t - peak, 0, None) / 0.09)
    env = np.where(t < peak, rise, fall)
    noise = rng.standard_normal(n)
    a = 0.985 - 0.55 * np.clip(t / peak, 0, 1) ** 2                     # фильтр раскрывается — звук «светлеет»
    a = np.where(t < peak, a, 0.43 + 0.5 * (1 - fall))
    air = _onepole(noise, a)
    air = air - _onepole(air, 0.97)
    air = air / (np.max(np.abs(air)) + 1e-9) * env
    tb = np.clip(t - peak, 0, None)
    boom = np.sin(2 * np.pi * (52 * tb - 6 * tb ** 2)) * np.exp(-tb / 0.22) * (t >= peak) * 0.55
    tail = _onepole(rng.standard_normal(n), 0.9) * np.exp(-tb / 0.35) * (t >= peak) * 0.25
    mono = air + boom + tail
    pan = np.clip(t / (peak + 0.1), 0, 1)                               # пролетает слева направо
    left, right = mono * np.cos(pan * np.pi / 2), mono * np.sin(pan * np.pi / 2)
    st = np.stack([left, right], axis=1)
    st = st / (np.max(np.abs(st)) + 1e-9) * 0.9
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def flash(path, seed=11):
    """Щелчок затвора старого фотоаппарата и короткий «хлопок» вспышки."""
    rng = np.random.default_rng(seed)
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for at, amp in ((0.0, 1.0), (0.055, 0.7)):        # две створки затвора
        i = int(at * SR)
        L = int(0.03 * SR)
        tt = np.arange(L) / SR
        click = rng.standard_normal(L) * np.exp(-tt / 0.004)
        click = np.diff(np.concatenate([[0], click]))
        out[i:i + L] += click[: n - i] * amp
    tb = t - 0.07
    pop = (np.sin(2 * np.pi * (2400 * tb + 900 * tb ** 2)) * np.exp(-np.clip(tb, 0, None) / 0.12) * (tb >= 0) * 0.18)
    air = _onepole(rng.standard_normal(n), 0.6) * np.exp(-np.clip(tb, 0, None) / 0.18) * (tb >= 0) * 0.35
    _save(path, out + pop + air)


def paper_drop(path, seed=5):
    """Фото падает на стол: шорох бумаги и мягкий глухой шлепок."""
    rng = np.random.default_rng(seed)
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    slide = _onepole(rng.standard_normal(n), 0.35)
    slide = (slide - _onepole(slide, 0.9)) * np.clip(t / 0.05, 0, 1) * np.exp(-t / 0.07) * 0.6
    tb = np.clip(t - 0.04, 0, None)
    thud = np.sin(2 * np.pi * 95 * tb) * np.exp(-tb / 0.035) * (t >= 0.04)
    flap = _onepole(rng.standard_normal(n), 0.5) * np.exp(-tb / 0.02) * (t >= 0.04) * 0.5
    _save(path, slide + thud * 0.8 + flap)


def slide(path, seed=29):
    """Фото на экране: мягкий шелест — снимок скользит по сукну стола. Без щелчков и хлопков, тихо."""
    rng = np.random.default_rng(seed)
    n = int(0.75 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    band = _onepole(noise, 0.55) - _onepole(noise, 0.93)              # «бумажная» полоса частот
    env = np.sin(np.pi * np.clip(t / 0.62, 0, 1)) ** 1.6                # плавно нарастает и уходит
    tb = np.clip(t - 0.5, 0, None)
    touch = np.sin(2 * np.pi * 120 * tb) * np.exp(-tb / 0.05) * (t >= 0.5) * 0.25   # еле слышное касание
    _save(path, band * env + touch)


def drop(path, seed=31):
    """Фото ложится на стол: тёплый глухой «тук» с коротким мягким хвостом, без шороха и щелчков."""
    rng = np.random.default_rng(seed)
    n = int(0.8 * SR)
    t = np.arange(n) / SR
    f = 105 - 35 * np.clip(t / 0.12, 0, 1)                             # тон чуть опускается — «мягко»
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.09)
    tick = _onepole(rng.standard_normal(n), 0.85) * np.exp(-t / 0.006) * 0.25   # еле заметное касание
    room = _onepole(rng.standard_normal(n), 0.97) * np.exp(-t / 0.18) * 0.12
    att = np.clip(t / 0.004, 0, 1)
    _save(path, (body + tick + room) * att)


def mark(path, seed=37):
    """Точка на карте: мягкий деревянный тон (как маримба), без резкой атаки."""
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    f0 = 587.3                                                          # ре второй октавы — нейтрально, не тревожно
    x = (np.sin(2 * np.pi * f0 * t) * np.exp(-t / 0.28) + 0.35 * np.sin(2 * np.pi * f0 * 2 * t) * np.exp(-t / 0.12)
         + 0.12 * np.sin(2 * np.pi * f0 * 4.01 * t) * np.exp(-t / 0.05))
    _save(path, x * np.clip(t / 0.006, 0, 1))


def trace(path, seconds=4.0, seed=41):
    """Путь ложится на карту: ровное мягкое «дыхание» кисти по бумаге — без штрихов и скрипа."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    band = _onepole(noise, 0.75) - _onepole(noise, 0.985)               # тёплая полоса, без шипения
    swell = 0.75 + 0.25 * np.sin(2 * np.pi * 0.6 * t)
    env = np.clip(t / 0.35, 0, 1) * np.clip((seconds - t) / 0.4, 0, 1) * swell
    _save(path, band * env)


def rustle(path, seed=9):
    """Лист бумаги кладут на стол — короткий сухой шорох."""
    rng = np.random.default_rng(seed)
    n = int(0.8 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    crackle = (rng.random(n) < 0.004) * rng.standard_normal(n) * 4
    x = noise - _onepole(noise, 0.8) + crackle
    env = np.clip(t / 0.08, 0, 1) * np.exp(-t / 0.22) * (1 + 0.5 * np.sin(2 * np.pi * 9 * t))
    _save(path, _onepole(x * env, 0.25))


def pen(path, seconds=1.2, seed=13):
    """Перо скрипит по бумаге — пока рисуется пунктир маршрута."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    hp = noise - _onepole(noise, 0.9)
    strokes = 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 6.5 * t))           # штрихи пунктира
    env = np.clip(t / 0.05, 0, 1) * np.clip((seconds - t) / 0.08, 0, 1) * (0.35 + 0.65 * _onepole(strokes, 0.995))
    _save(path, _onepole(hp, 0.3) * env)


def pin(path, seed=17):
    """Точка на карте: короткий деревянный «тук»."""
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * 820 * t) * np.exp(-t / 0.018) + np.sin(2 * np.pi * 1640 * t) * np.exp(-t / 0.008) * 0.4
    _save(path, x)


def hit(path, seed=19):
    """Появление фото человека: низкий кинематографичный удар с хвостом."""
    rng = np.random.default_rng(seed)
    n = int(2.2 * SR)
    t = np.arange(n) / SR
    boom = np.sin(2 * np.pi * (48 * t - 5 * t ** 2)) * np.exp(-t / 0.5)
    body = _onepole(rng.standard_normal(n), 0.97) * np.exp(-t / 0.25) * 3
    click = rng.standard_normal(n) * np.exp(-t / 0.006) * 0.3
    _save(path, boom + body + click)


def _loopable(x, fade=1.5):
    """Конец плавно переходит в начало — звук можно повторять без щелчка."""
    k = int(fade * SR)
    head, tail = x[:k].copy(), x[-k:].copy()
    w = np.linspace(0, 1, k)
    x = x[k:].copy()
    x[-k:] = tail * (1 - w) + head * w
    return x


def _lp(x, hz):
    a = float(np.exp(-2 * np.pi * hz / SR))
    from scipy.signal import lfilter  # noqa — есть не везде
    return lfilter([1 - a], [1, -a], x)


def _lowpass(x, hz):
    try:
        return _lp(x, hz)
    except Exception:
        return _onepole(x, float(np.exp(-2 * np.pi * hz / SR)))


def engine(path, seconds=30.0, seed=43):
    """Гул поршневого самолёта изнутри: низкое «бу-бу-бу» мотора, гул винта, слабая вибрация корпуса."""
    rng = np.random.default_rng(seed)
    n = int((seconds + 1.5) * SR)
    t = np.arange(n) / SR
    f0 = 31 + 0.8 * np.sin(2 * np.pi * 0.07 * t) + 0.3 * np.sin(2 * np.pi * 0.23 * t)  # обороты чуть «плавают»
    ph = 2 * np.pi * np.cumsum(f0) / SR
    x = sum(np.sin(k * ph + k) / k ** 0.8 for k in range(1, 9))
    x *= 0.75 + 0.25 * np.sin(ph * 0.5) ** 2                       # поршни: пульсация
    prop = np.sin(2 * np.pi * np.cumsum(f0 * 2.6) / SR) * 0.35      # винт
    rumble = _lowpass(rng.standard_normal(n), 180) * 6
    air = _lowpass(rng.standard_normal(n), 900) * 0.6 * (0.8 + 0.2 * np.sin(2 * np.pi * 0.11 * t))
    y = _lowpass(x + prop, 420) + rumble + air
    _save(path, _loopable(y))


def wind(path, seconds=30.0, seed=47):
    """Ветер и шторм: воющий порыв то сильнее, то слабее, низкий шум волн."""
    rng = np.random.default_rng(seed)
    n = int((seconds + 1.5) * SR)
    t = np.arange(n) / SR
    g = 0.55 + 0.45 * np.clip(np.sin(2 * np.pi * 0.09 * t) + 0.5 * np.sin(2 * np.pi * 0.23 * t + 1), -1, 1)
    hiss = _lowpass(rng.standard_normal(n), 700) * g
    howl = np.sin(2 * np.pi * np.cumsum(380 + 140 * g) / SR) * 0.05 * g ** 2
    sea = _lowpass(rng.standard_normal(n), 120) * 5 * (0.7 + 0.3 * np.sin(2 * np.pi * 0.13 * t) ** 2)
    _save(path, _loopable(hiss + howl + sea))


def static(path, seconds=20.0, seed=53):
    """Эфир: шипение старого приёмника, треск, «плывущий» тон настройки."""
    rng = np.random.default_rng(seed)
    n = int((seconds + 1.5) * SR)
    t = np.arange(n) / SR
    hiss = rng.standard_normal(n)
    hiss = hiss - _lowpass(hiss, 1200)
    hiss = _lowpass(hiss, 5000) * 0.5
    crack = np.zeros(n)
    for _ in range(int(seconds * 9)):
        a = rng.integers(0, n - 2000)
        L = rng.integers(40, 900)
        crack[a:a + L] += rng.standard_normal(L) * rng.uniform(0.4, 2.0) * np.exp(-np.arange(L) / (L / 3))
    whistle = np.sin(2 * np.pi * np.cumsum(900 + 300 * np.sin(2 * np.pi * 0.05 * t)) / SR) * 0.04
    swell = 0.7 + 0.3 * np.sin(2 * np.pi * 0.17 * t) ** 2
    _save(path, _loopable((hiss + crack * 0.6 + whistle) * swell))


def alarm(path, seed=59):
    """Тревожный зуммер в кабине (старый электромеханический): три коротких гудка, глухо, как сквозь мотор."""
    n = int(3.2 * SR)
    t = np.arange(n) / SR
    tone = np.sign(np.sin(2 * np.pi * 440 * t)) * 0.5 + np.sign(np.sin(2 * np.pi * 330 * t)) * 0.35
    env = np.zeros(n)
    for k in range(3):
        a, b = int((0.15 + k * 0.95) * SR), int((0.15 + k * 0.95 + 0.55) * SR)
        e = np.ones(b - a)
        r = int(0.02 * SR)
        e[:r] = np.linspace(0, 1, r)
        e[-r:] = np.linspace(1, 0, r)
        env[a:b] = e
    y = _lowpass(tone * env, 1800)
    y = y - _lowpass(y, 200)
    _save(path, y)


def thunder(path, seed=61):
    """Далёкий гром: треск и долгий низкий раскат."""
    rng = np.random.default_rng(seed)
    n = int(6.0 * SR)
    t = np.arange(n) / SR
    env = (1 - np.exp(-t / 0.08)) * np.exp(-t / 1.6)
    env *= 0.6 + 0.4 * np.abs(_lowpass(rng.standard_normal(n), 3) * 40).clip(0, 1)
    y = _lowpass(rng.standard_normal(n), 140) * env * 8 + _lowpass(rng.standard_normal(n), 900) * env * np.exp(-t / 0.4)
    _save(path, y)


def ensure(sfx_dir):
    """Если своих звуков нет — делаем свои один раз."""
    sfx_dir = Path(sfx_dir)
    sfx_dir.mkdir(exist_ok=True)
    names = [p.name.lower() for p in sfx_dir.iterdir()]
    try:
        if not any(n.startswith("typewriter") for n in names):
            typewriter(sfx_dir / "typewriter_auto.wav")
        if not any(n.startswith("flash") and "_auto" not in n for n in names):   # своих вспышек нет —
            for nm, sd in (("flash_auto.wav", 11), ("flash_auto2.wav", 23), ("flash_auto3.wav", 31)):
                if nm not in names:                            # несколько вариантов — не звучит одинаково
                    flash(sfx_dir / nm, seed=sd)
        for kind, fn in (("paper", paper_drop), ("rustle", rustle), ("pen", pen), ("pin", pin), ("hit", hit),
                         ("slide", slide), ("drop", drop), ("mark", mark), ("trace", trace),
                         ("engine", engine), ("wind", wind), ("static", static), ("alarm", alarm),
                         ("thunder", thunder)):
            if not any(n.startswith(kind) for n in names):
                fn(sfx_dir / f"{kind}_auto.wav")
        (sfx_dir / "whoosh_auto.wav").unlink(missing_ok=True)        # старый «вжух» не понравился — заменён
        if not any(n.startswith("whoosh") for n in names if n != "whoosh_auto.wav"):
            whoosh(sfx_dir / "whoosh_auto2.wav")
    except Exception:
        pass
