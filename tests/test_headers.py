from grok_imagine_archive.config import AccountConfig
from grok_imagine_archive.headers import build_cookie, build_headers
from grok_imagine_archive.statsig import is_real_statsig


def _account(**overrides: object) -> AccountConfig:
    data = {
        "alias": "demo",
        "sso": "test-sso-token",
        "cf_clearance": "clearance-value",
        "cf_cookies": "x-anonuserid=anon-1; x-challenge=chal; x-signature=sig; x-userid=user-1; __cf_bm=bm",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "browser": "chrome150",
    }
    data.update(overrides)
    return AccountConfig.from_mapping(data)


def test_build_cookie_includes_device_and_antibot_tokens() -> None:
    cookie = build_cookie(_account())
    assert "sso=test-sso-token" in cookie
    assert "sso-rw=test-sso-token" in cookie
    assert "grok_device_id=" in cookie
    assert "cf_clearance=clearance-value" in cookie
    assert "x-anonuserid=anon-1" in cookie
    assert "x-challenge=chal" in cookie
    assert "x-signature=sig" in cookie
    assert "x-userid=user-1" in cookie
    assert "__cf_bm=bm" in cookie
    # Auth cookies from extras must not be duplicated / override controlled values.
    assert cookie.count("sso=") == 1


def test_build_cookie_prefers_browser_device_id() -> None:
    cookie = build_cookie(
        _account(cf_cookies="grok_device_id=browser-device-123; x-userid=user-1")
    )
    assert "grok_device_id=browser-device-123" in cookie
    assert cookie.count("grok_device_id=") == 1


def test_minimal_headers_use_real_statsig_and_skip_fingerprint_noise() -> None:
    headers = build_headers(
        _account(),
        method="POST",
        path="/rest/media/post/list",
        minimal=True,
    )
    assert is_real_statsig(headers["x-statsig-id"])
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Cookie" in headers
    assert "Content-Type" in headers
    # Minimal mode omits easily-mismatched browser fingerprint headers.
    for key in ("Baggage", "sentry-trace", "sec-ch-ua", "Sec-Ch-Ua", "Origin", "Referer"):
        assert key not in headers


def test_full_headers_include_client_hints() -> None:
    headers = build_headers(
        _account(),
        method="POST",
        path="/rest/media/folder/list",
        minimal=False,
    )
    assert "sec-ch-ua" in headers
    assert "150" in headers["sec-ch-ua"]
    assert headers["Referer"]
    assert is_real_statsig(headers["x-statsig-id"])
