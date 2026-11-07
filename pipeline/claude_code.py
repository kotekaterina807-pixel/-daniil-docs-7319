"""Проверка кадров через Claude Code — по подписке Claude (Pro/Max), без оплаты API.
Программа собирает листы с превью кандидатов (pipeline/review.py) и отдаёт каждый лист
программе Claude Code (`claude -p`). Claude смотрит лист и отвечает номерами подходящих кадров,
а если подходящих нет — предлагает новый поисковый запрос."""
import os
import re
import shutil
import subprocess
from pathlib import Path

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ClaudeCodeError(Exception):
    pass


def find_exe():
    """Путь к Claude Code (не к приложению Claude для ПК)."""
    cands = []
    w = shutil.which("claude")
    if w:
        cands.append(Path(w))
    home = Path.home()
    cands += [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude",
              Path(os.environ.get("APPDATA", home)) / "npm" / "claude.cmd"]
    for p in cands:
        if p.exists() and "anthropicclaude" not in str(p).lower().replace(" ", ""):
            return p
    return None


def _env(cfg):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.pop("DOCUSTUDIO_CHILD", None)
    proxy = str(cfg.get("proxy_url", "")).strip()
    if proxy:                                     # Claude Code идёт в интернет через прокси программы
        from . import proxybridge                 # SOCKS он не понимает — даём ему локальный HTTP-мост
        proxy = proxybridge.http_proxy_for(proxy)
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[k] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
    return env


def open_login(cfg):
    """Открывает Claude Code в отдельном окне (с прокси программы) — для входа по подписке."""
    exe = find_exe()
    if not exe:
        return {"ok": False, "warn": True, "message": "Claude Code не найден. Установите его: PowerShell → "
                "irm https://claude.ai/install.ps1 | iex"}
    proxy = str(cfg.get("proxy_url", "")).strip()
    warn = ""
    if not proxy:
        warn = " Прокси программы не указан — если Claude Code напишет «Unable to connect», впишите прокси в поле ниже."

    if os.name != "nt":
        return {"ok": False, "warn": True, "message": "Вход открывается только на Windows."}
    cmd = [str(exe)]
    if str(exe).lower().endswith(".cmd"):
        cmd = ["cmd", "/k"] + cmd
    try:
        subprocess.Popen(cmd, env=_env(cfg), cwd=str(Path.home()), creationflags=0x00000010)  # новое окно консоли
    except OSError as e:
        return {"ok": False, "warn": True, "message": f"Не удалось открыть Claude Code: {e}"}
    return {"ok": True, "warn": bool(warn), "message": "Открыл окно Claude Code: выберите «Claude account with "
            "subscription», войдите в браузере, затем /exit." + warn}


LOG = None          # куда писать «жду сброса лимита» (ставит сборка)


def run(cfg, prompt, workdir, timeout=900):
    """Один запрос к Claude Code. Если лимит подписки на этот период исчерпан (на 30-минутном видео
    запросов много), не роняем сборку: ждём сброса лимита (проверка раз в 15 минут, до 6 часов)."""
    import time as _t
    for k in range(25):
        try:
            return _run_once(cfg, prompt, workdir, timeout)
        except ClaudeCodeError as e:
            if "лимит подписки" not in str(e) or k == 24:
                raise
            if LOG:
                LOG(f"  Лимит подписки Claude на этот период исчерпан — жду сброса и продолжу сам "
                    f"(проверка через 15 минут, попытка {k + 1}).")
            _t.sleep(900)


