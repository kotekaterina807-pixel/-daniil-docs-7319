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


def whoosh(path, seconds=1.6, seed=3):
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    tt = np.linspace(0, 1, n)
    noise = rng.standard_normal(n)
    a = 0.995 - 0.12 * np.sin(np.pi * tt) ** 2                                       # фильтр открывается и закрывается
    x = _onepole(noise, a)
    x = x - _onepole(x, 0.995)                                                       # убираем гул
    env = np.sin(np.pi * np.clip(tt * 1.15, 0, 1)) ** 2
    _save(path, x * env)


def ensure(sfx_dir):
    """Если своих звуков нет — делаем свои один раз."""
    sfx_dir = Path(sfx_dir)
    sfx_dir.mkdir(exist_ok=True)
    names = [p.name.lower() for p in sfx_dir.iterdir()]
    try:
        if not any(n.startswith("typewriter") for n in names):
            typewriter(sfx_dir / "typewriter_auto.wav")
        if not any(n.startswith("whoosh") for n in names):
            whoosh(sfx_dir / "whoosh_auto.wav")
    except Exception:
        pass
