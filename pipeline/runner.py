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

from . import align, extras, render, script_format, script_gen, timing, tts
from .config import MUSIC_DIR, OUTPUT_DIR, ensure_dirs, ffmpeg_exe, lang
from . import library
from .visuals import VisualFinder

PAUSE_SEC = 0.45     # пауза между абзацами (0.8 звучало как допрос — слишком рвано)
DOC_PAUSE = 2.6      # лист в конце главы: тишина, чтобы зритель дочитал до названия следующей главы
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
    from .config import load_config
    script = script_format.apply_text({"title": "", "language": load_config().get("language", "ru"), "tags": [],
                                       "description": ""}, text)   # без строки «ЯЗЫК» — язык выбранного голоса
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


def _sfx_file(kind):
    from .config import SFX_DIR
    from . import sfx as _sfx
    _sfx.ensure(SFX_DIR)                            # своих звуков нет — программа делает свои
    files = [p for p in SFX_DIR.iterdir() if p.name.lower().startswith(kind) and
             p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac")]
    return random.choice(files) if files else None


def _sfx_events(shots):
    """Звуковые акценты: вспышка и шлепок фото в стопке, машинка, пока печатается лист (и шорох листа),
    скрип пера и «тук» точек на карте, низкий удар при появлении человека, «свуш» перед новой главой."""
    pick = lambda kind: (lambda f: str(f) if f else None)(_sfx_file(kind))   # каждый раз — случайный вариант
    tw, wh = pick("typewriter"), pick("whoosh")
    ev = []
    last_hit = -99.0
    for sh in shots:
        t0, t1 = sh["s"] / SR, sh["e"] / SR
        name = Path(str(sh.get("src", ""))).name
        if sh.get("stack") is not None:                  # фото ложится в стопку — затвор, вспышка, шлепок
            fl, pd = pick("flash"), pick("paper")
            if fl and not sh.get("stack_solo"):
                ev.append({"file": fl, "at": max(0.0, t0 - 0.02), "dur": 0.9, "vol": 0.28, "fade": 0.2})
            if pd:
                ev.append({"file": pd, "at": t0 + 0.3, "dur": 0.6, "vol": 0.22, "fade": 0.1})
        if sh.get("person") and t0 - last_hit > 20:      # лицо героя — низкий удар (не чаще раза в 20 с)
            h = pick("hit")
            if h:
                ev.append({"file": h, "at": max(0.0, t0 - 0.05), "dur": 2.0, "vol": 0.2, "fade": 0.6})
                last_hit = t0
        if name.startswith("map_") and sh.get("map_times"):
            pn, pi = pick("pen"), pick("pin")
            for k, mt in enumerate(sh["map_times"]):
                if k and pn:
                    ev.append({"file": pn, "at": t0 + max(0.0, mt - 1.1), "dur": 1.1, "vol": 0.16, "fade": 0.1})
                if pi:
                    ev.append({"file": pi, "at": t0 + mt, "dur": 0.35, "vol": 0.3, "fade": 0.05})
        if name.startswith("doc_"):
            a = sh.get("type_start") if sh.get("type_start") is not None else 0.3
            b = sh.get("type_end") if sh.get("type_end") is not None else (t1 - t0 - 0.6)
            rs = pick("rustle")
            if rs:
                ev.append({"file": rs, "at": t0, "dur": 0.8, "vol": 0.25, "fade": 0.2})
            if tw:
                dur = max(0.3, min(b, t1 - t0) - a)             # ровно пока печатаются слова, ни секундой дольше
                ev.append({"file": tw, "at": t0 + a, "dur": dur, "vol": 0.12, "fade": 0.08})
        elif wh and sh.get("role") == "card" and not any(e["file"] == wh and abs(e["at"] - t0) < 6 for e in ev):
            ev.append({"file": wh, "at": max(0.0, t0 - 0.55), "dur": 1.6, "vol": 0.3})   # один «свуш» на главу
    return ev[:800]


OLD_LOOK = ("gblur=sigma=0.9,"                          # чуть мягче, как старая плёнка/камера
            "eq=saturation=0.62:contrast=1.04:gamma=0.95,"
            "eq=brightness='0.012*sin(t*17)+0.008*sin(t*5.3)':eval=frame,"   # лёгкое мерцание плёнки
            "colorbalance=rs=0.06:gs=0.02:bs=-0.07:rm=0.04:bm=-0.05:rh=0.03:bh=-0.04,"
            "noise=alls=13:allf=t+u,vignette=PI/4")    # «староватая» плёнка: тёплый выцветший цвет, зерно, виньетка


def _grade(niche, mood, old_look=False):
    parts = [g for g in (niche.get("grade", ""), MOOD_GRADE.get(mood, "")) if g]
    if old_look:
        parts.append(OLD_LOOK)
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
    from .config import voice_for_lang, voice_lang
    v_ = voice_for_lang(cfg, code)                    # голос диктора — на языке видео
    if v_ and v_.get("id") != cfg.get("elevenlabs_voice_id"):
        cfg["elevenlabs_voice_id"] = v_["id"]
        log(f"Язык видео: {L['name']} — диктор «{v_.get('name', '')}» (голос этого языка).")
    else:
        cur = next((x for x in cfg.get("eleven_voices") or [] if x.get("id") == cfg.get("elevenlabs_voice_id")), None)
        if cur and voice_lang(cur) and voice_lang(cur) != code:
            log(f"  ВНИМАНИЕ: видео на языке «{L['name']}», а выбранный голос — другого языка, и голоса нужного "
                f"языка в «Моих голосах» нет. Добавьте его во вкладке «Голоса».")
        else:
            log(f"Язык видео: {L['name']}.")
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
    provider = tts.provider_of(cfg)
    voice = f"{cfg.get('elevenlabs_voice_id')}:{cfg.get('eleven_speed')}:{cfg.get('eleven_stability')}:{cfg.get('elevenlabs_model')}"
    if not str(cfg.get("elevenlabs_voice_id", "")).strip():
        raise RuntimeError("Не выбран голос диктора — выберите его на вкладке «Голоса».")
    if cfg.get("visual_check") == "claude_code":
        from . import claude_code
        if not claude_code.find_exe():
            raise RuntimeError("Кадры должен проверять Claude Code, но он не установлен. Установите его "
                               "(PowerShell: irm https://claude.ai/install.ps1 | iex, затем в терминале: claude → /login) "
                               "или выберите другой способ "
                               "проверки в «Параметры → Монтаж».")
    segs = [s for ch in script["chapters"] for s in ch["segments"]]
    log(f"Озвучка: {len(segs)} абзацев ({'Lumean' if provider == 'lumean' else 'ElevenLabs'}, {L['name']})...")
    todo = []
    for i, s in enumerate(segs):
        s["audio"] = str(audio_dir / f"{_key('elevenlabs', voice, s['text'], 'p3')}.wav")
        if not Path(s["audio"]).exists():
            todo.append(i)
    done = [len(segs) - len(todo)]

    def speak(i):
        if stop_flag():
            return
        s = segs[i]
        prev_t = segs[i - 1]["text"] if i else ""
        next_t = segs[i + 1]["text"] if i + 1 < len(segs) else ""
        tts.synthesize(cfg, niche, s["text"], s["audio"], prev_t, next_t, log)
        done[0] += 1
        progress(0.10 * done[0] / len(segs), f"Озвучка {done[0]}/{len(segs)}")
        if done[0] % 5 == 0 or done[0] == len(segs):
            log(f"  Озвучено {done[0]}/{len(segs)}")

    # заказы Lumean выполняются на их стороне — отправляем по 3 абзаца сразу
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3 if provider == "lumean" else 2) as ex:
        list(ex.map(speak, todo))
    check_stop()
    progress(0.10, f"Озвучка {len(segs)}/{len(segs)}")

    # ---------- 2. таймлайн (в сэмплах — без рассинхрона) ----------
    narration = project / "narration.wav"
    pos = 0
    subs, chapter_starts, blocks = [], [], []
    synced = [0]
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
            for si_, s in enumerate(ch["segments"]):
                with wave.open(s["audio"], "rb") as w:
                    n = w.getnframes()
                    out.writeframes(w.readframes(n))
                pause = int(PAUSE_SEC * SR)
                if si_ == len(ch["segments"]) - 1 and any(v == "document" for v, _ in s.get("shots") or []):
                    pause += int(DOC_PAUSE * SR)          # дальше название главы — листу нужно время повисеть
                out.writeframes(b"\x00\x00" * pause)
                sents, pauses, dur_s = timing.sentence_times(s["text"], s["audio"])
                rec = None
                if cfg.get("word_sync", True):            # точное время каждого слова — по распознаванию речи
                    try:
                        rec = align.words(s["audio"], cfg.get("language", "ru"), log)
                        if rec and len(sents) > 1:
                            st = align.chunk_starts([t for _, _, t in sents], rec, dur_s)
                            if st:
                                ends = st[1:] + [dur_s]
                                sents = [(a, max(a + 0.3, b), t) for a, b, (_, _, t) in zip(st, ends, sents)]
                        if rec:
                            synced[0] += 1
                    except Exception as e:
                        log(f"  Синхронизация по словам: {e}")
                        rec = None
                for a, b, t in sents:
                    subs.append({"text": t, "start": pos / SR + a, "end": pos / SR + b})
                blocks.append({"type": "seg", "seg": s, "chapter": ci, "start": pos, "end": pos + n + pause,
                               "pauses": pauses, "sent_bounds": [b for _, b, _ in sents[:-1]],
                               "sents": sents, "words": rec})
                pos += n + pause
    total_sec = pos / SR
    log(f"Длительность видео: {total_sec / 60:.1f} мин.")
    if synced[0]:
        log(f"  Кадры привязаны к словам диктора (распознавание речи): абзацев {synced[0]}.")

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
    min_shot = max(2.4, shot_sec * 0.8)          # короче кадр не бывает: частые склейки режут глаз
    seg_blocks = [b for b in blocks if b["type"] == "seg"]
    jobs = []
    for bi, b in enumerate(seg_blocks):
        s = b["seg"]
        span = (b["end"] - b["start"]) / SR
        specs = list(s.get("shots") or [(s["visual"], s["query"])])
        sents = b["sents"] or [(0.0, span, s["text"])]
        if cfg.get("visual_check") == "claude_code":
            # кадры строго под речь: единица — фраза (длинное предложение делится по запятым),
            # смена кадра — в паузе перед словами, строки кадров сценария — подсказки для этих фраз
            units = _speech_units(sents, b["pauses"], span, b.get("words"))
            specs, ranges, texts = _assign_specs(specs, units)
            if b.get("words"):                       # фото человека — ровно на его имени
                ranges = _person_on_name(specs, ranges, b["words"])
            k = len(specs)
        else:
            k = len(specs)
            ranges, texts = None, None
        if ranges is None:
            ranges, texts = [], []
        for i in range(k if not texts else 0):
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
            n = min(n, max(1, int(length / min_shot)))
            if cfg.get("visual_check") == "claude_code":
                n = 3 if length >= 8.0 else 2 if length >= 4.6 else 1   # фраза ≈ кадр; от 4 с — два, от 7 с — три
            spec = {"visual": vis, "query": query, "t0": t0, "t1": t1, "n": n, "text": txt}
            if vis == "person":
                parts = [x.strip() for x in query.split("|")]
                spec["query"] = parts[0]
                spec["shown"] = parts[1] if len(parts) > 1 and parts[1] else parts[0]
                # если фото человека нет — кадры по теме этого человека (3-е поле строки), иначе по соседней строке
                other = next((q for v, q in specs[i + 1:] + specs[:i][::-1] if v != "person"), "dark atmosphere")
                spec["fill_query"] = parts[2] if len(parts) > 2 and parts[2] else other
                spec["role"] = parts[3] if len(parts) > 3 else ""
            if vis == "document":
                parts = [x.strip() for x in query.split("|")]
                spec["doc_text"], spec["doc_head"] = parts[0], (parts[1] if len(parts) > 1 else "")
                b["specs"].append(spec)                   # лист с текстом делается сам — подбирать нечего
                continue
            if vis == "map":
                spec["map"] = query
                b["specs"].append(spec)                   # карта рисуется сама — подбирать нечего
                continue
            b["specs"].append(spec)
            jobs.append((bi, len(b["specs"]) - 1))

    finder.person_alt = {sp["query"]: sp.get("shown", "") for b in seg_blocks for sp in b["specs"]
                         if sp["visual"] == "person"}
    mode = cfg.get("visual_check", "claude")
    if mode == "auto":
        mode = "claude"
    if mode == "claude":
        if str(cfg.get("anthropic_key", "")).strip() or str(cfg.get("lumean_key", "")).strip():
            cfg["llm_provider"] = "claude"            # проверка кадров и запасные запросы — через Claude
            cfg["strict_visuals"] = True
            log("  Кадры проверяет Claude.")
        else:
            mode = "chat"
            log("  Нет ключа Lumean или Claude — кадры выберете через листы в чат.")
    if mode == "none":
        cfg["strict_visuals"] = False
    if mode == "claude_code":
        log("  Кадры проверяет Claude Code (по подписке Claude).")
    if mode in ("chat", "claude_code"):
        _choose_in_chat(cfg, project, finder, seg_blocks, jobs, vis_cache, log, check_stop, progress,
                        auto=(mode == "claude_code"), topic=script.get("topic", ""))
    else:
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
    _calm_videos(project, seg_blocks, log)
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
    person_cards = {}           # сколько карточек уже сделано для каждого человека (каждый раз новый вид)
    use_count = {}
    last_item = [None]

    def first_video(items):
        return next((it for it in items if it["kind"] == "video"), items[0] if items else None)

    roles = {}
    for b_ in seg_blocks:                         # кто есть кто: имя → роль (из строк [человек] сценария)
        for x in b_["specs"]:
            if x.get("visual") == "person" and x.get("role"):
                roles[x.get("shown", "")] = roles[x.get("query", "")] = x["role"]

    def add_overlay(png, start_sec, dur_sec, anim, person=False):
        """Надписи никогда не накладываются: если место занято, надпись ждёт (до 3 с), иначе пропускается."""
        if anim in ("label", "title"):
            busy = [(o["start"] / render.FPS, (o["start"] + o["frames"]) / render.FPS)
                    for o in overlays if o["anim"] in ("label", "title", "fade")]
            want = start_sec
            for a, b in sorted(busy):
                if start_sec < b + 0.4 and start_sec + dur_sec > a - 0.4:
                    start_sec = b + 0.4
            if start_sec - want > 3.0:
                return False
        overlays.append({"png": str(png), "start": int(round(start_sec * render.FPS)),
                         "frames": max(1, int(round(dur_sec * render.FPS))), "anim": anim, "person": person})
        return True

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
        # строки без кадров не показывают чужие ролики повторно: их время забирает соседняя строка
        # (предыдущая в абзаце, иначе следующая), её кадры просто идут дольше
        specs_ = b["specs"]
        for spec in specs_:                        # карта маршрута — заранее, у неё свой кадр
            if spec["visual"] == "map" and not spec.get("items"):
                try:
                    spec["items"] = [_map_item(spec, b, shots_dir, max(0.5, spec["t1"] - spec["t0"]), spec["t0"])]
                except Exception as e:
                    log(f"  Карта не получилась ({e}) — будет обычный кадр.")
        for spec in specs_:                        # листы с текстом — заранее, у них свои кадры
            if spec["visual"] == "document" and not spec.get("items"):
                try:
                    spec["items"] = [_document_item(spec, b, shots_dir, max(0.5, spec["t1"] - spec["t0"]), spec["t0"])]
                except Exception as e:
                    log(f"  Лист с текстом не получился ({e}) — будет обычный кадр.")
        for si, spec in enumerate(specs_):
            if spec.get("items"):
                continue
            prev = next((x for x in specs_[:si][::-1] if x.get("items")), None)
            nxt = next((x for x in specs_[si + 1:] if x.get("items")), None)
            host = prev or nxt
            if host:
                host["t0"], host["t1"] = min(host["t0"], spec["t0"]), max(host["t1"], spec["t1"])
                spec["_merged"] = True
        reserve = [it for x in specs_ for it in (x.get("items") or [])[max(1, int((x["t1"] - x["t0"]) / min_shot)):]]
        for si, spec in enumerate(b["specs"]):
            items = list(spec.get("items") or [])
            t0, t1 = spec["t0"], spec["t1"]
            length = t1 - t0
            if spec.get("auto_person") and any(it.get("person") for it in items) and cfg.get("show_labels", True) \
                    and length >= 1.8 and seg_start + t0 + 0.1 >= busy_until[0]:
                nm = spec["auto_person"][1]               # фото названного во фразе человека — подпись с именем
                lp = shots_dir / f"label2_{_key(nm, str(accent))}.png"
                extras.make_label(nm, lp, accent)
                dur = min(LABEL_SEC - 1, length - 0.2)
                add_overlay(lp, seg_start + t0 + 0.1, dur, "label", person=True)
                busy_until[0] = seg_start + t0 + 0.1 + dur
            if spec["visual"] == "person":
                has_photo = any(it.get("person") for it in items)
                if not has_photo:
                    # фото в свободных источниках нет — карточка с инициалами и именем, первым кадром
                    var = person_cards[spec["shown"]] = person_cards.get(spec["shown"], -1) + 1
                    cp = shots_dir / f"person_{_key(spec['shown'], spec.get('role', ''), str(accent), str(var))}.jpg"
                    if not cp.exists():
                        extras.make_person_card(spec["shown"], cp, accent, spec.get("role", ""), var)
                    items.insert(0, {"path": str(cp), "kind": "image", "url": "card:" + spec["shown"],
                                     "duration": 0, "person": True})
                elif cfg.get("show_labels", True) and length >= 1.8:
                    # подпись с именем у фото человека — всегда, она важнее прочих надписей
                    lp = shots_dir / f"label2_{_key(spec['shown'], str(accent))}.png"
                    extras.make_label(spec["shown"] + (" | " + spec["role"] if spec.get("role") else ""), lp, accent)
                    dur = min(LABEL_SEC - 1, length - 0.2)
                    add_overlay(lp, seg_start + t0 + 0.1, dur, "label", person=True)
                    busy_until[0] = max(busy_until[0], seg_start + t0 + 0.1 + dur)
            if not items:
                if spec.get("_merged"):                 # время этой строки уже отдано соседней
                    continue
                items = [last_item[0]] if last_item[0] else []
            # отрезки: в паузах речи, не длиннее max_shot (если кадр один — можно до 2×max_shot)
            # без повторов: отрезков столько, сколько разных кадров; один кадр повторяется
            # (другим участком) только если иначе он шёл бы дольше 1,5×max_shot
            lim = max_shot * 1.5
            long_lim = max_shot * 2.2                   # один ролик может идти дольше, лишь бы не повторяться
            n = spec["n"]
            if items:
                k = min(len(items), max(1, int(length / min_shot)))   # лишние кадры не берём — без мельтешения
                items = items[:k]
                while length / len(items) > lim and reserve:          # мало кадров — добираем одобренные из этой сцены
                    extra = reserve.pop(0)
                    if all(extra["path"] != x["path"] for x in items) and not use_count.get(extra["path"]):
                        items.append(extra)
                k = len(items)
                n = k if length / k <= long_lim else max(k, math.ceil(length / long_lim - 1e-6))
            pauses = [(t - t0, ln) for t, ln in b["pauses"] if t0 < t < t1]
            bounds = [x - t0 for x in b["sent_bounds"] if t0 < x < t1]
            cuts = timing.cut_points(length, n, pauses, bounds, min_shot=min_shot)
            edges = [0.0] + cuts + [length]
            fixed = [0.0]
            for a_, b_ in zip(edges, edges[1:]):
                parts = max(1, math.ceil((b_ - a_) / long_lim - 1e-6))
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
                    "clip_dur": (it or {}).get("duration", 0), "offset_n": uses, "speed": (it or {}).get("speed", 1.0),
                    "s": b["start"] + int((t0 + edges[j]) * SR), "e": b["start"] + int((t0 + edges[j + 1]) * SR),
                    "mood": s.get("mood", "normal"), "first_of_seg": first_in_seg, "zoom_in": zoom_flip,
                    "person": bool((it or {}).get("person")), "hold": float((it or {}).get("hold") or 0),
                    "who": _who(spec, roles) if (it or {}).get("person") else "",
                    "type_start": (it or {}).get("type_start"), "type_end": (it or {}).get("type_end"),
                    "map_times": (it or {}).get("map_times"),
                    "_qa": {"blk": b, "spec": spec, "a": t0 + edges[j], "b": t0 + edges[j + 1],
                            "url": (it or {}).get("url", "")},
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

    if cfg.get("visual_check") == "claude_code" and cfg.get("qa_check", True):
        _quality_check(cfg, project, shots, finder, script.get("topic", ""), log, progress)
        # кадр забракован, а замены нет — его время отдаём соседнему кадру (лучше дольше хороший, чем плохой)
        def stretchable(x, extra):
            # карточки людей, листы с текстом и фото людей не растягиваем; и никакой кадр — дольше 8 с
            return (x.get("_qa") and not x.get("_bad") and not x.get("person")
                    and not str(x["_qa"].get("url", "")).startswith(("card:", "doc:"))
                    and (x["e"] - x["s"] + extra) / SR <= 6.0)
        kept, dropped = [], 0
        for i, sh in enumerate(shots):
            if sh.get("_bad"):
                extra = sh["e"] - sh["s"]
                nxt = shots[i + 1] if i + 1 < len(shots) else None
                if kept and stretchable(kept[-1], extra):
                    kept[-1]["e"] = sh["e"]
                    dropped += 1
                    continue
                if nxt is not None and stretchable(nxt, extra):
                    nxt["s"] = sh["s"]
                    dropped += 1
                    continue
            kept.append(sh)
        shots = kept
        if dropped:
            log(f"  Убрано плохих кадров без замены: {dropped} — их время заняли соседние кадры.")
    # фото/карточка человека — 3–5 секунд: успеть увидеть лицо и прочитать имя, но не висеть 15 секунд
    for i, sh in enumerate(shots):
        if not sh.get("person") or (sh["e"] - sh["s"]) / SR <= 5.0:
            continue
        cut = sh["s"] + int(4.5 * SR)
        nxt = shots[i + 1] if i + 1 < len(shots) else None
        if nxt is not None and not nxt.get("person") and (nxt["e"] - cut) / SR <= 9.0:
            nxt["s"] = cut                                   # следующий кадр начинается раньше
            sh["e"] = cut
        else:                                                # иначе после фото — другой кадр этой же сцены
            blk = (sh.get("_qa") or {}).get("blk")
            pool = [x for x in shots if x is not sh and not x.get("person") and x.get("src")
                    and x["kind"] in ("video", "image") and (x.get("_qa") or {}).get("blk") is blk
                    and not str((x.get("_qa") or {}).get("url", "")).startswith(("card:", "doc:"))]
            if not pool:
                pool = [x for x in shots if not x.get("person") and x.get("src") and x["kind"] in ("video", "image")
                        and not str((x.get("_qa") or {}).get("url", "")).startswith(("card:", "doc:"))]
            if pool:
                src = min(pool, key=lambda x: abs(x["s"] - sh["s"]))
                extra = dict(src, s=cut, e=sh["e"], offset_n=int(src.get("offset_n", 0)) + 1, first_of_seg=False,
                             person=False)
                extra["_qa"] = dict(src.get("_qa") or {})
                sh["e"] = cut
                shots.append(extra)
    shots.sort(key=lambda x: x["s"])
    # фото человека — не меньше 4,5 с: зритель успевает рассмотреть лицо и прочитать «имя | кто он».
    # Время берём у следующих кадров (короткие уступают целиком); лист, карточку главы и другого человека не трогаем
    i = 0
    while i < len(shots):
        sh = shots[i]
        dur = (sh["e"] - sh["s"]) / SR
        if sh.get("person") and dur < 4.5 and i + 1 < len(shots):
            want = sh["s"] + int(4.5 * SR)
            j = i + 1
            while j < len(shots) and shots[j]["s"] < want:
                nx = shots[j]
                if nx.get("person") or nx.get("role") in ("card", "title") or nx.get("hold"):
                    want = nx["s"]
                    break
                if (nx["e"] - want) / SR >= 1.5:
                    nx["s"] = want
                    break
                want = max(want, nx["e"])
                shots.pop(j)
            sh["e"] = max(sh["e"], want)
        i += 1
    # лист с цитатой не исчезает сразу после последнего слова: держим его ещё 2–4 с, следующий кадр короче
    i = 0
    while i < len(shots):
        sh = shots[i]
        if sh.get("hold") and i + 1 < len(shots):
            want = sh["e"] + int(sh["hold"] * SR)              # лист висит, пока зритель дочитывает
            j = i + 1
            while j < len(shots) and shots[j]["s"] < want:
                nx = shots[j]
                if nx.get("person") or nx.get("role") in ("card", "title"):
                    want = nx["s"]                             # фото человека и карточку главы не трогаем
                    break
                if (nx["e"] - want) / SR >= 1.5:
                    nx["s"] = want                             # следующий кадр начинается позже
                    break
                want = max(want, nx["e"])                      # короткий кадр целиком уступает время листу
                shots.pop(j)
            sh["e"] = max(sh["e"], want)
        i += 1
    # подпись «Имя Фамилия | кто он» — на КАЖДОМ фото человека, ровно пока фото на экране
    # (тот же человек снова через несколько секунд — подпись не повторяем)
    overlays[:] = [o for o in overlays if not o.get("person")]
    last_label = {}
    if cfg.get("show_labels", True):
        for sh in shots:
            who = sh.get("who") or ""
            if not sh.get("person") or not who or str(sh.get("src", "")).find("person_") >= 0:
                continue                                   # у карточки имя и так крупно написано
            a, b_ = sh["s"] / SR + 0.15, sh["e"] / SR - 0.15
            if b_ - a < 1.0:
                continue
            prev = next((o for o in reversed(overlays) if o.get("person") == who), None)
            if prev and abs(prev["start"] + prev["frames"] - a * render.FPS) < 0.5 * render.FPS:
                prev["frames"] = max(1, int(round(b_ * render.FPS)) - prev["start"])   # тот же человек дальше — подпись не мигает
                continue
            # подпись человека важнее остальных: другие надписи на это время убираем
            s_, e_ = a * render.FPS, b_ * render.FPS
            overlays[:] = [o for o in overlays if o["anim"] != "label" or o.get("person")
                           or o["start"] + o["frames"] < s_ - 12 or o["start"] > e_ + 12]
            lp = shots_dir / f"label3_{_key(who, str(accent))}.png"
            if not lp.exists():
                extras.make_label(who, lp, accent)
            # на КАЖДОЕ появление фото — подпись полностью выезжает и держится, пока фото на экране
            overlays.append({"png": str(lp), "start": int(round(a * render.FPS)),
                             "frames": max(1, int(round((b_ - a) * render.FPS))), "anim": "label", "person": who})
            last_label[who] = a
        n_ps = sum(1 for sh in shots if sh.get("person"))
        log(f"  Фото людей в монтаже: {n_ps}, подписей «имя | кто он»: {len(last_label) and sum(1 for o in overlays if o.get('person'))}.")
    for o in overlays:
        o.pop("person", None)
    for sh in shots:
        sh.pop("_qa", None)

    # ---------- 4б. «стопка фото»: подряд идущие фото ложатся друг на друга (как в документальных фильмах) ----------
    if cfg.get("photo_stack", True):
        group = []

        def flush():
            if group:                      # любое фото — отпечатком под углом; одиночное ложится без вспышки
                solo = len(group) == 1 and not group[0].get("person")
                for k, x in enumerate(group):
                    x["stack"] = [(y["src"], y["src"] + str(y["s"])) for y in group[max(0, k - 3):k]]
                    x["stack_seed"] = x["src"] + str(x["s"])
                    x["stack_last"] = k == len(group) - 1
                    x["stack_solo"] = solo
            group.clear()
        for sh in shots:
            ok = (sh["kind"] == "image" and sh.get("src") and not sh.get("role")
                  and not Path(str(sh["src"])).name.startswith(("person_", "doc_", "map_")))
            if ok and group and len(group) < 4:
                group.append(sh)
            else:
                flush()
                if ok:
                    group.append(sh)
        flush()

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
        if sh.get("stack") is not None and nxt.get("stack") and not sh.get("stack_last"):
            fr = 0                                    # в стопке новое фото ложится сверху — без наплыва
        sh["trans"], sh["tail"] = "fade", fr
    for i, sh in enumerate(shots):
        g = _grade(niche, sh["mood"], bool(cfg.get("old_look", True)))
        if sh.get("darken"):
            g = (g + "," if g != "null" else "") + DARKEN
        sh["grade"] = g
        sh["out"] = str(shots_dir / (f"{i:04d}_" + _key(sh["kind"], sh["src"], str(sh["frames"]), str(sh["tail"]),
                                                          sh["grade"], str(sh.get("zoom_in")), str(sh.get("offset_n", 0)),
                                                          str(sh.get("stack")), str(sh.get("stack_solo")), "v4") + ".mp4"))

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

    from . import music as _music
    try:
        music, mdesc = _music.build_bed(script, chapter_starts, total_sec, niche.get("id", ""), project / "music_bed.wav", log)
    except Exception as e:
        log(f"  Музыка по главам не получилась ({e}) — один трек.")
        music, mdesc = _pick_music(niche), ""
    log(f"Свожу звук{' с музыкой: ' + (mdesc or music.name) if music else ' (музыки нет: положите mp3 в папку music)'}...")
    final = project / f"{_slug(script.get('title', 'video'))}.mp4"
    sfx = _sfx_events(shots)
    if sfx:
        log(f"  Звуки: {len(sfx)} (вспышки фото, машинка и шорох листов, перо на карте, удар на лицах, переходы к главам).")
    render.final_mix(video_only, narration, music, final, total_sec, float(cfg.get("music_volume", 0.12)), sfx=sfx)

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


