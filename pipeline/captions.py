"""Крупные пословные подписи (как у каналов с ведущим) и штампы поверх кадра (ниша «еда»).

Подписи: по 1–3 слова, ровно под голос (время слов — распознавание речи), белые буквы с чёрной обводкой,
главное слово (число или самое длинное) — жёлтое. Штамп: красная рамка с надписью, наклон, справа сверху."""
import difflib
import random
import re

from PIL import Image, ImageDraw

from .align import norm

W, H = 1920, 1080
HEAD_FONTS = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\bahnschrift.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
YELLOW, RED = (250, 204, 21), (214, 40, 40)


def _font(size):
    from .extras import _font as f
    return f(size, HEAD_FONTS)


def timed_words(text, rec, dur):
    """Слова сценария с временем: [(start, end, слово как в тексте)]. rec — слова распознавания [(a, b, w)]."""
    toks = re.findall(r"\S+", text or "")
    if not toks:
        return []
    keys = [norm(re.sub(r"[^\w-]", "", t.lower())) for t in toks]
    res = [None] * len(toks)
    if rec:
        rk = [norm(w) for _, _, w in rec]
        sm = difflib.SequenceMatcher(a=keys, b=rk, autojunk=False)
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                a, b, _ = rec[blk.b + k]
                res[blk.a + k] = (a, b)
    known = [i for i, r in enumerate(res) if r]
    if not known:                                       # распознавания нет — время по длине слов
        tot = sum(len(t) + 1 for t in toks)
        pos = 0.0
        for i, t in enumerate(toks):
            a = dur * pos / tot
            pos += len(t) + 1
            res[i] = (a, dur * pos / tot)
    else:
        for i in range(len(toks)):                      # пропуски — между соседями с известным временем
            if res[i]:
                continue
            lo = max([j for j in known if j < i], default=None)
            hi = min([j for j in known if j > i], default=None)
            a = res[lo][1] if lo is not None else 0.0
            b = res[hi][0] if hi is not None else min(dur, a + 0.4 * (i - (lo or 0) + 1))
            n_gap = (hi if hi is not None else len(toks)) - (lo if lo is not None else -1) - 1
            k = i - (lo if lo is not None else -1) - 1
            step = max(0.05, (b - a) / max(1, n_gap))
            res[i] = (a + k * step, a + (k + 1) * step)
    return [(r[0], r[1], t) for r, t in zip(res, toks)]


def chunks(words, max_words=3, max_chars=20):
    """Группы по 1–3 слова: граница — знак препинания, пауза больше 0,3 с или длина."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        txt = " ".join(x[2] for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = (re.search(r"[.,!?;:—–]$", w[2]) or len(cur) >= max_words or len(txt) >= max_chars
               or (nxt and nxt[0] - w[1] > 0.3))
        if brk or not nxt:
            out.append((cur[0][0], cur[-1][1], txt))
            cur = []
    return out


def caption_png(text, out):
    """Подпись: белые заглавные буквы с чёрной обводкой, главное слово жёлтое. Прозрачный PNG 1920x1080."""
    words = [re.sub(r"[«»\"„“]", "", w) for w in text.upper().split()]
    words = [w for w in words if w]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not words:
        img.save(out)
        return
    clean = [re.sub(r"[^\w%$€₽]", "", w) for w in words]
    key = next((i for i, w in enumerate(clean) if re.search(r"\d", w)), None)
    if key is None:
        key = max(range(len(clean)), key=lambda i: len(clean[i]))
    d = ImageDraw.Draw(img)
    size = 88
    f = _font(size)
    while d.textlength(" ".join(words), font=f) > W - 260 and size > 50:
        size -= 6
        f = _font(size)
    sp = d.textlength(" ", font=f)
    total = sum(d.textlength(w, font=f) for w in words) + sp * (len(words) - 1)
    x, y = (W - total) / 2, H - 215
    for i, w in enumerate(words):
        col = YELLOW if i == key and len(words) > 1 else (255, 255, 255)
        d.text((x, y), w, font=f, fill=col + (255,), stroke_width=7, stroke_fill=(0, 0, 0, 255))
        x += d.textlength(w, font=f) + sp
    img.save(out)


def stamp_png(text, out, seed=""):
    """Штамп: красная рамка со скруглением и красные буквы, наклон 6–10°, справа сверху. Прозрачный PNG."""
    rnd = random.Random(str(seed) + text)
    t = (text or "").upper().strip()
    lines = [x.strip() for x in t.split("|") if x.strip()] or [t]
    f = _font(76)
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    tw = max(probe.textlength(ln, font=f) for ln in lines)
    lh = int(f.size * 1.05)
    bw, bh = int(tw + 70), int(lh * len(lines) + 46)
    st = Image.new("RGBA", (bw + 20, bh + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(st)
    d.rounded_rectangle([10, 10, 10 + bw, 10 + bh], 16, outline=RED + (235,), width=9)
    for i, ln in enumerate(lines):
        d.text((10 + (bw - d.textlength(ln, font=f)) / 2, 30 + i * lh), ln, font=f, fill=RED + (235,))
    st = st.rotate(rnd.uniform(6, 10) * rnd.choice((-1, 1)), resample=Image.BICUBIC, expand=True)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x = min(W - st.width - 70, 1340 - st.width // 2 + rnd.randint(-40, 40))
    img.paste(st, (max(60, x), 170 + rnd.randint(-30, 40)), st)
    img.save(out)
