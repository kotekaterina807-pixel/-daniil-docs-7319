"""Настройки программы: ключи API, голоса, пути."""
import json
import os
from pathlib import Path

VERSION = "3.2 (03.11.2026)"
CONFIG_REV = 2      # при смене — обновляются настройки монтажа у уже сохранённых конфигов
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
NICHES_DIR = ROOT / "niches"
MUSIC_DIR = ROOT / "music"
OUTPUT_DIR = ROOT / "output"

DEFAULTS = {
    # нейросеть для сценария и проверки кадров: yandex (работает из РФ, оплата картой РФ) | claude
    "llm_provider": "yandex",
    "yandex_api_key": "",
    "yandex_folder_id": "",
    "yandex_text_model": "aliceai-llm",          # сценарий
    "yandex_vision_model": "qwen3.6-35b-a3b",    # проверка кадров (понимает изображения)
    "yandex_voice": "alexander",                 # голос диктора для русского (API v3)
    "yandex_speed": "1.0",
    "anthropic_key": "",
    "claude_model": "claude-sonnet-5-5",
    "pexels_key": "",
    "pixabay_key": "",
    "tts_provider": "yandex",          # yandex | openai | elevenlabs | edge
    "openai_key": "",
    "openai_voice": "onyx",
    "elevenlabs_key": "",
    "elevenlabs_voice_id": "",
    "elevenlabs_model": "eleven_multilingual_v2",
    "edge_voice": "ru-RU-DmitryNeural",
    "music_volume": 0.12,              # громкость фоновой музыки (0..1)
    "shot_seconds": 1.8,               # примерная длина одного кадра (≈90 кадров на 3 минуты)
    "max_shot_seconds": 2.8,           # ни один кадр не длиннее этого
    "show_labels": True,               # всплывающие надписи (места, люди, даты)
    "strict_visuals": True,            # каждый кадр проверяется нейросетью по смыслу и на водяные знаки
    "vision_model": "claude-haiku-5-5",
    "min_relevance": 7,                # порог соответствия смыслу (0–10)
    "reserve_relevance": 5,            # запасной порог: такие кадры берутся, только если не хватает лучших
    "max_checked_per_segment": 48,     # сколько кандидатов максимум проверять на один абзац
    "language": "ru",                  # язык видео по умолчанию (код из LANGS)
    "label_min_gap_sec": 45,           # минимум секунд между всплывающими надписями
    "allow_historic_symbols": False,   # пропускать кадры с символикой экстремистских режимов
    # интерфейс
    "theme": "light",                  # light | dark | system
    "sound_done": True,                # звук, когда видео готово
    "open_folder_after": False,        # открывать папку проекта после сборки
    "default_niche": "mysteries.json",
    "update_repo": "kotekaterina807-pixel/-daniil-docs-7319",   # GitHub: откуда приходят обновления
}

# Языки видео. wpm — средний темп диктора (слов в минуту), нужен для расчёта длины сценария.
LANGS = {
    "ru": {"name": "Русский", "prompt": "русском", "wpm": 135, "chapter": "ГЛАВА",
           "timecodes": "Таймкоды:", "sources": "Использованные материалы:",
           "edge_voice": "ru-RU-DmitryNeural"},
    "en": {"name": "English", "prompt": "английском (English)", "wpm": 150, "chapter": "CHAPTER",
           "timecodes": "Chapters:", "sources": "Sources:", "edge_voice": "en-US-GuyNeural"},
    "de": {"name": "Deutsch", "prompt": "немецком (Deutsch)", "wpm": 130, "chapter": "KAPITEL",
           "timecodes": "Kapitel:", "sources": "Quellen:", "edge_voice": "de-DE-ConradNeural"},
    "fr": {"name": "Français", "prompt": "французском (Français)", "wpm": 150, "chapter": "CHAPITRE",
           "timecodes": "Chapitres :", "sources": "Sources :", "edge_voice": "fr-FR-HenriNeural"},
    "es": {"name": "Español", "prompt": "испанском (Español)", "wpm": 150, "chapter": "CAPÍTULO",
           "timecodes": "Capítulos:", "sources": "Fuentes:", "edge_voice": "es-ES-AlvaroNeural"},
}


def lang(code) -> dict:
    return LANGS.get(code) or LANGS["ru"]


# Запасное место для настроек, если в папку программы писать нельзя
ALT_CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "VideoGen" / "config.json"


def clean_folder_id(value) -> str:
    """Из любой вставленной строки (адрес страницы, ID с хвостом /dashboard и т.п.) достаёт ID каталога b1g…"""
    import re
    value = (value or "").strip()
    m = re.search(r"/folders/([a-z0-9]{20})", value) or re.search(r"\b(b1g[a-z0-9]{17})\b", value)
    return m.group(1) if m else value.split("/")[0].strip()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for path in (CONFIG_PATH, ALT_CONFIG_PATH):
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
    cfg["yandex_folder_id"] = clean_folder_id(cfg.get("yandex_folder_id", ""))
    if not str(cfg.get("update_repo") or "").strip():
        cfg["update_repo"] = DEFAULTS["update_repo"]
    if int(cfg.get("config_rev", 0) or 0) < CONFIG_REV:
        # новая версия монтажа: старые сохранённые значения темпа и голоса заменяются новыми
        for k in ("shot_seconds", "max_shot_seconds", "min_relevance", "max_checked_per_segment"):
            cfg[k] = DEFAULTS[k]
        if cfg.get("yandex_voice") in ("", "ermil"):
            cfg["yandex_voice"] = DEFAULTS["yandex_voice"]
        cfg["config_rev"] = CONFIG_REV
    return cfg


def save_config(cfg: dict) -> Path:
    """Сохраняет настройки. Возвращает путь к файлу. Если папка программы защищена от записи —
    сохраняет в %APPDATA%\\VideoGen."""
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    try:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        return CONFIG_PATH
    except OSError:
        ALT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALT_CONFIG_PATH.write_text(data, encoding="utf-8")
        return ALT_CONFIG_PATH


def list_niches() -> dict:
    """Возвращает {название ниши: путь к файлу}."""
    out = {}
    for p in sorted(NICHES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out[data.get("name", p.stem)] = p
        except Exception:
            continue
    return out


def load_niche(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ensure_dirs():
    for d in (OUTPUT_DIR, MUSIC_DIR):
        os.makedirs(d, exist_ok=True)
