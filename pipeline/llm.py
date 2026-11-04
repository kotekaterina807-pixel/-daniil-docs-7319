"""Доступ к нейросети Claude (Anthropic API): проверка кадров по смыслу.
Сообщение пользователя — строка или список частей: {"type": "text", "text": ...} / {"type": "image", "b64": ...}."""
import time

import requests

CLAUDE_URL = "https://api.anthropic.com/v1/messages"


class LLMError(Exception):
    pass


def _parts_openai(content):
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if p["type"] == "text":
            out.append({"type": "text", "text": p["text"]})
        else:
            out.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + p["b64"]}})
    return out


def _parts_claude(content):
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if p["type"] == "text":
            out.append({"type": "text", "text": p["text"]})
        else:
            out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": p["b64"]}})
    return out


def _post(url, headers, body, log, who):
    last = None
    for attempt in range(5):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as e:
            last = e
            log(f"  {who}: нет связи ({e}), повтор...")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504, 529):
            wait = 8 * (attempt + 1)
            log(f"  {who}: сервер занят ({r.status_code}), повтор через {wait} с...")
            time.sleep(wait)
            continue
        if r.status_code in (401, 403):
            raise LLMError(f"{who}: ключ не подходит или нет прав ({r.status_code}). Проверьте ключ "
                           f"и галочку «AI» у ключа Lumean. Ответ: {r.text[:300]}")
        if r.status_code == 402:
            raise LLMError(f"{who}: не хватает денег на балансе для проверки кадров — пополните баланс "
                           f"(Lumean: «Магазин» / Anthropic: Billing).")
        raise LLMError(f"{who}: ошибка {r.status_code}: {r.text[:400]}")
    raise LLMError(f"{who} не отвечает: {last}")


def chat(cfg, system, content, max_tokens=4000, purpose="text", log=print):
    """purpose: "text" — сценарий, "vision" — проверка кадров (дешёвая модель с картинками)."""
    """Claude напрямую (ключ Anthropic) или через Lumean (тот же ключ, что для озвучки)."""
    key = str(cfg.get("anthropic_key", "")).strip()
    url, who = CLAUDE_URL, "Claude"
    model = cfg.get("vision_model" if purpose == "vision" else "claude_model")
    if not key:
        lkey = str(cfg.get("lumean_key", "")).strip()
        if not lkey:
            raise LLMError("Нет ключа для Claude: вставьте ключ Lumean (или ключ Claude) в «Сервисы и ключи».")
        from . import lumean
        try:
            model = lumean.ai_model(cfg)
        except lumean.LumeanError as e:
            raise LLMError(str(e))
        key, url, who = lkey, lumean.AI_BASE + "/messages", "Claude (через Lumean)"
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": _parts_claude(content)}]}
    if system:
        body["system"] = system
    data = _post(url, {"x-api-key": key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}, body, log, who)
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
