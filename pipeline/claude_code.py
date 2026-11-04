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
        proxy = proxy.replace("socks5h://", "socks5://")
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
    elif proxy.startswith("socks"):
        warn = (" Прокси указан как SOCKS — если Claude Code напишет «Unable to connect», впишите HTTP-вариант "
                "(http://…; у VPN-приложений это обычно соседний порт).")
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


PROMPT = """Ты подбираешь видеокадры для документального видео на YouTube.
Открой файл {sheet} инструментом Read (это картинка). На нём строки сценария; у каждой строки —
номер, текст диктора, поисковый запрос и до 8 пронумерованных превью (видео или фото).
Тема видео: {topic}

Для КАЖДОЙ строки выбери кадры, которые показывают именно то, о чём говорит диктор в этой фразе
(место, предмет, действие, эпоху, настроение). Порядок — от лучшего к худшему. «нужно до N» — максимум кадров.

СТРОГО НЕ БРАТЬ:
- кадры с водяными знаками, логотипами стоков, надписями «preview», полупрозрачными знаками, рамками;
- кадры с наложенным текстом, субтитрами, ценниками, интерфейсом, брендами и рекламой;
- анахронизмы (современные машины, телефоны, дроны, люди в современной одежде, если речь о прошлом);
- кадры не по смыслу, «просто красивые», мультяшные, 3D-рендеры с явной компьютерной графикой;
- жестокие и кровавые кадры.
Если подходящих нет — не бери ничего, а предложи новый короткий поисковый запрос НА АНГЛИЙСКОМ
для фото/видеостока (что конкретно должно быть в кадре).

Ответ — ТОЛЬКО строки такого вида, без пояснений:
12: 3 1 5
13: - | query: abandoned lighthouse stormy sea
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
        if qm and not nums:
            queries[no] = qm.group(1).strip()[:120]
    return picks, queries
