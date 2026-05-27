# Grok Imagine Archive Agent Notes

- Do not pull real account data during release review unless the user explicitly re-authorizes it. Use the mock archive and local verification gates for public-release review.
- Default account config is local-only at `config/accounts.toml`. Never print token/cookie values.
- If request/header behavior is unclear, use a local `grok2api` checkout as a reference when available.
- Current API examples come from `samples/grok-imagine-saved-current.har`. Older HAR files are not contracts.
- Do not commit or expose `config/accounts.toml`, `samples/`, or `archive/`.
- The tool is read-only against Grok. It must not delete or mutate cloud assets.
