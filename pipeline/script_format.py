"""Сценарий в виде простого текстового файла, который удобно править в Блокноте."""
import re

MOOD_TO_TEXT = {"normal": "обычный", "tense": "спокойный", "climax": "эффектный"}
TEXT_TO_MOOD = {v: k for k, v in MOOD_TO_TEXT.items()}

HEADER = """# СЦЕНАРИЙ. Можно править в Блокноте и сохранить, затем нажать «Собрать видео».
# Правила:
#   == ГЛАВА: Название ==   — начало главы
#   [музыка] спокойная / загадочная / тревожная — настроение музыки для этой главы
#   [карта] Заголовок | Место широта долгота | Место широта долгота ...  — старая карта: точка появляется,
#           когда диктор называет место, пунктир маршрута прокладывается к ней (на весь абзац)
#   Абзац текста           — то, что прочитает диктор
#   [stock] запрос         — ВИДЕО (Pixabay), запрос на английском: что должно быть видно в кадре
#   [archive] запрос       — архивное фото реального места/документа (Wikimedia), запрос на английском
#   [photo] запрос         — фото (Pixabay, затем Wikimedia) — только если видео не бывает
#   [документ] текст на листе | заголовок  — старый лист, текст печатается слово за словом под голос
#                  (цитаты в «кавычках» из речи диктора становятся такими листами автоматически)
#   [человек] Имя Фамилия | Имя по-русски | запрос, если фото нет | кто он  — фото этого человека
#                  (если фото нет в свободных источниках — карточка с инициалами, именем и «кто он»)
#                  (имя на английском, как в Википедии; третье поле — кадры по его теме, например vintage airplane);
#                            если свободного фото нет — на экране появится подпись с именем
#   Строк кадров у абзаца несколько: они идут по порядку предложений, каждая — под своё предложение.
#   [надпись] Главное | пояснение — всплывающая подпись на экране (строку можно удалить)
#   [радио] Кто | фраза в оригинале | перевод — радиопереговоры: голос «из эфира», на экране осциллограф,
#                  фраза и перевод (абзац только из строк [радио] — отдельный блок переговоров)
#   [фокус] запрос фото | цель 1 ; цель 2 ; цель 3 — фото, на котором камера по очереди приближается
#                  к целям (люди, предметы) и отъезжает. Только в самых интересных местах, 2–5 раз на ролик
#   [доска] Имя = кто он ; Имя = кто он — доска расследования: фото на пробке, красная нить,
#                  камера подъезжает к новым людям. Каждая следующая [доска] добавляет людей к прежним
#   == ПРОЛОГ ==           — холодное начало до первой главы: без названия главы, быстрый монтаж,
#                  в конце — затемнение («занавес»), потом глава 1
#   #! ПРОИЗНОШЕНИЕ: слово = как читать — подсказка диктору (на экране и в субтитрах остаётся слово)
#   #! СТИЛЬ: luft_see / expedition / krimi — стиль внутри ниши (цвет, карта, музыка)
#   #! ДОСКА: Название дела | подпись — карточка в центре доски расследования
#   #! ЗАСТАВКА: нет — без заставки с названием в начале
#   [переход] обычный / спокойный / эффектный — переход к этому абзацу
#            спокойный — для страшных и тревожных моментов, эффектный — для поворотов и разгадок
#   Абзацы разделяются пустой строкой. Строки с # в начале игнорируются.
"""


