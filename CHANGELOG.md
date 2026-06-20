# Changelog

## v0.1.0

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
