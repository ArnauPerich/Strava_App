"""Stream REPORTE: endpoint de estadísticas para el dashboard."""
from flask import Blueprint, request, session, jsonify

from streams.reporte.service import get_stats

reporte_bp = Blueprint("reporte", __name__)


@reporte_bp.route("/api/stats")
def api_stats():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"error": "unauthenticated"}), 401
    period = request.args.get("period", "week")
    return jsonify(get_stats(athlete_id, period))
