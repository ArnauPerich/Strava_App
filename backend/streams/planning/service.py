"""Stream PLANNING: objetivos por periodo (semana/mes/año) y su progreso.

Cada objetivo está anclado a una INSTANCIA concreta del periodo mediante una
clave canónica (`period_key`):
  - semana:  "2026-W24"  (año ISO + número de semana ISO)
  - mes:     "2026-06"
  - año:     "2026"

Así, "2 km de natación esta semana" y "3 km la semana que viene" son dos filas
distintas. El progreso real se agrega desde la tabla `activities` en el rango de
fechas de esa instancia.
"""
import sqlite3
from datetime import datetime, timedelta

import config
from streams.reporte.service import ACTIVITY_META, fallback_meta

DB_PATH = config.DB_PATH

PERIODS = ("week", "month", "year")

_MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun",
                 "jul", "ago", "sep", "oct", "nov", "dic"]
_MONTHS_LONG = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# ── Resolución de la instancia del periodo ────────────────────────────────────

def _week_key(d):
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_label(start):
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS_SHORT[start.month - 1]}"
    return (f"{start.day} {_MONTHS_SHORT[start.month - 1]} – "
            f"{end.day} {_MONTHS_SHORT[end.month - 1]}")


def resolve_period(period_type, key):
    """Devuelve (start, end, label, prev_key, next_key, canonical_key).

    `start`/`end` delimitan el rango [start, end). Si `key` es inválida o vacía,
    se usa la instancia que contiene a "ahora".
    """
    if period_type not in PERIODS:
        period_type = "week"
    now = datetime.now()

    if period_type == "week":
        try:
            y, w = key.split("-W")
            monday = datetime.strptime(f"{int(y)} {int(w)} 1", "%G %V %u")
        except Exception:
            monday = now - timedelta(days=now.weekday())
        start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        prev = _week_key(start - timedelta(days=7))
        nxt = _week_key(start + timedelta(days=7))
        return start, end, _week_label(start), prev, nxt, _week_key(start)

    if period_type == "month":
        try:
            y, m = map(int, key.split("-"))
            datetime(y, m, 1)
        except Exception:
            y, m = now.year, now.month
        start = datetime(y, m, 1)
        end = datetime(y + (1 if m == 12 else 0), (m % 12) + 1, 1)
        pm, py = (12, y - 1) if m == 1 else (m - 1, y)
        nm, ny = (1, y + 1) if m == 12 else (m + 1, y)
        label = f"{_MONTHS_LONG[m - 1]} {y}"
        return start, end, label, f"{py}-{pm:02d}", f"{ny}-{nm:02d}", f"{y}-{m:02d}"

    # year
    try:
        y = int(key)
        datetime(y, 1, 1)
    except Exception:
        y = now.year
    start = datetime(y, 1, 1)
    end = datetime(y + 1, 1, 1)
    return start, end, str(y), str(y - 1), str(y + 1), str(y)


# ── Agregados desde activities ────────────────────────────────────────────────

def _actual_by_type(conn, athlete_id, start, end):
    rows = conn.execute("""
        SELECT type, SUM(distance)/1000.0
        FROM activities
        WHERE athlete_id=? AND start_date >= ? AND start_date < ?
        GROUP BY type
    """, (str(athlete_id), start.isoformat(), end.isoformat())).fetchall()
    return {t: (km or 0.0) for t, km in rows}


# ── API del stream ────────────────────────────────────────────────────────────

def all_types():
    """Catálogo de tipos de actividad disponibles para fijar objetivos."""
    out = []
    for atype, meta in ACTIVITY_META.items():
        out.append({"type": atype, "label": meta["label"], "color": meta["color"]})
    return out


def get_plan(athlete_id, period_type, key):
    start, end, label, prev, nxt, canonical = resolve_period(period_type, key)

    conn = sqlite3.connect(DB_PATH)
    actual = _actual_by_type(conn, athlete_id, start, end)
    rows = conn.execute("""
        SELECT activity_type, target_km
        FROM goals
        WHERE athlete_id=? AND period_type=? AND period_key=?
    """, (str(athlete_id), period_type, canonical)).fetchall()
    conn.close()

    targets = {atype: tgt for atype, tgt in rows}

    # Orden estable según ACTIVITY_META; tipos desconocidos al final.
    order = list(ACTIVITY_META.keys())
    ordered = sorted(targets.keys(),
                     key=lambda t: order.index(t) if t in order else len(order))

    goals = []
    tgt_sum = 0.0
    done_sum = 0.0
    for atype in ordered:
        target = round(targets[atype], 1)
        a = round(actual.get(atype, 0.0), 1)
        pct = min(100, round(a / target * 100)) if target > 0 else 0
        meta = fallback_meta(atype)
        goals.append({
            "type": atype, "label": meta["label"], "color": meta["color"],
            "target": target, "actual": a, "pct": pct,
        })
        tgt_sum += target
        done_sum += min(a, target)

    total_pct = round(done_sum / tgt_sum * 100) if tgt_sum > 0 else 0

    return {
        "period": period_type,
        "key": canonical,
        "label": label,
        "prev": prev,
        "next": nxt,
        "goals": goals,
        "all_types": all_types(),
        "total_pct": total_pct,
    }


def set_goal(athlete_id, period_type, key, activity_type, target_km):
    """Crea/actualiza un objetivo. target_km <= 0 lo elimina. Devuelve el plan."""
    _, _, _, _, _, canonical = resolve_period(period_type, key)
    conn = sqlite3.connect(DB_PATH)
    try:
        if target_km is None or target_km <= 0:
            conn.execute("""
                DELETE FROM goals
                WHERE athlete_id=? AND period_type=? AND period_key=? AND activity_type=?
            """, (str(athlete_id), period_type, canonical, activity_type))
        else:
            conn.execute("""
                INSERT INTO goals (athlete_id, period_type, period_key, activity_type, target_km, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id, period_type, period_key, activity_type)
                DO UPDATE SET target_km = excluded.target_km, updated_at = excluded.updated_at
            """, (str(athlete_id), period_type, canonical, activity_type,
                  round(float(target_km), 2), datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()
    return get_plan(athlete_id, period_type, canonical)
