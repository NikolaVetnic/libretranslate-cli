"""Minimal LibreTranslate HTTP client (standard library only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LTError(Exception):
    """The server answered with an error."""


class LTUnreachable(LTError):
    """The server could not be reached at all."""


class LibreTranslate:
    def __init__(self, url: str, api_key: str | None = None, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            if self.api_key:
                payload = {**payload, "api_key": self.api_key}
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(body).get("error", body)
            except (ValueError, AttributeError):
                msg = body or e.reason
            raise LTError(f"HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise LTUnreachable(f"cannot reach LibreTranslate at {self.url} ({reason})") from None

    def languages(self) -> list[dict[str, Any]]:
        return self._request("GET", "/languages")

    def detect(self, text: str) -> list[dict[str, Any]]:
        return self._request("POST", "/detect", {"q": text})

    def translate(
        self,
        text: str,
        target: str,
        source: str = "auto",
        fmt: str = "text",
        alternatives: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"q": text, "source": source, "target": target, "format": fmt}
        if alternatives:
            payload["alternatives"] = alternatives
        return self._request("POST", "/translate", payload)

    def is_ready(self) -> bool:
        try:
            return bool(self.languages())
        except LTError:
            return False
