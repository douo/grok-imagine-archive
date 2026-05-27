from __future__ import annotations

import base64
import random
import re
import string
import uuid
from urllib.parse import urlparse

from .config import AccountConfig


_CHAR_MAP = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
    }
)


def _sanitize(value: str | None, *, strip_spaces: bool = False) -> str:
    out = (value or "").translate(_CHAR_MAP)
    out = re.sub(r"\s+", "", out) if strip_spaces else out.strip()
    return out.encode("latin-1", errors="ignore").decode("latin-1")


def _major_version(browser: str, ua: str) -> str | None:
    for src in (browser, ua):
        match = re.search(r"(\d{2,3})", src or "")
        if match:
            return match.group(1)
    return None


def _client_hints(browser: str, ua: str) -> dict[str, str]:
    version = _major_version(browser, ua) or "136"
    if "mac" in ua.lower():
        platform = '"macOS"'
    elif "windows" in ua.lower():
        platform = '"Windows"'
    elif "linux" in ua.lower():
        platform = '"Linux"'
    else:
        platform = '"macOS"'
    return {
        "sec-ch-ua": f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }


def _statsig_id() -> str:
    if random.choice((True, False)):
        rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        msg = f"e:TypeError: Cannot read properties of null (reading 'children['{rand}']')"
    else:
        rand = "".join(random.choices(string.ascii_lowercase, k=10))
        msg = f"e:TypeError: Cannot read properties of undefined (reading '{rand}')"
    return base64.b64encode(msg.encode()).decode()


def build_cookie(account: AccountConfig) -> str:
    token = account.sso[4:] if account.sso.startswith("sso=") else account.sso
    token = _sanitize(token, strip_spaces=True)
    cookie = f"sso={token}; sso-rw={token}"
    extra = _sanitize(account.cf_cookies)
    clearance = _sanitize(account.cf_clearance, strip_spaces=True)
    if clearance and extra:
        if re.search(r"(?:^|;\s*)cf_clearance=", extra):
            extra = re.sub(
                r"(^|;\s*)cf_clearance=[^;]*",
                r"\1cf_clearance=" + clearance,
                extra,
                count=1,
            )
        else:
            extra = f"{extra.rstrip('; ')}; cf_clearance={clearance}"
    elif clearance:
        extra = f"cf_clearance={clearance}"
    if extra:
        cookie += f"; {extra}"
    return cookie


def build_headers(
    account: AccountConfig,
    *,
    content_type: str = "application/json",
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
    include_cookie: bool = True,
) -> dict[str, str]:
    ua = _sanitize(account.user_agent)
    origin = _sanitize(origin)
    referer = _sanitize(referer)
    origin_host = urlparse(origin).hostname
    referer_host = urlparse(referer).hostname
    site = "same-origin" if origin_host and origin_host == referer_host else "same-site"
    if content_type in ("image/jpeg", "image/png", "video/mp4", "video/webm"):
        accept = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        )
        dest = "document"
    else:
        accept = "*/*"
        dest = "empty"
    headers = {
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Baggage": (
            "sentry-environment=production,"
            "sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,"
            "sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c"
        ),
        "Content-Type": content_type,
        "Origin": origin,
        "Priority": "u=1, i",
        "Referer": referer,
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": site,
        "User-Agent": ua,
        "x-statsig-id": _statsig_id(),
        "x-xai-request-id": str(uuid.uuid4()),
    }
    headers.update(_client_hints(account.browser, ua))
    if include_cookie:
        headers["Cookie"] = build_cookie(account)
    return headers
