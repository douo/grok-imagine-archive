from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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

    def __post_init__(self) -> None:
        self.impersonate = normalize_impersonate(self.account.browser)
        self.session = Session(impersonate=self.impersonate)
        self.proxies = (
            {"http": self.account.proxy, "https": self.account.proxy}
            if self.account.proxy
            else None
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "GrokClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"https://grok.com{path}"
        response = self.session.post(
            url,
            data=json.dumps(payload, separators=(",", ":")),
            headers=build_headers(self.account),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if response.status_code != 200:
            raise GrokClientError(
                f"{path} failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except Exception as exc:
            raise GrokClientError(f"{path} returned non-JSON response: {exc}") from exc

    def get(self, url: str, *, timeout: int | None = None, stream: bool = True):
        headers = build_headers(
            self.account,
            content_type="application/octet-stream",
            origin="https://grok.com",
            referer="https://grok.com/imagine",
        )
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
