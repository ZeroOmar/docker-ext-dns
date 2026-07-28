from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RecordType(str, Enum):
    A = "A"
    CNAME = "CNAME"


class RecordSource(str, Enum):
    EXT_DNS = "ext-dns"
    TRAEFIK = "traefik"


class DNSVerificationStatus(str, Enum):
    PENDING = "pending"
    CHECKING = "checking"
    NOERROR = "NOERROR"
    NXDOMAIN = "NXDOMAIN"
    SERVFAIL = "SERVFAIL"
    MISMATCH = "MISMATCH"


class DNSRecord(BaseModel):
    hostname: str
    record_type: RecordType
    value: str
    source: RecordSource = RecordSource.EXT_DNS


class ContainerRecord(BaseModel):
    container_id: str
    container_name: str
    plugin: str
    hostname: str
    record_type: RecordType
    value: str
    source: RecordSource = RecordSource.EXT_DNS
    last_updated: datetime
    dns_status: DNSVerificationStatus = DNSVerificationStatus.PENDING
    dns_checked_at: Optional[datetime] = None


class ComponentHealth(BaseModel):
    """Health of a single subsystem. `detail` carries the error message when
    `ok` is False, or a short status note when healthy."""
    ok: bool
    detail: Optional[str] = None


class InstanceStatus(BaseModel):
    name: str = "local"
    url: str
    healthy: bool  # overall: app + docker socket + every provider are ok
    record_count: int
    providers: list[str]
    last_reconcile: Optional[datetime]
    version: str = "0.13.0"
    # Per-subsystem breakdown of the overall `healthy` flag.
    app: ComponentHealth = Field(default_factory=lambda: ComponentHealth(ok=True))
    docker: ComponentHealth = Field(default_factory=lambda: ComponentHealth(ok=True))
    provider_health: dict[str, ComponentHealth] = Field(default_factory=dict)


class RemoteInstanceInfo(BaseModel):
    name: str
    url: str
    insecure: bool = False
    proxied: bool = True
