"""Grounding nutricional en datos reales: Open Food Facts.

El LLM identifica el alimento y estima los gramos; los valores por 100 g salen
de aquí (datos reales) en vez de inventarlos. Se cachean en `food_cache` para no
repetir la misma búsqueda. Open Food Facts es gratuito y sin clave de API.

Si OFF no encuentra nada usable, devolvemos None y el llamador cae al estimado
por 100 g que dio la propia visión (mejor algo razonable que nada).
"""
import re
import sqlite3
from datetime import datetime

import requests

import config

DB_PATH = config.DB_PATH

# OFF pide un User-Agent identificativo. Búsqueda en el dominio español para que
# los nombres en español casen mejor con los productos.
_OFF_URL = "https://es.openfoodfacts.org/cgi/search.pl"
_HEADERS = {"User-Agent": "PulseStravaApp/1.0 (nutrition lookup)"}
_TIMEOUT = 6


def _normalize(name):
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


# ── Caché ─────────────────────────────────────────────────────────────────────

def _cache_get(key):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT kcal_100, protein_100, carbs_100, fat_100, source FROM food_cache WHERE key=?",
        (key,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"kcal": row[0], "protein": row[1], "carbs": row[2], "fat": row[3], "source": row[4]}


def _cache_put(key, name, per100):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT OR REPLACE INTO food_cache
           (key, name, kcal_100, protein_100, carbs_100, fat_100, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, name, per100["kcal"], per100["protein"], per100["carbs"],
         per100["fat"], per100["source"], datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ── Open Food Facts ───────────────────────────────────────────────────────────

def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _from_off(name):
    """Busca en Open Food Facts y devuelve los macros por 100 g del mejor match."""
    params = {
        "search_terms": name,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": 8,
        "fields": "product_name,nutriments",
    }
    resp = requests.get(_OFF_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    products = resp.json().get("products", []) or []
    for p in products:
        nutr = p.get("nutriments") or {}
        kcal = _num(nutr.get("energy-kcal_100g"))
        if not kcal:  # sin energía no sirve; probamos el siguiente producto
            continue
        return {
            "kcal":    round(kcal, 1),
            "protein": round(_num(nutr.get("proteins_100g")) or 0, 1),
            "carbs":   round(_num(nutr.get("carbohydrates_100g")) or 0, 1),
            "fat":     round(_num(nutr.get("fat_100g")) or 0, 1),
            "source":  "openfoodfacts",
        }
    return None


def lookup_per100(name, logger=None):
    """Macros reales por 100 g para `name`, o None si no se encuentra.

    Orden: caché → Open Food Facts. Nunca lanza: ante cualquier error devuelve
    None para que el llamador use su estimado de respaldo.
    """
    key = _normalize(name)
    if not key:
        return None
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        per100 = _from_off(name)
    except Exception as e:  # noqa: BLE001 — degradamos a fallback, no rompemos
        if logger:
            logger.warning("OFF lookup error for %r: %s", name, e)
        return None
    if per100:
        _cache_put(key, name, per100)
    return per100
