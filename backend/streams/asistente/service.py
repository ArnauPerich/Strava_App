"""Stream ASISTENTE: cliente OpenAI, prompt, contexto base y tools de consulta.

En vez de meterle al LLM un contexto estático y limitado (antes: agregados de
semana/mes/año + las 15 últimas actividades), le damos un contexto base mínimo y
un par de TOOLS para que consulte la tabla `activities` bajo demanda. Así puede
responder cosas como "mi actividad más larga" o "km corridos en 2024" sobre TODO
el historial, no solo lo que cupiera en el prompt.

El SQL se construye SIEMPRE en el servidor a partir de parámetros validados
(columnas/órdenes en lista blanca, valores parametrizados, scope por athlete_id).
El LLM nunca escribe SQL.
"""
import json
import sqlite3
from datetime import datetime

import config
from streams.reporte.service import get_stats, fallback_meta

DB_PATH = config.DB_PATH

VOICE_SYSTEM_PROMPT = (
    "Eres PULSE, el asistente de voz personal de una app de actividad deportiva "
    "conectada a Strava. Respondes preguntas sobre las actividades deportivas del "
    "usuario. Dispones de herramientas (tools) para consultar su historial completo: "
    "úsalas siempre que la pregunta requiera datos concretos (la actividad más larga, "
    "totales por año, una actividad puntual, etc.) en lugar de inventar. Basa tu "
    "respuesta EXCLUSIVAMENTE en los datos del contexto base o en lo que devuelvan las "
    "herramientas. Tu respuesta se convertirá en audio, así que habla de forma natural, "
    "cercana y breve: máximo 2 o 3 frases, sin listas, sin markdown, sin emojis y sin "
    "símbolos raros. Di los números redondeados y con un tono motivador. Responde "
    "siempre en español. Si el dato que piden no está, dilo con naturalidad."
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


# ── Contexto base (barato, para preguntas frecuentes sin tool round-trip) ─────

def build_base_context(athlete_id):
    now = datetime.now()
    firstname = _get_firstname(athlete_id)
    parts = [
        f"Nombre del usuario: {firstname}.",
        f"Fecha y hora actual: {now.strftime('%d/%m/%Y %H:%M')}.",
        "Para cualquier dato que no esté aquí (actividad más larga, totales por año, "
        "una actividad concreta, etc.) usa las herramientas disponibles.",
    ]
    for period, lbl in (("week", "esta semana"), ("month", "este mes"), ("year", "este año")):
        s = get_stats(athlete_id, period)
        line = f"Resumen {lbl}: {s['total_km']} km en total."
        for a in s["activities"]:
            line += f" {a['label']}: {a['km']} km, {a['count']} act., {a['hours']} h."
        parts.append(line)
    return "\n".join(parts)


def _get_firstname(athlete_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT firstname FROM tokens WHERE athlete_id=?",
                       (str(athlete_id),)).fetchone()
    conn.close()
    return row[0] if row and row[0] else "atleta"


# ── Tools ─────────────────────────────────────────────────────────────────────

_SPORT_TYPES = "Run, TrailRun, Ride, VirtualRide, Swim, Walk, Hike, Workout, WeightTraining"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_activities",
            "description": (
                "Busca actividades concretas en el historial completo del usuario, "
                "ordenadas por una métrica. Úsala para 'mi actividad más larga', 'la "
                "salida con más desnivel', 'mi última natación', 'las 3 carreras más "
                "rápidas', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sport_type": {
                        "type": "string",
                        "description": (
                            "Tipo tal como lo guarda Strava (en inglés). Valores "
                            f"habituales: {_SPORT_TYPES}. Omítelo para buscar en todos."
                        ),
                    },
                    "date_from": {"type": "string", "description": "Fecha inicio inclusive, YYYY-MM-DD. Opcional."},
                    "date_to":   {"type": "string", "description": "Fecha fin inclusive, YYYY-MM-DD. Opcional."},
                    "sort_by": {
                        "type": "string",
                        "enum": ["distance", "elevation", "time", "date"],
                        "description": "Métrica de ordenación.",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["desc", "asc"],
                        "description": "desc = mayor/más reciente primero (por defecto).",
                    },
                    "limit": {"type": "integer", "description": "Cuántas devolver (1-25, por defecto 5)."},
                },
                "required": ["sort_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_activities",
            "description": (
                "Calcula totales, promedios o conteos sobre el historial completo. "
                "Úsala para 'cuántos km he corrido este año', 'cuántas actividades en "
                "total', 'desnivel acumulado', 'distancia media de mis nataciones', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": ["total_distance", "total_elevation", "total_time", "count", "avg_distance"],
                        "description": "total_distance/avg_distance en km, total_elevation en m, total_time en horas.",
                    },
                    "sport_type": {"type": "string", "description": f"Opcional. Valores: {_SPORT_TYPES}."},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD. Opcional."},
                    "date_to":   {"type": "string", "description": "YYYY-MM-DD. Opcional."},
                    "group_by": {
                        "type": "string",
                        "enum": ["type", "month", "year"],
                        "description": "Opcional, para desglosar el resultado.",
                    },
                },
                "required": ["metric"],
            },
        },
    },
]

