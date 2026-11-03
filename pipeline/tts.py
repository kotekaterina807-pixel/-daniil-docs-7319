"""Озвучка: Yandex SpeechKit (по умолчанию), ElevenLabs, OpenAI, Edge (бесплатно)."""
import asyncio
import re
import subprocess
import time
import wave
from pathlib import Path

import requests

from .config import ffmpeg_exe

SAMPLE_RATE = 44100


class TTSError(Exception):
    pass


def _chunks(text, limit):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        if len(cur) + len(sent) + 1 > limit and cur:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts


def _post_audio(url, headers, body, out_path, log):
    for attempt in range(5):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            log(f"  Сеть: {e}; повтор...")
            time.sleep(5)
            continue
        if r.status_code == 200:
            Path(out_path).write_bytes(r.content)
            return
        if r.status_code in (429, 500, 502, 503):
            time.sleep(8 * (attempt + 1))
            continue
        raise TTSError(f"Ошибка озвучки {r.status_code}: {r.text[:300]}")
    raise TTSError("Сервис озвучки не отвечает.")


def _openai(cfg, text, out_mp3, instructions, log):
    key = cfg.get("openai_key", "").strip()
    if not key:
        raise TTSError("Не указан ключ OpenAI в настройках.")
    body = {
        "model": "gpt-4o-mini-tts",
        "voice": cfg.get("openai_voice", "onyx"),
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        body["instructions"] = instructions
    _post_audio("https://api.openai.com/v1/audio/speech",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                body, out_mp3, log)


def _elevenlabs(cfg, text, out_mp3, prev_text, next_text, log):
    key = cfg.get("elevenlabs_key", "").strip()
    voice = cfg.get("elevenlabs_voice_id", "").strip()
    if not key or not voice:
        raise TTSError("Не указан ключ ElevenLabs или ID голоса в настройках.")
    body = {
        "text": text,
        "model_id": cfg.get("elevenlabs_model", "eleven_multilingual_v2"),
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.15},
    }
    if prev_text:
        body["previous_text"] = prev_text[-300:]
    if next_text:
        body["next_text"] = next_text[:300]
    _post_audio(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                {"xi-api-key": key, "Content-Type": "application/json"}, body, out_mp3, log)


# Голоса Yandex SpeechKit: язык -> (код языка, голос по умолчанию для других языков)
YANDEX_LANGS = {"ru": ("ru-RU", "alexander"), "en": ("en-US", "john"), "de": ("de-DE", "lea")}
# Все русские голоса Yandex SpeechKit (API v3) с амплуа. Первое амплуа — по умолчанию.
ROLE_RU = {"neutral": "нейтральный", "good": "доброжелательный", "evil": "раздражённый", "friendly": "дружелюбный",
           "strict": "строгий", "whisper": "шёпот", "support": "поддержка", "casual": "разговорный",
           "sales": "продажи", "formal": "официальный", "narrator": "рассказчик"}
