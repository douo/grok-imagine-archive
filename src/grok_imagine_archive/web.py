from __future__ import annotations

import sqlite3
from secrets import compare_digest
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from .archive import Archive, json_loads


def create_app(
    alias: str,
    *,
    access_token: str = "",
    aliases: list[str] | tuple[str, ...] | None = None,
) -> FastAPI:
    account_aliases = normalize_aliases(alias, aliases)
    app = FastAPI(title=f"Grok Imagine Archive - {alias}")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not access_token or request.url.path == "/healthz":
            return await call_next(request)
        provided = extract_access_token(request)
        if not provided or not compare_digest(provided, access_token):
            if request.url.path == "/":
                return PlainTextResponse("Unauthorized", status_code=401)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        if request.query_params.get("token") == access_token:
            response.set_cookie(
                "grok_imagine_archive_token",
                access_token,
                httponly=True,
                samesite="lax",
                secure=False,
            )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "account": alias}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.replace("__ALIAS__", alias)

    @app.get("/api/accounts")
    def accounts() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in account_aliases:
            archive = Archive(account)
            exists = archive.db_path.exists()
            status: dict[str, Any] | None = None
            if exists:
                opened = archive.open_readonly()
                try:
                    status = opened.status()
                finally:
                    opened.close()
            result.append(
                {
                    "alias": account,
                    "selected": account == alias,
                    "archiveExists": exists,
                    "status": status,
                }
            )
        return result

    @app.get("/api/status")
    def status(account: str = "") -> dict[str, Any]:
        selected = select_accounts(account, account_aliases)
        if len(selected) == 1:
            return account_status(selected[0])
        return {"accounts": [account_status(current) for current in selected]}

    @app.get("/api/folders")
    def folders(account: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for current in select_accounts(account, account_aliases):
            archive = Archive(current)
            if not archive.db_path.exists():
                continue
            opened = archive.open_readonly()
            try:
                account_rows = opened.db.execute(
                    "SELECT id, name FROM folders ORDER BY name COLLATE NOCASE"
                ).fetchall()
            finally:
                opened.close()
            rows.extend(
                {"account": current, "id": row["id"], "name": row["name"]}
                for row in account_rows
            )
        rows.sort(key=lambda row: (row["account"].lower(), (row["name"] or row["id"]).lower()))
        return rows

    @app.get("/api/posts")
    def posts(
        account: str = "",
        folder: str = "",
        media: str = "",
        q: str = "",
        sort: str = "desc",
        limit: int = Query(80, ge=1, le=300),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        selected = select_accounts(account, account_aliases)
        order = "asc" if sort == "asc" else "desc"
        items: list[dict[str, Any]] = []
        total = 0
        for current in selected:
            page = query_posts_for_account(
                current,
                folder=folder,
                media=media,
                q=q,
                sort=order,
                limit=offset + limit,
                offset=0,
            )
            items.extend(page["items"])
            total += page["total"]
        items.sort(
            key=lambda item: (
                item.get("createTime") or "",
                item.get("account") or "",
                item.get("id") or "",
            ),
            reverse=order == "desc",
        )
        return {
            "items": items[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/posts/{post_id}")
    def post_detail(post_id: str, account: str = "") -> dict[str, Any]:
        for current in select_accounts(account, account_aliases):
            detail = query_post_detail(current, post_id)
            if detail is not None:
                return detail
        raise HTTPException(status_code=404)

    @app.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        media_account, relative_path = split_media_account(path, alias, account_aliases)
        requested = resolve_media_path(Archive(media_account).root, relative_path)
        if not requested.exists() or not requested.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            requested,
            media_type=guess_media_type(requested),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return app


def normalize_aliases(alias: str, aliases: list[str] | tuple[str, ...] | None) -> list[str]:
    ordered: list[str] = []
    for current in (alias, *(aliases or ())):
        if current and current not in ordered:
            ordered.append(current)
    return ordered


def select_accounts(account: str, aliases: list[str]) -> list[str]:
    if not account:
        return aliases
    if account not in aliases:
        raise HTTPException(status_code=404, detail=f"unknown account: {account}")
    return [account]


def account_status(alias: str) -> dict[str, Any]:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return {"account": alias, "archiveExists": False}
    opened = archive.open_readonly()
    try:
        status = opened.status()
    finally:
        opened.close()
    status["account"] = alias
    status["archiveExists"] = True
    return status


def query_posts_for_account(
    alias: str,
    *,
    folder: str,
    media: str,
    q: str,
    sort: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return {"items": [], "total": 0}
    where: list[str] = []
    params: list[Any] = []
    if folder:
        folder_account, separator, folder_id = folder.partition(":")
        if separator and folder_account != alias:
            return {"items": [], "total": 0}
        where.append(
            "EXISTS (SELECT 1 FROM post_folders pf WHERE pf.post_id = p.id AND pf.folder_id = ?)"
        )
        params.append(folder_id if separator else folder)
    if media:
        where.append("p.media_type = ?")
        params.append(media)
    if q:
        where.append("(p.id LIKE ? OR p.prompt LIKE ? OR p.original_prompt LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    order = "ASC" if sort == "asc" else "DESC"
    opened = archive.open_readonly()
    try:
        rows = opened.db.execute(
            f"""
            SELECT p.*, (
                SELECT local_path FROM assets a
                 WHERE a.post_id = p.id AND a.status = 'downloaded'
                 ORDER BY CASE
                   WHEN a.kind = 'video' THEN 0
                   WHEN a.role = 'thumbnail' THEN 1
                   WHEN a.role = 'media' THEN 2
                   ELSE 2
                 END, a.kind
                 LIMIT 1
            ) AS preview_path,
            (
                SELECT kind FROM assets a
                 WHERE a.post_id = p.id AND a.status = 'downloaded'
                 ORDER BY CASE
                   WHEN a.kind = 'video' THEN 0
                   WHEN a.role = 'thumbnail' THEN 1
                   WHEN a.role = 'media' THEN 2
                   ELSE 2
                 END, a.kind
                 LIMIT 1
            ) AS preview_kind,
            EXISTS (
                SELECT 1 FROM assets a
                 WHERE a.post_id = p.id
                   AND (a.role = 'source' OR a.source_path LIKE '%inputMediaItems%')
            ) AS has_uploaded_source,
            EXISTS (
                SELECT 1 FROM assets a
                 WHERE a.post_id = p.id AND a.kind = 'video'
            ) AS has_video,
            (SELECT COUNT(*) FROM post_edges e WHERE e.parent_post_id = p.id) AS child_count,
            (SELECT COUNT(*) FROM post_edges e WHERE e.child_post_id = p.id) AS parent_count
              FROM posts p
              {where_sql}
             ORDER BY p.create_time {order}
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        total = opened.db.execute(
            f"SELECT COUNT(*) AS n FROM posts p {where_sql}",
            params,
        ).fetchone()["n"]
    finally:
        opened.close()
    return {
        "items": [post_row_to_dict(row, account=alias) for row in rows],
        "total": total,
    }


def query_post_detail(alias: str, post_id: str) -> dict[str, Any] | None:
    archive = Archive(alias)
    if not archive.db_path.exists():
        return None
    opened = archive.open_readonly()
    try:
        row = opened.db.execute(
            """
            SELECT p.*,
                   NULL AS preview_path,
                   NULL AS preview_kind,
                   EXISTS (
                     SELECT 1 FROM assets a
                      WHERE a.post_id = p.id
                        AND (a.role = 'source' OR a.source_path LIKE '%inputMediaItems%')
                   ) AS has_uploaded_source,
                   EXISTS (
                     SELECT 1 FROM assets a
                      WHERE a.post_id = p.id AND a.kind = 'video'
                   ) AS has_video,
                   (SELECT COUNT(*) FROM post_edges e WHERE e.parent_post_id = p.id) AS child_count,
                   (SELECT COUNT(*) FROM post_edges e WHERE e.child_post_id = p.id) AS parent_count
              FROM posts p
             WHERE p.id = ?
            """,
            (post_id,),
        ).fetchone()
        if not row:
            return None
        assets = opened.db.execute(
            "SELECT * FROM assets WHERE post_id = ? ORDER BY kind, role",
            (post_id,),
        ).fetchall()
        folders = opened.db.execute(
            """
            SELECT folder_id, folder_name
              FROM post_folders
             WHERE post_id = ?
             ORDER BY folder_name
            """,
            (post_id,),
        ).fetchall()
        edges = opened.db.execute(
            """
            SELECT e.parent_post_id, e.child_post_id, e.relation,
                   parent.prompt AS parent_prompt,
                   parent.media_type AS parent_media_type,
                   child.prompt AS child_prompt,
                   child.media_type AS child_media_type
              FROM post_edges e
              LEFT JOIN posts parent ON parent.id = e.parent_post_id
              LEFT JOIN posts child ON child.id = e.child_post_id
             WHERE e.parent_post_id = ? OR e.child_post_id = ?
             ORDER BY e.relation, e.parent_post_id, e.child_post_id
            """,
            (post_id, post_id),
        ).fetchall()
    finally:
        opened.close()
    data = post_row_to_dict(row, account=alias)
    data["raw"] = json_loads(row["raw_json"], {})
    data["assets"] = [asset_row_to_dict(asset) for asset in assets]
    data["folders"] = [dict(folder) for folder in folders]
    data["edges"] = [dict(edge) for edge in edges]
    data["relationships"] = summarize_relationships(alias, post_id, data["edges"])
    data["hasUploadedSource"] = any(
        asset["role"] == "source" or "inputMediaItems" in (asset["sourcePath"] or "")
        for asset in data["assets"]
    )
    data["hasVideo"] = any(asset["kind"] == "video" for asset in data["assets"]) or (
        data["mediaType"] == "MEDIA_POST_TYPE_VIDEO"
    )
    return data


def summarize_relationships(
    alias: str, post_id: str, edges: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    original_parents: list[dict[str, Any]] = []
    original_children: list[dict[str, Any]] = []
    nested_parents: list[dict[str, Any]] = []
    nested_children: list[dict[str, Any]] = []
    for edge in edges:
        parent_id = edge["parent_post_id"]
        child_id = edge["child_post_id"]
        relation = edge["relation"]
        if relation == "original":
            if child_id == post_id:
                original_parents.append(
                    relationship_row(
                        alias, parent_id, edge.get("parent_prompt"), edge.get("parent_media_type")
                    )
                )
            elif parent_id == post_id:
                original_children.append(
                    relationship_row(
                        alias, child_id, edge.get("child_prompt"), edge.get("child_media_type")
                    )
                )
        elif relation == "nested":
            if child_id == post_id:
                nested_parents.append(
                    relationship_row(
                        alias, parent_id, edge.get("parent_prompt"), edge.get("parent_media_type")
                    )
                )
            elif parent_id == post_id:
                nested_children.append(
                    relationship_row(
                        alias, child_id, edge.get("child_prompt"), edge.get("child_media_type")
                    )
                )
    return {
        "originalParents": original_parents,
        "originalChildren": original_children,
        "nestedParents": nested_parents,
        "nestedChildren": nested_children,
    }


def relationship_row(
    alias: str, post_id: str, prompt: str | None, media_type: str | None
) -> dict[str, Any]:
    return {
        "account": alias,
        "id": post_id,
        "prompt": prompt or "",
        "mediaType": media_type or "",
    }


def split_media_account(path: str, default_alias: str, aliases: list[str]) -> tuple[str, str]:
    first, separator, rest = path.partition("/")
    if separator and first in aliases:
        return first, rest
    return default_alias, path


def extract_access_token(request: Request) -> str:
    header_token = request.headers.get("x-access-token", "")
    if header_token:
        return header_token
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    query_token = request.query_params.get("token", "")
    if query_token:
        return query_token
    return request.cookies.get("grok_imagine_archive_token", "")


def resolve_media_path(root: Path, path: str) -> Path:
    archive_root = root.resolve()
    requested = (archive_root / path).resolve()
    try:
        requested.relative_to(archive_root)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return requested


def post_row_to_dict(row: sqlite3.Row, *, account: str = "") -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "account": account,
        "id": row["id"],
        "createTime": row["create_time"],
        "mediaType": row["media_type"],
        "mimeType": row["mime_type"],
        "prompt": row["prompt"],
        "originalPrompt": row["original_prompt"],
        "modelName": row["model_name"],
        "width": row["width"],
        "height": row["height"],
        "originalPostId": row["original_post_id"],
        "parentPostId": row["parent_post_id"],
        "isLiked": bool(row["is_liked"]),
        "rRated": bool(row["r_rated"]),
        "previewPath": row["preview_path"] if "preview_path" in keys else None,
        "previewKind": row["preview_kind"] if "preview_kind" in keys else None,
        "hasUploadedSource": bool(row["has_uploaded_source"]) if "has_uploaded_source" in keys else False,
        "hasVideo": bool(row["has_video"]) if "has_video" in keys else False,
        "childCount": row["child_count"] if "child_count" in keys else 0,
        "parentCount": row["parent_count"] if "parent_count" in keys else 0,
    }


def asset_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "assetKey": row["asset_key"],
        "postId": row["post_id"],
        "kind": row["kind"],
        "role": row["role"],
        "url": row["url"],
        "mimeType": row["mime_type"],
        "sourcePath": row["source_path"],
        "localPath": row["local_path"],
        "status": row["status"],
        "sha256": row["sha256"],
        "size": row["size"],
        "failReason": row["fail_reason"],
    }


def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grok Imagine Archive - __ALIAS__</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #08090d;
      --surface: #10121a;
      --surface-2: #191c28;
      --text: #f3f5fa;
      --muted: #8c97a8;
      --line: #1e2233;
      --accent: #d5b35b;
      --accent-rgb: 213, 179, 91;
      --cyan: #4ab1c0;
      --green: #60b078;
      --red: #d25b52;
      --shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
      --transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(8, 9, 13, 0.8);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
    }
    .bar {
      display: grid;
      grid-template-columns: minmax(130px, auto) minmax(190px, 1fr) repeat(4, minmax(120px, auto));
      gap: 12px;
      align-items: center;
      padding: 14px 24px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #fff 0%, var(--muted) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    input, select, button {
      height: 38px;
      min-width: 0;
      border: 1px solid var(--line);
      background: var(--surface-2);
      color: var(--text);
      border-radius: 8px;
      padding: 0 12px;
      font: inherit;
      font-size: 13px;
      outline: none;
      transition: var(--transition);
    }
    input:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(var(--accent-rgb), 0.2);
    }
    input::placeholder {
      color: var(--muted);
      opacity: 0.8;
    }
    button {
      cursor: pointer;
      background: var(--surface-2);
      font-weight: 500;
    }
    button:hover {
      border-color: var(--accent);
      background: rgba(var(--accent-rgb), 0.1);
      color: var(--accent);
    }
    main { padding: 24px; }
    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 20px;
      color: var(--muted);
      font-weight: 500;
    }
    .grid {
      columns: 5 240px;
      column-gap: 16px;
    }
    .tile {
      display: inline-block;
      width: 100%;
      margin: 0 0 16px;
      break-inside: avoid;
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      background: var(--surface);
      cursor: pointer;
      box-shadow: var(--shadow);
      transition: var(--transition);
    }
    .tile:hover {
      border-color: rgba(var(--accent-rgb), 0.5);
      transform: translateY(-4px);
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.6);
    }
    .media-wrap {
      position: relative;
      background: #090a0f;
      overflow: hidden;
      display: block;
      width: 100%;
    }
    @keyframes skeleton-pulse {
      0% { background-color: #10121a; }
      50% { background-color: #1e212f; }
      100% { background-color: #10121a; }
    }
    .media-wrap.loading::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      animation: skeleton-pulse 1.6s ease-in-out infinite;
      z-index: 1;
    }
    .tile img, .tile video {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      opacity: 0;
      transition: opacity 0.4s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .tile img.loaded, .tile video.loaded {
      opacity: 1;
    }
    .badge-row {
      position: absolute;
      top: 10px;
      left: 10px;
      right: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      pointer-events: none;
      z-index: 2;
    }
    .badge {
      min-height: 22px;
      padding: 2px 8px;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 999px;
      background: rgba(8, 9, 13, 0.8);
      color: var(--text);
      font-size: 11px;
      font-weight: 600;
      backdrop-filter: blur(4px);
    }
    .badge.video { color: #e9faff; border-color: rgba(74, 177, 192, 0.4); }
    .badge.source { color: #f3efd0; border-color: rgba(213, 179, 91, 0.4); }
    .badge.account { color: #e7f5ea; border-color: rgba(96, 176, 120, 0.4); }
    .meta { padding: 14px; }
    .prompt {
      color: var(--text);
      max-height: 64px;
      font-size: 13.5px;
      font-weight: 500;
      overflow: hidden;
      overflow-wrap: anywhere;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
    }
    .sub {
      color: var(--muted);
      font-size: 11px;
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      gap: 8px;
      overflow-wrap: anywhere;
    }
    .kv { color: var(--muted); }
    .empty {
      display: none;
      padding: 60px 0;
      color: var(--muted);
      text-align: center;
      font-size: 16px;
    }
    .sentinel {
      height: 20px;
      margin-top: 20px;
      width: 100%;
    }
    .loading-spinner {
      display: none;
      text-align: center;
      padding: 30px 0;
      color: var(--muted);
      font-size: 14px;
      font-weight: 500;
    }
    .loading-spinner.active {
      display: block;
    }
    .progress-float {
      position: fixed;
      right: 24px;
      bottom: 24px;
      z-index: 8;
      min-width: 60px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: rgba(16, 18, 26, 0.85);
      color: var(--text);
      text-align: center;
      font-weight: 600;
      font-size: 12px;
      backdrop-filter: blur(12px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    }
    dialog {
      width: min(1200px, 95vw);
      height: min(880px, 92vh);
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #0d0f16;
      color: var(--text);
      padding: 0;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
      overflow: hidden;
      animation: dialog-fade-in 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    @keyframes dialog-fade-in {
      from { transform: scale(0.97); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }
    dialog::backdrop {
      background: rgba(4, 5, 8, 0.85);
      backdrop-filter: blur(8px);
    }
    .detail { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(350px, .85fr); height: 100%; }
    .viewer { min-height: 0; display: flex; align-items: center; justify-content: center; background: #050608; }
    .viewer img, .viewer video { max-width: 100%; max-height: 100%; object-fit: contain; }
    .viewer video { width: 100%; }
    .side { overflow: auto; padding: 24px; border-left: 1px solid var(--line); }
    .side-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
    .side h2 { font-size: 15px; font-weight: 650; margin: 20px 0 10px; color: var(--accent); }
    .side h2:first-child { margin-top: 0; }
    .post-id { font-size: 15px; font-weight: 700; overflow-wrap: anywhere; }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      padding: 14px;
      background: #08090d;
      border: 1px solid var(--line);
      border-radius: 8px;
      max-height: 260px;
      overflow: auto;
      font-family: 'Courier New', Courier, monospace;
      font-size: 12px;
    }
    .asset, .rel-item {
      padding: 12px 0;
      border-top: 1px solid var(--line);
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .rel-item button {
      height: auto;
      min-height: 32px;
      margin-bottom: 6px;
      text-align: left;
      overflow-wrap: anywhere;
    }
    .line-list { display: grid; gap: 6px; margin: 8px 0 14px; }
    .line-item { color: var(--muted); overflow-wrap: anywhere; }
    .pill-line { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
    @media (max-width: 860px) {
      .bar { grid-template-columns: 1fr 1fr; }
      h1, #q { grid-column: 1 / -1; }
      .detail { grid-template-columns: 1fr; }
      .viewer { min-height: 42vh; }
      .side { border-left: 0; border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Grok Archive</h1>
      <input id="q" placeholder="Search prompt or ID" autocomplete="off">
      <select id="account"><option value="">All accounts</option></select>
      <select id="folder"><option value="">All folders</option></select>
      <select id="media"><option value="">All media</option><option value="MEDIA_POST_TYPE_IMAGE">Images</option><option value="MEDIA_POST_TYPE_VIDEO">Videos</option></select>
      <select id="sort"><option value="desc">Newest</option><option value="asc">Oldest</option></select>
    </div>
  </header>
  <main>
    <div class="topline"><div id="count"></div></div>
    <div id="grid" class="grid"></div>
    <div id="empty" class="empty">No posts match the current filters.</div>
    <div id="loading-spinner" class="loading-spinner">Loading more items...</div>
    <div id="sentinel" class="sentinel"></div>
  </main>
  <div id="progress" class="progress-float">0%</div>
  <dialog id="dialog"><div id="detail" class="detail"></div></dialog>
  <script>
    const state = { offset: 0, limit: 80, total: 0, loading: false, exhausted: false, suppressClose: false };
    const grid = document.querySelector('#grid');
    const dialog = document.querySelector('#dialog');
    const detail = document.querySelector('#detail');
    const controls = ['q', 'account', 'folder', 'media', 'sort'].map(id => document.querySelector('#' + id));
    const els = {
      account: document.querySelector('#account'),
      folder: document.querySelector('#folder'),
      q: document.querySelector('#q'),
      media: document.querySelector('#media'),
      sort: document.querySelector('#sort'),
      count: document.querySelector('#count'),
      empty: document.querySelector('#empty'),
      progress: document.querySelector('#progress')
    };

    function apiParams(extra = {}) {
      const params = new URLSearchParams({
        account: els.account.value,
        folder: els.folder.value,
        media: els.media.value,
        q: els.q.value,
        sort: els.sort.value,
        limit: state.limit,
        offset: state.offset,
        ...extra
      });
      for (const [key, value] of [...params.entries()]) if (!value) params.delete(key);
      return params;
    }

    function mediaUrl(account, path) {
      if (!path) return '';
      return '/media/' + encodeURI(account + '/' + path);
    }

    function pageUrl(post = null) {
      const params = new URLSearchParams();
      for (const id of ['account', 'folder', 'media', 'sort', 'q']) {
        const value = document.querySelector('#' + id).value;
        if (value) params.set(id, value);
      }
      if (post) {
        params.set('post', post.id);
        params.set('postAccount', post.account);
      }
      const query = params.toString();
      return location.pathname + (query ? '?' + query : '');
    }

    function syncControlsFromUrl() {
      const params = new URLSearchParams(location.search);
      for (const id of ['account', 'folder', 'media', 'sort', 'q']) {
        const el = document.querySelector('#' + id);
        el.value = params.has(id) ? params.get(id) : (id === 'sort' ? 'desc' : '');
      }
      state.pendingFolder = params.get('folder') || '';
    }

    async function loadAccounts() {
      const accounts = await fetch('/api/accounts').then(r => r.json());
      for (const account of accounts) {
        const option = document.createElement('option');
        option.value = account.alias;
        const count = account.status && account.status.posts != null ? ` (${account.status.posts})` : '';
        option.textContent = account.alias + count;
        els.account.appendChild(option);
      }
    }

    async function loadFolders() {
      const selected = state.pendingFolder || els.folder.value;
      state.pendingFolder = '';
      const folders = await fetch('/api/folders?' + apiParams({ limit: '', offset: '' })).then(r => r.json());
      els.folder.replaceChildren(new Option('All folders', ''));
      for (const folder of folders) {
        const option = document.createElement('option');
        option.value = folder.account + ':' + folder.id;
        option.textContent = `[${folder.account}] ${folder.name || folder.id}`;
        els.folder.appendChild(option);
      }
      els.folder.value = [...els.folder.options].some(option => option.value === selected) ? selected : '';
    }

    function resetList() {
      state.offset = 0;
      state.total = 0;
      state.exhausted = false;
      grid.replaceChildren();
      els.empty.style.display = 'none';
      updateCount();
    }

    async function load(reset = false) {
      if (reset) resetList();
      if (state.loading || state.exhausted) return;
      state.loading = true;
      const spinner = document.querySelector('#loading-spinner');
      if (spinner) spinner.classList.add('active');
      try {
        const data = await fetch('/api/posts?' + apiParams()).then(r => r.json());
        state.total = data.total;
        state.offset += data.items.length;
        state.exhausted = state.offset >= state.total || data.items.length === 0;
        for (const item of data.items) grid.appendChild(tile(item));
        els.empty.style.display = state.total === 0 ? 'block' : 'none';
      } finally {
        state.loading = false;
        if (spinner) spinner.classList.remove('active');
        updateCount();
      }
    }

    function updateCount() {
      els.count.textContent = `${Math.min(state.offset, state.total)} / ${state.total}`;
      updateProgress();
    }

    function updateProgress() {
      const scrollMax = Math.max(1, document.documentElement.scrollHeight - innerHeight);
      const scrollFraction = Math.min(1, Math.max(0, scrollY / scrollMax));
      const loadedFraction = state.total ? Math.min(1, state.offset / state.total) : 0;
      els.progress.textContent = Math.round(scrollFraction * loadedFraction * 100) + '%';
    }

    function previewMedia(item) {
      const wrap = document.createElement('div');
      wrap.className = 'media-wrap';
      if (item.width && item.height && item.width > 0 && item.height > 0) {
        wrap.style.aspectRatio = `${item.width} / ${item.height}`;
      } else {
        wrap.style.aspectRatio = '1 / 1';
      }

      if (!item.previewPath) {
        const missing = document.createElement('div');
        missing.className = 'empty';
        missing.style.display = 'block';
        missing.textContent = 'Missing media';
        wrap.appendChild(missing);
      } else if (item.previewKind === 'video') {
        const video = document.createElement('video');
        wrap.classList.add('loading');
        video.addEventListener('loadeddata', () => {
          wrap.classList.remove('loading');
          video.classList.add('loaded');
        });
        video.addEventListener('error', () => {
          wrap.classList.remove('loading');
        });
        video.src = mediaUrl(item.account, item.previewPath);
        video.muted = true;
        video.loop = true;
        video.playsInline = true;
        video.preload = 'metadata';
        wrap.appendChild(video);
        wrap.addEventListener('mouseenter', () => video.play().catch(() => {}));
        wrap.addEventListener('mouseleave', () => { video.pause(); video.currentTime = 0; });
      } else {
        const img = document.createElement('img');
        wrap.classList.add('loading');
        img.addEventListener('load', () => {
          wrap.classList.remove('loading');
          img.classList.add('loaded');
        });
        img.addEventListener('error', () => {
          wrap.classList.remove('loading');
        });
        img.src = mediaUrl(item.account, item.previewPath);
        img.loading = 'lazy';
        img.decoding = 'async';
        wrap.appendChild(img);
      }
      const badges = document.createElement('div');
      badges.className = 'badge-row';
      badges.appendChild(badge(item.account, 'account'));
      if (item.hasVideo || item.previewKind === 'video') badges.appendChild(badge('VIDEO', 'video'));
      if (item.hasUploadedSource) badges.appendChild(badge('UPLOAD', 'source'));
      if (item.childCount || item.parentCount) badges.appendChild(badge(`TREE ${item.parentCount}/${item.childCount}`, ''));
      wrap.appendChild(badges);
      return wrap;
    }

    function badge(text, kind) {
      const span = document.createElement('span');
      span.className = 'badge ' + kind;
      span.textContent = text;
      return span;
    }

    function tile(item) {
      const div = document.createElement('article');
      div.className = 'tile';
      div.appendChild(previewMedia(item));
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.innerHTML = '<div class="prompt"></div><div class="sub"><span></span><span></span></div>';
      meta.querySelector('.prompt').textContent = item.prompt || item.originalPrompt || item.id;
      meta.querySelectorAll('span')[0].textContent = item.id;
      meta.querySelectorAll('span')[1].textContent = item.createTime || '';
      div.appendChild(meta);
      div.addEventListener('click', () => openDetail(item.id, item.account, true));
      return div;
    }

    async function openDetail(id, account, push = false) {
      const params = new URLSearchParams({ account });
      const data = await fetch('/api/posts/' + encodeURIComponent(id) + '?' + params).then(r => r.json());
      renderDetail(data);
      if (!dialog.open) dialog.showModal();
      if (push) history.pushState({ post: id, account }, '', pageUrl(data));
    }

    function detailMedia(data) {
      const primary = data.assets.find(a => a.localPath && a.role === 'media') ||
        data.assets.find(a => a.localPath && a.role !== 'thumbnail') ||
        data.assets.find(a => a.localPath);
      const viewer = document.createElement('div');
      viewer.className = 'viewer';
      if (!primary) {
        const missing = document.createElement('div');
        missing.className = 'kv';
        missing.textContent = 'Missing media';
        viewer.appendChild(missing);
      } else if (primary.kind === 'video') {
        const video = document.createElement('video');
        video.src = mediaUrl(data.account, primary.localPath);
        video.controls = true;
        video.playsInline = true;
        video.preload = 'metadata';
        viewer.appendChild(video);
      } else {
        const img = document.createElement('img');
        img.src = mediaUrl(data.account, primary.localPath);
        img.decoding = 'async';
        viewer.appendChild(img);
      }
      return viewer;
    }

    function renderDetail(data) {
      const side = document.createElement('div');
      side.className = 'side';
      side.innerHTML = `
        <div class="side-head"><div><div class="post-id"></div><div class="kv summary"></div></div><button class="close">Close</button></div>
        <div class="pill-line"></div>
        <h2>Prompt</h2><pre class="prompt-full"></pre>
        <h2>Folders</h2><div class="folders line-list"></div>
        <h2>Original parent</h2><div class="original-parents"></div>
        <h2>Nested parent</h2><div class="nested-parents"></div>
        <h2>Child posts</h2><div class="children"></div>
        <h2>Assets</h2><div class="assets"></div>
        <h2>Raw JSON</h2><pre class="raw"></pre>`;
      side.querySelector('.post-id').textContent = data.id;
      side.querySelector('.summary').textContent = `${data.account} · ${data.mediaType || ''} · ${data.width || ''}x${data.height || ''} · ${data.modelName || ''}`;
      side.querySelector('.prompt-full').textContent = data.prompt || data.originalPrompt || '';
      side.querySelector('.raw').textContent = JSON.stringify(data.raw, null, 2);
      const pills = side.querySelector('.pill-line');
      pills.appendChild(badge(data.account, 'account'));
      if (data.hasVideo) pills.appendChild(badge('VIDEO', 'video'));
      if (data.hasUploadedSource) pills.appendChild(badge('UPLOAD', 'source'));
      renderFolders(side.querySelector('.folders'), data.folders);
      renderRelations(side.querySelector('.original-parents'), data.relationships.originalParents, 'Source post');
      renderRelations(side.querySelector('.nested-parents'), data.relationships.nestedParents, 'Containing post');
      renderRelations(
        side.querySelector('.children'),
        [...data.relationships.nestedChildren, ...data.relationships.originalChildren],
        'Child post'
      );
      renderAssets(side.querySelector('.assets'), data.assets);
      side.querySelector('.close').addEventListener('click', () => closeDetail(true));
      detail.replaceChildren(detailMedia(data), side);
    }

    function renderFolders(node, folders) {
      if (!folders.length) {
        node.textContent = 'None';
        return;
      }
      for (const folder of folders) {
        const row = document.createElement('div');
        row.className = 'line-item';
        row.textContent = `${folder.folder_name || folder.folder_id} (${folder.folder_id})`;
        node.appendChild(row);
      }
    }

    function renderRelations(node, relations, prefix) {
      if (!relations.length) {
        node.textContent = 'None';
        node.className = 'line-item';
        return;
      }
      for (const rel of relations) {
        const row = document.createElement('div');
        row.className = 'rel-item';
        const btn = document.createElement('button');
        btn.textContent = `${prefix}: ${rel.id}`;
        btn.addEventListener('click', () => openDetail(rel.id, rel.account, true));
        const prompt = document.createElement('div');
        prompt.textContent = rel.prompt || rel.mediaType || '';
        row.append(btn, prompt);
        node.appendChild(row);
      }
    }

    function renderAssets(node, assets) {
      for (const asset of assets) {
        const row = document.createElement('div');
        row.className = 'asset';
        const title = document.createElement('div');
        title.textContent = `${asset.kind} / ${asset.role} / ${asset.status} / ${asset.size || 0} bytes`;
        const path = document.createElement('div');
        path.className = 'line-item';
        path.textContent = asset.localPath || asset.url;
        row.append(title, path);
        node.appendChild(row);
      }
    }

    function closeDetail(push = false) {
      state.suppressClose = true;
      if (dialog.open) dialog.close();
      state.suppressClose = false;
      if (push) history.pushState({}, '', pageUrl());
    }

    async function refreshFromControls(push = true) {
      if (push) history.pushState({}, '', pageUrl());
      await loadFolders();
      await load(true);
    }

    dialog.addEventListener('click', event => { if (event.target === dialog) closeDetail(true); });
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDetail(true); });
    dialog.addEventListener('close', () => {
      if (!state.suppressClose && new URLSearchParams(location.search).has('post')) closeDetail(true);
    });
    els.account.addEventListener('change', () => {
      els.folder.value = '';
      refreshFromControls(true);
    });
    for (const control of [els.folder, els.media, els.sort]) {
      control.addEventListener('change', () => refreshFromControls(true));
    }
    els.q.addEventListener('input', () => {
      clearTimeout(window._searchTimer);
      window._searchTimer = setTimeout(() => refreshFromControls(true), 250);
    });
    addEventListener('scroll', updateProgress, { passive: true });
    addEventListener('resize', updateProgress);
    addEventListener('popstate', async () => {
      syncControlsFromUrl();
      await loadFolders();
      await load(true);
      const params = new URLSearchParams(location.search);
      if (params.has('post')) {
        await openDetail(params.get('post'), params.get('postAccount') || els.account.value, false);
      } else if (dialog.open) {
        state.suppressClose = true;
        dialog.close();
        state.suppressClose = false;
      }
    });

    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) load(false);
    }, { rootMargin: '800px 0px' });
    observer.observe(document.querySelector('#sentinel'));

    (async function init() {
      await loadAccounts();
      syncControlsFromUrl();
      await loadFolders();
      await load(true);
      const params = new URLSearchParams(location.search);
      if (params.has('post')) await openDetail(params.get('post'), params.get('postAccount') || els.account.value, false);
    })();
  </script>
</body>
</html>
"""