def _choose_in_chat(cfg, project, finder, seg_blocks, jobs, vis_cache, log, check_stop, progress,
                    auto=False, topic=""):
    """Кадры выбирает Claude в чате по листам с превью (см. pipeline/review.py)."""
    from . import review
    rev_path, ch_path = project / "review.json", project / "выбор_кадров.json"
    sig = [[bi, si, seg_blocks[bi]["specs"][si]["visual"], seg_blocks[bi]["specs"][si]["query"],
            seg_blocks[bi]["specs"][si]["n"]] for bi, si in jobs]
    lines = review.load_review(rev_path)
    if not lines or [ln.get("sig") for ln in lines] != sig:
        lines = None
        ch_path.unlink(missing_ok=True)
    if lines is None:
        log("Ищу кандидатов под каждую строку сценария (проверять будет " + ("Claude Code" if auto else "Claude в чате") + ")...")
        taken = {u for v in vis_cache.values() for it in v.get("items", []) for u in [it.get("url", "")]}
        plan = {}
        if auto:                                  # режиссёрский план: что должно быть в кадре + точные запросы
            from . import claude_code
            claude_code.LOG = log
            plan_path = project / "план_кадров.json"
            try:
                cached = json.loads(plan_path.read_text(encoding="utf-8"))
                if cached.get("sig") == sig:
                    plan = {int(k): v for k, v in cached["plan"].items()}
            except Exception:
                pass
            if not plan:
                log("Claude Code составляет план кадров: что должно быть в кадре на каждой фразе...")
                progress(0.09, "План кадров")
                plan = claude_code.plan(cfg, [{"no": no, "text": seg_blocks[bi]["specs"][si]["text"]}
                                              for no, (bi, si) in enumerate(jobs, 1)], topic, project, log)
                if plan:
                    _save_json(plan_path, {"sig": sig, "plan": {str(k): v for k, v in plan.items()}})
                    log(f"  План готов: {len(plan)} фраз.")
        heroes = []                                # герои истории — люди из строк [человек] сценария
        for b_ in seg_blocks:
            for sp_ in b_["specs"]:
                if sp_["visual"] == "person":
                    heroes.append((sp_["query"], sp_.get("shown", "")))
        plan_roles = {}                            # «кто он» для подписи — из плана режиссёра, на языке видео
        for it_ in plan.values():
            for en_, r_ in (it_.get("roles") or {}).items():
                plan_roles.setdefault(en_, r_)
        lines = []
        doc_used = []
        named = {}
        pp_key = lambda pp: (_stems(pp[0])[-1:] or [""])[0]
        for no, (bi, si) in enumerate(jobs, 1):
            check_stop()
            spec = dict(seg_blocks[bi]["specs"][si])
            pl = plan.get(no) or {}
            if pl.get("queries"):
                spec["queries"] = pl["queries"]
                if spec["visual"] == "person":
                    spec["fill_query"] = pl["queries"][0]
            if pl.get("source") == "лист" and spec["visual"] not in ("person", "document") \
                    and not spec.get("auto_person") and _doc_ok(seg_blocks, bi, si, doc_used):
                sp0 = seg_blocks[bi]["specs"][si]          # ключевая фраза — лист, где печатаются слова диктора
                sp0["visual"], sp0["doc_text"] = "document", sp0["text"].strip()
                head = re.sub(r"^(лист|документ)\s*[:—-]\s*", "", pl.get("show", ""), flags=re.I)[:60]
                if cfg.get("language", "ru") not in ("ru",) and re.search(r"[а-яё]", head, re.I):
                    head = ""                         # заголовок не на языке видео — лучше без него
                sp0["doc_head"] = head
                doc_used.append((bi, si))
                lines.append({"no": no, "sig": sig[no - 1], "text": spec["text"], "query": "лист с текстом", "n": 1,
                              "show": "лист", "person": "", "cands": [], "doc": True, "doc_head": sp0["doc_head"]})
                continue
            if pl.get("source") == "архив" and spec["visual"] == "stock":
                spec["visual"] = "archive"            # старые фото, гравюры, картины той эпохи
            if spec["visual"] == "person" and pl.get("people"):
                yr = next((pp[2] for pp in pl["people"] if len(pp) > 2 and _is_hero(pp, [[spec["query"], spec.get("shown", "")]])), "")
                if yr:
                    spec["when"] = seg_blocks[bi]["specs"][si]["when"] = yr
            if spec["visual"] == "person":
                named.setdefault(seg_blocks[bi].get("chapter"), set()).add(pp_key([spec["query"], spec.get("shown", "")]))
            if pl.get("people") and spec["visual"] != "person":
                # фото человека — только героя истории (есть строка [человек] в сценарии) и только во фразе,
                # где звучит его имя: никаких фото «заранее» и случайных людей, о которых ничего не сказано
                ch_ = seg_blocks[bi].get("chapter")
                # герой из [человек] или любой другой реальный человек, чьё имя звучит в этой фразе
                cand = next((pp for pp in pl["people"] if _name_spoken(pp, spec["text"])
                             and (_is_hero(pp, heroes) or len(_stems(pp[0])) >= 2)), None)
                if cand:
                    named.setdefault(ch_, set()).add(pp_key(cand))
                else:
                    # «он бросает оркестр», «руководителя нет» — о герое, которого уже назвали в этой главе:
                    # показываем его другие фото (в форме, с оркестром…). До первого упоминания — никогда.
                    cand = next((pp for pp in pl["people"] if pp_key(pp) in named.get(ch_, set())), None)
                if cand:
                    spec["auto_person"] = cand
                    seg_blocks[bi]["specs"][si]["auto_person"] = cand
                    role = (pl.get("roles") or {}).get(cand[0], "") or plan_roles.get(cand[0], "")
                    if role and not (cfg.get("language", "ru") != "ru" and re.search(r"[а-яё]", role, re.I)):
                        spec["auto_role"] = seg_blocks[bi]["specs"][si]["auto_role"] = role
            if spec["visual"] != "person" and spec["n"] < 2 and spec.get("t1", 0) - spec.get("t0", 0) >= 4.6:
                spec["n"] = 2                     # минимум два разных кадра на обычную фразу
            cands = review.collect(finder, spec, taken)
            q = spec["query"] if spec["visual"] != "person" else \
                f"фото человека: {spec['query']}, затем — {spec.get('fill_query', '')}"
            keep = ("kind", "url", "thumb", "duration", "source", "page", "title", "author", "license", "person", "local", "segment")
            lines.append({"no": no, "sig": sig[no - 1], "text": spec["text"], "query": q, "n": spec["n"],
                          "show": pl.get("show", ""),
                          "person": spec.get("shown", "") if spec["visual"] == "person"
                          else (spec["auto_person"][1] if spec.get("auto_person") else ""),
                          "cands": [{k: c.get(k) for k in keep} for c in cands]})
            progress(0.10 + 0.25 * no / len(jobs), f"Поиск кадров {no}/{len(jobs)}")
        review.save_review(rev_path, lines)
    for (bi, si), ln in zip(jobs, lines):         # фразы-листы (по плану режиссёра) — и при повторной сборке
        if ln.get("doc"):
            sp0 = seg_blocks[bi]["specs"][si]
            sp0["visual"], sp0["doc_text"], sp0["doc_head"] = "document", sp0["text"].strip(), ln.get("doc_head", "")
    pick_lines = [ln for ln in lines if not ln.get("doc")]
    if not ch_path.exists() and auto:
        from . import claude_code as _cc
        _cc.LOG = log
        _choose_with_claude_code(cfg, project, finder, pick_lines, rev_path, ch_path, topic, log, check_stop, progress)
        review.save_review(rev_path, lines)       # вместе с фразами-листами (тот же список, кандидаты обновлены)
    if not ch_path.exists():
        sheets = review.make_sheets(pick_lines, project, log)
        raise review.NeedChoices(sheets, len(lines))
    choices = {int(k): v for k, v in json.loads(ch_path.read_text(encoding="utf-8")).items()}
    log("Скачиваю выбранные кадры...")
    picked = [(ln, [ln["cands"][k - 1] for k in choices.get(ln["no"], []) if 0 < k <= len(ln["cands"])])
              for ln in lines]
    for ln, cs in picked:                         # строка о человеке: его фото показываем всегда, первыми
        need = 2 if ln["n"] >= 2 else 1
        have = [c for c in cs if c.get("person")]
        for ph in [c for c in ln["cands"] if c.get("person") and c not in cs][:max(0, need - len(have))]:
            cs.insert(len([c for c in cs if c.get("person")]), ph)
    flat = [c for _, cs in picked for c in cs]

    def dl(c):
        return c["url"], finder._download(c)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        paths = dict(ex.map(dl, flat))
    credits = []
    for (bi, si), (ln, cs) in zip(jobs, picked):
        if ln.get("doc"):
            continue                              # лист с текстом сделается сам при монтаже
        items = []
        for c in cs:
            pth = paths.get(c["url"])
            if pth:
                items.append({"path": str(pth), "kind": c["kind"], "url": c["url"],
                              "duration": c.get("duration") or 0, "person": bool(c.get("person"))})
                credits.append(c)
        sp_ = seg_blocks[bi]["specs"][si]
        if sp_.get("visual") == "person" or sp_.get("auto_person"):
            # фразу о человеке открывает его фото — на имени, а не «где-то потом» (иначе фото могло не попасть в монтаж)
            items.sort(key=lambda it: not it.get("person"))
        seg_blocks[bi]["specs"][si]["items"] = items
        seg_blocks[bi]["specs"][si]["_cands"] = ln["cands"]      # запасные варианты — для контроля качества
    st = getattr(finder, "src_stats", {}) or {}
    names_ = {"_stock": "видео Pixabay", "_pixabay_photos": "фото Pixabay", "_wikimedia": "Wikimedia",
              "_web_images": "интернет (Bing)", "_loc": "Библ. Конгресса", "_openverse": "Openverse",
              "_ia_video": "Internet Archive", "_aic": "Чикаго", "_met": "Met"}
    if st:
        log("  Кандидаты по источникам: " + ", ".join(f"{names_.get(k, k)} {v}" for k, v in
                                                     sorted(st.items(), key=lambda x: -x[1])))
    vis_cache["chat"] = {"items": [it for b in seg_blocks for sp in b["specs"] for it in sp.get("items", [])],
                         "credits": credits}
    _save_json(project / "visuals.json", vis_cache)
    progress(0.45, "Кадры выбраны")


