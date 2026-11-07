"""Контроль качества перед монтажом: Claude Code смотрит КАЖДЫЙ кадр готового видеоряда вместе
со словами, которые звучат в этот момент, и заменяет неподходящие на лучший запасной вариант.
Листы проверки и отчёт сохраняются в папке проекта (проверка_NN.jpg, проверка.json)."""
import itertools
import json
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import claude_code, extras, review
from .config import ffmpeg_exe

CUR_W, CUR_H = 560, 315        # текущий кадр — крупно
ALT_W, ALT_H = 300, 169        # запасные варианты
PER_SHEET = 4
SHEET_W = 14 + CUR_W + 14 + 3 * (ALT_W + 8) + 14

PROMPT = """Ты — строгий редактор документального видео на YouTube. Тема: {topic}
Открой файл {sheet} инструментом Read. На нём кадры готового видео по порядку. У каждого кадра:
номер, время в ролике, ТОЧНЫЕ слова диктора, которые звучат, пока кадр на экране, сам кадр (крупно, слева)
и до трёх запасных вариантов (справа, номера 1–3).

Проверь КАЖДЫЙ кадр. Кадр хороший, только если зритель, услышав эти слова, увидит то же самое:
тот же предмет, место, действие, человека, эпоху. Плохой кадр, если:
- он не про эти слова (общий план «природа/лес/море», когда говорят о конкретном; посторонние вещи);
- на нём человек, которого в этих словах НЕ называют, или человек появился раньше, чем о нём сказали;
- ПОДПИСЬ на фото называет другого человека (однофамилец, другое имя: «Stella Mallory» вместо Джорджа Мэллори,
  «Irvine-Smith» вместо Эндрю Ирвина) или это обложка/экслибрис/страница книги вместо фото человека;
- предмет не той эпохи: современный объектив, пластик, современная одежда в истории 1920-х;
- памятник, табличка, могила вместо самого человека; современные люди/техника в истории о прошлом;
- водяной знак, логотип, надписи, интерфейс; быстрый мельтешащий кадр; мультяшная графика.

Ответ — ТОЛЬКО строки, по одной на каждый кадр, без пояснений:
7: ok
8: 2      ← кадр плохой, лучше запасной вариант 2
9: плохо | search: wooden barrels ship hold; old ship cargo barrels   ← плохой, запасные не подходят
Для каждого плохого кадра без замены ОБЯЗАТЕЛЬНО допиши после «search:» 1–2 точных английских запроса
(2–5 слов) — что именно искать, чтобы кадр совпал со словами.
{draw}"""

DRAW = """
Кроме запросов, для плохого кадра опиши по-английски картинку, которую художник нарисует, если поиск не поможет,
ровно про эти слова диктора: место, время года и суток, эпоха, предметы, действие, ракурс. Лица реальных людей
не рисуем: люди — со спины, силуэтом, издалека или в капюшонах/масках. Без надписей. Формат:
9: плохо | search: snowy ridge climbers 1920s | draw: two climbers in 1920s wool clothing seen from behind on a snowy ridge
"""


def _frame(item, out):
    """Картинка того, что реально будет на экране (для видео — кадр из середины используемого куска)."""
    try:
        if item["kind"] == "image":
            return Image.open(item["src"])
        t = max(0.5, min(3.0, float(item.get("clip_dur") or 4) / 3))
        for tt in (t, 0.3, 0.0):                       # ролик короче, чем заявлено, — берём кадр раньше
            Path(out).unlink(missing_ok=True)
            subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{tt:.2f}", "-i",
                            item["src"], "-frames:v", "1", "-q:v", "3", str(out)], timeout=60,
                           creationflags=0x08000000 if __import__("os").name == "nt" else 0)
            if Path(out).exists() and Path(out).stat().st_size > 0:
                im = Image.open(out)
                im.load()
                return im
        return None
    except Exception:
        return None


