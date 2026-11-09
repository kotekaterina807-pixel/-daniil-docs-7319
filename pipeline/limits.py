"""Лимит подписки Claude: заметить, что он кончился, дождаться сброса и сообщить в Telegram.

Лимит общий для чата с Claude и для сборки, поэтому программа сама раз в 30 минут
коротко спрашивает Claude Code (самая лёгкая модель, ответ в одно слово). Когда лимит
исчерпан — пишет в Telegram, когда сбросится; когда сбросился — «Лимиты сброшены, пора работать»."""
import json
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config

STATE_FILE = config.ROOT / "лимит_claude.json"
_lock = threading.Lock()
_MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def is_limit_text(text):
    low = (text or "").lower()
    return ("usage limit" in low or "limit reached" in low or "rate limit" in low
            or ("hit your" in low and "limit" in low) or "out of extra usage" in low)


def reset_time(text):
    """Когда сбросится лимит (unix-время) — из сообщения Claude Code. None, если не указано."""
    t = text or ""
    m = re.search(r"limit reached\|(\d{10})", t)
    if m:
        return int(m.group(1))
    m = re.search(r"resets?\s+(?:at\s+)?(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                  r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t, re.I)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    hh, mm = int(hh), int(mm or 0)
    if ap:
        hh = hh % 12 + (12 if ap.lower() == "pm" else 0)
    if hh > 23 or mm > 59:
        return None
    now = datetime.now()
    if mon and mon.lower()[:3] in _MON:
        dt = now.replace(month=_MON[mon.lower()[:3]], day=int(day), hour=hh, minute=mm, second=0, microsecond=0)
        if dt < now - timedelta(days=2):
            dt = dt.replace(year=dt.year + 1)
    else:
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
    return int(dt.timestamp())


def _load():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st):
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _tg(cfg, text):
    if not (cfg.get("tg_notify", True) and cfg.get("tg_token") and cfg.get("tg_chat_id")):
        return
    try:
        from . import telegram
        telegram.send_text(cfg, text)
    except Exception:
        pass


def _when(ts):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    day = "" if dt.date() == datetime.now().date() else (
        "завтра " if dt.date() == (datetime.now() + timedelta(days=1)).date() else dt.strftime("%d.%m "))
    return f"{day}в {dt:%H:%M}"


def hit(cfg, raw, building=False):
    """Claude Code ответил, что лимит исчерпан."""
    ts = reset_time(raw)
    with _lock:
        st = _load()
        first = not st.get("exhausted")
        st.update(exhausted=True, since=st.get("since") or time.time(), raw=(raw or "")[-300:])
        if ts:
            st["reset_at"] = ts
        _save(st)
    if first:
        w = _when(st.get("reset_at"))
        _tg(cfg, "⏸ Лимит Claude исчерпан" + (f", сброс {w}" if w else "") + "."
            + (" Сборка на паузе — продолжу сама после сброса." if building else ""))


def ok(cfg, building=False):
    """Запрос к Claude прошёл. Если до этого лимит был исчерпан — значит, он сбросился."""
    with _lock:
        st = _load()
        if not st.get("exhausted"):
            return
        _save({"exhausted": False, "last_reset": time.time()})
    _tg(cfg, "🔋 Лимиты Claude сброшены — пора работать!" + (" Сборка продолжается." if building else ""))


def _probe(cfg):
    """Короткий запрос к Claude Code. True — ответил, False — лимит, None — другая ошибка."""
    from .claude_code import find_exe, _env, _NO_WINDOW
    exe = find_exe()
    if not exe:
        return None
    cmd = [str(exe), "-p", "--output-format", "text", "--model", "haiku"]
    if str(exe).lower().endswith(".cmd"):
        cmd = ["cmd", "/c"] + cmd
    try:
        p = subprocess.run(cmd, input="Ответь одним словом: ок", capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=tempfile.gettempdir(), env=_env(cfg),
                           timeout=180, creationflags=_NO_WINDOW)
    except Exception:
        return None
    raw = (p.stdout or "") + " " + (p.stderr or "")
    if is_limit_text(raw) and len(raw) < 600:
        hit(cfg, raw)
        return False
    if p.returncode == 0 and (p.stdout or "").strip():
        ok(cfg)
        return True
    return None


def watch_step(cfg, busy):
    """Раз в минуту из фоновой петли программы. busy — идёт сборка (она сама сообщает о лимите)."""
    if busy or not cfg.get("tg_notify", True) or not cfg.get("limit_watch", True):
        return
    st = _load()
    now = time.time()
    if st.get("exhausted"):
        due = st.get("reset_at")
        nxt = (due + 60) if due and now < due + 60 else (st.get("probe_at", 0) + 15 * 60)
    else:
        nxt = st.get("probe_at", 0) + 30 * 60
    if now < nxt:
        return
    st["probe_at"] = now
    _save(st)
    _probe(cfg)
    if not _load().get("exhausted"):
        with _lock:
            s2 = _load()
            s2["probe_at"] = now
            _save(s2)
