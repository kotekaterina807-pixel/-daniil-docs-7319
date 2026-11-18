"""Предпроверка сценария перед сборкой: длина (цель 25–30 минут), главы, доля фото, люди, повторы запросов
и оценка расхода лимита Claude. Ничего не меняет и не останавливает сборку — только пишет предупреждения в журнал."""
import re

BAD_WORDS = re.compile(r"\b(flower|sunflower|bench|cat|dog|beach|party|office|business|laptop|city skyline|"
                       r"cartoon|vector|illustration|logo)\b", re.I)
GENERIC = {"mountain", "mountains", "forest", "sky", "clouds", "snow", "nature", "landscape", "dark", "fog", "water"}


WPM = {"ru": 135, "en": 150, "de": 130, "fr": 150, "es": 150}
MIN_MINUTES, GOOD_MINUTES = 25, 30


def check(text, wpm=None, photo_cap=0.40):
    lang_m = re.search(r"(?im)^#!\s*ЯЗЫК:\s*([a-z]{2})", text or "")
    wpm = wpm or WPM.get(lang_m.group(1).lower() if lang_m else "de", 130)
    people = set(m.strip() for m in re.findall(r"(?im)^#!\s*ФОТО:\s*([^=\n]+)=", text or ""))
    words, chapters = 0, 0
    kinds = {"stock": [], "archive": [], "photo": [], "хроника": [], "человек": []}
    for raw in text.replace("\r\n", "\n").split("\n"):
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^\[(stock|archive|photo|хроника|человек)\]\s*(.*)$", ln, re.I)
        if m:
            kinds[m.group(1).lower()].append(m.group(2).strip())
            continue
        if ln.startswith("=="):
            if re.match(r"^==\s*ГЛАВА", ln, re.I):
                chapters += 1
            continue
        if ln.startswith("["):
            continue
        words += len(ln.split())
    shots = sum(len(v) for k, v in kinds.items() if k != "человек")
    photos = len(kinds["archive"]) + len(kinds["photo"]) + len(kinds["человек"])
    share = photos / max(1, shots + len(kinds["человек"]))
    minutes = words / wpm
    out = [f"Предпроверка сценария: {words} слов (≈{minutes:.1f} мин), глав {chapters}, строк кадров {shots}, "
           f"фото-строк {photos} ({share * 100:.0f}%)."]
    warn = []
    if minutes < MIN_MINUTES:
        warn.append(f"ролик получится ≈{minutes:.0f} мин — меньше {MIN_MINUTES}. Для {MIN_MINUTES}–{GOOD_MINUTES} минут "
                    f"нужно {MIN_MINUTES * wpm}–{GOOD_MINUTES * wpm} слов (сейчас {words}, не хватает "
                    f"≈{MIN_MINUTES * wpm - words}). Сборка продолжится")
    if kinds["человек"] and not people:
        warn.append("есть строки [человек], но нет ни одной строки «#! ФОТО: Имя = ссылка» — лица будут искаться наугад")
    if share > photo_cap:
        warn.append(f"фото-строк {share * 100:.0f}% при пределе {photo_cap * 100:.0f}% — часть станет видео автоматически")
    st = [q.lower() for q in kinds["stock"]]
    dup = len(st) - len(set(st))
    if dup > max(5, len(st) // 10):
        warn.append(f"повторяющихся видео-запросов: {dup} (кадры будут одинаковыми)")
    if len(set(st)) < 100:
        warn.append(f"разных видео-запросов {len(set(st))}, для 25 минут лучше от 150")
    gen = [q for q in st if q in GENERIC]
    if gen:
        warn.append(f"слишком общие запросы: {', '.join(sorted(set(gen))[:6])}")
    bad = [q for q in st if BAD_WORDS.search(q)]
    if bad:
        warn.append(f"запросы, дающие случайные кадры: {'; '.join(bad[:4])}")
    # оценка расхода лимита Claude: строк кадров ≈ фразы диктора (≈11 слов), лист — 3 строки, запрос — 3 листа
    lines_est = max(1, words // 11)
    sheets = -(-lines_est // 3)
    calls = -(-sheets // 3) + -(-lines_est // 45)
    out.append(f"  Оценка расхода Claude: ≈{calls} запросов (план кадров {-(-lines_est // 45)}, проверка "
               f"≈{sheets} листов пачками по 3).")
    return out + [f"  Внимание: {w}" for w in warn]
