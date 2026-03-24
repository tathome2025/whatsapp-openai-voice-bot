from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from html import escape
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from app.admin_auth import hash_password, make_session_token, parse_session_token, verify_password
from app.config import get_settings
from app.db import AppRepo
from app.language_store import LanguagePreferenceStore
from app.openai_client import OpenAIClient
from app.voice_store import VoicePreferenceStore
from app.whatsapp import WhatsAppClient, extract_inbound_messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
repo = AppRepo(settings)
whatsapp = WhatsAppClient(settings)
openai = OpenAIClient(settings)
voice_store = VoicePreferenceStore(settings)
language_store = LanguagePreferenceStore(settings)

app = FastAPI(title="WhatsApp OpenAI Voice Bot", version="0.2.0")

ADMIN_COOKIE_NAME = "wa_admin_session"


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class AdminWhitelistUpsert(BaseModel):
    chat_id: str
    label: str = ""


class AdminMemoryCreate(BaseModel):
    chat_id: str
    content: str


class AdminUserCreate(BaseModel):
    email: str
    password: str
    display_name: str = ""
    status: str = "active"


def _bootstrap_admin_if_needed() -> None:
    email = settings.admin_bootstrap_email.strip().lower()
    password = settings.admin_bootstrap_password
    if not email or not password:
        return

    if repo.count_admin_users() > 0:
        return

    password_hash = hash_password(password)
    repo.upsert_admin_user(
        email=email,
        display_name="Bootstrap Admin",
        password_hash=password_hash,
        status="active",
    )
    logger.info("Bootstrap admin account created: %s", email)


_bootstrap_admin_if_needed()


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
      <li>Manage user memory and personalization preferences</li>
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
      <li>Send your WhatsApp number and request to <a href="mailto:{contact_email}">{contact_email}</a>, or</li>
      <li>Message the bot with: <code>delete my data</code></li>
    </ol>

    <h2>What Will Be Deleted</h2>
    <ul>
      <li>Conversation records associated with your WhatsApp number</li>
      <li>User memory records associated with your WhatsApp number</li>
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
        "admin": {
            "admin_users": repo.count_admin_users(),
            "session_secret_configured": bool(settings.admin_session_secret),
        },
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return _render_admin_html()


@app.get("/admin/auth/me")
async def admin_me(request: Request) -> dict[str, Any]:
    user = await _get_admin_user_from_request(request)
    if not user:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user": {
            "id": int(user.get("id") or 0),
            "email": str(user.get("email") or ""),
            "display_name": str(user.get("display_name") or ""),
        },
    }


