"""Предпроверка сценария перед сборкой: длина (цель 25–30 минут), главы, доля фото, люди, повторы запросов
и оценка расхода лимита Claude. Ничего не меняет и не останавливает сборку — только пишет предупреждения в журнал."""
import re

BAD_WORDS = re.compile(r"\b(flower|sunflower|bench|cat|dog|beach|party|office|business|laptop|city skyline|"
                       r"cartoon|vector|illustration|logo)\b", re.I)
GENERIC = {"mountain", "mountains", "forest", "sky", "clouds", "snow", "nature", "landscape", "dark", "fog", "water"}


WPM = {"ru": 135, "en": 150, "de": 130, "fr": 150, "es": 150}
MIN_MINUTES, GOOD_MINUTES = 25, 30


def check(text, wpm=None, photo_cap=0.40, min_minutes=None, min_queries=100):
    lang_m = re.search(r"(?im)^#!\s*ЯЗЫК:\s*([a-z]{2})", text or "")
    wpm = wpm or WPM.get(lang_m.group(1).lower() if lang_m else "de", 130)
    people = set(m.strip() for m in re.findall(r"(?im)^#!\s*ФОТО:\s*([^=\n]+)=", text or ""))
    words, chapters = 0, 0
    kinds = {"stock": [], "archive": [], "photo": [], "хроника": [], "человек": [], "карточка": []}
    for raw in text.replace("\r\n", "\n").split("\n"):
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^\[(stock|archive|photo|хроника|человек)\]\s*(.*)$", ln, re.I)
        if m:
            kinds[m.group(1).lower()].append(m.group(2).strip())
            continue
        if re.match(r"^\[(цифра|число|дата|цитата|сравнение|заголовок|таймлайн|новость|далее|список|ведущий)\]", ln, re.I):
            kinds["карточка"].append(ln)
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
           f"фото-строк {photos} ({share * 100:.0f}%), хроники {len(kinds['хроника'])}, инфо-карточек {len(kinds['карточка'])}."]
    warn = []
    lo = min_minutes or MIN_MINUTES                       # у каждой ниши своя длина (тайны 25–30, стройка/еда 12–15)
    hi = GOOD_MINUTES if lo == MIN_MINUTES else lo + 4
    if minutes < lo:
        warn.append(f"ролик получится ≈{minutes:.0f} мин — меньше {lo}. Для {lo}–{hi} минут "
                    f"нужно {lo * wpm}–{hi * wpm} слов (сейчас {words}, не хватает "
                    f"≈{lo * wpm - words}). Сборка продолжится")
    if kinds["человек"] and not people:
        warn.append("есть строки [человек], но нет ни одной строки «#! ФОТО: Имя = ссылка» — лица будут искаться наугад")
    if share > photo_cap:
        warn.append(f"фото-строк {share * 100:.0f}% при пределе {photo_cap * 100:.0f}% — часть станет видео автоматически")
    st = [q.lower() for q in kinds["stock"]]
    dup = len(st) - len(set(st))
    if dup > max(5, len(st) // 10):
        warn.append(f"повторяющихся видео-запросов: {dup} (кадры будут одинаковыми)")
    if len(set(st)) < min_queries:
        warn.append(f"разных видео-запросов {len(set(st))}, нужно от {min_queries}")
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
    out += hook_check(text, wpm)
    return out + [f"  Внимание: {w}" for w in warn]


SCENE_START = re.compile(r"^(?:\d{1,2}\.\s*)?(?:januar|februar|märz|april|mai|juni|juli|august|september|oktober|"
                         r"november|dezember|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|"
                         r"noviembre|diciembre|im (?:jahr|sommer|winter|frühjahr|herbst)|\d{4})\b[^.!?]{0,40}[.]", re.I)


def hook_check(text, wpm):
    """Хук (пролог): длина, когда звучит вопрос ролика, не начинается ли ролик с описания обстановки.
    Статистика канала: зрители уходят в первую минуту — сильное должно звучать в первой фразе."""
    lines, on = [], False
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        ln = raw.strip()
        if ln.startswith("=="):
            if on:
                break
            on = bool(re.match(r"^==\s*ПРОЛОГ", ln, re.I))
            continue
        if on and ln and not ln.startswith(("#", "[")):
            lines.append(ln)
    if not lines:
        return ["  Хук: пролога (== ПРОЛОГ ==) нет — ролик начнётся сразу с главы."]
    words = " ".join(lines).split()
    sec = lambda n: n / max(1, wpm) * 60
    total = sec(len(words))
    out, warn = [], []
    q_at = None
    for i, w in enumerate(words):
        if "?" in w:
            q_at = sec(i + 1)
            break
    first = re.split(r"(?<=[.!?])\s+", " ".join(lines))[0]
    out.append(f"  Хук: пролог ≈{total:.0f} с, первый вопрос на ≈{q_at:.0f}-й секунде." if q_at is not None
               else f"  Хук: пролог ≈{total:.0f} с, вопроса в прологе нет.")
    if total > 50:
        warn.append(f"пролог ≈{total:.0f} с — длиннее 45 с, зрители уходят раньше. Сократите до 30–45 с")
    if q_at is None:
        warn.append("в прологе нет вопроса ролика. Нужен до 20-й секунды (на экране его покажет программа)")
    elif q_at > 22:
        warn.append(f"вопрос ролика звучит только на ≈{q_at:.0f}-й секунде, нужен до 20-й. Перенесите его выше")
    if SCENE_START.match(first) or len(first.split()) <= 4:
        warn.append(f"ролик начинается с обстановки («{first[:60]}»). Первая фраза должна быть главной уликой "
                    "или последним моментом, дата и место — после")
    return out + [f"  Внимание (хук): {w}" for w in warn]
