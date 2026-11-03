"""Своя библиотека футажей: папка footage с роликами и фото, которые пользователь скачал сам.
Каждый файл один раз просматривает нейросеть (что в кадре, водяные знаки) — результат в footage/_index.json.
Дальше ролики подбираются по смыслу абзаца без интернета (нужен только доступ к нейросети для проверки)."""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path

from PIL import Image

from . import llm
from .config import ROOT, ffmpeg_exe

FOOTAGE_DIR = ROOT / "footage"
INDEX_PATH = FOOTAGE_DIR / "_index.json"
THUMBS_DIR = FOOTAGE_DIR / "_thumbs"
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STOP = {"the", "and", "with", "from", "into", "over", "under", "near", "for", "old", "a", "an", "of", "in", "on", "at"}


def _words(text):
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOP}


def _key(p: Path):
    st = p.stat()
    return hashlib.md5(f"{p.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def _probe(path):
    """Длительность (сек), ширина, высота — из вывода ffmpeg."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    dur, w, h = 0.0, 0, 0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", p.stderr)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", p.stderr)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    return dur, w, h


def _contact_sheet(path, kind, dur, out):
    """Превью: 3 кадра ролика в ряд (начало, середина, конец) или само фото."""
    if kind == "image":
        im = Image.open(path).convert("RGB")
        im.thumbnail((768, 432))
        im.save(out, "JPEG", quality=82)
        return out
    frames = []
    for i, frac in enumerate((0.15, 0.5, 0.85)):
        f = out.with_name(f"{out.stem}_{i}.jpg")
        subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(dur * frac, 0):.2f}",
                        "-i", str(path), "-frames:v", "1", "-vf", "scale=384:-2", str(f)],
                       capture_output=True, creationflags=_NO_WINDOW)
        if f.exists():
            frames.append(Image.open(f).convert("RGB"))
    if not frames:
        return None
    h = min(im.height for im in frames)
    sheet = Image.new("RGB", (sum(im.width for im in frames) + 8 * (len(frames) - 1), h), (0, 0, 0))
    x = 0
    for im in frames:
        sheet.paste(im.crop((0, 0, im.width, h)), (x, 0))
        x += im.width + 8
    sheet.save(out, "JPEG", quality=82)
    for i in range(3):
        out.with_name(f"{out.stem}_{i}.jpg").unlink(missing_ok=True)
    return out


def _describe(cfg, sheet, log):
    b64 = base64.b64encode(Path(sheet).read_bytes()).decode()
    prompt = """Это кадры из одного стокового ролика (или одно фото) для документальных видео.
Опиши, что видно, НА АНГЛИЙСКОМ. Верни СТРОГО JSON:
{"tags": ["15–25 английских ключевых слов: объекты, место, природа, погода, время суток, эпоха, действие, настроение"],
 "description": "одно предложение на английском",
 "watermark": false,
 "text_overlay": false,
 "graphic": false}
watermark — водяной знак или логотип стока/канала; text_overlay — надписи/титры поверх кадра;
graphic — тела погибших, кровь, насилие."""
    raw = llm.chat(cfg, "", [{"type": "image", "b64": b64}, {"type": "text", "text": prompt}], 600, "vision", log)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    a, b = raw.find("{"), raw.rfind("}")
    return json.loads(raw[a:b + 1])


def load_index():
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_index(cfg, log=print, stop_flag=lambda: False):
    """Находит новые файлы в footage и описывает их нейросетью. Возвращает индекс."""
    FOOTAGE_DIR.mkdir(exist_ok=True)
    THUMBS_DIR.mkdir(exist_ok=True)
    index = load_index()
    files = [p for p in FOOTAGE_DIR.rglob("*")
             if p.is_file() and p.suffix.lower() in VIDEO_EXT | IMAGE_EXT and "_thumbs" not in p.parts]
    present = {}
    new = []
    for p in files:
        k = _key(p)
        present[k] = p
        if k not in index:
            new.append((k, p))
    # удалённые файлы убираем из индекса
    for k in [k for k in index if k not in present]:
        index.pop(k, None)
    if new:
        log(f"Библиотека футажей: новых файлов {len(new)} — просматриваю (один раз для каждого файла)...")
    for i, (k, p) in enumerate(new):
        if stop_flag():
            break
        kind = "video" if p.suffix.lower() in VIDEO_EXT else "image"
        try:
            if kind == "video":
                dur, w, h = _probe(p)
            else:
                with Image.open(p) as im:
                    w, h = im.size
                dur = 0.0
            sheet = _contact_sheet(p, kind, dur, THUMBS_DIR / f"{k}.jpg")
            if not sheet:
                log(f"  Не удалось прочитать файл: {p.name}")
                continue
            info = {"tags": re.findall(r"[a-z]{3,}", p.stem.lower().replace("_", " ").replace("-", " "))}
            try:
                info = _describe(cfg, sheet, log)
            except Exception as e:
                log(f"  {p.name}: нейросеть не описала файл ({e}); использую название файла.")
            index[k] = {
                "path": str(p), "kind": kind, "duration": round(dur, 2), "width": w, "height": h,
                "thumb": str(sheet), "tags": [str(t).lower() for t in info.get("tags", [])][:30],
                "description": str(info.get("description", "")),
                "bad": bool(info.get("watermark") or info.get("text_overlay") or info.get("graphic")),
            }
        except Exception as e:
            log(f"  Пропускаю {p.name}: {e}")
            continue
        if (i + 1) % 10 == 0 or i + 1 == len(new):
            log(f"  Просмотрено {i + 1}/{len(new)}")
            INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    usable = sum(1 for v in index.values() if not v["bad"])
    if index:
        log(f"Библиотека футажей: {usable} подходящих файлов из {len(index)} "
            f"(с водяными знаками/надписями отброшены: {len(index) - usable}).")
    return index


def search(index, query, limit=16):
    """Кандидаты из библиотеки по совпадению слов запроса с описанием. Без совпадений — ничего."""
    q = _words(query)
    if not q:
        return []
    scored = []
    for k, v in index.items():
        if v.get("bad") or not Path(v["path"]).exists():
            continue
        if v["kind"] == "video" and v.get("width") and v.get("height") and v["height"] > v["width"]:
            continue                                   # вертикальные ролики не подходят для 16:9
        words = _words(" ".join(v.get("tags", [])) + " " + v.get("description", ""))
        score = len(q & words) + 0.5 * len(q & _words(" ".join(v.get("tags", [])[:8])))
        if score > 0:
            scored.append((score, k, v))
    scored.sort(key=lambda x: -x[0])
    return [{"kind": v["kind"], "url": "file://" + v["path"], "local": v["path"], "thumb": v["thumb"],
             "duration": v.get("duration", 0), "source": "library", "page": "", "title": Path(v["path"]).name,
             "author": "", "license": ""} for _, _, v in scored[:limit]]
