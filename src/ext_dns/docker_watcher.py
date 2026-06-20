import asyncio
import logging
import re
from typing import Awaitable, Callable

import docker
import docker.errors

from ext_dns.models import DNSRecord, RecordSource, RecordType

log = logging.getLogger(__name__)

LABEL_PREFIX = "ext-dns"
TRAEFIK_LABEL_PREFIX = "traefik"

# Traefik router rules look like: Host(`a.lan`) || (Host(`b.lan`, `c.lan`) && PathPrefix(`/x`))
_HOST_RULE_RE = re.compile(r"Host\(([^)]*)\)")  # capture the args inside Host( ... )
_HOST_ARG_RE = re.compile(r"`([^`]+)`")  # each backtick-quoted hostname


def extract_traefik_hosts(container_labels: dict[str, str]) -> list[str]:
    """
    Extract every hostname from Host(`...`) expressions across all
    `traefik.http.routers.<router>.rule` labels. De-duplicated, order-preserved.

    Handles Host(`a`), Host(`a`, `b`), Host(`a`) || Host(`b`), and rules that
    combine Host() with other matchers (PathPrefix, Headers, …) — only Host()
    arguments are returned.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for key, value in container_labels.items():
        if not key.startswith(TRAEFIK_LABEL_PREFIX + ".http.routers."):
            continue
        if not key.endswith(".rule"):
            continue
        for group in _HOST_RULE_RE.findall(value or ""):
            for host in _HOST_ARG_RE.findall(group):
                h = host.strip()
                if h and h not in seen:
                    seen.add(h)
                    hosts.append(h)
    return hosts


def extract_traefik_network(container_labels: dict[str, str]) -> str | None:
    """
    Read the `traefik.docker.network` label. Parsed for documentation and future
    A-record support; not used for CNAME records (which need no container IP).
    """
    return container_labels.get(TRAEFIK_LABEL_PREFIX + ".docker.network")


def _traefik_opted_out(container_labels: dict[str, str], plugin_name: str) -> bool:
    """A container opts out of Traefik integration for a plugin via
    `ext-dns.<plugin>.traefik=false` (also 0/no/off)."""
    val = container_labels.get(f"{LABEL_PREFIX}.{plugin_name}.traefik")
    if val is None:
        return False
    return val.strip().lower() in ("false", "0", "no", "off")


def _get_container_ip(
    networks: dict, preferred_network: str | None
) -> str | None:
    if not networks:
        return None

    if preferred_network:
        net = networks.get(preferred_network)
        if net is None:
            return None
        return net.get("IPAddress") or None

    for net_name in sorted(networks.keys()):
        ip = networks[net_name].get("IPAddress")
        if ip:
            log.debug("Selected network '%s' for IP resolution (first alphabetically)", net_name)
            return ip

    return None


def extract_dns_labels(
    container_labels: dict[str, str],
    networks: dict,
) -> dict[str, list[DNSRecord]]:
    plugins: dict[str, dict[str, str]] = {}
    for key, value in container_labels.items():
        if not key.startswith(LABEL_PREFIX + "."):
            continue
        rest = key[len(LABEL_PREFIX) + 1:]
        parts = rest.split(".", 1)
        if len(parts) != 2:
            continue
        plugin_name, field = parts
        plugins.setdefault(plugin_name, {})[field] = value

    records: dict[str, list[DNSRecord]] = {}
    for plugin_name, fields in plugins.items():
        # `traefik` is a reserved field used only as a Traefik opt-out marker; a
        # container carrying only that field is not requesting an ext-dns record.
        if set(fields.keys()) == {"traefik"}:
            continue

        hostname = fields.get("hostname")
        record_type_str = fields.get("type", "").upper()

        if not hostname:
            log.warning("Plugin '%s': missing 'hostname' label, skipping", plugin_name)
            continue
        if record_type_str not in ("A", "CNAME"):
            log.warning(
                "Plugin '%s': invalid or missing 'type' label '%s', skipping",
                plugin_name, record_type_str,
            )
            continue

        if record_type_str == "CNAME":
            target = fields.get("target")
            if not target:
                log.warning(
                    "Plugin '%s': CNAME record for '%s' missing 'target' label, skipping",
                    plugin_name, hostname,
                )
                continue
            records.setdefault(plugin_name, []).append(
                DNSRecord(
                    hostname=hostname,
                    record_type=RecordType.CNAME,
                    value=target,
                )
            )
        else:
            preferred_network = fields.get("network")
            ip = _get_container_ip(networks, preferred_network)
            if ip is None:
                if preferred_network:
                    log.warning(
                        "Plugin '%s': network '%s' not found on container, skipping",
                        plugin_name, preferred_network,
                    )
                else:
                    log.warning(
                        "Plugin '%s': container has no IP address on any network, skipping",
                        plugin_name,
                    )
                continue
            records.setdefault(plugin_name, []).append(
                DNSRecord(
                    hostname=hostname,
                    record_type=RecordType.A,
                    value=ip,
                )
            )

    return records


class DockerWatcher:
    def __init__(
        self,
        on_state_change: Callable[[], Awaitable[None]],
        docker_url: str | None = None,
        traefik: dict[str, dict] | None = None,
    ) -> None:
        self._docker_url = docker_url or "unix://var/run/docker.sock"
        self._on_state_change = on_state_change
        self._client: docker.DockerClient | None = None
        # { plugin_name: {"hostname": str | None} } for plugins with Traefik
        # integration enabled (filtered in main.py). Empty => feature off.
        self._traefik: dict[str, dict] = traefik or {}

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self._docker_url)
        return self._client

    def _discover_traefik_hostname(self, containers: list) -> str | None:
        """Auto-discover the Traefik target hostname from the first Host(`...`)
        rule on a container whose name contains 'traefik'."""
        for container in containers:
            name = (container.name or "").lstrip("/")
            if "traefik" not in name.lower():
                continue
            hosts = extract_traefik_hosts(container.labels or {})
            if hosts:
                log.info(
                    "Auto-discovered Traefik hostname '%s' from container '%s'",
                    hosts[0], name,
                )
                return hosts[0]
        return None

    def get_desired_state(
        self,
    ) -> dict[str, tuple[str, str, dict[str, list[DNSRecord]]]]:
        """
        Returns a dict keyed by container_id:
          { container_id: (container_name, short_id, { plugin_name: [DNSRecord] }) }
        Includes containers with at least one ext-dns label, plus — when Traefik
        integration is enabled — containers with a Traefik Host() router rule.
        """
        result: dict[str, tuple[str, str, dict[str, list[DNSRecord]]]] = {}
        try:
            containers = self._get_client().containers.list()
        except docker.errors.DockerException as exc:
            log.error("Failed to list containers: %s", exc)
            self._client = None
            return result

        _MISSING = object()
        discovered = _MISSING  # computed at most once per call, lazily

        for container in containers:
            labels: dict[str, str] = container.labels or {}
            has_ext_dns = any(k.startswith(LABEL_PREFIX + ".") for k in labels)
            traefik_hosts = extract_traefik_hosts(labels)

            if not has_ext_dns and not (traefik_hosts and self._traefik):
                continue

            networks: dict = {}
            try:
                networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            except Exception:
                pass

            records = extract_dns_labels(labels, networks)

            if traefik_hosts and self._traefik:
                extract_traefik_network(labels)  # parsed-but-unused for CNAME
                for plugin_name, tcfg in self._traefik.items():
                    if _traefik_opted_out(labels, plugin_name):
                        continue
                    target = tcfg.get("hostname")
                    if not target:
                        if discovered is _MISSING:
                            discovered = self._discover_traefik_hostname(containers)
                        target = discovered
                    if not target:
                        log.warning(
                            "Plugin '%s': Traefik enabled but no hostname configured "
                            "and no '*traefik*' container with a Host() rule found; "
                            "skipping Traefik CNAMEs for '%s'",
                            plugin_name, container.name.lstrip("/"),
                        )
                        continue

                    existing = records.setdefault(plugin_name, [])
                    existing_hostnames = {r.hostname for r in existing}
                    for host in traefik_hosts:
                        if host in existing_hostnames:
                            continue  # ext-dns labels take precedence
                        if host == target:
                            log.debug(
                                "Skipping self-referential Traefik CNAME '%s' -> '%s'",
                                host, target,
                            )
                            continue
                        existing.append(
                            DNSRecord(
                                hostname=host,
                                record_type=RecordType.CNAME,
                                value=target,
                                source=RecordSource.TRAEFIK,
                            )
                        )
                        existing_hostnames.add(host)

            records = {p: recs for p, recs in records.items() if recs}
            if records:
                result[container.id] = (
                    container.name.lstrip("/"),
                    container.short_id,
                    records,
                )

        return result

    async def watch(self) -> None:
        # docker-py's events() is a blocking generator. Running it directly on the
        # event loop freezes every other task (reconciler, web server) between
        # events. Offload the blocking iteration to a worker thread and hand each
        # event back to the loop via run_coroutine_threadsafe.
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._watch_blocking, loop)

    def _watch_blocking(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            events = self._get_client().events(
                decode=True,
                filters={"type": "container"},
            )
            for event in events:
                status = event.get("status", "")
                if status in ("start", "die", "destroy"):
                    asyncio.run_coroutine_threadsafe(
                        self._on_state_change(), loop
                    )
        except Exception as exc:
            log.error("Docker event stream error: %s", exc)
            self._client = None
        finally:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
