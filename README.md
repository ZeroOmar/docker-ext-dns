# docker-ext-dns

DNS record manager for Docker Compose environments, inspired by Kubernetes [external-dns](https://github.com/kubernetes-sigs/external-dns).

Watches running containers, reads `ext-dns.*` labels, and automatically creates, updates, and deletes DNS records in configured DNS providers. Includes a web UI to inspect record state across one or more instances.

## Features

- Reacts to Docker container start/stop events in real time
- Supports A records (container IP) and CNAME records
- **Traefik integration** — reads Traefik router labels and creates CNAMEs to your Traefik host automatically
- Modular provider system — add new DNS backends by implementing one interface
- DNS verification: checks whether each record actually resolves after creation
- Source of truth: a managed name is fully replaced on create (any conflicting A/CNAME of the same name is removed first)
- Web UI with multi-instance aggregation, a per-record source badge (ext-dns / traefik), a sortable records table, and version indicators that flag (for the local instance and every connected one) when a newer release is available on GitHub

## Quick Start

```yaml
# docker-compose.yml
services:
  ext-dns:
    image: ghcr.io/zeroomar/docker-ext-dns:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      EXT_DNS_CONFIG: |
        interval: 30
        plugins:
          pihole:
            url: http://pihole:80
            password: your-pihole-password
        web:
          port: 8080
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    group_add:
      - "999"   # docker group GID — adjust to match your host

  my-service:
    image: nginx:alpine
    labels:
      ext-dns.pihole.hostname: my-service.lan
      ext-dns.pihole.type: A
```

## Labels

| Label | Required | Description |
|---|---|---|
| `ext-dns.<plugin>.hostname` | yes | DNS name to manage |
| `ext-dns.<plugin>.type` | yes | `A` or `CNAME` |
| `ext-dns.<plugin>.target` | CNAME only | CNAME target value |
| `ext-dns.<plugin>.network` | no | Docker network to read IP from (A records) |
| `ext-dns.<plugin>.traefik` | no | Set to `false` to opt a container out of Traefik integration for this plugin |

Multiple plugins per container are supported.

## Traefik integration

When enabled per plugin (see Configuration), docker-ext-dns also reads Traefik
router labels and creates a **CNAME** record for each routed hostname, pointing
at your Traefik host. No `ext-dns.*` labels are needed for these — just your
existing Traefik labels:

```yaml
my-app:
  image: nginx:alpine
  labels:
    traefik.http.routers.my-app.rule: Host(`app.lan`)
    # → CNAME app.lan -> <traefik hostname>
```

- Every hostname inside `Host(`...`)` is extracted, including `Host(`a`, `b`)`
  and `Host(`a`) || Host(`b`)` forms.
- The CNAME target is the per-plugin `traefik.hostname` from config, or — if
  omitted — auto-discovered from the first `Host(`...`)` rule on a container
  whose name contains `traefik`.
- `ext-dns.*` labels take precedence: if a container defines an `ext-dns` record
  for the same hostname, the Traefik CNAME for that hostname is skipped.
- Opt a container out with `ext-dns.<plugin>.traefik: "false"`.
- The `traefik.docker.network` label is parsed but currently unused (CNAMEs need
  no container IP); it is reserved for a future A-record mode.

## Configuration (`EXT_DNS_CONFIG`)

```yaml
interval: 30            # seconds between reconcile loops (minimum 5)
change_concurrency: 2   # max record changes applied at once (throttles large diffs)
change_delay: 0         # optional seconds to pause after each change
plugins:
  pihole:
    url: http://pihole:80
    password: secret  # omit if Pi-hole has no password
    traefik:          # optional — enable Traefik label integration for this plugin
      enabled: true
      hostname: traefik.lan   # optional; auto-discovered from a *traefik* container if omitted
web:
  port: 8080
```

When a reconcile produces many changes, they are applied with bounded
concurrency (`change_concurrency`, default `2`) so the DNS backend is not
overloaded. Set `change_delay` to add a fixed pause after each change for even
gentler pacing. The single DNS restart still happens once, after all changes.

In addition, the Pi-hole provider **serializes all config writes** internally:
Pi-hole applies each change through a single shared temporary file, so concurrent
writes corrupt one another (`cannot read dnsmasq.conf.temp` → `400 Invalid
configuration`). Reads remain concurrent.

## API

| Endpoint | Description |
|---|---|
| `GET /api/health` | Instance health and summary |
| `GET /api/records` | All managed records (`?plugin=` and `?dns_status=` filters) |
| `GET /api/instances` | Instance metadata |
| `GET /api/instances/{name}/records` | Records proxied from a configured remote instance |
| `GET /api/instances/{name}/health` | Health (including version) proxied from a configured remote instance |
| `POST /api/reconcile` | Trigger an immediate reconcile cycle |

## Multi-Instance

To aggregate records from multiple docker-ext-dns deployments into one UI, configure remote instances in `EXT_DNS_CONFIG`. The local instance fetches their records server-side, so self-signed HTTPS certificates are handled correctly without any browser trust issues.

```yaml
interval: 30
plugins:
  pihole:
    url: https://pihole.home:443
    password: secret
    insecure: true          # skip TLS verification for self-signed cert

instances:
  - name: server-room
    url: http://192.168.1.50:8080   # plain HTTP — direct fetch
  - name: remote-site
    url: https://dns.remote.lan:8080
    insecure: true          # self-signed cert — proxied through local backend

web:
  port: 8080
```

The web UI auto-discovers these instances from `/api/instances` on every load. Records from all instances appear under a single table with per-instance tabs. Each tab's indicator shows the instance's state: green (reachable, up to date), amber (reachable but running an older version than the latest GitHub release), or red (unreachable). You can also add instances ad-hoc via the UI (stored in browser `localStorage`) for plain-HTTP instances reachable from your browser.

## Providers

### Pi-hole (v6)

Config keys: `url`, `password` (optional if no auth set), `insecure` (default `false`).

Set `insecure: true` to skip TLS certificate verification when Pi-hole is behind a self-signed certificate.

Manages records via `PUT`/`DELETE /api/config/dns%2Fhosts/{value}` and `dns%2FcnameRecords/{value}`.

### Adding a new provider

1. Subclass `ext_dns.providers.base.DNSProvider`
2. Implement `name`, `list_records`, `create_record`, `update_record`, `delete_record`
3. Add one entry to `_REGISTRY` in `ext_dns/providers/__init__.py`

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
EXT_DNS_CONFIG="interval: 30" docker-ext-dns
```
