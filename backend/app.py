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
from streams.nutricion.routes import nutricion_bp
from streams.planning.routes import planning_bp


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


app = Flask(__name__, template_folder=config.TEMPLATE_DIR,
            static_folder=config.STATIC_DIR, static_url_path="/static")
app.secret_key = config.SECRET_KEY
# Long-lived, persistent session cookie (helps when the device keeps the cookie)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

socketio.init_app(app)

# ── Logging ───────────────────────────────────────────────────────────────────
# Hacemos que app.logger emita a nivel INFO (incluidas las llamadas a tools del
# asistente). Bajo gunicorn reutilizamos sus handlers para que todo acabe en
# /var/log/strava-app/error.log; en local (python app.py) usamos la consola.
_gunicorn_logger = logging.getLogger("gunicorn.error")
if _gunicorn_logger.handlers:
    app.logger.handlers = _gunicorn_logger.handlers
    app.logger.setLevel(_gunicorn_logger.level or logging.INFO)
else:
    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)

# ── Streams ───────────────────────────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(reporte_bp)
app.register_blueprint(asistente_bp)
app.register_blueprint(nutricion_bp)
app.register_blueprint(planning_bp)


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


# ── PWA ───────────────────────────────────────────────────────────────────────
# El service worker DEBE servirse desde la raíz para que su scope controle "/".
@app.route("/sw.js")
def _service_worker():
    resp = send_from_directory(config.STATIC_DIR, "sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache"  # que el navegador detecte updates
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/manifest.webmanifest")
def _manifest():
    resp = send_from_directory(config.STATIC_DIR, "manifest.webmanifest")
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


init_db()


# ── Scheduler de notificaciones (fin de plazo) ────────────────────────────────
# Hilo de fondo que revisa cada 30 min si algún objetivo está por vencer. Es
# seguro porque desplegamos con gunicorn --workers 1 (un único proceso).
def _start_deadline_scheduler(interval=1800):
    import threading, time

    def loop():
        time.sleep(60)  # margen tras el arranque
        from streams.planning import notify
        while True:
            try:
                notify.check_deadlines()
            except Exception as e:
                app.logger.warning("check_deadlines error: %s", e)
            time.sleep(interval)

    threading.Thread(target=loop, name="deadline-scheduler", daemon=True).start()


_start_deadline_scheduler()

if __name__ == "__main__":
    socketio.run(app, debug=True, use_reloader=False,
                 host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