def _sheets(entries, out_dir, prefix="проверка"):
    for old in out_dir.glob(f"{prefix}_*.jpg"):
        old.unlink(missing_ok=True)
    alt_urls = list({a["thumb"] for e in entries for a in e["alts"] if a.get("thumb")})
    with ThreadPoolExecutor(max_workers=8) as ex:
        thumbs = dict(zip(alt_urls, ex.map(review._thumb, alt_urls)))
    f_head = extras._font(24, extras.SEMI_FONTS)
    f_small = extras._font(19, extras.SEMI_FONTS)
    f_num = extras._font(26, extras.SMALL_CAPS_FONTS)
    row_h = 70 + CUR_H + 16
    names = []
    for si in range(0, len(entries), PER_SHEET):
        chunk = entries[si:si + PER_SHEET]
        img = Image.new("RGB", (SHEET_W, 20 + row_h * len(chunk)), (18, 22, 32))
        d = ImageDraw.Draw(img)
        y = 14
        for e in chunk:
            mm, ss = divmod(int(e["time"]), 60)
            head = f"{e['no']}.  {mm}:{ss:02d}  ·  «{e['said']}»"
            for k, part in enumerate(textwrap.wrap(head, 120)[:2]):
                d.text((14, y + k * 27), part, font=f_head, fill=(240, 240, 245))
            cy = y + 62
            cur = e.get("frame_img")
            if cur is not None:
                img.paste(ImageOps.fit(cur.convert("RGB"), (CUR_W, CUR_H), Image.LANCZOS), (14, cy))
            else:
                d.rectangle([14, cy, 14 + CUR_W, cy + CUR_H], fill=(40, 44, 56))
            d.rectangle([14, cy, 14 + 150, cy + 30], fill=(200, 40, 40))
            d.text((22, cy + 3), "СЕЙЧАС В КАДРЕ", font=f_small, fill=(255, 255, 255))
            for k, a in enumerate(e["alts"][:3]):
                x = 14 + CUR_W + 14 + k * (ALT_W + 8)
                t = thumbs.get(a.get("thumb"))
                if t is not None:
                    img.paste(ImageOps.fit(t, (ALT_W, ALT_H), Image.LANCZOS), (x, cy))
                else:
                    d.rectangle([x, cy, x + ALT_W, cy + ALT_H], fill=(40, 44, 56))
                d.rectangle([x, cy, x + 36, cy + 32], fill=(35, 87, 232))
                d.text((x + 10, cy + 1), str(k + 1), font=f_num, fill=(255, 255, 255))
                kind = "видео" if a["kind"] == "video" else ("человек" if a.get("person") else "фото")
                d.text((x + 4, cy + ALT_H + 4), kind, font=f_small, fill=(200, 200, 210))
            y += row_h
        p = out_dir / f"{prefix}_{si // PER_SHEET + 1:02d}.jpg"
        img.save(p, quality=88)
        names.append(p.name)
    return names


PROMPTS = {}          # номер кадра → что нарисовать (из последнего ответа Claude)
SEARCHES = {}         # номер кадра → новые запросы, если кадр плохой и запасные не подошли


def parse(text):
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        no, _, verdict = line.partition(":")
        if not no.strip().isdigit():
            continue
        verdict, _, extra = verdict.partition("|")
        for part in extra.split("|"):
            part = part.strip()
            if part.lower().startswith("draw:"):
                PROMPTS[int(no)] = part[5:].strip()
            elif part.lower().startswith("search:"):
                SEARCHES[int(no)] = [q.strip(" .\"'") for q in part[7:].split(";") if q.strip(" .\"'")][:2]
        v = verdict.strip().lower()
        if v.startswith("ok") or v.startswith("ок"):
            out[int(no)] = "ok"
        elif v[:1].isdigit():
            out[int(no)] = int(v[:1])
        elif v.startswith("плох") or v.startswith("bad") or v.startswith("-"):
            out[int(no)] = "bad"
    return out


