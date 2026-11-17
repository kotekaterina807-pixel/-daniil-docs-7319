"""Предпроверка сценария перед сборкой: считает слова, главы, долю фото и повторы запросов.
Ничего не меняет и не останавливает сборку — только пишет предупреждения в журнал."""
import re

BAD_WORDS = re.compile(r"\b(flower|sunflower|bench|cat|dog|beach|party|office|business|laptop|city skyline|"
                       r"cartoon|vector|illustration|logo)\b", re.I)
GENERIC = {"mountain", "mountains", "forest", "sky", "clouds", "snow", "nature", "landscape", "dark", "fog", "water"}


def check(text, wpm=125, photo_cap=0.40):
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
    if minutes < 20:
        warn.append(f"ролик получится ≈{minutes:.0f} мин, для 25 минут нужно около 3100 слов")
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
    return out + [f"  Внимание: {w}" for w in warn]
