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


def _canon_types(types):
    """Normaliza una lista de tipos: válidos, sin duplicados y en orden estable."""
    order = list(ACTIVITY_META.keys())
    seen = []
    for t in types or []:
        if t in ACTIVITY_META and t not in seen:
            seen.append(t)
    seen.sort(key=lambda t: order.index(t))
    return seen


def get_plan(athlete_id, period_type, key):
    start, end, label, prev, nxt, canonical = resolve_period(period_type, key)

    conn = sqlite3.connect(DB_PATH)
    actual = _actual_by_type(conn, athlete_id, start, end)
    rows = conn.execute("""
        SELECT id, activity_types, name, target_km
        FROM goals
        WHERE athlete_id=? AND period_type=? AND period_key=?
    """, (str(athlete_id), period_type, canonical)).fetchall()
    conn.close()

    order = list(ACTIVITY_META.keys())

    def sort_key(row):
        types = [t for t in (row[1] or "").split(",") if t]
        return order.index(types[0]) if types and types[0] in order else len(order)

    goals = []
    tgt_sum = 0.0
    done_sum = 0.0
    for gid, types_str, name, target_km in sorted(rows, key=sort_key):
        types = [t for t in (types_str or "").split(",") if t]
        if not types:
            continue
        target = round(target_km, 1)
        # El objetivo se cumple con la SUMA de la distancia de todos sus tipos.
        a = round(sum(actual.get(t, 0.0) for t in types), 1)
        pct = min(100, round(a / target * 100)) if target > 0 else 0
        types_label = " + ".join(fallback_meta(t)["label"] for t in types)
        name = (name or "").strip()
        color = fallback_meta(types[0])["color"]
        goals.append({
            "id": gid, "types": types, "color": color,
            "name": name,
            "label": name or types_label,   # nombre propio, o los deportes si no hay
            "sub": types_label if name else "",
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


def set_goal(athlete_id, period_type, key, types, target_km, name="", notify_level=0):
    """Crea un objetivo combinado con nombre y nivel de notificación opcionales."""
    start, end, _, _, _, canonical = resolve_period(period_type, key)
    type_list = _canon_types(types)
    if not type_list or target_km is None or target_km <= 0:
        return get_plan(athlete_id, period_type, canonical)
    types_str = ",".join(type_list)
    name = (name or "").strip()[:40]
    try:
        notify_level = max(0, min(3, int(notify_level)))
    except (TypeError, ValueError):
        notify_level = 0

    conn = sqlite3.connect(DB_PATH)
    try:
        # last_pct inicial = progreso ya existente, para no disparar "subió" al crear.
        actual = _actual_by_type(conn, athlete_id, start, end)
        a = sum(actual.get(t, 0.0) for t in type_list)
        target = round(float(target_km), 2)
        last_pct = min(100, int(round(a / target * 100))) if target > 0 else 0
        conn.execute("""
            INSERT INTO goals (athlete_id, period_type, period_key, activity_types,
                               name, target_km, updated_at, notify_level, last_pct, notif_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
        """, (str(athlete_id), period_type, canonical, types_str, name,
              target, datetime.now().isoformat(), notify_level, last_pct))
        conn.commit()
    finally:
        conn.close()
    return get_plan(athlete_id, period_type, canonical)


def update_goal(athlete_id, period_type, key, goal_id, target_km):
    """Cambia el target de un objetivo existente por id. target_km <= 0 lo borra."""
    _, _, _, _, _, canonical = resolve_period(period_type, key)
    conn = sqlite3.connect(DB_PATH)
    try:
        if target_km is None or target_km <= 0:
            conn.execute("DELETE FROM goals WHERE id=? AND athlete_id=?",
                         (goal_id, str(athlete_id)))
        else:
            conn.execute("UPDATE goals SET target_km=?, updated_at=? WHERE id=? AND athlete_id=?",
                         (round(float(target_km), 2), datetime.now().isoformat(),
                          goal_id, str(athlete_id)))
        conn.commit()
    finally:
        conn.close()
    return get_plan(athlete_id, period_type, canonical)
