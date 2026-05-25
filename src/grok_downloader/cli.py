from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from .archive import Archive
from .client import GrokClient
from .config import DEFAULT_CONFIG, get_account, load_accounts
from .download import download_pending_assets
from .sync import sync_account
from .verify import verify_account
from .web import create_app


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grok-downloader")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_check = auth_sub.add_parser("check")
    add_account_arg(auth_check)
    auth_check.set_defaults(func=cmd_auth_check)

    sync = sub.add_parser("sync")
    add_account_arg(sync)
    sync.add_argument("--full", action="store_true", help="run until all listing cursors are exhausted")
    sync.add_argument("--limit", type=int, default=None, help="limit root liked posts for a small test sync")
    sync.add_argument("--page-limit", type=int, default=40)
    sync.add_argument("--no-download", action="store_true")
    sync.add_argument("--download-concurrency", type=int, default=6)
    sync.set_defaults(func=cmd_sync)

    download = sub.add_parser("download")
    add_account_arg(download)
    download.add_argument("--concurrency", type=int, default=8)
    download.add_argument("--no-retry-failed", action="store_true")
    download.set_defaults(func=cmd_download)

    status = sub.add_parser("status")
    add_account_arg(status)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    verify = sub.add_parser("verify")
    add_account_arg(verify)
    verify.set_defaults(func=cmd_verify)

    web = sub.add_parser("web")
    add_account_arg(web)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=7860)
    web.add_argument(
        "--token-env",
        default="GROK_DOWNLOADER_WEB_TOKEN",
        help="environment variable containing the optional Web UI access token",
    )
    web.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="allow binding Web UI to a non-loopback host without an access token",
    )
    web.set_defaults(func=cmd_web)
    return parser


def add_account_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="andy")


def cmd_auth_check(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    with GrokClient(account, timeout=30) as client:
        response = client.folder_list()
    with Archive(account.alias) as archive:
        folders = archive.upsert_folders(response)
    print(f"auth ok: account={account.alias} folders={len(folders)}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    summary = sync_account(
        account,
        full=bool(args.full),
        limit_posts=args.limit,
        page_limit=args.page_limit,
        download=not args.no_download,
        download_concurrency=args.download_concurrency,
    )
    print(
        "sync done: "
        f"account={account.alias} pages={summary.pages} folders={summary.folders} "
        f"posts={summary.posts_seen} assets={summary.assets_seen} "
        f"downloaded={summary.downloaded} errors={summary.errors}"
    )
    return 0 if summary.errors == 0 else 2


def cmd_download(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    summary = download_pending_assets(
        account,
        concurrency=args.concurrency,
        retry_failed=not args.no_retry_failed,
        progress=True,
    )
    print(
        "download done: "
        f"account={account.alias} total={summary.total} downloaded={summary.downloaded} "
        f"already_present={summary.already_present} failed={summary.failed}"
    )
    return 0 if summary.failed == 0 else 2


def cmd_verify(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    summary = verify_account(account.alias)
    print(
        "verify: "
        f"account={account.alias} posts={summary.posts} images={summary.images} "
        f"videos={summary.videos} thumbnails={summary.thumbnails} "
        f"downloaded={summary.downloaded} failed={summary.failed} "
        f"missing={summary.missing} hash_mismatches={summary.hash_mismatches}"
    )
    return 0 if summary.failed == 0 and summary.missing == 0 and summary.hash_mismatches == 0 else 2


def cmd_status(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    with Archive(account.alias) as archive:
        status = archive.status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        latest = status["latest_run"] or {}
        print(
            "status: "
            f"account={account.alias} posts={status['posts']} images={status['images']} "
            f"videos={status['videos']} thumbnails={status['thumbnails']} "
            f"downloaded={status['downloaded']} failed={status['failed']} "
            f"missing={status['missing']} latest_run={latest.get('status', 'none')}"
        )
        print(f"archive={status['archive_root']}")
    return 0 if status["failed"] == 0 and status["missing"] == 0 else 2


def cmd_web(args: argparse.Namespace) -> int:
    account = get_account(args.account, args.config)
    aliases = [
        configured.alias
        for configured in load_accounts(args.config)
        if configured.enabled
    ]
    access_token = os.getenv(args.token_env, "").strip()
    if not is_loopback_host(args.host) and not access_token and not args.allow_unauthenticated:
        raise ValueError(
            "refusing to bind unauthenticated Web UI to a non-loopback host; "
            f"set {args.token_env} or pass --allow-unauthenticated"
        )
    app = create_app(account.alias, access_token=access_token, aliases=aliases)
    print(f"web ui: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}
