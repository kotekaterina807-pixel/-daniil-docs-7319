"""Проверка кадров через Claude в чате (бесплатно и точнее автоматической нейросети).
1) Программа находит кандидатов под каждую строку кадров и собирает «листы»: строка сценария + 8 превью с номерами.
2) Листы отправляются Claude в чат, Claude отвечает номерами подходящих кадров.
3) Ответ вставляется в программу — сборка продолжается с выбранными кадрами."""
import io
import json
import re
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageOps

from . import extras
from .visuals import UA, _overlap

CANDS = 8                 # кандидатов на строку (2 ряда по 4)
PER_ROW = 4
ROWS_PER_SHEET = 3        # строк сценария на лист
TW, TH = 372, 209         # крупные превью — Claude лучше видит, что в кадре, и водяные знаки
SHEET_W = 14 + PER_ROW * (TW + 8) + 6


class NeedChoices(Exception):
    """Кандидаты собраны, нужен выбор кадров в чате."""

    def __init__(self, sheets, lines):
        super().__init__("Нужно выбрать кадры")
        self.sheets = sheets
        self.lines = lines


def collect(finder, spec, taken):
    """До CANDS кандидатов для строки: сначала видео, без повторов по всему видео."""
    out = []
    if spec["visual"] == "person":
        for c in finder._person_photos(spec["query"], spec.get("shown", ""))[:4]:
            if c["url"] not in taken and c.get("thumb"):
                out.append(dict(c, person=True))
        query, visual = spec.get("fill_query") or "dark atmosphere", "stock"
    else:
        query, visual = spec["query"], spec["visual"]
    pool, seen_pool = [], set()
    planned = [q for q in spec.get("queries") or [] if q]
    if planned:
        # режиссёрский план: по 3 лучших кадра на каждый точный запрос, потом запрос из сценария;
        # без обрезанных «на 2 слова» запросов — они и давали «просто лес» и «просто стену»
        groups = []
        for q in planned + [query]:
            g = []
            for c in finder._candidates(visual, [q]):
                if c.get("thumb") and c["url"] not in taken and c["url"] not in seen_pool:
                    seen_pool.add(c["url"])
                    g.append(c)
                    if len(g) >= 4:
                        break
            groups.append(g)
        for k in range(4):                         # по очереди из каждого запроса
            for g in groups:
                if k < len(g):
                    pool.append(g[k])
        if len(pool) < CANDS:                      # мало — фото этих предметов (они есть почти всегда)
            for q in planned[:2]:
                for c in finder._candidates("photo", [q])[:3]:
                    if c.get("thumb") and c["url"] not in taken and c["url"] not in seen_pool:
                        seen_pool.add(c["url"])
                        pool.append(c)
    else:
        for c in finder._candidates(visual, finder._variants(query)):   # источники опрашиваются по мере надобности
            if c.get("thumb") and c["url"] not in taken and c["url"] not in seen_pool:
                seen_pool.add(c["url"])
                pool.append(c)
                if len(pool) >= 24:
                    break
    if visual != "photo" and not planned:
        pool.sort(key=lambda c: (0 if c["kind"] == "video" else 1, -_overlap(query, c.get("tags", ""))))
    seen = {c["url"] for c in out}
    for c in pool:
        if len(out) >= CANDS:
            break
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        out.append(c)
    for c in out:
        taken.add(c["url"])
    return out


def _thumb(url):
    try:
        if Path(url).exists():
            im = Image.open(url)
        else:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content))
        return ImageOps.fit(im.convert("RGB"), (TW, TH), Image.LANCZOS)
    except Exception:
        return None