def _choose_with_claude_code(cfg, project, finder, lines, rev_path, ch_path, topic, log, check_stop, progress):
    """Листы с кандидатами смотрит Claude Code по подписке. Где подходящего нет — новый запрос и второй круг."""
    from concurrent.futures import ThreadPoolExecutor
    from . import claude_code, review
    keep = ("kind", "url", "thumb", "duration", "source", "page", "title", "author", "license", "person", "local", "segment")
    by_no = {ln["no"]: ln for ln in lines}
    choices = {}

    def ask(sheet_lines, prefix, label):
        sheets = review.make_sheets(sheet_lines, project, log, prefix=prefix)
        done = [0]

        def one(name):
            check_stop()
            ans = claude_code.run_cached(cfg, claude_code.PROMPT.format(sheet=name, topic=topic or "документальное видео"),
                                         project, name)
            done[0] += 1
            progress(0.36 + 0.08 * done[0] / max(1, len(sheets)), f"{label}: лист {done[0]}/{len(sheets)}")
            return claude_code.parse(ans)
        picks, queries = {}, {}
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                for p, q in ex.map(one, sheets):
                    picks.update(p)
                    queries.update(q)
        except claude_code.ClaudeCodeError as e:
            raise RuntimeError(str(e))
        return picks, queries

    log(f"Claude Code смотрит кадры ({len(lines)} строк сценария)...")
    picks, queries = ask(lines, "кадры_лист", "Claude проверяет кадры")
    for no, ks in picks.items():
        if no in by_no:
            choices[no] = [k for k in ks if 1 <= k <= len(by_no[no]["cands"])]
    found = [ln for ln in lines if ln.get("person") and any(c.get("person") for c in ln["cands"])]
    missing = [ln["person"] for ln in lines if ln.get("person") and ln not in found]
    if missing:
        log("  Фото не найдено в свободных источниках: " + ", ".join(dict.fromkeys(missing)) +
            " — вместо фото будет карточка с именем и кадры по его теме.")
    for who, notes in (getattr(finder, "person_diag", {}) or {}).items():
        log(f"    Поиск фото «{who}»: " + "; ".join(notes[:6]))
    for rnd in (1, 2):                                   # до двух дополнительных кругов поиска
        retry = [by_no[no] for no in by_no if len(choices.get(no, [])) < by_no[no]["n"] and queries.get(no)]
        if not retry:
            break
        log(f"  Круг {rnd + 1}: не хватает подходящих кадров в {len(retry)} строках — ищу по запросам Claude...")
        taken = {c["url"] for ln in lines for c in ln["cands"]}
        for ln in retry:
            check_stop()
            kept = [ln["cands"][k - 1] for k in choices.get(ln["no"], [])]
            spec = {"visual": "stock", "query": queries[ln["no"]], "n": ln["n"], "text": ln["text"]}
            new = [{k: c.get(k) for k in keep} for c in review.collect(finder, spec, taken)]
            ln["cands"] = kept + new[: max(2, review.CANDS - len(kept))]
            ln["query"] = queries[ln["no"]]
            choices[ln["no"]] = list(range(1, len(kept) + 1))
        retry = [ln for ln in retry if len(ln["cands"]) > len(choices.get(ln["no"], []))]
        if not retry:
            break
        picks2, queries = ask(retry, f"кадры_круг{rnd + 1}", f"Круг {rnd + 1}")
        for no, ks in picks2.items():
            if no in by_no:
                ks = [k for k in ks if 1 <= k <= len(by_no[no]["cands"])]
                choices[no] = list(dict.fromkeys(choices.get(no, []) + ks))
    short = sum(1 for no in by_no if not choices.get(no))
    if short:
        log(f"  Строк без своих кадров: {short} — их время займут соседние кадры той же сцены (без повторов).")
    review.save_review(rev_path, lines)
    ch_path.write_text(json.dumps({str(k): v for k, v in choices.items()}, ensure_ascii=False), encoding="utf-8")


