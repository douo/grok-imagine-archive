from __future__ import annotations

from grok_downloader.web import create_app


app = create_app("demo", aliases=["demo", "studio"])