_SORT_COLS = {"distance": "distance", "elevation": "elevation_gain", "time": "moving_time", "date": "start_date"}
_METRICS = {
    "total_distance":  "ROUND(SUM(distance)/1000.0, 1)",
    "total_elevation": "ROUND(SUM(elevation_gain))",
    "total_time":      "ROUND(SUM(moving_time)/3600.0, 1)",
    "count":           "COUNT(*)",
    "avg_distance":    "ROUND(AVG(distance)/1000.0, 1)",
}
_GROUP_EXPR = {"type": "type", "month": "strftime('%Y-%m', start_date)", "year": "strftime('%Y', start_date)"}


def _build_filters(athlete_id, args):
    where = ["athlete_id=?"]
    params = [str(athlete_id)]
    if args.get("sport_type"):
        where.append("LOWER(type)=LOWER(?)")
        params.append(args["sport_type"])
    if args.get("date_from"):
        where.append("date(start_date) >= date(?)")
        params.append(args["date_from"])
    if args.get("date_to"):
        where.append("date(start_date) <= date(?)")
        params.append(args["date_to"])
    return " AND ".join(where), params


def _fmt_activity(row):
    name, typ, sd, dist, mt, elev = row
    return {
        "name": name,
        "type": typ,
        "label": fallback_meta(typ)["label"],
        "date": (sd or "")[:10],
        "distance_km": round((dist or 0) / 1000, 1),
        "moving_time_min": round((mt or 0) / 60),
        "elevation_gain_m": round(elev or 0),
    }


def _search_activities(athlete_id, args):
    col = _SORT_COLS.get(args.get("sort_by"), "distance")
    order = "ASC" if str(args.get("order", "desc")).lower() == "asc" else "DESC"
    try:
        limit = max(1, min(int(args.get("limit", 5)), 25))
    except (TypeError, ValueError):
        limit = 5
    where, params = _build_filters(athlete_id, args)
    sql = (
        "SELECT name, type, start_date, distance, moving_time, elevation_gain "
        f"FROM activities WHERE {where} ORDER BY {col} {order} LIMIT {limit}"
    )
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"results": [_fmt_activity(r) for r in rows]}


def _aggregate_activities(athlete_id, args):
    metric_sql = _METRICS.get(args.get("metric"))
    if not metric_sql:
        return {"error": "metric no válida"}
    where, params = _build_filters(athlete_id, args)
    group = _GROUP_EXPR.get(args.get("group_by"))
    conn = sqlite3.connect(DB_PATH)
    if group:
        rows = conn.execute(
            f"SELECT {group} AS g, {metric_sql} FROM activities WHERE {where} "
            f"GROUP BY g ORDER BY g", params
        ).fetchall()
        conn.close()
        return {"metric": args["metric"], "groups": [{"group": g, "value": v} for g, v in rows]}
    row = conn.execute(f"SELECT {metric_sql} FROM activities WHERE {where}", params).fetchone()
    conn.close()
    return {"metric": args["metric"], "value": row[0] if row else 0}


def run_tool(name, args, athlete_id):
    """Despacha una tool. Robusto a argumentos inesperados o malos."""
    if not isinstance(args, dict):
        args = {}
    try:
        if name == "search_activities":
            return _search_activities(athlete_id, args)
        if name == "aggregate_activities":
            return _aggregate_activities(athlete_id, args)
    except Exception as e:  # noqa: BLE001 — devolvemos el error al modelo, no rompemos
        return {"error": str(e)}
    return {"error": f"tool desconocida: {name}"}


# ── Orquestación del LLM con tool-calling ─────────────────────────────────────

def generate_answer(client, athlete_id, question, history, logger=None):
    """Responde la pregunta usando tools si hace falta. Devuelve texto plano."""
    context = build_base_context(athlete_id)
    messages = [{"role": "system", "content": VOICE_SYSTEM_PROMPT + "\n\nCONTEXTO BASE:\n" + context}]
    messages += history
    messages.append({"role": "user", "content": question})

    for _ in range(4):  # como mucho 4 rondas de tools
        chat = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            temperature=0.5,
            max_tokens=300,
        )
        msg = chat.choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip()

        # Reconstruimos el mensaje del asistente con sus tool_calls como dicts.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(tc.function.name, args, athlete_id)
            if logger:
                logger.info("voice tool %s(%s) -> %s", tc.function.name, args, result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # Si tras varias rondas sigue pidiendo tools, forzamos una respuesta final.
    final = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tool_choice="none",
        max_tokens=300,
    )
    return (final.choices[0].message.content or "").strip()