MOTION_SLOW = 6.0      # выше — ролик проигрывается медленнее (см. render.motion_score)
MOTION_DROP = 11.0     # выше — ролик убирается, если у строки есть другие кадры


def _calm_videos(project, seg_blocks, log):
    """Быстрые и резкие ролики (таймлапсы, тряска, мелькание) режут глаз: замеряем движение,
    слишком быстрые убираем, умеренно быстрые замедляем."""
    cache_path = project / "motion.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    vids = {it["path"] for b in seg_blocks for sp in b["specs"] for it in sp.get("items", [])
            if it.get("kind") == "video" and it.get("path")}
    todo = [p for p in vids if p not in cache]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            for p, sc in zip(todo, ex.map(render.motion_score, todo)):
                cache[p] = round(sc, 2)
        _save_json(cache_path, cache)
    slowed = dropped = 0
    for b in seg_blocks:
        for sp in b["specs"]:
            items = sp.get("items") or []
            score = lambda it: cache.get(it.get("path"), 0) if it.get("kind") == "video" else 0
            keep = [it for it in items if score(it) <= MOTION_DROP]
            if not keep and items:                        # все быстрые — оставляем самый спокойный
                keep = [min(items, key=score)]
            dropped += len(items) - len(keep)
            for it in keep:
                sc = score(it)
                if sc > MOTION_SLOW:
                    it["speed"] = round(max(0.5, MOTION_SLOW / sc), 2)
                    slowed += 1
            if len(keep) != len(items):
                sp["items"] = keep
    log(f"  Проверка на быстрые кадры: роликов {len(vids)}, убрано {dropped}, замедлено {slowed}.")


