"""Telegram HTTPS adapter. Exceptions carry codes, never token-bearing URLs."""

from __future__ import annotations

import json
import mimetypes
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class TransportError(Exception):
    def __init__(self, code: int, retry_after: int = 0, *, unchanged: bool = False) -> None:
        super().__init__(f"Telegram error {code}")
        self.code = code
        self.retry_after = retry_after
        self.unchanged = unchanged


class TelegramAPI:
    def __init__(self, token: str, timeout: int = 8) -> None:
        if not token or ":" not in token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN in your local .env file")
        self._token = token
        self.timeout = timeout
        self.username = ""
        self._last: dict[int, float] = {}
        self._global_last = 0.0

    def _throttle(self, chat_id: int) -> None:
        now = time.monotonic()
        delay = max(self._last.get(chat_id, 0) + 1.05 - now, self._global_last + 0.04 - now, 0)
        if delay:
            time.sleep(delay)
        self._last[chat_id] = self._global_last = time.monotonic()

    def _request(
        self, method: str, payload: dict[str, Any], upload: tuple[str, str, bytes] | None = None,
        *, request_timeout: int | None = None,
    ) -> Any:
        if upload:
            boundary = secrets.token_hex(16)
            body = bytearray()
            for key, value in payload.items():
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            field, filename, data = upload
            safe_name = Path(filename).name.replace('"', "").replace("\r", "").replace("\n", "")
            mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
            )
            body.extend(data)
            body.extend(f"\r\n--{boundary}--\r\n".encode())
            content_type = f"multipart/form-data; boundary={boundary}"
            raw = bytes(body)
        else:
            raw = json.dumps(payload).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}", raw, {"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=request_timeout or max(self.timeout, int(payload.get("timeout", 0)) + 5)
            ) as response:
                result = json.loads(response.read(10_000_000))
        except urllib.error.HTTPError as exc:
            retry_after = 0
            unchanged = False
            try:
                error = json.loads(exc.read(20_000))
                retry_after = int(error.get("parameters", {}).get("retry_after", 0))
                unchanged = "message is not modified" in str(error.get("description", "")).lower()
            except (ValueError, TypeError):
                pass
            raise TransportError(exc.code, retry_after, unchanged=unchanged) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TransportError(503) from None
        if not result.get("ok"):
            raise TransportError(
                int(result.get("error_code", 503)), int(result.get("parameters", {}).get("retry_after", 0)),
                unchanged="message is not modified" in str(result.get("description", "")).lower(),
            )
        return result["result"]

    def me(self) -> dict[str, Any]:
        result: dict[str, Any] = self._request("getMe", {})
        self.username = result.get("username", "")
        return result

    def updates(self, offset: int, timeout: int = 20) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._request(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": 100, "allowed_updates": ["message", "callback_query"]},
        )
        return result

    def send(self, chat_id: int, text: str, keyboard: Any = None) -> dict[str, Any]:
        self._throttle(chat_id)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard} if isinstance(keyboard, list) else keyboard
        result: dict[str, Any] = self._request("sendMessage", payload)
        return result

    def edit(
        self, chat_id: int, message_id: int, text: str, keyboard: Any = None, *, caption: bool = False
    ) -> dict[str, Any]:
        if caption and len(text) > 1024:
            return self.send(chat_id, text, keyboard)
        self._throttle(chat_id)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption" if caption else "text": text[:1024 if caption else 4096],
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        try:
            result: dict[str, Any] = self._request("editMessageCaption" if caption else "editMessageText", payload)
            return result
        except TransportError as exc:
            if exc.code == 400 and exc.unchanged:
                return {"message_id": message_id}
            if exc.code == 400:
                # Deleted/too-old message: navigation can safely send a new one.
                return self.send(chat_id, text, keyboard)
            raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self._request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]},
                          request_timeout=3)
        except TransportError as exc:
            if exc.code not in (400, 429, 500, 502, 503, 504):
                # A failed optional spinner acknowledgement must not discard the action.
                raise

    def document(self, chat_id: int, filename: str, data: bytes) -> dict[str, Any]:
        self._throttle(chat_id)
        result: dict[str, Any] = self._request("sendDocument", {"chat_id": chat_id}, ("document", filename, data))
        return result

    def photo(self, chat_id: int, path: str, caption: str = "", keyboard: Any = None) -> dict[str, Any]:
        self._throttle(chat_id)
        payload: dict[str, Any] = {"chat_id": chat_id, "caption": caption[:1024]}
        if keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        if not Path(path).is_file():
            payload["photo"] = path
            return self._request("sendPhoto", payload)
        result: dict[str, Any] = self._request(
            "sendPhoto",
            payload,
            ("photo", Path(path).name, Path(path).read_bytes()),
        )
        return result

    def file(self, file_id: str) -> bytes:
        info = self._request("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > 1_000_000:
            raise TransportError(413)
        path = info.get("file_path", "")
        if not path or ".." in path or ":" in path or "?" in path:
            raise TransportError(400)
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/file/bot{self._token}/{path}", timeout=self.timeout
            ) as response:
                data = response.read(1_000_001)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError(503) from None
        if len(data) > 1_000_000:
            raise TransportError(413)
        return data


class FakeTransport:
    """No network. Deterministic adapter for simulated Telegram interactions."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.username = "KitMode_demo_bot"
        self.files: dict[str, bytes] = {}

    def send(self, chat_id: int, text: str, keyboard: Any = None) -> dict[str, Any]:
        event = dict(method="send", chat_id=chat_id, text=text, keyboard=keyboard, message_id=len(self.events) + 1)
        self.events.append(event)
        return event

    def edit(
        self, chat_id: int, message_id: int, text: str, keyboard: Any = None, *, caption: bool = False
    ) -> dict[str, Any]:
        event = self.send(chat_id, text, keyboard)
        event["method"], event["message_id"] = "edit_caption" if caption else "edit", message_id
        return event

    def answer(self, callback_id: str, text: str = "") -> None:
        self.events.append(dict(method="answer", callback_id=callback_id, text=text))

    def document(self, chat_id: int, filename: str, data: bytes) -> dict[str, Any]:
        event = dict(method="document", chat_id=chat_id, filename=filename, data=data)
        self.events.append(event)
        return event

    def photo(self, chat_id: int, path: str, caption: str = "", keyboard: Any = None) -> dict[str, Any]:
        event = dict(method="photo", chat_id=chat_id, path=path, caption=caption, text=caption, keyboard=keyboard,
                     message_id=len(self.events) + 1, photo=[{"file_id": "fake-welcome-photo"}])
        self.events.append(event)
        return event

    def file(self, file_id: str) -> bytes:
        return self.files[file_id]