def _run_once(cfg, prompt, workdir, timeout=900):
    """Один запрос к Claude Code. Текст задания идёт через stdin (без проблем с кавычками в Windows)."""
    exe = find_exe()
    if not exe:
        raise ClaudeCodeError("Claude Code не установлен. Установите его (PowerShell: "
                              "irm https://claude.ai/install.ps1 | iex), затем войдите: claude → /login.")
    cmd = [str(exe), "-p", "--output-format", "text", "--allowedTools", "Read",
           "--model", cfg.get("claude_code_model") or "sonnet"]
    if str(exe).lower().endswith(".cmd"):
        cmd = ["cmd", "/c"] + cmd
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(workdir), env=_env(cfg), timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError("Claude Code не ответил за отведённое время.")
    except OSError as e:
        raise ClaudeCodeError(f"Не удалось запустить Claude Code: {e}")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    low = (out + " " + err).lower()
    if "login" in low and ("please run" in low or "not logged" in low or "invalid api key" in low):
        raise ClaudeCodeError("Claude Code не авторизован: откройте терминал, введите claude и выполните /login "
                              "(вход аккаунтом с подпиской).")
    if "usage limit" in low or "limit reached" in low or "rate limit" in low:
        raise ClaudeCodeError("Исчерпан лимит подписки Claude на этот период — дождитесь сброса лимита.")
    if p.returncode != 0 and not out:
        raise ClaudeCodeError(f"Claude Code: ошибка {p.returncode}: {(err or out)[:300]}")
    return out


def check(cfg, workdir):
    """Для «Проверить подключения»: установлен ли Claude Code и отвечает ли он по подписке."""
    ans = run(cfg, "Ответь одним словом: ok", workdir, timeout=180)
    return find_exe(), ans


PROMPT = """Ты подбираешь видеокадры для документального видео на YouTube. Главное — ТОЧНОСТЬ ПО РАССКАЗУ.
Открой файл {sheet} инструментом Read (это картинка). На нём строки сценария; у каждой строки —
номер, текст диктора, «В КАДРЕ» и до 8 пронумерованных превью (видео или фото).
Тема видео: {topic}

У строки может быть пометка «В КАДРЕ: …» — это то, что зритель ОБЯЗАН увидеть. Кадр, где этого
предмета или действия нет, не подходит, даже если он красивый и «в тему» (просто лес вместо раскопок,
просто стена вместо столба с надписью, руки с книгой вместо документа из истории — не подходят).

Для КАЖДОЙ строки выбери кадры, которые показывают ИМЕННО то, о чём говорит диктор в этой фразе:
то же место, тот же предмет, то же действие, та же эпоха и обстановка, что в истории.
Ничего не додумывай от себя: кадр не должен добавлять в историю того, чего в ней нет.
Если точного кадра нет — лучше спокойный нейтральный кадр места действия (природа, погода, пейзаж
той местности и эпохи), чем кадр с посторонними людьми или предметами.
ВОЗРАСТ: если во фразе год или возраст человека — бери фото, где он того возраста (молодой в 25, а не старик).
КОНКРЕТИКА: если названо конкретное (корабль, место, человек) — кадр именно с ним; общий пейзаж «на тему» — только
если конкретного кадра нет совсем.
ПЕРЕЧИСЛЕНИЕ: если в «В КАДРЕ» пункты через « / » (каюта / кладовая / бочки) — выбери по своему кадру на
КАЖДЫЙ пункт и пиши номера в том же порядке, что пункты: кадры пойдут ровно под эти слова. Нет кадра для
пункта — допиши запрос именно для него.
Порядок — от лучшего к худшему (для перечисления — в порядке пунктов). «нужно до N» — сколько разных кадров нужно этой строке; минимум 2,
если строка длиннее пары секунд. Фото подходящего предмета лучше, чем видео не про то.
Если подходящих меньше, чем нужно, возьми те, что подходят, и допиши запрос, чтобы найти ещё
(формат ниже). Одинаковых или почти одинаковых кадров не бери.

СТРОКИ «ЧЕЛОВЕК: …»: если среди превью есть фото именно этого человека (с пометкой «человек») —
бери ТОЛЬКО его фото, первым номером. Если его фото нет — бери кадры, связанные именно с ним
и с этой фразой (его профессия, техника, место, эпоха), без посторонних людей. Случайные люди
вместо названного человека, а также кадры не про него (просто море, просто небо) — грубая ошибка.

ЛЮДИ В КАДРЕ: только если фраза о людях, и они выглядят уместно для места и эпохи истории.
Для событий прошлого — никаких современных людей, яркой современной одежды и снаряжения,
смартфонов, машин, позирующих моделей, улыбающихся туристов и офисов.

СТРОГО НЕ БРАТЬ:
- кадры с водяными знаками, логотипами стоков, надписями «preview», полупрозрачными знаками, рамками;
- кадры с наложенным текстом, субтитрами, ценниками, интерфейсом, брендами и рекламой;
- быстрые и резкие кадры: таймлапсы, ускоренная съёмка, гонки, мелькание, вспышки, тряска,
  размытые от скорости дороги и огни, быстрые пролёты камеры. Нужны спокойные, плавные кадры;
- анахронизмы; мультяшные кадры и 3D-рендеры с явной компьютерной графикой;
- жестокие и кровавые кадры.
Если подходящих нет или их меньше, чем нужно, — предложи новый короткий поисковый запрос НА АНГЛИЙСКОМ
для видеостока: что конкретно и спокойно должно быть в кадре по смыслу фразы, без людей, если они не нужны.

Ответ — ТОЛЬКО строки такого вида, без пояснений:
12: 3 1 5
13: - | query: snowy mountain slope at dusk
14: 2 | query: vintage propeller airplane cockpit
"""


