"""Webhook de Strava: verificación (GET) y eventos create/update/delete (POST)."""
from flask import Blueprint, request, jsonify, Response

import config
from core import db
from core.strava import fetch_and_store_single, delete_activity
from extensions import socketio

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhook", methods=["GET"])
def webhook_verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == config.WEBHOOK_VERIFY_TOKEN:
        return jsonify({"hub.challenge": challenge})
    return Response(status=403)


@webhook_bp.route("/webhook", methods=["POST"])
def webhook_event():
    data = request.get_json(silent=True) or {}
    if data.get("object_type") == "activity":
        aspect      = data.get("aspect_type")
        athlete_id  = str(data.get("owner_id", ""))
        activity_id = data.get("object_id")
        if athlete_id and activity_id:
            changed = 0
            if aspect == "delete":
                # No necesita token: solo borramos de la BD local.
                changed = delete_activity(athlete_id, activity_id)
            elif aspect in ("create", "update"):
                token = db.get_valid_token(athlete_id)
                if token:
                    changed = fetch_and_store_single(athlete_id, activity_id, token)
            if changed:
                socketio.emit("refresh", {"athlete_id": athlete_id})
    return jsonify({"status": "ok"})
