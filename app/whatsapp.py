from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def _base_url(self) -> str:
        return f"https://graph.facebook.com/{self.settings.whatsapp_graph_version}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.whatsapp_access_token}",
        }

    @property
    def _messages_url(self) -> str:
        return f"{self._base_url}/{self.settings.whatsapp_phone_number_id}/messages"

    @property
    def _media_upload_url(self) -> str:
        return f"{self._base_url}/{self.settings.whatsapp_phone_number_id}/media"

    async def send_text_message(self, chat_id: str, body: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "text",
            "text": {"body": body[:4096]},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._messages_url,
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
        resp.raise_for_status()

    async def send_audio_message(self, chat_id: str, media_id: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "audio",
            "audio": {"id": media_id},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._messages_url,
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
        resp.raise_for_status()

    async def fetch_media_meta(self, media_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/{media_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def download_media_bytes(self, download_url: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(download_url, headers=self._headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type

    async def upload_media(self, data: bytes, filename: str, mime_type: str) -> str:
        files = {
            "file": (filename, data, mime_type),
            "messaging_product": (None, "whatsapp"),
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self._media_upload_url, headers=self._headers, files=files)
        resp.raise_for_status()
        payload = resp.json()
        media_id = payload.get("id")
        if not media_id:
            raise RuntimeError(f"WhatsApp media upload returned no id: {payload}")
        return str(media_id)

    async def health_check(self) -> dict[str, Any]:
        if not self.settings.whatsapp_access_token or not self.settings.whatsapp_phone_number_id:
            return {"ok": False, "error": "WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is missing"}

        url = f"{self._base_url}/{self.settings.whatsapp_phone_number_id}"
        params = {"fields": "id,display_phone_number"}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        return {"ok": True}


def extract_inbound_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                chat_id = str(message.get("from") or "").strip()
                message_id = str(message.get("id") or "").strip()
                msg_type = str(message.get("type") or "").strip().lower()

                if not chat_id:
                    continue

                if msg_type == "audio":
                    media = message.get("audio") or {}
                    media_id = str(media.get("id") or "").strip()
                    if not media_id:
                        continue
                    items.append(
                        {
                            "type": "audio",
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "media_id": media_id,
                            "mime_type": str(media.get("mime_type") or "audio/ogg"),
                        }
                    )
                    continue

                if msg_type == "text":
                    text = str((message.get("text") or {}).get("body") or "").strip()
                    if not text:
                        continue
                    items.append(
                        {
                            "type": "text",
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "text": text,
                        }
                    )

    return items


def extract_audio_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [item for item in extract_inbound_messages(payload) if item.get("type") == "audio"]
