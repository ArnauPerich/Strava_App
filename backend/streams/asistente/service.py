"""Stream ASISTENTE: cliente OpenAI, prompt y contexto de datos del atleta."""
import sqlite3
from datetime import datetime

import config
from streams.reporte.service import get_stats, fallback_meta

DB_PATH = config.DB_PATH

VOICE_SYSTEM_PROMPT = (
    "Eres PULSE, el asistente de voz personal de una app de actividad deportiva "
    "conectada a Strava. Respondes preguntas sobre las actividades deportivas del "
    "usuario usando EXCLUSIVAMENTE los datos proporcionados abajo. Tu respuesta se "
    "convertirá en audio, así que habla de forma natural, cercana y breve: máximo "
    "2 o 3 frases, sin listas, sin markdown, sin emojis y sin símbolos raros. "
    "Di los números redondeados y con un tono motivador. Responde siempre en español. "
    "Si el dato que piden no está, dilo con naturalidad."
)

_openai_client = None


def get_openai():
    """Construye el cliente de OpenAI de forma perezosa para que la app
    arranque aunque falte la clave."""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


def build_activity_context(athlete_id):
    """Resumen compacto en texto plano de los datos del atleta para el LLM de voz."""
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    fn_row = conn.execute("SELECT firstname FROM tokens WHERE athlete_id=?",
                          (str(athlete_id),)).fetchone()
    firstname = (fn_row[0] if fn_row and fn_row[0] else "atleta")

    parts = [
        f"Nombre del usuario: {firstname}.",
        f"Fecha y hora actual: {now.strftime('%d/%m/%Y %H:%M')}.",
    ]

    for period, lbl in (("week", "esta semana"), ("month", "este mes"), ("year", "este año")):
        s = get_stats(athlete_id, period)
        line = f"Resumen {lbl}: {s['total_km']} km en total."
        for a in s["activities"]:
            line += f" {a['label']}: {a['km']} km, {a['count']} act., {a['hours']} h."
        parts.append(line)

    rows = conn.execute("""
        SELECT name, type, distance, moving_time, start_date, elevation_gain
        FROM activities WHERE athlete_id=? ORDER BY start_date DESC LIMIT 15
    """, (str(athlete_id),)).fetchall()
    conn.close()

    if rows:
        parts.append("Últimas actividades (de más reciente a más antigua):")
        for name, typ, dist, mt, sd, elev in rows:
            meta = fallback_meta(typ)
            day = (sd or "")[:10]
            parts.append(
                f"- {day} {meta['label']} «{name}»: "
                f"{round((dist or 0)/1000, 1)} km, {round((mt or 0)/60)} min, "
                f"{round(elev or 0)} m desnivel."
            )
    return "\n".join(parts)
