#!/usr/bin/env python3
"""Gestiona la suscripción al webhook de Strava para Pulse.

Strava sólo permite UNA suscripción push por aplicación. Sin ella, Strava nunca
avisa a la app de actividades nuevas, así que no se actualizan solas.

Ejecútalo EN LA VPS (lee backend/.env) con el python del venv:

  /opt/strava-app/venv/bin/python3 setup/strava_webhook.py list
  /opt/strava-app/venv/bin/python3 setup/strava_webhook.py create
  /opt/strava-app/venv/bin/python3 setup/strava_webhook.py create https://mi-host/webhook
  /opt/strava-app/venv/bin/python3 setup/strava_webhook.py delete

IMPORTANTE: la app (systemctl) debe estar arrancada y accesible por HTTPS cuando
ejecutes 'create', porque Strava llama al callback para verificarlo al instante.
"""
import os
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV  = os.path.join(HERE, "..", "backend", ".env")
load_dotenv(ENV)

CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
VERIFY_TOKEN  = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
REDIRECT_URI  = os.getenv("REDIRECT_URI", "")

API = "https://www.strava.com/api/v3/push_subscriptions"


def default_callback():
    """Deriva https://<host>/webhook a partir de REDIRECT_URI."""
    p = urlparse(REDIRECT_URI)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}/webhook"


def list_subs(quiet=False):
    r = requests.get(API, params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    if not quiet:
        print(f"[list] {r.status_code}: {r.text}")
    try:
        return r.json() if r.ok else []
    except Exception:
        return []


def create(callback):
    if callback.startswith("http://"):
        print("!! El callback debe ser HTTPS. Strava rechaza HTTP.")
        return
    print(f"[create] callback = {callback}")
    if not VERIFY_TOKEN:
        print("!! WEBHOOK_VERIFY_TOKEN está vacío en .env. Pon un token y reinicia el servicio.")
        return
    r = requests.post(API, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "callback_url":  callback,
        "verify_token":  VERIFY_TOKEN,
    })
    print(f"[create] {r.status_code}: {r.text}")
    if r.ok:
        print("OK. Suscripción creada. Sube una actividad para probar.")
    else:
        print("Fallo. Revisa que la app esté arrancada y accesible por HTTPS, "
              "y que el dominio coincida con el de Strava.")


def delete_all():
    subs = list_subs(quiet=True)
    if not subs:
        print("[delete] no hay suscripciones.")
        return
    for s in subs:
        r = requests.delete(f"{API}/{s['id']}",
                            data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        print(f"[delete] id={s['id']} -> {r.status_code} {r.text or 'borrada'}")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Faltan STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET en backend/.env")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        list_subs()
    elif cmd == "create":
        cb = sys.argv[2] if len(sys.argv) > 2 else default_callback()
        if not cb:
            sys.exit("No pude derivar el callback desde REDIRECT_URI. "
                     "Pásalo a mano: create https://tu-host/webhook")
        delete_all()        # Strava sólo permite una; limpia la anterior
        create(cb)
    elif cmd == "delete":
        delete_all()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
