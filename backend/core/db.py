"""Capa de base de datos: esquema, tokens, device tokens y estado de sync."""
import time
import secrets
import sqlite3
from datetime import datetime

import requests

import config

DB_PATH = config.DB_PATH


def connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strava_id       TEXT UNIQUE,
            athlete_id      TEXT,
            name            TEXT,
            type            TEXT,
            distance        REAL,
            moving_time     INTEGER,
            start_date      TEXT,
            elevation_gain  REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            athlete_id    TEXT PRIMARY KEY,
            access_token  TEXT,
            refresh_token TEXT,
            expires_at    INTEGER,
            firstname     TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS device_tokens (
            token       TEXT PRIMARY KEY,
            athlete_id  TEXT,
            created_at  TEXT,
            last_seen   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            athlete_id  TEXT PRIMARY KEY,
            synced_at   TEXT
        )
    """)
    # Stream nutrición: un registro por alimento añadido a un día concreto.
    # `grams` = ración estimada/ajustada; `fat` completa los macros principales.
    c.execute("""
        CREATE TABLE IF NOT EXISTS food_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id  TEXT,
            day         TEXT,
            name        TEXT,
            grams       REAL,
            kcal        REAL,
            protein     REAL,
            carbs       REAL,
            fat         REAL,
            created_at  TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_food_athlete_day ON food_log(athlete_id, day)")

    # Stream planning: un objetivo de distancia (km) anclado a una instancia del
    # periodo (`period_key`: semana "2026-W24" · mes "2026-06" · año "2026").
    # `activity_types` es una lista de tipos separados por comas: el objetivo se
    # cumple con la SUMA de la distancia de todos ellos (p.ej. "TrailRun,Run").
    _migrate_goals(c)
    c.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     TEXT,
            period_type    TEXT,
            period_key     TEXT,
            activity_types TEXT,
            target_km      REAL,
            updated_at     TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_goals_lookup "
              "ON goals(athlete_id, period_type, period_key)")

    # Caché de valores nutricionales por 100 g (Open Food Facts u otra fuente),
    # para no repetir búsquedas del mismo alimento. `key` = nombre normalizado.
    c.execute("""
        CREATE TABLE IF NOT EXISTS food_cache (
            key         TEXT PRIMARY KEY,
            name        TEXT,
            kcal_100    REAL,
            protein_100 REAL,
            carbs_100   REAL,
            fat_100     REAL,
            source      TEXT,
            created_at  TEXT
        )
    """)

    _migrate_food_log(c)
    conn.commit()
    conn.close()


def _migrate_food_log(cursor):
    """Añade columnas nuevas a `food_log` en bases de datos ya existentes."""
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(food_log)").fetchall()}
    for name, decl in (("grams", "REAL"), ("fat", "REAL")):
        if name not in cols:
            cursor.execute(f"ALTER TABLE food_log ADD COLUMN {name} {decl}")


def _migrate_goals(cursor):
    """Convierte el esquema antiguo (`activity_type`, un tipo por objetivo) al
    nuevo (`activity_types`, lista combinada). Cada objetivo antiguo pasa a ser
    un objetivo de un único tipo, conservando su target."""
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(goals)").fetchall()}
    if not cols or "activity_type" not in cols or "activity_types" in cols:
        return
    cursor.execute("ALTER TABLE goals RENAME TO goals_old")
    cursor.execute("""
        CREATE TABLE goals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id     TEXT,
            period_type    TEXT,
            period_key     TEXT,
            activity_types TEXT,
            target_km      REAL,
            updated_at     TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO goals (athlete_id, period_type, period_key, activity_types, target_km, updated_at)
        SELECT athlete_id, period_type, period_key, activity_type, target_km, updated_at FROM goals_old
    """)
    cursor.execute("DROP TABLE goals_old")


# ── Estado de sincronización inicial ──────────────────────────────────────────

def initial_sync_done(athlete_id):
    conn = connect()
    row = conn.execute(
        "SELECT 1 FROM sync_state WHERE athlete_id=?", (str(athlete_id),)
    ).fetchone()
    conn.close()
    return row is not None


def mark_initial_sync_done(athlete_id):
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO sync_state (athlete_id, synced_at) VALUES (?, ?)",
        (str(athlete_id), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ── Tokens de Strava ──────────────────────────────────────────────────────────

def save_tokens(athlete_id, access_token, refresh_token, expires_at, firstname=""):
    conn = connect()
    conn.execute("""
        INSERT INTO tokens (athlete_id, access_token, refresh_token, expires_at, firstname)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(athlete_id) DO UPDATE SET
            access_token  = excluded.access_token,
            refresh_token = excluded.refresh_token,
            expires_at    = excluded.expires_at,
            firstname     = excluded.firstname
    """, (athlete_id, access_token, refresh_token, expires_at, firstname))
    conn.commit()
    conn.close()


def get_firstname(athlete_id):
    conn = connect()
    row = conn.execute(
        "SELECT firstname FROM tokens WHERE athlete_id=?", (str(athlete_id),)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else ""


def get_valid_token(athlete_id):
    """Devuelve un access_token válido, refrescándolo si está por caducar."""
    conn = connect()
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM tokens WHERE athlete_id=?",
        (athlete_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    access_token, refresh_token, expires_at = row
    if time.time() > expires_at - 300:
        resp = requests.post("https://www.strava.com/oauth/token", data={
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        if resp.ok:
            data = resp.json()
            save_tokens(athlete_id, data["access_token"], data["refresh_token"], data["expires_at"])
            return data["access_token"]
        return None
    return access_token


# ── Device tokens (auto-login persistente por dispositivo) ────────────────────

def create_device_token(athlete_id):
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat()
    conn = connect()
    conn.execute(
        "INSERT INTO device_tokens (token, athlete_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
        (token, str(athlete_id), now, now)
    )
    conn.commit()
    conn.close()
    return token


def resolve_device_token(token):
    if not token:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT athlete_id FROM device_tokens WHERE token=?", (token,)
    ).fetchone()
    if row:
        conn.execute("UPDATE device_tokens SET last_seen=? WHERE token=?",
                     (datetime.now().isoformat(), token))
        conn.commit()
    conn.close()
    return row[0] if row else None


def revoke_device_token(token):
    if not token:
        return
    conn = connect()
    conn.execute("DELETE FROM device_tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()
