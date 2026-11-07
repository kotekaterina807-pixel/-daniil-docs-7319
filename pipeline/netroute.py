"""Маршрут для каждого сайта отдельно: через прокси или напрямую.

Программа сама узнаёт, какому сайту что нужно: если через прокси сайт не отвечает или не пускает
(обрыв, 403, 410, 451…), запрос тут же повторяется напрямую — и наоборот. Удачный маршрут запоминается
для этого сайта в routes.json и дальше используется сразу, без лишних попыток. Раз в 3 дня маршрут
перепроверяется (сайты и прокси меняются). Можно и закрепить вручную: в routes.json "pin": "direct"/"proxy".
Касается всех запросов программы (кадры, архивы, озвучка, Telegram, GitHub, Leonardo)."""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "routes.json"
TTL = 3 * 24 * 3600
BAD_STATUS = {403, 407, 410, 451, 502, 503}
NO_PROXY = {"http": None, "https": None}
_lock = threading.Lock()
_routes = None
_orig = requests.Session.request


def _load():
    global _routes
    if _routes is None:
        try:
            _routes = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _routes = {}
    return _routes


def _save():
    try:
        FILE.write_text(json.dumps(_routes, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _proxy_on():
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))


def get(host):
    with _lock:
        r = _load().get(host) or {}
    if r.get("pin") in ("direct", "proxy"):
        return r["pin"]
    if r.get("route") and time.time() - float(r.get("t", 0)) < TTL:
        return r["route"]
    return None


def _remember(host, route):
    with _lock:
        d = _load().setdefault(host, {})
        if d.get("route") != route or time.time() - float(d.get("t", 0)) > 3600:
            d.update({"route": route, "t": int(time.time())})
            _save()


def summary():
    """Для «Проверить подключения»: какие сайты идут напрямую, какие через прокси."""
    with _lock:
        rs = dict(_load())
    direct = sorted(h for h, r in rs.items() if (r.get("pin") or r.get("route")) == "direct")
    proxy = sorted(h for h, r in rs.items() if (r.get("pin") or r.get("route")) == "proxy")
    return direct, proxy


def _bad(resp):
    return resp is not None and resp.status_code in BAD_STATUS


def _routed(self, method, url, *args, **kw):
    host = (urlparse(url).hostname or "").lower()
    if not _proxy_on() or not host or host in ("127.0.0.1", "localhost") or kw.get("proxies") or not self.trust_env:
        return _orig(self, method, url, *args, **kw)       # прокси не задан или маршрут выбран вручную в коде
    first = get(host) or "proxy"
    if "files" in kw or hasattr(kw.get("data"), "read"):    # загрузка файла — повторять нельзя (файл уже прочитан)
        k = dict(kw)
        if first == "direct":
            k["proxies"] = NO_PROXY
        return _orig(self, method, url, *args, **k)
    second = "direct" if first == "proxy" else "proxy"

    def go(route):
        k = dict(kw)
        if route == "direct":
            k["proxies"] = NO_PROXY
        return _orig(self, method, url, *args, **k)
    err = resp = None
    try:
        resp = go(first)
        if not _bad(resp):
            _remember(host, first)
            return resp
    except (requests.ConnectionError, requests.Timeout) as e:
        err = e
    try:                                                    # другой маршрут
        resp2 = go(second)
        if not _bad(resp2):
            _remember(host, second)
            return resp2
        if resp is None:
            return resp2
    except (requests.ConnectionError, requests.Timeout):
        pass
    if resp is not None:
        return resp
    raise err


def install():
    if getattr(requests.Session.request, "_docu_routed", False):
        return
    _routed._docu_routed = True
    requests.Session.request = _routed
