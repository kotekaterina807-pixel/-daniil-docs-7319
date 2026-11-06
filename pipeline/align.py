"""Точная привязка кадров к речи: распознаём озвучку (Vosk, офлайн, бесплатно) и узнаём,
в какую секунду звучит каждое слово сценария. По этим временам меняются кадры, появляются фото людей и подписи.

Модель распознавания (~45 МБ) скачивается один раз при первой сборке.
Если что-то не получилось (нет модели, нет пакета) — программа работает по-старому, по расчёту времени."""
import difflib
import json
import os
import re
import subprocess
import threading
import zipfile
from pathlib import Path

import requests

from .config import ROOT, ffmpeg_exe

MODELS = {
    "ru": "vosk-model-small-ru-0.22",
    "pl": "vosk-model-small-pl-0.22",
    "en": "vosk-model-small-en-us-0.15",
    "de": "vosk-model-small-de-0.15",
}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_lock = threading.Lock()
_model = {}


def _models_dir():
    """Kaldi на Windows не открывает пути с русскими буквами (C:\\Users\\Кирби\\...),
    поэтому модель кладём в C:\\ProgramData\\DocuStudio — там только латиница."""
    for base in (os.environ.get("ProgramData"), os.environ.get("PUBLIC")):
        if base and str(base).isascii():
            d = Path(base) / "DocuStudio" / "models"
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d
            except OSError:
                continue
    d = ROOT / "models"
    d.mkdir(exist_ok=True)
    return d


def _get_model(lang, log):
    name = MODELS.get(lang)
    if not name:
        return None
    with _lock:
        if name in _model:
            return _model[name]
        try:
            from vosk import Model, SetLogLevel
        except Exception:
            log("  Синхронизация по словам недоступна: не установлен пакет vosk (обновите программу).")
            _model[name] = None
            return None
        SetLogLevel(-1)
        d = _models_dir()
        path = d / name
        if not (path / "am").exists() and not (path / "conf").exists():
            log(f"  Скачиваю модель распознавания речи ({name}, ~45 МБ) — один раз...")
            url = f"https://alphacephei.com/vosk/models/{name}.zip"
            tmp = d / (name + ".zip.part")
            try:
                with requests.get(url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                with zipfile.ZipFile(tmp) as z:
                    z.extractall(d)
            except Exception as e:
                log(f"  Модель распознавания не скачалась ({type(e).__name__}) — время кадров по расчёту.")
                _model[name] = None
                return None
            finally:
                tmp.unlink(missing_ok=True)
        try:
            _model[name] = Model(str(path))
        except Exception as e:
            log(f"  Модель распознавания не загрузилась ({e}) — время кадров по расчёту.")
            _model[name] = None
        return _model[name]


def norm(w):
    w = w.lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-яąćęłńóśźż]+", "", w)


def words(wav_path, lang="ru", log=print):
    """[(start, end, слово)] для файла озвучки. Результат кэшируется рядом с файлом."""
    wav_path = Path(wav_path)
    cache = wav_path.with_suffix(wav_path.suffix + f".words-{lang}.json")
    if cache.exists():
        try:
            return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        except Exception:
            pass
    model = _get_model(lang, log)
    if model is None:
        return None
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(model, 16000)
    rec.SetWords(True)
    p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(wav_path),
                          "-ar", "16000", "-ac", "1", "-f", "s16le", "-"], stdout=subprocess.PIPE,
                         creationflags=_NO_WINDOW)
    out = []
    while True:
        data = p.stdout.read(8000)
        if not data:
            break
        if rec.AcceptWaveform(data):
            out += json.loads(rec.Result()).get("result", [])
    out += json.loads(rec.FinalResult()).get("result", [])
    p.wait()
    res = [(round(float(w["start"]), 3), round(float(w["end"]), 3), norm(w["word"])) for w in out if w.get("word")]
    try:
        cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return res


def chunk_starts(chunks, rec_words, dur):
    """Во сколько начинается каждый кусок текста (по распознанным словам).
    Сопоставление по последовательности слов: не страшны ошибки распознавания отдельных слов."""
    toks, owner = [], []
    for ci, ch in enumerate(chunks):
        for w in ch.split():
            n = norm(w)
            if n:
                toks.append(n)
                owner.append(ci)
    rt = [w for _, _, w in rec_words]
    if not toks or not rt:
        return None
    sm = difflib.SequenceMatcher(None, toks, rt, autojunk=False)
    t_of = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            t_of[a + k] = rec_words[b + k][0]
    if len(t_of) < max(2, len(toks) // 5):          # почти ничего не совпало — не доверяем
        return None
    known = sorted(t_of)

    def time_at(i):
        if i in t_of:
            return t_of[i]
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:
            return max(0.0, t_of[nxt] - (nxt - i) * 0.33)
        if nxt is None:
            return min(dur, t_of[prev] + (i - prev) * 0.33)
        return t_of[prev] + (t_of[nxt] - t_of[prev]) * (i - prev) / (nxt - prev)
    starts = []
    for ci in range(len(chunks)):
        first = next((i for i, o in enumerate(owner) if o == ci), None)
        starts.append(time_at(first) if first is not None else (starts[-1] if starts else 0.0))
    starts[0] = 0.0
    for i in range(1, len(starts)):                  # монотонно
        starts[i] = max(starts[i], starts[i - 1] + 0.3)
    return starts


def word_time(rec_words, stem, t0, t1):
    """Когда в промежутке [t0, t1] звучит слово, начинающееся с stem (для фото человека — точно на имени)."""
    stem = norm(stem)
    for s, e, w in rec_words or []:
        if t0 - 0.3 <= s <= t1 and w.startswith(stem):
            return s
    return None