def _person_on_name(specs, ranges, rec):
    ranges = [list(r) for r in ranges]
    for i, (v, q) in enumerate(specs):
        if v != "person" or i == 0:
            continue
        parts = [x.strip() for x in q.split("|")]
        shown = parts[1] if len(parts) > 1 and parts[1] else parts[0]
        stems = [w[:max(3, len(w) - 2)] for w in shown.split() if len(w) > 2]
        t = None
        for st in stems[::-1]:                       # сначала фамилия
            t = align.word_time(rec, st, ranges[i][0], ranges[i][1])
            if t is not None:
                break
        if t is not None and t - 0.25 > ranges[i][0] + 0.8 and t - 0.25 < ranges[i][1] - 1.0:
            ranges[i - 1][1] = ranges[i][0] = t - 0.25
    return [tuple(r) for r in ranges]


def _who(spec, roles):
    """Подпись к фото человека: «Имя Фамилия | кто он»."""
    if spec.get("visual") == "person":
        name = spec.get("shown") or spec.get("query", "")
        role = spec.get("role") or roles.get(name, "")
    elif spec.get("auto_person"):
        en, ru = spec["auto_person"][0], spec["auto_person"][1]
        name = ru or en
        role = roles.get(ru) or roles.get(en) or spec.get("auto_role", "")
    else:
        return ""
    return name + (" | " + role if role else "")


