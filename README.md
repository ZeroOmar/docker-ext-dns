# docker-ext-dns

DNS record manager for Docker Compose environments, inspired by Kubernetes [external-dns](https://github.com/kubernetes-sigs/external-dns).

Watches running containers, reads `ext-dns.*` labels, and automatically creates, updates, and deletes DNS records in configured DNS providers. Includes a web UI to inspect record state across one or more instances.

## Features

- Reacts to Docker container start/stop events in real time
- Supports A records (container IP) and CNAME records
- Modular provider system — add new DNS backends by implementing one interface
- DNS verification: checks whether each record actually resolves after creation
- Web UI with multi-instance aggregation

## Quick Start

```yaml
# docker-compose.yml
services:
  ext-dns:
    image: docker-ext-dns:latest
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

Multiple plugins per container are supported.

## Configuration (`EXT_DNS_CONFIG`)

```yaml
interval: 30          # seconds between reconcile loops (minimum 5)
plugins:
  pihole:
    url: http://pihole:80
    password: secret  # omit if Pi-hole has no password
web:
  port: 8080
```

## API

| Endpoint | Description |
|---|---|
| `GET /api/health` | Instance health and summary |
| `GET /api/records` | All managed records (`?plugin=` and `?dns_status=` filters) |
| `GET /api/instances` | Instance metadata |
| `POST /api/reconcile` | Trigger an immediate reconcile cycle |

## Providers

### Pi-hole (v6)

Config keys: `url`, `password` (optional if no auth set).

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
