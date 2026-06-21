# Changelog

## 0.4.0

### Fixed
- **Empty-state message no longer lingers under a populated table** — the web UI toggled a `hidden` class on the "No records found" block, but no `.hidden` CSS rule existed, so the class had no effect and the message stayed visible beneath the records. Added the missing `.hidden { display: none !important; }` rule

### Added
- **Sortable records table** — the web UI table now defaults to ordering by Instance, then Container. Clicking any column header sorts by that column; clicking the same header again toggles ascending/descending, with an arrow indicator on the active column. Dates sort chronologically and other columns use natural (numeric-aware) string comparison

## 0.3.0

### Fixed
- **Serialized Pi-hole config writes** — Pi-hole applies every config change through a single shared temporary file (`/etc/pihole/dnsmasq.conf.temp`), so two concurrent `PUT`/`DELETE /api/config/...` requests race and corrupt each other, producing `400 Invalid configuration` (`cannot read dnsmasq.conf.temp: No such file or directory`) and cascading `ReadTimeout` errors whenever a reconcile applied many changes at once. The Pi-hole provider now serializes all mutating requests behind a write lock (reads stay concurrent), eliminating the race

### Added
- **Change throttling** — new top-level config keys `change_concurrency` (default `2`, max simultaneous record changes) and `change_delay` (default `0`, seconds to pause after each change) bound how fast a large diff is applied so the DNS backend is not overloaded. The single DNS restart still runs once, after all changes complete

## 0.2.0

### Added
- **Traefik integration** — when enabled per plugin (`plugins.<name>.traefik.enabled: true`), docker-ext-dns reads Traefik router labels (`traefik.http.routers.<router>.rule`) and creates a CNAME record for every hostname in each `Host(`...`)` expression, pointing at the Traefik host. The target is taken from `traefik.hostname` or auto-discovered from the first `Host()` rule on a container whose name contains `traefik`. `ext-dns.*` labels take precedence for the same hostname, and a container can opt out with `ext-dns.<plugin>.traefik: "false"`
- **Record source indicator** — each record now carries its origin (`ext-dns` or `traefik`), exposed on the API and shown as a badge column in the web UI

### Changed
- **Source-of-truth record creation** — creating a record now first deletes any existing record of the same name in *both* Pi-hole config elements (`dns/hosts` and `dns/cnameRecords`) before writing. This guarantees the app owns its managed names and fixes a bug where changing a record's type (A↔CNAME) left the old record behind, leaving Pi-hole with a conflicting A *and* CNAME for one name

## 0.1.9

### Fixed
- **Event loop no longer blocked by the Docker watcher** — `DockerWatcher.watch()` ran docker-py's blocking `events()` generator directly on the asyncio event loop, freezing every other task (reconciler, web server) between Docker events; the blocking iteration now runs in a worker thread via `asyncio.to_thread` and hands events back with `run_coroutine_threadsafe`. This was the real cause of Pi-hole `ConnectTimeout` failures that surfaced at 60–90 s despite the 10 s client timeout — the async connect and its timeout timer could not progress while the loop was parked inside `next(events)`
- **`get_desired_state()` offloaded off the loop** — the reconciler called the blocking docker-py `containers.list()` synchronously on the event loop each reconcile cycle; it is now wrapped in `asyncio.to_thread`

## 0.1.8

### Changed
- **Batched Pi-hole DNS restart** — record mutations now use `?restart=false`; after all creates/updates/deletes for a reconcile cycle complete, a single `POST /api/action/restartdns` is issued per provider instead of restarting FTL on every individual record change
- **`restart_dns()` on provider base class** — no-op default so other future providers don't need to implement it

## 0.1.7

### Fixed
- **HTTP proxy bypass for Pi-hole** — all httpx clients now set `trust_env=False` so they ignore `HTTP_PROXY` / `HTTPS_PROXY` environment variables; Docker Compose and some container runtimes inject proxy env vars automatically, causing requests to a private IP like `10.x.x.x` to be routed through the proxy which then times out (~90 s) before httpx's own 10 s timeout could fire

## 0.1.6

### Changed
- **`GET /api/auth` non-200 now logged at WARNING** — previously silenced at DEBUG; Pi-hole returning 401 for an unauthenticated probe is now visible in the logs with the status code
- **`GET /api/auth` connection errors now logged at WARNING** — `ConnectTimeout`, `ConnectError`, and similar failures during the auth probe were previously invisible at INFO level
- **`POST /api/auth` connection errors include URL** — instead of a bare `httpx.ConnectTimeout`, the error now reads `Pi-hole connection to http://… failed: ConnectTimeout:`
- **`POST /api/auth` 401 body logged** — the Pi-hole response body is logged at WARNING before raising so the exact Pi-hole error message is visible
- **Auth start and POST logged at INFO** — `Pi-hole authenticating to …` and `Pi-hole POST /api/auth at …` are now INFO so they show without enabling DEBUG

## 0.1.5

### Added
- **Pi-hole response logging** — every HTTP call to Pi-hole is logged at DEBUG level with method, path, and status code; non-2xx responses also log up to 800 bytes of the response body at WARNING level so the exact error from Pi-hole is visible
- **Full traceback on provider errors** — reconciler error logs now include `exc_info=True`; previously empty-looking messages like `Failed to list records from 'pihole': ` now print the full exception type and stack trace

### Changed
- **Resilient background tasks** — reconciler and Docker watcher are each wrapped in a restart loop; a crash in either no longer takes down the web UI — the failing component logs the error and restarts after 5 seconds while the FastAPI server keeps serving

## 0.1.4

### Fixed
- **Pi-hole concurrent auth race** — `_ensure_auth` now holds an `asyncio.Lock` with a double-checked locking pattern; concurrent reconcile tasks (creates, deletes) no longer fire simultaneous `POST /api/auth` requests that cause Pi-hole to reject the second login with 401

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
