from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from .config import AccountConfig
from .statsig import generate as generate_statsig


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
        match = re.search(r"(?:chrome|Chrome)/?(\d{2,3})", src or "")
        if match:
            return match.group(1)
        match = re.search(r"(\d{2,3})", src or "")
        if match:
            return match.group(1)
    return None


def _client_hints(browser: str, ua: str) -> dict[str, str]:
    """Optional client hints. Prefer version from UA so it stays consistent."""
    version = _major_version("", ua) or _major_version(browser, "") or "146"
    lowered = (ua or "").lower()
    if "mac" in lowered:
        platform = '"macOS"'
    elif "windows" in lowered:
        platform = '"Windows"'
    elif "linux" in lowered:
        platform = '"Linux"'
    else:
        platform = '"macOS"'
    # Match current Chrome order observed in live HAR captures.
    return {
        "sec-ch-ua": f'"Not;A=Brand";v="8", "Chromium";v="{version}", "Google Chrome";v="{version}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }


def build_cookie(account: AccountConfig) -> str:
    """Build Cookie header aligned with browser + grok2api.

    Includes sso/sso-rw, grok_device_id (prefer browser value from cf_cookies),
    optional anti-bot session cookies (x-anonuserid / x-challenge / x-signature /
    x-userid / __cf_bm / ...), and cf_clearance.
    """
    token = account.sso[4:] if account.sso.startswith("sso=") else account.sso
    token = _sanitize(token, strip_spaces=True)
    extra = _sanitize(account.cf_cookies)
    clearance = _sanitize(account.cf_clearance, strip_spaces=True)

    # Prefer the browser-issued device id when present in the pasted cookie jar.
    device_id = ""
    if extra:
        match = re.search(r"(?:^|;\s*)grok_device_id=([^;]+)", extra)
        if match:
            device_id = match.group(1).strip()
    if not device_id:
        # Deterministic fallback so parallel workers don't thrash sessions.
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"grok-device:{token}"))

    parts = [f"sso={token}", f"sso-rw={token}", f"grok_device_id={device_id}"]

    # Drop auth cookies from extras so we control their values / order.
    if extra:
        cleaned: list[str] = []
        for piece in extra.split(";"):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            name = piece.split("=", 1)[0].strip().lower()
            if name in {"sso", "sso-rw", "grok_device_id", "cf_clearance"}:
                continue
            cleaned.append(piece)
        extra = "; ".join(cleaned)

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
        parts.append(extra)
    return "; ".join(parts)


def build_headers(
    account: AccountConfig,
    *,
    content_type: str = "application/json",
    origin: str = "https://grok.com",
    referer: str = "https://grok.com/",
    include_cookie: bool = True,
    method: str = "POST",
    path: str = "/rest/media/post/list",
    minimal: bool = True,
) -> dict[str, str]:
    """Build request headers.

    Default ``minimal=True`` follows auroro-grok2api: only the headers grok's
    anti-bot path actually needs. Extra browser-fingerprint headers (Baggage,
    sentry-trace, mismatched Sec-Ch-Ua, etc.) can increase code:7 rejections.
    """
    ua = _sanitize(account.user_agent)
    origin = _sanitize(origin)
    referer = _sanitize(referer)
    path = path or urlparse(referer).path or "/rest/media/post/list"
    method = (method or "POST").upper()

    headers: dict[str, str] = {
        "Content-Type": content_type,
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "x-statsig-id": generate_statsig(path, method),
        "x-xai-request-id": str(uuid.uuid4()),
    }
    if include_cookie:
        headers["Cookie"] = build_cookie(account)

    if not minimal:
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
        headers.update(
            {
                "Accept": accept,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": origin,
                "Priority": "u=1, i",
                "Referer": referer,
                "Sec-Fetch-Dest": dest,
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": site,
            }
        )
        headers.update(_client_hints(account.browser, ua))
    return headers
