"""Отчёт о готовом видео для проверки Claude: отправляется в ваш ЗАКРЫТЫЙ репозиторий GitHub.
Что уходит: итоговая раскадровка (секунда → слова → кадр), листы контроля качества и его итог,
кадры готового видео каждые 2 секунды (сетки с временем), субтитры с таймингом, журнал сборки, сценарий.
Само видео целиком не отправляется. Ключи и настройки программы не отправляются никогда."""
import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from . import extras
from .config import ffmpeg_exe

API = "https://api.github.com"


class ReportError(Exception):
    pass


def _h(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DocuStudio"}


def check(cfg):
    repo, token = (cfg.get("report_repo") or "").strip(), (cfg.get("report_token") or "").strip()
    if not repo or "/" not in repo or not token:
        raise ReportError("Укажите репозиторий (владелец/название) и ключ доступа GitHub.")
    try:
        r = requests.get(f"{API}/repos/{repo}", headers=_h(token), timeout=20)
    except requests.RequestException as e:
        raise ReportError(f"Нет связи с GitHub ({type(e).__name__}).")
    if r.status_code in (401, 403):
        raise ReportError("GitHub не принял ключ доступа — создайте его заново (права Contents: Read and write).")
    if r.status_code == 404:
        try:
            u = requests.get(f"{API}/user", headers=_h(token), timeout=20)
        except requests.RequestException:
            u = None
        name = repo.split("/")[1]
        if u is not None and u.status_code == 200:
            raise ReportError(f"Ключ рабочий, но доступа к репозиторию «{repo}» у него нет. На GitHub откройте ключ → "
                              f"Repository access → Only select repositories → выберите {name}; Permissions → Contents: "
                              f"Read and write → Update. Также проверьте, что название репозитория написано без ошибок.")
        raise ReportError("GitHub не принял ключ — создайте его заново и вставьте целиком.")
    d = r.json()
    if not d.get("private"):
        raise ReportError("Репозиторий открытый — для отчётов нужен ЗАКРЫТЫЙ (Private).")
    if not (d.get("permissions") or {}).get("push", True):
        raise ReportError("У ключа нет права записи (Contents: Read and write).")
    return repo


def _put(repo, token, path, data, msg):
    url = f"{API}/repos/{repo}/contents/{path}"
    sha = None
    g = requests.get(url, headers=_h(token), timeout=30)
    if g.status_code == 200:
        sha = g.json().get("sha")
    body = {"message": msg, "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    for attempt in range(6):
        try:
            r = requests.put(url, headers=_h(token), json=body, timeout=120)
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (200, 201):
            return
        if r.status_code in (409, 422) and attempt < 5:                # файл изменился — берём свежий sha
            time.sleep(2)
            try:
                g = requests.get(url, headers=_h(token), timeout=30)
                if g.status_code == 200:
                    body["sha"] = g.json().get("sha")
            except requests.RequestException:
                pass
            continue
        if r.status_code in (403, 429, 500, 502, 503, 504) and attempt < 5:   # GitHub притормаживает или сбоит
            wait = int(r.headers.get("Retry-After") or 0) or 30 * (attempt + 1)
            time.sleep(min(wait, 180))
            continue
        raise ReportError(f"GitHub не принял файл {path}: {r.status_code} {r.text[:160]}")
    raise ReportError(f"GitHub не принял файл {path}: нет связи")


def video_grids(video, out_dir, every=2.0, per=20):
    """Кадры готового видео каждые every секунд — сетки 5×4 с отметкой времени."""
    tmp = Path(tempfile.mkdtemp())
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                    "-vf", f"fps=1/{every},scale=384:-2", str(tmp / "f_%04d.jpg")], timeout=1800,
                   creationflags=0x08000000 if __import__("os").name == "nt" else 0)
    frames = sorted(tmp.glob("f_*.jpg"))
    font = extras._font(22, extras.SEMI_FONTS)
    grids = []
    for gi in range(0, len(frames), per):
        chunk = frames[gi:gi + per]
        W, H = 384, 216
        img = Image.new("RGB", (5 * (W + 6) + 6, ((len(chunk) + 4) // 5) * (H + 6) + 6), (12, 12, 16))
        d = ImageDraw.Draw(img)
        for k, f in enumerate(chunk):
            x, y = 6 + (k % 5) * (W + 6), 6 + (k // 5) * (H + 6)
            img.paste(Image.open(f).convert("RGB").resize((W, H)), (x, y))
            t = (gi + k) * every
            d.rectangle([x, y, x + 76, y + 28], fill=(0, 0, 0))
            d.text((x + 6, y + 2), f"{int(t // 60)}:{int(t % 60):02d}", font=font, fill=(255, 220, 120))
        p = out_dir / f"видео_{gi // per + 1:02d}.jpg"
        img.save(p, quality=82)
        grids.append(p)
    return grids


def _mask(text):
    """Ключи никогда не уходят в отчёт: key=..., token=..., Bearer ..., логин:пароль@ прокси."""
    import re
    text = re.sub(r"(?i)((?:api_?)?key|token|apikey|password|secret)=([^&\s'\"]+)", r"\1=•••", text)
    text = re.sub(r"(?i)(bearer|bot)\s*[:]?\s*[A-Za-z0-9_\-:.]{12,}", r"\1 •••", text)
    text = re.sub(r"(?i)(github_pat_|ghp_|sk-|lk-)[A-Za-z0-9_\-]+", r"\1•••", text)
    return re.sub(r"//[^/@\s:]+:[^/@\s]+@", "//•••:•••@", text)


def send(cfg, project, log_lines, log=print):
    """Отправляет отчёт о проекте. Возвращает папку в репозитории."""
    repo = check(cfg)
    token = cfg["report_token"].strip()
    project = Path(project)
    video = next((p for p in sorted(project.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                  if p.name != "video_only.mp4" and not p.name.endswith(("_telegram.mp4", ".part.mp4"))), None)
    folder = f"reports/{time.strftime('%Y-%m-%d_%H%M')}_{project.name[16:60] or project.name[:40]}"
    tmp = Path(tempfile.mkdtemp())
    # сначала самое важное (журнал, данные, кадры готового видео), картинки подбора — в конце
    files = []
    (tmp / "журнал.txt").write_text(_mask("\n".join(log_lines or [])), encoding="utf-8")
    files.append(tmp / "журнал.txt")
    err = Path(__file__).resolve().parent.parent / "error_log.txt"       # подробности последних ошибок
    if err.exists():
        (tmp / "ошибки.txt").write_text(_mask(err.read_text(encoding="utf-8", errors="replace")[-6000:]), encoding="utf-8")
        files.append(tmp / "ошибки.txt")
    for name in ("монтаж_кадры.json", "проверка.json", "выбор_кадров.json", "план_кадров.json", "script.json",
                 "subtitles.srt", "правки.json", "произношение.txt", "фокус.json"):
        if (project / name).exists():
            files.append(project / name)
    if video:
        log("  Отчёт: кадры готового видео каждые 2 секунды...")
        try:
            files += video_grids(video, tmp)
        except Exception as e:
            log(f"  Отчёт: кадры видео не получились ({e}).")
    files += sorted(project.glob("проверка_*.jpg"))
    files += sorted(project.glob("раскадровка_*.jpg"))[:40]
    info = {"project": project.name, "video": video.name if video else None,
            "sent": time.strftime("%Y-%m-%d %H:%M:%S"), "version": cfg.get("_version", "")}
    log(f"  Отчёт для Claude: отправляю {len(files)} файлов в GitHub...")
    for k, f in enumerate(files):
        _put(repo, token, f"{folder}/{f.name}", f.read_bytes(), f"Отчёт: {project.name}")
        time.sleep(1.0)                                   # не чаще ~60 файлов в минуту — иначе GitHub режет
    _put(repo, token, f"{folder}/info.json", json.dumps(info, ensure_ascii=False, indent=1).encode(), "info")
    _put(repo, token, "latest.txt", folder.encode(), "latest")
    return folder
