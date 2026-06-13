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
import random
import logging
from datetime import datetime

from core.db import connect
from core import push
from streams.planning.service import resolve_period, _actual_by_type, fallback_meta

log = logging.getLogger("pulse.notify")

PERIODS = ("week", "month", "year")
_PERIOD_WORD = {"week": "esta semana", "month": "este mes", "year": "este año"}


# ── Plantillas de mensajes ────────────────────────────────────────────────────
# 3 variantes (título, cuerpo) por categoría; al notificar se elige una al azar.
# Edita libremente los textos. Huecos disponibles en cualquier plantilla:
#   {name}    nombre del objetivo (o sus deportes)
#   {pct}     porcentaje actual (entero)
#   {actual}  km hechos        {target} km objetivo        {falta} km que faltan
#   {period}  "esta semana" / "este mes" / "este año"
MESSAGES = {
    # Objetivo completado (100%).
    "done": [
        ("Well done PUSSY!",
         "Bien hecho, un objetivo más cumplido. Pero no te relajes, los hombre de "
         "verdad no se marcan estos objetivos tan flojos..."),
        ("Another day, another victory",
         "Eso es! Otro día, la misma mierda: te levantas, te superas, te acuestas"),
        ("Es la batalla, no la guerra!",
         "Recuerda... ganar una batalla no significa ganar la guerra! No aflojes BITCH!"),
    ],
    # Buen progreso (subió el %).
    "progress": [
        ("STAY HARD!",
         "Muy bien... hoy has sido capaz de mover el puto culo del sofá. ¿Te piensas "
         "que ya está? Falta muuuuchooooooo..."),
        ("Eat or be eaten!",
         "Esa es la actitud! Levantarse cada día, acercarte cada vez más a tu objetivo. "
         "De lo contrario, estás muerto!"),
        ("HAHAHAHAHAHAH",
         "¿Estás contento? ¿Te crees que has conseguido algo importante? Me río de ti "
         "xaval! Sigue y no aflojes!"),
    ],
    # Queda poco: el plazo se acerca y aún no has cumplido (vas tarde).
    "deadline": [
        ("TIC TAC, BITCH",
         "Se te acaba {period} y aún vas al {pct}%. Deja de calentar el sofá y "
         "mueve el puto culo, que te faltan {falta} km."),
        ("The clock is ticking...",
         "El reloj no espera a nadie, y menos a ti. Vas tarde y te queda nada. "
         "¿Vas a apretar o a seguir siendo un mediocre?"),
        ("¿Lo vas a dejar escapar?",
         "Faltan {falta} km y el tiempo se agota. O sudas AHORA o mañana eres otro "
         "FRACASADO más. Tú decides..."),
    ],
    # Fallo: el plazo terminó sin completar el objetivo.
    "fail": [
        ("¿No era suficiente decepcionar a tus padres?",
         "Otro día de tu rutina miserable: decepcionar. Y no era suficiente decepcionar "
         "a dos que encima ahora queda registrado en la app..."),
        ("¿A qué has venido?",
         "Solo una pregunta: ¿estás aquí para pasar el rato y seguir con tu miserable "
         "vida? ¿No? Pues más te vale cumplir tu próximo objetivo."),
        ("Sin comentarios...",
         "Te propones un objetivo de mierda y aún así fracasas. FRACASADO!"),
    ],
}


def _g(n):
    """Formatea un número quitando el .0 sobrante: 11.0 → '11', 1.5 → '1.5'."""
    return format(n, "g")


def _vars(g, label):
    return {
        "name": label,
        "pct": g["pct"],
        "actual": _g(g["actual"]),
        "target": _g(g["target"]),
        "falta": _g(round(g["target"] - g["actual"], 1)),
        "period": _PERIOD_WORD.get(g["period"], ""),
    }


