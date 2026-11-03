"""Самопроверка перед работой: ключи, доступ к сервисам, FFmpeg, видеокарта, место на диске.
Каждый пункт: (название, успех, пояснение, критично_ли)."""
import base64
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from PIL import Image

from . import llm, render, tts
from .config import ROOT, ffmpeg_exe

UA = {"User-Agent": "YTDocVideoGenerator/1.0 (personal YouTube documentary tool)"}


def _ffmpeg():
    try:
        p = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0)
        return ("FFmpeg (монтаж)", p.returncode == 0, "" if p.returncode == 0 else "не запускается — запустите install.bat", True)
    except Exception as e:
        return ("FFmpeg (монтаж)", False, f"не найден ({e}) — запустите install.bat", True)


def _gpu():
    ok = render.gpu_available()
    return ("Видеокарта NVIDIA для кодирования", ok,
            "используется" if ok else "не используется — видео будет кодироваться процессором (медленнее)", False)


def _disk():
    free_gb = shutil.disk_usage(ROOT).free / 1024 ** 3
    ok = free_gb >= 15
    return ("Место на диске", ok, f"свободно {free_gb:.0f} ГБ" + ("" if ok else " — для ролика 30 мин нужно ~10–15 ГБ"), False)


def _proxy(cfg):
    name = "Прокси программы"
    url = str(cfg.get("proxy_url", "")).strip()
    try:
        r = requests.get("https://ipinfo.io/json", headers=UA, timeout=20)
        d = r.json()
        where = f"{d.get('country', '?')}, {d.get('city', '')}".strip(", ")
        return (name, True, (f"через {url} → " if url else "без прокси (системное подключение) → ") + f"выход в интернет: {where}", False)
    except Exception as e:
        return (name, False, f"{'прокси ' + url + ' не отвечает' if url else 'нет связи'} ({type(e).__name__}) — проверьте, что VPN-приложение запущено", True)


def _pixabay(cfg):
    name = "Видео и фото Pixabay"
    key = cfg.get("pixabay_key", "").strip()
    if not key:
        return (name, False, "не указан ключ Pixabay", True)
    try:
        r = requests.get("https://pixabay.com/api/videos/", params={"key": key, "q": "sea", "per_page": 3},
                         headers=UA, timeout=20)
    except requests.ConnectionError:
        return (name, False, "соединение с pixabay.com обрывается — запросы программы до Pixabay не доходят", True)
    except requests.Timeout:
        return (name, False, "pixabay.com не отвечает (таймаут)", True)
    if r.status_code in (400, 401, 403) or "invalid" in r.text.lower():
        return (name, False, "ключ Pixabay не подходит — скопируйте его заново целиком (с дефисом и буквами)", True)
    try:
        if r.status_code == 200 and r.json().get("hits"):
            return (name, True, "ключ работает, видео находятся", True)
    except ValueError:
        pass
    if False:
        return (name, False, "ключ Pixabay не подходит — скопируйте его заново целиком (с дефисом и буквами)", True)
    return (name, False, f"ответ {r.status_code}: {r.text[:150]}", True)


def _wikimedia():
    name = "Архив фото Wikimedia"
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php",
                         params={"action": "query", "format": "json", "list": "search", "srsearch": "lighthouse",
                                 "srnamespace": 6, "srlimit": 1}, headers=UA, timeout=20)
        ok = r.status_code == 200
        return (name, ok, "" if ok else f"ответ {r.status_code}", False)
    except requests.RequestException as e:
        return (name, False, f"недоступен ({type(e).__name__}) — архивные фото брать не получится", False)


def _eleven(cfg):
    name = "Озвучка ElevenLabs"
    if not str(cfg.get("elevenlabs_key", "")).strip():
        return (name, False, "не указан ключ ElevenLabs", True)
    try:
        sub = tts.el_subscription(cfg)
    except Exception as e:
        return (name, False, str(e)[:300], True)
    left = max(0, int(sub["limit"] or 0) - int(sub["used"] or 0))
    msg = f"ключ работает, тариф {sub['tier']}, осталось символов: {left:,}".replace(",", " ")
    if not str(cfg.get("elevenlabs_voice_id", "")).strip():
        return (name, False, msg + " — но голос не выбран (вкладка «Голоса»)", True)
    return (name, True, msg + f" (~{left // 1100} мин озвучки)", True)


def _claude(cfg):
    name = "Проверка кадров — Claude"
    if not str(cfg.get("anthropic_key", "")).strip():
        return (name, False, "не указан ключ Claude — кадры будут выбираться через листы в чат", False)
    im = Image.new("RGB", (64, 64), (40, 90, 160))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    content = [{"type": "image", "b64": base64.b64encode(buf.getvalue()).decode()},
               {"type": "text", "text": 'Какого цвета эта картинка? Верни СТРОГО JSON: {"color": "одно слово"}'}]
    try:
        ans = llm.chat(dict(cfg, llm_provider="claude"), "", content, 50, "vision", lambda m: None).strip()
        return (name, bool(ans), f"Claude отвечает («{ans[:40]}»)" if ans else "пустой ответ", True)
    except Exception as e:
        return (name, False, str(e)[:300], True)


def run_all(cfg, log=print):
    checks = [_ffmpeg, _gpu, _disk]
    checks.append(lambda: _proxy(cfg))
    checks.append(lambda: _eleven(cfg))
    if cfg.get("visual_check", "claude") in ("claude", "auto"):
        checks.append(lambda: _claude(cfg))
    checks += [lambda: _pixabay(cfg), _wikimedia]
    out = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as e:
            out.append(("Проверка", False, str(e)[:200], False))
    return out
