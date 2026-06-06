"""Stream ASISTENTE: round-trip de voz (audio → Whisper → GPT → TTS → audio)."""
from flask import Blueprint, request, session, jsonify, Response, current_app

import config
from streams.asistente.service import get_openai, generate_answer

asistente_bp = Blueprint("asistente", __name__)


@asistente_bp.route("/api/voice", methods=["POST"])
def api_voice():
    """Voice round-trip: audio in → Whisper → GPT → TTS → audio out."""
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"error": "unauthenticated"}), 401
    if not config.OPENAI_API_KEY:
        return jsonify({"error": "no_api_key"}), 503

    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no_audio"}), 400
    audio_bytes = f.read()
    if not audio_bytes:
        return jsonify({"error": "no_audio"}), 400
    filename = f.filename or "audio.webm"

    client = get_openai()

    # 1 ── Speech to text
    try:
        tr = client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_bytes),
            language="es",
        )
        question = (tr.text or "").strip()
    except Exception as e:
        current_app.logger.error("whisper error: %s", e)
        return jsonify({"error": "stt_failed"}), 502
    if not question:
        return jsonify({"error": "empty"}), 422

    # 2 ── Reasoning: el LLM consulta el historial con tools si hace falta
    history = session.get("voice_history", [])
    try:
        answer = generate_answer(client, athlete_id, question, history,
                                 logger=current_app.logger)
    except Exception as e:
        current_app.logger.error("gpt error: %s", e)
        return jsonify({"error": "llm_failed"}), 502
    if not answer:
        return jsonify({"error": "llm_failed"}), 502

    # keep a short rolling history (text only) for follow-up questions
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    session["voice_history"] = history[-6:]

    # 3 ── Text to speech
    try:
        speech = client.audio.speech.create(
            model="tts-1",
            voice=config.OPENAI_TTS_VOICE,
            input=answer,
            response_format="mp3",
        )
        out = speech.content
    except Exception as e:
        current_app.logger.error("tts error: %s", e)
        return jsonify({"error": "tts_failed"}), 502

    return Response(out, mimetype="audio/mpeg")
