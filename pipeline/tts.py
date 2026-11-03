"""Озвучка: ElevenLabs."""
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


EL_API = "https://api.elevenlabs.io/v1"


def _el_key(cfg):
    key = str(cfg.get("elevenlabs_key", "")).strip()
    if not key:
        raise TTSError("Не указан ключ ElevenLabs — вставьте его в «Параметры → Сервисы и ключи».")
    return key


def el_error(r):
    """Понятный текст ошибки ElevenLabs."""
    t = r.text[:400]
    low = t.lower()
    if r.status_code == 401 and "unusual_activity" in low:
        return ("ElevenLabs отключил бесплатный тариф для этого подключения (VPN/прокси). "
                "Нужен платный тариф ElevenLabs — на нём это ограничение не действует.")
    if r.status_code == 401:
        return "ElevenLabs не принял ключ — скопируйте его заново в Profile → API Keys."
    if "quota_exceeded" in low or r.status_code == 402:
        return "На тарифе ElevenLabs закончились символы — пополните тариф или дождитесь нового месяца."
    if "voice_not_found" in low or r.status_code == 404:
        return "Голос не найден — проверьте ID голоса (Voice ID) или добавьте голос в «My Voices» на сайте."
    return f"ElevenLabs: ошибка {r.status_code}: {t[:250]}"


def _el_speed(cfg):
    try:
        return max(0.7, min(1.2, float(str(cfg.get("eleven_speed", "0.9")).replace(",", "."))))
    except ValueError:
        return 0.9


def _elevenlabs(cfg, text, out_mp3, prev_text, next_text, log, voice=None):
    key = _el_key(cfg)
    voice = (voice or str(cfg.get("elevenlabs_voice_id", ""))).strip()
    if not voice:
        raise TTSError("Не выбран голос ElevenLabs — выберите его на вкладке «Голоса».")
    body = {
        "text": text,
        "model_id": cfg.get("elevenlabs_model", "eleven_multilingual_v2"),
        "voice_settings": {"stability": float(cfg.get("eleven_stability", 0.45)),
                           "similarity_boost": 0.8, "style": float(cfg.get("eleven_style", 0.2)),
                           "use_speaker_boost": True, "speed": _el_speed(cfg)},
    }
    if prev_text:
        body["previous_text"] = prev_text[-300:]
    if next_text:
        body["next_text"] = next_text[:300]
    url = f"{EL_API}/text-to-speech/{voice}?output_format=mp3_44100_128"
    for attempt in range(5):
        try:
            r = requests.post(url, headers={"xi-api-key": key, "Content-Type": "application/json"},
                              json=body, timeout=180)
        except requests.RequestException as e:
            log(f"  ElevenLabs: нет связи ({e}), повтор...")
            time.sleep(5)
            continue
        if r.status_code == 200 and r.content:
            Path(out_mp3).write_bytes(r.content)
            return
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(8 * (attempt + 1))
            continue
        raise TTSError(el_error(r))
    raise TTSError("ElevenLabs не отвечает — проверьте, что VPN включён.")


def _el_get(cfg, path, params=None):
    try:
        r = requests.get(EL_API + path, headers={"xi-api-key": _el_key(cfg)}, params=params, timeout=30)
    except requests.RequestException as e:
        raise TTSError(f"Нет связи с ElevenLabs ({type(e).__name__}) — проверьте, что VPN включён.")
    if r.status_code != 200:
        raise TTSError(el_error(r))
    return r.json()


def _el_voice_row(v):
    lab = v.get("labels") or {}
    desc = ", ".join(x for x in (lab.get("gender"), lab.get("age"), lab.get("accent"),
                                 lab.get("description") or lab.get("descriptive"), lab.get("use_case")) if x)
    return {"id": v.get("voice_id"), "name": v.get("name") or v.get("voice_id"), "desc": desc,
            "preview": v.get("preview_url") or "", "category": v.get("category", "")}


def el_voice_info(cfg, voice_id):
    return _el_voice_row(_el_get(cfg, f"/voices/{voice_id.strip()}"))


def el_account_voices(cfg):
    return [_el_voice_row(v) for v in _el_get(cfg, "/voices").get("voices", [])]


def el_subscription(cfg):
    d = _el_get(cfg, "/user/subscription")
    return {"tier": d.get("tier", ""), "used": d.get("character_count", 0), "limit": d.get("character_limit", 0)}


def make_sample(cfg, provider, value, text, out_mp3):
    """Озвучивает фразу одним голосом ElevenLabs — для прослушивания."""
    out_mp3 = Path(out_mp3)
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_mp3.with_suffix(".part.mp3")
    try:
        _elevenlabs(cfg, text, tmp, "", "", lambda m: None, voice=value)
        tmp.replace(out_mp3)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise TTSError(str(e))
    return out_mp3


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


def to_wav(inputs, out_wav, gap=0.0, tempo=1.0):
    """Склеивает один или несколько аудиофайлов в WAV моно 44.1 кГц (gap — пауза между частями, сек)."""
    import os
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    for i in inputs:
        cmd += ["-i", str(i)]
    n = len(inputs)
    pre = "".join(f"[{i}:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=mono"
                  f"{f',apad=pad_dur={gap}' if gap and i < n - 1 else ''}[p{i}];" for i in range(n))
    filt = pre + "".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1" + \
        (f",atempo={tempo:.3f}" if abs(tempo - 1) > 0.01 else "") + "[a]"
    cmd += ["-filter_complex", filt, "-map", "[a]", "-ac", "1", "-ar", str(SAMPLE_RATE), str(out_wav)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       creationflags=0x08000000 if os.name == "nt" else 0)
    if p.returncode != 0:
        raise TTSError(f"Не удалось собрать звук: {p.stderr[-400:]}")


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def synthesize(cfg, niche, text, out_wav, prev_text="", next_text="", log=print):
    """Озвучивает абзац через ElevenLabs и сохраняет WAV. Возвращает длительность в секундах."""
    out_wav = Path(out_wav)
    mp3s = []
    for j, part in enumerate(_chunks(text, 2500)):
        mp3 = out_wav.with_name(f"{out_wav.stem}_{j}.mp3")
        _elevenlabs(cfg, part, mp3, prev_text, next_text, log)
        mp3s.append(mp3)
    to_wav(mp3s, out_wav)
    for m in mp3s:
        m.unlink(missing_ok=True)
    return wav_duration(out_wav)
