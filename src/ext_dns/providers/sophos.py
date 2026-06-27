import asyncio
import logging
import re

from sophosfirewall_python.api_client import (
    SophosFirewallAPIError,
    SophosFirewallZeroRecords,
)
from sophosfirewall_python.firewallapi import SophosFirewall

from ext_dns.models import DNSRecord, RecordType
from ext_dns.providers.base import DNSProvider

log = logging.getLogger(__name__)

# Status codes returned by the DNS host entry add/edit operations that are fatal
# for a single entry (vs. recoverable ones like 502 "already exists" on add, or
# 503 "identical/reverse-DNS conflict").
_TERMINAL_CODES = {"500", "510", "541"}


class SophosFirewallProvider(DNSProvider):
    """
    DNS provider for Sophos Firewall (SFOS) v22.0+, managing DNS host entries
    via the on-box XML API (through the official `sophosfirewall-python` SDK).

    Sophos DNS host entries support only A/AAAA/PTR records — not CNAME — so this
    provider declares `supports_cname = False`; the reconciler resolves desired
    CNAMEs to an IP and hands them here as A records.

    Config keys:
      hostname     (str, required)  — firewall host/IP
      username     (str, required)  — API user account
      password     (str, required)  — API user password
      port         (int, optional)  — admin/API port (default 4444)
      insecure     (bool, optional) — skip TLS verification (default false)
      min_os_major (int, optional)  — minimum acceptable SFOS major version (default 22)
      ttl          (int, optional)  — TTL for new A records (default 60)
    """

    supports_cname = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        try:
            self._hostname: str = config["hostname"]
            self._username: str = config["username"]
            self._password: str = config["password"]
        except KeyError as exc:
            raise ValueError(
                f"sophos-firewall requires config key {exc}"
            ) from exc
        self._port: int = int(config.get("port", 4444))
        self._insecure: bool = bool(config.get("insecure", False))
        self._min_os_major: int = int(config.get("min_os_major", 22))
        self._ttl: int = int(config.get("ttl", 60))

        self._fw = SophosFirewall(
            username=self._username,
            password=self._password,
            hostname=self._hostname,
            port=self._port,
            verify=not self._insecure,
        )

        # One-time OS-compatibility gate (None = not yet checked).
        self._compatible: bool | None = None
        self._incompat_msg: str | None = None
        self._compat_lock = asyncio.Lock()
        # Sophos applies DNS host entry changes immediately; still serialize
        # mutations so concurrent add/update/remove don't race.
        self._write_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "sophos-firewall"

    # ------------------------------------------------------------------ compat

    async def _ensure_compatible(self) -> None:
        if self._compatible is True:
            return
        if self._compatible is False:
            raise RuntimeError(f"Sophos firewall incompatible: {self._incompat_msg}")
        async with self._compat_lock:
            if self._compatible is True:
                return
            if self._compatible is False:
                raise RuntimeError(
                    f"Sophos firewall incompatible: {self._incompat_msg}"
                )
            # login() raises on bad credentials / unreachable host — let those
            # propagate without latching _compatible, so a transient failure is
            # retried on the next reconcile rather than disabling the provider.
            xml = await asyncio.to_thread(self._fw.login, "xml")
            major = self._parse_major_version(xml)
            if major is None:
                self._compatible = False
                self._incompat_msg = (
                    "could not determine SFOS version from API response"
                )
                raise RuntimeError(f"Sophos firewall incompatible: {self._incompat_msg}")
            if major < self._min_os_major:
                self._compatible = False
                self._incompat_msg = (
                    f"SFOS major version {major} is below the required "
                    f"minimum {self._min_os_major}"
                )
                raise RuntimeError(f"Sophos firewall incompatible: {self._incompat_msg}")
            self._compatible = True
            log.info(
                "Sophos firewall %s:%s reports SFOS major version %d (>= %d) — compatible",
                self._hostname, self._port, major, self._min_os_major,
            )

    @staticmethod
    def _parse_major_version(login_xml: str) -> int | None:
        """The SFOS XML API echoes its version in the <Response APIVersion="..">
        attribute (e.g. v22.0 -> "2200.1"); the first two digits are the major
        version (1905 -> 19, 2150 -> 21, 2200 -> 22)."""
        m = re.search(r'APIVersion\s*=\s*"(\d{2})', login_xml or "")
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------------ read

    async def list_records(self) -> list[DNSRecord]:
        await self._ensure_compatible()
        try:
            data = await asyncio.to_thread(self._fw.get_tag, "DNSHostEntry")
        except SophosFirewallZeroRecords:
            return []

        records: list[DNSRecord] = []
        entries = (data or {}).get("Response", {}).get("DNSHostEntry")
        for entry in self._as_list(entries):
            if not isinstance(entry, dict):
                continue
            hostname = entry.get("HostName")
            if not hostname:
                continue
            address_list = entry.get("AddressList") or {}
            for addr in self._as_list(address_list.get("Address")):
                if not isinstance(addr, dict):
                    continue
                # Only surface manually-entered IPv4 addresses — the kind we
                # create. InterfaceIP entries put an interface name (e.g. "PortA")
                # in <IPAddress>, not a real IP, and we never manage those.
                if (addr.get("EntryType") or "Manual") != "Manual":
                    continue
                if (addr.get("IPFamily") or "IPv4") != "IPv4":
                    continue
                ip = addr.get("IPAddress")
                if not ip:
                    continue
                records.append(
                    DNSRecord(hostname=hostname, record_type=RecordType.A, value=ip)
                )
        log.debug("Sophos DNS host entries: %d A records", len(records))
        return records

    # ------------------------------------------------------------------ write

    async def create_record(self, record: DNSRecord) -> None:
        await self._ensure_compatible()
        await self._upsert(record)

    async def update_record(self, record: DNSRecord) -> None:
        await self._ensure_compatible()
        await self._upsert(record)

    async def _upsert(self, record: DNSRecord) -> None:
        """Create-or-update the A host entry for `record`, handling the documented
        Sophos status codes. Tries `add` first; on 502 (already exists) switches
        to `update`. A 503 means the desired config already exists, *unless* the
        clashing name in the message differs from ours — that is a reverse-DNS
        (PTR) conflict, which we resolve by re-submitting without the PTR."""
        if record.record_type != RecordType.A:
            # CNAMEs are converted to A upstream by the reconciler; Sophos cannot
            # store anything else natively.
            log.warning(
                "Sophos cannot store %s record '%s' natively; skipping",
                record.record_type.value, record.hostname,
            )
            return

        async with self._write_lock:
            # 1) add
            try:
                await asyncio.to_thread(self._submit, record, "add", "Enable")
                return
            except SophosFirewallAPIError as exc:
                code, _msg = self._status(exc)
                if code == "503":
                    await self._resolve_503(record, "add")
                    return
                if code != "502":
                    raise  # terminal add failure (500/510/541/…)
                log.debug("Sophos entry '%s' already exists; updating", record.hostname)

            # 2) update (entry already existed)
            try:
                await asyncio.to_thread(self._submit, record, "update", "Enable")
                return
            except SophosFirewallAPIError as exc:
                code, _msg = self._status(exc)
                if code == "503":
                    await self._resolve_503(record, "update")
                    return
                raise

    async def _resolve_503(self, record: DNSRecord, operation: str) -> None:
        """A 503 ("...already exists") is ambiguous: either our exact entry is
        already present (a genuine no-op), or the IP/reverse-DNS (PTR) is already
        owned by a *different* host entry. Disambiguate from live state rather than
        the (un-quoted) message: if our entry already exists we're done; otherwise
        re-submit with reverse lookup disabled so the A record still lands (the PTR
        for that IP belongs to the other entry)."""
        if await self._exists(record):
            log.debug("Sophos entry '%s' already up to date", record.hostname)
            return
        log.info(
            "Sophos %s for '%s' (%s) hit an IP/PTR conflict; retrying without PTR",
            operation, record.hostname, record.value,
        )
        await asyncio.to_thread(self._submit, record, operation, "Disable")

    async def _exists(self, record: DNSRecord) -> bool:
        """True if a DNS host entry for this hostname already maps to this IP."""
        for r in await self.list_records():
            if (
                r.hostname.lower() == record.hostname.lower()
                and r.value == record.value
            ):
                return True
        return False

    def _submit(self, record: DNSRecord, operation: str, reverse: str) -> None:
        """Blocking SDK call (run via asyncio.to_thread)."""
        self._fw.submit_xml(self._payload(record, reverse), set_operation=operation)

    def _payload(self, record: DNSRecord, reverse: str) -> str:
        return (
            "<DNSHostEntry>"
            f"<HostName>{record.hostname}</HostName>"
            "<AddressList><Address>"
            "<EntryType>Manual</EntryType>"
            "<IPFamily>IPv4</IPFamily>"
            f"<IPAddress>{record.value}</IPAddress>"
            f"<TTL>{self._ttl}</TTL>"
            "<Weight>1</Weight>"
            "<PublishOnWAN>Disable</PublishOnWAN>"
            "</Address></AddressList>"
            f"<AddReverseDNSLookUp>{reverse}</AddReverseDNSLookUp>"
            "</DNSHostEntry>"
        )

    async def delete_record(self, hostname: str, record_type: str) -> None:
        await self._ensure_compatible()
        async with self._write_lock:
            try:
                await asyncio.to_thread(
                    self._fw.remove, "DNSHostEntry", hostname, "HostName"
                )
            except SophosFirewallZeroRecords:
                return
            except SophosFirewallAPIError as exc:
                _code, msg = self._status(exc)
                low = msg.lower()
                # A missing entry reports 500 "Operation could not be performed on
                # Entity." — for delete that means it's already gone (desired state).
                if "could not be performed" in low or (
                    "not" in low and ("exist" in low or "found" in low)
                ):
                    log.debug("Sophos delete '%s': entry not present (%s)", hostname, msg)
                    return
                raise

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _as_list(value) -> list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    @staticmethod
    def _status(exc: SophosFirewallAPIError) -> tuple[str, str]:
        """Extract (code, message) from a SophosFirewallAPIError. The SDK raises
        it with either the response element dict ({'Status': {'@code','#text'}})
        from submit_xml, or a 'CODE: text' string from get/remove/_post."""
        arg = exc.args[0] if exc.args else ""
        if isinstance(arg, dict):
            status = arg.get("Status")
            if isinstance(status, dict):
                return str(status.get("@code", "")), str(status.get("#text", ""))
            return "", str(arg)
        text = str(arg)
        m = re.match(r"\s*(\d{3})\s*[:\-]\s*(.*)", text, re.S)
        if m:
            return m.group(1), m.group(2).strip()
        return "", text