@app.post("/admin/auth/login")
async def admin_login(request: Request, body: AdminLoginRequest, response: Response) -> dict[str, Any]:
    if not settings.admin_session_secret:
        raise HTTPException(status_code=503, detail="ADMIN_SESSION_SECRET is not configured")

    email = body.email.strip().lower()
    password = body.password
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password are required")

    user = repo.get_admin_user_by_email(email)
    if not user or str(user.get("status") or "") != "active":
        raise HTTPException(status_code=401, detail="Invalid login")

    if not verify_password(password, str(user.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Invalid login")

    session_token = make_session_token(
        int(user["id"]),
        str(user.get("email") or ""),
        settings.admin_session_secret,
        settings.admin_session_hours,
    )
    secure_cookie = bool(os.getenv("VERCEL_URL")) or request.url.scheme == "https"
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=session_token,
        max_age=settings.admin_session_hours * 3600,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    repo.touch_admin_login(int(user["id"]))

    return {
        "ok": True,
        "user": {
            "id": int(user["id"]),
            "email": str(user.get("email") or ""),
            "display_name": str(user.get("display_name") or ""),
        },
    }


@app.post("/admin/auth/logout")
async def admin_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/admin/api/users")
async def admin_list_users(request: Request) -> dict[str, Any]:
    await _assert_admin_auth(request)
    return {"items": repo.list_known_users()}


@app.get("/admin/api/whitelist")
async def admin_list_whitelist(request: Request) -> dict[str, Any]:
    await _assert_admin_auth(request)
    return {"items": repo.list_whitelist()}


@app.post("/admin/api/whitelist")
async def admin_upsert_whitelist(request: Request, body: AdminWhitelistUpsert) -> dict[str, Any]:
    await _assert_admin_auth(request)
    chat_id = _normalize_chat_id(body.chat_id)
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
    item = repo.upsert_whitelist(chat_id, body.label.strip())
    return {"item": item}


@app.delete("/admin/api/whitelist/{chat_id}")
async def admin_delete_whitelist(request: Request, chat_id: str) -> dict[str, bool]:
    await _assert_admin_auth(request)
    deleted = repo.remove_whitelist(_normalize_chat_id(chat_id))
    return {"deleted": deleted}


@app.get("/admin/api/conversations")
async def admin_list_conversations(request: Request, chat_id: str, limit: int = 200) -> dict[str, Any]:
    await _assert_admin_auth(request)
    normalized = _normalize_chat_id(chat_id)
    if not normalized:
        raise HTTPException(status_code=400, detail="chat_id is required")
    items = repo.list_conversation_logs(normalized, limit=limit)
    return {"chat_id": normalized, "items": items}


@app.get("/admin/api/memories")
async def admin_list_memories(
    request: Request,
    chat_id: str,
    include_inactive: bool = False,
) -> dict[str, Any]:
    await _assert_admin_auth(request)
    normalized = _normalize_chat_id(chat_id)
    if not normalized:
        raise HTTPException(status_code=400, detail="chat_id is required")
    items = repo.list_memories(normalized, include_inactive=bool(include_inactive))
    return {"chat_id": normalized, "items": items}


@app.post("/admin/api/memories")
async def admin_add_memory(request: Request, body: AdminMemoryCreate) -> dict[str, Any]:
    await _assert_admin_auth(request)
    chat_id = _normalize_chat_id(body.chat_id)
    content = body.content.strip()
    if not chat_id or not content:
        raise HTTPException(status_code=400, detail="chat_id and content are required")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="content is too long")
    item = repo.add_memory(chat_id, content, created_by="admin")
    return {"item": item}


@app.delete("/admin/api/memories/{memory_id}")
async def admin_archive_memory(request: Request, memory_id: int) -> dict[str, bool]:
    await _assert_admin_auth(request)
    archived = repo.archive_memory(memory_id)
    return {"archived": archived}


@app.get("/admin/api/admin-users")
async def admin_list_admin_users(request: Request) -> dict[str, Any]:
    await _assert_admin_auth(request)
    return {"items": repo.list_admin_users()}


@app.post("/admin/api/admin-users")
async def admin_create_admin_user(request: Request, body: AdminUserCreate) -> dict[str, Any]:
    await _assert_admin_auth(request)

    email = body.email.strip().lower()
    password = body.password
    display_name = body.display_name.strip() or email
    status = body.status.strip().lower() or "active"

    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password are required")
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="status must be active or disabled")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")

    password_hash = hash_password(password)
    item = repo.upsert_admin_user(
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        status=status,
    )
    return {"item": item}


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
        chat_id = _normalize_chat_id(str(item.get("chat_id") or ""))
        if not chat_id:
            ignored += 1
            continue

        if not repo.is_whitelisted(chat_id):
            ignored += 1
            continue

        try:
            msg_type = item.get("type")
            if msg_type == "audio":
                await _handle_audio_message({**item, "chat_id": chat_id})
                processed += 1
                continue

            if msg_type == "text":
                handled = await _handle_text_message({**item, "chat_id": chat_id})
                if handled:
                    processed += 1
                else:
                    ignored += 1
                continue

            ignored += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("Failed to process message type=%s chat=%s: %s", item.get("type"), chat_id, exc)
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
    transcript = transcript.strip()
    repo.log_message(
        chat_id,
        direction="in",
        role="user",
        source_type="audio",
        message_text=transcript,
    )

    memory_command = _parse_memory_command(transcript)
    if memory_command is not None:
        reply_text = _handle_memory_command(chat_id, memory_command)
    else:
        reply_instruction = _reply_language_instruction(selected_language)
        memory_context = _build_memory_context(repo.list_memories(chat_id))
        reply_text = await openai.generate_reply_text(
            transcript,
            reply_language_instruction=reply_instruction,
            memory_context=memory_context,
        )

    reply_text = reply_text.strip()
    repo.log_message(
        chat_id,
        direction="out",
        role="assistant",
        source_type="audio",
        message_text=reply_text,
    )

    selected_voice = await voice_store.get_voice(chat_id)
    tts_bytes, tts_filename, tts_mime = await openai.synthesize_speech(reply_text, voice=selected_voice)

    uploaded_media_id = await whatsapp.upload_media(
        data=tts_bytes,
        filename=tts_filename,
        mime_type=tts_mime,
    )
    await whatsapp.send_audio_message(chat_id, uploaded_media_id)


