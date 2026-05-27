from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(os.getenv("GROK_IMAGINE_ARCHIVE_CONFIG", "config/accounts.toml")).expanduser()
DEFAULT_ARCHIVE = Path(os.getenv("GROK_IMAGINE_ARCHIVE_ROOT", "archive")).expanduser()


@dataclass(frozen=True)
class AccountConfig:
    alias: str
    sso: str
    enabled: bool = True
    cf_clearance: str = ""
    cf_cookies: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )
    browser: str = "chrome136"
    proxy: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "AccountConfig":
        alias = str(data.get("alias") or "").strip()
        sso = str(data.get("sso") or "").strip()
        if not alias:
            raise ValueError("account entry is missing alias")
        if not sso:
            raise ValueError(f"account {alias!r} is missing sso")
        return cls(
            alias=alias,
            enabled=bool(data.get("enabled", True)),
            sso=sso,
            cf_clearance=str(data.get("cf_clearance") or "").strip(),
            cf_cookies=str(data.get("cf_cookies") or "").strip(),
            user_agent=str(data.get("user_agent") or cls.user_agent).strip(),
            browser=str(data.get("browser") or cls.browser).strip(),
            proxy=str(data.get("proxy") or "").strip(),
        )


def project_root() -> Path:
    return Path.cwd()


def archive_root() -> Path:
    return Path(os.getenv("GROK_IMAGINE_ARCHIVE_ROOT", str(DEFAULT_ARCHIVE))).expanduser()


def load_accounts(config_path: Path = DEFAULT_CONFIG) -> list[AccountConfig]:
    if not config_path.exists():
        raise FileNotFoundError(f"missing config file: {config_path}")
    raw = tomllib.loads(config_path.read_text())
    entries = raw.get("accounts")
    if not isinstance(entries, list):
        raise ValueError("config must contain [[accounts]] entries")
    return [AccountConfig.from_mapping(entry) for entry in entries]


def get_account(alias: str, config_path: Path = DEFAULT_CONFIG) -> AccountConfig:
    accounts = load_accounts(config_path)
    for account in accounts:
        if account.alias == alias:
            if not account.enabled:
                raise ValueError(f"account {alias!r} is disabled")
            return account
    aliases = ", ".join(account.alias for account in accounts) or "<none>"
    raise ValueError(f"account {alias!r} not found; configured accounts: {aliases}")
