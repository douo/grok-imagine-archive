# Grok Downloader Agent Notes

- Final delivery is not only the downloader implementation. Run a real full sync for account `andy` and verify the downloaded local archive.
- Default account config is local-only at `config/accounts.toml`. Never print token/cookie values.
- If request/header behavior is unclear, use `/home/tiou/playground/grok-playground/grok2api` as a reference.
- Current API examples come from `samples/grok-imagine-saved-current.har`. Older HAR files are not contracts.
- Do not commit or expose `config/accounts.toml`, `samples/`, or `archive/`.
- The tool is read-only against Grok. It must not delete or mutate cloud assets.