def check(cfg, project, entries, finder, topic, log, progress=None, taken=None, _round=1):
    """entries: [{"no", "time", "said", "shot", "alts"}]. Меняет shot на месте. Возвращает отчёт."""
    if not entries:
        return {}
    tmp = project / "_qa_frames"
    tmp.mkdir(exist_ok=True)
    for e in entries:
        e["frame_img"] = _frame(e["shot"], tmp / f"{e['no']}.jpg")
    names = _sheets(entries, project, prefix="проверка" if _round == 1 else "проверка_повтор")
    from . import illustrate
    draw_on = illustrate.available(cfg)
    log(f"Контроль качества: Claude Code проверяет каждый кадр ({len(entries)} шт., листов {len(names)})..."
        if _round == 1 else f"  Повторная проверка: {len(entries)} кадров с новыми вариантами...")
    verdicts = {}
    done = [0]
    PROMPTS.clear()
    SEARCHES.clear()

    def one(name):
        ans = claude_code.run(cfg, PROMPT.format(sheet=name, topic=topic or "документальное видео",
                                                 draw=DRAW if draw_on else ""), project)
        done[0] += 1
        if progress:
            progress(done[0] / len(names), f"Контроль качества: лист {done[0]}/{len(names)}")
        return parse(ans)
    with ThreadPoolExecutor(max_workers=2) as ex:
        for v in ex.map(one, names):
            verdicts.update(v)
    by_no = {e["no"]: e for e in entries}
    verdicts = {k: v for k, v in verdicts.items() if k in by_no}
    prompts = {k: v for k, v in PROMPTS.items() if k in by_no}
    searches = {k: v for k, v in SEARCHES.items() if k in by_no}
    replaced, bad = 0, []
    used = set()
    for no, v in sorted(verdicts.items()):
        e = by_no.get(no)
        if not e or v == "ok":
            continue
        if isinstance(v, int) and 1 <= v <= len(e["alts"]):
            a = e["alts"][v - 1]
            if a["url"] in used:
                bad.append(no)
                continue
            path = finder._download(a)
            if path:
                used.add(a["url"])
                sh = e["shot"]
                sh.update({"kind": a["kind"], "src": str(path), "clip_dur": a.get("duration") or 0,
                           "offset_n": 0, "speed": 1.0, "person": bool(a.get("person"))})
                replaced += 1
                continue
        bad.append(no)
    taken = set(taken or ()) | used
    if _round == 1 and bad:                          # второй шанс: новый поиск по запросам Claude и ещё одна проверка
        retry = []
        for no in bad:
            qs = searches.get(no) or []
            if not qs:
                continue
            cands, seen = [], set()
            for q in qs:
                for src in (finder._candidates("archive", [q]), iter(finder._web_images(q)),
                            finder._candidates("stock", [q])):
                    for c in itertools.islice(src, 4):
                        if c.get("url") and c["url"] not in taken and c["url"] not in seen and c.get("thumb"):
                            seen.add(c["url"])
                            cands.append(c)
                    if len(cands) >= 6:
                        break
            if cands:
                e = by_no[no]
                retry.append({"no": no, "time": e["time"], "said": e["said"], "shot": e["shot"], "alts": cands[:3]})
        if retry:
            log(f"  Нет подходящей замены у {len(retry)} кадров — ищу по новым запросам Claude и проверяю ещё раз...")
            rep2 = check(cfg, project, retry, finder, topic, log, None, taken, _round=2)
            fixed = {int(k) for k in (rep2.get("verdicts") or {})} - set(rep2.get("bad_left") or [])
            replaced += len(fixed)
            bad = [n for n in bad if n not in fixed]
            for k, v in (rep2.get("prompts") or {}).items():
                prompts.setdefault(int(k), v)
    drawn = 0
    if draw_on and bad and _round == 1:                               # нет подходящего кадра — рисуем ровно про эти слова
        from concurrent.futures import ThreadPoolExecutor as _TP
        jobs = [(no, prompts.get(no)) for no in bad if prompts.get(no)]
        log(f"  Рисую кадры, которых нет ни в архивах, ни в интернете: {len(jobs)} шт. (Leonardo)...")
        stop = [False]

        def paint(job):
            no, prompt = job
            if stop[0]:
                return no, None
            try:
                return no, illustrate.generate(cfg, prompt, project / "_drawn" / f"{no}_{abs(hash(prompt)) % 10**8}.jpg", log)
            except illustrate.IllustrateError:
                stop[0] = True
                return no, None
        (project / "_drawn").mkdir(exist_ok=True)
        with _TP(max_workers=3) as ex:
            for no, path in ex.map(paint, jobs):
                if path:
                    by_no[no]["shot"].update({"kind": "image", "src": str(path), "clip_dur": 0, "offset_n": 0,
                                              "speed": 1.0, "person": False})
                    bad.remove(no)
                    drawn += 1
        if drawn:
            log(f"  Нарисовано кадров: {drawn}.")
    report = {"checked": len(entries), "answered": len(verdicts), "replaced": replaced, "bad_left": bad, "drawn": drawn,
              "prompts": {str(k): v for k, v in prompts.items()},
              "verdicts": {str(k): v for k, v in verdicts.items()},
              "said": {str(e["no"]): e["said"] for e in entries}}
    if _round > 1:
        for e in entries:
            e.pop("frame_img", None)
        return report
    (project / "проверка.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  Контроль качества: проверено {len(verdicts)} из {len(entries)}, заменено {replaced}"
        + (f", без хорошей замены: {len(bad)} (кадры {', '.join(map(str, bad[:12]))})" if bad else "") + ".")
    # итоговая раскадровка (после замен) — по ней видно весь ролик: секунда → слова → кадр
    try:
        for e in entries:
            e["frame_img"] = _frame(e["shot"], tmp / f"{e['no']}_final.jpg")
            e["alts"] = []
        _sheets(entries, project, prefix="раскадровка")
    except Exception as e:
        log(f"  Итоговая раскадровка не сохранилась: {e}")
    for e in entries:
        e.pop("frame_img", None)
    return report
