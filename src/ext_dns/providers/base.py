from abc import ABC, abstractmethod

from ext_dns.models import DNSRecord


class DNSProvider(ABC):
    # Whether this provider can natively store CNAME records. Providers that
    # cannot (e.g. Sophos Firewall DNS host entries, which support only A/AAAA/
    # PTR) set this to False; the reconciler then resolves desired CNAMEs to an
    # IP and manages them as A records instead.
    supports_cname: bool = True

    def __init__(self, config: dict) -> None:
        self.config = config

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def list_records(self) -> list[DNSRecord]: ...

    @abstractmethod
    async def create_record(self, record: DNSRecord) -> None: ...

    @abstractmethod
    async def update_record(self, record: DNSRecord) -> None: ...

    @abstractmethod
    async def delete_record(self, hostname: str, record_type: str) -> None: ...

    async def restart_dns(self) -> None:
        """Called once after a batch of creates/updates/deletes. No-op by default."""

    async def health_check(self) -> bool:
        try:
            await self.list_records()
            return True
        except Exception:
            return False