def to_text(script: dict) -> str:
    lines = [HEADER, f"#! НАЗВАНИЕ: {script.get('title', '')}",
             f"#! ОБЛОЖКА: {script.get('thumbnail_text', '')}",
             f"#! ЯЗЫК: {script.get('language', 'ru')}",
             f"#! ТЕГИ: {', '.join(script.get('tags', []))}"]
    for ln in (script.get("description", "") or "").splitlines():
        lines.append(f"#! ОПИСАНИЕ: {ln}")
    for f in script.get("facts_to_verify", []):
        lines.append(f"#! ФАКТ: {f}")
    lines.append("")
    for k_, v_ in (script.get("say") or {}).items():
        lines.append(f"#! ПРОИЗНОШЕНИЕ: {k_} = {v_}")
    if script.get("style"):
        lines.append(f"#! СТИЛЬ: {script['style']}")
    if script.get("board"):
        lines.append(f"#! ДОСКА: {script['board'].get('title', '')} | {script['board'].get('sub', '')}")
    if script.get("title_card") is False:
        lines.append("#! ЗАСТАВКА: нет")
    for ch in script["chapters"]:
        lines.append("== ПРОЛОГ ==" if ch.get("prolog") else f"== ГЛАВА: {ch['title']} ==")
        lines.append("")
        for s in ch["segments"]:
            if s.get("radio"):
                for ln in s["radio"]:
                    lines.append(f"[радио] {ln.get('who', '')} | {ln.get('en', '')} | {ln.get('de', '')}")
                lines.append(f"[переход] {MOOD_TO_TEXT.get(s.get('mood', 'normal'), 'обычный')}")
                lines.append("")
                continue
            lines.append(s["text"])
            shots = s.get("shots") or [(s["visual"], s["query"])]
            focus = s.get("focus") or {}
            for i_, (v, q) in enumerate(shots):
                if str(i_) in focus or i_ in focus:
                    tg = focus.get(str(i_)) or focus.get(i_)
                    lines.append(f"[фокус] {q} | {' ; '.join(tg)}")
                    continue
                lines.append(f"[человек] {q}" if v == "person" else f"[карта] {q}" if v == "map"
                             else f"[доска] {q}" if v == "board" else f"[{v}] {q}")
            if s.get("label"):
                lines.append(f"[надпись] {s['label']}")
            if s.get("person") and not any(v == "person" for v, _ in shots):
                lines.append(f"[человек] {s['person']}")
            if s.get("music"):
                lines.append(f"[музыка] {s['music']}")
            lines.append(f"[переход] {MOOD_TO_TEXT.get(s.get('mood', 'normal'), 'обычный')}")
            lines.append("")
    return "\n".join(lines)


