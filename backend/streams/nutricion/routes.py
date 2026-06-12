"""Stream NUTRICIÓN: endpoints del dashboard diario, análisis y confirmación."""
import re

from flask import Blueprint, request, session, jsonify, current_app

import config
from streams.nutricion import service

nutricion_bp = Blueprint("nutricion", __name__)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _athlete_or_401():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return None, (jsonify({"error": "unauthenticated"}), 401)
    return athlete_id, None


def _day_param():
    day = request.values.get("day", "") or service.today_str()
    return day if _DAY_RE.match(day) else service.today_str()


@nutricion_bp.route("/api/nutrition")
def api_nutrition():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    return jsonify(service.get_day(athlete_id, _day_param()))


@nutricion_bp.route("/api/nutrition/photo", methods=["POST"])
def api_nutrition_photo():
    """Analiza la foto y DEVUELVE los alimentos detectados, sin guardarlos.

    El usuario los confirma/ajusta en el cliente y luego llama a /confirm.
    """
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    if not config.OPENAI_API_KEY:
        return jsonify({"error": "no_api_key"}), 503

    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "no_photo"}), 400
    image_bytes = f.read()
    if not image_bytes:
        return jsonify({"error": "no_photo"}), 400

    try:
        detected = service.analyze_food_photo(
            image_bytes, f.mimetype or "image/jpeg", logger=current_app.logger)
    except Exception as e:
        current_app.logger.error("nutrition vision error: %s", e)
        return jsonify({"error": "vision_failed"}), 502

    if not detected:
        return jsonify({"error": "no_food"}), 422

    # Calibración (Fase 4): registramos lo detectado para comparar con lo confirmado.
    current_app.logger.info("nutrition detect %s -> %s", athlete_id,
                            [(d["name"], d["grams"], d["source"]) for d in detected])
    return jsonify({"detected": detected})


@nutricion_bp.route("/api/nutrition/photo/confirm", methods=["POST"])
def api_nutrition_confirm():
    """Guarda los alimentos confirmados (posiblemente ajustados) por el usuario."""
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    day = _day_param()
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "no_items"}), 400

    saved = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()[:80]
        if not name:
            continue
        service.add_entry(athlete_id, day, name, it.get("grams"),
                          it.get("kcal"), it.get("protein"),
                          it.get("carbs"), it.get("fat"))
        saved += 1

    current_app.logger.info("nutrition confirm %s %s -> %d items", athlete_id, day, saved)
    result = service.get_day(athlete_id, day)
    result["saved"] = saved
    return jsonify(result)


@nutricion_bp.route("/api/nutrition/entry/update", methods=["POST"])
def api_nutrition_update():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    entry_id = body.get("id")
    day = body.get("day") if _DAY_RE.match(str(body.get("day", ""))) else service.today_str()
    if entry_id is None:
        return jsonify({"error": "no_id"}), 400
    service.update_entry(athlete_id, entry_id, body.get("kcal"),
                         body.get("protein"), body.get("carbs"), body.get("fat"))
    return jsonify(service.get_day(athlete_id, day))


@nutricion_bp.route("/api/nutrition/entry/delete", methods=["POST"])
def api_nutrition_delete():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    entry_id = body.get("id")
    day = body.get("day") if _DAY_RE.match(str(body.get("day", ""))) else service.today_str()
    if entry_id is None:
        return jsonify({"error": "no_id"}), 400
    service.delete_entry(athlete_id, entry_id)
    return jsonify(service.get_day(athlete_id, day))
