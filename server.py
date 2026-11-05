"""Сервер интерфейса: окно программы — это страница в отдельном окне Edge (режим приложения),
а вся работа (озвучка, подбор кадров, монтаж) идёт здесь, в Python.
Слушает только 127.0.0.1, запросы с изменениями требуют секретный токен."""
import io
import json
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline import config, runner, tts  # noqa: E402

UI_DIR = ROOT / "ui"
PORT_FILE = ROOT / ".ui_port"
TOKEN_FILE = ROOT / ".ui_token"
PORTS = list(range(8765, 8785))
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Файлы и папки пользователя — обновление их не трогает
KEEP = {"config.json", "output", "footage", "music", "voice_samples", ".venv", ".ui_profile", ".ui_webview",
        ".ui_port", ".ui_token", "error_log.txt"}

SAMPLE_TEXT = ("В декабре тысяча девятисотого года с маяка на крошечном острове исчезли три смотрителя. "
               "Их так и не нашли. Ни тел, ни лодки, ни записки.")


def _open_path(p):
    p = str(p)
    if os.name == "nt":
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _h(text):
    """Имя файла примера зависит от фразы и скорости речи — при смене скорости пример озвучится заново."""
    import hashlib
    c = config.load_config()
    speed = f"{c.get('eleven_speed')}|{c.get('eleven_stability')}"
    return hashlib.md5(f"{text}|{speed}".encode("utf-8")).hexdigest()[:8]


