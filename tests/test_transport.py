from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from silver_bullet.transport import ExternalServiceError, UrllibJsonTransport


def _server_error(url):
    return urllib.error.HTTPError(
        url,
        503,
        "unavailable",
        {},
        io.BytesIO(b'{"message":"temporary"}'),
    )


def test_read_request_retries_transient_server_error(monkeypatch):
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise _server_error(request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    transport = UrllibJsonTransport(
        maximum_attempts=3, retry_delay_seconds=0
    )

    with pytest.raises(ExternalServiceError, match="temporary"):
        transport.request("GET", "https://provider.test/account")

    assert len(calls) == 3


def test_order_post_is_never_retried_after_ambiguous_server_error(monkeypatch):
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise _server_error(request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    transport = UrllibJsonTransport(
        maximum_attempts=3, retry_delay_seconds=0
    )

    with pytest.raises(ExternalServiceError, match="temporary"):
        transport.request(
            "POST", "https://provider.test/orders", body={"symbol": "ES"}
        )

    assert len(calls) == 1
