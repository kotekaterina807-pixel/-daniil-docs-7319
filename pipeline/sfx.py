"""Свои звуки, сделанные программой (никаких чужих файлов — никаких претензий по правам):
стук печатной машинки для листов с цитатами и «вжух» для перехода к новой главе.
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


def ensure(sfx_dir):
    """Если своих звуков нет — делаем свои один раз."""
    sfx_dir = Path(sfx_dir)
    sfx_dir.mkdir(exist_ok=True)
    names = [p.name.lower() for p in sfx_dir.iterdir()]
    try:
        if not any(n.startswith("typewriter") for n in names):
            typewriter(sfx_dir / "typewriter_auto.wav")
        if not any(n.startswith("flash") for n in names):
            flash(sfx_dir / "flash_auto.wav")
        (sfx_dir / "whoosh_auto.wav").unlink(missing_ok=True)        # старый «вжух» не понравился — заменён
        if not any(n.startswith("whoosh") for n in names if n != "whoosh_auto.wav"):
            whoosh(sfx_dir / "whoosh_auto2.wav")
    except Exception:
        pass