def _fmt(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ---------------- фоновая работа ----------------
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.log = []
        self.reset("")

    def reset(self, kind):
        self.kind = kind
        self.running = False
        self.frac = None
        self.stage = ""
        self.t0 = None
        self.t_end = None
        self.eta = None
        self.frac_start = 0.0
        self.frac_t0 = 0.0
        self.error = None
        self.result = None
        self.stop = False
        self.project = None          # папка проекта, который сейчас собирается
        self.hist = []               # (время, доля) — для оценки оставшегося времени
        self.eta_t = None
        self.done_id = 0 if not hasattr(self, "done_id") else self.done_id

    def add_log(self, msg):
        with self.lock:
            for line in str(msg).splitlines() or [""]:
                self.log.append(line)
            if len(self.log) > 5000:
                del self.log[:1000]
                self.log_offset = getattr(self, "log_offset", 0) + 1000

    def progress(self, frac, stage):
        frac = max(0.0, min(1.0, frac))
        if self.frac is None:
            self.frac_start, self.frac_t0 = frac, time.time() - self.t0
        self.frac, self.stage = frac, stage
        now = time.time()
        self.hist.append((now, frac))
        if len(self.hist) > 2000:
            del self.hist[:500]

    def status(self):
        if not self.t0:
            return {"running": False, "kind": self.kind, "error": self.error, "done_id": self.done_id,
                    "result": self.result}
        now = self.t_end or time.time()
        elapsed = now - self.t0
        eta_text, eta_sec = "", None
        if self.running and self.frac is not None and self.frac > 0.02 and elapsed > 12 and self.hist:
            # скорость по последним 3 минутам (этапы идут с разной скоростью) + по всей сборке
            t_now, f_now = self.hist[-1]
            old = next(((t, f) for t, f in self.hist if t >= t_now - 180), self.hist[0])
            rate_win = (f_now - old[1]) / max(1.0, t_now - old[0]) if t_now - old[0] > 20 else 0
            rate_all = (f_now - self.frac_start) / max(1.0, elapsed - self.frac_t0)
            rate = 0.6 * rate_win + 0.4 * rate_all if rate_win > 0 else rate_all
            if rate > 0:
                raw = (1 - f_now) / rate - (now - t_now)
                raw = max(0.0, raw)
                if self.eta is None:
                    self.eta, self.eta_t = raw, now
                else:
                    dt = max(0.0, now - (self.eta_t or now))
                    self.eta = max(0.0, self.eta - dt)                # время идёт — оценка уменьшается
                    k = min(1.0, dt / 25.0)                           # плавно подтягиваемся к новой оценке
                    self.eta += (raw - self.eta) * k
                    self.eta_t = now
                eta_sec = round(self.eta)
                eta_text = _fmt(self.eta)
        return {"running": self.running, "kind": self.kind, "frac": self.frac, "stage": self.stage,
                "elapsed": _fmt(elapsed), "elapsed_sec": round(elapsed), "eta": eta_text, "eta_sec": eta_sec,
                "error": self.error, "done_id": self.done_id, "result": self.result, "project": self.project}


JOB = Job()
STATE = {"last_seen": time.time(), "selfcheck": None, "restart": False, "browser": None, "bye": 0.0}


def run_job(kind, fn):
    with JOB.lock:
        if JOB.running:
            return False
        JOB.reset(kind)
        JOB.running = True
        JOB.t0 = time.time()

    def wrapper():
        try:
            JOB.result = fn()
        except Exception as e:
            JOB.error = str(e)
            JOB.add_log(f"ОШИБКА: {e}")
            try:
                with open(ROOT / "error_log.txt", "a", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
            except OSError:
                pass
        finally:
            JOB.running = False
            JOB.t_end = time.time()
            JOB.done_id += 1
    threading.Thread(target=wrapper, daemon=True).start()
    return True


# ---------------- проекты ----------------
def _project_info(d: Path):
    data = runner._load_json(d / "script.json", {}) if (d / "script.json").exists() else {}
    videos = [p for p in d.glob("*.mp4") if p.name != "video_only.mp4" and not p.name.endswith(".part.mp4")
              and not p.name.endswith("_telegram.mp4")]
    video = max(videos, key=lambda p: p.stat().st_mtime) if videos else None
    dur = 0.0
    nar = d / "narration.wav"
    if nar.exists():
        try:
            dur = tts.wav_duration(nar)
        except Exception:
            dur = 0.0
    thumb = d / "thumbnail_1.jpg"
    return {
        "id": d.name,
        "title": data.get("title") or d.name,
        "created": time.strftime("%d.%m.%Y %H:%M", time.localtime(d.stat().st_mtime)),
        "mtime": d.stat().st_mtime,
        "ready": bool(video),
        "video": video.name if video else "",
        "size_mb": round(video.stat().st_size / 1024 ** 2) if video else 0,
        "duration": _fmt(dur) if dur else "",
        "thumb": thumb.name if thumb.exists() else "",
        "language": data.get("language", "ru"),
        "has_description": (d / "description.txt").exists(),
        "need_choices": (d / "review.json").exists() and not (d / "выбор_кадров.json").exists() and not video,
        "sheets": sorted(p.name for p in d.glob("кадры_лист_*.jpg")),
    }


def list_projects():
    if not config.OUTPUT_DIR.exists():
        return []
    out = []
    for d in config.OUTPUT_DIR.iterdir():
        if d.is_dir() and not d.name.startswith("_") and (d / "script.json").exists():
            try:
                out.append(_project_info(d))
            except OSError:
                continue
    for it in out:
        it["building"] = bool(JOB.running and JOB.kind == "build" and JOB.project == it["id"])
        if it["building"]:
            it["ready"] = False
            it["progress"] = round((JOB.frac or 0) * 100)
    out.sort(key=lambda x: -x["mtime"])
    return out


def _project_dir(pid):
    d = (config.OUTPUT_DIR / str(pid)).resolve()
    if d.parent != config.OUTPUT_DIR.resolve() or not d.is_dir():
        raise ValueError("Проект не найден")
    return d


# ---------------- обновление ----------------
def _version_of(text):
    import re
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _vkey(v):
    """«4.10 (05.11.2026)» → (4, 10) — для сравнения версий числами."""
    import re
    m = re.match(r"\s*([\d.]+)", v or "")
    return tuple(int(x) for x in m.group(1).split(".") if x) if m else ()


def is_newer(latest, current):
    return bool(_vkey(latest)) and _vkey(latest) > _vkey(current)


def apply_update(zip_bytes, log):
    """Ставит новую версию из архива: заменяет файлы программы, ключи/проекты/футажи/музыку не трогает."""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    marker = next((n for n in names if n.endswith("pipeline/config.py")), None)
    if not marker:
        raise ValueError("Это не архив программы (нет pipeline/config.py).")
    prefix = marker[: -len("pipeline/config.py")]
    new_version = _version_of(zf.read(marker).decode("utf-8", "replace"))
    old_req = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    count = 0
    for n in names:
        if not n.startswith(prefix):
            continue
        rel = n[len(prefix):]
        if not rel or rel.split("/")[0] in KEEP or "__pycache__" in rel:
            continue
        target = (ROOT / rel).resolve()
        if ROOT.resolve() not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        tmp.write_bytes(zf.read(n))
        os.replace(tmp, target)
        count += 1
    removed = ROOT / "_removed.txt"
    if removed.exists():                            # файлы, которых в новой версии больше нет
        for rel in removed.read_text(encoding="utf-8").splitlines():
            rel = rel.strip()
            if rel and not rel.startswith("#") and rel.split("/")[0] not in KEEP:
                t = (ROOT / rel).resolve()
                if ROOT.resolve() in t.parents and t.is_file():
                    t.unlink(missing_ok=True)
    log(f"Обновлено файлов: {count}. Новая версия: {new_version or 'неизвестна'}.")
    new_req = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    if new_req != old_req:
        log("Ставлю новые компоненты (pip)...")
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy")}
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        py = str(py) if py.exists() else sys.executable.replace("pythonw.exe", "python.exe")
        p = subprocess.run([py, "-m", "pip", "install", "-r",
                            str(ROOT / "requirements.txt")], capture_output=True, text=True,
                           creationflags=_NO_WINDOW, env=env)
        log("Компоненты установлены." if p.returncode == 0 else f"pip: {p.stderr[-400:]}")
    make_shortcut(quiet=True)
    return new_version


def schedule_restart():
    """Перезапуск сервера на том же порту — окно само переподключится."""
    STATE["restart"] = True

    def go():
        time.sleep(0.8)
        exe = sys.executable
        args = [exe, str(ROOT / "app.py")] + (["--reopen"] if STATE.get("native") else ["--no-window"])
        subprocess.Popen(args, cwd=str(ROOT), creationflags=_NO_WINDOW)
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def make_shortcut(quiet=False):
    """Ярлык «DocuStudio» на рабочем столе с иконкой программы (Windows)."""
    if os.name != "nt":
        return False
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    target = pyw if pyw.exists() else Path(sys.executable)
    try:
        import app                                   # собственный процесс DocuStudio.exe — для VPN-маршрутизации
        target = app.branded_exe() or target
    except Exception:
        pass
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'DocuStudio.lnk'));"
        f"$s.TargetPath='{target}';$s.Arguments='\"{ROOT / 'app.py'}\"';"
        f"$s.WorkingDirectory='{ROOT}';$s.IconLocation='{ROOT / 'app.ico'},0';"
        "$s.Description='DocuStudio — документальные видео';$s.Save()"
    )
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    return p.returncode == 0


