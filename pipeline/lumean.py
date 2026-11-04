"""Lumean (lumean.app): озвучка голосами ElevenLabs и доступ к Claude по одному ключу.
Озвучка: шаблон с голосом → заказ (асинхронно) → ссылка на файл → скачивание.
Документация: https://api.lumean.app/docs/llm.md"""
import threading
import time
from pathlib import Path

import requests

BASE = "https://api.lumean.app/api/public"
AI_BASE = "https://api.lumean.app/v1"
TEMPLATE_NAME = "DocuStudio"
FINAL_OK = {"completed", "result_delivered"}
FINAL_BAD = {"failed", "compensated", "cancelled"}
NEEDED = ["orders.read", "orders.write", "orders.download", "templates.read", "templates.write",
          "voices.read", "billing.read"]
_lock = threading.Lock()


class LumeanError(Exception):
    pass


def _key(cfg):
    k = str(cfg.get("lumean_key", "")).strip()
    if not k:
        raise LumeanError("Не указан ключ Lumean — вставьте его в «Параметры → Сервисы и ключи».")
    return k


def _explain(r):
    """Понятный текст ошибки Lumean по коду и полю reason."""
    try:
        d = r.json()
    except ValueError:
        d = {}
    reason = d.get("reason") or d.get("error_code") or ""
    msg = d.get("message") or r.text[:200]
    if r.status_code == 401:
        return "Lumean не принял ключ — скопируйте его заново (Lumean → API → Ключи)."
    if r.status_code == 403:
        return ("У ключа Lumean не хватает прав. На сайте: API → Ключи → ⋯ → Редактировать → "
                "«Разрешения» → «Все права» → Сохранить.")
    if r.status_code == 402 or reason in ("insufficient_balance", "insufficient_allowance", "no_subscription"):
        return "На Lumean не хватает токенов подписки или баланса — пополните на сайте Lumean."
    if reason == "limit_exceeded":
        return "Исчерпан лимит подписки Lumean на этот период."
    if reason == "token_quota_exceeded":
        return "Исчерпан лимит токенов у ключа Lumean (вкладка «Лимиты» в настройках ключа)."
    if r.status_code == 422:
        errs = d.get("errors") or {}
        first = next(iter(errs.values()), [""])
        return f"Lumean отклонил запрос: {msg} {first[0] if first else ''}".strip()
    return f"Lumean: ошибка {r.status_code}: {msg}"