def make_sheets(lines, out_dir, log=print, prefix="кадры_лист"):
    """Листы для проверки: на каждой строке — текст диктора, запрос и 8 пронумерованных превью."""
    out_dir = Path(out_dir)
    for old in out_dir.glob(f"{prefix}_*.jpg"):
        old.unlink(missing_ok=True)
    urls = [c["thumb"] for ln in lines for c in ln["cands"]]
    with ThreadPoolExecutor(max_workers=6) as ex:
        thumbs = dict(zip(urls, ex.map(_thumb, urls)))
    f_head = extras._font(24, extras.SEMI_FONTS)
    f_small = extras._font(19, extras.SEMI_FONTS)
    f_num = extras._font(26, extras.SMALL_CAPS_FONTS)
    sheets = []
    def row_h(ln):
        rows = max(1, -(-len(ln["cands"]) // PER_ROW))
        return 82 + rows * (TH + 8) + 12
    for si in range(0, len(lines), ROWS_PER_SHEET):
        chunk = lines[si:si + ROWS_PER_SHEET]
        img = Image.new("RGB", (SHEET_W, 20 + sum(row_h(ln) for ln in chunk)), (18, 22, 32))
        d = ImageDraw.Draw(img)
        y = 14
        for ln in chunk:
            head = f"{ln['no']}.  нужно до {ln['n']}  ·  «{ln['text']}»"
            if ln.get("person"):
                head = f"{ln['no']}.  ЧЕЛОВЕК: {ln['person']}  ·  «{ln['text']}»"
            for k, part in enumerate(textwrap.wrap(head, 105)[:2]):
                d.text((14, y + k * 27), part, font=f_head, fill=(240, 240, 245))
            show = ln.get("show") or ln["query"]
            d.text((14, y + 56), textwrap.shorten(f"В КАДРЕ: {show}", 120, placeholder="…"), font=f_small,
                   fill=(255, 205, 120))
            for k, c in enumerate(ln["cands"]):
                x = 14 + (k % PER_ROW) * (TW + 8)
                ty = y + 82 + (k // PER_ROW) * (TH + 8)
                t = thumbs.get(c["thumb"])
                if t is not None:
                    img.paste(t, (x, ty))
                else:
                    d.rectangle([x, ty, x + TW, ty + TH], fill=(40, 44, 56))
                    d.text((x + 100, ty + 80), "нет превью", font=f_small, fill=(160, 160, 170))
                d.rectangle([x, ty, x + 40, ty + 34], fill=(35, 87, 232))
                d.text((x + (9 if k < 9 else 3), ty + 2), str(k + 1), font=f_num, fill=(255, 255, 255))
                kind = "видео" if c["kind"] == "video" else ("человек" if c.get("person") else "фото")
                tag = kind + (f" {int(c.get('duration') or 0)}с" if c["kind"] == "video" else "")
                tw = d.textlength(tag, font=f_small)
                d.rectangle([x + TW - tw - 12, ty + TH - 26, x + TW, ty + TH], fill=(0, 0, 0))
                d.text((x + TW - tw - 6, ty + TH - 25), tag, font=f_small, fill=(230, 230, 230))
            if not ln["cands"]:
                d.text((14, y + 120), "кандидатов не нашлось", font=f_head, fill=(255, 140, 140))
            y += row_h(ln)
        p = out_dir / f"{prefix}_{si // ROWS_PER_SHEET + 1:02d}.jpg"
        img.save(p, quality=90)
        sheets.append(p.name)
    log(f"  Листы для проверки кадров: {len(sheets)} шт. (строк сценария: {len(lines)})")
    return sheets


def parse_choices(text, lines):
    """Ответ вида «12: 3 1 5» / «13: -». Возвращает {номер строки: [номера кадров]}."""
    valid = {ln["no"]: len(ln["cands"]) for ln in lines}
    out = {}
    for m in re.finditer(r"(?m)^\s*(\d+)\s*[:.)\-–—]\s*([0-9 ,;]*|[-–—нет]+)\s*$", text or ""):
        no = int(m.group(1))
        if no not in valid:
            continue
        nums = [int(x) for x in re.findall(r"\d+", m.group(2))]
        out[no] = [k for k in dict.fromkeys(nums) if 1 <= k <= valid[no]]
    return out


def save_review(path, lines):
    Path(path).write_text(json.dumps({"lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8")


def load_review(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("lines")
    except Exception:
        return None
