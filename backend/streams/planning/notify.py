"""Decisión y envío de notificaciones de objetivos.

Dos disparadores, modulados por el nivel de insistencia de cada objetivo:

  Progreso (al guardar una actividad, `on_activity`):
    · nivel 1 (suave)      → solo al COMPLETAR (100%)
    · nivel 2 (normal)     → completar + si el % sube ≥20 pts de golpe
    · nivel 3 (insistente) → completar + cualquier subida del %

  Fin de plazo (periódico, `check_deadlines`):
    · nivel 1 → nada
    · nivel 2 → un aviso cuando queda ≤25% del tiempo y vas por debajo del ritmo
    · nivel 3 → avisos al quedar ≤50%, ≤25% y ≤10% del tiempo (si no está completo)

Cada aviso puntual (completado / fin de plazo) se envía una sola vez: se registra
en la columna `notif_sent` del objetivo.
"""
import logging
from datetime import datetime

from core.db import connect
from core import push
from streams.planning.service import resolve_period, _actual_by_type, fallback_meta

log = logging.getLogger("pulse.notify")

PERIODS = ("week", "month", "year")
_PERIOD_WORD = {"week": "esta semana", "month": "este mes", "year": "este año"}


def _label(name, types):
    name = (name or "").strip()
    if name:
        return name
    return " + ".join(fallback_meta(t)["label"] for t in types)


def _sent_set(s):
    return set(x for x in (s or "").split(",") if x)


def _mark_sent(conn, goal_id, sent, kind):
    sent.add(kind)
    conn.execute("UPDATE goals SET notif_sent=? WHERE id=?",
                 (",".join(sorted(sent)), goal_id))


def _current_goals(conn, athlete_id):
    """Itera los objetivos del atleta que están en su instancia ACTUAL.

    Devuelve dicts con todo lo calculado: pct, actual, fracción de tiempo restante.
    """
    rows = conn.execute("""
        SELECT id, period_type, period_key, activity_types, name,
               target_km, notify_level, last_pct, notif_sent
        FROM goals
        WHERE athlete_id=? AND notify_level > 0
    """, (str(athlete_id),)).fetchall()

    # Rango/actuales por periodo se calculan una vez (no por objetivo).
    cache = {}
    out = []
    now = datetime.now()
    for (gid, ptype, pkey, types_str, name, target, level, last_pct, sent) in rows:
        if ptype not in PERIODS:
            continue
        if ptype not in cache:
            start, end, *_rest, cur = resolve_period(ptype, "")
            actual = _actual_by_type(conn, athlete_id, start, end)
            cache[ptype] = (start, end, cur, actual)
        start, end, cur, actual = cache[ptype]
        if pkey != cur:                 # objetivo de otra semana/mes/año: no avisar
            continue
        types = [t for t in (types_str or "").split(",") if t]
        if not types or not target or target <= 0:
            continue
        a = round(sum(actual.get(t, 0.0) for t in types), 1)
        pct = min(100, int(round(a / target * 100)))
        total = (end - start).total_seconds()
        time_left = max(0.0, (end - now).total_seconds()) / total if total else 0.0
        out.append({
            "id": gid, "level": level, "name": name, "types": types,
            "target": round(target, 1), "actual": a, "pct": pct,
            "last_pct": last_pct or 0, "sent": _sent_set(sent),
            "time_left": time_left, "period": ptype,
        })
    return out


# ── Disparador 1: progreso tras una actividad ─────────────────────────────────

def on_activity(athlete_id):
    if not push.enabled():
        return
    conn = connect()
    try:
        for g in _current_goals(conn, athlete_id):
            label = _label(g["name"], g["types"])
            pct, last = g["pct"], g["last_pct"]

            if pct >= 100 and "done" not in g["sent"]:
                push.send_to_athlete(
                    athlete_id, "🎉 ¡Objetivo completado!",
                    f"«{label}»: {g['actual']:g} km. ¡{g['pct']}%!",
                    tag=f"goal-{g['id']}-done", url="/")
                _mark_sent(conn, g["id"], g["sent"], "done")
            elif pct < 100 and pct > last:
                jump = pct - last
                notify = (g["level"] == 3) or (g["level"] == 2 and jump >= 20)
                if notify:
                    push.send_to_athlete(
                        athlete_id, "🔥 ¡Buen trabajo!",
                        f"«{label}» al {pct}% ({g['actual']:g}/{g['target']:g} km).",
                        tag=f"goal-{g['id']}-prog", url="/")

            if pct != last:
                conn.execute("UPDATE goals SET last_pct=? WHERE id=?", (pct, g["id"]))
        conn.commit()
    finally:
        conn.close()


# ── Disparador 2: se acerca el fin del plazo ──────────────────────────────────

# Umbrales de "tiempo restante" por nivel (fracción del periodo) y su etiqueta.
_DEADLINES = {
    2: [(0.25, "d25")],
    3: [(0.50, "d50"), (0.25, "d25"), (0.10, "d10")],
}


def check_deadlines(athlete_id=None):
    """Revisa objetivos próximos a vencer. Sin athlete_id, recorre todos."""
    if not push.enabled():
        return
    conn = connect()
    try:
        if athlete_id is None:
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT athlete_id FROM goals WHERE notify_level > 0").fetchall()]
        else:
            ids = [str(athlete_id)]

        for aid in ids:
            for g in _current_goals(conn, aid):
                if g["pct"] >= 100 or g["level"] < 2:
                    continue
                thresholds = _DEADLINES.get(g["level"], [])
                # Umbrales ya cruzados (queda ≤ frac del tiempo) y los aún no avisados.
                crossed = [(f, k) for f, k in thresholds if g["time_left"] <= f]
                new = [(f, k) for f, k in crossed if k not in g["sent"]]
                if not new:
                    continue
                elapsed_pct = (1.0 - g["time_left"]) * 100
                behind = g["pct"] < elapsed_pct
                # Nivel 2 solo molesta si vas por debajo del ritmo; nivel 3 siempre.
                if g["level"] == 2 and not behind:
                    continue
                label = _label(g["name"], g["types"])
                falta = round(g["target"] - g["actual"], 1)
                push.send_to_athlete(
                    aid, "⏳ Queda poco",
                    f"«{label}» {_PERIOD_WORD[g['period']]}: vas al {g['pct']}% "
                    f"({g['actual']:g}/{g['target']:g} km, faltan {falta:g}).",
                    tag=f"goal-{g['id']}-{new[-1][1]}", url="/")
                # Marca TODOS los umbrales cruzados (no solo el más ajustado) para
                # que los más holgados no vuelvan a disparar después.
                for _f, k in crossed:
                    _mark_sent(conn, g["id"], g["sent"], k)
        conn.commit()
    finally:
        conn.close()
