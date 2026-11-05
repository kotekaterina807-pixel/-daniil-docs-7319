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


def run(cfg, prompt, workdir, timeout=900):
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
Порядок — от лучшего к худшему. «нужно до N» — сколько разных кадров нужно этой строке; минимум 2,
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

ИСТОЧНИК: «архив» — если лучше всего подойдут настоящие старые фотографии, гравюры, картины, карты, документы
той эпохи (люди того времени, их быт, корабли, города, события): они из музеев и архивов и создают атмосферу
подлинности; «видео» — если нужна живая съёмка (природа, море, огонь, предметы крупно).
Для историй прошлого чаще бери «архив» там, где речь о людях, быте, событиях.

ЛЮДИ: если во фразе назван реальный человек (по имени или должности, например «губернатор Джон Уайт»),
укажи его: Имя Фамилия латиницей, как в английской Википедии = как по-русски. Иначе поставь «-».

Для каждой фразы дай 3 коротких поисковых запроса НА АНГЛИЙСКОМ (2–4 слова), от самого точного к более
широкому: для «видео» — то, что реально снимают для видеостоков; для «архива» — как называют такие
изображения в музеях («1580s engraving ship», «colonial settlement painting», «1930s aviator photograph»).
Для событий прошлого — без современных людей и техники.

Ответ — ТОЛЬКО строки такого вида, без пояснений:
12 | в кадре: деревянный столб с вырезанными буквами | видео | люди: - | carved wooden post; letters carved in wood; old wooden pole closeup
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
                elif low in ("архив", "видео", "фото"):
                    item["source"] = low
                elif low.startswith("люди"):
                    for pp in x.split(":", 1)[-1].split(","):
                        en, _, ru = pp.partition("=")
                        if en.strip() and en.strip() != "-":
                            item["people"].append([en.strip(), (ru or en).strip()])
            qs = parts[-1] if parts else ""
            item["queries"] = [q.strip(" .\"'") for q in re.split(r"[;,]", qs) if q.strip(" .\"'")][:3]
            if not item["show"] and len(parts) > 1:
                item["show"] = parts[0][:140]
            out[int(m.group(1))] = item
    return out
