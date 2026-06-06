"""Stream NUTRICIÓN: registro diario de comida y análisis de fotos con visión.

Modelo de datos: cada foto identificada añade una fila a `food_log` (un alimento
en un día). El dashboard agrega por día las tres métricas: kcal, proteínas y
carbohidratos. El análisis de la imagen lo hace gpt-4o-mini (visión), el mismo
proveedor que el asistente de voz.
"""
import json
import base64
import sqlite3
from datetime import datetime

import config
from streams.asistente.service import get_openai

DB_PATH = config.DB_PATH

# Metadatos de cada burbuja: etiqueta, color, unidad y valor de referencia
# (un "día completo" típico) que se usa solo para escalar el tamaño de la burbuja.
NUTRI_META = [
    {"key": "kcal",    "label": "Calorías", "unit": "kcal", "color": "#FF5533", "ref": 2500},
    {"key": "protein", "label": "Proteína", "unit": "g",    "color": "#00CFFF", "ref": 180},
    {"key": "carbs",   "label": "Carbos",   "unit": "g",    "color": "#FFD60A", "ref": 350},
]

_VISION_PROMPT = (
    "Eres un nutricionista. En la imagen hay comida o bebida. Identifícala y estima "
    "sus valores nutricionales TOTALES de la ración que se ve. Responde SOLO con un "
    "objeto JSON con estas claves exactas: "
    '{"name": string en español corto, "kcal": number, "protein": number en gramos, '
    '"carbs": number en gramos}. '
    "Redondea a enteros. Si en la imagen no hay comida reconocible, devuelve "
    '{"name": "", "kcal": 0, "protein": 0, "carbs": 0}.'
)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ── Consulta del día ──────────────────────────────────────────────────────────

def get_day(athlete_id, day):
    conn = sqlite3.connect(DB_PATH)
    totals = conn.execute("""
        SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(protein),0), COALESCE(SUM(carbs),0), COUNT(*)
        FROM food_log WHERE athlete_id=? AND day=?
    """, (str(athlete_id), day)).fetchone()
    entries = conn.execute("""
        SELECT id, name, kcal, protein, carbs
        FROM food_log WHERE athlete_id=? AND day=?
        ORDER BY id DESC
    """, (str(athlete_id), day)).fetchall()
    conn.close()

    kcal, protein, carbs, count = totals
    values = {"kcal": round(kcal), "protein": round(protein), "carbs": round(carbs)}
    bubbles = []
    for m in NUTRI_META:
        v = values[m["key"]]
        bubbles.append({
            "key":    m["key"],
            "label":  m["label"],
            "unit":   m["unit"],
            "color":  m["color"],
            "value":  v,
            "weight": _weight(v, m["ref"]),
        })
    return {
        "day": day,
        "count": count,
        "bubbles": bubbles,
        "entries": [
            {"id": e[0], "name": e[1], "kcal": round(e[2]), "protein": round(e[3]), "carbs": round(e[4])}
            for e in entries
        ],
    }


def _weight(value, ref):
    """0..1 para escalar el tamaño de la burbuja (saturando en la referencia)."""
    if not ref:
        return 0.5
    return max(0.0, min(1.0, value / ref))


# ── Registro ──────────────────────────────────────────────────────────────────

def add_entry(athlete_id, day, name, kcal, protein, carbs):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        INSERT INTO food_log (athlete_id, day, name, kcal, protein, carbs, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(athlete_id), day, name, kcal, protein, carbs, datetime.now().isoformat()))
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()
    return entry_id


def delete_entry(athlete_id, entry_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM food_log WHERE id=? AND athlete_id=?", (entry_id, str(athlete_id)))
    removed = conn.total_changes
    conn.commit()
    conn.close()
    return removed


# ── Análisis de imagen (visión) ───────────────────────────────────────────────

def analyze_food_image(image_bytes, mime="image/jpeg"):
    """Devuelve {name, kcal, protein, carbs} a partir de la foto. Lanza en error."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"
    client = get_openai()
    chat = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=200,
        temperature=0.2,
    )
    raw = chat.choices[0].message.content or "{}"
    data = json.loads(raw)
    return {
        "name":    str(data.get("name", "")).strip()[:80],
        "kcal":    _num(data.get("kcal")),
        "protein": _num(data.get("protein")),
        "carbs":   _num(data.get("carbs")),
    }


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, n), 1)
