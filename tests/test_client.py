from grok_downloader.client import normalize_impersonate


def test_normalize_impersonate_keeps_supported_browser() -> None:
    assert normalize_impersonate("chrome146") == "chrome146"


def test_normalize_impersonate_falls_back_to_closest_supported_chrome() -> None:
    assert normalize_impersonate("chrome148") == "chrome146"
