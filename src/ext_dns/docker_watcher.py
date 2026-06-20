import asyncio
import logging
from typing import Awaitable, Callable

import docker
import docker.errors

from ext_dns.models import DNSRecord, RecordType

log = logging.getLogger(__name__)

LABEL_PREFIX = "ext-dns"


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
) -> dict[str, DNSRecord]:
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

    records: dict[str, DNSRecord] = {}
    for plugin_name, fields in plugins.items():
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
            records[plugin_name] = DNSRecord(
                hostname=hostname,
                record_type=RecordType.CNAME,
                value=target,
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
            records[plugin_name] = DNSRecord(
                hostname=hostname,
                record_type=RecordType.A,
                value=ip,
            )

    return records


class DockerWatcher:
    def __init__(
        self,
        on_state_change: Callable[[], Awaitable[None]],
        docker_url: str | None = None,
    ) -> None:
        self._docker_url = docker_url or "unix://var/run/docker.sock"
        self._on_state_change = on_state_change
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self._docker_url)
        return self._client

    def get_desired_state(
        self,
    ) -> dict[str, tuple[str, str, dict[str, DNSRecord]]]:
        """
        Returns a dict keyed by container_id:
          { container_id: (container_name, short_id, { plugin_name: DNSRecord }) }
        Only containers that have at least one ext-dns label are included.
        """
        result: dict[str, tuple[str, str, dict[str, DNSRecord]]] = {}
        try:
            containers = self._get_client().containers.list()
        except docker.errors.DockerException as exc:
            log.error("Failed to list containers: %s", exc)
            self._client = None
            return result

        for container in containers:
            labels: dict[str, str] = container.labels or {}
            if not any(k.startswith(LABEL_PREFIX + ".") for k in labels):
                continue

            networks: dict = {}
            try:
                networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            except Exception:
                pass

            records = extract_dns_labels(labels, networks)
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
