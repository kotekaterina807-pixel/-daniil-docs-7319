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
DOC_HOLD = 3.0       # лист после последнего напечатанного слова висит ровно 3 с — и сразу следующий кадр
DOC_PAUSE = 2.6      # лист в конце главы: тишина, чтобы зритель дочитал до названия следующей главы
CARD_SEC = 1.8       # название главы поверх видео
TITLE_SEC = 4.0      # заставка с названием в начале
CURTAIN_SEC = 2.2    # «занавес» после пролога: затемнение и удар, потом глава 1
PROLOG_SHOT = 1.6    # в прологе кадры короче — современный быстрый монтаж
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


def _topic_rules(script, cfg, niche=None):
    """Тема для Claude + стиль ниши. У каждой ниши свои правила кадров (niches/*.json → visual_rules);
    если их нет — жёсткое правило эпохи: видео о прошлом — только кадры, которые выглядят как то время."""
    t = script.get("topic", "") or "документальное видео"
    era = script.get("era") or "прошлое (не современность)"
    vr = (niche or {}).get("visual_rules")
    if vr:
        return f"{t}\nЭПОХА И МЕСТО: {era}.\nСТИЛЬ НИШИ (обязателен для каждого кадра): {vr}"
    if not cfg.get("old_look", True):
        return t
    return (f"{t}\nЭПОХА И МЕСТО: {era}. ЖЁСТКОЕ ПРАВИЛО ЭПОХИ: подходят только кадры, которые выглядят как то время и то "
            f"место: подлинные архивные фото темы, старые чёрно-белые фото, люди в одежде того времени, старая техника, "
            f"старые документы; природа — только дикая, без примет современности, и похожая на место действия. "
            f"ВСЕГДА ПЛОХО, даже если подходит по словам: современные люди и одежда, лыжники/сноубордисты/туристы в "
            f"современном снаряжении, горнолыжные склоны и подъёмники, современные машины, техника, здания, дороги, "
            f"телефоны, яркие современные цвета; чужие горы и места (например, острые Альпы вместо пологого Урала). "
            f"Лучше старое фото по общей теме, чем современный кадр «по словам».")


def _contiguous(shots, log=print):
    """Кадры встык: каждый начинается ровно там, где кончился предыдущий. Склейка считает время как
    сумму длительностей, поэтому любое наложение двух кадров сдвигало ВСЁ дальнейшее видео позже звука
    (к концу ролика — на 4–5 с: вспышка звучала, а фото появлялось позже, диктор «убегал» от кадров)."""
    shots = sorted(shots, key=lambda x: (x["s"], x["e"]))
    out = []
    fixed = 0
    for sh in shots:
        if not out:
            if sh["s"] != 0:
                sh["s"] = 0
            out.append(sh)
            continue
        prev = out[-1]
        if sh["s"] != prev["e"]:
            fixed += 1
            if sh["s"] < prev["e"]:                       # наложение: следующий кадр начинается позже
                if sh["e"] - prev["e"] < int(0.4 * SR):   # от него почти ничего не осталось — убрать
                    if sh.get("person") and not prev.get("person"):
                        prev["e"] = sh["s"]               # фото человека важнее — укорачиваем предыдущий
                        out.append(sh)
                    continue
                if sh.get("person") and not prev.get("person") and sh["s"] - prev["s"] > int(1.0 * SR):
                    prev["e"] = sh["s"]                   # человек появляется вовремя, предыдущий кадр короче
                else:
                    sh["s"] = prev["e"]
            else:                                         # дырка: предыдущий кадр тянется до следующего
                prev["e"] = sh["s"]
        out.append(sh)
    if fixed:
        log(f"  Тайминг: выровнено стыков кадров: {fixed} (картинка идёт точно со звуком).")
    return out


def _plain(x):
    """Обычный кадр (видео/фото), которым можно заполнить время. Листы, карты, карточки и люди — нет."""
    nm = Path(str(x.get("src") or "")).name
    return (bool(x.get("src")) and x.get("kind") in ("video", "image") and not x.get("person")
            and not nm.startswith(("doc_", "map_", "person_", "card_", "radio_", "board_"))
            and not x.get("focus")
            and not str((x.get("_qa") or {}).get("url", "")).startswith(("doc:", "map:", "card:", "radio:", "board:")))


def _fit_docs(shots):
    """Лист виден, пока печатается текст, и ещё ровно DOC_HOLD секунд. Не дольше (остаток — соседний кадр)
    и не меньше (если следующий кадр длинный — он начинается позже)."""
    shots = sorted(shots, key=lambda x: x["s"])
    out, seen = [], set()
    i = 0
    while i < len(shots):
        sh = shots[i]
        nm = Path(str(sh.get("src") or "")).name
        if not nm.startswith("doc_") or sh.get("type_end") is None:
            out.append(sh)
            i += 1
            continue
        j = i                                            # все куски этого листа подряд
        while j + 1 < len(shots) and Path(str(shots[j + 1].get("src") or "")).name == nm:
            j += 1
        s0, e0 = sh["s"], shots[j]["e"]
        if nm in seen:                                   # тот же лист второй раз (после другого кадра) — не нужен
            donor = next((x for x in reversed(out) if _plain(x)), None) or \
                    next((x for x in shots[j + 1:] if _plain(x)), None)
            if donor:
                out.append(dict(donor, s=s0, e=e0, offset_n=int(donor.get("offset_n", 0)) + 1,
                                first_of_seg=False, person=False, who=None, stack=None))
            elif out:
                out[-1]["e"] = e0
            i = j + 1
            continue
        seen.add(nm)
        want = s0 + int((sh["type_end"] + DOC_HOLD) * SR)
        whole = dict(sh, e=min(e0, want))
        out.append(whole)
        rest = e0 - whole["e"]
        nxt = shots[j + 1] if j + 1 < len(shots) else None
        if rest > int(0.4 * SR):                         # лист закончился раньше строки — остаток соседнему кадру
            if nxt is not None and _plain(nxt) and (nxt["e"] - whole["e"]) / SR <= 9.0:
                nxt["s"] = whole["e"]
            else:
                donor = next((x for x in reversed(out[:-1]) if _plain(x)), None) or \
                        next((x for x in shots[j + 1:] if _plain(x)), None)
                if donor:
                    out.append(dict(donor, s=whole["e"], e=e0, offset_n=int(donor.get("offset_n", 0)) + 1,
                                    first_of_seg=False, person=False, who=None, stack=None))
                else:
                    whole["e"] = e0
        elif want - e0 > int(0.5 * SR) and nxt is not None and _plain(nxt):
            # текст допечатался под самый конец строки — лист висит ещё до 3 с, следующий кадр позже
            add = min(want - e0, (nxt["e"] - nxt["s"]) - int(2.0 * SR))
            if add > 0:
                whole["e"] = e0 + add
                nxt["s"] = whole["e"]
        i = j + 1
    return sorted(out, key=lambda x: x["s"])


def _late_person_photo(finder, spec, memo):
    """Фото человека, которое не нашлось при подборе: ищем по имени вместе с темой ролика.
    Только файлы, где в названии есть его имя и фамилия (иначе — карточка, а не чужое лицо)."""
    who = spec.get("query") or spec.get("shown") or ""
    if who in memo:
        return memo[who]
    memo[who] = None
    try:
        alt = getattr(finder, "person_alt", {}).get(who, "") or spec.get("shown", "")
        for c in finder._person_photos(who, alt)[:4]:
            if c.get("url") in finder.used or not c.get("thumb"):
                continue
            path = finder._download(c)
            if path:
                finder.used.add(c["url"])
                finder.credits.append(c)
                memo[who] = {"path": str(path), "kind": "image", "url": c["url"], "duration": 0, "person": True}
                break
    except Exception:
        pass
    return memo[who]


SOUND_KINDS = {"мотор": "engine", "самолёт": "engine", "самолет": "engine", "engine": "engine",
               "ветер": "wind", "шторм": "wind", "буря": "wind", "wind": "wind",
               "эфир": "static", "помехи": "static", "радио": "static", "static": "static",
               "сирена": "alarm", "тревога": "alarm", "зуммер": "alarm", "alarm": "alarm",
               "гром": "thunder", "thunder": "thunder"}
BED_VOL = {"engine": 0.16, "wind": 0.14, "static": 0.04, "alarm": 0.12, "thunder": 0.22}


