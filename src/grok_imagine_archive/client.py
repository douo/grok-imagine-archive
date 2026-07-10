from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import Session
from curl_cffi.requests.impersonate import BrowserTypeLiteral
from typing import get_args

from .config import AccountConfig
from .headers import build_headers


class GrokClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class GrokClient:
    account: AccountConfig
    timeout: int = 60
    impersonate: str = field(init=False)
    session: Session = field(init=False)
    proxies: dict[str, str] | None = field(init=False)

    def __post_init__(self) -> None:
        self.impersonate = normalize_impersonate(self.account.browser)
        self.session = self._new_session()
        self.proxies = (
            {"http": self.account.proxy, "https": self.account.proxy}
            if self.account.proxy
            else None
        )

    def _new_session(self) -> Session:
        return Session(impersonate=self.impersonate)

    def reset_session(self) -> None:
        """Drop TLS session state (used after Cloudflare 403 challenges)."""
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._new_session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "GrokClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"https://grok.com{path}"
        headers = build_headers(
            self.account,
            method="POST",
            path=path,
            referer="https://grok.com/imagine/saved",
            origin="https://grok.com",
        )
        response = self.session.post(
            url,
            data=json.dumps(payload, separators=(",", ":")),
            headers=headers,
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if response.status_code != 200:
            if is_cloudflare_challenge(response):
                self.reset_session()
            raise GrokClientError(
                response_error_message(path, response),
                status_code=response.status_code,
            )
        try:
            return response.json()
        except Exception as exc:
            raise GrokClientError(f"{path} returned non-JSON response: {exc}") from exc

    def get(self, url: str, *, timeout: int | None = None, stream: bool = True):
        parsed = urlparse(url)
        headers = build_headers(
            self.account,
            content_type="application/octet-stream",
            origin="https://grok.com",
            referer="https://grok.com/imagine",
            method="GET",
            path=parsed.path or "/",
        )
        # Asset CDNs do not need JSON content-type.
        headers.pop("Content-Type", None)
        return self.session.get(
            url,
            headers=headers,
            timeout=timeout or self.timeout,
            proxies=self.proxies,
            stream=stream,
        )

    def folder_list(self) -> dict[str, Any]:
        return self.post("/rest/media/folder/list", {})

    def post_list(
        self,
        *,
        cursor: str = "",
        limit: int = 40,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "limit": limit,
            "filter": {
                "source": "MEDIA_POST_SOURCE_LIKED",
                "safeForWork": False,
            },
        }
        if cursor:
            payload["cursor"] = cursor
        if folder_id:
            payload["filter"]["folderId"] = folder_id
        return self.post("/rest/media/post/list", payload)

    def post_folders(self, post_id: str) -> Any:
        return self.post("/rest/media/post/folders", {"postId": post_id})


def normalize_impersonate(browser: str | None) -> str:
    requested = (browser or "").strip() or "chrome"
    supported = {str(item) for item in get_args(BrowserTypeLiteral)}
    if requested in supported:
        return requested
    match = re.fullmatch(r"chrome(\d+)", requested)
    if match:
        requested_version = int(match.group(1))
        chrome_versions = sorted(
            int(name.removeprefix("chrome"))
            for name in supported
            if re.fullmatch(r"chrome\d+", name)
        )
        compatible = [version for version in chrome_versions if version <= requested_version]
        if compatible:
            return f"chrome{compatible[-1]}"
    return "chrome"


def response_error_message(path: str, response: Any) -> str:
    status_code = getattr(response, "status_code", "unknown")
    message = f"{path} failed with HTTP {status_code}"
    if is_cloudflare_challenge(response):
        return (
            f"{message}; Cloudflare challenge detected. Refresh cf_clearance and keep "
            "browser/user_agent aligned with the browser session that produced it; "
            "cf_clearance is IP-bound — use the same egress/proxy as the browser; "
            "also paste full browser Cookie into cf_cookies when possible "
            "(x-anonuserid/x-challenge/x-signature/x-userid)."
        )
    if is_grok_antibot(response):
        return (
            f"{message}; Grok anti-bot rejected the request (code:7). "
            "Check x-statsig-id generation, cookie session tokens, and UA/TLS alignment."
        )
    return message


def is_cloudflare_challenge(response: Any) -> bool:
    headers = {str(key).lower(): str(value).lower() for key, value in response.headers.items()}
    if headers.get("cf-mitigated") == "challenge":
        return True
    server = headers.get("server", "")
    content_type = headers.get("content-type", "")
    if "cloudflare" not in server or "text/html" not in content_type:
        return False
    body = str(getattr(response, "text", "") or "").lower()
    return "just a moment" in body or "challenges.cloudflare.com" in body


def is_grok_antibot(response: Any) -> bool:
    """Detect grok application-level anti-bot rejection (not Cloudflare HTML)."""
    body = str(getattr(response, "text", "") or "")
    if not body:
        return False
    lowered = body.lower()
    if "anti-bot" in lowered or "request rejected by anti-bot" in lowered:
        return True
    try:
        data = response.json()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if isinstance(err, dict) and err.get("code") == 7:
        return True
    return data.get("code") == 7