# ---------------- HTTP ----------------
PUBLIC_KEYS = ["pixabay_key", "pexels_key", "proxy_url", "lumean_key", "lumean_ai_model",
               "tts_provider", "edge_voice", "anthropic_key", "openai_key", "elevenlabs_key", "elevenlabs_voice_id",
               "music_volume", "shot_seconds", "show_labels", "theme", "sound_done", "open_folder_after",
               "default_niche", "update_repo", "language", "visual_check", "eleven_speed", "eleven_stability", "eleven_style",
               "tg_token", "tg_chat_id", "tg_send", "old_look"]


class Handler(BaseHTTPRequestHandler):
    server_version = "DocuStudio"

    def log_message(self, *a):
        pass

    # --- ответы ---
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._send(404, {"error": "нет файла"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        if rng and rng.startswith("bytes="):                    # перемотка аудио/видео
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                start = int(a) if a else max(0, size - int(b or 0))
                end = min(size - 1, int(b)) if (a and b) else size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, start + 8 * 1024 * 1024 - 1)          # видео отдаём кусками по 8 МБ
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _authorized(self, q):
        return self.headers.get("X-Token") == TOKEN or q.get("t", [""])[0] == TOKEN

    # --- GET ---
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(UI_DIR / "index.html")
            if p.startswith("/ui/"):
                f = (UI_DIR / p[4:]).resolve()
                if UI_DIR.resolve() not in f.parents:
                    return self._send(404, {})
                return self._file(f)
            if p == "/favicon.ico":
                return self._file(ROOT / "app.ico")
            if not self._authorized(q):
                return self._send(403, {"error": "нет доступа"})
            if p == "/api/ping":                     # для второго запуска: открыто ли уже окно
                seen = STATE.get("last_seen") or 0
                open_ = seen > (STATE.get("bye") or 0) and time.time() - seen < 90
                return self._send(200, {"ok": True, "window_open": open_})
            STATE["last_seen"] = time.time()
            if p == "/api/state":
                since = int(q.get("since", ["0"])[0] or 0)
                off = getattr(JOB, "log_offset", 0)
                with JOB.lock:
                    lines = JOB.log[max(0, since - off):]
                    total = off + len(JOB.log)
                return self._send(200, {"job": JOB.status(), "log": lines, "log_next": total,
                                        "selfcheck": STATE["selfcheck"]})
            if p == "/api/init":
                cfg = config.load_config()
                niches = [{"id": str(v.name), "name": k} for k, v in config.list_niches().items()]
                return self._send(200, {
                    "version": config.VERSION,
                    "cfg": {k: cfg.get(k, "") for k in PUBLIC_KEYS},
                    "niches": niches,
                    "platform": os.name,
                })
            if p == "/api/projects":
                return self._send(200, list_projects())
            if p == "/api/project_file":
                d = _project_dir(q.get("id", [""])[0])
                name = Path(q.get("name", [""])[0]).name
                return self._file(d / name)
            if p == "/api/voice_audio":
                provider = "eleven"
                value = q.get("value", [""])[0].replace(":", "_")
                h = Path(q.get("h", [""])[0]).name
                return self._file(ROOT / "voice_samples" / provider / f"{Path(value).name}_{h}.mp3")
            if p == "/api/music":
                files = []
                if config.MUSIC_DIR.exists():
                    for f in sorted(config.MUSIC_DIR.rglob("*")):
                        if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
                            files.append({"name": f.name, "rel": str(f.relative_to(config.MUSIC_DIR)).replace("\\", "/"),
                                          "folder": f.parent.name if f.parent != config.MUSIC_DIR else "",
                                          "size_mb": round(f.stat().st_size / 1024 ** 2, 1)})
                return self._send(200, files)
            if p == "/api/music_audio":
                rel = q.get("rel", [""])[0]
                f = (config.MUSIC_DIR / rel).resolve()
                if config.MUSIC_DIR.resolve() not in f.parents:
                    return self._send(404, {})
                return self._file(f)
            return self._send(404, {"error": "нет такого адреса"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    # --- POST ---
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/bye" and q.get("t", [""])[0] == TOKEN:     # окно закрывается
            STATE["bye"] = time.time()
            return self._send(200, {"ok": True})
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "нет доступа"})
        STATE["last_seen"] = time.time()
        p = u.path
        try:
            raw = self._body()
            if p == "/api/update_zip":
                return self._update_zip(raw)
            data = json.loads(raw.decode("utf-8") or "{}") if raw else {}
            return self._route(p, data)
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def _route(self, p, data):
        if p == "/api/heartbeat":
            return self._send(200, {"ok": True})
        if p == "/api/save":
            return self._send(200, save_settings(data.get("values") or {}))
        if p == "/api/build_text":
            return self._build_text(data)
        if p == "/api/rebuild":
            d = _project_dir(data.get("id"))

            def job():
                sj = runner._load_json(d / "script.json", {})
                npath = sj.get("niche_file")
                if not (npath and Path(npath).exists()):
                    npath = _niche_path(data.get("niche"))
                return _build(d, Path(npath))
            ok = run_job("build", job)
            return self._send(200 if ok else 409, {"ok": ok, "error": None if ok else "Уже идёт работа"})
        if p == "/api/choices":
            from pipeline import review
            d = _project_dir(data.get("id"))
            lines = review.load_review(d / "review.json")
            if not lines:
                return self._send(400, {"error": "В проекте нет листов для выбора кадров"})
            ch = review.parse_choices(data.get("text", ""), lines)
            if not ch:
                return self._send(400, {"error": "Не нашёл в тексте номеров. Вставьте ответ Claude целиком: строки вида «12: 3 1»."})
            missing = [ln["no"] for ln in lines if ln["no"] not in ch]
            (d / "выбор_кадров.json").write_text(json.dumps({str(k): v for k, v in ch.items()}), encoding="utf-8")
            return self._send(200, {"ok": True, "lines": len(ch), "missing": missing})
        if p == "/api/stop":
            JOB.stop = True
            JOB.add_log("Остановка после текущего шага... Сделанное сохранится — повторная сборка продолжит.")
            return self._send(200, {"ok": True})
        if p == "/api/selfcheck":
            def job():
                from pipeline import selfcheck
                JOB.add_log("Проверка подключений...")
                res = selfcheck.run_all(config.load_config(), JOB.add_log)
                STATE["selfcheck"] = [{"name": n, "ok": ok, "msg": m, "critical": c} for n, ok, m, c in res]
                for r in STATE["selfcheck"]:
                    JOB.add_log(("  ✔ " if r["ok"] else "  ✘ ") + r["name"] + (f" — {r['msg']}" if r["msg"] else ""))
                return {"selfcheck": STATE["selfcheck"]}
            ok = run_job("selfcheck", job)
            return self._send(200 if ok else 409, {"ok": ok, "error": None if ok else "Уже идёт работа"})
        if p == "/api/eleven":
            cfg = config.load_config()
            prov = tts.provider_of(cfg)
            has = bool(str(cfg.get("lumean_key" if prov == "lumean" else "elevenlabs_key", "")).strip())
            vs = cfg.get("eleven_voices") or []
            cur = cfg.get("elevenlabs_voice_id", "")
            vs = [v for v in vs if v.get("id") == cur] + [v for v in vs if v.get("id") != cur]   # выбранный — первым
            return self._send(200, {"voices": vs, "current": cur,
                                    "has_key": has, "provider": prov})
        if p == "/api/lumean_library":
            from pipeline import lumean
            try:
                return self._send(200, {"ok": True, **lumean.library(config.load_config(), str(data.get("search") or ""),
                                                                   str(data.get("gender") or ""), str(data.get("age") or ""),
                                                                   str(data.get("use_cases") or ""), str(data.get("language") or "ru"),
                                                                   int(data.get("page") or 0))})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if p == "/api/eleven_add":
            import re as _re
            cfg = config.load_config()
            row = data.get("voice_obj")
            if not row:
                raw = str(data.get("voice") or "").strip()
                m = _re.search(r"(?:voiceId=|voice_id=|/voices?/)([A-Za-z0-9]{16,32})", raw) or _re.fullmatch(r"\s*([A-Za-z0-9]{16,32})\s*", raw)
                if not m:
                    return self._send(400, {"error": "Не похоже на ID голоса (Voice ID — 20 латинских букв и цифр)."})
                vid = m.group(1)
                if tts.provider_of(cfg) == "lumean":
                    row = {"id": vid, "name": str(data.get("name") or "").strip() or f"Голос {vid[:6]}", "desc": "добавлен по ID", "preview": ""}
                else:
                    try:
                        row = tts.el_voice_info(cfg, vid)
                    except Exception as e:
                        if "не найден" in str(e):
                            return self._send(400, {"error": "Голос не найден в вашем аккаунте ElevenLabs. Нажмите у голоса «Add to my voices» и вставьте ID снова."})
                        return self._send(400, {"error": str(e)})
            row = {k: row.get(k, "") for k in ("id", "name", "desc", "preview")}
            if not row["id"]:
                return self._send(400, {"error": "Нет ID голоса"})
            voices = [v for v in (cfg.get("eleven_voices") or []) if v.get("id") != row["id"]] + [row]
            cfg["eleven_voices"] = voices
            if not cfg.get("elevenlabs_voice_id"):
                cfg["elevenlabs_voice_id"] = row["id"]
            config.save_config(cfg)
            return self._send(200, {"ok": True, "voice": row})
        if p == "/api/eleven_import":
            cfg = config.load_config()
            try:
                rows = tts.el_account_voices(cfg)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            have = {v.get("id") for v in (cfg.get("eleven_voices") or [])}
            cfg["eleven_voices"] = (cfg.get("eleven_voices") or []) + [r for r in rows if r["id"] not in have]
            config.save_config(cfg)
            return self._send(200, {"ok": True, "added": len([r for r in rows if r["id"] not in have])})
        if p == "/api/eleven_order":
            cfg = config.load_config()
            ids = [str(x) for x in (data.get("ids") or [])]
            vs = cfg.get("eleven_voices") or []
            by = {v.get("id"): v for v in vs}
            ordered = [by[i] for i in ids if i in by] + [v for v in vs if v.get("id") not in ids]
            cfg["eleven_voices"] = ordered
            if ordered:
                cfg["elevenlabs_voice_id"] = ordered[0].get("id", "")
            config.save_config(cfg)
            return self._send(200, {"ok": True, "current": cfg.get("elevenlabs_voice_id", ""),
                                    "name": ordered[0].get("name", "") if ordered else ""})
        if p == "/api/eleven_remove":
            cfg = config.load_config()
            vid = str(data.get("id") or "")
            cfg["eleven_voices"] = [v for v in (cfg.get("eleven_voices") or []) if v.get("id") != vid]
            if cfg.get("elevenlabs_voice_id") == vid:          # убрали выбранный — выбирается следующий сверху
                cfg["elevenlabs_voice_id"] = (cfg["eleven_voices"][0].get("id", "") if cfg["eleven_voices"] else "")
            config.save_config(cfg)
            return self._send(200, {"ok": True})
        if p == "/api/eleven_usage":
            try:
                cfg = config.load_config()
                if tts.provider_of(cfg) == "lumean":
                    from pipeline import lumean
                    acc = lumean.account(cfg)
                    return self._send(200, {"ok": True, "tier": acc["tier"], "used": acc["used"], "limit": acc["limit"], "unit": "токенов"})
                return self._send(200, {"ok": True, **tts.el_subscription(cfg)})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if p == "/api/claude_login":
            from pipeline import claude_code
            return self._send(200, claude_code.open_login(config.load_config()))
        if p == "/api/tg_connect":
            from pipeline import telegram
            cfg = config.load_config()
            try:
                bot = telegram.bot_name(cfg.get("tg_token", ""))
                chat, who = telegram.find_chat(cfg["tg_token"])
            except telegram.TgError as e:
                return self._send(200, {"ok": False, "error": str(e)})
            if not chat:
                return self._send(200, {"ok": False, "need_start": True, "bot": bot,
                                        "error": f"Откройте бота @{bot} в Telegram и нажмите «Start», потом снова «Подключить»."})
            cfg["tg_chat_id"] = chat
            config.save_config(cfg)
            try:
                telegram.send_text(cfg, "✅ DocuStudio подключена. Сюда будут приходить готовые видео.")
            except telegram.TgError as e:
                return self._send(200, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, "bot": bot, "who": who, "chat": chat})
        if p == "/api/tg_send":
            name = str(data.get("project", ""))
            proj = config.OUTPUT_DIR / name
            final = _final_video(proj) if name and proj.is_dir() and proj.resolve().parent == config.OUTPUT_DIR.resolve() else None
            if not final:
                return self._send(404, {"error": "В проекте нет готового видео."})
            threading.Thread(target=_tg_send_bg, args=(final, name), daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/open_url":
            url = str(data.get("url") or "")
            if not url.startswith(("https://elevenlabs.io/", "https://console.anthropic.com/", "https://platform.claude.com/",
                                   "https://lumean.app/", "https://t.me/BotFather")):
                return self._send(400, {"error": "Этот адрес открыть нельзя"})
            import webbrowser
            webbrowser.open(url)
            return self._send(200, {"ok": True})
        if p == "/api/voice_sample":
            provider = "eleven"
            value = str(data.get("value") or "")
            text = (data.get("text") or SAMPLE_TEXT).strip()[:400]
            h = _h(text)
            out = ROOT / "voice_samples" / provider / f"{value.replace(':', '_')}_{h}.mp3"
            if not out.exists():
                try:
                    tts.make_sample(config.load_config(), provider, value, text, out)
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, "h": h})
        if p == "/api/open":
            return self._open(data)
        if p == "/api/delete_project":
            d = _project_dir(data.get("id"))
            if JOB.running:
                return self._send(409, {"error": "Дождитесь окончания сборки"})
            shutil.rmtree(d, ignore_errors=True)
            return self._send(200, {"ok": True})
        if p == "/api/text_file":
            d = _project_dir(data.get("id"))
            name = Path(data.get("name", "")).name
            f = d / name
            return self._send(200, {"text": f.read_text(encoding="utf-8-sig") if f.exists() else ""})
        if p == "/api/check_update":
            return self._check_update()
        if p == "/api/update_online":
            return self._update_online()
        if p == "/api/shortcut":
            ok = make_shortcut()
            return self._send(200, {"ok": ok})
        if p == "/api/import_music":
            return self._send(400, {"error": "используйте папку music"})
        return self._send(404, {"error": "нет такого действия"})

    def _build_text(self, data):
        text = data.get("text") or ""
        if not text.strip():
            return self._send(400, {"error": "Файл сценария пустой"})
        inbox = config.OUTPUT_DIR / "_scripts"
        inbox.mkdir(parents=True, exist_ok=True)
        name = Path(data.get("name") or "script.txt").name
        if not name.lower().endswith(".txt"):
            name += ".txt"
        path = inbox / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
        path.write_text(text.replace("\r\n", "\n"), encoding="utf-8-sig")
        npath = _niche_path(data.get("niche"))

        def job():
            project = runner.project_from_text(path, npath, JOB.add_log)
            return _build(project, npath)
        ok = run_job("build", job)
        return self._send(200 if ok else 409, {"ok": ok, "error": None if ok else "Уже идёт работа"})

    def _open(self, data):
        what = data.get("what")
        from pipeline import library
        places = {"output": config.OUTPUT_DIR, "footage": library.FOOTAGE_DIR, "music": config.MUSIC_DIR,
                  "voices": ROOT / "voice_samples", "app": ROOT}
        if what in places:
            places[what].mkdir(parents=True, exist_ok=True)
            _open_path(places[what])
            return self._send(200, {"ok": True})
        d = _project_dir(data.get("id"))
        if what == "project":
            _open_path(d)
        elif what == "video":
            info = _project_info(d)
            if not info["video"]:
                return self._send(404, {"error": "Видео ещё не собрано"})
            _open_path(d / info["video"])
        elif what == "script":
            _open_path(d / "script.txt")
        else:
            return self._send(400, {"error": "неизвестно что открыть"})
        return self._send(200, {"ok": True})

    def _update_zip(self, raw):
        if JOB.running:
            return self._send(409, {"error": "Дождитесь окончания сборки, потом обновляйте."})
        logs = []
        ver = apply_update(raw, lambda m: (logs.append(m), JOB.add_log(m)))
        self._send(200, {"ok": True, "version": ver, "log": logs})
        schedule_restart()

    def _check_update(self):
        repo = (config.load_config().get("update_repo") or "").strip().strip("/")
        if not repo:
            return self._send(200, {"ok": False, "error": "Не указан адрес обновлений"})
        import requests
        latest, code = "", 0
        try:                                         # API GitHub отдаёт свежий файл (raw-адрес кэшируется минутами)
            r = requests.get(f"https://api.github.com/repos/{repo}/contents/pipeline/config.py",
                             params={"ref": "main"}, headers={"Accept": "application/vnd.github.raw"}, timeout=20)
            code = r.status_code
            if r.status_code == 200:
                latest = _version_of(r.text)
        except requests.RequestException:
            pass
        if not latest:
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/main/pipeline/config.py",
                             params={"t": int(time.time())}, timeout=20)
            code = r.status_code
            latest = _version_of(r.text) if r.status_code == 200 else ""
        if not latest:
            return self._send(200, {"ok": False, "error": f"Сервер обновлений ответил {code}"})
        return self._send(200, {"ok": True, "latest": latest, "current": config.VERSION,
                                "newer": is_newer(latest, config.VERSION)})

    def _update_online(self):
        if JOB.running:
            return self._send(409, {"error": "Дождитесь окончания сборки, потом обновляйте."})
        repo = (config.load_config().get("update_repo") or "").strip().strip("/")
        if not repo:
            return self._send(400, {"error": "Не указан адрес обновлений"})
        import requests
        r = requests.get(f"https://codeload.github.com/{repo}/zip/refs/heads/main", timeout=120)
        if r.status_code != 200:
            return self._send(502, {"error": f"Не удалось скачать обновление ({r.status_code})"})
        try:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            cfgname = next(n for n in zf.namelist() if n.endswith("pipeline/config.py"))
            zver = _version_of(zf.read(cfgname).decode("utf-8", "replace"))
        except Exception:
            zver = ""
        if zver and is_newer(config.VERSION, zver):      # никогда не откатываемся на старую версию
            return self._send(409, {"error": f"На сервере пока версия {zver}, а у вас уже {config.VERSION} — "
                                             "обновлять нечего."})
        return self._update_zip(r.content)


