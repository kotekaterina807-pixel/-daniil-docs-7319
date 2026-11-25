"""Разбор роликов-примеров для новой ниши (без загрузки самих видео).

Даниил кладёт ролики в папку `samples/` рядом с программой (можно по подпапкам: канал/ролик.mp4).
Из чата в queue/ кладётся samples_*.json: {"samples": true}. Программа для каждого ролика делает:
контактные листы (кадр раз в 3 с), кадры хука (первые 30 с, раз в секунду), монтажные склейки
(время каждой), статистику: длина, разрешение, число склеек, средняя/медианная длина кадра,
и облегчённую копию ролика (360p, части по 4 мин, ~5 МБ) — чтобы Claude посмотрел его со звуком и текстом.
Всё уходит в reports/samples_<дата>/<папка>_<ролик>/ репозитория отчётов."""
import json
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from . import config
from .report import _put

EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _probe(ff, f):
    t = _run([ff, "-hide_banner", "-i", str(f)]).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", t)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0
    r = re.search(r"Video:.*?, (\d{3,5})x(\d{3,5})", t)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", t)
    return dur, (f"{r[1]}x{r[2]}" if r else ""), (float(fps[1]) if fps else 0)


def _cuts(ff, f, thr=0.3):
    t = _run([ff, "-hide_banner", "-i", str(f), "-an", "-vf", f"select='gt(scene,{thr})',showinfo",
              "-f", "null", "-"]).stderr
    return [float(x) for x in re.findall(r"pts_time:(\d+\.?\d*)", t)]


def _sheets(ff, f, out, dur):
    """Листы: раз в 3 с по 20 кадров (минута на лист); хук: первые 30 с раз в секунду."""
    names = []
    _run([ff, "-y", "-i", str(f), "-t", "30", "-an", "-vf", "fps=1,scale=320:-1,tile=5x6",
          "-frames:v", "1", "-q:v", "4", str(out / "хук_0-30с.jpg")])
    names.append("хук_0-30с.jpg")
    _run([ff, "-y", "-i", str(f), "-an", "-vf", "fps=1/3,scale=320:-1,tile=5x4",
          "-q:v", "5", str(out / "лист_%02d.jpg")])
    names += sorted(p.name for p in out.glob("лист_*.jpg"))
    return names


def _light_copy(ff, f, out):
    """Облегчённая копия: 360p, 24 к/с, моно-звук, части по 4 минуты (каждая ~5 МБ, влезает в GitHub)."""
    _run([ff, "-y", "-i", str(f), "-vf", "scale=-2:360,fps=24", "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "30", "-maxrate", "160k", "-bufsize", "320k", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
          "-f", "segment", "-segment_time", "240", "-reset_timestamps", "1", str(out / "видео_%02d.mp4")])
    return sorted(p.name for p in out.glob("видео_*.mp4") if p.stat().st_size < 40_000_000)


def run(cfg, cmd, log=print):
    ff = config.ffmpeg_exe()
    base = Path(cmd.get("folder") or config.ROOT / "samples")
    files = sorted(p for p in base.rglob("*") if p.suffix.lower() in EXT) if base.exists() else []
    if not files:
        raise RuntimeError(f"в папке {base} нет видео")
    repo, token = (cfg.get("report_repo") or "").strip(), (cfg.get("report_token") or "").strip()
    day = time.strftime("%Y-%m-%d_%H%M")
    summary = []
    for f in files:
        name = re.sub(r"[^\w\-]+", "_", f"{f.parent.name}_{f.stem}" if f.parent != base else f.stem)[:50]
        log(f"Разбор примера: {f.name}")
        dur, res, fps = _probe(ff, f)
        cuts = _cuts(ff, f)
        pts = [0.0] + cuts + [dur]
        shots = [b - a for a, b in zip(pts, pts[1:]) if b - a > 0.2]
        info = {"файл": f.name, "папка": str(f.parent.name), "минут": round(dur / 60, 1), "разрешение": res,
                "fps": fps, "склеек": len(cuts), "склеек_в_минуту": round(len(cuts) / (dur / 60), 1) if dur else 0,
                "средняя_длина_кадра_с": round(statistics.mean(shots), 1) if shots else 0,
                "медиана_с": round(statistics.median(shots), 1) if shots else 0,
                "кадров_длиннее_6с": sum(1 for s in shots if s > 6),
                "склейки_в_первые_20с": [round(c, 1) for c in cuts if c < 20],
                "время_склеек": [round(c, 1) for c in cuts]}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            names = _sheets(ff, f, out, dur)
            if cmd.get("video", True):
                log(f"  облегчённая копия: {f.name}")
                names += _light_copy(ff, f, out)
            if repo and token:
                folder = f"reports/samples_{day}/{name}"
                for n in names:
                    _put(repo, token, f"{folder}/{n}", (out / n).read_bytes(), f"пример {name}: {n}")
                _put(repo, token, f"{folder}/статистика.json",
                     json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"), f"пример {name}: статистика")
        summary.append({k: info[k] for k in ("файл", "минут", "склеек_в_минуту", "средняя_длина_кадра_с")})
    log(f"Примеры разобраны: {len(files)}, reports/samples_{day}")
    return summary, f"reports/samples_{day}"
