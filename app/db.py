from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings


class AppRepo:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.supabase_rest_url
        self.service_role_key = settings.supabase_service_role_key.strip()

    @property
    def is_ready(self) -> bool:
        return bool(self.base_url and self.service_role_key)

    def health_check(self) -> dict[str, Any]:
        if not self.is_ready:
            return {"ok": False, "error": "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing"}

        try:
            _ = self.count_admin_users()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: Any | None = None,
        prefer: str | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        if not self.is_ready:
            raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured")

        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(
                method=method,
                url=url,
                headers=self._headers(prefer=prefer),
                params=params,
                json=payload,
            )

        if resp.status_code >= 400:
            text = resp.text.strip()
            raise RuntimeError(f"Supabase request failed ({resp.status_code}) {path}: {text}")
        return resp

    @staticmethod
    def _json_list(resp: httpx.Response) -> list[dict[str, Any]]:
        data = resp.json()
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    @staticmethod
    def _first_or_none(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        return items[0] if items else None

    def is_whitelisted(self, chat_id: str) -> bool:
        resp = self._request(
            "GET",
            "/whitelist_contacts",
            params={"select": "chat_id", "chat_id": f"eq.{chat_id}", "limit": 1},
        )
        return len(self._json_list(resp)) > 0

    def list_whitelist(self) -> list[dict[str, Any]]:
        resp = self._request(
            "GET",
            "/whitelist_contacts",
            params={
                "select": "chat_id,label,created_at,updated_at",
                "order": "updated_at.desc,chat_id.asc",
            },
        )
        return self._json_list(resp)

    def upsert_whitelist(self, chat_id: str, label: str = "") -> dict[str, Any]:
        body = [{"chat_id": chat_id, "label": label}]
        resp = self._request(
            "POST",
            "/whitelist_contacts",
            params={"on_conflict": "chat_id"},
            payload=body,
            prefer="resolution=merge-duplicates,return=representation",
        )
        items = self._json_list(resp)
        return self._first_or_none(items) or {}

    def remove_whitelist(self, chat_id: str) -> bool:
        resp = self._request(
            "DELETE",
            "/whitelist_contacts",
            params={"chat_id": f"eq.{chat_id}"},
            prefer="return=representation",
        )
        return len(self._json_list(resp)) > 0

    def log_message(self, chat_id: str, *, direction: str, role: str, source_type: str, message_text: str) -> None:
        body = {
            "chat_id": chat_id,
            "direction": direction,
            "role": role,
            "source_type": source_type,
            "message_text": message_text,
        }
        self._request("POST", "/conversation_logs", payload=body, prefer="return=minimal")

    def list_conversation_logs(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        resp = self._request(
            "GET",
            "/conversation_logs",
            params={
                "select": "id,chat_id,direction,role,source_type,message_text,created_at",
                "chat_id": f"eq.{chat_id}",
                "order": "id.desc",
                "limit": safe_limit,
            },
        )
        return self._json_list(resp)

    def list_known_users(self) -> list[dict[str, Any]]:
        try:
            resp = self._request("POST", "/rpc/list_known_users", payload={})
            return self._json_list(resp)
        except Exception:  # noqa: BLE001
            return self._list_known_users_fallback()

    def _list_known_users_fallback(self) -> list[dict[str, Any]]:
        whitelist = self.list_whitelist()
        whitelist_by_chat = {str(item.get("chat_id") or ""): item for item in whitelist}

        logs_resp = self._request(
            "GET",
            "/conversation_logs",
            params={
                "select": "chat_id,message_text,created_at,id",
                "order": "id.desc",
                "limit": 5000,
            },
        )
        logs = self._json_list(logs_resp)

        latest_by_chat: dict[str, dict[str, Any]] = {}
        for row in logs:
            chat = str(row.get("chat_id") or "")
            if not chat or chat in latest_by_chat:
                continue
            latest_by_chat[chat] = row

        mem_resp = self._request(
            "GET",
            "/user_memories",
            params={"select": "chat_id", "limit": 5000},
        )
        memory_rows = self._json_list(mem_resp)

        chats: set[str] = set(whitelist_by_chat.keys())
        chats.update(latest_by_chat.keys())
        for row in memory_rows:
            chat = str(row.get("chat_id") or "")
            if chat:
                chats.add(chat)

        items: list[dict[str, Any]] = []
        for chat in chats:
            wl = whitelist_by_chat.get(chat)
            latest = latest_by_chat.get(chat)
            items.append(
                {
                    "chat_id": chat,
                    "label": str((wl or {}).get("label") or ""),
                    "last_message": str((latest or {}).get("message_text") or "") if latest else "",
                    "last_message_at": str((latest or {}).get("created_at") or "") if latest else "",
                    "whitelisted": bool(wl),
                }
            )

        items.sort(key=lambda x: str(x.get("last_message_at") or ""), reverse=True)
        return items

    def add_memory(self, chat_id: str, content: str, *, created_by: str) -> dict[str, Any]:
        body = {
            "chat_id": chat_id,
            "content": content,
            "created_by": created_by,
            "status": "active",
        }
        resp = self._request("POST", "/user_memories", payload=body, prefer="return=representation")
        items = self._json_list(resp)
        return self._first_or_none(items) or {}

    def list_memories(self, chat_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "select": "id,chat_id,content,created_by,status,created_at,updated_at",
            "chat_id": f"eq.{chat_id}",
            "order": "id.desc",
        }
        if not include_inactive:
            params["status"] = "eq.active"

        resp = self._request("GET", "/user_memories", params=params)
        return self._json_list(resp)

    def archive_memory(self, memory_id: int) -> bool:
        body = {"status": "archived", "updated_at": self._now_iso()}
        resp = self._request(
            "PATCH",
            "/user_memories",
            params={"id": f"eq.{int(memory_id)}"},
            payload=body,
            prefer="return=representation",
        )
        return len(self._json_list(resp)) > 0

    def get_admin_user_by_email(self, email: str) -> dict[str, Any] | None:
        norm = email.strip().lower()
        resp = self._request(
            "GET",
            "/admin_users",
            params={
                "select": "id,email,display_name,password_hash,status,created_at,updated_at,last_login_at",
                "email": f"eq.{norm}",
                "limit": 1,
            },
        )
        return self._first_or_none(self._json_list(resp))

    def get_admin_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        resp = self._request(
            "GET",
            "/admin_users",
            params={
                "select": "id,email,display_name,password_hash,status,created_at,updated_at,last_login_at",
                "id": f"eq.{int(user_id)}",
                "limit": 1,
            },
        )
        return self._first_or_none(self._json_list(resp))

    def list_admin_users(self) -> list[dict[str, Any]]:
        resp = self._request(
            "GET",
            "/admin_users",
            params={
                "select": "id,email,display_name,status,created_at,updated_at,last_login_at",
                "order": "id.desc",
            },
        )
        return self._json_list(resp)

    def upsert_admin_user(self, *, email: str, display_name: str, password_hash: str, status: str = "active") -> dict[str, Any]:
        body = [
            {
                "email": email.strip().lower(),
                "display_name": display_name.strip(),
                "password_hash": password_hash,
                "status": status,
            }
        ]
        resp = self._request(
            "POST",
            "/admin_users",
            params={"on_conflict": "email"},
            payload=body,
            prefer="resolution=merge-duplicates,return=representation",
        )
        items = self._json_list(resp)
        return self._first_or_none(items) or {}

    def touch_admin_login(self, user_id: int) -> None:
        body = {"last_login_at": self._now_iso(), "updated_at": self._now_iso()}
        self._request(
            "PATCH",
            "/admin_users",
            params={"id": f"eq.{int(user_id)}"},
            payload=body,
            prefer="return=minimal",
        )

    def count_admin_users(self) -> int:
        resp = self._request(
            "GET",
            "/admin_users",
            params={"select": "id", "limit": 1},
            prefer="count=exact",
        )
        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            total = content_range.split("/")[-1].strip()
            try:
                return int(total)
            except ValueError:
                pass
        return len(self._json_list(resp))

    def get_user_voice(self, chat_id: str, *, default_voice: str) -> str:
        resp = self._request(
            "GET",
            "/user_preferences",
            params={"select": "voice", "chat_id": f"eq.{chat_id}", "limit": 1},
        )
        item = self._first_or_none(self._json_list(resp))
        voice = str((item or {}).get("voice") or "").strip().lower()
        return voice or default_voice

    def set_user_voice(self, chat_id: str, voice: str) -> None:
        body = [{"chat_id": chat_id, "voice": voice.lower()}]
        self._request(
            "POST",
            "/user_preferences",
            params={"on_conflict": "chat_id"},
            payload=body,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def get_user_language(self, chat_id: str, *, default_language: str) -> str:
        resp = self._request(
            "GET",
            "/user_preferences",
            params={"select": "language", "chat_id": f"eq.{chat_id}", "limit": 1},
        )
        item = self._first_or_none(self._json_list(resp))
        language = str((item or {}).get("language") or "").strip()
        return language or default_language

    def set_user_language(self, chat_id: str, language: str) -> None:
        body = [{"chat_id": chat_id, "language": language}]
        self._request(
            "POST",
            "/user_preferences",
            params={"on_conflict": "chat_id"},
            payload=body,
            prefer="resolution=merge-duplicates,return=minimal",
        )