def _tokens_left():
    """Остаток токенов Lumean (None, если озвучка не через Lumean или сайт не ответил)."""
    try:
        cfg = config.load_config()
        if tts.provider_of(cfg) != "lumean":
            return None
        from pipeline import lumean
        acc = lumean.account(cfg)
        return int(acc["left"])
    except Exception:
        return None


def _final_video(project):
    vids = [p for p in Path(project).glob("*.mp4") if p.name != "video_only.mp4"
            and not p.name.endswith(".part.mp4") and not p.name.endswith("_telegram.mp4")]
    return max(vids, key=lambda p: p.stat().st_mtime) if vids else None


def _tg_send_bg(path, title):
    from pipeline import telegram
    try:
        t = runner._load_json(Path(path).parent / "script.json", {}).get("title") or title
        msg = telegram.send_video(config.load_config(), path, f"🎬 {t}", JOB.add_log)
        JOB.add_log("Telegram: " + msg)
    except Exception as e:
        JOB.add_log(f"Telegram: не отправилось — {e}")


def _build(project, npath):
    """Сборка проекта. Если нужен выбор кадров в чате — возвращает это как результат, а не ошибку."""
    from pipeline import review
    JOB.project = project.name
    before = _tokens_left()
    try:
        runner.step_build(config.load_config(), config.load_niche(npath), project, JOB.add_log,
                          lambda: JOB.stop, JOB.progress)
    except review.NeedChoices as e:
        JOB.add_log("Кадры-кандидаты собраны. Отправьте листы Claude в чат и вставьте его ответ в программу.")
        return {"project": project.name, "need_choices": True, "sheets": e.sheets, "lines": e.lines}
    final = _final_video(project)
    cfg = config.load_config()
    if final and cfg.get("tg_send", True) and cfg.get("tg_token") and cfg.get("tg_chat_id"):
        threading.Thread(target=_tg_send_bg, args=(final, project.name), daemon=True).start()
    after = _tokens_left()
    if before is not None and after is not None:
        JOB.add_log(f"Озвучка: потрачено токенов Lumean {max(0, before - after):,}, осталось {after:,}".replace(",", " "))
    if config.load_config().get("open_folder_after"):
        try:
            _open_path(project)
        except Exception:
            pass
    return {"project": project.name}


