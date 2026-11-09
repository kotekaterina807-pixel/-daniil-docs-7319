import os
"""Поиск, проверка и скачивание визуала: видео Pixabay (главный источник), Wikimedia Commons (архив, люди),
фото Pixabay — только если видео не нашлось."""
import hashlib
import html
import json
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
# CC BY / CC BY-SA — можно, автор указывается в описании видео. NC/ND и «все права защищены» — нет.
CLEAN_LICENSE = re.compile(r"public domain|\bpd\b|\bpd-|cc0|no restrictions|cc[ -]?by(?![ -]?nc)(?![ -]?nd)", re.I)
NOT_CLEAN_LICENSE = re.compile(r"copyrighted|fair use|all rights|\bnc\b|non-?commercial|\bnd\b|no ?deriv", re.I)

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


NOT_PORTRAIT = re.compile(
    r"memorial|monument|statue|plaque|grave|tomb|headstone|cemetery|bust\b|sculpture|stamp|coin|medal|"
    r"stained[ _-]?glass|church|window|signature|autograph|mural|graffiti|памятник|мемориал|могил|надгроб|"
    r"бюст|скульптур|марка|табличк|доска",
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
            r = _direct_retry(url, params, timeout, self._fetch(url, params, timeout))
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            return r
        return r

    @staticmethod
    def _fetch(url, params, timeout):
        """Обрыв связи (прокси моргнул, SSL-сбой) — не повод выключать источник на всю сборку:
        ещё 2 попытки через прокси, затем напрямую. Ошибка — только если не вышло ни так, ни так."""
        import os as _os
        err = None
        for k in range(3):
            try:
                return requests.get(url, params=params, headers=UA, timeout=timeout)
            except requests.ConnectionError as e:
                err = e
                time.sleep(1.5 * (k + 1))
        if _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy"):
            try:
                return _DIRECT.get(url, params=params, headers=UA, timeout=timeout)
            except requests.RequestException:
                pass
        raise err

    def _diag(self, person, msg):
        """Почему для человека нашлось/не нашлось фото — попадает в журнал и в отчёт для Claude."""
        with self._lock:
            d = self.__dict__.setdefault("person_diag", {}).setdefault(person, [])
            if msg not in d:
                d.append(msg)

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
    def _wm_item(p, min_w=1000, min_h=560, check_cats=True):
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
        if ARCHIVE_BLOCK.search(p.get("title", "") + (" " + cats if check_cats else "")):
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

    def _commons_category(self, cat, depth=1, limit=120):
        """Все фото раздела Wikimedia Commons (и его подразделов на depth уровней): подлинные снимки темы
        или человека — например плёнки самой группы Дятлова. Только свободные лицензии."""
        if self._down.get("wm"):
            return []
        cat = cat if cat.lower().startswith("category:") else "Category:" + cat

        def fetch():
            out, seen_cats, queue = [], set(), [(cat, 0)]
            while queue and len(out) < limit:
                c, d = queue.pop(0)
                if c in seen_cats:
                    continue
                seen_cats.add(c)
                try:
                    r = self._get("https://commons.wikimedia.org/w/api.php", {
                        "action": "query", "format": "json", "generator": "categorymembers", "gcmtitle": c,
                        "gcmtype": "file|subcat", "gcmlimit": 100, "prop": "imageinfo|categories",
                        "iiprop": "url|size|extmetadata", "iiurlwidth": 1920, "cllimit": "max"})
                    pages = (r.json().get("query") or {}).get("pages") or {}
                except requests.ConnectionError:
                    self._down["wm"] = True
                    break
                except Exception:
                    continue
                for p in pages.values():
                    t = p.get("title", "")
                    if t.startswith("Category:"):
                        if d < depth and len(seen_cats) < 12:
                            queue.append((t, d + 1))
                        continue
                    if not re.search(r"\.(jpe?g|png|tiff?)$", t, re.I):
                        continue
                    it = self._wm_item(p, min_w=500, min_h=350, check_cats=False)
                    if it:
                        it["source"] = "Wikimedia Commons (архив темы)"
                        it["topic"] = True
                        out.append(it)
            return out
        return self._cached(("wmcat", cat, depth), fetch)

    def topic_archive(self, terms):
        """Архив темы: разделы Commons, найденные по названию темы (#! АРХИВ в сценарии)."""
        out, seen = [], set()
        for term in [t.strip() for t in terms if t and t.strip()][:4]:
            cats = [term] if term.lower().startswith("category:") else []
            if not cats:
                try:
                    r = self._get("https://commons.wikimedia.org/w/api.php", {
                        "action": "query", "format": "json", "list": "search", "srsearch": term,
                        "srnamespace": 14, "srlimit": 4})
                    hits = [h.get("title", "") for h in (r.json().get("query") or {}).get("search") or []]
                    key = [w for w in re.findall(r"\w{4,}", term.lower())]
                    cats = [h for h in hits if any(w in h.lower() for w in key)][:2]
                except Exception:
                    cats = []
            for c in cats:
                for it in self._commons_category(c):
                    if it["url"] not in seen:
                        seen.add(it["url"])
                        out.append(it)
        if out:
            self.log(f"  Архив темы (Wikimedia Commons): {len(out)} подлинных фото.")
        return out

    # ---------- Поиск картинок в интернете (DuckDuckGo, без ключа) ----------
    STOCK_SITES = re.compile(r"getty|shutterstock|alamy|istock|dreamstime|depositphotos|123rf|pond5|stock\.adobe|"
                             r"bigstock|agefotostock|superstock|granger|bridgemanimages|pinterest|facebook|"
                             r"instagram|tiktok|youtube|ytimg|vk\.com|userapi|aliexpress|amazon|ebay|etsy|"
                             r"redbubble|teepublic|zazzle|fineartamerica|pixels\.com|mediastorehouse|"
                             # знаменитости, кино, игры, таблоиды — чужие права и не по теме
                             r"netflix|imdb|media-amazon|people\.com|eonline|hellomagazine|hello!|dailymail|"
                             r"thesun\.co|mirror\.co\.uk|tmz|usmagazine|vogue|elle\.|harpersbazaar|cosmopolitan|"
                             r"glamour|buzzfeed|boredpanda|fandom|wikia|gamepedia|game-maps|ign\.com|gamespot|"
                             r"wallpaper|hdwallpapers|wallhaven|deviantart|artstation|giphy|tenor|memes?\b|"
                             r"wireimage|filmmagic|rexfeatures|shutterfly|apimages|reuters|afp\.com|epa\.eu", re.I)
    OFF_TOPIC = re.compile(r"then and now|celebrit|actress|actor\b|red carpet|premiere|baywatch|bikini|swimsuit|"
                           r"netflix|movie poster|film still|trailer|video game|fallout|minecraft|fortnite|"
                           r"\bmeme|clip ?art|cartoon|vector|\blogo\b|\bicon\b|weather forecast|"
                           r"dog breeds?|funny|emoji|infographic|template|mockup|stock photo", re.I)

    def _bing_images(self, query, min_w):
        """Поиск картинок Bing (как в браузере). Возвращает сырые результаты [{image, thumbnail, url, title, width, height}]."""
        r = requests.get("https://www.bing.com/images/search",
                         params={"q": query, "form": "HDRSC2", "first": 1, "qft": "+filterui:imagesize-large"},
                         headers=dict(BROWSER_UA, **{"Accept-Language": "en-US,en;q=0.8"}), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Bing ответил {r.status_code}")
        res = []
        for m in re.finditer(r'm="(\{[^"]+\})"', r.text):
            try:
                d = json.loads(html.unescape(m.group(1)))
            except Exception:
                continue
            if d.get("murl"):
                res.append({"image": d["murl"], "thumbnail": d.get("turl") or d["murl"], "url": d.get("purl") or "",
                            "title": d.get("t") or "", "width": 0, "height": 0})
        return res

    def _web_filter(self, raw, min_w):
        out = []
        for res in raw[:50]:
            url, src = res.get("image") or "", (res.get("url") or "") + " " + (res.get("source") or "")
            w, h = int(res.get("width") or 0), int(res.get("height") or 0)
            title = res.get("title") or ""
            if not url.startswith("http") or (w and (w < min_w or h < 300)):   # размер неизвестен — проверим при скачивании
                continue
            if self.STOCK_SITES.search(url + " " + src) or ARCHIVE_BLOCK.search(title) or \
                    self.OFF_TOPIC.search(title + " " + url):
                continue
            if re.search(r"\.(svg|gif)(\?|$)", url, re.I):
                continue
            dom = re.sub(r"^https?://(www\.)?", "", res.get("url") or url).split("/")[0]
            out.append({"kind": "image", "url": url, "thumb": res.get("thumbnail") or url, "web": True,
                        "source": "Интернет: " + dom, "page": res.get("url") or "", "title": title,
                        "author": dom, "license": "из интернета", "tags": title})
        return out

    def _web_images(self, query, min_w=500):
        """Картинки из интернета по запросу — как поиск картинок в браузере. Стоковые агентства с водяными
        знаками и магазины отбрасываются; каждую картинку дальше смотрит Claude (смысл, знаки, надписи)."""
        if self._down.get("web") and time.time() - float(self._down["web"]) < 600:
            return []                    # связь пропала — 10 минут не пробуем, потом снова

        def fetch():
            out = []
            try:
                raw = []
                try:
                    raw = self._bing_images(query, min_w)
                except Exception as e:
                    self._diag(query, f"Bing: {e}")
                if raw:
                    return self._web_filter(raw, min_w)
                s = requests.Session()
                s.headers.update(BROWSER_UA)
                s.headers["Accept"] = "text/html,application/json;q=0.9,*/*;q=0.8"
                page = s.get("https://duckduckgo.com/", params={"q": query, "iax": "images", "ia": "images"}, timeout=20)
                m = re.search(r"vqd=[\"']?([\d-]+)", page.text)
                if not m:
                    self._diag(query, f"DuckDuckGo: не отдал страницу поиска ({page.status_code})")
                    return out
                s.headers["Referer"] = "https://duckduckgo.com/"
                r = s.get("https://duckduckgo.com/i.js", params={"l": "wt-wt", "o": "json", "q": query, "vqd": m.group(1),
                                                                  "f": ",,,,,", "p": "1"}, timeout=20)
                if r.status_code != 200:
                    return out
                return self._web_filter(r.json().get("results") or [], min_w)
            except requests.ConnectionError as e:
                if not self._down.get("web"):
                    self.log(f"  Поиск картинок в интернете не ответил ({type(e).__name__}) — попробую снова через 10 минут.")
                self._down["web"] = time.time()
                raise
            except Exception as e:
                self.log(f"  Поиск картинок в интернете: {e}")
            return out
        try:
            return self._cached(("web", query, min_w), fetch)
        except requests.ConnectionError:
            return []                    # неудачный поиск не запоминаем — повторится, когда связь вернётся

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
                if r.status_code in (401, 403, 410):
                    self._down["loc"] = True             # сайт закрыт для этого подключения — не тратим время
                    return out
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
                if r.status_code in (401, 403, 410):
                    self._down["met"] = True
                    return out
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
                    self._diag(name, f"Википедия {lang}: у статьи нет фото")
                    return []
                q = {"action": "query", "format": "json", "titles": "File:" + fname,
                     "prop": "imageinfo|categories", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920, "cllimit": "max"}
                pages = (self._get("https://commons.wikimedia.org/w/api.php", q).json().get("query") or {}).get("pages") or {}
                if all("missing" in p for p in pages.values()):
                    # старые фото часто лежат не в Commons, а в самой Википедии (с пометкой «общественное достояние»)
                    pages = (self._get(f"https://{lang}.wikipedia.org/w/api.php", q).json().get("query") or {}).get("pages") or {}
                out = []
                for p in pages.values():
                    if "imageinfo" not in p:
                        continue
                    info = (p.get("imageinfo") or [{}])[0]
                    if info.get("width", 0) < 220:
                        self._diag(name, f"Википедия {lang}: фото {fname} слишком маленькое ({info.get('width')} px)")
                        continue
                    # старые портреты бывают небольшими — для людей допускаем от 220 px (служебные категории
                    # Википедии вроде «…template…» не проверяем: лицензию уже проверили)
                    p2 = dict(p)
                    p2["imageinfo"] = [dict(info, width=max(info.get("width", 0), 1000),
                                            height=max(info.get("height", 0), 560))]
                    it = self._wm_item(p2, 220, 220, check_cats=False)
                    if it and "/1920px-" not in it["url"] and info.get("url"):
                        it["url"] = info["url"]                 # маленький оригинал — берём его целиком
                    if it:
                        out.append(it)
                    else:
                        lic = ((info.get("extmetadata") or {}).get("LicenseShortName") or {}).get("value", "?")
                        self._diag(name, f"Википедия {lang}: фото {fname} не взято (лицензия «{lic}»)")
                if out:
                    self._diag(name, f"Википедия {lang}: фото найдено")
                return out
            except requests.ConnectionError as e:
                self._diag(name, f"Википедия {lang}: нет связи ({type(e).__name__})")
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
                hdr = dict(BROWSER_UA, Referer=item.get("page") or "") if item.get("web") else UA
                with requests.get(item["url"], headers=hdr, stream=True, timeout=60) as r:
                    if r.status_code in (401, 403, 410):
                        r = _DIRECT.get(item["url"], headers=UA, stream=True, timeout=60)
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                if item["kind"] == "image":            # сайт мог отдать страницу вместо картинки
                    from PIL import Image as _I
                    conv = None
                    with _I.open(tmp) as im:
                        im.load()
                        if im.width < 300 or im.height < 250:
                            raise ValueError("слишком маленькая картинка")
                        if im.mode not in ("RGB", "L") or (im.format or "").upper() not in ("JPEG", "PNG"):
                            conv = im.convert("RGB")
                    if conv is not None:
                        conv.save(tmp, "JPEG", quality=92)
                tmp.replace(path)
                return path
            except Exception:
                if item.get("web"):                    # чужой сайт не отдал картинку — повторять бесполезно
                    break
                time.sleep(3)
        tmp.unlink(missing_ok=True)
        return None

    def _candidates(self, visual, queries):
        """Сначала ВИДЕО по всем вариантам запроса, фото — только если видео не хватило.
        archive — реальное фото места/документа: Wikimedia, затем видео, затем фото Pixabay."""
        if visual == "archive":
            # музеи (Met, Чикаго) отдают много картин «примерно на тему» — они в конце;
            # точный поиск картинок по названию (как в браузере) — сразу после Wikimedia
            order = [self._wikimedia, self._web_images, self._loc, self._ia_video, self._openverse,
                     self._stock, self._pixabay_photos, self._aic, self._met]
        elif visual == "photo":
            order = [self._web_images, self._wikimedia, self._pixabay_photos, self._openverse, self._stock,
                     self._aic, self._met]
        else:
            order = [self._stock, self._ia_video, self._web_images, self._pixabay_photos, self._wikimedia]
        for src in order:
            for q in queries:
                for c in src(q):
                    st = self.__dict__.setdefault("src_stats", {})
                    k = getattr(src, "__name__", "?")
                    st[k] = st.get(k, 0) + 1
                    yield c

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

    def _person_photos(self, person, alt="", when=""):
        """Фото конкретного человека: главное фото его статьи в Википедии (en — по английскому имени,
        ru — по русскому), затем файлы архива, где в названии есть его фамилия. Только свободные лицензии."""
        names = [n for n in dict.fromkeys([person.strip(), (alt or "").strip()]) if n]
        # фото, указанное в сценарии (#! ФОТО: Имя = ссылка) — подлинное, его и берём первым
        manual = getattr(self, "manual_photos", None) or {}
        if manual:
            fold = lambda x: re.sub(r"[^a-zа-яё]", "", x.lower().replace("j", "y").replace("w", "v"))
            keys = {fold(n.split()[-1]) for n in names if n.split()}
            for nm, url in manual.items():
                if nm.split() and fold(nm.split()[-1]) in keys and url.startswith("http"):
                    return [{"kind": "image", "url": url, "thumb": url, "source": "указано в сценарии",
                             "page": url, "title": nm, "author": "архив", "license": "указано в сценарии",
                             "tags": nm, "person": True}]
        surnames = {n.split()[-1].lower().strip(",") for n in names}
        for n in names:                                   # «Дятлов, Игорь» → фамилия первой
            if "," in n:
                surnames.add(n.split(",")[0].lower())
        seen, out = set(), []
        # полное имя: фамилия отдельным словом (не «Irvine-Smith») + имя или инициал.
        # Иначе находились однофамильцы: «Stella Mallory» вместо Джорджа Мэллори.
        full = []
        for n in names:
            parts = [x.strip(",.").lower() for x in n.replace(",", " ").split() if x.strip(",.")]
            if len(parts) >= 2:
                sur, first = (parts[0], parts[1]) if "," in n else (parts[-1], parts[0])
                full.append((sur, first))

        def name_ok(text):
            t = text.replace("_", " ").replace("%20", " ")
            for sur, first in full:
                if not re.search(rf"(?<![\w-]){re.escape(sur)}(?![\w-])", t):
                    continue
                if re.search(rf"(?<![\w-]){re.escape(first)}(?![\w-])", t) or \
                        re.search(rf"(?<![\w-]){re.escape(first[0])}\.", t):
                    return True
            return not full and any(sn in t for sn in surnames)

        def add(cands, need_name=False):
            for c in cands:
                text = (c.get("title", "") + " " + c.get("page", "")).lower()
                if NOT_PORTRAIT.search(text + " " + c.get("tags", "")):
                    continue                            # памятники, таблички, могилы, марки — не фото человека
                if c["url"] in seen or (need_name and not name_ok(text)):
                    continue
                seen.add(c["url"])
                out.append(c)
        if when:                                          # «25-летний врач в 1884-м» — фото того времени, а не старика
            for n in [x for x in names if not re.search("[а-яё]", x, re.I)][:1]:
                add(self._wikimedia(f"{n} {when}"), need_name=True)
                add(self._web_images(f"{n} {when} photo", min_w=300), need_name=True)
                add(self._web_images(f"young {n} photo", min_w=300), need_name=True)
            if out:
                self._diag(person, f"фото {when} года: {len(out)}")
        for n in names:
            lang_order = ("ru", "en") if re.search("[а-яё]", n, re.I) else ("en", "ru")
            for lang in lang_order:
                add(self._wiki_page_image(n, lang))
                for t in self._wiki_title(n, lang)[:2]:
                    # только статья именно об этом человеке («Glenn Miller», «Andrew Irvine (mountaineer)»),
                    # а не об однофамильце с похожим именем («Frazier Glenn Miller Jr.»)
                    if name_ok(t.lower()) and re.sub(r"\s*\(.*?\)", "", t).strip().lower() in [x.lower() for x in names]:
                        add(self._wiki_page_image(t, lang))
        for n in [x for x in names if not re.search("[а-яё]", x, re.I)][:1]:
            # раздел Commons «Category:Имя Фамилия» — там подлинные фото именно этого человека
            add(self._commons_category(n, depth=0, limit=12))
        # подлинные фото темы (архив Commons), где в названии файла есть его имя и фамилия
        add([c for c in getattr(self, "topic_items", None) or [] if c.get("kind") == "image"], need_name=True)
        for n in names:
            for q in (n, f"{n} portrait", f"{n} photograph"):
                add(self._wikimedia(q), need_name=True)
        latin = [n for n in names if not re.search("[а-яё]", n, re.I)]
        for n in latin:                                  # дальше — Библиотека Конгресса и Openverse
            if len(out) >= 6:
                break
            add(self._loc(n), need_name=True)
            add(self._openverse(n, min_w=400), need_name=True)
        hint = (getattr(self, "person_hint", "") or "").strip()
        if hint and len(out) < 3:                        # имя + тема ролика: «Zinaida Kolmogorova Dyatlov Pass»
            for n in names[:2]:
                add(self._web_images(f"{n} {hint}", min_w=250), need_name=True)
                add(self._web_images(f"{n} {hint} photo", min_w=250), need_name=True)
                add(self._wikimedia(f"{n} {hint}"), need_name=True)
            if len(out):
                self._diag(person, f"поиск с темой «{hint}»: фото {len(out)}")
        before = len(out)
        for n in names:                                  # и поиск фото в интернете — как в браузере
            if len(out) >= 8:
                break
            add(self._web_images(f"{n} photo", min_w=300), need_name=True)
            add(self._web_images(f"{n} portrait", min_w=300), need_name=True)
        if len(out) > before:
            self._diag(person, f"интернет: фото {len(out) - before}")
        if not out:
            self._diag(person, "ни в одном источнике нет фото с полным именем")
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
