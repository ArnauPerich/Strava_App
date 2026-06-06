"""Stream REPORTE: cálculo de estadísticas agregadas por periodo."""
import sqlite3
from datetime import datetime, timedelta

import config

DB_PATH = config.DB_PATH

ACTIVITY_META = {
    "Run":          {"label": "Carrera",    "color": "#FF5533", "glow": "#FF553366"},
    "TrailRun":     {"label": "Trail",      "color": "#FF8C42", "glow": "#FF8C4266"},
    "Ride":         {"label": "Ciclismo",   "color": "#00CFFF", "glow": "#00CFFF66"},
    "VirtualRide":  {"label": "Virtual",    "color": "#40C4FF", "glow": "#40C4FF55"},
    "Swim":         {"label": "Natación",   "color": "#00FFB2", "glow": "#00FFB266"},
    "Walk":         {"label": "Caminata",   "color": "#FFD60A", "glow": "#FFD60A55"},
    "Hike":         {"label": "Senderismo", "color": "#FFAA00", "glow": "#FFAA0055"},
    "Workout":      {"label": "Gym",        "color": "#BF5FFF", "glow": "#BF5FFF55"},
    "WeightTraining":{"label": "Pesas",     "color": "#9D4EDD", "glow": "#9D4EDD55"},
}


def fallback_meta(act_type):
    return ACTIVITY_META.get(act_type, {"label": act_type, "color": "#888888", "glow": "#88888844"})


def get_stats(athlete_id, period):
    now = datetime.now()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT type, SUM(distance)/1000.0, COUNT(*), SUM(moving_time)
        FROM activities
        WHERE athlete_id=? AND start_date >= ?
        GROUP BY type
        ORDER BY SUM(distance) DESC
    """, (str(athlete_id), start.isoformat())).fetchall()
    conn.close()

    total_km = sum(r[1] for r in rows)
    result = []
    for act_type, km, count, total_time in rows:
        meta = fallback_meta(act_type)
        result.append({
            "type":    act_type,
            "label":   meta["label"],
            "color":   meta["color"],
            "glow":    meta["glow"],
            "km":      round(km, 1),
            "count":   count,
            "hours":   round(total_time / 3600, 1),
        })
    return {"activities": result, "total_km": round(total_km, 1)}
