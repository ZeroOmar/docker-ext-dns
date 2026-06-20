import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import dns.exception
import dns.resolver

from ext_dns.docker_watcher import DockerWatcher
from ext_dns.models import (
    ContainerRecord,
    DNSRecord,
    DNSVerificationStatus,
    RecordType,
)
from ext_dns.providers.base import DNSProvider

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Reconciler:
    def __init__(
        self,
        providers: list[DNSProvider],
        interval: int,
        change_concurrency: int = 2,
        change_delay: float = 0.0,
    ) -> None:
        self._providers: dict[str, DNSProvider] = {p.name: p for p in providers}
        self._interval = interval
        self._change_concurrency = max(1, change_concurrency)
        self._change_delay = max(0.0, change_delay)
        self._watcher: Optional[DockerWatcher] = None
        self._state: dict[str, ContainerRecord] = {}
        self._managed: set[tuple[str, str]] = set()
        self._event = asyncio.Event()
        self.last_reconcile: Optional[datetime] = None

    def set_watcher(self, watcher: DockerWatcher) -> None:
        self._watcher = watcher

    @property
    def state(self) -> list[ContainerRecord]:
        return list(self._state.values())

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())

    async def trigger_reconcile(self) -> None:
        self._event.set()

    async def run(self) -> None:
        while True:
            await self._reconcile()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._interval)
                self._event.clear()
            except asyncio.TimeoutError:
                pass

    async def _reconcile(self) -> None:
        if self._watcher is None:
            return

        desired = await asyncio.to_thread(self._watcher.get_desired_state)

        # Build desired set: (plugin, hostname) -> (DNSRecord, container_id, container_name)
        desired_records: dict[tuple[str, str], tuple[DNSRecord, str, str]] = {}
        for container_id, (container_name, _short_id, plugin_records) in desired.items():
            for plugin_name, records in plugin_records.items():
                if plugin_name not in self._providers:
                    log.warning(
                        "Container '%s' references unconfigured plugin '%s', skipping",
                        container_name, plugin_name,
                    )
                    continue
                for record in records:
                    desired_records[(plugin_name, record.hostname)] = (
                        record, container_id, container_name,
                    )

        # Build actual set per provider
        actual_records: dict[tuple[str, str], DNSRecord] = {}
        for plugin_name, provider in self._providers.items():
            try:
                records = await provider.list_records()
                for r in records:
                    actual_records[(plugin_name, r.hostname)] = r
            except Exception as exc:
                log.error("Failed to list records from '%s': %s", plugin_name, exc, exc_info=True)

        # Compute diff
        creates: list[tuple[str, DNSRecord, str, str]] = []
        updates: list[tuple[str, DNSRecord, str, str]] = []
        deletes: list[tuple[str, str, str]] = []

        for (plugin_name, hostname), (record, container_id, container_name) in desired_records.items():
            actual = actual_records.get((plugin_name, hostname))
            if actual is None:
                creates.append((plugin_name, record, container_id, container_name))
            elif actual.value != record.value or actual.record_type != record.record_type:
                updates.append((plugin_name, record, container_id, container_name))
            else:
                self._upsert_state(plugin_name, record, container_id, container_name)

        for (plugin_name, hostname), actual in actual_records.items():
            if (plugin_name, hostname) in self._managed and (plugin_name, hostname) not in desired_records:
                deletes.append((plugin_name, hostname, actual.record_type))

        # Remove state entries for containers no longer running
        stale_keys = [
            k for k in self._state
            if k not in {
                f"{plugin}:{record.hostname}"
                for (plugin, hostname), (record, cid, cname) in desired_records.items()
                for _ in [None]  # dummy
            }
        ]
        # Simpler stale key detection
        desired_state_keys = {
            f"{plugin}:{record.hostname}"
            for (plugin, _hostname), (record, _cid, _cname) in desired_records.items()
        }
        for k in list(self._state.keys()):
            if k not in desired_state_keys:
                del self._state[k]

        # Apply changes concurrently
        changed_plugins: set[str] = set()
        tasks = []
        for plugin_name, record, container_id, container_name in creates:
            changed_plugins.add(plugin_name)
            tasks.append(self._do_create(plugin_name, record, container_id, container_name))
        for plugin_name, record, container_id, container_name in updates:
            changed_plugins.add(plugin_name)
            tasks.append(self._do_update(plugin_name, record, container_id, container_name))
        for plugin_name, hostname, record_type in deletes:
            changed_plugins.add(plugin_name)
            tasks.append(self._do_delete(plugin_name, hostname, record_type))

        if tasks:
            if len(tasks) > self._change_concurrency:
                log.info(
                    "Applying %d record changes (max %d at a time%s)",
                    len(tasks), self._change_concurrency,
                    f", {self._change_delay}s apart" if self._change_delay else "",
                )
            await self._apply_throttled(tasks)
            # Restart DNS only once per plugin, after all changes have been applied.
            for plugin_name in changed_plugins:
                try:
                    await self._providers[plugin_name].restart_dns()
                except Exception as exc:
                    log.error("Failed to restart DNS for '%s': %s", plugin_name, exc, exc_info=True)

        # DNS verification for all managed records
        verify_tasks = [
            self._verify_record(key, rec)
            for key, rec in self._state.items()
        ]
        if verify_tasks:
            await asyncio.gather(*verify_tasks, return_exceptions=True)

        self.last_reconcile = _utcnow()
        log.info(
            "Reconcile complete: %d desired, %d creates, %d updates, %d deletes",
            len(desired_records), len(creates), len(updates), len(deletes),
        )

    async def _apply_throttled(self, tasks: list) -> None:
        """Run change operations with bounded concurrency (and an optional pause
        after each) so a large diff does not flood the DNS backend."""
        sem = asyncio.Semaphore(self._change_concurrency)

        async def run(coro) -> None:
            async with sem:
                try:
                    await coro
                finally:
                    if self._change_delay:
                        await asyncio.sleep(self._change_delay)

        await asyncio.gather(*(run(t) for t in tasks), return_exceptions=True)

    def _upsert_state(
        self,
        plugin_name: str,
        record: DNSRecord,
        container_id: str,
        container_name: str,
    ) -> None:
        key = f"{plugin_name}:{record.hostname}"
        existing = self._state.get(key)
        if existing is None:
            self._state[key] = ContainerRecord(
                container_id=container_id,
                container_name=container_name,
                plugin=plugin_name,
                hostname=record.hostname,
                record_type=record.record_type,
                value=record.value,
                source=record.source,
                last_updated=_utcnow(),
            )
        else:
            existing.container_id = container_id
            existing.container_name = container_name
            existing.value = record.value
            existing.record_type = record.record_type
            existing.source = record.source

    async def _do_create(
        self,
        plugin_name: str,
        record: DNSRecord,
        container_id: str,
        container_name: str,
    ) -> None:
        provider = self._providers[plugin_name]
        try:
            await provider.create_record(record)
            self._managed.add((plugin_name, record.hostname))
            self._upsert_state(plugin_name, record, container_id, container_name)
            log.info("Created %s record '%s' via %s", record.record_type, record.hostname, plugin_name)
        except Exception as exc:
            log.error("Failed to create record '%s' via %s: %s", record.hostname, plugin_name, exc, exc_info=True)

    async def _do_update(
        self,
        plugin_name: str,
        record: DNSRecord,
        container_id: str,
        container_name: str,
    ) -> None:
        provider = self._providers[plugin_name]
        try:
            await provider.update_record(record)
            self._managed.add((plugin_name, record.hostname))
            self._upsert_state(plugin_name, record, container_id, container_name)
            log.info("Updated %s record '%s' via %s", record.record_type, record.hostname, plugin_name)
        except Exception as exc:
            log.error("Failed to update record '%s' via %s: %s", record.hostname, plugin_name, exc, exc_info=True)

    async def _do_delete(
        self,
        plugin_name: str,
        hostname: str,
        record_type: str,
    ) -> None:
        provider = self._providers[plugin_name]
        try:
            await provider.delete_record(hostname, record_type)
            self._managed.discard((plugin_name, hostname))
            key = f"{plugin_name}:{hostname}"
            self._state.pop(key, None)
            log.info("Deleted record '%s' via %s", hostname, plugin_name)
        except Exception as exc:
            log.error("Failed to delete record '%s' via %s: %s", hostname, plugin_name, exc, exc_info=True)

    async def _verify_record(self, key: str, record: ContainerRecord) -> None:
        record.dns_status = DNSVerificationStatus.CHECKING
        loop = asyncio.get_running_loop()
        try:
            answers = await loop.run_in_executor(
                None,
                lambda: dns.resolver.resolve(record.hostname, record.record_type.value),
            )
            resolved = answers[0].to_text().rstrip(".")
            if record.record_type == RecordType.A:
                status = (
                    DNSVerificationStatus.NOERROR
                    if resolved == record.value
                    else DNSVerificationStatus.MISMATCH
                )
            else:
                status = (
                    DNSVerificationStatus.NOERROR
                    if resolved == record.value.rstrip(".")
                    else DNSVerificationStatus.MISMATCH
                )
        except dns.resolver.NXDOMAIN:
            status = DNSVerificationStatus.NXDOMAIN
        except dns.resolver.NoAnswer:
            status = DNSVerificationStatus.NXDOMAIN
        except dns.exception.DNSException:
            status = DNSVerificationStatus.SERVFAIL
        except Exception:
            status = DNSVerificationStatus.SERVFAIL

        record.dns_status = status
        record.dns_checked_at = _utcnow()