def _req(cfg, method, path, retries=4, **kw):
    """Запрос к публичному API с повторами при занятости сервиса."""
    headers = {"X-API-KEY": _key(cfg), "Accept": "application/json", "X-Locale": "ru"}
    for attempt in range(retries):
        try:
            r = requests.request(method, BASE + path, headers=headers, timeout=60, **kw)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise LumeanError(f"Нет связи с Lumean ({type(e).__name__}).")
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code == 429:
            try:
                d = r.json()
            except ValueError:
                d = {}
            reason = d.get("reason", "")
            if reason in ("rate_limit_exceeded", "concurrency_limit_exceeded", "policy_cooldown_active") \
                    and attempt < retries - 1:
                wait = int(r.headers.get("Retry-After") or d.get("retry_after") or 15)
                time.sleep(min(max(wait, 5), 120))
                continue
            raise LumeanError(_explain(r))
        if r.status_code in (500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise LumeanError(_explain(r))
        try:
            return r.json().get("data")
        except ValueError:
            raise LumeanError("Lumean вернул непонятный ответ.")
    raise LumeanError("Lumean не отвечает.")


# ---------- ключ, аккаунт ----------
def key_info(cfg):
    return _req(cfg, "GET", "/key")


def missing_permissions(cfg):
    perms = set(key_info(cfg).get("permissions") or [])
    if "*" in perms:
        return []
    return [p for p in NEEDED if p not in perms]


def account(cfg):
    """Остаток токенов подписки (1 токен ≈ 1 символ озвучки ElevenLabs) и тариф."""
    d = _req(cfg, "GET", "/subscriptions") or {}
    plans, left, total = [], 0, 0
    for s in d.get("subscriptions") or []:
        for it in s.get("items") or []:
            plans.append((it.get("plan") or {}).get("name") or "")
            left += int(it.get("tokens_remaining") or 0)
            total += int(it.get("token_allowance") or 0)
    return {"tier": ", ".join(p for p in plans if p) or "нет подписки", "used": max(0, total - left),
            "limit": total, "left": left, "ai_models": d.get("available_models") or []}


# ---------- голоса ----------
def library(cfg, search="", gender="", age="", use_cases="", language="ru", page=0):
    params = {"page": page, "page_size": 30}
    for k, v in (("search", search), ("gender", gender), ("age", age), ("use_cases", use_cases),
                 ("required_languages", language)):
        if v:
            params[k] = v
    d = _req(cfg, "GET", "/voices/elevenlabs/library", params=params) or {}
    out = []
    for v in d.get("voices") or []:
        desc = ", ".join(x for x in (v.get("gender"), v.get("age"), v.get("accent"), v.get("descriptive"),
                                     v.get("use_case"), v.get("language")) if x)
        out.append({"id": v.get("voice_id"), "name": v.get("name") or v.get("voice_id"), "desc": desc,
                    "preview": v.get("preview_url") or "", "about": (v.get("description") or "")[:200]})
    return {"voices": out, "has_more": bool(d.get("has_more")), "page": d.get("page", page)}


# ---------- шаблон ----------
def _tts_config(cfg, voice_id, language="ru"):
    return {"tts_settings": {
        "mode": "mode_v1",
        "model_id": cfg.get("elevenlabs_model", "eleven_multilingual_v2"),
        "voice_id": voice_id,
        "language_code": language,
        "advanced_voice_settings": True,
        "prev_next_text": True,
        "voice_settings": {"stability": float(cfg.get("eleven_stability", 0.45)),
                           "similarity_boost": 0.8, "style": float(cfg.get("eleven_style", 0.2)),
                           "use_speaker_boost": True,
                           "speed": max(0.7, min(1.2, float(cfg.get("eleven_speed", 0.9))))},
    }}


def ensure_template(cfg, voice_id, save=None):
    """Один шаблон «DocuStudio» на аккаунт: голос и настройки на каждый заказ задаются через config_override."""
    with _lock:
        tid = str(cfg.get("lumean_template_id", "")).strip()
        if tid:
            return tid
        d = _req(cfg, "GET", "/templates", params={"page": 1, "per_page": 30, "search": TEMPLATE_NAME,
                                                     "service": "elevenlabs"}) or {}
        items = d.get("items") if isinstance(d, dict) else d
        tid = next((t["id"] for t in (items or []) if t.get("name") == TEMPLATE_NAME), None)
        if not tid:
            t = _req(cfg, "POST", "/templates", json={"service_key": "elevenlabs", "name": TEMPLATE_NAME,
                                                     "config": _tts_config(cfg, voice_id)})
            tid = t["id"]
        cfg["lumean_template_id"] = tid
        if save:
            save(tid)
        return tid


# ---------- озвучка ----------
def synthesize(cfg, text, out_file, log=print, voice=None, language="ru", name="", save_template=None):
    """Озвучивает текст одним заказом и сохраняет аудиофайл (mp3). Ждёт готовности заказа."""
    voice = (voice or str(cfg.get("elevenlabs_voice_id", ""))).strip()
    if not voice:
        raise LumeanError("Не выбран голос — выберите его на вкладке «Голоса».")
    tid = ensure_template(cfg, voice, save_template)
    body = {"template_id": tid, "input_text": text, "name": (name or text[:60])[:120],
            "config_override": _tts_config(cfg, voice, language)}
    try:
        order = _req(cfg, "POST", "/orders", json=body)
    except LumeanError as e:
        if "шаблон" in str(e).lower() or "template" in str(e).lower():
            cfg["lumean_template_id"] = ""             # шаблон удалили на сайте — создаём заново
            body["template_id"] = ensure_template(cfg, voice, save_template)
            order = _req(cfg, "POST", "/orders", json=body)
        else:
            raise
    oid = order["id"]
    t0, retried = time.time(), False
    while True:
        time.sleep(3)
        o = _req(cfg, "GET", f"/orders/{oid}")
        st = o.get("status")
        if st in FINAL_OK:
            break
        if st == "partially_completed":
            if not retried:
                retried = True
                try:
                    _req(cfg, "POST", f"/orders/{oid}/items/retry-failed")
                except LumeanError:
                    pass
                continue
            if time.time() - t0 > 600:
                raise LumeanError("Lumean озвучил абзац не полностью: часть фрагментов отклонена. "
                                  "Перефразируйте абзац и пересоберите видео.")
        if st in FINAL_BAD:
            um = (o.get("result") or {}).get("user_message") or st
            raise LumeanError(f"Lumean не озвучил абзац: {um}")
        if time.time() - t0 > 1800:
            raise LumeanError("Lumean слишком долго озвучивает абзац (больше 30 минут).")
    files = (o.get("result") or {}).get("files") or []
    if not files:
        raise LumeanError("Lumean завершил заказ, но не вернул файл.")
    url = _req(cfg, "POST", "/storage/url", json={"path": files[0]})["url"]
    for attempt in range(4):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                tmp = Path(str(out_file) + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                tmp.replace(out_file)
                return out_file
        except requests.RequestException:
            time.sleep(4)
    raise LumeanError("Не удалось скачать озвучку с Lumean.")


# ---------- Claude через Lumean ----------
_model_cache = {}


def ai_model(cfg):
    """Модель Claude для проверки кадров: из настроек или самая новая Sonnet в каталоге Lumean."""
    if str(cfg.get("lumean_ai_model", "")).strip():
        return cfg["lumean_ai_model"].strip()
    key = _key(cfg)
    if key in _model_cache:
        return _model_cache[key]
    r = requests.get(AI_BASE + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        raise LumeanError(f"Lumean AI: список моделей недоступен ({r.status_code}). "
                          "Проверьте галочку «AI» у ключа.")
    ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("kind", "chat") == "chat"]
    claude = [i for i in ids if "claude" in i.lower()]
    pick = sorted([i for i in claude if "sonnet" in i.lower()], reverse=True) or sorted(claude, reverse=True)
    if not pick:
        raise LumeanError("В Lumean сейчас нет моделей Claude.")
    _model_cache[key] = pick[0]
    return pick[0]
