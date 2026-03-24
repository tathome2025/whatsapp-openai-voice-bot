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
from app.language_store import LanguagePreferenceStore
from app.openai_client import OpenAIClient
from app.voice_store import VoicePreferenceStore
from app.whatsapp import WhatsAppClient, extract_inbound_messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
whatsapp = WhatsAppClient(settings)
openai = OpenAIClient(settings)
voice_store = VoicePreferenceStore(settings)
language_store = LanguagePreferenceStore(settings)

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
        "language": {
            "default_language": settings.openai_default_language,
            "available_languages": _allowed_languages(),
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
    selected_language = await language_store.get_language(chat_id)
    transcribe_language = _transcribe_language_code(selected_language)

    transcript = await openai.transcribe_audio(
        audio_bytes=audio_bytes,
        filename="user_audio.ogg",
        mime_type=input_mime,
        language=transcribe_language,
    )
    logger.info("Transcribed from %s: %s", chat_id, transcript)

    reply_instruction = _reply_language_instruction(selected_language)
    reply_text = await openai.generate_reply_text(
        transcript,
        reply_language_instruction=reply_instruction,
    )
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
    voice_command = _parse_voice_command(text)
    if voice_command is not None:
        return await _handle_voice_command(chat_id, voice_command)

    language_command = _parse_language_command(text)
    if language_command is not None:
        return await _handle_language_command(chat_id, language_command)

    return False


async def _handle_voice_command(chat_id: str, command: dict[str, str]) -> bool:
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


async def _handle_language_command(chat_id: str, command: dict[str, str]) -> bool:
    allowed = _allowed_languages()
    if command["action"] == "show":
        current = await language_store.get_language(chat_id)
        body = (
            f"目前主要語言: {current}\n"
            f"可用語言: {', '.join(allowed)}\n"
            "切換方法: language <語言代碼>\n"
            "例子: language zh-HK 或 language en"
        )
        await whatsapp.send_text_message(chat_id, body)
        return True

    target_language = str(command.get("language") or "")
    if target_language not in allowed:
        await whatsapp.send_text_message(
            chat_id,
            f"未支援語言「{target_language}」。可用: {', '.join(allowed)}",
        )
        return True

    await language_store.set_language(chat_id, target_language)
    await whatsapp.send_text_message(chat_id, f"已設定主要語言為 {target_language}。")
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


def _allowed_languages() -> list[str]:
    raw = (settings.openai_languages or "").strip()
    if not raw:
        return [settings.openai_default_language]

    langs = [v.strip() for v in raw.split(",") if v.strip()]
    if not langs:
        return [settings.openai_default_language]
    if settings.openai_default_language not in langs:
        langs.append(settings.openai_default_language)
    return langs


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


def _parse_language_command(text: str) -> dict[str, str] | None:
    raw = text.strip()
    normalized = raw.lower()
    if normalized in {
        "language",
        "lang",
        "language list",
        "語言",
        "语言",
        "語言列表",
        "语言列表",
    }:
        return {"action": "show"}

    match = re.fullmatch(
        r"(?:language|lang|set\s+language|語言|语言)\s*(?::|=|\s)\s*([^\s]+)\s*",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    token = match.group(1).strip()
    resolved = _resolve_language_alias(token)
    if not resolved:
        return {"action": "set", "language": token}
    return {"action": "set", "language": resolved}


def _resolve_language_alias(token: str) -> str | None:
    key = token.strip().lower()
    aliases = {
        "zh": "zh-HK",
        "zh-hk": "zh-HK",
        "hk": "zh-HK",
        "cantonese": "zh-HK",
        "廣東話": "zh-HK",
        "广东话": "zh-HK",
        "粵語": "zh-HK",
        "粤语": "zh-HK",
        "zh-tw": "zh-TW",
        "traditional": "zh-TW",
        "繁中": "zh-TW",
        "繁體": "zh-TW",
        "繁体": "zh-TW",
        "zh-cn": "zh-CN",
        "simplified": "zh-CN",
        "簡中": "zh-CN",
        "简中": "zh-CN",
        "簡體": "zh-CN",
        "简体": "zh-CN",
        "en": "en",
        "english": "en",
        "英文": "en",
        "ja": "ja",
        "japanese": "ja",
        "日文": "ja",
        "日本語": "ja",
        "ko": "ko",
        "korean": "ko",
        "韓文": "ko",
        "韩文": "ko",
    }
    return aliases.get(key)


def _transcribe_language_code(language: str) -> str:
    key = language.strip().lower()
    if key.startswith("zh"):
        return "zh"
    if key.startswith("en"):
        return "en"
    if key.startswith("ja"):
        return "ja"
    if key.startswith("ko"):
        return "ko"
    return "zh"


def _reply_language_instruction(language: str) -> str:
    mapping = {
        "zh-HK": "Always reply in Traditional Chinese with Hong Kong wording and Cantonese-friendly style.",
        "zh-TW": "Always reply in Traditional Chinese used in Taiwan.",
        "zh-CN": "Always reply in Simplified Chinese used in Mainland China.",
        "en": "Always reply in English.",
        "ja": "Always reply in Japanese.",
        "ko": "Always reply in Korean.",
    }
    return mapping.get(language, "Always reply in Traditional Chinese.")


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