YANDEX_VOICES = [
    # (голос, имя, пол, амплуа, модель, подходит для документального кино)
    ("alexander", "Александр", "м", ["neutral", "good"], "general", True),
    ("kirill", "Кирилл", "м", ["neutral", "strict", "good"], "general", True),
    ("anton", "Антон", "м", ["neutral", "good"], "general", True),
    ("denis", "Денис", "м", ["neutral", "formal", "casual", "sales"], "livetts", True),
    ("irina", "Ирина", "ж", ["neutral", "narrator", "formal", "support", "sales"], "livetts", True),
    ("filipp", "Филипп", "м", [], "general", False),
    ("ermil", "Ермил", "м", ["neutral", "good"], "general", False),
    ("zahar", "Захар", "м", ["neutral", "good"], "general", False),
    ("madi_ru", "Мади", "м", [], "general", False),
    ("vasily", "Василий", "м", ["support", "sales"], "livetts", False),
    ("sergey", "Сергей", "м", ["support", "sales"], "livetts", False),
    ("julia", "Юлия", "ж", ["neutral", "strict"], "general", False),
    ("marina", "Марина", "ж", ["neutral", "whisper", "friendly"], "general", False),
    ("dasha", "Даша", "ж", ["neutral", "good", "friendly"], "general", False),
    ("jane", "Джейн", "ж", ["neutral", "good", "evil"], "general", False),
    ("omazh", "Омаж", "ж", ["neutral", "evil"], "general", False),
    ("lera", "Лера", "ж", ["neutral", "friendly"], "general", False),
    ("masha", "Маша", "ж", ["good", "strict", "friendly"], "general", False),
    ("saule_ru", "Сауле", "ж", ["neutral", "strict", "whisper"], "general", False),
    ("zamira_ru", "Замира", "ж", ["neutral", "strict", "friendly"], "general", False),
    ("zhanar_ru", "Жанар", "ж", ["neutral", "strict", "friendly"], "general", False),
    ("yulduz_ru", "Юлдуз", "ж", ["neutral", "strict", "friendly", "whisper"], "general", False),
    ("sofia", "София", "ж", ["support", "casual"], "livetts", False),
    ("vera", "Вера", "ж", ["support", "casual"], "livetts", False),
]
V1_VOICES = {"ermil", "filipp", "zahar", "jane", "omazh", "alena", "marina", "madirus"}


def voice_value(voice, role=""):
    """Значение для настроек: «голос» (амплуа по умолчанию) или «голос:амплуа»."""
    v = next((x for x in YANDEX_VOICES if x[0] == voice), None)
    if not role or (v and v[3] and role == v[3][0]):
        return voice
    return f"{voice}:{role}"


def voice_title(value):
    voice, _, role = str(value or "").partition(":")
    v = next((x for x in YANDEX_VOICES if x[0] == voice), None)
    if not v:
        return value
    role = role or (v[3][0] if v[3] else "")
    return v[1] + (f" — {ROLE_RU.get(role, role)}" if role else "") + (" (LiveTTS)" if v[4] == "livetts" else "")


# совместимость: список (значение, подпись) для всех голосов и амплуа
RU_VOICES = [(voice_value(v[0], r), voice_title(voice_value(v[0], r))) for v in YANDEX_VOICES for r in (v[3] or [""])]
LIVETTS = {v[0] for v in YANDEX_VOICES if v[4] == "livetts"}
# Бесплатные голоса Microsoft Edge (без ключа)
EDGE_VOICES = [("ru-RU-DmitryNeural", "Дмитрий", "м"), ("ru-RU-SvetlanaNeural", "Светлана", "ж"),
               ("ru-RU-DariyaNeural", "Дарья", "ж")]
V3_URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"


def _split_voice(value):
    voice, _, role = str(value or "alexander").strip().partition(":")
    return voice.strip() or "alexander", role.strip()