def _sfx_events(shots):
    """Звуки строго по своим местам:
    - вспышка — только когда на экране появляется фото (не на видео и не на листе);
    - скрип пера и «тук» меток — только пока на экране карта, ни до, ни после;
    - машинка — только пока печатаются слова на листе;
    - низкий удар — при появлении портрета героя; «свуш» — перед новой главой."""
    pick = lambda kind: (lambda f: str(f) if f else None)(_sfx_file(kind))   # каждый раз — случайный вариант
    tw, wh = pick("typewriter"), pick("whoosh")
    ev = []
    last_hit = -99.0
    last_photo = -99.0
    done_maps = set()
    done_beds = set()
    for i, sh in enumerate(shots):
        t0, t1 = sh["s"] / SR, sh["e"] / SR
        name = Path(str(sh.get("src", ""))).name
        is_photo = sh.get("kind") == "image" and not name.startswith(("doc_", "map_", "person_"))
        if is_photo and sh.get("stack") is not None and not sh.get("stack_solo") and t0 - last_photo > 3.5:
            fl = pick("drop")                             # фото ложится в стопку — мягкий тёплый «тук» в момент касания
            if fl:
                ev.append({"file": fl, "at": t0 + 0.3, "dur": 0.8, "vol": 0.2, "fade": 0.3})
                last_photo = t0
        if sh.get("person") and not name.startswith("person_") and t0 - last_hit > 20:
            h = pick("hit")                               # настоящий портрет героя — низкий удар (не чаще раза в 20 с)
            if h:
                ev.append({"file": h, "at": t0, "dur": 2.0, "vol": 0.14, "fade": 0.6})
                last_hit = t0
        if name.startswith("map_") and sh.get("map_times") and name not in done_maps:
            done_maps.add(name)                           # карта бывает разрезана на куски — звуки один раз
            j = i
            while j + 1 < len(shots) and Path(str(shots[j + 1].get("src", ""))).name == name:
                j += 1
            m_end = shots[j]["e"] / SR - 0.3              # конец карты на экране
            pn, pi = pick("trace"), pick("mark")
            mts = sh["map_times"]
            from .fx import dash_secs
            for k, mt in enumerate(mts):
                at = t0 + mt
                if at >= m_end:
                    break
                sd = dash_secs(mts[k] - mts[k - 1]) if k else 0.0   # ровно пока ложится пунктир
                if k and pn and at - sd >= t0:
                    ev.append({"file": pn, "at": at - sd, "dur": sd, "vol": 0.07, "fade": 0.3})
                if pi:                                    # точка появилась — мягкий короткий тон
                    ev.append({"file": pi, "at": at, "dur": 0.9, "vol": 0.1, "fade": 0.3})
        bed = sh.get("bed") or {}
        snd = bed.get("sfx") or []
        if snd and bed.get("id") not in done_beds:
            done_beds.add(bed.get("id"))                  # [звук] абзаца: фон на весь абзац или разовый звук
            b0, b1 = bed["s"] / SR, bed["e"] / SR
            for nm_ in snd:
                kind = SOUND_KINDS.get(nm_.split()[0])
                f_ = pick(kind) if kind else None
                if not f_:
                    continue
                if kind in ("engine", "wind", "static"):
                    ev.append({"file": f_, "at": b0, "dur": b1 - b0 + 0.6, "vol": BED_VOL[kind],
                               "fade": 0.9, "fade_in": 0.6})
                else:
                    ev.append({"file": f_, "at": b0 + (0.4 if kind == "alarm" else 0.2), "dur": 3.2 if kind == "alarm" else 5.5,
                               "vol": BED_VOL[kind], "fade": 0.5})
        if sh.get("role") == "curtain":                   # занавес после пролога — глухой удар в темноту
            h = pick("hit")
            if h:
                ev.append({"file": h, "at": t0, "dur": 2.2, "vol": 0.3, "fade": 0.9})
            continue
        if sh.get("prolog") and sh.get("first_of_seg") and wh and i and t0 > 1.0:
            ev.append({"file": wh, "at": max(0.0, t0 - 0.35), "dur": 0.9, "vol": 0.09, "fade": 0.3})
        if name.startswith("doc_") and tw and not sh.get("no_type"):
            a = sh.get("type_start") if sh.get("type_start") is not None else 0.3
            b = sh.get("type_end") if sh.get("type_end") is not None else (t1 - t0 - 0.6)
            dur = max(0.3, min(b, t1 - t0) - a)           # ровно пока печатаются слова, ни секундой дольше
            ev.append({"file": tw, "at": t0 + a, "dur": dur, "vol": 0.08, "fade": 0.08})
        elif wh and sh.get("role") == "card" and not any(e["file"] == wh and abs(e["at"] - t0) < 6 for e in ev):
            ev.append({"file": wh, "at": max(0.0, t0 - 0.55), "dur": 1.6, "vol": 0.18})   # один «свуш» на главу
    return ev[:800]


OLD_LOOK = ("gblur=sigma=0.6,"                          # чуть мягче, как старая плёнка/камера
            "eq=saturation=0.66:contrast=1.04:gamma=0.96,"
            "eq=brightness='0.006*sin(t*17)+0.004*sin(t*5.3)':eval=frame,"   # едва заметное мерцание плёнки
            "colorbalance=rs=0.05:gs=0.02:bs=-0.06:rm=0.03:bm=-0.04:rh=0.02:bh=-0.03,"
            "noise=alls=5:allf=t")                     # тонкое зерно; виньетка — одна, мягкая, в стиле ниши
