"""Small JSON-over-HTTP transport shared by optional broker integrations."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any: ...


class ExternalServiceError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class UrllibJsonTransport:
    timeout_seconds: float = 15.0
    maximum_attempts: int = 3
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        if self.maximum_attempts < 1:
            raise ValueError("HTTP maximum attempts must be positive")
        if self.retry_delay_seconds < 0:
            raise ValueError("HTTP retry delay cannot be negative")

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        query = _encode_query(params or {})
        target = f"{url}?{query}" if query else url
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            target, data=data, headers=request_headers, method=method.upper()
        )
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    payload = response.read()
                return None if not payload else json.loads(payload)
            except urllib.error.HTTPError as exc:
                payload = exc.read().decode("utf-8", errors="replace")
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise ExternalServiceError(
                        _error_message(payload, exc.reason), status=exc.code
                    ) from exc
                last_error = ExternalServiceError(
                    _error_message(payload, exc.reason), status=exc.code
                )
            except urllib.error.URLError as exc:
                last_error = ExternalServiceError(str(exc.reason))
            if attempt < self.maximum_attempts:
                time.sleep(self.retry_delay_seconds * attempt)
        raise last_error


def _encode_query(params: Mapping[str, Any]) -> str:
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = str(value).lower()
        elif isinstance(value, (tuple, list, set)):
            cleaned[key] = ",".join(str(item) for item in value)
        else:
            cleaned[key] = value
    return urllib.parse.urlencode(cleaned)


def _error_message(payload: str, fallback: Any) -> str:
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return payload or str(fallback)
    if isinstance(parsed, dict):
        return str(
            parsed.get("message")
            or parsed.get("errorText")
            or parsed.get("error")
            or parsed
        )
    return str(parsed)
