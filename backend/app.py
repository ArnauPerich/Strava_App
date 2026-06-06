"""Pulse – punto de entrada. Crea la app Flask, registra los streams y arranca.

Estructura:
  core/       infraestructura compartida (BD, Strava, auth, webhook)
  reporte/    stream del dashboard de estadísticas
  asistente/  stream del asistente de voz
"""
import logging
from datetime import timedelta

from flask import Flask, render_template, session, Response, send_from_directory

import config
from extensions import socketio
from core.db import init_db
from core.auth import auth_bp
from core.webhook import webhook_bp
from streams.reporte.routes import reporte_bp
from streams.asistente.routes import asistente_bp


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


app = Flask(__name__, template_folder=config.TEMPLATE_DIR)
app.secret_key = config.SECRET_KEY
# Long-lived, persistent session cookie (helps when the device keeps the cookie)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

socketio.init_app(app)

# ── Streams ───────────────────────────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(reporte_bp)
app.register_blueprint(asistente_bp)


# ── Rutas generales (shell de la web) ─────────────────────────────────────────
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
    return send_from_directory(config.STATIC_DIR, "apple-touch-icon.png")


init_db()

if __name__ == "__main__":
    socketio.run(app, debug=True, use_reloader=False,
                 host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