def apply_text(script: dict, text: str) -> dict:
    """Читает отредактированный текст и обновляет главы/названия в сценарии."""
    chapters = []
    current = None
    block = []
    desc_lines, facts, options = [], [], []

    def flush():
        nonlocal block
        if not block:
            return
        txt, label, person, mood, music = [], "", "", "normal", ""
        shots, radio_lines, focus = [], [], {}
        for ln in block:
            m = re.match(r"^\[(archive|stock|photo|фото|видео|архив)\]\s*(.*)$", ln.strip(), re.I)
            tag = re.match(r"^\[(надпись|человек|переход|документ|музыка|карта|радио|фокус|доска)\]\s*(.*)$",
                           ln.strip(), re.I)
            if m:
                kind = {"фото": "photo", "видео": "stock", "архив": "archive"}.get(m.group(1).lower(),
                                                                               m.group(1).lower())
                shots.append((kind, m.group(2).strip() or "dark atmosphere"))
            elif tag:
                name, val = tag.group(1).lower(), tag.group(2).strip()
                if name == "надпись":
                    label = val
                elif name == "документ":
                    if val:
                        shots.append(("document", val))
                elif name == "карта":
                    if val:
                        shots.append(("map", val))
                elif name == "радио":
                    if val:
                        from .radio import parse as _rp
                        radio_lines.append(_rp(val))
                elif name == "фокус":
                    if val:
                        q_, _, tg = val.partition("|")
                        targets = [t.strip() for t in tg.split(";") if t.strip()]
                        shots.append(("archive", q_.strip() or "archive photo"))
                        if targets:
                            focus[str(len(shots) - 1)] = targets
                elif name == "доска":
                    if val:
                        shots.append(("board", val))
                elif name == "музыка":
                    music = val.lower()
                elif name == "человек":
                    if val:
                        person = person or val
                        shots.append(("person", val))
                else:
                    mood = TEXT_TO_MOOD.get(val.lower(), "normal")
            else:
                txt.append(ln.strip())
        body = " ".join(t for t in txt if t)
        nonlocal current
        if (body or radio_lines) and current is None:
            current = {"title": "Вступление", "segments": []}
            chapters.append(current)
        if body:
            if not any(v != "person" for v, _ in shots):
                shots.append(("stock", "dark atmosphere"))
            first = next((x for x in shots if x[0] != "person"), shots[0])
            seg = {"text": body, "visual": first[0], "query": first[1],
                   "label": label, "person": person, "mood": mood, "shots": shots, "music": music}
            if focus:
                seg["focus"] = focus
            current["segments"].append(seg)
        if radio_lines:                               # переговоры — свой блок (после текста абзаца, если он есть)
            current["segments"].append({"text": " ".join(r["de"] or r["en"] for r in radio_lines),
                                        "radio": radio_lines, "visual": "radio", "query": "",
                                        "shots": [("radio", "")], "label": "", "person": "",
                                        "mood": mood, "music": "" if body else music})
        block = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#! НАЗВАНИЕ:"):
            script["title"] = line.split(":", 1)[1].strip() or script.get("title", "")
            continue
        if line.startswith("#! ОБЛОЖКА:"):
            script["thumbnail_text"] = line.split(":", 1)[1].strip()
            continue
        meta = re.match(r"^#!\s*(ЯЗЫК|ТЕГИ|ОПИСАНИЕ|ФАКТ|ТЕМА|АРХИВ|ЭПОХА|ФОТО|ВАРИАНТ НАЗВАНИЯ|ПРОИЗНОШЕНИЕ|СТИЛЬ|"
                        r"ДОСКА|ЗАСТАВКА):\s?(.*)$", line)
        if meta:
            key, val = meta.group(1), meta.group(2).strip()
            if key == "ЯЗЫК" and val:
                script["language"] = val.lower()
            elif key == "ТЕГИ":
                script["tags"] = [t.strip() for t in val.split(",") if t.strip()]
            elif key == "ОПИСАНИЕ":
                desc_lines.append(val)
            elif key == "ФАКТ" and val:
                facts.append(val)
            elif key == "ТЕМА" and val:
                script["topic"] = val
            elif key == "ЭПОХА" and val:              # время и место действия: кадры только «оттуда»
                script["era"] = val
            elif key == "ФОТО" and val and "=" in val:  # «Имя Фамилия = ссылка на фото» — подлинное фото человека
                nm, url = val.split("=", 1)
                script.setdefault("photos", {})[nm.strip()] = url.strip()
            elif key == "АРХИВ" and val:              # разделы Wikimedia Commons с подлинными фото темы
                script["archive"] = [t.strip() for t in val.split(";") if t.strip()]
            elif key == "ВАРИАНТ НАЗВАНИЯ" and val:
                options.append(val)
            elif key == "ПРОИЗНОШЕНИЕ" and "=" in val:  # «слово = как его читать» — только для диктора
                w_, _, x_ = val.partition("=")
                if w_.strip() and x_.strip():
                    script.setdefault("say", {})[w_.strip()] = x_.strip()
            elif key == "СТИЛЬ" and val:
                script["style"] = val.strip()
            elif key == "ДОСКА" and val:
                t_, _, s_ = val.partition("|")
                script["board"] = {"title": t_.strip(), "sub": s_.strip()}
            elif key == "ЗАСТАВКА":
                script["title_card"] = val.strip().lower() not in ("нет", "no", "off", "0", "false")
            continue
        if line.startswith("#"):
            continue
        if re.match(r"^==\s*ПРОЛОГ\s*==\s*$", line, re.I):
            flush()
            current = {"title": "", "segments": [], "prolog": True}
            chapters.append(current)
            continue
        m = re.match(r"^==\s*ГЛАВА:\s*(.*?)\s*==\s*$", line)
        if m:
            flush()
            current = {"title": m.group(1), "segments": []}
            chapters.append(current)
            continue
        if not line.strip():
            flush()
            continue
        block.append(line)
    flush()
    chapters = [c for c in chapters if c["segments"]]
    if not chapters:
        raise ValueError("В файле сценария не найдено ни одного абзаца текста.")
    script["chapters"] = chapters
    if desc_lines:
        script["description"] = "\n".join(desc_lines).strip()
    if facts:
        script["facts_to_verify"] = facts
    if options:
        script["title_options"] = options
    return script
