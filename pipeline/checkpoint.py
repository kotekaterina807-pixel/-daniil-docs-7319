"""Проверка Claude перед монтажом.

Кадры подобраны и проверены, но монтаж (самый долгий этап) ещё не начат. Программа делает
раскадровку (время → слова диктора → кадр), кладёт её в закрытый репозиторий отчётов
(папка live/), пишет в Telegram и ждёт ответа Claude из чата:
  queue/review_*.json  {"project": "...", "approve": true}            — монтировать
  queue/review_*.json  {"project": "...", "times": ["4:31", ...]}     — эти кадры заменить
Нет ответа за отведённое время — монтирует как есть и сообщает об этом."""
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import extras, qa
from .report import _put

PER = 24                 # кадров на лист (4 × 6)
TW, TH = 360, 202


class Restart(Exception):
    """Claude прислал правки — собрать кадры заново (озвучка и всё готовое остаются)."""


def _ok(cfg):
    repo, token = (cfg.get("report_repo") or "").strip(), (cfg.get("report_token") or "").strip()
    return (repo, token) if repo and "/" in repo and token else (None, None)


def _mmss(t):
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def _said(sh, sr):
    q = sh.get("_qa") or {}
    rec = (q.get("blk") or {}).get("words") or []
    said = ""
    if rec and "a" in q:
        said = " ".join(w for st, en, w in rec if q["a"] - 0.1 <= st < q["b"])
    return said.strip() or (q.get("spec") or {}).get("text", "")


def storyboard(shots, project, sr):
    out = Path(project) / "_проверка_перед_монтажом"
    out.mkdir(exist_ok=True)
    for f in out.glob("*.jpg"):
        f.unlink(missing_ok=True)
    f_t = extras._font(22, extras.SEMI_FONTS)
    f_s = extras._font(17, extras.SEMI_FONTS)
    rows, names, index = 6, [], []
    for si in range(0, len(shots), PER):
        part = shots[si:si + PER]
        img = Image.new("RGB", (4 * (TW + 8) + 8, rows * (TH + 70) + 8), (18, 18, 22))
        d = ImageDraw.Draw(img)
        for k, sh in enumerate(part):
            x, y = 8 + (k % 4) * (TW + 8), 8 + (k // 4) * (TH + 70)
            im = None
            if sh.get("src") and sh["kind"] in ("image", "video"):
                im = qa._frame(sh, out / f"_{si + k}.jpg")
            if im is not None:
                im = im.convert("RGB")
                im.thumbnail((TW, TH))
                img.paste(im, (x + (TW - im.width) // 2, y + (TH - im.height) // 2))
            else:
                d.rectangle([x, y, x + TW, y + TH], fill=(40, 36, 30))
                d.text((x + 10, y + 10), {"doc": "ЛИСТ С ТЕКСТОМ", "map": "КАРТА"}.get(sh["kind"], sh["kind"].upper()),
                       font=f_t, fill=(220, 210, 190))
            t = sh["s"] / sr
            d.rectangle([x, y, x + 74, y + 28], fill=(0, 0, 0))
            d.text((x + 6, y + 3), _mmss(t), font=f_t, fill=(255, 210, 120))
            said = _said(sh, sr)
            who = sh.get("who") or ""
            line = (f"[{who}] " if who else "") + said
            d.text((x + 2, y + TH + 4), line[:44], font=f_s, fill=(235, 235, 240))
            d.text((x + 2, y + TH + 26), line[44:88], font=f_s, fill=(200, 200, 210))
            d.text((x + 2, y + TH + 48), line[88:132], font=f_s, fill=(170, 170, 180))
            index.append({"t": _mmss(t), "end": _mmss(sh["e"] / sr), "kind": sh["kind"], "said": said[:200],
                          "who": who, "file": Path(str(sh.get("src") or "")).name,
                          "url": (sh.get("_qa") or {}).get("url", "")})
        p = out / f"кадры_{si // PER + 1:02d}.jpg"
        img.save(p, quality=72)
        names.append(p)
    for f in out.glob("_*.jpg"):
        f.unlink(missing_ok=True)
    return names, index


def publish(cfg, project, sheets, index, log_lines):
    repo, token = _ok(cfg)
    base = f"live/{Path(project).name[:60]}"
    info = {"project": Path(project).name, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sheets": len(sheets),
            "shots": len(index)}
    _put(repo, token, f"{base}/состояние.json", json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8"),
         "проверка перед монтажом")
    _put(repo, token, f"{base}/кадры.json", json.dumps(index, ensure_ascii=False, indent=0).encode("utf-8"),
         "кадры перед монтажом")
    _put(repo, token, f"{base}/журнал.txt", "\n".join(log_lines[-400:]).encode("utf-8"), "журнал")
    for p in sheets:
        _put(repo, token, f"{base}/{p.name}", p.read_bytes(), "раскадровка")
        time.sleep(1.0)
    return base


def wait(cfg, project, check_stop, minutes):
    """Ждёт ответа Claude. Возвращает команду (dict) или None, если время вышло."""
    from . import remote
    key = Path(project).name.lower()
    end = time.time() + minutes * 60
    while time.time() < end:
        for _ in range(6):                     # опрос раз в 30 с, остановку проверяем каждые 5 с
            check_stop()
            time.sleep(5)
        try:
            for name, sha in remote.pending(cfg):
                if not name.lower().startswith("review") or not name.lower().endswith(".json"):
                    continue
                text = remote.take(cfg, name, sha)
                try:
                    cmd = json.loads(text)
                except ValueError:
                    continue
                if (cmd.get("project") or "").lower() in key:
                    return cmd
        except Exception:
            pass
    return None