def _niche_path(niche_id):
    niches = config.list_niches()
    for name, path in niches.items():
        if path.name == niche_id or name == niche_id:
            return path
    cfg = config.load_config()
    for name, path in niches.items():
        if path.name == cfg.get("default_niche"):
            return path
    return next(iter(niches.values()))


def apply_proxy(cfg=None):
    """Прокси только для DocuStudio: все запросы программы (Pixabay, ElevenLabs, Claude, GitHub) идут через него,
    остальные программы на компьютере его не используют."""
    cfg = cfg or config.load_config()
    url = str(cfg.get("proxy_url", "")).strip()
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if url:
            os.environ[k] = url
        else:
            os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


def clean_key(v):
    """Убирает то, что часто прилипает при копировании: пробелы, невидимые символы, кавычки, «X-API-KEY:»."""
    import re
    v = re.sub(r"[\s ​-‏⁠﻿]", "", v)
    v = v.strip("\"'`«»“”‘’<>")
    v = re.sub(r"^(x-api-key:|x-api-key=|apikey:|api_key=|bearer)", "", v, flags=re.I)
    return v.strip("\"'`«»“”‘’<>")


def save_settings(values):
    """Сохраняет параметры. Возвращает предупреждения (например, перепутаны ключ и ID каталога)."""
    cfg = config.load_config()
    notes = []
    for k, v in values.items():
        if k not in PUBLIC_KEYS:
            continue
        if isinstance(v, str):
            v = v.strip().replace("\n", "").replace("\r", "")
            if k.endswith("_key") or k.endswith("_id"):
                v = clean_key(v)
                if k.endswith("_key") and v and any(m in v for m in ("•", "●", "*", "…", "...")):
                    notes.append("Это скрытая (сокращённая) версия ключа — в списке ключей сайт показывает только "
                                 "его начало. Полный ключ виден один раз, при создании: создайте новый ключ "
                                 "и сразу нажмите «Копировать».")
                    continue
        if k in ("music_volume", "shot_seconds"):
            try:
                v = float(str(v).replace(",", "."))
            except ValueError:
                notes.append(f"Поле {k} должно быть числом — не сохранено.")
                continue
        cfg[k] = v
    if "proxy_url" in values:
        u = str(cfg.get("proxy_url", "")).strip()
        if u and "://" not in u:
            u = "socks5h://" + u                     # «127.0.0.1:10808» → socks5h://127.0.0.1:10808
        if u.startswith("socks5://"):
            u = "socks5h://" + u[len("socks5://"):]   # имена сайтов тоже через прокси
        cfg["proxy_url"] = u
    path = config.save_config(cfg)
    apply_proxy(cfg)
    return {"ok": True, "notes": notes, "path": str(path)}


