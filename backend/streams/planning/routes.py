"""Stream PLANNING: endpoints para leer y fijar objetivos por periodo."""
from flask import Blueprint, request, session, jsonify

from streams.planning import service

planning_bp = Blueprint("planning", __name__)

_VALID_TYPES = {t["type"] for t in service.all_types()}


def _athlete_or_401():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return None, (jsonify({"error": "unauthenticated"}), 401)
    return athlete_id, None


def _period_param():
    p = request.values.get("period", "week")
    return p if p in service.PERIODS else "week"


@planning_bp.route("/api/planning")
def api_planning():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    key = request.args.get("key", "")
    return jsonify(service.get_plan(athlete_id, _period_param(), key))


@planning_bp.route("/api/planning/goal", methods=["POST"])
def api_planning_goal():
    athlete_id, err = _athlete_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}

    period = body.get("period")
    if period not in service.PERIODS:
        return jsonify({"error": "bad_period"}), 400

    key = str(body.get("key", ""))

    target = body.get("target")
    if target is not None:
        try:
            target = float(target)
        except (TypeError, ValueError):
            return jsonify({"error": "bad_target"}), 400

    # Editar/borrar un objetivo existente por id.
    if body.get("id") is not None:
        try:
            goal_id = int(body["id"])
        except (TypeError, ValueError):
            return jsonify({"error": "bad_id"}), 400
        return jsonify(service.update_goal(athlete_id, period, key, goal_id, target))

    # Crear un objetivo combinado a partir de una lista de tipos.
    types = body.get("types")
    if not isinstance(types, list):
        return jsonify({"error": "bad_types"}), 400
    types = [t for t in types if t in _VALID_TYPES]
    if not types:
        return jsonify({"error": "bad_types"}), 400

    return jsonify(service.set_goal(athlete_id, period, key, types, target))
