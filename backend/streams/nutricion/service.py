"""Stream NUTRICIÓN: registro diario de comida y análisis de fotos con visión.

Modelo de datos: cada alimento confirmado añade una fila a `food_log` (un
alimento en un día). El dashboard agrega por día cuatro métricas: kcal, proteínas,
carbohidratos y grasas.

Pipeline de la foto (precisión "basada en datos reales"):
  1. Visión (gpt-4o) DESCOMPONE el plato en una lista de alimentos y estima los
     GRAMOS de cada uno, además de un respaldo de macros por 100 g.
  2. Cada alimento se busca en Open Food Facts (`fooddb`) para obtener macros por
     100 g REALES; si no hay match, se usa el respaldo de la visión.
  3. Macros del alimento = (por 100 g) × gramos / 100.
  4. NO se guarda automáticamente: se devuelven los alimentos detectados para que
     el usuario confirme/ajuste antes de registrar (ver routes).
"""
import json
import base64
import sqlite3
from datetime import datetime

import config
from streams.asistente.service import get_openai
from streams.nutricion import fooddb

DB_PATH = config.DB_PATH

# Metadatos de cada burbuja: etiqueta, color, unidad y valor de referencia
# (un "día completo" típico) que se usa solo para escalar el tamaño de la burbuja.
NUTRI_META = [
    {"key": "kcal",    "label": "Calorías", "unit": "kcal", "color": "#FF5533", "ref": 2500},
    {"key": "protein", "label": "Proteína", "unit": "g",    "color": "#00CFFF", "ref": 180},
    {"key": "carbs",   "label": "Carbos",   "unit": "g",    "color": "#FFD60A", "ref": 350},
    {"key": "fat",     "label": "Grasas",   "unit": "g",    "color": "#A06CFF", "ref": 80},
]

_VISION_PROMPT = (
    "Eres un nutricionista experto en estimación de raciones. En la imagen hay "
    "comida o bebida. Identifica POR SEPARADO cada alimento distinto del plato "
    "(p. ej. arroz, pollo y ensalada son tres alimentos). Para CADA uno estima los "
    "gramos de la ración visible usando como referencia de escala el plato, los "
    "cubiertos, las manos o el vaso. Da también, como respaldo, sus valores "
    "nutricionales por 100 g.\n"
    "Responde SOLO con un objeto JSON con esta forma exacta:\n"
    '{"items": [{"name": string en español corto y genérico (p. ej. "arroz blanco '
    'cocido"), "grams": number (gramos de la ración), "confidence": number entre 0 '
    'y 1, "per100": {"kcal": number, "protein": number, "carbs": number, "fat": '
    "number}}]}.\n"
    "Los valores de per100 son por 100 g de ese alimento, en gramos los macros. "
    "Redondea a enteros. Si en la imagen no hay comida reconocible, devuelve "
    '{"items": []}.'
)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ── Consulta del día ──────────────────────────────────────────────────────────

def get_day(athlete_id, day):
    conn = sqlite3.connect(DB_PATH)
    totals = conn.execute("""
        SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(protein),0),
               COALESCE(SUM(carbs),0), COALESCE(SUM(fat),0), COUNT(*)
        FROM food_log WHERE athlete_id=? AND day=?
    """, (str(athlete_id), day)).fetchone()
    entries = conn.execute("""
        SELECT id, name, grams, kcal, protein, carbs, fat
        FROM food_log WHERE athlete_id=? AND day=?
        ORDER BY id DESC
    """, (str(athlete_id), day)).fetchall()
    conn.close()

    kcal, protein, carbs, fat, count = totals
    values = {"kcal": round(kcal), "protein": round(protein),
              "carbs": round(carbs), "fat": round(fat)}
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
            {"id": e[0], "name": e[1], "grams": round(e[2] or 0),
             "kcal": round(e[3]), "protein": round(e[4]),
             "carbs": round(e[5]), "fat": round(e[6] or 0)}
            for e in entries
        ],
    }


def _weight(value, ref):
    """0..1 para escalar el tamaño de la burbuja (saturando en la referencia)."""
    if not ref:
        return 0.5
    return max(0.0, min(1.0, value / ref))


# ── Registro ──────────────────────────────────────────────────────────────────

def add_entry(athlete_id, day, name, grams, kcal, protein, carbs, fat):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        INSERT INTO food_log (athlete_id, day, name, grams, kcal, protein, carbs, fat, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(athlete_id), day, name, _num(grams), _num(kcal), _num(protein),
          _num(carbs), _num(fat), datetime.now().isoformat()))
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()
    return entry_id


def update_entry(athlete_id, entry_id, kcal, protein, carbs, fat):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE food_log SET kcal=?, protein=?, carbs=?, fat=?
        WHERE id=? AND athlete_id=?
    """, (_num(kcal), _num(protein), _num(carbs), _num(fat), entry_id, str(athlete_id)))
    changed = conn.total_changes
    conn.commit()
    conn.close()
    return changed


def delete_entry(athlete_id, entry_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM food_log WHERE id=? AND athlete_id=?", (entry_id, str(athlete_id)))
    removed = conn.total_changes
    conn.commit()
    conn.close()
    return removed


# ── Análisis de imagen (visión + grounding) ───────────────────────────────────

def analyze_food_photo(image_bytes, mime="image/jpeg", logger=None):
    """Devuelve una lista de alimentos detectados, SIN guardarlos.

    Cada alimento: {name, grams, confidence, per100:{kcal,protein,carbs,fat},
    source, kcal, protein, carbs, fat}. Lanza si la visión falla.
    """
    raw_items = _vision_items(image_bytes, mime)
    result = []
    for it in raw_items:
        name = str(it.get("name", "")).strip()[:80]
        grams = _num(it.get("grams"))
        if not name or grams <= 0:
            continue
        per100 = _clean_per100(it.get("per100"))

        # Grounding en datos reales; si no hay match, usamos el respaldo de visión.
        real = fooddb.lookup_per100(name, logger=logger)
        source = "estimate"
        if real:
            per100 = {k: real[k] for k in ("kcal", "protein", "carbs", "fat")}
            source = real["source"]

        factor = grams / 100.0
        result.append({
            "name":       name,
            "grams":      round(grams, 1),
            "confidence": _clamp01(it.get("confidence")),
            "per100":     per100,
            "source":     source,
            "kcal":       _num(per100["kcal"] * factor),
            "protein":    _num(per100["protein"] * factor),
            "carbs":      _num(per100["carbs"] * factor),
            "fat":        _num(per100["fat"] * factor),
        })
    return result


def _vision_items(image_bytes, mime):
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"
    client = get_openai()
    chat = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=700,
        temperature=0.2,
    )
    raw = chat.choices[0].message.content or "{}"
    data = json.loads(raw)
    items = data.get("items")
    return items if isinstance(items, list) else []


def _clean_per100(p):
    p = p if isinstance(p, dict) else {}
    return {
        "kcal":    _num(p.get("kcal")),
        "protein": _num(p.get("protein")),
        "carbs":   _num(p.get("carbs")),
        "fat":     _num(p.get("fat")),
    }


def _clamp01(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.5
    return round(max(0.0, min(1.0, n)), 2)


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, n), 1)
