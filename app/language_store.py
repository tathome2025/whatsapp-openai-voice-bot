from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import Settings


class LanguagePreferenceStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._loaded = False
        self._data: dict[str, str] = {}
        self._path = Path(settings.language_store_path)

    async def get_language(self, chat_id: str) -> str:
        await self._load_once()
        return self._data.get(chat_id, self.settings.openai_default_language)

    async def set_language(self, chat_id: str, language: str) -> None:
        await self._load_once()
        async with self._lock:
            self._data[chat_id] = language
            self._save()

    async def _load_once(self) -> None:
        if self._loaded:
            return

        async with self._lock:
            if self._loaded:
                return

            if self._path.exists():
                try:
                    content = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(content, dict):
                        self._data = {
                            str(k): str(v)
                            for k, v in content.items()
                            if str(k).strip() and str(v).strip()
                        }
                except Exception:  # noqa: BLE001
                    self._data = {}

            self._loaded = True

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, ensure_ascii=True), encoding="utf-8")
        except Exception:  # noqa: BLE001
            # If storage is unavailable (read-only FS, transient runtime),
            # keep in-memory values for current instance.
            return
