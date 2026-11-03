"""Сборка проекта по шагам. Каждый шаг кешируется: при сбое перезапуск продолжит с места остановки."""
import datetime
import hashlib
import json
import math
import os
import random
import re
import wave
from pathlib import Path

from . import extras, render, script_format, script_gen, timing, tts
from .config import MUSIC_DIR, OUTPUT_DIR, ensure_dirs, ffmpeg_exe, lang
from . import library
from .visuals import VisualFinder

PAUSE_SEC = 0.35     # пауза между абзацами
CARD_SEC = 1.8       # название главы поверх видео
TITLE_SEC = 4.0      # заставка с названием в начале
LABEL_SEC = 5.0      # сколько держится всплывающая подпись

# Переходы: только плавный наплыв, короткий
TR_INNER = 0.30      # между кадрами внутри абзаца
TR_PARA = 0.45       # между абзацами
TR_CARD = 0.45       # в название главы и из него
TR_TITLE = 0.80      # из заставки в первый кадр
DARKEN = "eq=brightness=-0.16:saturation=0.8,gblur=sigma=4"   # фон под названием ролика и главы

# Лёгкая поправка цвета по настроению
MOOD_GRADE = {"normal": "", "tense": "eq=brightness=-0.015:saturation=0.95", "climax": "eq=contrast=1.04:saturation=1.03"}
SR = tts.SAMPLE_RATE


def _slug(text):
    s = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[\s_]+", "-", s)[:50] or "video"


def new_project_dir(topic):
    ensure_dirs()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    d = OUTPUT_DIR / f"{stamp}_{_slug(topic)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(p, data):
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- Шаг 1: сценарий ----------------
def step_script(cfg, niche, niche_path, topic, minutes, project, log=print):
    project = Path(project)
    script = script_gen.generate_script(cfg, niche, topic, minutes, log)
    script["niche_file"] = str(niche_path)
    script["minutes"] = minutes
    script["language"] = cfg.get("language", "ru")
    _save_json(project / "script.json", script)
    (project / "script.txt").write_text(script_format.to_text(script), encoding="utf-8")
    extras.write_facts(script, project / "ПРОВЕРИТЬ_ФАКТЫ.txt")
    log(f"Сценарий сохранён: {project / 'script.txt'}")
    return script


def project_from_text(txt_path, niche_path, log=print):
    """Новый проект из готового сценария (.txt), написанного вручную или в чате."""
    text = Path(txt_path).read_text(encoding="utf-8-sig")
    script = script_format.apply_text({"title": "", "language": "ru", "tags": [], "description": ""}, text)
    if not script.get("title"):
        script["title"] = script["chapters"][0]["title"]
    script.setdefault("topic", script["title"])
    script["niche_file"] = str(niche_path)
    project = new_project_dir(script["title"])
    _save_json(project / "script.json", script)
    (project / "script.txt").write_text(text, encoding="utf-8")
    extras.write_facts(script, project / "ПРОВЕРИТЬ_ФАКТЫ.txt")
    words = sum(len(s["text"].split()) for c in script["chapters"] for s in c["segments"])
    log(f"Сценарий загружен: «{script['title']}», глав: {len(script['chapters'])}, слов: {words} "
        f"(~{words / lang(script.get('language', 'ru'))['wpm']:.1f} мин).")
    log(f"Проект: {project}")
    return project


# ---------------- Шаг 2: видео ----------------
def _key(*parts):
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _pick_music(niche):
    folder = MUSIC_DIR / niche.get("id", "")
    files = []
    for d in (folder, MUSIC_DIR):
        if d.exists():
            files = [p for p in d.iterdir() if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac")]
            if files:
                break
    return random.choice(files) if files else None


def _grade(niche, mood):
    parts = [g for g in (niche.get("grade", ""), MOOD_GRADE.get(mood, "")) if g]
    return ",".join(parts) or "null"


def _frame_from_video(clip, out):
    import subprocess
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(clip),
                    "-frames:v", "1", str(out)], capture_output=True,
                   creationflags=0x08000000 if os.name == "nt" else 0)
    return str(out) if Path(out).exists() else None


