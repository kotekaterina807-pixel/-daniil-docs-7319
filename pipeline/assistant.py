"""Помощник во вкладке программы: без ИИ, по компактному состоянию сборки и концу журнала.
Объясняет, почему сборка встала (диск, лимит Claude, сеть, зависание, ошибка), и принимает команды:
«продолжай», «останови», «статус», «очередь». Чат ограничен по длине: при пороге сохраняется резюме,
переписка очищается. Резюме лежит в output/_stats/assistant.json (обновление его не трогает)."""
import json
import re
import shutil
import time
from pathlib import Path

LIMIT = 40          # сообщений в чате до очистки


def _path():
    from .config import OUTPUT_DIR
    return Path(OUTPUT_DIR) / "_stats" / "assistant.json"


def load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {"messages": [], "summary": ""}


def save(st):
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _free_gb(path):
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return None


def diagnose(job, state):
    """job — компактное состояние (JOB.live_data()), state — флаги сервера. Возвращает понятный ответ."""
    from .config import OUTPUT_DIR, ROOT
    log = [str(x) for x in job.get("log") or []]
    tail = "\n".join(log[-40:]).lower()
    out = []
    running = job.get("running")
    if running:
        out.append(f"Идёт сборка «{(job.get('project') or '')[16:70]}»: этап {job.get('stage_no')}/{job.get('stages')} — "
                   f"{job.get('stage') or '…'}, всего {job.get('progress', 0)}%, идёт {job.get('minutes', 0)} мин, "
                   f"осталось ≈{job.get('eta_min', 0)} мин.")
    elif job.get("error"):
        out.append(f"Сборка остановилась с ошибкой: {job['error'][:300]}")
    else:
        out.append("Сейчас ничего не собирается.")
    free = _free_gb(OUTPUT_DIR if Path(OUTPUT_DIR).exists() else ROOT)
    if free is not None and free < 10:
        out.append(f"Диск: свободно {free:.0f} ГБ — для монтажа нужно 8–15 ГБ. Освободите место (корзина, старые проекты).")
    try:
        lim = json.loads((ROOT / "лимит_claude.json").read_text(encoding="utf-8"))
    except Exception:
        lim = {}
    if lim.get("exhausted") or "лимит подписки claude" in tail:
        when = ""
        if lim.get("reset_at"):
            when = time.strftime(" Сброс около %H:%M.", time.localtime(lim["reset_at"]))
        out.append("Лимит Claude исчерпан — сборка ждёт сброса и продолжит сама." + when)
    if re.search(r"недоступен|нет связи|connectionerror|timed out|не отвечает|proxy|прокси", tail):
        out.append("Сеть: часть сайтов не отвечала (см. журнал). Проверьте интернет/VPN; программа повторяет запросы сама.")
    if running and job.get("idle_min", 0) >= 15:
        out.append(f"Похоже на зависание: этап не двигается {job['idle_min']} мин. Нажмите «Останови», затем "
                   f"«Продолжай» — сделанное сохранится.")
    if "остановлено пользователем" in tail and not running:
        out.append("Сборку остановили кнопкой. «Продолжай» запустит её с места остановки.")
    if state.get("update_pending"):
        out.append(f"Ждёт установки обновления {state['update_pending']} — поставится, когда программа освободится.")
    if not running and not job.get("error") and len(out) == 1:
        out.append("Проблем не видно.")
    return "\n".join(out)


def command(text):
    """Что просит пользователь: stop / continue / status / queue / clear / help."""
    t = (text or "").strip().lower()
    if re.search(r"^(стоп|останов|stop|пауза)", t):
        return "stop"
    if re.search(r"^(продолж|дальше|continue|запусти|возобнов)", t):
        return "continue"
    if re.search(r"очеред|queue", t):
        return "queue"
    if re.search(r"^(очисти|clear)", t):
        return "clear"
    if re.search(r"помощ|help|что умеешь|команд", t):
        return "help"
    return "status"


HELP = ("Я подсказываю, что происходит со сборкой, по журналу программы. Команды: «почему встало» (или «статус»), "
        "«продолжай» — продолжить последнюю незаконченную сборку, «останови», «очередь» — задания из чата с Claude, "
        "«очисти» — очистить переписку.")


def add(st, role, text):
    """Добавить сообщение. При пороге: короткое резюме сохраняется, переписка очищается."""
    st.setdefault("messages", []).append({"role": role, "text": text, "t": time.strftime("%H:%M")})
    cleared = False
    if len(st["messages"]) > LIMIT:
        last = [m["text"] for m in st["messages"] if m["role"] == "bot"][-1:]
        st["summary"] = ("Резюме прошлой переписки (" + time.strftime("%d.%m %H:%M") + "): " +
                         (last[0][:400] if last else "без проблем"))
        st["messages"] = [{"role": "bot", "t": time.strftime("%H:%M"),
                           "text": "Переписка стала длинной — очистил её. " + st["summary"]}]
        cleared = True
    save(st)
    return cleared