# ---------------- запуск ----------------
def _load_token():
    try:
        t = TOKEN_FILE.read_text().strip()
        if len(t) >= 20:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        TOKEN_FILE.write_text(t)
    except OSError:
        pass
    return t


TOKEN = _load_token()


def _bind():
    preferred = []
    try:
        preferred.append(int(PORT_FILE.read_text().strip()))
    except (OSError, ValueError):
        pass
    for attempt in range(25):                       # после перезапуска порт освобождается не сразу
        for port in preferred + PORTS:
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                srv.daemon_threads = True
                PORT_FILE.write_text(str(port))
                return srv, port
            except OSError:
                continue
        time.sleep(0.4)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    PORT_FILE.write_text(str(port))
    return srv, port


def _ping(port):
    """None — программа не запущена; иначе True/False — открыто ли её окно (не обновляет «последний визит»)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping?t={TOKEN}", timeout=2) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("window_open"))
    except Exception:
        return None


def _focus_existing(title="DocuStudio"):
    """Выводит уже открытое окно программы на передний план. True — окно найдено."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, title)
        if not hwnd:
            return False
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)                    # SW_RESTORE — из свёрнутого
        else:
            u.ShowWindow(hwnd, 5)                    # SW_SHOW
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _alive(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?t={TOKEN}", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _find_browser():
    cands = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def open_window(url):
    exe = _find_browser()
    if exe:
        prof = ROOT / ".ui_profile"
        proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1280,860",
                                 "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"],
                                creationflags=_NO_WINDOW)
        STATE["browser"] = (proc, time.time())
        return True
    import webbrowser
    webbrowser.open(url)
    return False


