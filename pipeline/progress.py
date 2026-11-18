"""Ход сборки по реальным этапам: процент и счётчик внутри этапа, общий процент и оценка оставшегося
времени (по скорости текущего этапа + сколько прошлые сборки тратили на следующие этапы).

Сборка сообщает о ходе строкой «Этап d/t» или «Этап» (доля 0..1 в progress() больше не важна).
Время этапов прошлых сборок хранится в output/_stats/stage_times.json (обновление его не трогает)."""
import json
import re
import time
from pathlib import Path

# этапы по порядку: (название, регулярка по тексту этапа, ожидаемые секунды для ролика ~25 мин)
STAGES = [
    ("Озвучка", r"^Озвучка", 600),
    ("План кадров", r"^План кадров", 240),
    ("Поиск кадров", r"^(Поиск|Подбор) кадров", 900),
    ("Проверка кадров", r"^(Проверка кадров|Круг \d|Claude проверяет)", 1500),
    ("Скачивание", r"^Скачивание", 500),
    ("Подготовка монтажа", r"^(Подготовка монтажа|Кадры выбраны|Раскладка|Контроль качества)", 240),
    ("Монтаж кадров", r"^Монтаж кадров", 2400),
    ("Склейка", r"^Склейка", 600),
    ("Сведение звука", r"^Сведение звука", 300),
    ("Файлы", r"^(Файлы|Готово)", 60),
]
NAMES = [s[0] for s in STAGES]
PCT_STAGES = {"Монтаж кадров", "Склейка"}           # показываются процентом (счётчик — в скобках)


def stats_path():
    from .config import OUTPUT_DIR
    return Path(OUTPUT_DIR) / "_stats" / "stage_times.json"


def load_hist():
    try:
        return json.loads(stats_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_hist(durations):
    """Запомнить длительность этапов этой сборки (сглаженно с прошлыми)."""
    h = load_hist()
    for k, v in durations.items():
        if v < 5:                    # этап пропущен (всё было готово) — не портим оценку
            continue
        h[k] = round(v if k not in h else 0.6 * h[k] + 0.4 * v, 1)
    try:
        p = stats_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def human(sec):
    """«3 мин 05 с», «1 ч 12 мин»; меньше 5 с не показывается вовсе (никаких «0 секунд»)."""
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} с"
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h} ч {m} мин" if h else f"{m} мин {s:02d} с"


def parse(text):
    """«Скачивание 120/370» → ("Скачивание", 120, 370); «Сведение звука» → ("Сведение звука", None, None)."""
    text = str(text or "").strip()
    idx = next((i for i, (_, rx, _) in enumerate(STAGES) if re.search(rx, text)), None)
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    d, t = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    pm = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if pm and d is None:
        d, t = float(pm.group(1)), 100
    return idx, d, t


class Tracker:
    def __init__(self):
        self.hist = load_hist()
        self.t0 = time.time()
        self.cur = None              # номер этапа
        self.cur_t0 = None
        self.done_n = None
        self.total_n = None
        self.text = ""
        self.durations = {}          # этап → секунды (завершённые)
        self.pct = 0.0
        self.changed = False         # сменился этап (для live.json)
        self.last_d_t = None         # (время, d) последнего сдвига счётчика

    def expected(self, i):
        name, _, dflt = STAGES[i]
        return float(self.hist.get(name, dflt))

    def update(self, text):
        idx, d, t = parse(text)
        now = time.time()
        if idx is None:
            self.text = str(text)
            return
        if self.cur is None or idx != self.cur:
            if self.cur is not None and idx < self.cur:
                return               # этапы не идут назад (например, «Поиск кадров» второго круга)
            if self.cur is not None:
                self.durations[NAMES[self.cur]] = self.durations.get(NAMES[self.cur], 0) + now - self.cur_t0
            self.cur, self.cur_t0 = idx, now
            self.changed = True
            self.last_d_t = None
        if d is not None and (self.done_n != d or self.last_d_t is None):
            self.last_d_t = (now, d)
        self.done_n, self.total_n = d, t
        self.text = str(text)

    def finish(self):
        if self.cur is not None and self.cur_t0:
            self.durations[NAMES[self.cur]] = self.durations.get(NAMES[self.cur], 0) + time.time() - self.cur_t0
        save_hist(self.durations)

    def stage_frac(self):
        if self.cur is None:
            return 0.0
        if self.done_n is not None and self.total_n:
            return max(0.0, min(1.0, self.done_n / self.total_n))
        # без счётчика: по ожидаемой длительности, но не больше 90%
        el = time.time() - (self.cur_t0 or time.time())
        return min(0.9, el / max(30.0, self.expected(self.cur)))

    def overall(self):
        if self.cur is None:
            return 0.0
        tot = sum(self.expected(i) for i in range(len(STAGES)))
        before = sum(self.expected(i) for i in range(self.cur))
        p = (before + self.stage_frac() * self.expected(self.cur)) / tot
        self.pct = max(self.pct, min(0.995, p))          # процент не идёт назад
        return self.pct

    def eta(self):
        """Секунды до конца: остаток текущего этапа + ожидаемое время следующих."""
        if self.cur is None:
            return None
        now = time.time()
        el = now - (self.cur_t0 or now)
        f = self.stage_frac()
        exp = self.expected(self.cur)
        if self.done_n is not None and self.total_n and self.done_n > 0 and el > 15:
            rest = el / self.done_n * (self.total_n - self.done_n)
            if f < 0.15:                                  # в начале этапа скорость ещё неточная
                rest = 0.5 * rest + 0.5 * exp * (1 - f)
        else:
            rest = max(exp - el, exp * 0.1)
        rest += sum(self.expected(i) for i in range(self.cur + 1, len(STAGES)))
        return max(30.0, rest)                            # пока идёт работа — не «0 секунд»

    def label(self):
        """Текст этапа для окна: «Скачивание 120/370», «Монтаж кадров 35% (125/356)»."""
        if self.cur is None:
            return self.text or "Подготовка"
        name = NAMES[self.cur]
        if self.done_n is None or not self.total_n:
            return name
        if self.total_n == 100 and name not in PCT_STAGES:
            return f"{name} {int(self.done_n)}%"
        if name in PCT_STAGES:
            return f"{name} {int(100 * self.done_n / self.total_n)}% ({int(self.done_n)}/{self.total_n})"
        return f"{name} {int(self.done_n)}/{self.total_n}"

    def snapshot(self):
        return {"stage": self.label(), "stage_no": (self.cur or 0) + 1, "stages": len(STAGES),
                "stage_name": NAMES[self.cur] if self.cur is not None else "",
                "stage_pct": round(self.stage_frac() * 100), "done": self.done_n, "total": self.total_n,
                "progress": round(self.overall() * 100), "eta_min": round((self.eta() or 0) / 60),
                "stages_done": {k: human(v) for k, v in self.durations.items() if v >= 5}}
