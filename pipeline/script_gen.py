"""Генерация сценария: сначала план, затем каждая глава отдельно."""
import json
import re
import time

from . import llm
from .config import lang as get_lang

WORDS_PER_MINUTE = 135  # темп по умолчанию; для каждого языка свой — см. config.LANGS


def _lang_rule(cfg):
    L = get_lang(cfg.get("language", "ru"))
    return (f"\n\nЯЗЫК ВИДЕО: весь текст для зрителя (закадровый текст, названия, надписи, описание, теги, "
            f"текст обложки) пиши на {L['prompt']} языке, естественно, как носитель. "
            f"Служебные поля JSON и поисковые запросы — как указано в задании.")


def _wpm(cfg):
    return get_lang(cfg.get("language", "ru"))["wpm"]

BASE_RULES = """Ты — главный сценарист документального YouTube-канала. Пишешь закадровый текст для длинных видео (20–60 минут), который смотрят до конца.

ПРАВИЛА УДЕРЖАНИЯ ЗРИТЕЛЯ:
- Первые 30 секунд: самый сильный факт или момент истории, затем вопрос, на который зритель захочет узнать ответ. Никаких «привет, друзья», «в этом видео мы расскажем».
- Каждая глава открывает новую деталь и заканчивается недосказанностью или новым вопросом, который тянет в следующую главу.
- Раскладывай историю как расследование: события, затем улики, затем версии, затем что осталось неизвестным.
- Конкретика вместо общих слов: даты, места, имена, числа, детали обстановки.
- Короткие предложения, разговорный дикторский ритм. Текст будет озвучен — никаких скобок, сносок, списков, эмодзи, ремарок.
- Числа и даты пиши словами так, как их прочитает диктор, если цифры могут быть прочитаны неоднозначно.

ПРАВИЛА ФАКТОВ (обязательно):
- Используй только хорошо задокументированные факты. Не выдумывай имена, даты, цитаты, документы, числа.
- Если чего-то точно не знаешь — не пиши это. Спорное явно называй версией или гипотезой и говори, кто её выдвигал.
- Не обвиняй реальных людей в преступлениях без приговора суда.
- Без натуралистичных описаний насилия и тел: YouTube ограничивает монетизацию шокирующего контента."""


class ScriptError(Exception):
    pass


def _call_claude(cfg, system, user, max_tokens=8000, log=print):
    """Запрос к нейросети для сценария (Yandex AI Studio или Claude — по настройкам)."""
    try:
        return llm.chat(cfg, system, user, max_tokens, "text", log)
    except llm.LLMError as e:
        raise ScriptError(str(e))


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON не найден")
    return json.loads(text[start:end + 1])


def _ask_json(cfg, system, user, max_tokens, log):
    for attempt in range(3):
        raw = _call_claude(cfg, system, user, max_tokens, log)
        try:
            return _extract_json(raw)
        except Exception:
            log("  Ответ пришёл в неверном формате, запрашиваю повторно...")
            user = user + "\n\nВерни СТРОГО валидный JSON без пояснений и без markdown."
    raise ScriptError("Нейросеть трижды вернула ответ в неверном формате.")


def make_outline(cfg, niche, topic, minutes, log=print):
    body_chapters = max(2 if minutes < 8 else 4, round(minutes / 3.5))
    total_words = int(minutes * _wpm(cfg))
    system = BASE_RULES + "\n\nСТИЛЬ НИШИ:\n" + niche["style"] + _lang_rule(cfg)
    user = f"""Тема видео: {topic}
Длительность: около {minutes} минут (примерно {total_words} слов закадрового текста).

Составь план видео. Структура:
1) вступление-крючок ({"20–30 секунд" if minutes < 8 else "60–90 секунд"}),
2) {body_chapters} основных глав,
3) финал (что известно точно, что осталось загадкой, вопрос зрителям для комментариев).

Верни JSON:
{{
  "title": "главное название видео (до 70 символов, интрига без обмана)",
  "title_options": ["ещё 4 варианта названия"],
  "thumbnail_text": "2–4 слова для обложки, крупно",
  "description": "описание для YouTube, 2–3 абзаца, без таймкодов",
  "tags": ["10–15 тегов"],
  "chapters": [
    {{"title": "короткое название главы", "content": "что именно раскрывается в главе: события, факты, детали", "ending": "чем глава заканчивается, какой вопрос остаётся", "share": 0.1}}
  ],
  "facts_to_verify": ["ключевое утверждение — где его проверить (книга, архив, статья)"]
}}
Поле share — доля главы в общей длине, сумма по всем главам = 1."""
    log("Составляю план видео...")
    outline = _ask_json(cfg, system, user, 6000, log)
    if not outline.get("chapters"):
        raise ScriptError("В плане нет глав.")
    total = sum(float(c.get("share", 0) or 0) for c in outline["chapters"]) or 1
    for c in outline["chapters"]:
        share = float(c.get("share", 0) or 0) or (1 / len(outline["chapters"]))
        c["words"] = max(120, int(total_words * share / total))
    return outline


