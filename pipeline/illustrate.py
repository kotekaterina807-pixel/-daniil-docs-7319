"""Рисованный кадр через Leonardo AI — когда ни в архивах, ни в интернете нет кадра ровно про эти слова.
Промпт пишет Claude, глядя на слова диктора. Стиль — старинная иллюстрация (не «фото»), чтобы зритель
не принял рисунок за документ. Лица реальных людей не рисуются: для них — настоящие фото.
Нужен ключ API Leonardo (оплата API отдельная от подписки на сайте; новым аккаунтам дают $5)."""
import time
from pathlib import Path

import requests

API = "https://cloud.leonardo.ai/api/rest/v1"
STYLE = ("vintage documentary illustration, muted sepia and faded colors, painterly, film grain, "
         "cinematic composition, historically accurate details, no text, no letters, no watermark, no logo")
NEGATIVE = "text, letters, watermark, logo, signature, modern objects, cartoon, anime, 3d render, deformed hands"
_model = {}


class IllustrateError(Exception):
    pass


def available(cfg):
    return bool(str(cfg.get("leonardo_key", "")).strip()) and cfg.get("ai_images", True)


def _h(cfg):
    return {"accept": "application/json", "content-type": "application/json",
            "authorization": f"Bearer {str(cfg.get('leonardo_key', '')).strip()}"}


def check(cfg):
    r = requests.get(f"{API}/me", headers=_h(cfg), timeout=20)
    if r.status_code in (401, 403):
        raise IllustrateError("Leonardo не принял ключ API — создайте его на app.leonardo.ai → API Access.")
    r.raise_for_status()
    return model_id(cfg)


def model_id(cfg):
    """Модель берём из списка доступных: Phoenix, затем Lucid, затем любая из рекомендуемых."""
    if cfg.get("leonardo_model"):
        return cfg["leonardo_model"]
    if "id" in _model:
        return _model["id"]
    r = requests.get(f"{API}/platformModels", headers=_h(cfg), timeout=20)
    r.raise_for_status()
    models = r.json().get("custom_models") or []
    for want in ("phoenix", "lucid origin", "lucid", "flux"):
        m = next((m for m in models if want in (m.get("name") or "").lower()), None)
        if m:
            _model["id"], _model["name"] = m["id"], m.get("name")
            return m["id"]
    if not models:
        raise IllustrateError("Leonardo не вернул список моделей.")
    _model["id"], _model["name"] = models[0]["id"], models[0].get("name")
    return _model["id"]


def generate(cfg, prompt, out_path, log=print):
    """Рисует один кадр 16:9. Возвращает путь к jpg или None."""
    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 1000:
        return out_path
    try:
        body = {"prompt": f"{prompt.strip().rstrip('.')}, {STYLE}"[:1400], "negative_prompt": NEGATIVE,
                "modelId": model_id(cfg), "width": 1344, "height": 768, "num_images": 1}
        r = requests.post(f"{API}/generations", headers=_h(cfg), json=body, timeout=60)
        if r.status_code == 402 or "credit" in r.text.lower() and r.status_code >= 400:
            raise IllustrateError("на балансе API Leonardo закончились деньги")
        r.raise_for_status()
        gid = (r.json().get("sdGenerationJob") or {}).get("generationId")
        if not gid:
            raise IllustrateError(f"Leonardo не начал рисовать: {r.text[:160]}")
        for k in range(40):
            time.sleep(3 if k < 10 else 6)
            g = requests.get(f"{API}/generations/{gid}", headers=_h(cfg), timeout=30).json().get("generations_by_pk") or {}
            st = g.get("status")
            if st == "COMPLETE":
                imgs = g.get("generated_images") or []
                if not imgs:
                    return None
                img = requests.get(imgs[0]["url"], timeout=120)
                img.raise_for_status()
                tmp = out_path.with_suffix(".part")
                tmp.write_bytes(img.content)
                from PIL import Image
                with Image.open(tmp) as im:
                    rgb = im.convert("RGB")
                rgb.save(out_path, "JPEG", quality=93)
                tmp.unlink(missing_ok=True)
                return out_path
            if st == "FAILED":
                return None
        return None
    except IllustrateError as e:
        log(f"  Рисованный кадр: {e}.")
        raise
    except Exception as e:
        log(f"  Рисованный кадр не получился ({type(e).__name__}: {str(e)[:120]}).")
        return None