async def _handle_text_message(item: dict[str, str]) -> bool:
    chat_id = item["chat_id"]
    text = item.get("text", "").strip()
    if not text:
        return False

    repo.log_message(
        chat_id,
        direction="in",
        role="user",
        source_type="text",
        message_text=text,
    )

    voice_command = _parse_voice_command(text)
    if voice_command is not None:
        reply = await _handle_voice_command(chat_id, voice_command)
        repo.log_message(chat_id, direction="out", role="assistant", source_type="text", message_text=reply)
        return True

    language_command = _parse_language_command(text)
    if language_command is not None:
        reply = await _handle_language_command(chat_id, language_command)
        repo.log_message(chat_id, direction="out", role="assistant", source_type="text", message_text=reply)
        return True

    memory_command = _parse_memory_command(text)
    if memory_command is not None:
        reply = _handle_memory_command(chat_id, memory_command)
        await whatsapp.send_text_message(chat_id, reply)
        repo.log_message(chat_id, direction="out", role="assistant", source_type="text", message_text=reply)
        return True

    return False


async def _handle_voice_command(chat_id: str, command: dict[str, str]) -> str:
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
        return body

    target_voice = str(command.get("voice") or "").lower()
    if target_voice not in allowed:
        body = f"未支援語音「{target_voice}」。可用: {', '.join(allowed)}"
        await whatsapp.send_text_message(chat_id, body)
        return body

    await voice_store.set_voice(chat_id, target_voice)
    body = f"已切換語音到 {target_voice}。"
    await whatsapp.send_text_message(chat_id, body)
    return body


async def _handle_language_command(chat_id: str, command: dict[str, str]) -> str:
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
        return body

    target_language = str(command.get("language") or "")
    if target_language not in allowed:
        body = f"未支援語言「{target_language}」。可用: {', '.join(allowed)}"
        await whatsapp.send_text_message(chat_id, body)
        return body

    await language_store.set_language(chat_id, target_language)
    body = f"已設定主要語言為 {target_language}。"
    await whatsapp.send_text_message(chat_id, body)
    return body


def _handle_memory_command(chat_id: str, command: dict[str, str]) -> str:
    action = command.get("action")
    if action == "show":
        memories = repo.list_memories(chat_id)
        if not memories:
            return "目前未有任何記憶紀錄。"

        lines = ["以下是你目前的記憶紀錄："]
        for idx, item in enumerate(memories[:20], start=1):
            lines.append(f"{idx}. {item.get('content', '')}")
        if len(memories) > 20:
            lines.append(f"... 還有 {len(memories) - 20} 條")
        return "\n".join(lines)

    content = str(command.get("content") or "").strip()
    if not content:
        return "請提供要記下的內容，例如：記低 明天早上10點同客開會"

    if len(content) > 2000:
        return "內容太長，請縮短到 2000 字內。"

    repo.add_memory(chat_id, content, created_by="user")
    return f"已幫你記低：{content}"


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


