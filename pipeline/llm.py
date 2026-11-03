"""Единый доступ к нейросетям: Yandex AI Studio (по умолчанию) или Claude.
Сообщение пользователя — строка или список частей: {"type": "text", "text": ...} / {"type": "image", "b64": ...}."""
import time

import requests

YANDEX_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"
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
            raise LLMError(f"{who}: ключ не подходит или нет прав ({r.status_code}). Проверьте ключ, "
                           f"ID каталога и роли сервисного аккаунта. Ответ: {r.text[:300]}")
        raise LLMError(f"{who}: ошибка {r.status_code}: {r.text[:400]}")
    raise LLMError(f"{who} не отвечает: {last}")


def chat(cfg, system, content, max_tokens=4000, purpose="text", log=print):
    """purpose: "text" — сценарий, "vision" — проверка кадров (дешёвая модель с картинками)."""
    provider = cfg.get("llm_provider", "yandex")
    if provider == "claude":
        key = cfg.get("anthropic_key", "").strip()
        if not key:
            raise LLMError("Не указан ключ Claude в настройках.")
        model = cfg.get("vision_model" if purpose == "vision" else "claude_model")
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": _parts_claude(content)}]}
        if system:
            body["system"] = system
        data = _post(CLAUDE_URL, {"x-api-key": key, "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"}, body, log, "Claude")
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    key = cfg.get("yandex_api_key", "").strip()
    from .config import clean_folder_id
    folder = clean_folder_id(cfg.get("yandex_folder_id", ""))
    if not key or not folder:
        raise LLMError("Не указан API-ключ Yandex Cloud или ID каталога в настройках.")
    name = cfg.get("yandex_vision_model" if purpose == "vision" else "yandex_text_model")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    parts = _parts_openai(content)
    if purpose == "vision":
        # модели Qwen по умолчанию «размышляют» и тратят лимит на рассуждения — просим отвечать сразу
        if isinstance(parts, str):
            parts += "\n/no_think"
        else:
            parts = parts + [{"type": "text", "text": "/no_think"}]
        max_tokens = max(max_tokens, 4000)
    messages.append({"role": "user", "content": parts})
    body = {"model": f"gpt://{folder}/{name}", "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.2 if purpose == "vision" else 0.6}
    data = _post(YANDEX_URL, {"Authorization": f"Api-Key {key}", "OpenAI-Project": folder,
                              "Content-Type": "application/json"}, body, log, "Yandex AI Studio")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"Yandex AI Studio вернул неожиданный ответ: {str(data)[:300]}")
    text = msg.get("content") or ""
    # убираем блок рассуждений, если модель вставила его в ответ
    import re as _re
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.S).strip()
    if not text:
        # ответ мог остаться внутри рассуждений — берём оттуда
        text = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return text
