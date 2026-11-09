"""Настройки программы: ключи API, голоса, пути."""
import json
import os
from pathlib import Path

VERSION = "10.5 (09.11.2026)"
CONFIG_REV = 10      # при смене — обновляются настройки монтажа у уже сохранённых конфигов
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
NICHES_DIR = ROOT / "niches"
MUSIC_DIR = ROOT / "music"
SFX_DIR = ROOT / "sfx"        # звуки: typewriter*.mp3 — печать на листах, whoosh*.mp3 — переход к главе
OUTPUT_DIR = ROOT / "output"

DEFAULTS = {
    "llm_provider": "claude",         # нейросеть: Claude (проверка кадров)
    "anthropic_key": "",
    "claude_model": "claude-sonnet-5-5",
    "pexels_key": "",
    "pixabay_key": "",
    "tts_provider": "lumean",          # lumean — голоса ElevenLabs через Lumean; elevenlabs — напрямую
    "openai_key": "",
    "openai_voice": "onyx",
    "elevenlabs_key": "",
    "elevenlabs_voice_id": "",
    "elevenlabs_model": "eleven_multilingual_v2",
    "lumean_key": "",                  # ключ Lumean: озвучка + Claude для проверки кадров
    "lumean_template_id": "",          # шаблон «DocuStudio» в Lumean (создаётся сам)
    "lumean_ai_model": "",             # модель Claude в Lumean; пусто — самая новая Sonnet
    "eleven_voices": [],               # сохранённые голоса ElevenLabs: [{id, name, desc, preview}]
    "eleven_speed": 1.05,               # скорость речи ElevenLabs (0.7–1.2)
    "eleven_stability": 0.35,
    "eleven_style": 0.35,
    "edge_voice": "ru-RU-DmitryNeural",
    "music_volume": 0.12,              # громкость фоновой музыки (0..1)
    "shot_seconds": 3.2,               # средняя длина одного кадра, сек
    "max_shot_seconds": 5.0,           # обычно кадр не длиннее этого
    "show_labels": True,               # всплывающие надписи (места, люди, даты)
    "visual_check": "claude_code",     # claude_code — Claude Code по подписке; claude — Claude по API; chat — листы в чат; none — без проверки
    "claude_code_model": "sonnet",
    "tg_token": "",                    # бот Telegram для отправки готовых видео (@BotFather)
    "tg_chat_id": "",                  # ваш чат с ботом — программа узнаёт сама после «Start»
    "tg_send": True,
    "tg_notify": True,
    "limit_watch": True,
    "claude_review": True,
    "review_wait_min": 120,
    "old_look": True,
    "word_sync": True,
    "qa_check": True,
    "remote_jobs": True,             # сборка по команде из чата с Claude (очередь в репозитории отчётов)
    "auto_update": True,             # новые версии ставятся сами (не во время сборки)
    "photo_stack": True,             # подряд идущие фото ложатся стопкой со вспышкой
    "leonardo_key": "",              # необязательно: рисует кадры, которых нет ни в архивах, ни в интернете
    "ai_images": True,
    "report_repo": "",                 # закрытый репозиторий GitHub для отчётов Claude (владелец/название)
    "report_token": "",                # ключ GitHub только к этому репозиторию (Contents: Read and write)
    "report_send": True,               # отправлять отчёт после каждой сборки                  # контроль качества: Claude Code проверяет каждый кадр перед монтажом                 # кадры по точному времени слов (распознавание озвучки, Vosk, офлайн)                  # «староватая» картинка: выцветший тёплый цвет, зерно плёнки, виньетка                   # отправлять каждое готовое видео     # модель Claude Code для проверки кадров
    "strict_visuals": True,            # каждый кадр проверяется нейросетью по смыслу и на водяные знаки
    "vision_model": "claude-sonnet-5-5",   # Claude проверяет кадры по смыслу
    "min_relevance": 8,                # порог соответствия смыслу (0–10)
    "reserve_relevance": 6,            # запасной порог: такие кадры берутся, только если не хватает лучших
    "max_checked_per_segment": 48,     # сколько кандидатов максимум проверять на один абзац
    "language": "ru",                  # язык видео по умолчанию (код из LANGS)
    "label_min_gap_sec": 45,           # минимум секунд между всплывающими надписями
    "allow_historic_symbols": False,   # пропускать кадры с символикой экстремистских режимов
    "proxy_url": "",                   # прокси только для программы, например socks5://127.0.0.1:10808
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


def voice_lang(v) -> str:
    """Язык сохранённого голоса: поле lang (из библиотеки) или код языка в описании голоса."""
    if not v:
        return ""
    if v.get("lang") in LANGS:
        return v["lang"]
    import re as _re
    m = _re.search(r"(?<![a-z])(" + "|".join(LANGS) + r")(?![a-z])", str(v.get("desc", "")).lower())
    return m.group(1) if m else ""


def voice_for_lang(cfg, code):
    """Голос диктора для языка: выбранный, если он этого языка, иначе первый сохранённый голос этого языка."""
    vs = cfg.get("eleven_voices") or []
    cur = next((v for v in vs if v.get("id") == cfg.get("elevenlabs_voice_id")), None)
    if cur and voice_lang(cur) in ("", code):
        return cur
    return next((v for v in vs if voice_lang(v) == code), cur)


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
    if not str(cfg.get("update_repo") or "").strip():
        cfg["update_repo"] = DEFAULTS["update_repo"]
    # Yandex Cloud из программы удалён: его ключи и настройки стираются
    for k in [k for k in cfg if k.startswith("yandex_")]:
        cfg.pop(k, None)
    cfg["llm_provider"] = "claude"
    if cfg.get("tts_provider") not in ("elevenlabs", "lumean"):
        cfg["tts_provider"] = "lumean"
    if int(cfg.get("config_rev", 0) or 0) < 7 and not str(cfg.get("elevenlabs_key", "")).strip():
        cfg["tts_provider"] = "lumean"
    if cfg.get("visual_check") == "auto":
        cfg["visual_check"] = "claude"
    if int(cfg.get("config_rev", 0) or 0) < CONFIG_REV:
        # новая версия монтажа: старые сохранённые значения темпа и голоса заменяются новыми
        for k in ("shot_seconds", "max_shot_seconds", "min_relevance", "reserve_relevance", "max_checked_per_segment"):
            cfg[k] = DEFAULTS[k]
        if int(cfg.get("config_rev", 0) or 0) < 5:
            cfg["tts_provider"] = "lumean"            # основная озвучка — голоса ElevenLabs через Lumean
            cfg["visual_check"] = "claude"            # кадры проверяет Claude
            cfg["vision_model"] = DEFAULTS["vision_model"]
        if int(cfg.get("config_rev", 0) or 0) < 8 and not str(cfg.get("anthropic_key", "")).strip() \
                and cfg.get("visual_check") in ("claude", "auto"):
            cfg["visual_check"] = "claude_code"       # кадры проверяет Claude по подписке, без оплаты API
        if int(cfg.get("config_rev", 0) or 0) < 9:     # живее голос: был медленный и ровный
            if float(cfg.get("eleven_speed", 0.9) or 0.9) <= 0.95:
                cfg["eleven_speed"] = DEFAULTS["eleven_speed"]
            if float(cfg.get("eleven_stability", 0.45) or 0.45) >= 0.45:
                cfg["eleven_stability"] = DEFAULTS["eleven_stability"]
            cfg["eleven_style"] = DEFAULTS["eleven_style"]
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