def parse(text):
    """{номер строки: [кадры]} и {номер строки: новый запрос}."""
    picks, queries = {}, {}
    for m in re.finditer(r"(?m)^\s*(\d+)\s*[:.)]\s*(.*)$", text or ""):
        no, rest = int(m.group(1)), m.group(2)
        main, _, q = rest.partition("|")
        qm = re.search(r"query\s*:\s*(.+)", q, re.I)
        nums = [int(x) for x in re.findall(r"\d+", main)]
        picks[no] = list(dict.fromkeys(nums))
        if qm:
            queries[no] = qm.group(1).strip()[:120]
    return picks, queries


PLAN_PROMPT = """Ты режиссёр документального видео на YouTube и подбираешь видеоряд. Тема: {topic}
Ниже фразы диктора по номерам. Кадр каждой фразы показывается РОВНО в момент, когда она звучит,
поэтому для КАЖДОЙ фразы определи, что именно зритель должен увидеть, чтобы картинка совпадала со словами:
конкретный предмет, место, действие (например, «археологи раскапывают землю», «деревянный столб с вырезанной
надписью», «старинный парусник в шторм»), с учётом эпохи и места истории. Не уходи в общие планы
(«лес», «природа», «человек листает книгу»), если фраза о чём-то конкретном.

ДОСКОНАЛЬНО ПО СЛОВАМ: разбери фразу на всё, что в ней можно ПОКАЗАТЬ, — каждый предмет, место, действие,
число, деталь. Если во фразе перечисление («в каютах вещи, в кладовой еда, в трюме бочки») — в «в кадре» перечисли
ВСЕ пункты по порядку через « / », и запросы дай по одному на каждый пункт в том же порядке: зритель должен увидеть
каюту, когда говорят о каюте, кладовую — когда о кладовой, бочки — когда о бочках. Никогда не обобщай перечисление
одним кадром. Каждая фраза — свои запросы, не повторяй запросы соседних фраз.

ИСТОЧНИК: «архив» — если лучше всего подойдут настоящие старые фотографии, гравюры, картины, карты, документы
той эпохи (люди того времени, их быт, корабли, города, события): они из музеев и архивов и создают атмосферу
подлинности; «видео» — если нужна живая съёмка (природа, море, огонь, предметы крупно).
Для историй прошлого чаще бери «архив» там, где речь о людях, быте, событиях.
«лист» — для 2–3 самых сильных фраз каждой главы, которые эффектно смотрятся напечатанными на старом листе:
запись в журнале, приговор, вывод суда, дата и место, жуткий факт, чьи-то слова (5–24 слова). На листе
печатаются РОВНО слова диктора в момент, когда он их говорит. В «в кадре» для листа напиши короткий заголовок
листа (например: «Судовой журнал, 1872»), запросы всё равно дай. Не больше трёх листов на главу и не подряд.

ЛЮДИ: если фраза о конкретном реальном человеке — по имени, по должности («капитан», «губернатор»)
или через «он/она», когда понятно, о ком речь, — укажи его: Имя Фамилия латиницей, как в английской
Википедии = как по-русски. Если во фразе есть год или возраст человека — допиши год в скобках после
латинского имени: «Arthur Conan Doyle (1884) = Артур Конан Дойл» — тогда найдём фото того времени, а не старости. Зритель должен видеть лицо человека, о котором рассказывают, как можно чаще.
Иначе поставь «-».

Для каждой фразы дай 3–4 поисковых запроса НА АНГЛИЙСКОМ (2–5 слов) — КОНКРЕТНО про то, что звучит:
если названо имя, корабль, место, год — оно должно быть в запросе («Mary Celeste brigantine painting»,
«Gibraltar harbour 1870s photo», «Arthur Conan Doyle 1880s»), без общих слов вроде «ocean», «ship», «old».
Первый запрос — самый точный (собственные имена + эпоха), последний — запасной. для «видео» — то, что реально снимают для видеостоков; для «архива» — как называют такие
изображения в музеях («1580s engraving ship», «colonial settlement painting», «1930s aviator photograph»).
Для событий прошлого — без современных людей и техники.

Ответ — ТОЛЬКО строки такого вида, без пояснений:
12 | в кадре: деревянный столб с вырезанными буквами | видео | люди: - | carved wooden post; letters carved in wood; old wooden pole closeup
14 | в кадре: каюта с вещами / кладовая с провизией / бочки в трюме | архив | люди: - | 19th century ship cabin; ship galley provisions; wooden barrels ship hold
15 | в кадре: Судовой журнал, 1872 | лист | люди: - | old ship logbook; handwritten log page
13 | в кадре: губернатор колонии | архив | люди: John White = Джон Уайт | elizabethan governor portrait; 1580s colonists engraving; roanoke colony painting

Фразы:
{lines}
"""


