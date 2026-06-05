import os
import time
import secrets
import logging
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, redirect, request, session, jsonify, Response, send_from_directory
from flask_socketio import SocketIO
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Suppress the spurious Werkzeug log when simple-websocket hijacks the socket ──
# The WebSocket upgrade works, but Werkzeug's dev server logs a false 500 +
# "write() before start_response" traceback afterwards. This filter drops only
# those specific records; every real error still gets through.
class _WSHijackFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "write() before start_response" in msg:
            return False
        if "transport=websocket" in msg and " 500 " in msg:
            return False
        return True

logging.getLogger("werkzeug").addFilter(_WSHijackFilter())

_BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR     = os.path.dirname(_BACKEND_DIR)
_TEMPLATE_DIR = os.path.join(_ROOT_DIR, "frontend", "templates")
_STATIC_DIR   = os.path.join(_ROOT_DIR, "frontend", "static")

app = Flask(__name__, template_folder=_TEMPLATE_DIR)
app.secret_key = os.getenv("SECRET_KEY", "change_this_in_production")
# Long-lived, persistent session cookie (helps when the device keeps the cookie)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# async_mode="threading" + simple-websocket (pure Python) → native WebSocket, no C compiler needed
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
DB_PATH       = os.path.join(_BACKEND_DIR, "data", "activities.db")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

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


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    conn.close()


def save_tokens(athlete_id, access_token, refresh_token, expires_at, firstname=""):
    conn = sqlite3.connect(DB_PATH)
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


# ── Device tokens (persistent auto-login per device) ──────────────────────────

def create_device_token(athlete_id):
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM device_tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()


def get_valid_token(athlete_id):
    conn = sqlite3.connect(DB_PATH)
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
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        if resp.ok:
            data = resp.json()
            save_tokens(athlete_id, data["access_token"], data["refresh_token"], data["expires_at"])
            return data["access_token"]
        return None
    return access_token


# ── Strava fetching ───────────────────────────────────────────────────────────

def fetch_and_store(athlete_id, access_token, pages=2):
    new_count = 0
    for page in range(1, pages + 1):
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
    added = conn.total_changes
    conn.commit()
    conn.close()
    return added


# ── Stats query ───────────────────────────────────────────────────────────────

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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
        authenticated=bool(session.get("athlete_id")),
        firstname=session.get("firstname", ""),
    )

# Silence the icon requests iOS/Safari fire automatically
@app.route("/favicon.ico")
@app.route("/apple-touch-icon-precomposed.png")
@app.route("/apple-touch-icon-120x120.png")
@app.route("/apple-touch-icon-120x120-precomposed.png")
def _icons():
    return Response(status=204)

@app.route("/apple-touch-icon.png")
def _apple_touch_icon():
    return send_from_directory(_STATIC_DIR, "apple-touch-icon.png")

@app.route("/auth/strava")
def auth_strava():
    url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&approval_prompt=force"
        f"&scope=activity:read_all,profile:read_all"
    )
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })
    if not resp.ok:
        return redirect("/")
    data = resp.json()
    athlete    = data["athlete"]
    athlete_id = str(athlete["id"])
    firstname  = athlete.get("firstname", "Atleta")
    save_tokens(athlete_id, data["access_token"], data["refresh_token"], data["expires_at"], firstname)
    session.permanent = True
    session["athlete_id"] = athlete_id
    session["firstname"]  = firstname
    # Initial bulk fetch
    fetch_and_store(athlete_id, data["access_token"], pages=10)
    # Issue a per-device token the frontend stores in localStorage for auto-login
    dt = create_device_token(athlete_id)
    return redirect(f"/?dt={dt}")

@app.route("/api/device-login", methods=["POST"])
def device_login():
    token = (request.get_json(silent=True) or {}).get("token")
    athlete_id = resolve_device_token(token)
    if not athlete_id:
        return jsonify({"ok": False}), 401
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT firstname FROM tokens WHERE athlete_id=?", (athlete_id,)).fetchone()
    conn.close()
    session.permanent = True
    session["athlete_id"] = athlete_id
    session["firstname"]  = row[0] if row else "Atleta"
    return jsonify({"ok": True, "firstname": session["firstname"]})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/api/logout", methods=["POST"])
def api_logout():
    # revoke only this device's token, clear the session
    token = (request.get_json(silent=True) or {}).get("token")
    revoke_device_token(token)
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/stats")
def api_stats():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"error": "unauthenticated"}), 401
    period = request.args.get("period", "week")
    return jsonify(get_stats(athlete_id, period))

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        return jsonify({"hub.challenge": challenge})
    return Response(status=403)

@app.route("/webhook", methods=["POST"])
def webhook_event():
    data = request.get_json(silent=True) or {}
    if data.get("object_type") == "activity" and data.get("aspect_type") in ("create", "update"):
        athlete_id  = str(data.get("owner_id", ""))
        activity_id = data.get("object_id")
        if athlete_id and activity_id:
            token = get_valid_token(athlete_id)
            if token:
                added = fetch_and_store_single(athlete_id, activity_id, token)
                if added:
                    socketio.emit("refresh", {"athlete_id": athlete_id})
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    socketio.run(app, debug=True, use_reloader=False,
                 host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)