def _doc_ok(seg_blocks, bi, si, doc_used):
    """Лист с текстом: не больше 3 на главу, не подряд, фраза 5–24 слова и 2,5–10 с."""
    sp = seg_blocks[bi]["specs"][si]
    words = len(sp.get("text", "").split())
    length = sp.get("t1", 0) - sp.get("t0", 0)
    if not (5 <= words <= 24 and 2.5 <= length <= 10):
        return False
    ch = seg_blocks[bi].get("chapter")
    specs = seg_blocks[bi]["specs"]
    # сразу после листа — название главы или фото человека: лист некогда дочитать
    nxt_blk = seg_blocks[bi + 1] if bi + 1 < len(seg_blocks) else None
    if si == len(specs) - 1 and (nxt_blk is None or nxt_blk.get("chapter") != ch):
        return False
    nxt = specs[si + 1] if si + 1 < len(specs) else (nxt_blk["specs"][0] if nxt_blk and nxt_blk.get("specs") else {})
    if nxt.get("visual") == "person" or nxt.get("auto_person"):
        return False
    # листы из сценария тоже считаются: не больше 3 на главу и не в соседних абзацах
    script_docs = [(b_, s_) for b_ in range(len(seg_blocks)) for s_, sp_ in enumerate(seg_blocks[b_]["specs"])
                   if sp_.get("visual") == "document" and (b_, s_) not in doc_used]
    same_ch = [x for x in doc_used + script_docs if seg_blocks[x[0]].get("chapter") == ch]
    if len(same_ch) >= 3:
        return False
    if any(x[0] == bi and abs(x[1] - si) <= 1 for x in doc_used):
        return False
    if any(abs(x[0] - bi) == 1 for x in script_docs):
        return False
    have = sum(1 for s_ in seg_blocks[bi]["specs"] if s_.get("visual") == "document")
    return have == 0


