"""Configuración central: variables de entorno y rutas del proyecto."""
import os
from dotenv import load_dotenv

load_dotenv()

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR    = os.path.dirname(_BACKEND_DIR)

TEMPLATE_DIR = os.path.join(_ROOT_DIR, "frontend", "templates")
STATIC_DIR   = os.path.join(_ROOT_DIR, "frontend", "static")
DB_PATH      = os.path.join(_BACKEND_DIR, "data", "activities.db")

SECRET_KEY = os.getenv("SECRET_KEY", "change_this_in_production")

# ── Strava ────────────────────────────────────────────────────────────────────
CLIENT_ID            = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET        = os.getenv("STRAVA_CLIENT_SECRET")
REDIRECT_URI         = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

# ── Asistente de voz (OpenAI) ─────────────────────────────────────────────────
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "nova")
