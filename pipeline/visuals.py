import os
"""Поиск, проверка и скачивание визуала: видео Pixabay (главный источник), Wikimedia Commons (архив, люди),
фото Pixabay — только если видео не нашлось."""
import hashlib
import html
import re
import threading
import time
from pathlib import Path

import requests

from . import library, vision

UA = {"User-Agent": "DocuStudio/6.5 (personal documentary video tool; https://github.com/kotekaterina807-pixel/-daniil-docs-7319)"}

BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/130.0 Safari/537.36", "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
_DIRECT = requests.Session()
_DIRECT.trust_env = False                       # без прокси программы


def _direct_retry(url, params, timeout, r):
    """Некоторые архивы (Openverse, Библиотека Конгресса, Met) не пускают адреса прокси-серверов
    и отвечают 403/410. Тогда тот же запрос — напрямую, с домашнего интернета: эти архивы из России открыты."""
    import os as _os
    if r is None or r.status_code not in (401, 403, 410):
        return r
    tries = [(requests, BROWSER_UA)]                # 1) тот же путь, но как обычный браузер
    if _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy"):
        tries += [(_DIRECT, UA), (_DIRECT, BROWSER_UA)]   # 2) напрямую, без прокси
    for sess, hdr in tries:
        try:
            r2 = sess.get(url, params=params, headers=hdr, timeout=timeout)
            if r2.status_code < 400:
                return r2
        except requests.RequestException:
            pass
    return r


# Только материалы без авторских прав: общественное достояние и CC0.
# CC BY / CC BY-SA и «Copyrighted free use» отбрасываются.
CLEAN_LICENSE = re.compile(r"public domain|\bpd\b|\bpd-|cc0|no restrictions", re.I)
NOT_CLEAN_LICENSE = re.compile(r"cc[ -]?by|copyrighted|fair use|all rights", re.I)

# Признаки водяных знаков, логотипов, чужих каналов и кадров из чужих видео.
BLOCK_WORDS = re.compile(
    r"watermark|logo|logotype|screenshot|screen[ _-]?(cap|capture|shot)|youtube|tiktok|instagram|"
    r"twitch|facebook|vk\.com|subscribe|youtube[ _-]?channel|film[ _-]?still|video[ _-]?still|frame[ _-]?from|trailer|poster|"
    r"advertis|\bpromo|copyright|©|getty|shutterstock|alamy|istock|depositphotos|123rf|dreamstime|"
    r"pond5|storyblocks|envato|green[ _-]?screen|chroma|lower[ _-]?third|template|mock[ _-]?up|"
    r"\bintro\b|\boutro\b|typography|lettering|\btext\b|\btitle\b|"
    r"time[ _-]?lapse|timelapse|hyper[ _-]?lapse|\bfast\b|\bspeed\b|speeding|motion[ _-]?blur|\bracing\b|"
    r"\bstrobe|\bflicker|\bglitch|\bshaky|\brush\b|\bzoom[ _-]?in\b",
    re.I)


# Для архивных фото (Wikimedia и т.п.) — только явные признаки чужих знаков и стоков. Слова вроде
# «copyright», «title», «text» в описаниях архивов встречаются у любых свободных фото
# («No known copyright restrictions», «Title: …») и раньше отсекали почти все портреты людей.
ARCHIVE_BLOCK = re.compile(
    r"watermark|\blogo\b|logotype|screenshot|screen[ _-]?(cap|capture|shot)|youtube|tiktok|instagram|"
    r"getty|shutterstock|alamy|istock|depositphotos|123rf|dreamstime|pond5|storyblocks|envato|"
    r"film[ _-]?still|video[ _-]?still|frame[ _-]?from|trailer|mock[ _-]?up|template",
    re.I)


STOP = {"the", "and", "with", "from", "into", "over", "under", "near", "for", "old", "a", "an", "of", "in", "on", "at"}


def _words(text):
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOP}


def _overlap(query, tags):
    q, t = _words(query), _words(tags)
    return len(q & t) / max(1, len(q))