def _native_available():
    if os.name != "nt" or os.environ.get("DOCUSTUDIO_BROWSER_WINDOW"):
        return False
    try:
        import webview  # noqa: F401  (pywebview — окно программы на движке WebView2)
        return True
    except Exception:
        return False


def _set_window_icon(title):
    """Иконка DocuStudio в заголовке и на панели задач (по умолчанию была бы иконка Python)."""
    try:
        import ctypes
        u = ctypes.windll.user32
        ico = str(ROOT / "app.ico")
        for _ in range(40):
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                for size, kind in ((32, 1), (16, 0)):       # ICON_BIG / ICON_SMALL
                    h = u.LoadImageW(None, ico, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
                    if h:
                        u.SendMessageW(hwnd, 0x80, kind, h)    # WM_SETICON
                return
            time.sleep(0.25)
    except Exception:
        pass


def native_window(url):
    """Настоящее окно приложения DocuStudio (свой процесс, своя иконка на панели задач, звук из программы).
    Блокирует до закрытия окна. False — не получилось, нужно открыть окно в браузере."""
    try:
        import ctypes
        import webview
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DocuStudio.App")
        except Exception:
            pass
        title = "DocuStudio"
        webview.create_window(title, url, width=1280, height=860, min_size=(980, 640),
                              background_color="#0b1220", text_select=True)
        threading.Thread(target=_set_window_icon, args=(title,), daemon=True).start()
        STATE["native"] = True
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(ROOT / ".ui_webview"))
        return True
    except Exception as e:
        print("Окно приложения не открылось, открываю в браузере:", e, flush=True)
        STATE["native"] = False
        return False