def _yandex_v3(cfg, text, out_mp3, log, voice_value=None):
    """SpeechKit API v3: все современные голоса (alexander, kirill, anton, LiveTTS и др.)."""
    import base64
    import json as _json
    key = cfg.get("yandex_api_key", "").strip()
    if not key:
        raise TTSError("Не указан API-ключ Yandex Cloud в настройках.")
    voice, role = _split_voice(voice_value or cfg.get("yandex_voice", "alexander"))
    hints = [{"voice": voice}]
    if role:
        hints.append({"role": role})
    body = {"text": text, "hints": hints, "outputAudioSpec": {"containerAudio": {"containerAudioType": "MP3"}},
            "loudnessNormalizationType": "LUFS", "unsafeMode": True}
    if voice in LIVETTS:
        body["model"] = "livetts"
    else:
        hints.append({"speed": str(cfg.get("yandex_speed", "1.0") or "1.0")})
    headers = {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"}
    from .config import clean_folder_id
    folder = clean_folder_id(cfg.get("yandex_folder_id", ""))
    if folder:
        headers["x-folder-id"] = folder
    for attempt in range(5):
        try:
            r = requests.post(V3_URL, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            log(f"  SpeechKit: нет связи ({e}), повтор...")
            time.sleep(5)
            continue
        if r.status_code == 200:
            audio = bytearray()
            for line in r.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except ValueError:
                    continue
                obj = obj.get("result", obj)
                data = (obj.get("audioChunk") or {}).get("data")
                if data:
                    audio += base64.b64decode(data)
            if not audio:
                raise TTSError(f"SpeechKit v3 вернул ответ без звука: {r.text[:200]}")
            Path(out_mp3).write_bytes(bytes(audio))
            return
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise TTSError("SpeechKit: ключ не подходит или у сервисного аккаунта нет роли "
                           f"ai.speechkit-tts.user ({r.status_code}): {r.text[:200]}")
        raise TTSError(f"SpeechKit (голос {voice}{':' + role if role else ''}): ошибка {r.status_code}: {r.text[:300]}")
    raise TTSError("SpeechKit не отвечает.")


def _yandex_v1(cfg, text, out_mp3, log):
    key = cfg.get("yandex_api_key", "").strip()
    if not key:
        raise TTSError("Не указан API-ключ Yandex Cloud в настройках.")
    code = cfg.get("language", "ru")
    lang_code, default_voice = YANDEX_LANGS[code]
    voice = _split_voice(cfg.get("yandex_voice"))[0] if code == "ru" else default_voice
    if code == "ru" and voice not in V1_VOICES:
        voice = "filipp"
    data = {"text": text, "lang": lang_code, "voice": voice, "format": "mp3",
            "speed": str(cfg.get("yandex_speed", "1.0"))}
    for attempt in range(5):
        try:
            r = requests.post("https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize",
                              headers={"Authorization": f"Api-Key {key}"}, data=data, timeout=120)
        except requests.RequestException as e:
            log(f"  SpeechKit: нет связи ({e}), повтор...")
            time.sleep(5)
            continue
        if r.status_code == 200 and r.content:
            Path(out_mp3).write_bytes(r.content)
            return
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise TTSError("SpeechKit: ключ не подходит или у сервисного аккаунта нет роли "
                           f"ai.speechkit-tts.user ({r.status_code}): {r.text[:200]}")
        raise TTSError(f"SpeechKit: ошибка {r.status_code}: {r.text[:300]}")
    raise TTSError("SpeechKit не отвечает.")


_v3_state = {"broken": False}


def _yandex(cfg, text, out_mp3, log):
    """Русский — через API v3 (новые голоса); если v3 недоступен — API v1."""
    if cfg.get("language", "ru") == "ru" and not _v3_state["broken"]:
        try:
            return _yandex_v3(cfg, text, out_mp3, log)
        except TTSError as e:
            if "ключ не подходит" in str(e):
                raise
            _v3_state["broken"] = True
            log(f"  Голос через API v3 не сработал ({str(e)[:160]}) — озвучиваю через API v1.")
    return _yandex_v1(cfg, text, out_mp3, log)


def _friendly(err):
    e = str(err)
    if "401" in e or "403" in e or "ключ не подходит" in e:
        return ("Yandex не принял ключ: проверьте API-ключ (AQVN…) и роль ai.speechkit-tts.user "
                "у сервисного аккаунта (Параметры → Сервисы и ключи → Проверить подключения)")
    if "Не указан" in e:
        return "Не указан API-ключ Yandex — вставьте его в «Параметры → Сервисы и ключи»"
    return e[:300]


def make_sample(cfg, provider, value, text, out_mp3):
    """Озвучивает фразу одним голосом — для прослушивания. Ошибку возвращает понятным текстом."""
    out_mp3 = Path(out_mp3)
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_mp3.with_suffix(".part.mp3")
    try:
        if provider == "edge":
            _edge(cfg, text, tmp, lambda m: None, voice=value)
        else:
            c = dict(cfg, language="ru")
            try:
                _yandex_v3(c, text, tmp, lambda m: None, voice_value=value)
            except TTSError as e3:
                voice = _split_voice(value)[0]
                if voice in V1_VOICES and "ключ не подходит" not in str(e3):
                    _yandex_v1(dict(c, yandex_voice=voice), text, tmp, lambda m: None)
                else:
                    raise
        tmp.replace(out_mp3)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise TTSError(_friendly(e))
    return out_mp3


def voice_samples(cfg, text, out_dir, log=print, provider="yandex"):
    """Озвучивает одну фразу всеми голосами (амплуа по умолчанию) — для сравнения на слух."""
    import hashlib
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    out_dir = Path(out_dir) / provider
    made, errors = [], []
    items = ([(v, t) for v, t, _ in EDGE_VOICES] if provider == "edge"
             else [(voice_value(v[0]), voice_title(voice_value(v[0]))) for v in YANDEX_VOICES])
    for value, title in items:
        mp3 = out_dir / f"{value.replace(':', '_')}_{h}.mp3"
        if mp3.exists():
            made.append(mp3)
            continue
        try:
            make_sample(cfg, provider, value, text, mp3)
            made.append(mp3)
            log(f"  ✔ {title}")
        except Exception as e:
            errors.append(f"{title}: {str(e)[:150]}")
            log(f"  ✘ {title}: {str(e)[:150]}")
            if "ключ" in str(e).lower():
                break                        # без ключа остальные голоса тоже не озвучатся
    return made, errors


def _edge(cfg, text, out_mp3, log, voice=None):
    try:
        import edge_tts
    except ImportError:
        raise TTSError("Модуль edge-tts не установлен. Запустите install.bat ещё раз.")

    async def run():
        await edge_tts.Communicate(text, voice or cfg.get("edge_voice", "ru-RU-DmitryNeural")).save(str(out_mp3))

    for attempt in range(4):
        try:
            asyncio.run(run())
            return
        except Exception as e:
            log(f"  Edge TTS: {e}; повтор...")
            time.sleep(4)
    raise TTSError("Бесплатная озвучка Edge не отвечает.")


def to_wav(inputs, out_wav, gap=0.0):
    """Склеивает один или несколько аудиофайлов в WAV моно 44.1 кГц (gap — пауза между частями, сек)."""
    import os
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for i in inputs:
        cmd += ["-i", str(i)]
    n = len(inputs)
    pre = "".join(f"[{i}:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=mono"
                  f"{f',apad=pad_dur={gap}' if gap and i < n - 1 else ''}[p{i}];" for i in range(n))
    filt = pre + "".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
    cmd += ["-filter_complex", filt, "-map", "[a]", "-ac", "1", "-ar", str(SAMPLE_RATE), str(out_wav)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=0x08000000 if os.name == "nt" else 0)
    if p.returncode != 0:
        raise TTSError(f"Не удалось собрать звук: {p.stderr[-400:]}")


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def synthesize(cfg, niche, text, out_wav, prev_text="", next_text="", log=print):
    """Озвучивает текст и сохраняет WAV. Возвращает длительность в секундах."""
    out_wav = Path(out_wav)
    provider = cfg.get("tts_provider", "yandex")
    if provider == "yandex" and cfg.get("language", "ru") not in YANDEX_LANGS:
        provider = "edge"          # у Yandex нет голосов для этого языка
    limit = {"openai": 1500, "elevenlabs": 2500, "edge": 3000, "yandex": 240}.get(provider, 1500)
    mp3s = []
    for j, part in enumerate(_chunks(text, limit)):
        mp3 = out_wav.with_name(f"{out_wav.stem}_{j}.mp3")
        if provider == "yandex":
            _yandex(cfg, part, mp3, log)
        elif provider == "elevenlabs":
            _elevenlabs(cfg, part, mp3, prev_text, next_text, log)
        elif provider == "edge":
            _edge(cfg, part, mp3, log)
        else:
            _openai(cfg, part, mp3, niche.get("tts_instructions", ""), log)
        mp3s.append(mp3)
    to_wav(mp3s, out_wav, gap=0.18 if provider == "yandex" else 0.0)
    for m in mp3s:
        m.unlink(missing_ok=True)
    return wav_duration(out_wav)