class VisualFinder:
    def __init__(self, cfg, niche, media_dir, log=print):
        self.cfg = cfg
        self.niche = niche
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.used = set()
        self.credits = []
        self._cache = {}
        self._warned_pexels = False
        self.library = {}          # индекс своей библиотеки футажей (заполняется в runner)
        self._down = {}            # сервисы, недоступные с этого компьютера
        self._lock = threading.Lock()
        self._net_lock = threading.Lock()
        self._last_pb = 0.0

    # ---------- сеть ----------
    def _get(self, url, params, timeout=30, pixabay=False):
        """GET с повторами; для Pixabay — не чаще ~1.4 запроса в секунду (лимит 100 в минуту)."""
        for attempt in range(4):
            if pixabay:
                with self._net_lock:
                    wait = self._last_pb + 0.7 - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    self._last_pb = time.time()
            r = _direct_retry(url, params, timeout, requests.get(url, params=params, headers=UA, timeout=timeout))
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            return r
        return r

    def _cached(self, key, fn):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        out = fn()
        with self._lock:
            self._cache[key] = out
        return out

    # ---------- Wikimedia ----------
    @staticmethod
    def _wm_item(p, min_w=1000, min_h=560):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("width", 0) < min_w or info.get("height", 0) < min_h:
            return None
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not CLEAN_LICENSE.search(lic) or NOT_CLEAN_LICENSE.search(lic):
            return None
        restrictions = (meta.get("Restrictions") or {}).get("value", "")
        if "trademark" in restrictions.lower():
            return None
        cats = " ".join(c.get("title", "") for c in p.get("categories") or [])
        desc = re.sub(r"<[^>]+>", " ", (meta.get("ImageDescription") or {}).get("value", ""))
        if ARCHIVE_BLOCK.search(p.get("title", "") + " " + cats):
            return None
        artist = re.sub(r"<[^>]+>", "", html.unescape((meta.get("Artist") or {}).get("value", ""))).strip()
        url = info.get("thumburl") or info.get("url")
        if not url:
            return None
        return {
            "kind": "image", "url": url,
            "thumb": url.replace("/1920px-", "/512px-") if "/1920px-" in url else url,
            "source": "Wikimedia Commons", "page": info.get("descriptionurl", ""),
            "title": p.get("title", "").replace("File:", ""), "author": artist or "неизвестен",
            "license": lic, "tags": p.get("title", "") + " " + desc[:300],
        }

    def _wikimedia(self, query):
        if self._down.get("wm"):
            return []

        def fetch():
            params = {
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
                "prop": "imageinfo|categories", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920,
                "cllimit": "max",
            }
            out = []
            try:
                r = self._get("https://commons.wikimedia.org/w/api.php", params)
                pages = (r.json().get("query") or {}).get("pages") or {}
                for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
                    it = self._wm_item(p)
                    if it:
                        out.append(it)
            except requests.ConnectionError as e:
                self._down["wm"] = True
                self.log(f"  Архив Wikimedia недоступен с этого компьютера — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Wikimedia: {e}")
            return out
        return self._cached(("wm", query), fetch)

    # ---------- Openverse (CC0 и общественное достояние; без ключа) ----------
    def _openverse(self, query, min_w=1000):
        if self._down.get("ov"):
            return []

        def fetch():
            out = []
            try:
                r = self._get("https://api.openverse.org/v1/images/",
                              {"q": query, "license": "cc0,pdm", "page_size": 20, "mature": "false"})
                if r.status_code != 200:
                    return out
                for it in r.json().get("results") or []:
                    if (it.get("width") or 0) < min_w or not it.get("url"):
                        continue
                    lic = (it.get("license") or "").lower()
                    if lic not in ("cc0", "pdm"):
                        continue
                    tags = " ".join(t.get("name", "") for t in it.get("tags") or [] if isinstance(t, dict))
                    if ARCHIVE_BLOCK.search((it.get("title") or "") + " " + tags):
                        continue
                    out.append({
                        "kind": "image", "url": it["url"], "thumb": it.get("thumbnail") or it["url"],
                        "source": f"Openverse ({it.get('source') or it.get('provider') or 'архив'})",
                        "page": it.get("foreign_landing_url") or "", "title": it.get("title") or "",
                        "author": it.get("creator") or "неизвестен",
                        "license": "CC0" if lic == "cc0" else "Public Domain", "tags": (it.get("title") or "") + " " + tags,
                    })
            except requests.ConnectionError as e:
                self._down["ov"] = True
                self.log(f"  Openverse недоступен с этого компьютера — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Openverse: {e}")
            return out
        return self._cached(("ov", query, min_w), fetch)

    # ---------- Библиотека Конгресса США (только «без известных ограничений»; без ключа) ----------
    def _loc(self, query, limit=6):
        if self._down.get("loc"):
            return []

        def fetch():
            out = []
            try:
                r = self._get("https://www.loc.gov/photos/", {"q": query, "fo": "json", "c": 15, "at": "results"})
                if r.status_code != 200:
                    return out
                for res in r.json().get("results") or []:
                    if len(out) >= limit:
                        break
                    fmt = " ".join(res.get("original_format") or []).lower()
                    imgs = [u.split("#")[0] for u in res.get("image_url") or []]
                    page = res.get("url") or res.get("id") or ""
                    if not imgs or "collection" in fmt or "web page" in fmt or not page:
                        continue
                    time.sleep(0.6)                       # бережно к сайту библиотеки
                    try:
                        it = self._get(page.rstrip("/") + "/", {"fo": "json", "at": "item"}).json().get("item") or {}
                    except Exception:
                        continue
                    rights = it.get("rights_advisory") or it.get("rights_information") or ""
                    rights = " ".join(rights) if isinstance(rights, list) else str(rights)
                    if not re.search(r"no known restrictions|public domain|no copyright", rights, re.I):
                        continue
                    title = res.get("title") or ""
                    if ARCHIVE_BLOCK.search(title):
                        continue
                    thumb = next((u for u in imgs if u.endswith("r.jpg")), imgs[0])
                    out.append({"kind": "image", "url": imgs[-1], "thumb": thumb, "source": "Library of Congress",
                                "page": page, "title": title, "author": "Library of Congress",
                                "license": "No known restrictions", "tags": title})
            except requests.ConnectionError as e:
                self._down["loc"] = True
                self.log(f"  Библиотека Конгресса недоступна с этого компьютера — дальше без неё ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Библиотека Конгресса: {e}")
            return out
        return self._cached(("loc", query), fetch)

    # ---------- Музей Метрополитен (открытый доступ, CC0; без ключа) ----------
    def _met(self, query, limit=6):
        if self._down.get("met"):
            return []

        def fetch():
            out = []
            try:
                r = self._get("https://collectionapi.metmuseum.org/public/collection/v1/search",
                              {"q": query, "hasImages": "true"})
                ids = (r.json().get("objectIDs") or [])[:20] if r.status_code == 200 else []
                for oid in ids:
                    if len(out) >= limit:
                        break
                    o = self._get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}", {}).json()
                    if not o.get("isPublicDomain") or not o.get("primaryImage"):
                        continue
                    title = o.get("title") or ""
                    if ARCHIVE_BLOCK.search(title):
                        continue
                    out.append({"kind": "image", "url": o["primaryImage"], "thumb": o.get("primaryImageSmall") or o["primaryImage"],
                                "source": "The Met (Open Access)", "page": o.get("objectURL") or "",
                                "title": f"{title} ({o.get('objectDate') or ''})", "author": o.get("artistDisplayName") or "неизвестен",
                                "license": "CC0", "tags": " ".join([title, o.get("objectName") or "", o.get("culture") or ""])})
            except requests.ConnectionError as e:
                self._down["met"] = True
                self.log(f"  Музей Метрополитен недоступен — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Музей Метрополитен: {e}")
            return out
        return self._cached(("met", query), fetch)

    # ---------- Чикагский институт искусств (общественное достояние, CC0; без ключа) ----------
    def _aic(self, query, limit=8):
        if self._down.get("aic"):
            return []

        def fetch():
            out = []
            try:
                r = self._get("https://api.artic.edu/api/v1/artworks/search",
                              {"q": query, "query[term][is_public_domain]": "true", "limit": 15,
                               "fields": "id,title,image_id,artist_display,date_display,is_public_domain,thumbnail"})
                for a in (r.json().get("data") or []) if r.status_code == 200 else []:
                    if len(out) >= limit:
                        break
                    if not a.get("is_public_domain") or not a.get("image_id"):
                        continue
                    if ((a.get("thumbnail") or {}).get("width") or 2000) < 900:
                        continue
                    title = a.get("title") or ""
                    base = f"https://www.artic.edu/iiif/2/{a['image_id']}/full"
                    out.append({"kind": "image", "url": f"{base}/1686,/0/default.jpg", "thumb": f"{base}/400,/0/default.jpg",
                                "source": "Art Institute of Chicago", "page": f"https://www.artic.edu/artworks/{a['id']}",
                                "title": f"{title} ({a.get('date_display') or ''})", "author": a.get("artist_display") or "неизвестен",
                                "license": "CC0", "tags": title})
            except requests.ConnectionError as e:
                self._down["aic"] = True
                self.log(f"  Чикагский институт искусств недоступен — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Чикагский институт искусств: {e}")
            return out
        return self._cached(("aic", query), fetch)

    # ---------- Internet Archive: старая кинохроника в общественном достоянии (без ключа) ----------
    def _ia_video(self, query, limit=4):
        if self._down.get("ia"):
            return []

        def secs(v):
            try:
                if ":" in str(v):
                    parts = [float(x) for x in str(v).split(":")]
                    return sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
                return float(v)
            except Exception:
                return 0.0

        def fetch():
            out = []
            try:
                q = (f"({query}) AND mediatype:(movies) AND "
                     f"(licenseurl:*publicdomain* OR licenseurl:*zero* OR rights:\"public domain\")")
                r = self._get("https://archive.org/advancedsearch.php",
                              {"q": q, "fl[]": ["identifier", "title", "licenseurl"], "rows": 12,
                               "sort[]": "downloads desc", "output": "json"})
                docs = ((r.json().get("response") or {}).get("docs") or []) if r.status_code == 200 else []
                for d in docs:
                    if len(out) >= limit:
                        break
                    ident, title = d.get("identifier"), d.get("title") or ""
                    if not ident or ARCHIVE_BLOCK.search(title):
                        continue
                    lic = str(d.get("licenseurl") or "public domain")
                    meta = self._get(f"https://archive.org/metadata/{ident}", {}).json()
                    files = [f for f in meta.get("files") or [] if str(f.get("name", "")).lower().endswith(".mp4")]
                    files.sort(key=lambda f: (0 if "h.264" in str(f.get("format", "")).lower() else 1,
                                              -int(f.get("height") or 0)))
                    f = next((f for f in files if int(f.get("size") or 0) < 900 * 1024 * 1024), None)
                    if not f:
                        continue
                    dur = secs(f.get("length"))
                    if dur < 20:
                        continue
                    start = round(min(dur * 0.3, max(0.0, dur - 20)), 1)   # без заставок в начале фильма
                    url = f"https://archive.org/download/{ident}/{requests.utils.quote(f['name'])}"
                    out.append({"kind": "video", "url": url, "thumb": f"https://archive.org/services/img/{ident}",
                                "duration": 14, "segment": [start, 14], "source": "Internet Archive (общественное достояние)",
                                "page": f"https://archive.org/details/{ident}", "title": title,
                                "author": (meta.get("metadata") or {}).get("creator") or "неизвестен",
                                "license": "Public Domain" if "publicdomain" in lic or "public" in lic.lower() else "CC0",
                                "tags": title})
            except requests.ConnectionError as e:
                self._down["ia"] = True
                self.log(f"  Internet Archive недоступен — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Internet Archive: {e}")
            return out
        return self._cached(("ia", query), fetch)

    def _wiki_page_image(self, name, lang):
        """Главное фото статьи Википедии о человеке — только если файл лежит в Commons со свободной лицензией."""
        def fetch():
            try:
                r = self._get(f"https://{lang}.wikipedia.org/w/api.php",
                              {"action": "query", "format": "json", "prop": "pageimages", "piprop": "name",
                               "titles": name, "redirects": 1})
                pages = (r.json().get("query") or {}).get("pages") or {}
                fname = next((p.get("pageimage") for p in pages.values() if p.get("pageimage")), None)
                if not fname:
                    return []
                r = self._get("https://commons.wikimedia.org/w/api.php",
                              {"action": "query", "format": "json", "titles": "File:" + fname,
                               "prop": "imageinfo|categories", "iiprop": "url|size|extmetadata",
                               "iiurlwidth": 1920, "cllimit": "max"})
                pages = (r.json().get("query") or {}).get("pages") or {}
                out = []
                for p in pages.values():
                    if "missing" in p:
                        continue
                    info = (p.get("imageinfo") or [{}])[0]
                    if info.get("width", 0) < 400:
                        continue
                    # портреты бывают небольшими — для людей допускаем от 400 px
                    p2 = dict(p)
                    p2["imageinfo"] = [dict(info, width=max(info.get("width", 0), 1000),
                                            height=max(info.get("height", 0), 560))]
                    it = self._wm_item(p2, 400, 400)
                    if it:
                        out.append(it)
                return out
            except requests.ConnectionError:
                return []
            except Exception as e:
                self.log(f"  Википедия ({lang}): {e}")
                return []
        return self._cached(("wp", lang, name), fetch)

    # ---------- Pexels ----------
    def _pexels(self, query):
        api_key = self.cfg.get("pexels_key", "").strip()
        if not api_key or self._down.get("px"):
            return []

        def fetch():
            out = []
            try:
                r = requests.get("https://api.pexels.com/videos/search",
                                 params={"query": query, "per_page": 30, "orientation": "landscape"},
                                 headers={"Authorization": api_key}, timeout=30)
                for v in r.json().get("videos", []):
                    slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
                    if BLOCK_WORDS.search(slug):
                        continue
                    files = [f for f in v.get("video_files", [])
                             if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1920]
                    if not files:
                        continue
                    best = min(files, key=lambda f: f.get("width") or 0)
                    out.append({
                        "kind": "video", "url": best["link"], "duration": v.get("duration", 0),
                        "thumb": v.get("image") or "", "source": "Pexels", "page": v.get("url", ""),
                        "title": "", "author": (v.get("user") or {}).get("name", ""),
                        "license": "Pexels License", "tags": slug,
                    })
            except requests.ConnectionError as e:
                self._down["px"] = True
                self.log(f"  Pexels недоступен с этого компьютера — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Pexels: {e}")
            return out
        return self._cached(("px", query), fetch)

    # ---------- Pixabay ----------
    def _pixabay(self, query):
        """Видео Pixabay в Full HD и выше (самая лёгкая версия не ниже 1920 px)."""
        api_key = self.cfg.get("pixabay_key", "").strip()
        if not api_key or self._down.get("pb"):
            return []

        def fetch():
            good, low = [], []
            try:
                r = self._get("https://pixabay.com/api/videos/",
                              {"key": api_key, "q": query[:100], "per_page": 50,
                               "video_type": "film", "safesearch": "true"}, pixabay=True)
                for v in r.json().get("hits", []):
                    tags = v.get("tags", "")
                    if BLOCK_WORDS.search(tags) or (v.get("duration") or 0) < 3:
                        continue
                    files = [f for f in (v.get("videos") or {}).values()
                             if isinstance(f, dict) and f.get("url")
                             and (f.get("width") or 0) > (f.get("height") or 0)]
                    hd = [f for f in files if (f.get("width") or 0) >= 1920]
                    mid = [f for f in files if (f.get("width") or 0) >= 1280]
                    if not mid:
                        continue
                    best = min(hd, key=lambda f: f["width"]) if hd else max(mid, key=lambda f: f["width"])
                    thumb = next((f.get("thumbnail") for f in (v.get("videos") or {}).values()
                                  if isinstance(f, dict) and f.get("thumbnail")), "")
                    item = {
                        "kind": "video", "url": best["url"], "duration": v.get("duration", 0), "thumb": thumb,
                        "source": "Pixabay", "page": v.get("pageURL", ""), "title": "",
                        "author": v.get("user", ""), "license": "Pixabay Content License", "tags": tags,
                    }
                    (good if hd else low).append(item)
            except requests.ConnectionError as e:
                self._down["pb"] = True
                self.log(f"  Pixabay недоступен с этого компьютера — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Pixabay: {e}")
            return good + low            # ролики ниже Full HD — только в самом конце
        return self._cached(("pb", query), fetch)

    def _pixabay_photos(self, query):
        """Фото Pixabay (тот же ключ) — запасной вариант, если видео нет."""
        api_key = self.cfg.get("pixabay_key", "").strip()
        if not api_key or self._down.get("pb"):
            return []

        def fetch():
            out = []
            try:
                r = self._get("https://pixabay.com/api/",
                              {"key": api_key, "q": query[:100], "image_type": "photo",
                               "orientation": "horizontal", "min_width": 1920, "safesearch": "true",
                               "per_page": 30}, pixabay=True)
                for h in r.json().get("hits", []):
                    if BLOCK_WORDS.search(h.get("tags", "")):
                        continue
                    url = h.get("imageURL") or h.get("fullHDURL") or h.get("largeImageURL")
                    if not url:
                        continue
                    out.append({
                        "kind": "image", "url": url, "thumb": h.get("webformatURL") or url,
                        "source": "Pixabay", "page": h.get("pageURL", ""), "title": "",
                        "author": h.get("user", ""), "license": "Pixabay Content License",
                        "tags": h.get("tags", ""),
                    })
            except requests.ConnectionError as e:
                self._down["pb"] = True
                self.log(f"  Pixabay недоступен с этого компьютера — дальше без него ({type(e).__name__}).")
            except Exception as e:
                self.log(f"  Pixabay (фото): {e}")
            return out
        return self._cached(("pbi", query), fetch)

    def _stock(self, query):
        """Видео: своя библиотека (footage), затем Pixabay и Pexels."""
        own = library.search(self.library, query) if self.library else []
        if not self._warned_pexels and not self.library and not (self.cfg.get("pexels_key", "").strip()
                                                                 or self.cfg.get("pixabay_key", "").strip()):
            self.log("  Нет ни библиотеки футажей, ни ключа Pixabay — видеокадров не будет, только архивные фото.")
            self._warned_pexels = True
        a = self._pixabay(query)
        b = self._pexels(query)
        mixed = []
        for i in range(max(len(a), len(b))):
            mixed += a[i:i + 1] + b[i:i + 1]
        return own + mixed

    # ---------- скачивание ----------
    def _download(self, item):
        if item.get("local"):
            return Path(item["local"]) if Path(item["local"]).exists() else None
        ext = ".mp4" if item["kind"] == "video" else ".jpg"
        name = hashlib.md5(item["url"].encode()).hexdigest()[:16] + ext
        path = self.media_dir / name
        if path.exists() and path.stat().st_size > 1000:
            return path
        tmp = path.with_suffix(ext + ".part")
        if item.get("segment"):                    # из длинного архивного фильма — только нужный кусок
            start, dur = item["segment"]
            import subprocess
            from .config import ffmpeg_exe
            try:
                subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
                                "-i", item["url"], "-t", str(dur), "-an", "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "18", "-f", "mp4", str(tmp)], timeout=300, check=True,
                               creationflags=0x08000000 if os.name == "nt" else 0)
                tmp.replace(path)
                return path
            except Exception:
                tmp.unlink(missing_ok=True)
                return None
        for attempt in range(3):
            try:
                with requests.get(item["url"], headers=UA, stream=True, timeout=60) as r:
                    if r.status_code in (401, 403, 410):
                        r = _DIRECT.get(item["url"], headers=UA, stream=True, timeout=60)
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                tmp.replace(path)
                return path
            except Exception:
                time.sleep(3)
        tmp.unlink(missing_ok=True)
        return None

    def _candidates(self, visual, queries):
        """Сначала ВИДЕО по всем вариантам запроса, фото — только если видео не хватило.
        archive — реальное фото места/документа: Wikimedia, затем видео, затем фото Pixabay."""
        if visual == "archive":
            order = [self._wikimedia, self._loc, self._ia_video, self._met, self._aic, self._openverse,
                     self._stock, self._pixabay_photos]
        elif visual == "photo":
            order = [self._pixabay_photos, self._wikimedia, self._met, self._aic, self._openverse, self._stock]
        else:
            order = [self._stock, self._ia_video, self._pixabay_photos, self._wikimedia, self._met]
        for src in order:
            for q in queries:
                yield from src(q)

    @staticmethod
    def _variants(query):
        words = query.split()
        out = [query]
        if len(words) > 3:
            out.append(" ".join(words[:3]))
        if len(words) > 2:
            out += [" ".join(words[:2]), " ".join(words[-2:])]
        seen, res = set(), []
        for q in out:
            if q not in seen:
                seen.add(q)
                res.append(q)
        return res

    def _claim(self, item):
        with self._lock:
            if item["url"] in self.used:
                return False
            self.used.add(item["url"])
            return True

    def _accept(self, item, found):
        if not self._claim(item):
            return False
        path = self._download(item)
        if not path:
            return False
        found.append({"path": str(path), "kind": item["kind"], "duration": item.get("duration", 0),
                      "credit": item, "url": item["url"]})
        with self._lock:
            self.credits.append(item)
        return True

    def _wiki_title(self, name, lang):
        """Точное название статьи Википедии о человеке (поиск по имени)."""
        def fetch():
            try:
                r = self._get(f"https://{lang}.wikipedia.org/w/api.php",
                              {"action": "query", "format": "json", "list": "search", "srsearch": name,
                               "srlimit": 3, "srnamespace": 0})
                hits = (r.json().get("query") or {}).get("search") or []
                return [h.get("title", "") for h in hits if h.get("title")]
            except Exception:
                return []
        return self._cached(("wt", lang, name), fetch)

    def _person_photos(self, person, alt=""):
        """Фото конкретного человека: главное фото его статьи в Википедии (en — по английскому имени,
        ru — по русскому), затем файлы архива, где в названии есть его фамилия. Только свободные лицензии."""
        names = [n for n in dict.fromkeys([person.strip(), (alt or "").strip()]) if n]
        surnames = {n.split()[-1].lower().strip(",") for n in names}
        for n in names:                                   # «Дятлов, Игорь» → фамилия первой
            if "," in n:
                surnames.add(n.split(",")[0].lower())
        seen, out = set(), []

        def add(cands, need_name=False):
            for c in cands:
                text = (c.get("title", "") + " " + c.get("page", "")).lower()
                if c["url"] in seen or (need_name and not any(sn in text for sn in surnames)):
                    continue
                seen.add(c["url"])
                out.append(c)
        for n in names:
            lang_order = ("ru", "en") if re.search("[а-яё]", n, re.I) else ("en", "ru")
            for lang in lang_order:
                add(self._wiki_page_image(n, lang))
                for t in self._wiki_title(n, lang)[:2]:
                    if any(sn in t.lower() for sn in surnames):
                        add(self._wiki_page_image(t, lang))
        for n in names:
            for q in (n, f"{n} portrait", f"{n} photograph"):
                add(self._wikimedia(q), need_name=True)
        latin = [n for n in names if not re.search("[а-яё]", n, re.I)]
        for n in latin:                                  # дальше — Библиотека Конгресса и Openverse
            if len(out) >= 6:
                break
            add(self._loc(n), need_name=True)
            add(self._openverse(n, min_w=400), need_name=True)
        return out

    def find_person(self, person, narration="", topic=""):
        """Одно фото человека или пусто."""
        pool = [c for c in self._person_photos(person, getattr(self, "person_alt", {}).get(person, ""))
                if c["url"] not in self.used and c.get("thumb")][:6]
        found = []
        if pool:
            order = vision.judge(self.cfg, narration or person, topic, pool, self.log, person=person)
            if not order and not vision.available():
                order = [0]
            for i in order[:1]:
                self._accept(pool[i], found)
        return found

    def find(self, visual, query, count, narration="", topic="", person=""):
        """До count кадров без повторов по всему видео, лучшие по смыслу первыми.
        Каждый кадр проверяется нейросетью: смысл, водяные знаки, анахронизмы."""
        if visual == "person":
            return self.find_person(query, narration, topic)
        found = []
        if not self.cfg.get("strict_visuals", True):
            pool = [c for c in self._candidates(visual, self._variants(query))]
            pool.sort(key=lambda c: -_overlap(query, c.get("tags", "")) - (0.5 if c["kind"] == "video" else 0))
            for item in pool:
                if len(found) >= count:
                    break
                self._accept(item, found)
            return found

        seen = set()
        reserve = []
        tried = self._variants(query)
        rounds = [tried]
        judged = 0
        budget = int(self.cfg.get("max_checked_per_segment", 48))
        r = 0
        while r < len(rounds) and len(found) < count and judged < budget:
            pool = [c for c in self._candidates(visual, rounds[r])
                    if c["url"] not in self.used and c["url"] not in seen and c.get("thumb")]
            # без проверки нейросетью — по совпадению тегов с запросом
            if not vision.available():
                pool.sort(key=lambda c: -_overlap(query, c.get("tags", "")))
            if visual != "photo":                     # сначала все видео, фото — только если видео не хватило
                pool.sort(key=lambda c: 0 if c["kind"] == "video" else 1)
            pos = [0]
            while len(found) < count and judged < budget:
                batch = []
                while pos[0] < len(pool) and len(batch) < 8:
                    c = pool[pos[0]]
                    if batch and c["kind"] != batch[0]["kind"]:
                        break                             # в одной проверке — только видео или только фото
                    pos[0] += 1
                    if c["url"] in seen:
                        continue
                    seen.add(c["url"])
                    batch.append(c)
                if not batch:
                    break
                judged += len(batch)
                min_rel = float(self.cfg.get("min_relevance", 7))
                for i, rel in vision.judge(self.cfg, narration or query, topic, batch, self.log,
                                           query=query, scores=True):
                    if not vision.available() and _overlap(query, batch[i].get("tags", "")) == 0 and \
                            batch[i]["source"] != "library":
                        reserve.append((0.0, batch[i]))
                        continue
                    if rel and rel < min_rel:
                        reserve.append((rel, batch[i]))
                        continue
                    if len(found) >= count:
                        reserve.append((rel or 9.0, batch[i]))
                        continue
                    self._accept(batch[i], found)
            r += 1
            if r == len(rounds) and len(found) < count and len(rounds) < 3 and judged < budget:
                alt = vision.alt_queries(self.cfg, narration or query, tried, self.log)
                alt = [q for q in alt if q not in tried]
                if alt:
                    tried += alt
                    rounds.append(alt)
        # запасные кадры (оценка 5–6) — лучше, чем повтор одного и того же
        reserve.sort(key=lambda x: -x[0])
        for rel, c in reserve:
            if len(found) >= count:
                break
            if rel >= float(self.cfg.get("reserve_relevance", 5)) or (not vision.available() and rel == 0.0):
                self._accept(c, found)
        if len(found) < count:
            self.log(f"  «{query}»: подходящих кадров {len(found)} из {count} (проверено {judged}).")
        return found

    def credits_text(self):
        lines = []
        seen = set()
        for c in self.credits:
            if c["page"] in seen:
                continue
            seen.add(c["page"])
            if c["source"] == "library":
                continue
            if c["source"] == "Wikimedia Commons":
                lines.append(f"{c['title']} — {c['author']}, {c['license']}, {c['page']}")
            else:
                what = "Фото" if c.get("kind") == "image" else "Видео"
                lines.append(f"{what}: {c['author']} ({c['source']}) {c['page']}")
        return "\n".join(lines)