def _speech_units(sents, pauses, span, rec=None):
    """Фразы озвучки: предложения, длинные (>6.5 с) — делятся по запятым/тире на части от ~2.5 с.
    Границы притягиваются к паузам речи, кадр меняется чуть раньше слов."""
    units = []
    for a, b, txt in sents:
        has_quote = bool(re.search(r"«[^»]{14,}»", txt))
        if b - a <= 3.2 and not has_quote:         # фраза длиннее ~3 с — на смысловые части: у каждой свой кадр
            units.append([a, b, txt])
            continue
        if has_quote:                              # цитата — отдельный кадр (лист с текстом) ровно на её словах
            parts = [x for x in re.split(r"\s*(?=«)|(?<=»)\s*", txt) if x.strip()]
        else:
            # части: по запятым, тире и по союзам «и / а / но / когда / чтобы / потому» — что сказано первым,
            # то и показываем первым (а не главный предмет фразы с её начала)
            parts = [x for x in re.split(r"(?<=[,;:])\s+|\s+(?=[—–]\s)|\s+(?=(?:и|а|но|когда|чтобы|потому|пока|после)\s)",
                                         txt) if x.strip()]
        merged = []
        for x in parts:                            # слишком короткие кусочки склеиваем (цитату — никогда)
            if merged and not x.startswith("«") and not merged[-1].startswith("«") \
                    and (len(merged[-1]) < 22 or len(x) < 12):
                merged[-1] += " " + x
            else:
                merged.append(x)
        if len(merged) <= 1:
            units.append([a, b, txt])
            continue
        if rec:                                    # части предложения — по распознанным словам
            sub = [(st - a, en - a, w) for st, en, w in rec if a - 0.3 <= st < b]
            starts = align.chunk_starts(merged, sub, b - a)
            if starts:
                bounds = [a + x for x in starts] + [b]
                for j, x in enumerate(merged):
                    t0_, t1_ = bounds[j], bounds[j + 1]
                    if j > 0 and t1_ - t0_ < 1.6 and not x.startswith("«") and not units[-1][2].startswith("«"):
                        units[-1][1] = t1_
                        units[-1][2] += " " + x
                    else:
                        units.append([t0_, t1_, x])
                continue
        total = sum(len(x) for x in merged)
        t, acc = a, 0
        for j, x in enumerate(merged):
            acc += len(x)
            end = b if j == len(merged) - 1 else a + (b - a) * acc / total
            if j < len(merged) - 1:               # к ближайшей паузе в пределах 0.7 с
                near = [pt for pt, ln in pauses if abs(pt - end) <= 0.7 and a + 1.5 < pt < b - 1.5]
                if near:
                    end = min(near, key=lambda pt: abs(pt - end))
            if end - t >= 1.6 or j == len(merged) - 1 or x.startswith("«") or (j + 1 < len(merged) and merged[j + 1].startswith("«")):
                units.append([t, end, x])
                t = end
            else:
                merged[j + 1] = x + " " + merged[j + 1] if j + 1 < len(merged) else x
    if units:
        units[0][0] = 0.0
        units[-1][1] = span
        for i in range(1, len(units)):            # смена кадра на 0.25 с раньше слов
            cut = max(units[i - 1][0] + 1.5, units[i][0] - 0.25)
            units[i - 1][1] = units[i][0] = cut
    return units


