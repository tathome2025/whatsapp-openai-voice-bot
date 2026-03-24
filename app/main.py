from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.openai_client import OpenAIClient
from app.whatsapp import WhatsAppClient, extract_audio_messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
whatsapp = WhatsAppClient(settings)
openai = OpenAIClient(settings)

app = FastAPI(title="WhatsApp OpenAI Voice Bot", version="0.1.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "whatsapp-openai-voice-bot"}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "whatsapp": await whatsapp.health_check(),
        "openai": await openai.health_check(),
    }


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> str:
    mode = request.query_params.get("hub.mode")
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and verify_token == settings.whatsapp_verify_token:
        return challenge or ""

    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, int | str]:
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")

    if not _verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    audio_messages = extract_audio_messages(payload)
    if not audio_messages:
        return {"status": "ok", "processed": 0, "ignored": 1, "failed": 0}

    processed = 0
    failed = 0

    for item in audio_messages:
        try:
            await _handle_audio_message(item)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            chat_id = item.get("chat_id", "")
            logger.exception("Failed to process audio for chat=%s: %s", chat_id, exc)
            if chat_id:
                await _safe_send_error(chat_id)

    return {
        "status": "ok",
        "processed": processed,
        "ignored": 0,
        "failed": failed,
    }


async def _handle_audio_message(item: dict[str, str]) -> None:
    chat_id = item["chat_id"]
    media_id = item["media_id"]
    inbound_mime = item.get("mime_type") or "audio/ogg"

    media_meta = await whatsapp.fetch_media_meta(media_id)
    download_url = str(media_meta.get("url") or "")
    if not download_url:
        raise RuntimeError(f"Missing media download URL for media_id={media_id}: {media_meta}")

    audio_bytes, downloaded_mime = await whatsapp.download_media_bytes(download_url)
    input_mime = downloaded_mime or inbound_mime

    transcript = await openai.transcribe_audio(
        audio_bytes=audio_bytes,
        filename="user_audio.ogg",
        mime_type=input_mime,
    )
    logger.info("Transcribed from %s: %s", chat_id, transcript)

    reply_text = await openai.generate_reply_text(transcript)
    tts_bytes, tts_filename, tts_mime = await openai.synthesize_speech(reply_text)

    uploaded_media_id = await whatsapp.upload_media(
        data=tts_bytes,
        filename=tts_filename,
        mime_type=tts_mime,
    )
    await whatsapp.send_audio_message(chat_id, uploaded_media_id)


async def _safe_send_error(chat_id: str) -> None:
    try:
        await whatsapp.send_text_message(
            chat_id,
            "抱歉，剛剛語音處理失敗。請再傳一次語音訊息。",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send fallback error text")


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not settings.whatsapp_app_secret:
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = signature_header.split("=", 1)[1]

    return hmac.compare_digest(expected, provided)
