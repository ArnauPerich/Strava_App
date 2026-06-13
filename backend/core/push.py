"""Web Push: guardado de suscripciones y envío de notificaciones (VAPID).

iOS solo entrega Web Push cuando la PWA está instalada en la pantalla de inicio
(iOS 16.4+). En el resto de plataformas funciona en el navegador con permiso.
"""
import json
import logging
from datetime import datetime

import config
from core.db import connect

log = logging.getLogger("pulse.push")

try:
    from pywebpush import webpush, WebPushException
    _HAS_WEBPUSH = True
except Exception:                       # pragma: no cover - dependencia opcional
    _HAS_WEBPUSH = False


def enabled():
    """True si hay claves VAPID y la librería disponible para enviar."""
    return bool(_HAS_WEBPUSH and config.VAPID_PRIVATE_KEY and config.VAPID_PUBLIC_KEY)


# ── Suscripciones ─────────────────────────────────────────────────────────────

def save_subscription(athlete_id, sub):
    """Guarda (o refresca) una suscripción. `sub` = objeto PushSubscription JSON."""
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (endpoint and p256dh and auth):
        return False
    conn = connect()
    conn.execute("""
        INSERT INTO push_subscriptions (athlete_id, endpoint, p256dh, auth, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            athlete_id = excluded.athlete_id,
            p256dh     = excluded.p256dh,
            auth       = excluded.auth
    """, (str(athlete_id), endpoint, p256dh, auth, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True


def _delete_endpoint(endpoint):
    conn = connect()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    conn.commit()
    conn.close()


def get_subscriptions(athlete_id):
    conn = connect()
    rows = conn.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE athlete_id=?",
        (str(athlete_id),)
    ).fetchall()
    conn.close()
    return rows


# ── Envío ─────────────────────────────────────────────────────────────────────

def send_to_athlete(athlete_id, title, body, *, tag=None, url="/", data=None):
    """Envía una notificación a todas las suscripciones del atleta.

    Devuelve el nº de envíos correctos. Limpia suscripciones caducadas (404/410).
    """
    if not enabled():
        return 0

    payload = json.dumps({
        "title": title,
        "body": body,
        "tag": tag or "pulse",
        "url": url,
        "data": data or {},
    })
    vapid_claims = {"sub": config.VAPID_SUBJECT}
    sent = 0
    for endpoint, p256dh, auth in get_subscriptions(athlete_id):
        try:
            webpush(
                subscription_info={"endpoint": endpoint,
                                   "keys": {"p256dh": p256dh, "auth": auth}},
                data=payload,
                vapid_private_key=config.VAPID_PRIVATE_KEY,
                vapid_claims=dict(vapid_claims),
                ttl=3600,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                _delete_endpoint(endpoint)        # suscripción muerta
            else:
                log.warning("webpush error (%s): %s", status, e)
        except Exception as e:                    # pragma: no cover
            log.warning("webpush unexpected error: %s", e)
    return sent
