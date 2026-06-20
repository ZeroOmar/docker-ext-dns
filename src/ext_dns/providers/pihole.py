import logging
import urllib.parse

import httpx

from ext_dns.models import DNSRecord, RecordType
from ext_dns.providers.base import DNSProvider

log = logging.getLogger(__name__)

_HOSTS_ELEMENT = urllib.parse.quote("dns/hosts", safe="")
_CNAME_ELEMENT = urllib.parse.quote("dns/cnameRecords", safe="")


class PiholeProvider(DNSProvider):
    """
    DNS provider for Pi-hole v6.

    Config keys:
      url      (str, required) — Pi-hole base URL, e.g. "http://pihole:80"
      password (str, optional) — Pi-hole web password; omit if no auth is set
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._url = config["url"].rstrip("/")
        self._password: str | None = config.get("password")
        self._sid: str | None = None
        self._no_auth = False

    @property
    def name(self) -> str:
        return "pihole"

    def _client(self) -> httpx.AsyncClient:
        headers = {}
        if self._sid:
            headers["sid"] = self._sid
        return httpx.AsyncClient(base_url=self._url, headers=headers, timeout=10)

    async def _ensure_auth(self) -> None:
        async with httpx.AsyncClient(base_url=self._url, timeout=10) as client:
            resp = await client.get("/api/auth")
            resp.raise_for_status()
            session = resp.json()["session"]

            if session["valid"] and session["sid"] is None and session["validity"] == -1:
                self._no_auth = True
                self._sid = None
                return

            if session["valid"] and session["sid"]:
                self._sid = session["sid"]
                return

            if not self._password:
                raise RuntimeError(
                    "Pi-hole requires authentication but no password is configured"
                )
            login = await client.post("/api/auth", json={"password": self._password})
            login.raise_for_status()
            self._sid = login.json()["session"]["sid"]

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response:
        async with self._client() as client:
            resp = await client.request(method, path, **kwargs)
            if resp.status_code == 401:
                self._sid = None
                await self._ensure_auth()
                async with self._client() as retry_client:
                    resp = await retry_client.request(method, path, **kwargs)
            return resp

    async def list_records(self) -> list[DNSRecord]:
        await self._ensure_auth()
        records: list[DNSRecord] = []

        resp = await self._request("GET", f"/api/config/{_HOSTS_ELEMENT}")
        resp.raise_for_status()
        hosts: list[str] = (
            resp.json().get("config", {}).get("dns", {}).get("hosts", [])
        )
        for entry in hosts:
            parts = entry.split()
            if len(parts) >= 2:
                ip, hostname = parts[0], parts[1]
                records.append(
                    DNSRecord(hostname=hostname, record_type=RecordType.A, value=ip)
                )

        resp = await self._request("GET", f"/api/config/{_CNAME_ELEMENT}")
        resp.raise_for_status()
        cnames: list[str] = (
            resp.json().get("config", {}).get("dns", {}).get("cnameRecords", [])
        )
        for entry in cnames:
            parts = entry.split(",")
            if len(parts) >= 2:
                alias, target = parts[0], parts[1]
                records.append(
                    DNSRecord(
                        hostname=alias, record_type=RecordType.CNAME, value=target
                    )
                )

        return records

    async def create_record(self, record: DNSRecord) -> None:
        await self._ensure_auth()
        element, value_str = self._encode(record)
        resp = await self._request(
            "PUT",
            f"/api/config/{element}/{value_str}",
            params={"restart": "true"},
        )
        if resp.status_code not in (201, 400):
            resp.raise_for_status()
        if resp.status_code == 400:
            body = resp.json()
            if "already" not in body.get("error", {}).get("message", "").lower():
                resp.raise_for_status()

    async def update_record(self, record: DNSRecord) -> None:
        await self.delete_record(record.hostname, record.record_type)
        await self.create_record(record)

    async def delete_record(self, hostname: str, record_type: str) -> None:
        await self._ensure_auth()
        if record_type == RecordType.A or record_type == "A":
            records = await self.list_records()
            existing = next(
                (r for r in records if r.hostname == hostname and r.record_type == RecordType.A),
                None,
            )
            if existing is None:
                return
            value_str = urllib.parse.quote(
                f"{existing.value} {hostname}", safe=""
            )
            element = _HOSTS_ELEMENT
        else:
            records = await self.list_records()
            existing = next(
                (r for r in records if r.hostname == hostname and r.record_type == RecordType.CNAME),
                None,
            )
            if existing is None:
                return
            value_str = urllib.parse.quote(
                f"{hostname},{existing.value}", safe=""
            )
            element = _CNAME_ELEMENT

        resp = await self._request(
            "DELETE",
            f"/api/config/{element}/{value_str}",
            params={"restart": "true"},
        )
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    async def logout(self) -> None:
        if self._sid and not self._no_auth:
            try:
                async with self._client() as client:
                    await client.delete("/api/auth")
            except Exception:
                pass
            self._sid = None

    def _encode(self, record: DNSRecord) -> tuple[str, str]:
        if record.record_type == RecordType.A:
            raw = f"{record.value} {record.hostname}"
            return _HOSTS_ELEMENT, urllib.parse.quote(raw, safe="")
        else:
            raw = f"{record.hostname},{record.value}"
            return _CNAME_ELEMENT, urllib.parse.quote(raw, safe="")
