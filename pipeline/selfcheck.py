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

UA = {"User-Agent": "DocuStudio/6.5 (personal documentary video tool; https://github.com/kotekaterina807-pixel/-daniil-docs-7319)"}


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
    import re as _re
    url = _re.sub(r"//([^:@/]+):([^@/]+)@", lambda m: "//" + m.group(1)[:2] + "•••:•••@", str(cfg.get("proxy_url", "")).strip())
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


def _archives():
    """Openverse и Библиотека Конгресса — дополнительные свободные архивы фото (без ключей)."""
    name = "Архивы: Openverse, Библиотека Конгресса, Met, Чикаго, Internet Archive"
    res = []
    for label, url, params in (("Openverse", "https://api.openverse.org/v1/images/", {"q": "lighthouse", "page_size": 1}),
                               ("Библиотека Конгресса", "https://www.loc.gov/photos/", {"q": "lighthouse", "fo": "json", "c": 1, "at": "results"}),
                               ("Met", "https://collectionapi.metmuseum.org/public/collection/v1/search", {"q": "ship", "hasImages": "true"}),
                               ("Чикаго", "https://api.artic.edu/api/v1/artworks/search", {"q": "ship", "limit": 1}),
                               ("Internet Archive", "https://archive.org/advancedsearch.php", {"q": "ship", "rows": 1, "output": "json"})):
        try:
            from .visuals import _direct_retry
            first = requests.get(url, params=params, headers=UA, timeout=20)
            r = _direct_retry(url, params, 20, first)
            how = "" if r is first else " напрямую"
            detail = f"через прокси {first.status_code}" + (f", напрямую {r.status_code}" if r is first and first.status_code >= 400 else "")
            res.append((label, r.status_code == 200, f"ответ {r.status_code}{how}" if r.status_code == 200 else detail))
        except requests.RequestException as e:
            res.append((label, False, type(e).__name__))
    good = [l for l, o, m in res if o]
    bad = [l for l, o, m in res if not o]
    ok = len(good) >= 3
    msg = "работают: " + (", ".join(good) or "нет")
    if bad:
        msg += "; закрыты для вашего подключения: " + ", ".join(bad) + (
            " — программа их просто пропускает, это не мешает сборке" if ok else
            " — проверьте прокси/VPN, архивных фото будет мало")
    return (name, ok, msg, False)


def _word_sync(cfg):
    name = "Синхронизация кадров по словам (распознавание речи)"
    try:
        import vosk  # noqa: F401
    except Exception:
        return (name, False, "пакет vosk не установлен — нажмите «Обновить программу»", True)
    from .align import MODELS, _models_dir
    m = MODELS.get(cfg.get("language", "ru"))
    if m and (_models_dir() / m).exists():
        return (name, True, f"готово, модель {m}", False)
    return (name, True, f"модель {m} (~45 МБ) скачается сама при первой сборке", False)


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


def _lumean(cfg):
    name = "Озвучка Lumean (голоса ElevenLabs)"
    from . import lumean
    if not str(cfg.get("lumean_key", "")).strip():
        return (name, False, "не указан ключ Lumean", True)
    try:
        miss = lumean.missing_permissions(cfg)
        acc = lumean.account(cfg)
    except Exception as e:
        return (name, False, str(e)[:300], True)
    if miss:
        return (name, False, "у ключа не хватает прав: " + ", ".join(miss) +
                " — на сайте Lumean: ⋯ у ключа → Редактировать → Разрешения → «Все права» → Сохранить", True)
    msg = f"ключ работает, тариф {acc['tier']}, осталось токенов: {acc['left']:,}".replace(",", " ")
    if not str(cfg.get("elevenlabs_voice_id", "")).strip():
        return (name, False, msg + " — но голос не выбран (вкладка «Голоса»)", True)
    return (name, True, msg + f" (~{acc['left'] // 1100} мин озвучки)", True)


def _claude(cfg):
    name = "Проверка кадров — Claude"
    if not str(cfg.get("anthropic_key", "")).strip() and not str(cfg.get("lumean_key", "")).strip():
        return (name, False, "нет ключа Lumean или Claude — кадры будут выбираться через листы в чат", False)
    im = Image.new("RGB", (64, 64), (40, 90, 160))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    content = [{"type": "image", "b64": base64.b64encode(buf.getvalue()).decode()},
               {"type": "text", "text": 'Какого цвета эта картинка? Верни СТРОГО JSON: {"color": "одно слово"}'}]
    try:
        ans = llm.chat(dict(cfg, llm_provider="claude"), "", content, 50, "vision", lambda m: None).strip()
        via = "напрямую" if str(cfg.get("anthropic_key", "")).strip() else "через Lumean"
        model = cfg.get("vision_model") if via == "напрямую" else __import__("pipeline.lumean", fromlist=["x"]).ai_model(cfg)
        return (name, bool(ans), f"Claude {via}, модель {model}: отвечает («{ans[:30]}»)" if ans else "пустой ответ", True)
    except Exception as e:
        return (name, False, str(e)[:300], True)


def _claude_code(cfg):
    name = "Проверка кадров — Claude Code (подписка)"
    from . import claude_code, config as _c
    if not claude_code.find_exe():
        return (name, False, "Claude Code не установлен — PowerShell: irm https://claude.ai/install.ps1 | iex, "
                             "затем в терминале: claude → /login", True)
    try:
        exe, ans = claude_code.check(cfg, _c.ROOT if hasattr(_c, "ROOT") else ".")
        return (name, bool(ans), f"работает по подписке ({exe.name}), ответ: «{ans[:30]}»" if ans else "пустой ответ", True)
    except Exception as e:
        return (name, False, str(e)[:300], True)


def _people_photos(cfg):
    """Фото людей: Википедия и поиск картинок в интернете (на примере Джорджа Мэллори)."""
    name = "Фото людей: Википедия и поиск в интернете"
    import tempfile
    from .visuals import VisualFinder
    f = VisualFinder(cfg, None, tempfile.mkdtemp(), log=lambda m: None)
    wiki = f._wiki_page_image("George Mallory", "en")
    web = f._web_images("George Mallory photo", min_w=300)
    notes = "; ".join((getattr(f, "person_diag", {}) or {}).get("George Mallory", []))
    msg = f"Википедия: {'работает' if wiki else 'фото не получено' + (' (' + notes + ')' if notes else '')}; " \
          f"поиск в интернете: {'работает, картинок ' + str(len(web)) if web else 'не отвечает'}"
    return (name, bool(wiki or web), msg, False)


def _report(cfg):
    name = "Отчёты для Claude (GitHub)"
    from . import report
    try:
        repo = report.check(cfg)
        return (name, True, f"закрытый репозиторий {repo} — отчёты после каждой сборки уходят Claude", False)
    except Exception as e:
        return (name, False, str(e)[:300], False)


def run_all(cfg, log=print):
    checks = [_ffmpeg, _gpu, _disk]
    checks.append(lambda: _proxy(cfg))
    checks.append(lambda: _lumean(cfg) if tts.provider_of(cfg) == "lumean" else _eleven(cfg))
    if cfg.get("visual_check", "claude") in ("claude", "auto"):
        checks.append(lambda: _claude(cfg))
    if cfg.get("visual_check") == "claude_code":
        checks.append(lambda: _claude_code(cfg))
    checks += [lambda: _pixabay(cfg), _wikimedia, _archives, lambda: _people_photos(cfg), lambda: _word_sync(cfg), lambda: _report(cfg)]
    out = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as e:
            out.append(("Проверка", False, str(e)[:200], False))
    return out
