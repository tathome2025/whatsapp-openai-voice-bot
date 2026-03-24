from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class OpenAIClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
        }

    async def transcribe_audio(self, audio_bytes: bytes, filename: str, mime_type: str) -> str:
        files = {
            "file": (filename, audio_bytes, mime_type),
            "model": (None, self.settings.openai_transcribe_model),
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=self._headers,
                files=files,
            )
        resp.raise_for_status()
        payload = resp.json()
        text = str(payload.get("text") or "").strip()
        if not text:
            raise RuntimeError(f"OpenAI transcription returned empty text: {payload}")
        return text

    async def generate_reply_text(self, user_text: str) -> str:
        payload = {
            "model": self.settings.openai_response_model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self.settings.openai_system_prompt,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_text}],
                },
            ],
            "max_output_tokens": 500,
        }
        headers = {**self._headers, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=payload,
            )
        resp.raise_for_status()

        body = resp.json()
        text = _extract_response_text(body).strip()
        if not text:
            raise RuntimeError(f"OpenAI response returned empty text: {body}")

        return text[: self.settings.max_reply_chars]

    async def synthesize_speech(self, text: str, *, voice: str | None = None) -> tuple[bytes, str, str]:
        response_format = (self.settings.openai_tts_format or "opus").strip().lower()
        selected_voice = (voice or self.settings.openai_tts_voice).strip()
        payload = {
            "model": self.settings.openai_tts_model,
            "voice": selected_voice,
            "input": text,
            "response_format": response_format,
        }
        headers = {**self._headers, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers=headers,
                json=payload,
            )
        resp.raise_for_status()

        mime_type = resp.headers.get("content-type", f"audio/{response_format}")
        return resp.content, f"reply.{response_format}", mime_type

    async def health_check(self) -> dict[str, Any]:
        if not self.settings.openai_api_key:
            return {"ok": False, "error": "OPENAI_API_KEY is missing"}

        payload = {"model": self.settings.openai_response_model, "input": "ping"}
        headers = {**self._headers, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        return {"ok": True}


def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    fragments: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = str(content.get("text") or "")
                if text:
                    fragments.append(text)

    return "\n".join(fragments)
