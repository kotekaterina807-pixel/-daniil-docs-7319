"""Синхронизация с речью: поиск пауз в озвучке, время каждого предложения, точки смены кадров."""
import re
import wave

import numpy as np

SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def sentences(text):
    """Предложения; цитата в «кавычках» не разрывается, даже если внутри неё точка."""
    out = []
    for s in (x for x in SENT_SPLIT.split(text.strip()) if x):
        if out and out[-1].count("«") > out[-1].count("»"):
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def find_pauses(wav_path, min_len=0.12):
    """Возвращает список пауз (середина_сек, длина_сек) внутри речи."""
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if data.size == 0:
        return [], 0.0
    hop = int(sr * 0.01)                     # окно 10 мс
    n = data.size // hop
    rms = np.sqrt(np.mean(data[:n * hop].reshape(n, hop) ** 2, axis=1) + 1e-9)
    loud = np.percentile(rms, 90) if n else 1.0
    silent = rms < max(loud * 0.06, 60.0)    # тишина: ниже ~ -24 дБ от громкой речи
    pauses, start = [], None
    for i, s in enumerate(silent):
        if s and start is None:
            start = i
        elif not s and start is not None:
            length = (i - start) * 0.01
            if length >= min_len and start > 0:      # паузы в самом начале не считаем
                pauses.append(((start + i) / 2 * 0.01, length))
            start = None
    return pauses, data.size / sr


def sentence_times(text, wav_path):
    """Начало и конец каждого предложения (сек, от начала абзаца) + все паузы абзаца."""
    sents = sentences(text)
    pauses, dur = find_pauses(wav_path)
    if len(sents) <= 1 or dur <= 0:
        return [(0.0, dur, text)], pauses, dur
    total = sum(len(s) for s in sents)
    bounds, acc = [], 0
    for s in sents[:-1]:
        acc += len(s)
        bounds.append(dur * acc / total)
    # каждую расчётную границу притягиваем к ближайшей длинной паузе
    used, snapped = set(), []
    for b in bounds:
        best, best_score = None, None
        for j, (t, ln) in enumerate(pauses):
            if j in used or abs(t - b) > 1.5:
                continue
            score = abs(t - b) - ln * 2.0           # длинная пауза важнее близости
            if best_score is None or score < best_score:
                best, best_score = j, score
        if best is not None:
            used.add(best)
            snapped.append(pauses[best][0])
        else:
            snapped.append(b)
    snapped = sorted(snapped)
    edges = [0.0] + snapped + [dur]
    return [(edges[i], edges[i + 1], sents[i]) for i in range(len(sents))], pauses, dur


def cut_points(span, count, pauses, sent_bounds, min_shot=2.0):
    """Точки смены кадров внутри абзаца (сек). Режет только в паузах речи, предпочитая границы предложений."""
    if count <= 1:
        return []
    shot = span / count
    cuts, prev = [], 0.0
    cand = [(t, ln + (0.3 if any(abs(t - b) < 0.05 for b in sent_bounds) else 0.0)) for t, ln in pauses]
    for k in range(1, count):
        ideal = shot * k
        best, best_score = None, None
        for t, weight in cand:
            if t - prev < min_shot or span - t < min_shot or abs(t - ideal) > shot * 0.45:
                continue
            score = abs(t - ideal) / shot - weight
            if best_score is None or score < best_score:
                best, best_score = t, score
        if best is None:
            # паузы рядом нет — режем в расчётной точке (смена картинки не обязана совпадать с паузой)
            if ideal - prev >= min_shot and span - ideal >= min_shot:
                best = ideal
            else:
                continue
        cuts.append(best)
        prev = best
    return cuts
