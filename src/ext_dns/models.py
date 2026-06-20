from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class RecordType(str, Enum):
    A = "A"
    CNAME = "CNAME"


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


class ContainerRecord(BaseModel):
    container_id: str
    container_name: str
    plugin: str
    hostname: str
    record_type: RecordType
    value: str
    last_updated: datetime
    dns_status: DNSVerificationStatus = DNSVerificationStatus.PENDING
    dns_checked_at: Optional[datetime] = None


class InstanceStatus(BaseModel):
    url: str
    healthy: bool
    record_count: int
    providers: list[str]
    last_reconcile: Optional[datetime]
    version: str = "0.1.7"


class RemoteInstanceInfo(BaseModel):
    name: str
    url: str
    insecure: bool = False
    proxied: bool = True
