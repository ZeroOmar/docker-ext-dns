# Changelog

## 0.1.3

### Fixed
- **Pi-hole auth short-circuit** — `_ensure_auth` now returns immediately if a session is already established, preventing redundant `POST /api/auth` calls on every `list_records`, `create_record`, and `delete_record` invocation
- **Resilient auth probe** — `GET /api/auth` errors (non-200 status, network failure, unexpected JSON shape) no longer abort before the login attempt; the provider falls through to `POST /api/auth` directly
- **Session expiry re-auth** — the 401 retry path in `_request` now resets `_no_auth` so a session that expires after initial "no auth" detection correctly re-authenticates
- **Clear auth error message** — a 401 from `POST /api/auth` now raises `"Pi-hole authentication failed — check the configured password"` instead of a generic HTTP status error

### Changed
- **`run_dev.sh` heredoc** — config is now assigned via `read -d '' … <<'YAML'` so YAML-quoted values (passwords, URLs) are passed literally without shell interference

## 0.1.2

### Added
- **`insecure` option for Pi-hole** — set `insecure: true` in the plugin config to skip TLS certificate verification when Pi-hole is behind a self-signed certificate
- **Server-side multi-instance aggregation** — configure remote docker-ext-dns instances in `EXT_DNS_CONFIG` under `instances:`; the local backend proxies their record fetches so self-signed HTTPS instances work without browser trust issues
- **`/api/instances` endpoint** — returns the list of server-configured remote instances (name, url, insecure, proxied)
- **`/api/instances/{name}/records` endpoint** — proxies a `/api/records` fetch to a named server-configured instance using the appropriate TLS settings

### Changed
- **Stateless web UI** — instance list is now read-only and driven entirely by `EXT_DNS_CONFIG`; removed add/remove instance controls and all `localStorage` usage
- **Local instance always predefined** — the local instance tab is always present and always fetches from the same host; no configuration required
- **Logs to stdout** — all log output is written to stdout; no file logging

## 0.1.0

### Added
- **Core reconciler** — async loop that diffs desired DNS state (from Docker labels) against actual provider state, then creates, updates, or deletes records; woken early by Docker events via `asyncio.Event`
- **Docker label format** — `ext-dns.<plugin>.hostname`, `ext-dns.<plugin>.type` (`A`/`CNAME`), `ext-dns.<plugin>.target` (CNAME), `ext-dns.<plugin>.network` (optional network selector for A records)
- **Pi-hole v6 provider** — manages custom A records (`dns/hosts`) and CNAME records (`dns/cnameRecords`) via the Pi-hole REST API; handles session auth with `GET /api/auth` + `POST /api/auth`, re-auth on 401, and logout on shutdown
- **DNS verification** — each reconcile cycle resolves every managed hostname with `dnspython` and reports `NOERROR`, `NXDOMAIN`, `MISMATCH`, or `SERVFAIL` status
- **FastAPI REST API** — `GET /api/health`, `GET /api/records`, `GET /api/instances`, `POST /api/reconcile`; query parameters validated by Pydantic with strict enum and pattern checks; no DNS provider proxy endpoints
- **Web UI** — single-file dark-themed SPA; table of all managed records with Instance, Container, Plugin, Hostname, Type, Value, Last Updated, and DNS Status columns; auto-refreshes every 30 seconds
- **Multi-instance support** — UI reads a list of docker-ext-dns instance URLs from `localStorage` and aggregates records from all instances into one table with per-instance tabs
- **Configuration** — single `EXT_DNS_CONFIG` environment variable containing YAML; supports `interval`, `plugins.<name>.*`, and `web.port`
- **Modular provider system** — abstract `DNSProvider` base class; new providers added by subclassing and registering in `_REGISTRY`
- **Dockerfile** — two-stage slim build with non-root `extdns` user
