"""Сборка по команде из чата с Claude.

Claude кладёт сценарий в папку queue/ ЗАКРЫТОГО репозитория отчётов. Программа раз в минуту
заглядывает туда: если есть сценарий и сейчас ничего не собирается — забирает его (удаляет из очереди)
и запускает сборку, как кнопка «Собрать». Ход дел пишется в status/ того же репозитория, готовый
отчёт уходит как обычно. Ключи и видео никуда не отправляются."""
import base64
import json
import time

import requests

from .report import API, _h, _put


def _cfg_ok(cfg):
    repo, token = (cfg.get("report_repo") or "").strip(), (cfg.get("report_token") or "").strip()
    if not cfg.get("remote_jobs", True) or not repo or "/" not in repo or not token:
        return None, None
    return repo, token


def pending(cfg):
    """Сценарии в очереди: [(имя, sha)], по порядку имён."""
    repo, token = _cfg_ok(cfg)
    if not repo:
        return []
    r = requests.get(f"{API}/repos/{repo}/contents/queue", headers=_h(token), timeout=30)
    if r.status_code != 200:
        return []
    items = r.json() if isinstance(r.json(), list) else []
    return sorted((x["name"], x["sha"]) for x in items
                  if x.get("type") == "file" and x.get("name", "").lower().endswith((".txt", ".json")))


def take(cfg, name, sha):
    """Забрать сценарий: скачать и убрать из очереди (чтобы не собрался дважды)."""
    repo, token = _cfg_ok(cfg)
    url = f"{API}/repos/{repo}/contents/queue/{name}"
    g = requests.get(url, headers=_h(token), timeout=60)
    g.raise_for_status()
    d = g.json()
    if d.get("content"):
        text = base64.b64decode(d["content"]).decode("utf-8-sig")
    else:                                                   # большой файл — через download_url
        text = requests.get(d["download_url"], headers=_h(token), timeout=60).content.decode("utf-8-sig")
    requests.delete(url, headers=_h(token), timeout=30,
                    json={"message": f"взят в сборку: {name}", "sha": d.get("sha") or sha}).raise_for_status()
    return text


def status(cfg, name, text):
    """Короткая запись о ходе сборки — её видит Claude."""
    repo, token = _cfg_ok(cfg)
    if not repo:
        return
    body = json.dumps({"script": name, "status": text, "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                      ensure_ascii=False, indent=1)
    try:
        _put(repo, token, "status/remote.json", body.encode("utf-8"), f"статус: {text[:60]}")
    except Exception:
        pass