def step_build(cfg, niche, project, log=print, stop_flag=lambda: False, progress=lambda f, t: None):
    """progress(доля 0..1, текст этапа) — для индикатора и оценки оставшегося времени."""
    project = Path(project)
    from .config import VERSION
    log(f"Версия программы: {VERSION}")
    script = _load_json(project / "script.json", None)
    if not script:
        raise RuntimeError("В папке проекта нет script.json — сначала создайте сценарий.")
    txt = project / "script.txt"
    if txt.exists():
        script = script_format.apply_text(script, txt.read_text(encoding="utf-8"))

    code = script.get("language", cfg.get("language", "ru"))
    L = lang(code)
    cfg = dict(cfg)
    cfg["language"] = code
    if not str(cfg.get("edge_voice", "")).lower().startswith(code + "-"):
        cfg["edge_voice"] = L["edge_voice"]
    accent = tuple(niche.get("accent", [200, 30, 30]))

    audio_dir, media_dir, shots_dir = project / "audio", project / "media", project / "shots"
    for d in (audio_dir, media_dir, shots_dir):
        d.mkdir(exist_ok=True)

    def check_stop():
        if stop_flag():
            raise RuntimeError("Остановлено пользователем.")

    # ---------- 1. озвучка ----------
    provider = cfg.get("tts_provider", "yandex")
    if provider == "yandex" and code not in tts.YANDEX_LANGS:
        provider = "edge"
        log(f"У Yandex SpeechKit нет голоса для языка «{L['name']}» — озвучка через Edge (бесплатно).")
    voice = {"openai": cfg.get("openai_voice"), "elevenlabs": cfg.get("elevenlabs_voice_id"),
             "edge": cfg.get("edge_voice"),
             "yandex": f"{cfg.get('yandex_voice')}:{cfg.get('yandex_speed')}" if code == "ru" else code
             }.get(provider, "")
    segs = [s for ch in script["chapters"] for s in ch["segments"]]
    log(f"Озвучка: {len(segs)} абзацев ({provider}, {L['name']})...")
    for i, s in enumerate(segs):
        check_stop()
        s["audio"] = str(audio_dir / f"{_key(provider, voice or '', s['text'])}.wav")
        if not Path(s["audio"]).exists():
            prev_t = segs[i - 1]["text"] if i else ""
            next_t = segs[i + 1]["text"] if i + 1 < len(segs) else ""
            tts.synthesize(cfg, niche, s["text"], s["audio"], prev_t, next_t, log)
        progress(0.10 * (i + 1) / len(segs), f"Озвучка {i + 1}/{len(segs)}")
        if (i + 1) % 10 == 0 or i + 1 == len(segs):
            log(f"  Озвучено {i + 1}/{len(segs)}")

    # ---------- 2. таймлайн (в сэмплах — без рассинхрона) ----------
    narration = project / "narration.wav"
    pos = 0
    subs, chapter_starts, blocks = [], [], []
    pause = int(PAUSE_SEC * SR)
    with wave.open(str(narration), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        title_len = int(TITLE_SEC * SR)
        out.writeframes(b"\x00\x00" * title_len)
        blocks.append({"type": "title", "start": 0, "end": title_len})
        pos = title_len
        for ci, ch in enumerate(script["chapters"]):
            chapter_starts.append((ch["title"], 0.0 if ci == 0 else pos / SR))
            if ci > 0:
                card = int(CARD_SEC * SR)
                out.writeframes(b"\x00\x00" * card)
                blocks.append({"type": "card", "chapter": ci, "start": pos, "end": pos + card})
                pos += card
            for s in ch["segments"]:
                with wave.open(s["audio"], "rb") as w:
                    n = w.getnframes()
                    out.writeframes(w.readframes(n))
                out.writeframes(b"\x00\x00" * pause)
                sents, pauses, _ = timing.sentence_times(s["text"], s["audio"])
                for a, b, t in sents:
                    subs.append({"text": t, "start": pos / SR + a, "end": pos / SR + b})
                blocks.append({"type": "seg", "seg": s, "chapter": ci, "start": pos, "end": pos + n + pause,
                               "pauses": pauses, "sent_bounds": [b for _, b, _ in sents[:-1]],
                               "sents": sents})
                pos += n + pause
    total_sec = pos / SR
    log(f"Длительность видео: {total_sec / 60:.1f} мин.")

    # ---------- 3. подбор кадров: своя строка кадра — под своё предложение ----------
    log("Подбираю кадры под каждое предложение и проверяю их по смыслу...")
    vis_cache_path = project / "visuals.json"
    vis_cache = _load_json(vis_cache_path, {})
    finder = VisualFinder(cfg, niche, media_dir, log)
    from . import vision
    vision.reset()
    try:
        finder.library = library.update_index(cfg, log, stop_flag)
    except Exception as e:
        log(f"  Библиотека футажей не прочитана: {e}")
    for v in vis_cache.values():
        for item in v.get("items", []):
            finder.used.add(item.get("url", ""))
    shot_sec = float(cfg.get("shot_seconds", 2.0))
    max_shot = float(cfg.get("max_shot_seconds", 3.2))
    seg_blocks = [b for b in blocks if b["type"] == "seg"]
    jobs = []
    for bi, b in enumerate(seg_blocks):
        s = b["seg"]
        span = (b["end"] - b["start"]) / SR
        specs = list(s.get("shots") or [(s["visual"], s["query"])])
        sents = b["sents"] or [(0.0, span, s["text"])]
        k = len(specs)
        ranges, texts = [], []
        for i in range(k):
            if k <= len(sents):
                i0, i1 = i * len(sents) // k, (i + 1) * len(sents) // k - 1
                t0 = 0.0 if i == 0 else sents[i0][0]
                t1 = span if i == k - 1 else sents[i1 + 1][0]
                txt = " ".join(x[2] for x in sents[i0:i1 + 1])
            else:                                     # строк кадров больше, чем предложений — делим время
                t0, t1 = span * i / k, span * (i + 1) / k
                txt = sents[min(len(sents) - 1, i * len(sents) // k)][2]
            ranges.append((t0, t1))
            texts.append(txt)
        b["specs"] = []
        for i, ((vis, query), (t0, t1), txt) in enumerate(zip(specs, ranges, texts)):
            length = max(0.1, t1 - t0)
            n = max(1, round(length / shot_sec), math.ceil(length / max_shot - 1e-6))
            n = min(n, max(1, int(length / 1.1)))
            spec = {"visual": vis, "query": query, "t0": t0, "t1": t1, "n": n, "text": txt}
            if vis == "person":
                name, _, shown = query.partition("|")
                spec["query"], spec["shown"] = name.strip(), (shown.strip() or name.strip())
                # остальное время этой строки — кадры по соседней строке сценария
                other = next((q for v, q in specs[i + 1:] + specs[:i][::-1] if v != "person"), "dark atmosphere")
                spec["fill_query"] = other
            b["specs"].append(spec)
            jobs.append((bi, len(b["specs"]) - 1))

    def run_job(job):
        bi, si = job
        b = seg_blocks[bi]
        spec = b["specs"][si]
        topic = script.get("topic", "")
        key = _key(spec["visual"], spec["query"], str(spec["n"]), spec["text"][:80],
                   "strict" if cfg.get("strict_visuals", True) else "", "v2")
        cached = vis_cache.get(key)
        if cached and all(Path(it["path"]).exists() for it in cached["items"]):
            return key, cached
        items = []
        if spec["visual"] == "person":
            items = [dict(f, person=True) for f in finder.find_person(spec["query"], spec["text"], topic)]
            rest = spec["n"] - len(items)
            if rest > 0:
                items += finder.find("stock", spec["fill_query"], rest, spec["text"], topic)
        else:
            items = finder.find(spec["visual"], spec["query"], spec["n"], spec["text"], topic)
        out = {"items": [{"path": f["path"], "kind": f["kind"], "url": f["url"], "duration": f.get("duration", 0),
                          "person": bool(f.get("person"))} for f in items],
               "credits": [f["credit"] for f in items]}
        return key, out

    from concurrent.futures import ThreadPoolExecutor
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for (bi, si), (key, res) in zip(jobs, ex.map(run_job, jobs)):
            check_stop()
            vis_cache[key] = res
            seg_blocks[bi]["specs"][si]["items"] = res["items"]
            done += 1
            if done % 5 == 0 or done == len(jobs):
                _save_json(vis_cache_path, vis_cache)
                log(f"  Кадры подобраны: {done}/{len(jobs)} строк сценария")
            progress(0.10 + 0.35 * done / len(jobs), f"Подбор кадров {done}/{len(jobs)}")
    finder.credits = [c for v in vis_cache.values() for c in v.get("credits", [])
                      if any(c.get("url") == it.get("url") for b in seg_blocks for sp in b["specs"]
                             for it in sp.get("items", []))]
    for b in seg_blocks:
        b["items"] = [it for sp in b["specs"] for it in sp.get("items", [])]
    st = vision.stats()
    n_video = sum(1 for b in seg_blocks for it in b["items"] if it["kind"] == "video")
    n_all = sum(len(b["items"]) for b in seg_blocks)
    log(f"  Найдено кадров: {n_all}, из них видео: {n_video}, фото: {n_all - n_video}.")
    if st.get("off"):
        log("  ВНИМАНИЕ: проверка по смыслу отключилась во время подбора — часть кадров выбрана по тегам. "
            "Нажмите «Проверить всё» в настройках и пришлите результат.")
    elif st.get("checked"):
        log(f"  Нейросеть проверила {st['checked']} кадров-кандидатов.")

    # ---------- 4. раскладка: каждая строка кадров звучит со своим предложением ----------
    shots = []          # {kind, src, s, e (сэмплы), mood, first_of_seg, ...}
    overlays = []       # надписи поверх видео: {png, start (кадр), frames, anim}
    label_gap = float(cfg.get("label_min_gap_sec", 45))
    last_label = -1e9
    busy_until = [0.0]          # до какой секунды на экране уже есть подпись
    use_count = {}
    last_item = [None]

    def first_video(items):
        return next((it for it in items if it["kind"] == "video"), items[0] if items else None)

    def add_overlay(png, start_sec, dur_sec, anim):
        overlays.append({"png": str(png), "start": int(round(start_sec * render.FPS)),
                         "frames": max(1, int(round(dur_sec * render.FPS))), "anim": anim})

    zoom_flip = False
    for b in blocks:
        if b["type"] == "title":
            first = next((first_video(x["items"]) for x in seg_blocks if x["items"]), None)
            title_png = shots_dir / f"title2_{_key(script.get('title', ''), str(accent))}.png"
            eyebrow = (niche.get("eyebrow") or {}).get(code, "")
            extras.make_title_png(script.get("title", ""), eyebrow, title_png, accent)
            shots.append({"kind": first["kind"] if first else "black", "src": first["path"] if first else "",
                          "clip_dur": (first or {}).get("duration", 0), "offset_n": 3,
                          "s": b["start"], "e": b["end"], "mood": "normal", "darken": True, "role": "title"})
            add_overlay(title_png, 0.5, TITLE_SEC - 0.3, "title")
            continue
        if b["type"] == "card":
            ci = b["chapter"]
            nxt = next((x for x in seg_blocks if x["chapter"] == ci), None)
            bg = first_video((nxt or {}).get("items") or [])
            card_png = shots_dir / f"chapter2_{ci}_{_key(script['chapters'][ci]['title'], str(accent))}.png"
            extras.make_chapter_png(script["chapters"][ci]["title"], ci, card_png, accent, L["chapter"])
            if bg:
                use_count[bg["path"]] = use_count.get(bg["path"], 0) + 1
            shots.append({"kind": bg["kind"] if bg else "black", "src": bg["path"] if bg else "",
                          "clip_dur": (bg or {}).get("duration", 0), "offset_n": 2,
                          "s": b["start"], "e": b["end"], "mood": "normal", "darken": True, "role": "card"})
            add_overlay(card_png, b["start"] / SR, CARD_SEC, "fade")
            continue
        s = b["seg"]
        seg_start = b["start"] / SR
        span = (b["end"] - b["start"]) / SR
        # надпись абзаца: не чаще раза в label_gap секунд
        if (cfg.get("show_labels", True) and s.get("label") and seg_start - last_label >= label_gap
                and seg_start >= TITLE_SEC + 1):
            lp = shots_dir / f"label2_{_key(s['label'], str(accent))}.png"
            extras.make_label(s["label"], lp, accent)
            add_overlay(lp, seg_start + 0.3, min(LABEL_SEC, span - 0.4), "label")
            last_label = seg_start
            busy_until[0] = seg_start + 0.3 + min(LABEL_SEC, span - 0.4)
        first_in_seg = True
        for si, spec in enumerate(b["specs"]):
            items = list(spec.get("items") or [])
            t0, t1 = spec["t0"], spec["t1"]
            length = t1 - t0
            if spec["visual"] == "person":
                # подпись с именем человека (если рядом нет другой надписи)
                if cfg.get("show_labels", True) and seg_start + t0 + 0.1 >= busy_until[0] and length >= 1.8:
                    lp = shots_dir / f"label2_{_key(spec['shown'], str(accent))}.png"
                    extras.make_label(spec["shown"], lp, accent)
                    dur = min(LABEL_SEC - 1, length - 0.2)
                    add_overlay(lp, seg_start + t0 + 0.1, dur, "label")
                    busy_until[0] = seg_start + t0 + 0.1 + dur
            if not items:                              # строка без кадров — берём соседние
                items = next((list(x.get("items")) for x in b["specs"][si + 1:] + b["specs"][:si][::-1]
                              if x.get("items")), [])
                if not items and last_item[0]:
                    items = [last_item[0]]
            # отрезки: в паузах речи, не длиннее max_shot (если кадр один — можно до 2×max_shot)
            n = spec["n"]
            if len(items) == 1:
                n = max(1, math.ceil(length / (2 * max_shot) - 1e-6))
            elif items:
                n = max(math.ceil(length / max_shot - 1e-6), min(n, len(items)))
            pauses = [(t - t0, ln) for t, ln in b["pauses"] if t0 < t < t1]
            bounds = [x - t0 for x in b["sent_bounds"] if t0 < x < t1]
            cuts = timing.cut_points(length, n, pauses, bounds, min_shot=1.0)
            edges = [0.0] + cuts + [length]
            fixed = [0.0]
            lim = 2 * max_shot if len(items) == 1 else max_shot
            for a_, b_ in zip(edges, edges[1:]):
                parts = max(1, math.ceil((b_ - a_) / lim - 1e-6))
                fixed += [a_ + (b_ - a_) * (q + 1) / parts for q in range(parts)]
            edges = fixed
            for j in range(len(edges) - 1):
                it = items[j % len(items)] if items else None
                uses = use_count.get(it["path"], 0) if it else 0
                if it:
                    use_count[it["path"]] = uses + 1
                    last_item[0] = it
                zoom_flip = not zoom_flip
                shots.append({
                    "kind": it["kind"] if it else "black", "src": it["path"] if it else "",
                    "clip_dur": (it or {}).get("duration", 0), "offset_n": uses,
                    "s": b["start"] + int((t0 + edges[j]) * SR), "e": b["start"] + int((t0 + edges[j + 1]) * SR),
                    "mood": s.get("mood", "normal"), "first_of_seg": first_in_seg, "zoom_in": zoom_flip,
                })
                first_in_seg = False
    shots[-1]["e"] = pos
    # стыки без щелей: конец кадра = начало следующего
    for a_, b_ in zip(shots, shots[1:]):
        a_["e"] = b_["s"]
    shots = [sh for sh in shots if sh["e"] > sh["s"]]
    reused = sum(1 for v in use_count.values() if v > 1)
    log(f"  Кадров в монтаже: {len(shots)} (в среднем {total_sec / max(1, len(shots)):.1f} с), "
        f"повторно использовано роликов: {reused}.")

    # ---------- 5. переходы (только плавный наплыв), кадры, хвосты под переходы ----------
    for i, sh in enumerate(shots):
        f0 = round(sh["s"] / SR * render.FPS)
        f1 = round(sh["e"] / SR * render.FPS)
        sh["frames"] = max(1, f1 - f0)
    for i, sh in enumerate(shots):
        if i == len(shots) - 1:
            sh["trans"], sh["tail"] = "fade", 0
            continue
        nxt = shots[i + 1]
        if sh.get("role") == "title":
            sec = TR_TITLE
        elif sh.get("role") == "card" or nxt.get("role") == "card":
            sec = TR_CARD
        elif nxt.get("first_of_seg"):
            sec = TR_PARA
        else:
            sec = TR_INNER
        fr = int(round(sec * render.FPS))
        fr = max(3, min(fr, int(sh["frames"] * 0.4), int(nxt["frames"] * 0.4)))
        sh["trans"], sh["tail"] = "fade", fr
    for i, sh in enumerate(shots):
        g = _grade(niche, sh["mood"])
        if sh.get("darken"):
            g = (g + "," if g != "null" else "") + DARKEN
        sh["grade"] = g
        sh["out"] = str(shots_dir / (f"{i:04d}_" + _key(sh["kind"], sh["src"], str(sh["frames"]), str(sh["tail"]),
                                                          sh["grade"], str(sh.get("zoom_in")), str(sh.get("offset_n", 0)),
                                                          "v2") + ".mp4"))

    workers = max(2, min(6, (os.cpu_count() or 4) // 2))
    log(f"Монтаж: {len(shots)} кадров (потоков: {workers}). Кодирование: "
        f"{'видеокарта NVIDIA' if render.gpu_available() else 'процессор'}. Это самый долгий этап.")
    render.render_shots(shots, workers, log,
                        on_progress=lambda d, t: progress(0.45 + 0.45 * d / t, f"Монтаж кадров {d}/{t}"))
    check_stop()

    log("Склеиваю видеоряд, переходы и надписи...")
    video_only = project / "video_only.mp4"
    render.assemble(shots, video_only, shots_dir, log, overlays=overlays,
                    on_progress=lambda d, t: progress(0.90 + 0.07 * d / t, f"Склейка {d}/{t}"))
    progress(0.97, "Сведение звука")

    music = _pick_music(niche)
    log(f"Свожу звук{' с музыкой: ' + music.name if music else ' (музыки нет: положите mp3 в папку music)'}...")
    final = project / f"{_slug(script.get('title', 'video'))}.mp4"
    render.final_mix(video_only, narration, music, final, total_sec, float(cfg.get("music_volume", 0.12)))

    # ---------- 6. субтитры, описание, обложки ----------
    extras.write_srt(subs, project / "subtitles.srt")
    extras.write_description(script, chapter_starts, finder.credits_text(), project / "description.txt",
                             L["timecodes"], L["sources"])
    images = [it["path"] for b in seg_blocks for it in b.get("items", []) if it["kind"] == "image"]
    if len(images) < 3:            # видео в приоритете — для обложки берём кадры из роликов
        vids = [it["path"] for b in seg_blocks for it in b.get("items", []) if it["kind"] == "video"]
        for n, v in enumerate(vids[:3 - len(images)]):
            fr = _frame_from_video(v, shots_dir / f"thumb_src_{n}.jpg")
            if fr:
                images.append(fr)
    for n, img in enumerate((images[:3] or [None])):
        extras.make_thumbnail(script.get("thumbnail_text") or script.get("title", ""), img,
                              project / f"thumbnail_{n + 1}.jpg", accent)
    video_only.unlink(missing_ok=True)
    progress(1.0, "Готово")
    log(f"ГОТОВО: {final}")
    return final
