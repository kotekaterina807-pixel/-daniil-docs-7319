"""Поиск «выстреливших» роликов у молодых каналов (YouTube Data API v3, ключ youtube_api_key).

Команда из чата: в queue/ репозитория отчётов кладётся outliers_*.json:
  {"outliers": true, "queries": ["verschollene expedition", ...], "lang": "de", "region": "DE",
   "days": 180, "channel_days": 365, "max_videos": 15, "min_views": 30000}
Программа ищет ролики по запросам, оставляет каналы, которым не больше channel_days дней и у которых
не больше max_videos роликов, и кладёт список (json + страница с превью) в reports/outliers_<дата>/.
Квота ключа: 10 000 единиц в день, один поиск — 100 единиц, остальное почти бесплатно."""
import html
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from .report import _put

YT = "https://www.googleapis.com/youtube/v3"


def _get(path, key, **params):
    r = requests.get(f"{YT}/{path}", params={**params, "key": key}, timeout=30)
    if r.status_code != 200:
        msg = ""
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"YouTube API {r.status_code}: {msg}")
    return r.json()


def _chunks(xs, n=50):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _dur(iso):
    """PT1H2M3S → секунды."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def find(cfg, cmd, log=print):
    key = (cfg.get("youtube_api_key") or "").strip()
    if not key:
        raise RuntimeError("нет ключа YouTube Data API (Настройки → Ключи → «Ключ YouTube»)")
    now = datetime.now(timezone.utc)
    after = (now - timedelta(days=int(cmd.get("days", 180)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    ch_days = int(cmd.get("channel_days", 365))
    max_videos = int(cmd.get("max_videos", 15))
    min_views = int(cmd.get("min_views", 30000))
    min_sec = int(cmd.get("min_minutes", 4)) * 60
    vids = {}
    for q in cmd.get("queries") or []:
        params = {"part": "snippet", "q": q, "type": "video", "order": "viewCount", "maxResults": 50,
                  "publishedAfter": after}
        if cmd.get("before_days"):
            params["publishedBefore"] = (now - timedelta(days=int(cmd["before_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cmd.get("lang"):
            params["relevanceLanguage"] = cmd["lang"]
        if cmd.get("region"):
            params["regionCode"] = cmd["region"]
        try:
            res = _get("search", key, **params)
        except RuntimeError as e:
            log(f"  Поиск «{q}»: {e}")
            if "quota" in str(e).lower():
                break
            continue
        for it in res.get("items") or []:
            vid = (it.get("id") or {}).get("videoId")
            if vid:
                vids.setdefault(vid, q)
        log(f"  Поиск «{q}»: {len(res.get('items') or [])} роликов")
    stats = {}
    for part in _chunks(list(vids)):
        for it in _get("videos", key, part="snippet,statistics,contentDetails", id=",".join(part)).get("items") or []:
            stats[it["id"]] = it
    chans = {}
    ch_ids = list({v["snippet"]["channelId"] for v in stats.values()})
    for part in _chunks(ch_ids):
        for it in _get("channels", key, part="snippet,statistics", id=",".join(part)).get("items") or []:
            chans[it["id"]] = it
    rows = []
    for vid, v in stats.items():
        sn, st = v["snippet"], v.get("statistics") or {}
        ch = chans.get(sn["channelId"])
        if not ch:
            continue
        cs, csn = ch.get("statistics") or {}, ch["snippet"]
        views = int(st.get("viewCount", 0))
        n_vid = int(cs.get("videoCount", 0) or 0)
        subs = int(cs.get("subscriberCount", 0) or 0) if not cs.get("hiddenSubscriberCount") else 0
        born = datetime.fromisoformat(csn["publishedAt"].replace("Z", "+00:00"))
        age = (now - born).days
        sec = _dur((v.get("contentDetails") or {}).get("duration"))
        if views < min_views or n_vid > max_videos or age > ch_days or sec < min_sec:
            continue
        if cmd.get("max_subs") and subs > int(cmd["max_subs"]):
            continue
        rows.append({
            "title": sn["title"], "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": ((sn.get("thumbnails") or {}).get("high") or {}).get("url", ""),
            "published": sn["publishedAt"][:10], "minutes": round(sec / 60, 1),
            "views": views, "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
            "channel": csn["title"], "channel_url": f"https://www.youtube.com/channel/{sn['channelId']}",
            "channel_thumb": ((csn.get("thumbnails") or {}).get("default") or {}).get("url", ""),
            "channel_since": csn["publishedAt"][:10], "channel_videos": n_vid, "subs": subs,
            "channel_views": int(cs.get("viewCount", 0) or 0), "query": vids[vid],
            "lang": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "",
        })
    rows.sort(key=lambda r: -r["views"])
    return rows


def by_channel(rows):
    """Один ряд на канал — его самый популярный ролик из найденных."""
    best = {}
    for r in rows:
        k = r["channel_url"]
        if k not in best or r["views"] > best[k]["views"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: -r["views"])


def page(rows, cmd):
    def fmt(n):
        return f"{n:,}".replace(",", " ")
    cards = []
    for r in rows:
        head = (f'<div class="h"><img class="av" src="{html.escape(r["channel_thumb"])}">'
                f'<div><a href="{html.escape(r["channel_url"])}"><b>{html.escape(r["channel"])}</b></a><br>'
                f'<span class="m">{fmt(r["subs"])} подписчиков · {r["channel_videos"]} роликов · с {r["channel_since"]}</span></div></div>'
                if cmd.get("mode") == "channels" else "")
        cards.append(
            f'<div class="c">{head}<a href="{html.escape(r["url"])}"><img src="{html.escape(r["thumb"])}"></a>'
            f'<div class="t"><a href="{html.escape(r["url"])}">{html.escape(r["title"])}</a></div>'
            f'<div class="m">{fmt(r["views"])} просмотров · {fmt(r["likes"])} лайков · {r["minutes"]} мин · {r["published"]}</div>'
            f'<div class="m"><a href="{html.escape(r["channel_url"])}">{html.escape(r["channel"])}</a> · '
            f'{fmt(r["subs"])} подписчиков · {r["channel_videos"]} роликов · канал с {r["channel_since"]}</div>'
            f'<div class="q">запрос: {html.escape(r["query"])}</div></div>')
    return ("<!doctype html><meta charset='utf-8'><title>Выстрелившие ролики</title><style>"
            "body{font-family:Arial;background:#111;color:#eee;margin:16px}a{color:#9cf}"
            ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}"
            ".c{background:#1c1c1c;padding:8px;border-radius:8px}.c img{width:100%;border-radius:6px}"
            ".h{display:flex;gap:10px;align-items:center;margin-bottom:8px}.av{width:48px;height:48px;border-radius:50%;width:48px}"
            ".t{font-weight:bold;margin:6px 0}.m{font-size:13px;color:#bbb}.q{font-size:12px;color:#777}</style>"
            f"<h2>Молодые каналы с сильными роликами ({len(rows)})</h2>"
            f"<p>Запросы: {html.escape(', '.join(cmd.get('queries') or []))}</p><div class='g'>"
            + "".join(cards) + "</div>")


def run(cfg, cmd, log=print):
    rows = find(cfg, cmd, log)
    if cmd.get("mode") == "channels":
        rows = by_channel(rows)
    repo, token = (cfg.get("report_repo") or "").strip(), (cfg.get("report_token") or "").strip()
    folder = f"reports/outliers_{time.strftime('%Y-%m-%d_%H%M')}"
    if repo and token:
        _put(repo, token, f"{folder}/список.json",
             json.dumps({"command": cmd, "rows": rows}, ensure_ascii=False, indent=1).encode("utf-8"),
             f"выстрелившие ролики: {len(rows)}")
        _put(repo, token, f"{folder}/список.html", page(rows, cmd).encode("utf-8"), "выстрелившие ролики: страница")
    log(f"Выстрелившие ролики: найдено {len(rows)}, список в {folder}")
    return rows, folder