def _watchdog(srv):
    """Закрывает сервер, когда окно закрыто и ничего не собирается."""
    while True:
        time.sleep(3)
        if JOB.running or STATE["restart"]:
            continue
        idle = time.time() - STATE["last_seen"]
        br = STATE.get("browser")
        closed = False
        if br:
            proc, t_start = br
            if proc.poll() is not None and time.time() - t_start > 8:
                closed = idle > 6                     # окно закрыто (страница перестала опрашивать)
        bye = STATE.get("bye") or 0
        if bye and STATE["last_seen"] < bye and time.time() - bye > 6:
            closed = True                             # страница закрыта и не открылась снова
        if closed or idle > 900:
            srv.shutdown()
            os._exit(0)


def main(argv):
    if sys.stdout is None:                          # запуск без консоли (pythonw)
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    no_window = "--no-window" in argv
    try:
        old = int(PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        old = None
    state = _ping(old) if old and not no_window and "--reopen" not in argv else None
    if state is not None:                            # программа уже запущена
        if state and _focus_existing():
            return                                   # окно уже открыто — поднимаем его, второе не открываем
        u = f"http://127.0.0.1:{old}/?t={TOKEN}"
        if not (_native_available() and native_window(u)):
            open_window(u)
        return
    config.ensure_dirs()
    apply_proxy()
    srv, port = _bind()
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    print(f"DocuStudio: {url}", flush=True)
    STATE["last_seen"] = time.time()
    want_window = not no_window and "--headless" not in argv
    if want_window and _native_available():
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=_watchdog, args=(srv,), daemon=True).start()
        if native_window(url):                      # окно закрыто
            STATE["bye"] = time.time()
            STATE["last_seen"] = time.time() - 10
            while True:                             # идёт сборка — дорабатываем, потом сторож закроет программу
                if not JOB.running and not STATE["restart"]:
                    srv.shutdown()
                    os._exit(0)
                time.sleep(2)
        open_window(url)                            # не вышло — окно в браузере, как раньше
        while True:
            time.sleep(3600)
    if want_window:
        open_window(url)
    if "--headless" not in argv:
        threading.Thread(target=_watchdog, args=(srv,), daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
