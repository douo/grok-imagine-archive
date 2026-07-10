import json

from grok_imagine_archive.client import is_cloudflare_challenge
from grok_imagine_archive.client import is_grok_antibot
from grok_imagine_archive.client import normalize_impersonate
from grok_imagine_archive.client import response_error_message


class DummyResponse:
    def __init__(
        self,
        *,
        status_code: int = 403,
        headers: dict[str, str] | None = None,
        text: str = "",
        payload: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if payload is None else json.dumps(payload)
        self._payload = payload

    def json(self) -> dict:
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


def test_normalize_impersonate_keeps_supported_browser() -> None:
    assert normalize_impersonate("chrome146") == "chrome146"


def test_normalize_impersonate_falls_back_to_closest_supported_chrome() -> None:
    assert normalize_impersonate("chrome148") == "chrome146"


def test_detects_cloudflare_challenge_header() -> None:
    response = DummyResponse(headers={"cf-mitigated": "challenge"})

    assert is_cloudflare_challenge(response)


def test_detects_cloudflare_challenge_html() -> None:
    response = DummyResponse(
        headers={"server": "cloudflare", "content-type": "text/html; charset=UTF-8"},
        text="<!doctype html><title>Just a moment...</title>",
    )

    assert is_cloudflare_challenge(response)


def test_cloudflare_challenge_error_message_is_actionable() -> None:
    response = DummyResponse(headers={"cf-mitigated": "challenge"})

    message = response_error_message("/rest/media/folder/list", response)

    assert "HTTP 403" in message
    assert "Cloudflare challenge detected" in message
    assert "Refresh cf_clearance" in message
    assert "IP-bound" in message


def test_detects_grok_antibot_code7() -> None:
    response = DummyResponse(
        status_code=403,
        headers={"content-type": "application/json"},
        payload={"error": {"code": 7, "message": "Request rejected by anti-bot rules."}},
    )
    assert is_grok_antibot(response)
    message = response_error_message("/rest/media/post/list", response)
    assert "anti-bot" in message.lower()
