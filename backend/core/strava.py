"""Sincronización de actividades con la API de Strava (insertar/actualizar/borrar)."""
import sqlite3

import requests

import config

DB_PATH = config.DB_PATH


def fetch_and_store(athlete_id, access_token, max_pages=None):
    """Baja el historial del atleta. Sin max_pages pagina hasta vaciar (todo)."""
    new_count = 0
    page = 1
    while max_pages is None or page <= max_pages:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": 100, "page": page},
            timeout=10,
        )
        if not resp.ok:
            break
        acts = resp.json()
        if not acts:
            break
        conn = sqlite3.connect(DB_PATH)
        for act in acts:
            conn.execute("""
                INSERT OR IGNORE INTO activities
                    (strava_id, athlete_id, name, type, distance, moving_time, start_date, elevation_gain)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(act["id"]), str(athlete_id),
                act.get("name", ""),
                act.get("sport_type") or act.get("type", "Other"),
                act.get("distance", 0),
                act.get("moving_time", 0),
                act.get("start_date_local", ""),
                act.get("total_elevation_gain", 0),
            ))
            if conn.total_changes:
                new_count += 1
        conn.commit()
        conn.close()
        page += 1
    return new_count


def fetch_and_store_single(athlete_id, activity_id, access_token):
    resp = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not resp.ok:
        return 0
    act = resp.json()
    conn = sqlite3.connect(DB_PATH)
    # UPSERT: inserta si es nueva, actualiza si ya existe (renombrados,
    # correcciones de distancia/tipo, etc. que llegan como evento 'update').
    conn.execute("""
        INSERT INTO activities
            (strava_id, athlete_id, name, type, distance, moving_time, start_date, elevation_gain)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strava_id) DO UPDATE SET
            name           = excluded.name,
            type           = excluded.type,
            distance       = excluded.distance,
            moving_time    = excluded.moving_time,
            start_date     = excluded.start_date,
            elevation_gain = excluded.elevation_gain
    """, (
        str(act["id"]), str(athlete_id),
        act.get("name", ""),
        act.get("sport_type") or act.get("type", "Other"),
        act.get("distance", 0),
        act.get("moving_time", 0),
        act.get("start_date_local", ""),
        act.get("total_elevation_gain", 0),
    ))
    added = conn.total_changes
    conn.commit()
    conn.close()
    return added


def delete_activity(athlete_id, activity_id):
    """Borra una actividad de la BD (evento 'delete' del webhook)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM activities WHERE strava_id=? AND athlete_id=?",
        (str(activity_id), str(athlete_id))
    )
    removed = conn.total_changes
    conn.commit()
    conn.close()
    return removed
