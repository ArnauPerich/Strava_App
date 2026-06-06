"""Stream NUTRICIÓN: endpoints del dashboard diario y subida de fotos."""
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

    day = _day_param()
    try:
        food = service.analyze_food_image(image_bytes, f.mimetype or "image/jpeg")
    except Exception as e:
        current_app.logger.error("nutrition vision error: %s", e)
        return jsonify({"error": "vision_failed"}), 502

    if not food["name"] and food["kcal"] == 0:
        return jsonify({"error": "no_food"}), 422

    service.add_entry(athlete_id, day, food["name"],
                      food["kcal"], food["protein"], food["carbs"])
    current_app.logger.info("nutrition add %s %s -> %s", athlete_id, day, food)
    # Devolvemos el día actualizado + el alimento detectado para feedback inmediato.
    result = service.get_day(athlete_id, day)
    result["added"] = food
    return jsonify(result)


@nutricion_bp.route("/api/nutrition/entry/delete", methods=["POST"])
def api_nutrition_delete():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    entry_id = body.get("id")
    day = body.get("day") or service.today_str()
    if not _DAY_RE.match(day):
        day = service.today_str()
    if entry_id is None:
        return jsonify({"error": "no_id"}), 400
    service.delete_entry(athlete_id, entry_id)
    return jsonify(service.get_day(athlete_id, day))