def _assign_specs(script_specs, units):
    """Строки кадров сценария → фразы. [человек] — к фразе, где звучит его имя; остальные — по порядку."""
    mp = next((q for v, q in script_specs if v == "map"), None)
    if mp and units:                               # карта маршрута — на весь абзац одним кадром
        return [("map", mp)], [(units[0][0], units[-1][1])], [" ".join(u[2] for u in units)]
    n = len(units)
    vis = [["stock", ""] for _ in range(n)]
    persons = {}
    others = [(v, q) for v, q in script_specs if v not in ("person", "document")]
    for j, (v, q) in enumerate(others):            # подсказки по порядку
        u = min(n - 1, j * n // max(1, len(others)))
        if not vis[u][1] or v == "archive":
            vis[u] = [v if v in ("archive", "photo", "stock") else "stock", q]
    for v, q in script_specs:
        if v != "person":
            continue
        parts = [x.strip() for x in q.split("|")]
        shown = parts[1] if len(parts) > 1 and parts[1] else parts[0]
        stems = [w[:max(3, len(w) - 2)].lower() for w in shown.split() if len(w) > 2]
        u = next((i for i, (_, _, t) in enumerate(units) if any(st in t.lower() for st in stems[-1:])), None)
        if u is None:
            u = next((i for i, (_, _, t) in enumerate(units) if any(st in t.lower() for st in stems)), None)
        if u is not None and u not in persons:
            persons[u] = q
    for i in range(n):                             # фразы без подсказки — подсказка соседней
        if not vis[i][1]:
            vis[i][1] = next((vis[j][1] for j in list(range(i - 1, -1, -1)) + list(range(i + 1, n)) if vis[j][1]),
                             "dark atmosphere")
    docs = {}
    for i, (_, _, t) in enumerate(units):          # цитата в «кавычках» из речи — лист с этим текстом
        m = re.search(r"«([^»]{14,})»", t)
        if m and len(m.group(1).split()) >= 6:                # короткая фраза на листе — 2 секунды «Потому» на экране, смысла нет
            docs[i] = m.group(1).strip()
    for v, q in script_specs:                      # [документ] из сценария — к самой похожей фразе
        if v != "document":
            continue
        body = q.split("|")[0]
        bw = {align.norm(w) for w in body.split()}
        best = max(range(n), key=lambda i: len(bw & {align.norm(w) for w in units[i][2].split()}))
        head = q.split("|")[1].strip() if "|" in q else ""
        # на листе печатается ровно то, что говорит диктор, — слово в слово и в тот же момент
        docs[best] = units[best][2].strip() + (" | " + head if head else "")
    specs = [("person", persons[i]) if i in persons else ("document", docs[i]) if i in docs
             else (vis[i][0], vis[i][1]) for i in range(n)]
    return specs, [(a, b) for a, b, _ in units], [t for _, _, t in units]


def _stems(name):
    return [align.norm(w)[:max(3, len(align.norm(w)) - 2)] for w in name.replace(",", " ").split() if len(align.norm(w)) > 2]


def _is_hero(pp, heroes):
    en, ru = pp[0], pp[1]
    for h_en, h_ru in heroes:
        if (en and h_en and _stems(en)[-1:] == _stems(h_en)[-1:]) or (ru and h_ru and _stems(ru)[-1:] == _stems(h_ru)[-1:]):
            return True
    return False


def _name_spoken(pp, text):
    words = [align.norm(w) for w in text.split()]
    stems = _stems(pp[1]) + _stems(pp[0])
    return any(w.startswith(st) for w in words for st in stems if st)


def _quality_check(cfg, project, shots, finder, topic, log, progress):
    """Каждый кадр — с точными словами, которые звучат в этот момент; Claude Code меняет неподходящие."""
    from . import qa
    used = {sh["_qa"]["url"] for sh in shots if sh.get("_qa") and sh["_qa"].get("url")}
    entries = []
    for sh in shots:
        q = sh.get("_qa")
        if not q or sh["kind"] not in ("video", "image") or not sh.get("src"):
            continue
        if str(q.get("url", "")).startswith(("doc:", "card:", "map:")):
            continue                                   # листы с текстом и карточки людей — сделаны программой
        rec = q["blk"].get("words")
        if rec:
            said = " ".join(w for st, en, w in rec if q["a"] - 0.1 <= st < q["b"])
        else:
            said = q["spec"].get("text", "")
        if not said.strip():
            said = q["spec"].get("text", "")
        alts = [c for c in q["spec"].get("_cands") or [] if c.get("url") not in used]
        alts.sort(key=lambda c: 0 if c.get("person") and q["spec"]["visual"] == "person" else 1)
        entries.append({"no": len(entries) + 1, "time": sh["s"] / SR, "said": said[:220], "shot": sh,
                        "alts": alts[:3]})
    try:
        rep = qa.check(cfg, project, entries, finder, topic, log,
                       lambda f, msg: progress(0.42 + 0.03 * f, msg), taken=used)
        bad = set(rep.get("bad_left") or [])
        for e in entries:
            if e["no"] in bad:
                e["shot"]["_bad"] = True
    except Exception as e:
        log(f"  Контроль качества не выполнен ({e}) — монтирую как есть.")


def _map_item(spec, b, shots_dir, length, t0):
    """Карта маршрута: точка — в момент, когда диктор называет место (по распознанным словам)."""
    parts = [x.strip() for x in spec["map"].split("|") if x.strip()]
    title, pts = "", []
    for x in parts:
        m = re.match(r"^(.+?)\s+(-?\d+(?:[.,]\d+)?)\s+(-?\d+(?:[.,]\d+)?)$", x)
        if m:
            pts.append((m.group(1).strip(), float(m.group(2).replace(",", ".")), float(m.group(3).replace(",", "."))))
        elif not pts:
            title = x
    if len(pts) < 2:
        raise ValueError("на карте меньше двух мест с координатами")
    rec = b.get("words") or []
    times, last = [], 0.3
    for name, _, _ in pts:
        st = align.word_time(rec, name.split()[0][:max(4, len(name.split()[0]) - 2)], t0 + last - 0.2, t0 + length) if rec else None
        if st is None:
            times.append(None)
        else:
            last = max(last, st - t0)
            times.append(max(0.3, st - t0))
    # места, которые не прозвучали, — равномерно между соседними (последнее — ближе к концу абзаца)
    known = [i for i, t in enumerate(times) if t is not None]
    for i in range(len(times)):
        if times[i] is None:
            prev = max([j for j in known if j < i], default=None)
            nxt = min([j for j in known if j > i], default=None)
            a = times[prev] if prev is not None else 0.4
            bnd = times[nxt] if nxt is not None else max(a + 1.5, length - 1.2)
            span = (nxt if nxt is not None else len(times)) - (prev if prev is not None else -1)
            times[i] = a + (bnd - a) * (i - (prev if prev is not None else -1)) / max(1, span)
    for i in range(1, len(times)):                     # по порядку и не теснее 1,2 с
        times[i] = max(times[i], times[i - 1] + 1.2)
    clip_len = length + 1.5
    out = shots_dir / f"map_{_key(spec['map'], f'{clip_len:.2f}', str([round(t, 2) for t in times]))}.mp4"
    if not out.exists():
        extras.make_map_clip(title, pts, times, clip_len, out, seed=spec["map"])
    return {"path": str(out), "kind": "video", "url": "map:" + title[:40], "duration": clip_len + 0.5,
            "map_times": times}


def _document_item(spec, b, shots_dir, length, t0):
    """Старый лист, на котором слова печатаются ровно тогда, когда их произносит диктор."""
    text = spec["doc_text"]
    words = text.split()
    reveal = None
    rec = b.get("words")
    if rec:
        # слова диктора этой фразы; каждое слово листа печатается ровно с того момента, как диктор его начал
        sub = [(st - t0, en - t0, w) for st, en, w in rec if t0 - 0.3 <= st < t0 + length + 0.3]
        reveal = align.word_starts(words, sub, length + 0.3) if sub else None
    if not reveal:
        step = max(0.15, (length - 0.6) / max(1, len(words)))
        reveal = [0.3 + i * step for i in range(len(words))]
    # после последнего слова лист ещё висит — зритель должен успеть дочитать (≈0,25 с на слово, 2–4 с)
    hold = max(2.5, min(5.0, 0.3 * len(words)))
    clip_len = length + hold
    out = shots_dir / f"doc_{_key(text, spec.get('doc_head', ''), f'{clip_len:.2f}', str(reveal[:3]))}.mp4"
    if not out.exists():
        extras.make_document_clip(text, spec.get("doc_head", ""), reveal, clip_len, out, seed=text)
    return {"path": str(out), "kind": "video", "url": "doc:" + text[:60], "duration": clip_len + 0.5, "hold": hold,
            "type_start": max(0.0, reveal[0]), "type_end": reveal[-1] + 0.22}   # машинка звучит только пока печатает
