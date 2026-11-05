"""Проверка кадров нейросетью: соответствие смыслу текста, водяные знаки, логотипы, надписи."""
import base64
import io
import json
import os
import re
import time

import requests
from PIL import Image

from . import llm

UA = {"User-Agent": "YTDocVideoGenerator/1.0 (personal YouTube documentary tool)"}


class VisionError(Exception):
    pass


# Если модель проверки кадров несколько раз подряд не дала разборчивого ответа,
# до конца сборки кадры выбираются по рейтингу поиска (без остановки сборки).
_state = {"fails": 0, "off": False, "err_logged": False, "checked": 0, "calls": 0}
_lock = __import__("threading").Lock()


def reset():
    _state.update(fails=0, off=False, err_logged=False, checked=0, calls=0)


def available():
    return not _state["off"]


def _thumb_b64(url):
    """Скачивает превью и уменьшает до 512 px по ширине (дешевле и быстрее проверка)."""
    for attempt in range(3):
        try:
            if os.path.exists(url):                      # превью из своей библиотеки
                im = Image.open(url).convert("RGB")
            else:
                r = requests.get(url, headers=UA, timeout=30)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
            im.thumbnail((512, 512))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            time.sleep(2)
    return None


def _post(cfg, content, max_tokens, log):
    try:
        return llm.chat(cfg, "", content, max_tokens, "vision", log)
    except llm.LLMError as e:
        raise VisionError(str(e))


def _json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


def stats():
    return dict(_state)


def judge(cfg, narration, topic, candidates, log=print, person="", query="", scores=False):
    """Оценивает кандидатов. Возвращает индексы подходящих, лучшие первыми.
    scores=True — пары (индекс, оценка), включая запасные (не ниже reserve_relevance)."""
    if _state["off"]:
        order = list(range(len(candidates)))           # проверка отключена — порядок поиска
        return [(i, 0.0) for i in order] if scores else order
    blocks, idx_map = [], []
    for i, c in enumerate(candidates):
        b64 = _thumb_b64(c["thumb"])
        if not b64:
            continue
        idx_map.append(i)
        blocks.append({"type": "text", "text": f"Кадр {len(idx_map)}:"})
        blocks.append({"type": "image", "b64": b64})
    if not idx_map:
        return []
    person_hint = ("" if not person else
                   f"Эти кадры найдены в архиве как фото человека: {person}. Узнавать человека по лицу НЕ нужно. "
                   f"Для relevance оцени: это фотография/портрет человека, соответствующая эпохе и тексту; "
                   f"people_faces для этого человека ставь false.\n")
    prompt = f"""Ты — строгий бильд-редактор документального YouTube-канала.
Тема видео: {topic}
Текст диктора, который звучит во время кадра:
«{narration}»
{f"Что должно быть видно в кадре: {query}" if query else ""}

{person_hint}
Для КАЖДОГО кадра выше оцени:
- relevance: 0–10, насколько зритель, услышав этот текст, увидит в кадре ИМЕННО то, о чём говорят.
  Главный предмет фразы (место, предмет, действие, погода, время суток) должен быть хорошо виден в кадре.
  9–10 — в кадре ровно то, о чём фраза; 7–8 — главный предмет есть, детали отличаются;
  4–6 — только общая атмосфера; 0–3 — про другое. Красивый, но не про это — не выше 4.
  Если фраза про ночь, а в кадре день (или про шторм, а море спокойное) — не выше 4.
  Кадр не должен добавлять в историю того, чего в ней нет: случайные люди, предметы, события — не выше 3.
  Признаки быстрой съёмки (таймлапс, смазанные огни, гонки, мелькание, тряска) — не выше 3.
- watermark: true, если есть водяной знак, логотип канала/ТВ/стока, чужие надписи или титры поверх, рамка, интерфейс плеера, субтитры.
- anachronism: true, если текст о прошлом, а в кадре явно современные вещи (современные машины, смартфоны, вывески, одежда).
- people_faces: true, если крупным планом лицо современного человека, которое зритель может принять за участника событий.
- graphic: true, если видны тела погибших, кровь, раны, насилие, оружие, направленное на человека.
- extremist: true, если на кадре заметно видна символика экстремистских/террористических организаций или нацистская символика.

Верни СТРОГО JSON: {{"frames": [{{"n": 1, "relevance": 0, "watermark": false, "anachronism": false, "people_faces": false, "graphic": false, "extremist": false}}]}}"""

    content = blocks + [{"type": "text", "text": prompt}]
    if _state["off"]:
        order = list(range(len(candidates)))           # проверка отключена — порядок поиска
        return [(i, 0.0) for i in order] if scores else order
    data = None
    for attempt in range(2):
        try:
            data = _json(_post(cfg, content, 1500, log))
            with _lock:
                _state["calls"] += 1
                _state["checked"] += len(idx_map)
            break
        except (ValueError, json.JSONDecodeError):
            continue
        except VisionError as e:
            if not _state.get("err_logged"):
                log(f"  Проверка кадров: {str(e)[:200]}")
                _state["err_logged"] = True
            break
    if not data or not isinstance(data.get("frames"), list):
        _state["fails"] += 1
        if _state["fails"] >= 2:
            _state["off"] = True
            log("  ВНИМАНИЕ: модель проверки кадров не даёт разборчивых ответов — до конца сборки "
                "кадры выбираются по рейтингу поиска. Нажмите «Проверить всё» и пришлите результат.")
        order = list(range(len(candidates)))
        return [(i, 0.0) for i in order] if scores else order
    _state["fails"] = 0
    min_rel = float(cfg.get("min_relevance", 7))
    if scores:
        min_rel = min(min_rel, float(cfg.get("reserve_relevance", 5)))
    scored = []
    for f in data.get("frames", []):
        try:
            n = int(f.get("n", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(idx_map):
            continue
        if f.get("watermark") or f.get("anachronism") or f.get("graphic"):
            continue
        if f.get("people_faces") and not person:
            continue
        if f.get("extremist") and not cfg.get("allow_historic_symbols", False):
            continue
        rel = float(f.get("relevance", 0) or 0)
        if rel >= min_rel:
            scored.append((rel, idx_map[n]))
    scored.sort(key=lambda x: -x[0])
    if scores:
        return [(i, rel) for rel, i in scored]
    return [i for _, i in scored]


def alt_queries(cfg, narration, tried, log=print):
    """Просит новые поисковые запросы, если по старым ничего подходящего не нашлось."""
    prompt = f"""Для документального видео нужен кадр под этот текст диктора:
«{narration}»
По этим запросам ничего подходящего не нашлось: {", ".join(tried)}.
Предложи 4 других поисковых запроса НА АНГЛИЙСКОМ (2–4 слова) для фотостока/видеостока и фотоархива:
что ВИДНО в кадре, который точно передаёт смысл текста (место, предмет, природа, эпоха, действие).
Верни СТРОГО JSON: {{"queries": ["...", "...", "...", "..."]}}"""
    if _state["off"]:
        return []
    try:
        return [q for q in _json(_post(cfg, prompt, 400, log)).get("queries", []) if isinstance(q, str)][:4]
    except (ValueError, json.JSONDecodeError, VisionError):
        return []