def plan(cfg, lines, topic, workdir, log=print):
    """Режиссёрский план: для каждой строки — что должно быть в кадре и 3 точных запроса для стоков.
    lines: [{"no", "text"}]. Возвращает {no: {"show": str, "queries": [..]}}."""
    out = {}
    for i in range(0, len(lines), 45):
        chunk = lines[i:i + 45]
        text = "\n".join(f"{ln['no']}. {ln['text']}" for ln in chunk)
        try:
            ans = run(cfg, PLAN_PROMPT.format(topic=topic or "документальное видео", lines=text), workdir, timeout=600)
        except ClaudeCodeError as e:
            log(f"  План кадров не получен ({e}) — ищу по запросам из сценария.")
            return out
        for line in (ans or "").splitlines():
            m = re.match(r"\s*(\d+)\s*\|(.+)$", line)
            if not m:
                continue
            parts = [x.strip() for x in m.group(2).split("|")]
            item = {"show": "", "queries": [], "source": "видео", "people": []}
            for x in parts[:-1]:
                low = x.lower()
                if low.startswith("в кадре"):
                    item["show"] = x.split(":", 1)[-1].strip()[:140]
                elif low in ("архив", "видео", "фото", "лист"):
                    item["source"] = low
                elif low.startswith("люди"):
                    for pp in x.split(":", 1)[-1].split(","):
                        en, _, ru = pp.partition("=")
                        yr = re.search(r"\((\d{4})s?\)", en)
                        en = re.sub(r"\s*\(.*?\)", "", en).strip()
                        if en and en != "-":
                            item["people"].append([en, (ru or en).strip()] + ([yr.group(1)] if yr else []))
            qs = parts[-1] if parts else ""
            item["queries"] = [q.strip(" .\"'") for q in re.split(r"[;,]", qs) if q.strip(" .\"'")][:4]
            if not item["show"] and len(parts) > 1:
                item["show"] = parts[0][:140]
            out[int(m.group(1))] = item
    return out