LIGHT_LOOK = "noise=alls=4:allf=t"                   # карты, доска, переговоры — свои цвета, только зерно


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
    style = (script.get("style") or "").strip()
    styles = niche.get("styles") or {}
    if style and style in styles:                    # стиль внутри ниши: цвет, музыка, оформление под тему
        niche = {**niche, **styles[style], "id": niche.get("id", ""), "styles": styles}
        log(f"Стиль ролика: {styles[style].get('name', style)}.")
    elif style:
        log(f"  Стиль «{style}» в нише не найден — обычное оформление ниши.")
    cfg["say"] = script.get("say") or {}
    if cfg["say"]:
        log(f"  Словарь произношения: {len(cfg['say'])} слов.")
    use_title = script.get("title_card", niche.get("title_card", True)) is not False
    title_sec = TITLE_SEC if use_title else 0.0
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
    from . import radio as _radio
    for i, s in enumerate(segs):
        if s.get("radio"):
            s["audio"] = str(audio_dir / f"radio_{_radio.key(s['radio'], cfg)}.wav")
        else:
            s["audio"] = str(audio_dir / f"{_key('elevenlabs', voice, tts.spoken(s['text'], cfg['say']), 'p3')}.wav")
        if not Path(s["audio"]).exists() or (s.get("radio") and not Path(s["audio"]).with_suffix(".json").exists()):
            todo.append(i)
    done = [len(segs) - len(todo)]

    def speak(i):
        if stop_flag():
            return
        s = segs[i]
        if s.get("radio"):                            # переговоры: голоса «из эфира»
            _radio.make(cfg, niche, s["radio"], s["audio"], log)
        else:
            prev_t = segs[i - 1]["text"] if i and not segs[i - 1].get("radio") else ""
            next_t = segs[i + 1]["text"] if i + 1 < len(segs) and not segs[i + 1].get("radio") else ""
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
        title_len = int(title_sec * SR)
        if title_len:
            out.writeframes(b"\x00\x00" * title_len)
            blocks.append({"type": "title", "start": 0, "end": title_len})
        pos = title_len
        for ci, ch in enumerate(script["chapters"]):
            chapter_starts.append((ch["title"] or ("Prolog" if ch.get("prolog") else ""), 0.0 if ci == 0 else pos / SR))
            if ci > 0 and script["chapters"][ci - 1].get("prolog"):     # занавес после пролога
                cur = int(CURTAIN_SEC * SR)
                out.writeframes(b"\x00\x00" * cur)
                blocks.append({"type": "curtain", "start": pos, "end": pos + cur})
                pos += cur
            if ci > 0 and not ch.get("prolog"):
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
                rec = None
                if s.get("radio"):                        # переговоры: время фраз известно точно
                    s["radio_timed"] = _radio.make(cfg, niche, s["radio"], s["audio"], log)
                    dur_s = n / SR
                    sents = [(ln["a"], ln["b"], ln.get("de") or ln.get("en", "")) for ln in s["radio_timed"]] \
                        or [(0.0, dur_s, s["text"])]
                    pauses = []
                else:
                    sents, pauses, dur_s = timing.sentence_times(s["text"], s["audio"])
                if not s.get("radio") and cfg.get("word_sync", True):   # точное время каждого слова — по распознаванию речи
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
                blocks.append({"type": "seg", "seg": s, "chapter": ci, "prolog": bool(ch.get("prolog")),
                               "start": pos, "end": pos + n + pause,
                               "pauses": pauses, "sent_bounds": [b for _, b, _ in sents[:-1]],
                               "sents": sents, "words": rec})
                pos += n + pause
    total_sec = pos / SR
    log(f"Длительность видео: {total_sec / 60:.1f} мин.")
    try:
        _pron_check(project, [b for b in blocks if b["type"] == "seg"], cfg.get("say") or {}, log)
    except Exception as e:
        log(f"  Проверка произношения не выполнена: {e}")
    if synced[0]:
        log(f"  Кадры привязаны к словам диктора (распознавание речи): абзацев {synced[0]}.")

    # ---------- 3. подбор кадров: своя строка кадра — под своё предложение ----------
    log("Подбираю кадры под каждое предложение и проверяю их по смыслу...")
    vis_cache_path = project / "visuals.json"
    vis_cache = _load_json(vis_cache_path, {})
    finder = VisualFinder(cfg, niche, media_dir, log)
    fixes_path = project / "правки.json"                 # правки после просмотра: какие кадры убрать
    fixes = _load_json(fixes_path, {})
    if fixes.get("archive"):
        script["archive"] = list(dict.fromkeys((script.get("archive") or []) + fixes["archive"]))
    finder.topic_items = []
    finder.manual_photos = script.get("photos") or {}
    finder.person_hint = re.sub(r"(?i)\b(incident|vorfall|случай|инцидент)\b", "",
                                next((a for a in script.get("archive") or [] if not a.lower().startswith("http")), "")).strip()
    if script.get("archive"):
        try:
            finder.topic_items = finder.topic_archive(script["archive"])
        except Exception as e:
            log(f"  Архив темы не прочитан: {e}")
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
        if s.get("radio"):                            # переговоры — свой кадр на весь блок, подбирать нечего
            b["specs"] = [{"visual": "radio", "query": "", "t0": 0.0, "t1": span, "n": 1, "text": s["text"]}]
            continue
        sh_list = s.get("shots") or []
        focus_q = {sh_list[int(k)][1]: v for k, v in (s.get("focus") or {}).items()
                   if str(k).isdigit() and int(k) < len(sh_list)}
        specs = list(s.get("shots") or [(s["visual"], s["query"])])
        sents = b["sents"] or [(0.0, span, s["text"])]
        if cfg.get("visual_check") == "claude_code":
            # кадры строго под речь: единица — фраза (длинное предложение делится по запятым),
            # смена кадра — в паузе перед словами, строки кадров сценария — подсказки для этих фраз
            units = _speech_units(sents, b["pauses"], span, b.get("words"))
            if len(specs) == 1 and specs[0][1] in focus_q and units:
                # абзац только с [фокус] — одно фото на весь абзац: наезд, переходы между лицами, отъезд
                specs, ranges, texts = list(specs), [(units[0][0], units[-1][1])], [" ".join(u[2] for u in units)]
            else:
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
            if b.get("prolog"):                       # пролог: короткие кадры, быстрый монтаж
                n = max(1, min(4, round(length / PROLOG_SHOT)))
            spec = {"visual": vis, "query": query, "t0": t0, "t1": t1, "n": n, "text": txt}
            if vis in ("archive", "photo") and query in focus_q:
                spec["focus"] = focus_q[query]          # фото с приближениями: один кадр на всю фразу
                spec["n"] = 2                           # запасной вариант на случай брака
            if vis == "board":
                spec["board"] = query
                b["specs"].append(spec)                 # доска рисуется сама — подбирать нечего
                continue
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
                        auto=(mode == "claude_code"), topic=_topic_rules(script, cfg, niche),
                        photos=script.get("photos"))
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
    late_photos = {}            # имя → фото, найденное в последний момент (или None)
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
    board_state = {"pins": [], "photos": {}}
    rw = {"de": "Funkverkehr", "ru": "Радиообмен", "en": "Radio traffic", "fr": "Trafic radio", "es": "Tráfico de radio"}
    radio_header = rw.get(code, rw["en"]) + (" · " + (script.get("board") or {}).get("sub", "").split("·")[0].strip()
                                             if (script.get("board") or {}).get("sub") else "")
    for b in blocks:
        if b["type"] == "curtain":                    # занавес после пролога: затемнение
            shots.append({"kind": "black", "src": "", "s": b["start"], "e": b["end"], "mood": "normal",
                          "role": "curtain"})
            continue
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
                and seg_start >= title_sec + 1):
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
                    spec["items"] = [_map_item(spec, b, shots_dir, max(0.5, spec["t1"] - spec["t0"]), spec["t0"], log)]
                except Exception as e:
                    log(f"  Карта не получилась ({e}) — будет обычный кадр.")
        for spec in specs_:                        # переговоры и доска расследования — заранее, свои кадры
            if spec["visual"] == "radio" and not spec.get("items"):
                try:
                    bg_ = None
                    if b.get("prolog"):                # в хуке переговоры — «голосовое» поверх хроники, она идёт дальше
                        src_ = next((x for x in reversed(shots) if x.get("kind") == "video" and x.get("src")
                                     and not Path(str(x["src"])).name.startswith(("map_", "radio_", "board_", "doc_"))),
                                    None)
                        if src_:
                            shown_ = (src_["e"] - src_["s"]) / SR
                            need_ = spec["t1"] - spec["t0"] + 1.0
                            cd_ = float(src_.get("clip_dur") or 0)
                            bg_ = (src_["src"], shown_ if cd_ >= shown_ + need_ else max(0.0, cd_ - need_))
                    spec["items"] = [_radio_item(spec, b, shots_dir, max(0.5, spec["t1"] - spec["t0"]), radio_header,
                                                 bg_)]
                except Exception as e:
                    log(f"  Кадр переговоров не получился ({str(e)[:120]}) — будет затемнение с субтитрами.")
            if spec["visual"] == "board" and not spec.get("items"):
                try:
                    spec["items"] = [_board_item(spec, b, shots_dir, max(0.5, spec["t1"] - spec["t0"]), spec["t0"],
                                                 board_state, script, finder, log)]
                except Exception as e:
                    log(f"  Доска расследования не получилась ({str(e)[:120]}) — будет обычный кадр.")
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
            ms = PROLOG_SHOT if b.get("prolog") else min_shot      # в прологе кадры короче
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
                if not has_photo:                       # последняя попытка: поиск по имени + теме ролика
                    ph = _late_person_photo(finder, spec, late_photos)
                    if ph:
                        items.insert(0, ph)
                        has_photo = True
                        if cfg.get("show_labels", True) and length >= 1.8:
                            lp = shots_dir / f"label2_{_key(spec['shown'], str(accent))}.png"
                            extras.make_label(spec["shown"] + (" | " + spec["role"] if spec.get("role") else ""), lp, accent)
                            dur = min(LABEL_SEC - 1, length - 0.2)
                            add_overlay(lp, seg_start + t0 + 0.1, dur, "label", person=True)
                            busy_until[0] = max(busy_until[0], seg_start + t0 + 0.1 + dur)
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
                while reserve and not items:            # своего кадра нет — неиспользованный одобренный из этой сцены
                    extra = reserve.pop(0)
                    if not use_count.get(extra["path"]):
                        items = [extra]
                if not items and shots and shots[-1].get("kind") == "image" and shots[-1].get("src") \
                        and shots[-1]["e"] >= b["start"] + int(t0 * SR) - SR // 2:
                    # и его нет — предыдущее фото просто остаётся на экране дольше (одно плавное движение),
                    # а не режется на склейки одного и того же снимка
                    shots[-1]["e"] = b["start"] + int(t1 * SR)
                    continue
                if not items:
                    items = [last_item[0]] if last_item[0] else []
            # отрезки: в паузах речи, не длиннее max_shot (если кадр один — можно до 2×max_shot)
            # без повторов: отрезков столько, сколько разных кадров; один кадр повторяется
            # (другим участком) только если иначе он шёл бы дольше 1,5×max_shot
            lim = max_shot * 1.5
            long_lim = max_shot * 2.2                   # один ролик может идти дольше, лишь бы не повторяться
            n = spec["n"]
            if items and b.get("prolog"):              # хук — из видео: одобренные ролики вперёд фото
                if not any(x.get("kind") == "video" for x in items):
                    vid = next((x for x in reserve if x.get("kind") == "video" and not use_count.get(x["path"])), None)
                    if vid:
                        reserve.remove(vid)
                        items = [vid] + items
                items = sorted(items, key=lambda x: x.get("kind") != "video")
            if items:
                k = min(len(items), max(1, int(length / ms)))         # лишние кадры не берём — без мельтешения
                items = items[:k]
                while length / len(items) > lim and reserve:          # мало кадров — добираем одобренные из этой сцены
                    extra = reserve.pop(0)
                    if all(extra["path"] != x["path"] for x in items) and not use_count.get(extra["path"]):
                        items.append(extra)
                k = len(items)
                n = k if length / k <= long_lim else max(k, math.ceil(length / long_lim - 1e-6))
                if all(x.get("kind") == "image" for x in items):
                    n = k                               # одно фото не режется на склейки самого себя
                if spec.get("focus") and items[0]["kind"] == "image":   # фото с приближениями — один кадр на фразу
                    items, k, n = items[:1], 1, 1
            pauses = [(t - t0, ln) for t, ln in b["pauses"] if t0 < t < t1]
            bounds = [x - t0 for x in b["sent_bounds"] if t0 < x < t1]
            cuts = timing.cut_points(length, n, pauses, bounds, min_shot=ms)
            edges = [0.0] + cuts + [length]
            fixed = [0.0]
            stills = bool(items) and all(x.get("kind") == "image" for x in items)
            for a_, b_ in zip(edges, edges[1:]):
                parts = 1 if ((spec.get("focus") and n == 1) or stills) else max(1, math.ceil((b_ - a_) / long_lim - 1e-6))
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
                    "who": _who(dict(spec, lang=cfg.get("language", "ru")), roles) if (it or {}).get("person") else "",
                    "type_start": (it or {}).get("type_start"), "type_end": (it or {}).get("type_end"),
                    "map_times": (it or {}).get("map_times"),
                    "focus": spec.get("focus") if (it and it["kind"] == "image" and n == 1) else None,
                    "src_url": (it or {}).get("url", ""),
                    "prolog": bool(b.get("prolog")),
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
        _quality_check(cfg, project, shots, finder, _topic_rules(script, cfg, niche), log, progress)
    _apply_fixes(shots, fixes, fixes_path, finder, media_dir, log)
    if True:
        # кадр забракован, а замены нет — его время отдаём соседнему кадру (лучше дольше хороший, чем плохой)
        def stretchable(x, extra, cap=6.0):
            # карточки людей, листы с текстом и фото людей не растягиваем; и никакой кадр — дольше 8 с
            return (x.get("_qa") and not x.get("_bad") and not x.get("person")
                    and not str(x["_qa"].get("url", "")).startswith(("card:", "doc:", "map:", "radio:", "board:"))
                    and (x["e"] - x["s"] + extra) / SR <= cap)
        kept, dropped = [], 0
        for i, sh in enumerate(shots):
            if sh.get("_bad"):
                extra = sh["e"] - sh["s"]
                cap = 14.0 if sh.get("_ban") else 9.0    # убранный по правке кадр — убрать обязательно
                nxt = shots[i + 1] if i + 1 < len(shots) else None
                if kept and stretchable(kept[-1], extra, cap):
                    kept[-1]["e"] = sh["e"]
                    dropped += 1
                    continue
                if nxt is not None and stretchable(nxt, extra, cap):
                    nxt["s"] = sh["s"]
                    dropped += 1
                    continue
                if sh.get("_ban") and kept and kept[-1].get("_qa") and not kept[-1].get("_bad") \
                        and not str(kept[-1]["_qa"].get("url", "")).startswith(("card:", "radio:", "board:")):
                    kept[-1]["e"] = sh["e"]              # убранный по правке — предыдущий кадр просто дольше,
                    dropped += 1                         # без повтора того же фото новой склейкой
                    continue
                if sh.get("_ban"):                       # соседи не тянутся — повторяем соседний кадр другим участком
                    src_ = next((x for x in reversed(kept) if _plain(x) and not x.get("_bad")
                                 and x.get("kind") == "video"), None)
                    if src_:
                        sh.update({k: src_[k] for k in ("src", "kind", "clip_dur") if k in src_})
                        sh["offset_n"] = int(src_.get("offset_n", 0)) + 1
                        sh["_bad"] = sh["_ban"] = False
                        sh["person"] = False
                        sh.pop("who", None)
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
    # подписи важных мест (#! МЕСТО): когда место впервые звучит, внизу «Место | что это» — как у людей, но реже
    places = script.get("places") or []
    if places and cfg.get("show_labels", True):
        done_pl, last_pl, n_pl = set(), -1e9, 0
        gap_pl = float(cfg.get("place_label_gap_sec", 60))
        for b_ in seg_blocks:
            rec_ = b_.get("words") or []
            if not rec_ or b_.get("prolog"):
                continue
            bs_, be_ = b_["start"] / SR, b_["end"] / SR
            for pl in places:
                if pl["name"] in done_pl:
                    continue
                st_ = None
                for w0 in [w for w in pl["name"].split() if len(w) > 3][::-1] or pl["name"].split()[:1]:
                    st_ = align.word_time(rec_, w0[:max(4, len(w0) - 2)], 0.0, be_ - bs_)
                    if st_ is not None:                   # сначала последнее слово: «USS Solomons» → «Solomons»
                        break
                if st_ is None:
                    continue
                t_ = bs_ + st_
                if t_ - last_pl < gap_pl:
                    continue
                sh_ = next((x for x in shots if x["s"] / SR <= t_ + 0.5 < x["e"] / SR), None)
                nm_ = Path(str((sh_ or {}).get("src", ""))).name
                if sh_ is None or sh_.get("person") or nm_.startswith(("map_", "radio_", "board_", "person_", "doc_")):
                    continue                              # на карте место и так подписано; на лицах — подпись человека
                txt_ = pl["name"] + (" | " + pl["role"] if pl.get("role") else "")
                lp_ = shots_dir / f"label2_{_key(txt_, str(accent))}.png"
                extras.make_label(txt_, lp_, accent)
                if add_overlay(lp_, t_ + 0.2, LABEL_SEC - 1, "label"):
                    done_pl.add(pl["name"])
                    last_pl = t_
                    n_pl += 1
        log(f"  Подписи мест: {n_pl} из {len(places)}.")
    for o in overlays:
        o.pop("person", None)
    merged_ = []                                         # одно и то же фото подряд — один кадр, без склейки «в себя»
    for sh in shots:
        pv = merged_[-1] if merged_ else None
        if pv and sh.get("kind") == "image" and pv.get("kind") == "image" and sh.get("src") \
                and sh.get("src") == pv.get("src") and abs(sh["s"] - pv["e"]) <= SR // 10 \
                and not sh.get("role") and not pv.get("role"):
            pv["e"] = sh["e"]
            continue
        merged_.append(sh)
    if len(merged_) < len(shots):
        log(f"  Одинаковые фото подряд объединены: {len(shots) - len(merged_)} склеек убрано.")
        shots[:] = merged_
    for sh in shots:
        blk_ = (sh.get("_qa") or {}).get("blk") or {}
        if (blk_.get("seg") or {}).get("sfx"):         # [звук] абзаца — запомнить до того, как служебное уйдёт
            sh["bed"] = {"id": id(blk_), "s": blk_["start"], "e": blk_["end"], "sfx": list(blk_["seg"]["sfx"])}
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
            ok = (sh["kind"] == "image" and sh.get("src") and not sh.get("role") and not sh.get("focus")
                  and not Path(str(sh["src"])).name.startswith(("person_", "doc_", "map_")))
            if ok and group and len(group) < 4:
                group.append(sh)
            else:
                flush()
                if ok:
                    group.append(sh)
        flush()

    shots = _fit_docs(shots)
    # карта и лист с печатью, разрезанные на несколько кадров, идут непрерывно (а не с начала в каждом куске)
    first_s = {}
    for sh in shots:
        nm = Path(str(sh.get("src", ""))).name
        if nm.startswith(("map_", "doc_", "radio_", "board_")) and sh["kind"] == "video":
            first_s.setdefault(nm, sh["s"])
            sh["clip_start"] = round((sh["s"] - first_s[nm]) / SR, 3)
            if sh["clip_start"] > 0:
                sh["map_times"] = None                   # звуки карты — только у первого куска
                cs = sh["clip_start"]
                if sh.get("type_end") is not None:       # лист: печать во втором куске — только если ещё идёт
                    sh["type_start"] = max(0.0, (sh.get("type_start") or 0) - cs)
                    sh["type_end"] = sh["type_end"] - cs
                    sh["no_type"] = sh["type_end"] <= 0.1
    shots = _contiguous(shots, log)
    _prepare_focus(cfg, project, shots, media_dir, log)
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
        if sh.get("role") == "curtain" or nxt.get("role") == "curtain":
            sec = 0.7                                  # в занавес и из него — через темноту
        elif sh.get("prolog") and nxt.get("prolog"):
            sec = 0.22                                 # пролог: быстрые современные склейки
        elif sh.get("role") == "title":
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
        if nxt.get("stack") is not None and not nxt.get("stack_solo"):
            fr = 0                                    # фото со вспышкой появляется сразу — вспышка звучит ровно на нём
        sh["trans"], sh["tail"] = "fade", fr
        if sh.get("prolog") and nxt.get("prolog") and fr:
            sh["trans"] = ("hblur", "flash", "hblur", "fade")[i % 4]      # «смазанный» рывок и вспышка
    for i, sh in enumerate(shots):
        g = _grade(niche, sh["mood"], bool(cfg.get("old_look", True)))
        if Path(str(sh.get("src") or "")).name.startswith(("map_", "radio_", "board_")):
            g = LIGHT_LOOK                             # у карты, доски и переговоров свои цвета — только зерно
        if sh.get("darken"):
            g = (g + "," if g != "null" else "") + DARKEN
        sh["grade"] = g
        sh["out"] = str(shots_dir / (f"{i:04d}_" + _key(sh["kind"], sh["src"], str(sh["frames"]), str(sh["tail"]),
                                                          sh["grade"], str(sh.get("zoom_in")), str(sh.get("offset_n", 0)),
                                                          str(sh.get("stack")), str(sh.get("stack_solo")),
                                                          str(sh.get("focus_boxes") or ""),
                                                          *([f"cs{sh['clip_start']}"] if sh.get("clip_start") else []),
                                                          "v4") + ".mp4"))   # готовые кадры прошлых сборок подхватываются

    try:                                   # карта монтажа: время → кадр (для правок по таймкодам и проверки)
        _save_json(project / "монтаж_кадры.json", [
            {"t": round(sh["s"] / SR, 2), "end": round(sh["e"] / SR, 2), "kind": sh["kind"],
             "file": Path(str(sh.get("src") or "")).name, "url": sh.get("src_url") or (sh.get("_qa") or {}).get("url", ""),
             "who": sh.get("who", ""), "person": bool(sh.get("person"))} for sh in shots])
    except Exception:
        pass
    _free_disk(project, shots_dir, shots, log)
    _review_before_render(cfg, project, shots, fixes, fixes_path, log, check_stop, progress)
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
    # проверка звука в готовом файле: если тишина — сводим проще (без эффектов, затем без музыки)
    mean, peak = render.audio_level(final)
    log(f"  Звук в итоге: средняя громкость {mean} дБ, пик {peak} дБ." if mean is not None else "  Звук в итоге: проверить не удалось.")
    for label, kw in (("без звуковых эффектов", dict(music=music, sfx=None)), ("только голос", dict(music=None, sfx=None))):
        if mean is not None and peak is not None and peak > -45:
            break
        log(f"  Звук в итоге тихий или пропал — свожу заново ({label}).")
        render.final_mix(video_only, narration, kw["music"], final, total_sec, float(cfg.get("music_volume", 0.12)), sfx=kw["sfx"])
        mean, peak = render.audio_level(final)
        log(f"  Звук после пересведения: средняя {mean} дБ, пик {peak} дБ.")

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
                    auto=False, topic="", photos=None):
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
        board_names = [nm for blk in seg_blocks for sp_ in blk.get("specs", [])
                       if sp_.get("visual") == "board" for nm, _ in _board_people(sp_.get("board") or sp_.get("query", ""))]
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
            if cfg.get("auto_docs", False) and pl.get("source") == "лист" and spec["visual"] not in ("person", "document") \
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
                if cand and _board_only(cand, board_names, photos):
                    cand = None                       # человек с доски без проверенного фото — только силуэт на доске
                if cand:
                    spec["auto_person"] = cand
                    seg_blocks[bi]["specs"][si]["auto_person"] = cand
                    role = (pl.get("roles") or {}).get(cand[0], "") or plan_roles.get(cand[0], "")
                    if role and not (cfg.get("language", "ru") != "ru" and re.search(r"[а-яё]", role, re.I)):
                        spec["auto_role"] = seg_blocks[bi]["specs"][si]["auto_role"] = role
            if spec["visual"] != "person" and spec["n"] < 2 and spec.get("t1", 0) - spec.get("t0", 0) >= 4.6:
                spec["n"] = 2                     # минимум два разных кадра на обычную фразу
            q = spec["query"] if spec["visual"] != "person" else \
                f"фото человека: {spec['query']}, затем — {spec.get('fill_query', '')}"
            lines.append({"no": no, "sig": sig[no - 1], "text": spec["text"], "query": q, "n": spec["n"],
                          "show": pl.get("show", ""),
                          "person": spec.get("shown", "") if spec["visual"] == "person"
                          else (spec["auto_person"][1] if spec.get("auto_person") else ""),
                          "cands": [], "_spec": spec})
        # поиск по всем фразам сразу, по 6 параллельно (раньше — строго по одной: 3 часа на 500 фраз).
        # Результаты поиска запоминаются, поэтому распределение кадров ниже (без повторов) идёт мгновенно.
        todo = [ln for ln in lines if "_spec" in ln]
        done_n = [0]

        def warm(ln):
            check_stop()
            try:
                review.collect(finder, ln["_spec"], set())
            except Exception:
                pass
            done_n[0] += 1
            progress(0.10 + 0.22 * done_n[0] / max(1, len(todo)), f"Поиск кадров {done_n[0]}/{len(todo)}")
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=6) as ex_:
            list(ex_.map(warm, todo))
        keep = ("kind", "url", "thumb", "duration", "source", "page", "title", "author", "license", "person", "local", "segment")
        for ln in todo:
            cands = review.collect(finder, ln.pop("_spec"), taken)
            ln["cands"] = [{k: c.get(k) for k in keep} for c in cands]
        progress(0.35, f"Поиск кадров {len(todo)}/{len(todo)}")
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

        empty = [0]

        def one(name):
            check_stop()
            ans = claude_code.run_cached(cfg, claude_code.PROMPT.format(sheet=name, topic=topic or "документальное видео"),
                                         project, name, check=lambda a: any(claude_code.parse(a)))
            done[0] += 1
            progress(0.36 + 0.08 * done[0] / max(1, len(sheets)), f"{label}: лист {done[0]}/{len(sheets)}")
            p, q = claude_code.parse(ans)
            if not p and not q:
                empty[0] += 1
            return p, q
        picks, queries = {}, {}
        try:
            with ThreadPoolExecutor(max_workers=int(cfg.get("claude_workers", 4))) as ex:
                for p, q in ex.map(one, sheets):
                    picks.update(p)
                    queries.update(q)
        except claude_code.ClaudeCodeError as e:
            raise RuntimeError(str(e))
        if sheets and empty[0] > max(1, len(sheets) // 10):
            raise RuntimeError(f"Claude не ответил по {empty[0]} листам из {len(sheets)} — кадры не проверены, "
                               f"монтаж не начинаю. Повторите сборку позже: уже полученные ответы сохранены.")
        return picks, queries

    log(f"Claude Code смотрит кадры ({len(lines)} строк сценария)...")
    picks, queries = ask(lines, "кадры_лист", "Claude проверяет кадры")
    for no, ks in picks.items():
        if no in by_no:
            choices[no] = [k for k in ks if 1 <= k <= len(by_no[no]["cands"])]
    for no, ln_ in by_no.items():                 # фото по прямой ссылке / портрет из сценария — берём сразу,
        if ln_["cands"] and ln_["cands"][0].get("source") in ("manual", "указано в сценарии") \
                and 1 not in choices.get(no, []):     # иначе второй круг заменял их чужими кадрами
            choices[no] = [1] + [k for k in choices.get(no, []) if k != 1]
    found = [ln for ln in lines if ln.get("person") and any(c.get("person") for c in ln["cands"])]
    missing = [ln["person"] for ln in lines if ln.get("person") and ln not in found]
    if missing:
        log("  Фото не найдено в свободных источниках: " + ", ".join(dict.fromkeys(missing)) +
            " — вместо фото будет карточка с именем и кадры по его теме.")
    for who, notes in (getattr(finder, "person_diag", {}) or {}).items():
        log(f"    Поиск фото «{who}»: " + "; ".join(notes[:6]))
    # до двух дополнительных кругов поиска; у длинного видео — один (иначе упирается в лимит подписки Claude)
    for rnd in ((1,) if len(by_no) > 120 else (1, 2)):
        # у длинного видео — только строки, где нет НИ ОДНОГО кадра (где один есть, он просто дольше на экране)
        need_ = (lambda no: not choices.get(no)) if len(by_no) > 120 else \
                (lambda no: len(choices.get(no, [])) < by_no[no]["n"])
        retry = [by_no[no] for no in by_no if need_(no) and queries.get(no)]
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
    for no, ln_ in by_no.items():                 # фото по прямой ссылке из сценария — берём всегда
        if ln_["cands"] and ln_["cands"][0].get("source") in ("manual", "указано в сценарии") \
                and 1 not in choices.get(no, []):
            choices[no] = [1] + [k for k in choices.get(no, []) if k != 1]
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
    if spec.get("lang", "ru") != "ru":               # видео не на русском — подпись без кириллицы
        if re.search(r"[а-яё]", name, re.I):
            name = spec.get("query", "") if spec.get("visual") == "person" else spec["auto_person"][0]
        if re.search(r"[а-яё]", role or "", re.I):
            role = ""
    return name + (" | " + role if role else "")


def _doc_ok(seg_blocks, bi, si, doc_used):
    """Лист с текстом: не больше 2 на главу (вместе с листами из сценария), не в соседних абзацах,
    фраза 5–24 слова и 2,5–10 с."""
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
    if len(same_ch) >= 2:
        return False
    if any(abs(x[0] - bi) <= 1 for x in doc_used):
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
    if mp and units:                               # карта маршрута — до последнего названного места, не дольше
        rest = [(v, q) for v, q in script_specs if v != "map"]
        k = len(units) - 1
        if rest and len(units) > 1:
            places = [re.sub(r"\s+-?\d+(?:[.,]\d+)?\s+-?\d+(?:[.,]\d+)?$", "", x.strip())
                      for x in mp.split("|")[1:]]
            st_ = [_stems(p_)[:1] for p_ in places if p_]
            hit = [i for i, (_, _, t) in enumerate(units)
                   if any(s_ and any(align.norm(w).startswith(s_[0]) for w in t.split()) for s_ in st_)]
            k = min(len(units) - 2, max(hit + [0]))
            npts = len([x for x in mp.split("|")[1:] if x.strip()])
            need = 0.4 + 2.3 * max(0, npts - 1) + 1.2      # путь успевает полностью лечь, и на конце — пауза
            while k < len(units) - 1 and units[k][1] - units[0][0] < need:
                k += 1
        head = [("map", mp)], [(units[0][0], units[k][1])], [" ".join(u[2] for u in units[:k + 1])]
        if k >= len(units) - 1:
            return head
        s2, r2, t2 = _assign_specs(rest, units[k + 1:])
        return head[0] + s2, head[1] + r2, head[2] + t2
    bd = next((q for v, q in script_specs if v == "board"), None)
    if bd and units:                               # доска расследования — до последнего названного на ней имени
        rest = [(v, q) for v, q in script_specs if v != "board"]
        k = len(units) - 1
        if rest and len(units) > 1:
            names = [_stems(nm)[-1:] for nm, _ in _board_people(bd)]
            hit = [i for i, (_, _, t) in enumerate(units)
                   if any(st and any(align.norm(w).startswith(st[0]) for w in t.split()) for st in names)]
            k = min(len(units) - 2, max(hit + [0]))
            while k < len(units) - 1 and units[k][1] - units[0][0] < 6.5:
                k += 1                                 # нить, фото и пауза на лице — не меньше ~6 с
        head = [("board", bd)], [(units[0][0], units[k][1])], [" ".join(u[2] for u in units[:k + 1])]
        if k >= len(units) - 1:
            return head
        s2, r2, t2 = _assign_specs(rest, units[k + 1:])
        return head[0] + s2, head[1] + r2, head[2] + t2
    n = len(units)
    vis = [["stock", ""] for _ in range(n)]
    persons = {}
    others = [(v, q) for v, q in script_specs if v not in ("person", "document")]
    for j, (v, q) in enumerate(others):            # подсказки по порядку
        u = min(n - 1, j * n // max(1, len(others)))
        if not vis[u][1] or v == "archive":
            vis[u] = [v if v in ("archive", "photo", "stock", "newsreel") else "stock", q]
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


def _tsec(t):
    """«4:31» / «1:02:05» / 271 → секунды."""
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(x) for x in str(t).strip().split(":")]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


_SHOT_RE = re.compile(r"^\d{4}_[0-9a-f]+\.mp4$")


def _gb_free(path):
    import shutil
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return 999.0


def _free_disk(project, shots_dir, shots, log):
    """Перед монтажом: удалить смонтированные куски прошлых сборок, которые этой сборке не нужны
    (каждая правка оставляла свой набор — десятки гигабайт). Если места всё равно мало — почистить
    куски у готовых старых проектов. Исходники, озвучка, листы, карты и готовые видео не трогаются."""
    keep = {Path(sh["out"]).name for sh in shots if sh.get("out")}
    freed = 0
    for f in shots_dir.glob("*.mp4"):
        if _SHOT_RE.match(f.name) and f.name not in keep:
            try:
                freed += f.stat().st_size
                f.unlink()
            except OSError:
                pass
    if _gb_free(project) < 15:
        for other in Path(project).parent.iterdir():
            if other == Path(project) or not (other / "shots").is_dir():
                continue
            if not any(other.glob("*.mp4")):          # у проекта нет готового видео — не трогаем
                continue
            for f in (other / "shots").glob("*.mp4"):
                if _SHOT_RE.match(f.name):
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                    except OSError:
                        pass
    free = _gb_free(project)
    if freed:
        log(f"  Освобождено на диске: {freed / 1e9:.1f} ГБ (куски прошлых сборок). Свободно: {free:.0f} ГБ.")
    if free < 6:
        raise RuntimeError(f"На диске свободно только {free:.1f} ГБ — для монтажа нужно хотя бы 6–8 ГБ. "
                           f"Освободите место (корзина, загрузки, старые видео в папке output) и запустите снова.")


def _review_before_render(cfg, project, shots, fixes, fixes_path, log, check_stop, progress):
    """Раскадровка — Claude в чат, ждём «можно монтировать» или список кадров на замену."""
    from . import checkpoint, telegram
    wait_min = int(cfg.get("review_wait_min", 120) or 0)
    if not cfg.get("claude_review", True) or wait_min <= 0 or not all(checkpoint._ok(cfg)):
        return
    progress(0.45, "Раскадровка для проверки Claude")
    try:
        sheets, index = checkpoint.storyboard(shots, project, SR)
        base = checkpoint.publish(cfg, project, sheets, index, list(cfg.get("_log_tail", lambda: [])()))
    except Exception as e:
        log(f"  Раскадровка для Claude не отправилась ({e}) — монтирую без проверки.")
        return
    log(f"Кадры подобраны. Раскадровка ({len(sheets)} листов) отправлена Claude — жду проверки "
        f"до {wait_min} мин. Напишите Claude в чат: «проверь кадры».")
    telegram.notify(cfg, f"Кадры для «{project.name[:60]}» подобраны, монтаж ещё не начат.\n"
                         f"Напиши Claude в чат: «проверь кадры». Жду до {wait_min} мин, потом монтирую сам.")
    progress(0.45, "Жду проверки кадров Claude")
    cmd = checkpoint.wait(cfg, project, check_stop, wait_min)
    if cmd is None:
        log("  Проверки Claude не было — монтирую как есть.")
        telegram.notify(cfg, f"Проверки кадров не было за {wait_min} мин — начинаю монтаж как есть.")
        return
    times = [str(t) for t in cmd.get("times") or []]
    if not times and not cmd.get("archive"):
        log("  Claude проверил кадры: можно монтировать." + (f" {cmd.get('note')}" if cmd.get("note") else ""))
        telegram.notify(cfg, "Claude проверил кадры — начинаю монтаж.")
        return
    fx = _load_json(fixes_path, {})
    fx["times"] = list(dict.fromkeys((fx.get("times") or []) + times))
    if cmd.get("archive"):
        fx["archive"] = list(dict.fromkeys((fx.get("archive") or []) + list(cmd["archive"])))
    _save_json(fixes_path, fx)
    log(f"  Claude нашёл кадры на замену: {', '.join(times) or 'новые разделы архива'} — подбираю заново "
        f"(озвучка и проверенные кадры остаются).")
    telegram.notify(cfg, f"Claude отметил кадры на замену ({len(times)} шт.) — меняю и пришлю раскадровку снова.")
    raise checkpoint.Restart()


def _apply_fixes(shots, fixes, fixes_path, finder, media_dir, log):
    """Правки после просмотра: кадры на указанных таймкодах убираются (и не вернутся при следующих сборках).
    Замена — подлинное фото из архива темы, ближе всего по смыслу к фразе; если архива нет — время забирают соседние.
    Сначала применяются прошлые правки, и только потом новые таймкоды ищутся в уже исправленном монтаже:
    иначе повторная правка попадает в старый кадр, а не в тот, что Claude видел на раскадровке."""
    if not fixes:
        return
    ban_urls = set(fixes.get("urls") or [])
    ban_files = set(fixes.get("files") or [])
    pool = []
    n_rep = n_cut = 0

    def words(t):
        return {align.norm(w) for w in re.split(r"[^\w]+", str(t or "")) if len(w) > 3}

    def replace_banned():
        nonlocal pool, n_rep, n_cut
        finder.used |= ban_urls
        pool = [c for c in getattr(finder, "topic_items", []) if c["url"] not in finder.used]
        for sh in shots:
            url = (sh.get("_qa") or {}).get("url", "")
            if not sh.get("src") or sh.get("_ban") or not (url in ban_urls or Path(str(sh["src"])).name in ban_files):
                continue
            q = sh.get("_qa") or {}
            sp = q.get("spec") or {}
            want = words(sp.get("text")) | words(sp.get("query")) | words(" ".join(sp.get("queries") or []))
            pool.sort(key=lambda c: -len(want & words(f"{c.get('title', '')} {c.get('page', '')}")))
            new = None
            while pool and not new:
                c = pool.pop(0)
                if finder._claim(c):
                    pth = finder._download(c)
                    if pth:
                        new = (c, pth)
            if new:
                c, pth = new
                sh.update({"src": str(pth), "kind": "image", "person": False, "offset_n": 0})
                sh.pop("who", None)
                sh.setdefault("_qa", {})["url"] = c["url"]
                finder.credits.append(c)
                n_rep += 1
            else:
                sh["_bad"] = sh["_ban"] = True
                sh["person"] = False                 # чужой портрет убран — подпись с именем тоже не нужна
                sh.pop("who", None)
                n_cut += 1

    replace_banned()                                # прошлые правки
    times = fixes.get("times") or []
    resolved = []
    for t in times:
        sec = _tsec(t)
        sh = next((x for x in shots if x["s"] <= sec * SR < x["e"] and not x.get("_ban")), None)
        if not sh or sh["kind"] not in ("video", "image") or not sh.get("src"):
            log(f"  Правка {t}: там карта, доска или карточка — пропускаю.")
            continue
        url = (sh.get("_qa") or {}).get("url", "")
        if str(url).startswith(("card:", "map:", "radio:", "board:")):
            log(f"  Правка {t}: там карта, доска или карточка — пропускаю.")
            continue
        if url:
            ban_urls.add(url)
        ban_files.add(Path(sh["src"]).name)
        resolved.append(t)
    if times:
        fixes["urls"], fixes["files"] = sorted(ban_urls), sorted(ban_files)
        fixes["done"] = list(fixes.get("done") or []) + resolved
        fixes["times"] = []
        _save_json(fixes_path, fixes)
        replace_banned()                            # новые правки
    if n_rep or n_cut:
        log(f"  Правки: заменено кадров {n_rep} (архив темы), убрано {n_cut}.")


def _quality_check(cfg, project, shots, finder, topic, log, progress):
    """Каждый кадр — с точными словами, которые звучат в этот момент; Claude Code меняет неподходящие."""
    from . import qa
    used = {sh["_qa"]["url"] for sh in shots if sh.get("_qa") and sh["_qa"].get("url")}
    # подлинные фото темы (архив) и портреты из сценария Claude уже выбрал по смыслу и эпохе — второй раз не смотрим
    trusted = {c.get("url") for c in getattr(finder, "topic_items", None) or []}
    trusted |= set((getattr(finder, "manual_photos", None) or {}).values())
    trusted |= set(getattr(finder, "direct_urls", None) or ())
    entries = []
    skipped = 0
    for sh in shots:
        q = sh.get("_qa")
        if not q or sh["kind"] not in ("video", "image") or not sh.get("src"):
            continue
        if str(q.get("url", "")).startswith(("doc:", "card:", "map:", "radio:", "board:")):
            continue                                   # листы, карты, доска, переговоры — сделаны программой
        if q.get("url") in trusted or sh.get("person") or \
                str((q.get("spec") or {}).get("query", "")).startswith("http"):
            skipped += 1                               # портреты и фото по прямой ссылке из сценария — не трогаем
            continue
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
    if skipped:
        log(f"  Подлинные фото темы и портреты ({skipped} кадров) уже выбраны Claude — повторно не проверяю.")
    try:
        rep = qa.check(cfg, project, entries, finder, topic, log,
                       lambda f, msg: progress(0.42 + 0.03 * f, msg), taken=used)
        bad = set(rep.get("bad_left") or [])
        for e in entries:
            if e["no"] in bad:
                e["shot"]["_bad"] = True
    except Exception as e:
        raise RuntimeError(f"Контроль качества не выполнен ({e}) — монтаж непроверенных кадров не начинаю. "
                           f"Повторите сборку: уже проверенные листы сохранены.")
    if entries and rep.get("answered", 0) < 0.9 * len(entries):
        raise RuntimeError(f"Контроль качества: Claude ответил только по {rep.get('answered', 0)} кадрам из "
                           f"{len(entries)} — монтаж непроверенных кадров не начинаю. Повторите сборку позже.")


def _pron_check(project, seg_blocks, say, log):
    """Слышит ли распознавание речи то, что написано в сценарии. Слова, которые диктор, похоже, произнёс
    неправильно (или распознавание их не узнало), — в «произношение.txt» с таймкодом и тем, что услышано.
    Имена и иностранные слова распознавание часто не знает — такие строки нужно проверить на слух."""
    import difflib
    rows, checked = [], 0
    for b in seg_blocks:
        s = b["seg"]
        rec = b.get("words")
        if s.get("radio") or not rec:
            continue
        checked += 1
        said = tts.spoken(s["text"], say)
        orig = s["text"].split()
        spk = said.split()
        exp = [align.norm(w) for w in spk]
        got = [align.norm(w) for _, _, w in rec]
        sm = difflib.SequenceMatcher(a=exp, b=got, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            words = [w for w in spk[i1:i2] if len(align.norm(w)) >= 4 and not align.norm(w).isdigit()]
            if not words:
                continue
            heard = " ".join(w for _, _, w in rec[j1:j2]) or "—"
            t = (b["start"] / SR) + (rec[min(j1, len(rec) - 1)][0] if rec else 0)
            near = " ".join(spk[max(0, i1 - 3):i2 + 3])
            rows.append((t, " ".join(words), heard, near))
    rows = [r for r in rows if r[1].lower() != r[2].lower()]
    f = Path(project) / "произношение.txt"
    lines = [f"Проверено абзацев: {checked}. Подозрительных мест: {len(rows)}.",
             "Формат: время | в сценарии | услышано распознаванием | контекст", ""]
    for t, w, h, near in rows:
        lines.append(f"{int(t // 60)}:{int(t % 60):02d} | {w} | {h} | …{near}…")
    f.write_text("\n".join(lines), encoding="utf-8")
    if checked:
        log(f"  Проверка произношения: {len(rows)} подозрительных мест (список — «произношение.txt»).")


def _prepare_focus(cfg, project, shots, media_dir, log):
    """Фото с приближениями: Claude находит на фото цели из строки [фокус] (люди, предметы), программа берёт
    оригинал в высоком разрешении. Не нашлось — обычный плавный наезд."""
    todo = [sh for sh in shots if sh.get("focus") and sh.get("kind") == "image" and sh.get("src")]
    if not todo:
        return
    from . import claude_code
    import requests
    cache_f = Path(project) / "фокус.json"
    cache = _load_json(cache_f, {})
    work = Path(project) / "_фокус"
    work.mkdir(exist_ok=True)
    for sh in todo:
        url = str(sh.get("src_url") or (sh.get("_qa") or {}).get("url") or "")
        src = sh["src"]
        if "/thumb/" in url and "upload.wikimedia.org" in url:      # оригинал Commons вместо уменьшенной копии
            orig = url.replace("/thumb/", "/").rsplit("/", 1)[0]
            dst = Path(media_dir) / f"orig_{_key(orig)}{Path(orig).suffix or '.jpg'}"
            if not dst.exists():
                try:
                    r = requests.get(orig, timeout=60, headers={"User-Agent": "DocuStudio (documentary)"}, stream=True)
                    if r.ok and int(r.headers.get("content-length") or 0) < 40_000_000:
                        dst.write_bytes(r.content)
                except Exception:
                    pass
            if dst.exists() and dst.stat().st_size > 20000:
                src = str(dst)
        ck = _key(src, json.dumps(sh["focus"], ensure_ascii=False), "f1")
        boxes = cache.get(ck)
        if boxes is None and cfg.get("visual_check") == "claude_code" and claude_code.find_exe():
            try:
                boxes = _ask_focus(cfg, work, src, sh["focus"], ck)
            except Exception as e:
                log(f"  Приближение: Claude не нашёл цели ({str(e)[:80]}).")
                boxes = []
            cache[ck] = boxes
            _save_json(cache_f, cache)
        boxes = [b for b in (boxes or []) if b]
        if boxes:
            sh["focus_boxes"] = boxes
            sh["focus_src"] = src
    n = sum(1 for sh in todo if sh.get("focus_boxes"))
    log(f"  Фото с приближениями: {n} из {len(todo)}.")


def _ask_focus(cfg, work, src, targets, ck):
    from PIL import Image, ImageDraw
    from . import claude_code
    im = Image.open(src).convert("RGB")
    im.thumbnail((1600, 1600))
    d = ImageDraw.Draw(im)
    f = extras._font(18, extras.SEMI_FONTS)
    for k in range(1, 10):                       # сетка 10% — Claude точнее называет координаты
        x, y = im.width * k / 10, im.height * k / 10
        d.line([(x, 0), (x, im.height)], fill=(255, 230, 0), width=1)
        d.line([(0, y), (im.width, y)], fill=(255, 230, 0), width=1)
        d.text((x + 3, 3), f"{k * 10}", font=f, fill=(255, 230, 0))
        d.text((3, y + 3), f"{k * 10}", font=f, fill=(255, 230, 0))
    name = f"focus_{ck}.jpg"
    im.save(Path(work) / name, quality=88)
    tl = "\n".join(f"{i + 1}) {t}" for i, t in enumerate(targets))
    prompt = (f"Открой файл {name} (фото с сеткой: жёлтые линии через каждые 10% ширины и высоты, подписаны). "
              f"Найди на фото по порядку:\n{tl}\n"
              "Для каждой цели дай рамку в процентах от ширины и высоты фото: [x0, y0, x1, y1] (левый верх и правый низ). "
              "Рамка — плотно вокруг цели (для человека — голова и плечи). Если цели на фото нет — null. "
              "Ответ — только JSON-массив без пояснений, например: [[12,30,25,58], null, [60,20,80,70]]")
    raw = claude_code.run_cached(cfg, prompt, work, name, timeout=300)
    m = re.search(r"\[\s*(?:\[|null).*\]", raw or "", re.S)
    arr = json.loads(m.group(0)) if m else []
    out = []
    for b in arr[:len(targets)]:
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
            x0, y0, x1, y1 = [max(0.0, min(100.0, float(v))) / 100 for v in b]
            if x1 - x0 > 0.01 and y1 - y0 > 0.01:
                out.append([x0, y0, x1, y1])
    return out


def _map_item(spec, b, shots_dir, length, t0, log=print):
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
    for k_, (name, _, _) in enumerate(pts):
        if k_ == 0:                                # начало маршрута — сразу, а не когда его назовут
            times.append(0.4)                      # (иначе «...и обратно в Fort Lauderdale» в конце абзаца
            continue                               #  сдвигало весь путь на последние секунды)
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
    gap = min(2.3, max(1.0, (length - 1.2 - times[0]) / max(1, len(times) - 1)))
    for i in range(1, len(times)):                     # по порядку; пунктир успевает дойти до места
        times[i] = max(times[i], times[i - 1] + gap)
    if times[-1] > length - 1.0 and times[-1] > times[0]:   # весь путь должен лечь до смены кадра
        a_ = times[0]
        sc_ = max(0.2, (length - 1.0 - a_)) / (times[-1] - a_)
        times = [a_ + (t_ - a_) * sc_ for t_ in times]
    clip_len = length + 1.5
    out = shots_dir / f"map_{_key(spec['map'], f'{clip_len:.2f}', str([round(t, 2) for t in times]), 'geo1')}.mp4"
    if not out.exists():
        from . import fx
        made = None
        try:                                       # географическая карта: рельеф, море, суша
            made = fx.make_geo_map_clip(title, pts, times, clip_len, out, seed=spec["map"], log=log)
        except Exception as e:
            log(f"  Карта с рельефом не получилась ({str(e)[:100]}) — старая бумажная карта.")
            Path(out).unlink(missing_ok=True)
        if not made:
            extras.make_map_clip(title, pts, times, clip_len, out, seed=spec["map"])
    return {"path": str(out), "kind": "video", "url": "map:" + title[:40], "duration": clip_len + 0.5,
            "map_times": times}


def _radio_item(spec, b, shots_dir, length, header, bg=None):
    """Кадр переговоров: осциллограф по настоящему звуку, кто говорит, фраза и перевод.
    bg=(ролик, начало) — в хуке: хроника на фоне и «голосовое сообщение» сбоку."""
    from . import fx
    s = b["seg"]
    lines = s.get("radio_timed") or []
    clip_len = length + 0.6
    if bg:
        out = shots_dir / f"radio_{_key(json.dumps(lines, ensure_ascii=False), f'{clip_len:.2f}', str(bg), 'vm1')}.mp4"
        if not out.exists():
            fx.make_radio_overlay_clip(lines, s["audio"], clip_len, out, bg[0], bg[1],
                                       label=header.split("·")[0].strip() if header else "FUNKSPRUCH")
        return {"path": str(out), "kind": "video", "url": "radio:" + out.stem, "duration": clip_len + 0.5}
    out = shots_dir / f"radio_{_key(json.dumps(lines, ensure_ascii=False), f'{clip_len:.2f}', header, 'r1')}.mp4"
    if not out.exists():
        fx.make_radio_clip(lines, s["audio"], clip_len, out, header=header, seed=s.get("text", ""))
    return {"path": str(out), "kind": "video", "url": "radio:" + out.stem, "duration": clip_len + 0.5}


def _board_only(pp, names, photos):
    """Человек есть на доске расследования, но проверенного фото (#! ФОТО) для него нет."""
    last = (_stems(pp[0])[-1:] or _stems(pp[1])[-1:] or [""])[0]
    if not last or not any((_stems(nm)[-1:] or [""])[0] == last for nm in names):
        return False
    return not any((_stems(k)[-1:] or [""])[0] == last for k in (photos or {}))


def _board_people(q):
    out = []
    for part in q.split(";"):
        nm, _, role = part.partition("=")
        if nm.strip():
            out.append((nm.strip(), role.strip()))
    return out


def _board_item(spec, b, shots_dir, length, t0, state, script, finder, log):
    """Доска расследования: люди из строки [доска] добавляются к уже висящим; камера подъезжает к новым
    в момент, когда диктор называет имя."""
    from . import fx
    people = _board_people(spec["board"])
    new = []
    for nm, role in people:
        if nm not in [p["name"] for p in state["pins"]]:
            photo = state["photos"].get(nm)
            if photo is None:
                # только проверенное фото из сценария (#! ФОТО): поиск по имени может найти однофамильца —
                # чужое лицо на доске хуже честного силуэта со знаком вопроса
                last = align.norm(nm.split()[-1])
                url = next((u for k, u in (script.get("photos") or {}).items()
                            if k == nm or align.norm(k.split()[-1]) == last), "")
                photo = ""
                if url:
                    try:
                        pth = finder._download({"kind": "image", "url": url, "thumb": url, "source": "manual",
                                                "page": url, "title": nm, "author": "", "license": ""})
                        photo = str(pth) if pth else ""
                    except Exception as e:
                        log(f"  Фото для доски ({nm}) не скачалось: {str(e)[:80]}")
                state["photos"][nm] = photo
            state["pins"].append({"name": nm, "role": role, "photo": photo or None})
            new.append(nm)
    rec = b.get("words") or []
    times = []
    for nm in new:
        last = nm.split()[-1]
        st = align.word_time(rec, align.norm(last)[:max(4, len(align.norm(last)) - 2)], t0 - 0.2, t0 + length) if rec else None
        times.append(None if st is None else st - t0)
    if times and all(t is not None for t in times):
        for i in range(1, len(times)):
            times[i] = max(times[i], times[i - 1] + 1.4)
    else:
        times = None
    bd = script.get("board") or {}
    title, sub = bd.get("title") or script.get("title", "")[:28], bd.get("sub", "")
    clip_len = length + 0.8
    pins = [dict(p) for p in state["pins"]]
    key = _key(json.dumps(pins, ensure_ascii=False), str(new), f"{clip_len:.2f}", str(times), title, sub, "b1")
    out = shots_dir / f"board_{key}.mp4"
    if not out.exists():
        fx.make_board_clip(title, sub, pins, new, clip_len, out, seed=title, times=times,
                           focus_names=[nm for nm, _ in people])
    return {"path": str(out), "kind": "video", "url": "board:" + out.stem, "duration": clip_len + 0.5}


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
    hold = DOC_HOLD
    clip_len = length + hold
    out = shots_dir / f"doc_{_key(text, spec.get('doc_head', ''), f'{clip_len:.2f}', str(reveal[:3]), 'h2')}.mp4"
    if not out.exists():
        extras.make_document_clip(text, spec.get("doc_head", ""), reveal, clip_len, out, seed=text)
    return {"path": str(out), "kind": "video", "url": "doc:" + text[:60], "duration": clip_len + 0.5, "hold": hold,
            "type_start": max(0.0, reveal[0]), "type_end": reveal[-1] + 0.22}   # машинка звучит только пока печатает
