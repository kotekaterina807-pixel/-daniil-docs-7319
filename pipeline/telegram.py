"""Отправка готовых видео в Telegram через вашего бота.
Бот создаётся в @BotFather (бесплатно), токен вставляется в программу, вы нажимаете «Start» у бота —
и программа присылает туда каждое готовое видео.
У ботов Telegram ограничение: файл не больше 50 МБ. Поэтому длинное видео программа сама пережимает
в лёгкую копию для просмотра (полная версия остаётся в папке проекта)."""
import json
import subprocess
from pathlib import Path

import requests

from .config import ffmpeg_exe

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 49 * 1024 * 1024                    # чуть меньше 50 МБ — лимит Bot API
_NO_WINDOW = 0x08000000 if __import__("os").name == "nt" else 0


class TgError(Exception):
    pass


def _call(token, method, timeout=30, files=None, **params):
    if not token:
        raise TgError("Не указан токен бота Telegram.")
    try:
        r = requests.post(API.format(token=token, method=method), data=params, files=files, timeout=timeout)
    except requests.RequestException as e:
        raise TgError(f"Нет связи с Telegram ({type(e).__name__}).")
    try:
        d = r.json()
    except ValueError:
        raise TgError(f"Telegram ответил {r.status_code}.")
    if not d.get("ok"):
        desc = d.get("description", "")
        if r.status_code == 401:
            raise TgError("Telegram не принял токен — скопируйте его из @BotFather заново.")
        if "chat not found" in desc or "bot was blocked" in desc:
            raise TgError("Бот не может написать вам: откройте его в Telegram и нажмите «Start».")
        raise TgError(f"Telegram: {desc or r.status_code}")
    return d.get("result")


def bot_name(token):
    me = _call(token, "getMe")
    return me.get("username", "")


def find_chat(token):
    """Чат, из которого боту последним написали (после нажатия «Start»)."""
    ups = _call(token, "getUpdates", timeout=20) or []
    for u in reversed(ups):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
    return None, ""


def send_text(cfg, text):
    _call(cfg.get("tg_token", ""), "sendMessage", chat_id=cfg.get("tg_chat_id", ""), text=text)


def _duration(path):
    try:
        p = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True, timeout=60,
                           creationflags=_NO_WINDOW)
        import re
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    except Exception:
        return 0.0


def light_copy(src, log=print):
    """Копия до ~48 МБ: битрейт под длину ролика, разрешение — сколько позволяет битрейт."""
    src = Path(src)
    dur = max(1.0, _duration(src))
    total_kbit = 46 * 1024 * 8                  # с запасом
    v_kbps = int(total_kbit / dur) - 96
    if v_kbps < 120:
        return None                              # слишком длинный — даже лёгкая копия не влезет
    height = 1080 if v_kbps > 4500 else 720 if v_kbps > 1500 else 540 if v_kbps > 700 else 360
    out = src.with_name(src.stem + "_telegram.mp4")
    log(f"  Telegram: готовлю лёгкую копию {height}p (лимит ботов — 50 МБ)...")
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.2)}k", "-bufsize", f"{v_kbps * 2}k",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=3600, creationflags=_NO_WINDOW)
    return out if out.exists() and out.stat().st_size <= LIMIT else None


def send_video(cfg, path, caption="", log=print):
    """Отправляет видео в чат. Большое — лёгкой копией. Возвращает текст для журнала."""
    token, chat = cfg.get("tg_token", ""), cfg.get("tg_chat_id", "")
    if not token or not chat:
        raise TgError("Telegram не подключён: Параметры → Сервисы и ключи → Telegram.")
    path = Path(path)
    send = path
    note = ""
    if path.stat().st_size > LIMIT:
        send = light_copy(path, log)
        if not send:
            send_text(cfg, f"✅ Готово: {caption}\nВидео слишком длинное для отправки ботом — оно в папке проекта на ПК.")
            return "видео слишком длинное для бота — отправлено только сообщение"
        note = "\n(лёгкая копия для просмотра — полное качество в папке проекта)"
    with open(send, "rb") as f:
        _call(token, "sendVideo", timeout=1800, files={"video": (send.name, f, "video/mp4")},
              chat_id=chat, caption=(caption + note)[:1000], supports_streaming="true")
    return f"видео отправлено в Telegram ({send.stat().st_size / 1048576:.0f} МБ)"
