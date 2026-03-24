from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from html import escape
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import get_settings
from app.openai_client import OpenAIClient
from app.voice_store import VoicePreferenceStore
from app.whatsapp import WhatsAppClient, extract_inbound_messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
whatsapp = WhatsAppClient(settings)
openai = OpenAIClient(settings)
voice_store = VoicePreferenceStore(settings)

app = FastAPI(title="WhatsApp OpenAI Voice Bot", version="0.1.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "whatsapp-openai-voice-bot"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> str:
    contact_email = escape(os.getenv("PRIVACY_CONTACT_EMAIL", "support@example.com"))
    content = f"""
    <h2>1. Data We Collect</h2>
    <ul>
      <li>WhatsApp phone number and message metadata</li>
      <li>Message content you send to the bot (including voice transcripts)</li>
      <li>Operational logs required for reliability and security</li>
    </ul>

    <h2>2. Why We Use Data</h2>
    <ul>
      <li>Provide voice assistant replies over WhatsApp</li>
      <li>Improve service quality and troubleshooting</li>
      <li>Prevent abuse and unauthorized access</li>
    </ul>

    <h2>3. Processors</h2>
    <ul>
      <li>Meta WhatsApp Cloud API (message transport)</li>
      <li>OpenAI API (speech recognition, response generation, text-to-speech)</li>
      <li>Vercel (hosting and runtime logs)</li>
    </ul>

    <h2>4. Data Retention</h2>
    <p>We retain data only as long as necessary to provide and secure the service, unless longer retention is required by law.</p>

    <h2>5. Data Deletion</h2>
    <p>You can request deletion anytime. See <a href="/data-deletion">Data Deletion Instructions</a>.</p>

    <h2>6. Contact</h2>
    <p>Privacy inquiries: <a href="mailto:{contact_email}">{contact_email}</a></p>

    <h2>7. Effective Date</h2>
    <p>2026-03-24</p>
    """
    return _render_legal_page("Privacy Policy", content)


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion() -> str:
    contact_email = escape(os.getenv("PRIVACY_CONTACT_EMAIL", "support@example.com"))
    content = f"""
    <h2>How to Request Deletion</h2>
    <ol>
      <li>Send us your WhatsApp number and deletion request via email to <a href="mailto:{contact_email}">{contact_email}</a>, or</li>
      <li>Message the bot with: <code>delete my data</code></li>
    </ol>

    <h2>What Will Be Deleted</h2>
    <ul>
      <li>Stored user messages and transcripts associated with your number</li>
      <li>Assistant response records linked to your number</li>
      <li>Related processing metadata where applicable</li>
    </ul>

    <h2>Timeline</h2>
    <p>Deletion requests are generally completed within 7 business days.</p>

    <h2>Effective Date</h2>
    <p>2026-03-24</p>
    """
    return _render_legal_page("Data Deletion Instructions", content)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "whatsapp": await whatsapp.health_check(),
        "openai": await openai.health_check(),
        "voice": {
            "default_voice": settings.openai_tts_voice,
            "available_voices": _allowed_voices(),
        },
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

    inbound_messages = extract_inbound_messages(payload)
    if not inbound_messages:
        return {"status": "ok", "processed": 0, "ignored": 1, "failed": 0}

    processed = 0
    ignored = 0
    failed = 0

    for item in inbound_messages:
        try:
            msg_type = item.get("type")
            if msg_type == "audio":
                await _handle_audio_message(item)
                processed += 1
                continue

            if msg_type == "text":
                handled = await _handle_text_message(item)
                if handled:
                    processed += 1
                else:
                    ignored += 1
                continue

            ignored += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            chat_id = item.get("chat_id", "")
            logger.exception("Failed to process message type=%s chat=%s: %s", item.get("type"), chat_id, exc)
            if chat_id:
                await _safe_send_error(chat_id)

    return {
        "status": "ok",
        "processed": processed,
        "ignored": ignored,
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
    selected_voice = await voice_store.get_voice(chat_id)
    tts_bytes, tts_filename, tts_mime = await openai.synthesize_speech(reply_text, voice=selected_voice)

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


async def _handle_text_message(item: dict[str, str]) -> bool:
    chat_id = item["chat_id"]
    text = item.get("text", "").strip()
    command = _parse_voice_command(text)
    if command is None:
        return False

    allowed = _allowed_voices()

    if command["action"] == "show":
        current = await voice_store.get_voice(chat_id)
        body = (
            f"目前語音: {current}\n"
            f"可用語音: {', '.join(allowed)}\n"
            "切換方法: voice <聲音名>\n"
            "例子: voice aria"
        )
        await whatsapp.send_text_message(chat_id, body)
        return True

    target_voice = str(command.get("voice") or "").lower()
    if target_voice not in allowed:
        await whatsapp.send_text_message(
            chat_id,
            f"未支援語音「{target_voice}」。可用: {', '.join(allowed)}",
        )
        return True

    await voice_store.set_voice(chat_id, target_voice)
    await whatsapp.send_text_message(chat_id, f"已切換語音到 {target_voice}。")
    return True


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


def _allowed_voices() -> list[str]:
    raw = (settings.openai_tts_voices or "").strip()
    if not raw:
        return [settings.openai_tts_voice.lower()]

    voices = [v.strip().lower() for v in raw.split(",") if v.strip()]
    if not voices:
        return [settings.openai_tts_voice.lower()]
    if settings.openai_tts_voice.lower() not in voices:
        voices.append(settings.openai_tts_voice.lower())
    return voices


def _parse_voice_command(text: str) -> dict[str, str] | None:
    raw = text.strip()
    normalized = raw.lower()
    if normalized in {
        "voice",
        "voices",
        "voice list",
        "聲音",
        "語音",
        "语音",
        "聲音列表",
        "語音列表",
        "语音列表",
    }:
        return {"action": "show"}

    match = re.fullmatch(
        r"(?:voice|set\s+voice|聲音|語音|语音)\s*(?::|=|\s)\s*([a-zA-Z0-9_-]+)\s*",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    return {"action": "set", "voice": match.group(1).lower()}


def _render_legal_page(title: str, body_html: str) -> str:
    safe_title = escape(title)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_title}</title>
    <style>
      body {{
        margin: 0;
        background: #f5f5f5;
        color: #1f2937;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      }}
      .shell {{
        max-width: 840px;
        margin: 32px auto;
        background: #fff;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 28px;
        line-height: 1.6;
      }}
      h1 {{
        margin: 0 0 16px;
        color: #111827;
      }}
      h2 {{
        margin: 20px 0 8px;
        color: #111827;
      }}
      code {{
        background: #f3f4f6;
        padding: 1px 6px;
        border-radius: 6px;
      }}
      a {{
        color: #2563eb;
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <h1>{safe_title}</h1>
      {body_html}
    </main>
  </body>
</html>"""
