"""Сценарий в виде простого текстового файла, который удобно править в Блокноте."""
import re

MOOD_TO_TEXT = {"normal": "обычный", "tense": "спокойный", "climax": "эффектный"}
TEXT_TO_MOOD = {v: k for k, v in MOOD_TO_TEXT.items()}

HEADER = """# СЦЕНАРИЙ. Можно править в Блокноте и сохранить, затем нажать «Собрать видео».
# Правила:
#   == ГЛАВА: Название ==   — начало главы
#   Абзац текста           — то, что прочитает диктор
#   [stock] запрос         — ВИДЕО (Pixabay), запрос на английском: что должно быть видно в кадре
#   [archive] запрос       — архивное фото реального места/документа (Wikimedia), запрос на английском
#   [photo] запрос         — фото (Pixabay, затем Wikimedia) — только если видео не бывает
#   [человек] Имя Фамилия | Имя по-русски | запрос, если фото нет  — фото этого человека
#                  (имя на английском, как в Википедии; третье поле — кадры по его теме, например vintage airplane);
#                            если свободного фото нет — на экране появится подпись с именем
#   Строк кадров у абзаца несколько: они идут по порядку предложений, каждая — под своё предложение.
#   [надпись] Главное | пояснение — всплывающая подпись на экране (строку можно удалить)
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
    for ch in script["chapters"]:
        lines.append(f"== ГЛАВА: {ch['title']} ==")
        lines.append("")
        for s in ch["segments"]:
            lines.append(s["text"])
            shots = s.get("shots") or [(s["visual"], s["query"])]
            for v, q in shots:
                lines.append(f"[человек] {q}" if v == "person" else f"[{v}] {q}")
            if s.get("label"):
                lines.append(f"[надпись] {s['label']}")
            if s.get("person") and not any(v == "person" for v, _ in shots):
                lines.append(f"[человек] {s['person']}")
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
        txt, label, person, mood = [], "", "", "normal"
        shots = []
        for ln in block:
            m = re.match(r"^\[(archive|stock|photo|фото|видео|архив)\]\s*(.*)$", ln.strip(), re.I)
            tag = re.match(r"^\[(надпись|человек|переход)\]\s*(.*)$", ln.strip(), re.I)
            if m:
                kind = {"фото": "photo", "видео": "stock", "архив": "archive"}.get(m.group(1).lower(),
                                                                               m.group(1).lower())
                shots.append((kind, m.group(2).strip() or "dark atmosphere"))
            elif tag:
                name, val = tag.group(1).lower(), tag.group(2).strip()
                if name == "надпись":
                    label = val
                elif name == "человек":
                    if val:
                        person = person or val
                        shots.append(("person", val))
                else:
                    mood = TEXT_TO_MOOD.get(val.lower(), "normal")
            else:
                txt.append(ln.strip())
        body = " ".join(t for t in txt if t)
        if body:
            nonlocal current
            if current is None:
                current = {"title": "Вступление", "segments": []}
                chapters.append(current)
            if not any(v != "person" for v, _ in shots):
                shots.append(("stock", "dark atmosphere"))
            first = next((x for x in shots if x[0] != "person"), shots[0])
            seg = {"text": body, "visual": first[0], "query": first[1],
                   "label": label, "person": person, "mood": mood, "shots": shots}
            current["segments"].append(seg)
        block = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#! НАЗВАНИЕ:"):
            script["title"] = line.split(":", 1)[1].strip() or script.get("title", "")
            continue
        if line.startswith("#! ОБЛОЖКА:"):
            script["thumbnail_text"] = line.split(":", 1)[1].strip()
            continue
        meta = re.match(r"^#!\s*(ЯЗЫК|ТЕГИ|ОПИСАНИЕ|ФАКТ|ТЕМА|ВАРИАНТ НАЗВАНИЯ):\s?(.*)$", line)
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
            elif key == "ВАРИАНТ НАЗВАНИЯ" and val:
                options.append(val)
            continue
        if line.startswith("#"):
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