def _parse_memory_command(text: str) -> dict[str, str] | None:
    raw = text.strip()
    normalized = raw.lower()

    if normalized in {
        "memory",
        "memories",
        "read memory",
        "show memory",
        "my memory",
        "my memories",
        "讀出紀錄",
        "读出记录",
        "記憶",
        "记忆",
        "紀錄",
        "记录",
    }:
        return {"action": "show"}

    if (
        ("讀出" in raw or "读出" in raw or "show" in normalized or "read" in normalized)
        and ("記憶" in raw or "记忆" in raw or "紀錄" in raw or "记录" in raw or "memory" in normalized)
    ):
        return {"action": "show"}

    add_match = re.fullmatch(
        r"(?:記低|记低|記住|记住|記下|记下|記錄|记录|remember(?:\s+that)?|save\s+this)\s*(?::|=|,|\s)\s*(.+)",
        raw,
        flags=re.IGNORECASE,
    )
    if add_match:
        return {"action": "add", "content": add_match.group(1).strip()}

    return None


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


def _build_memory_context(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""

    lines = ["User memories:"]
    for item in memories[:30]:
        lines.append(f"- {str(item.get('content') or '').strip()}")
    return "\n".join(lines)


def _normalize_chat_id(value: str) -> str:
    return value.strip()


async def _assert_admin_auth(request: Request) -> dict[str, Any]:
    user = await _get_admin_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized admin session")
    return user


async def _get_admin_user_from_request(request: Request) -> dict[str, Any] | None:
    if not settings.admin_session_secret:
        return None

    token = request.cookies.get(ADMIN_COOKIE_NAME, "").strip()
    if not token:
        return None

    payload = parse_session_token(token, settings.admin_session_secret)
    if not payload:
        return None

    uid_raw = payload.get("uid")
    if uid_raw in (None, ""):
        return None

    try:
        user_id = int(uid_raw)
    except (TypeError, ValueError):
        return None

    user = repo.get_admin_user_by_id(user_id)
    if not user:
        return None

    if str(user.get("status") or "") != "active":
        return None

    email = str(payload.get("email") or "").strip().lower()
    if email and not hmac.compare_digest(email, str(user.get("email") or "").strip().lower()):
        return None

    return user


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


def _render_admin_html() -> str:
    return """<!doctype html>
<html lang="zh-Hant">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WA Voice Bot Admin</title>
    <style>
      :root {
        --bg: #0f1115;
        --card: #171a21;
        --card2: #1d2230;
        --text: #f7f7f8;
        --sub: #b8beca;
        --line: #2a3242;
        --accent: #f97316;
        --ok: #16a34a;
        --bad: #ef4444;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        background: radial-gradient(circle at top right, #1f2937 0%, var(--bg) 45%);
        color: var(--text);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      }
      .wrap { max-width: 1280px; margin: 0 auto; padding: 20px; }
      .grid { display: grid; gap: 14px; grid-template-columns: repeat(12, 1fr); }
      .card {
        background: linear-gradient(160deg, var(--card), var(--card2));
        border: 1px solid var(--line);
        border-radius: 12px;
        padding: 14px;
      }
      .col-12 { grid-column: span 12; }
      .col-6 { grid-column: span 6; }
      .col-4 { grid-column: span 4; }
      .col-8 { grid-column: span 8; }
      h1 { margin: 0 0 8px; font-size: 22px; }
      h2 { margin: 0 0 10px; font-size: 16px; color: #fff; }
      p, label { color: var(--sub); font-size: 14px; }
      input, textarea, select, button {
        width: 100%;
        margin: 6px 0;
        border-radius: 8px;
        border: 1px solid var(--line);
        background: #0f1520;
        color: var(--text);
        padding: 9px 10px;
      }
      textarea { min-height: 90px; resize: vertical; }
      button {
        background: var(--accent);
        border: none;
        color: #fff;
        font-weight: 600;
        cursor: pointer;
      }
      button.ghost {
        background: transparent;
        border: 1px solid var(--line);
        color: var(--sub);
      }
      table { width: 100%; border-collapse: collapse; font-size: 13px; }
      th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }
      .row { display: flex; gap: 8px; }
      .row > * { flex: 1; }
      .muted { color: var(--sub); font-size: 12px; }
      .ok { color: var(--ok); }
      .bad { color: var(--bad); }
      .pill {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        border: 1px solid var(--line);
        font-size: 12px;
      }
      pre {
        white-space: pre-wrap;
        background: #0c1018;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px;
        font-size: 12px;
        color: #e6eaf0;
        margin: 0;
      }
      #app { display: none; }
      .sp { height: 8px; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card col-12" id="login">
        <h1>WA Voice Bot Admin</h1>
        <p>登入後可管理白名單、對話記錄、記憶內容和 Admin 帳戶。</p>
        <div class="row">
          <input id="login-email" placeholder="admin@example.com" />
          <input id="login-password" type="password" placeholder="password" />
          <button id="login-btn">登入</button>
        </div>
        <p id="login-msg" class="muted"></p>
      </div>

      <div id="app">
        <div class="grid">
          <div class="card col-12">
            <div class="row">
              <div>
                <h1>控制台</h1>
                <p id="auth-user" class="muted"></p>
              </div>
              <div style="max-width:220px">
                <button id="logout-btn" class="ghost">登出</button>
              </div>
            </div>
          </div>

          <div class="card col-6">
            <h2>白名單</h2>
            <div class="row">
              <input id="wl-chat" placeholder="85291234567" />
              <input id="wl-label" placeholder="Label" />
            </div>
            <div class="row">
              <button id="wl-add">新增 / 更新</button>
              <button id="wl-refresh" class="ghost">刷新</button>
            </div>
            <div class="sp"></div>
            <table>
              <thead><tr><th>Chat ID</th><th>Label</th><th></th></tr></thead>
              <tbody id="wl-body"></tbody>
            </table>
          </div>

          <div class="card col-6">
            <h2>Admin 帳戶管理</h2>
            <div class="row">
              <input id="au-email" placeholder="new-admin@example.com" />
              <input id="au-name" placeholder="Display Name" />
            </div>
            <div class="row">
              <input id="au-password" type="password" placeholder="Password (>=8)" />
              <select id="au-status"><option value="active">active</option><option value="disabled">disabled</option></select>
            </div>
            <div class="row">
              <button id="au-add">新增 / 更新 Admin</button>
              <button id="au-refresh" class="ghost">刷新</button>
            </div>
            <div class="sp"></div>
            <table>
              <thead><tr><th>ID</th><th>Email</th><th>Status</th><th>Last Login</th></tr></thead>
              <tbody id="au-body"></tbody>
            </table>
          </div>

          <div class="card col-4">
            <h2>用戶清單</h2>
            <div class="row">
              <button id="users-refresh" class="ghost">刷新</button>
            </div>
            <table>
              <thead><tr><th>Chat ID</th><th>WL</th><th>Last</th></tr></thead>
              <tbody id="users-body"></tbody>
            </table>
          </div>

          <div class="card col-8">
            <h2>選中用戶</h2>
            <p class="muted" id="selected-chat">尚未選擇</p>
            <div class="row">
              <button id="conv-refresh" class="ghost">刷新對話</button>
              <button id="mem-refresh" class="ghost">刷新記憶</button>
            </div>
            <div class="row">
              <textarea id="mem-content" placeholder="為該用戶植入記憶，例如：客戶A偏好星期三上午開會"></textarea>
            </div>
            <div class="row">
              <button id="mem-add">新增記憶（Admin）</button>
            </div>
          </div>

          <div class="card col-6">
            <h2>對話記錄（文字）</h2>
            <div id="conv-list" class="muted">請先選擇用戶</div>
          </div>

          <div class="card col-6">
            <h2>記憶清單</h2>
            <table>
              <thead><tr><th>ID</th><th>Content</th><th>By</th><th></th></tr></thead>
              <tbody id="mem-body"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <script>
      async function api(path, options = {}) {
        const method = options.method || 'GET';
        const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
        const body = options.body ? JSON.stringify(options.body) : undefined;
        const res = await fetch(path, { method, headers, body, credentials: 'include' });
        const text = await res.text();
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
        if (!res.ok) throw new Error(data.detail || data.error || text || `HTTP ${res.status}`);
        return data;
      }

      const state = { selectedChat: '' };

      function esc(s) {
        return String(s || '').replace(/[&<>'"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;' }[c]));
      }

      async function checkAuth() {
        try {
          const data = await api('/admin/auth/me');
          if (data.authenticated) {
            document.getElementById('login').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            document.getElementById('auth-user').textContent = `${data.user.email} (${data.user.display_name || 'Admin'})`;
            await Promise.all([loadWhitelist(), loadUsers(), loadAdminUsers()]);
            return;
          }
        } catch (_) {}
        document.getElementById('login').style.display = 'block';
        document.getElementById('app').style.display = 'none';
      }

      async function login() {
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;
        const msg = document.getElementById('login-msg');
        msg.textContent = '';
        try {
          await api('/admin/auth/login', { method: 'POST', body: { email, password } });
          await checkAuth();
        } catch (err) {
          msg.textContent = err.message;
        }
      }

      async function logout() {
        await api('/admin/auth/logout', { method: 'POST' });
        await checkAuth();
      }

      async function loadWhitelist() {
        const data = await api('/admin/api/whitelist');
        const body = document.getElementById('wl-body');
        body.innerHTML = (data.items || []).map(item => `
          <tr>
            <td>${esc(item.chat_id)}</td>
            <td>${esc(item.label)}</td>
            <td><button class="ghost" onclick="delWhitelist('${esc(item.chat_id)}')">刪除</button></td>
          </tr>
        `).join('');
      }

      async function addWhitelist() {
        const chat_id = document.getElementById('wl-chat').value.trim();
        const label = document.getElementById('wl-label').value.trim();
        if (!chat_id) return;
        await api('/admin/api/whitelist', { method: 'POST', body: { chat_id, label } });
        document.getElementById('wl-chat').value = '';
        document.getElementById('wl-label').value = '';
        await loadWhitelist();
        await loadUsers();
      }

      async function delWhitelist(chatId) {
        await api(`/admin/api/whitelist/${encodeURIComponent(chatId)}`, { method: 'DELETE' });
        await loadWhitelist();
        await loadUsers();
      }

      async function loadUsers() {
        const data = await api('/admin/api/users');
        const body = document.getElementById('users-body');
        body.innerHTML = (data.items || []).map(item => {
          const wl = Number(item.whitelisted) ? '<span class="pill ok">YES</span>' : '<span class="pill bad">NO</span>';
          return `
            <tr onclick="selectChat('${esc(item.chat_id)}')" style="cursor:pointer">
              <td>${esc(item.chat_id)}${item.label ? `<div class='muted'>${esc(item.label)}</div>` : ''}</td>
              <td>${wl}</td>
              <td><div class='muted'>${esc(item.last_message_at || '')}</div><div>${esc((item.last_message || '').slice(0, 48))}</div></td>
            </tr>
          `;
        }).join('');
      }

      async function selectChat(chatId) {
        state.selectedChat = chatId;
        document.getElementById('selected-chat').textContent = `當前用戶: ${chatId}`;
        await Promise.all([loadConversations(), loadMemories()]);
      }

      async function loadConversations() {
        const container = document.getElementById('conv-list');
        if (!state.selectedChat) {
          container.textContent = '請先選擇用戶';
          return;
        }
        const data = await api(`/admin/api/conversations?chat_id=${encodeURIComponent(state.selectedChat)}&limit=200`);
        const items = data.items || [];
        if (!items.length) {
          container.textContent = '無對話記錄';
          return;
        }
        container.innerHTML = items.map(i => {
          return `<pre>[${esc(i.created_at)}] ${esc(i.direction)} / ${esc(i.source_type)}\n${esc(i.message_text)}</pre>`;
        }).join('<div class="sp"></div>');
      }

      async function loadMemories() {
        const body = document.getElementById('mem-body');
        if (!state.selectedChat) {
          body.innerHTML = '';
          return;
        }
        const data = await api(`/admin/api/memories?chat_id=${encodeURIComponent(state.selectedChat)}`);
        body.innerHTML = (data.items || []).map(item => `
          <tr>
            <td>${esc(item.id)}</td>
            <td>${esc(item.content)}</td>
            <td>${esc(item.created_by)}</td>
            <td><button class="ghost" onclick="archiveMemory(${Number(item.id)})">封存</button></td>
          </tr>
        `).join('');
      }

      async function addMemory() {
        if (!state.selectedChat) return;
        const content = document.getElementById('mem-content').value.trim();
        if (!content) return;
        await api('/admin/api/memories', { method: 'POST', body: { chat_id: state.selectedChat, content } });
        document.getElementById('mem-content').value = '';
        await loadMemories();
      }

      async function archiveMemory(id) {
        await api(`/admin/api/memories/${id}`, { method: 'DELETE' });
        await loadMemories();
      }

      async function loadAdminUsers() {
        const data = await api('/admin/api/admin-users');
        const body = document.getElementById('au-body');
        body.innerHTML = (data.items || []).map(item => `
          <tr>
            <td>${esc(item.id)}</td>
            <td>${esc(item.email)}<div class='muted'>${esc(item.display_name || '')}</div></td>
            <td>${esc(item.status)}</td>
            <td>${esc(item.last_login_at || '')}</td>
          </tr>
        `).join('');
      }

      async function addAdminUser() {
        const email = document.getElementById('au-email').value.trim();
        const password = document.getElementById('au-password').value;
        const display_name = document.getElementById('au-name').value.trim();
        const status = document.getElementById('au-status').value;
        if (!email || !password) return;
        await api('/admin/api/admin-users', { method: 'POST', body: { email, password, display_name, status } });
        document.getElementById('au-email').value = '';
        document.getElementById('au-password').value = '';
        document.getElementById('au-name').value = '';
        await loadAdminUsers();
      }

      document.getElementById('login-btn').addEventListener('click', () => login().catch(err => alert(err.message)));
      document.getElementById('logout-btn').addEventListener('click', () => logout().catch(err => alert(err.message)));
      document.getElementById('wl-add').addEventListener('click', () => addWhitelist().catch(err => alert(err.message)));
      document.getElementById('wl-refresh').addEventListener('click', () => loadWhitelist().catch(err => alert(err.message)));
      document.getElementById('users-refresh').addEventListener('click', () => loadUsers().catch(err => alert(err.message)));
      document.getElementById('conv-refresh').addEventListener('click', () => loadConversations().catch(err => alert(err.message)));
      document.getElementById('mem-refresh').addEventListener('click', () => loadMemories().catch(err => alert(err.message)));
      document.getElementById('mem-add').addEventListener('click', () => addMemory().catch(err => alert(err.message)));
      document.getElementById('au-add').addEventListener('click', () => addAdminUser().catch(err => alert(err.message)));
      document.getElementById('au-refresh').addEventListener('click', () => loadAdminUsers().catch(err => alert(err.message)));

      window.delWhitelist = delWhitelist;
      window.selectChat = selectChat;
      window.archiveMemory = archiveMemory;

      checkAuth().catch(err => {
        document.getElementById('login-msg').textContent = err.message;
      });
    </script>
  </body>
</html>"""
