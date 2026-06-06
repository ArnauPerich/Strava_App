"""Extensiones Flask instanciadas aparte para evitar imports circulares.

Los blueprints importan `socketio` desde aquí; `app.py` lo enlaza con
`socketio.init_app(app)` al construir la aplicación.
"""
from flask_socketio import SocketIO

# async_mode="threading" + simple-websocket (Python puro) → WebSocket nativo
# sin necesidad de compilador C.
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
