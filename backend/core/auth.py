"""Rutas de autenticación: OAuth de Strava, login por dispositivo y logout."""
from flask import Blueprint, redirect, request, session, jsonify, current_app
import requests

import config
from core import db
from core.strava import fetch_and_store

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/auth/strava")
def auth_strava():
    url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={config.CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={config.REDIRECT_URI}"
        f"&approval_prompt=force"
        f"&scope=activity:read_all,profile:read_all"
    )
    return redirect(url)


@auth_bp.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        current_app.logger.error("callback sin code: args=%s", dict(request.args))
        return redirect("/")
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })
    if not resp.ok:
        current_app.logger.error("token POST falló: %s %s", resp.status_code, resp.text)
        return redirect("/")
    data = resp.json()
    athlete    = data["athlete"]
    athlete_id = str(athlete["id"])
    firstname  = athlete.get("firstname", "Atleta")
    db.save_tokens(athlete_id, data["access_token"], data["refresh_token"], data["expires_at"], firstname)
    session.permanent = True
    session["athlete_id"] = athlete_id
    session["firstname"]  = firstname
    # Bulk inicial: baja TODO el historial una sola vez por atleta.
    # A partir de ahí, el webhook mantiene la BD al día (fetch_and_store_single).
    if not db.initial_sync_done(athlete_id):
        fetch_and_store(athlete_id, data["access_token"])  # todas las páginas
        db.mark_initial_sync_done(athlete_id)
    # Issue a per-device token the frontend stores in localStorage for auto-login
    dt = db.create_device_token(athlete_id)
    return redirect(f"/?dt={dt}")


@auth_bp.route("/api/device-login", methods=["POST"])
def device_login():
    token = (request.get_json(silent=True) or {}).get("token")
    athlete_id = db.resolve_device_token(token)
    if not athlete_id:
        return jsonify({"ok": False}), 401
    session.permanent = True
    session["athlete_id"] = athlete_id
    session["firstname"]  = db.get_firstname(athlete_id) or "Atleta"
    return jsonify({"ok": True, "firstname": session["firstname"]})


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    # revoke only this device's token, clear the session
    token = (request.get_json(silent=True) or {}).get("token")
    db.revoke_device_token(token)
    session.clear()
    return jsonify({"ok": True})