def write_chapter(cfg, niche, topic, outline, idx, prev_tail, log=print):
    ch = outline["chapters"][idx]
    plan = "\n".join(f"{i + 1}. {c['title']} — {c.get('content', '')}" for i, c in enumerate(outline["chapters"]))
    system = BASE_RULES + "\n\nСТИЛЬ НИШИ:\n" + niche["style"] + _lang_rule(cfg)
    visual_hint = niche.get("visual_hint", "")
    user = f"""Тема видео: {topic}
Название: {outline.get('title', '')}

План всего видео:
{plan}

Сейчас пишешь главу {idx + 1}: «{ch['title']}».
Что раскрыть: {ch.get('content', '')}
Чем закончить: {ch.get('ending', '')}
Объём: около {ch['words']} слов закадрового текста.
{"Конец предыдущей главы (продолжай без повторов): " + prev_tail if prev_tail else "Это начало видео."}

Разбей текст на сегменты по 2–4 предложения (30–60 слов). Для каждого сегмента:

"visual" — тип картинки:
- "archive" — архивное фото/документ/карта/портрет (Wikimedia Commons): реальные люди, места, события, техника;
- "stock" — атмосферное видео (Pexels): природа, туман, лес, снег, город ночью, руки с документами, свеча и т.п.
{visual_hint}

"query" — поисковый запрос НА АНГЛИЙСКОМ, 2–5 слов: что именно ВИДНО в кадре, который точно передаёт смысл сегмента (место, предмет, природа, эпоха, действие). Без абстракций.

"person" — если сегмент рассказывает о конкретном реальном человеке и его фото уместно — имя и фамилия НА АНГЛИЙСКОМ, как в английской Википедии (например «Amelia Earhart»). Иначе пустая строка.
  НЕ указывай person (оставь пусто), если человек: преступник или подозреваемый в насильственном преступлении; жертва насилия; ребёнок или подросток; террорист, лидер экстремистской организации или нацистский преступник; если фото может показывать погибшего. Для таких сегментов — только атмосферный кадр без лиц.

"mood" — настроение сегмента, от него зависит переход к нему:
- "tense" — страшный, тревожный, трагический момент (переход будет спокойным и медленным);
- "climax" — самый интересный момент: поворот, разгадка, неожиданный факт (переход будет эффектным);
- "normal" — обычное повествование.
  "climax" — не больше 1–2 раз на главу.

"label" — всплывающая надпись на экране. Ставь её РЕДКО: только когда ВПЕРВЫЕ появляется ключевое место, главный участник или ключевая дата. Не больше одной надписи на 4–5 сегментов, во вступлении — не больше одной. В остальных сегментах — пустая строка.
  Формат: «Главное | пояснение» на языке видео. Главное — до 30 символов, пояснение — до 40.
  Образец формата: «Название места | регион, год», «Имя Фамилия | кто это».
  Только точные факты. Если не уверен — не ставь.

Верни JSON:
{{"segments": [{{"text": "текст диктора", "visual": "archive", "query": "english search query", "person": "", "mood": "normal", "label": ""}}]}}"""
    log(f"Пишу главу {idx + 1}/{len(outline['chapters'])}: {ch['title']}")
    data = _ask_json(cfg, system, user, 8000, log)
    segs = []
    for s in data.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        vis = s.get("visual", "stock")
        segs.append({
            "text": text,
            "visual": vis if vis in ("archive", "stock") else "stock",
            "query": (s.get("query") or "").strip() or "dark atmosphere",
            "label": (s.get("label") or "").strip(),
            "person": (s.get("person") or "").strip(),
            "mood": s.get("mood") if s.get("mood") in ("normal", "tense", "climax") else "normal",
        })
    if not segs:
        raise ScriptError(f"Глава {idx + 1} пришла пустой.")
    return segs


def generate_script(cfg, niche, topic, minutes, log=print):
    outline = make_outline(cfg, niche, topic, minutes, log)
    chapters = []
    prev_tail = ""
    for i in range(len(outline["chapters"])):
        segs = write_chapter(cfg, niche, topic, outline, i, prev_tail, log)
        chapters.append({"title": outline["chapters"][i]["title"], "segments": segs})
        prev_tail = " ".join(s["text"] for s in segs[-2:])
    words = sum(len(s["text"].split()) for c in chapters for s in c["segments"])
    log(f"Сценарий готов: {words} слов, примерно {words / _wpm(cfg):.0f} мин.")
    return {
        "topic": topic,
        "title": outline.get("title", topic),
        "title_options": outline.get("title_options", []),
        "thumbnail_text": outline.get("thumbnail_text", ""),
        "description": outline.get("description", ""),
        "tags": outline.get("tags", []),
        "facts_to_verify": outline.get("facts_to_verify", []),
        "chapters": chapters,
        "language": cfg.get("language", "ru"),
    }


def suggest_topics(cfg, niche, count=12, exclude=(), log=print):
    system = BASE_RULES + "\n\nСТИЛЬ НИШИ:\n" + niche["style"] + _lang_rule(cfg)
    ex = ("\nНе предлагай эти темы: " + "; ".join(exclude)) if exclude else ""
    user = f"""Предложи {count} тем для длинных видео (20–40 минут) в нише «{niche['name']}».
Требования: реальные задокументированные истории; достаточно материала на 20+ минут; есть сильная интрига или поворот; есть архивные фото в открытом доступе.
Смешай очень известные темы (большой поисковый спрос) и малоизвестные (мало конкурентов на YouTube).{ex}
Верни JSON: {{"topics": ["тема — одно предложение, в чём интрига"]}}"""
    log("Подбираю темы...")
    return _ask_json(cfg, system, user, 3000, log).get("topics", [])