def _pick(category, fmt):
    """Elige una variante al azar de la categoría y la rellena."""
    title, body = random.choice(MESSAGES[category])
    return title, body.format(**fmt)


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
    """Itera los objetivos del atleta que están en su instancia ACTUAL,
    con su pct/actual ya calculados."""
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
                title, body = _pick("done", _vars(g, label))
                push.send_to_athlete(athlete_id, title, body,
                                     tag=f"goal-{g['id']}-done", url="/")
                _mark_sent(conn, g["id"], g["sent"], "done")
            elif pct < 100 and pct > last:
                jump = pct - last
                notify = (g["level"] == 3) or (g["level"] == 2 and jump >= 20)
                if notify:
                    title, body = _pick("progress", _vars(g, label))
                    push.send_to_athlete(athlete_id, title, body,
                                         tag=f"goal-{g['id']}-prog", url="/")

            if pct != last:
                conn.execute("UPDATE goals SET last_pct=? WHERE id=?", (pct, g["id"]))
        conn.commit()
    finally:
        conn.close()


# ── Disparador 2: se acerca el fin del plazo (queda poco) ─────────────────────
# Umbrales de "tiempo restante" por nivel (fracción del periodo) y su etiqueta.
_DEADLINES = {
    2: [(0.25, "d25")],
    3: [(0.50, "d50"), (0.25, "d25"), (0.10, "d10")],
}


def check_deadlines(athlete_id=None):
    """Avisa de objetivos próximos a vencer (aún a tiempo). Niveles 2 y 3."""
    if not push.enabled():
        return
    conn = connect()
    try:
        if athlete_id is None:
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT athlete_id FROM goals WHERE notify_level >= 2").fetchall()]
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
                title, body = _pick("deadline", _vars(g, label))
                push.send_to_athlete(aid, title, body,
                                     tag=f"goal-{g['id']}-{new[-1][1]}", url="/")
                # Marca TODOS los umbrales cruzados (no solo el más ajustado) para
                # que los más holgados no vuelvan a disparar después.
                for _f, k in crossed:
                    _mark_sent(conn, g["id"], g["sent"], k)
        conn.commit()
    finally:
        conn.close()


# ── Disparador 3: el plazo terminó sin cumplir el objetivo (fallo) ────────────
# A diferencia del progreso, mira objetivos cuya instancia (semana/mes/año) YA
# ha terminado y no se completaron. Niveles 2 y 3.

def check_failures(athlete_id=None):
    """Avisa de objetivos vencidos sin completar. Sin athlete_id, recorre todos."""
    if not push.enabled():
        return
    conn = connect()
    try:
        sql = ("SELECT id, athlete_id, period_type, period_key, activity_types, "
               "name, target_km, notif_sent FROM goals "
               "WHERE notify_level >= 2 AND target_km > 0")
        params = ()
        if athlete_id is not None:
            sql += " AND athlete_id=?"
            params = (str(athlete_id),)
        rows = conn.execute(sql, params).fetchall()

        now = datetime.now()
        for (gid, aid, ptype, pkey, types_str, name, target, sent) in rows:
            sentset = _sent_set(sent)
            # Ya completado o ya avisado del fallo: nada que hacer.
            if "done" in sentset or "fail" in sentset:
                continue
            try:
                start, end, *_ = resolve_period(ptype, pkey)
            except Exception:
                continue
            if now < end:                       # el periodo aún no ha terminado
                continue
            types = [t for t in (types_str or "").split(",") if t]
            if not types:
                continue
            actual = _actual_by_type(conn, aid, start, end)
            a = round(sum(actual.get(t, 0.0) for t in types), 1)
            pct = min(100, int(round(a / target * 100))) if target > 0 else 0
            if pct >= 100:                      # lo logró: no es fallo
                _mark_sent(conn, gid, sentset, "done")
                continue
            label = _label(name, types)
            g = {"pct": pct, "actual": a, "target": round(target, 1), "period": ptype}
            title, body = _pick("fail", _vars(g, label))
            push.send_to_athlete(aid, title, body, tag=f"goal-{gid}-fail", url="/")
            _mark_sent(conn, gid, sentset, "fail")
        conn.commit()
    finally:
        conn.close()


def run_periodic():
    """Chequeos del scheduler: "queda poco" (aún a tiempo) + "fallo" (ya vencido)."""
    check_deadlines()
    check_failures()
