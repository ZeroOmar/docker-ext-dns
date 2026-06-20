import asyncio
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
      url      (str, required)  — Pi-hole base URL, e.g. "https://pihole:443"
      password (str, optional)  — Pi-hole web password; omit if no auth is set
      insecure (bool, optional) — skip TLS certificate verification (default false)
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._url = config["url"].rstrip("/")
        self._password: str | None = config.get("password")
        self._insecure: bool = bool(config.get("insecure", False))
        self._sid: str | None = None
        self._no_auth = False
        self._auth_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "pihole"

    def _client(self) -> httpx.AsyncClient:
        headers = {}
        if self._sid:
            headers["sid"] = self._sid
        return httpx.AsyncClient(
            base_url=self._url,
            headers=headers,
            timeout=10,
            verify=not self._insecure,
        )

    async def _ensure_auth(self) -> None:
        if self._no_auth or self._sid:
            return
        async with self._auth_lock:
            # Re-check after acquiring lock: another coroutine may have auth'd first.
            if self._no_auth or self._sid:
                return

            async with httpx.AsyncClient(
                base_url=self._url, timeout=10, verify=not self._insecure
            ) as client:
                try:
                    resp = await client.get("/api/auth")
                    if resp.status_code == 200:
                        session = resp.json().get("session", {})
                        if (
                            session.get("valid")
                            and session.get("sid") is None
                            and session.get("validity") == -1
                        ):
                            log.info("Pi-hole requires no authentication")
                            self._no_auth = True
                            return
                        if session.get("valid") and session.get("sid"):
                            log.debug("Reusing existing Pi-hole session")
                            self._sid = session["sid"]
                            return
                except Exception as exc:
                    log.debug("GET /api/auth probe failed (%s), proceeding to login", exc)

                if not self._password:
                    raise RuntimeError(
                        "Pi-hole requires authentication but no password is configured"
                    )
                log.debug("Authenticating to Pi-hole at %s", self._url)
                login = await client.post("/api/auth", json={"password": self._password})
                if login.status_code == 401:
                    raise RuntimeError(
                        "Pi-hole authentication failed — check the configured password"
                    )
                login.raise_for_status()
                self._sid = login.json()["session"]["sid"]
                log.info(
                    "Authenticated to Pi-hole (sid: %s…)",
                    self._sid[:8] if self._sid else "?",
                )

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response:
        async with self._client() as client:
            resp = await client.request(method, path, **kwargs)
            if resp.status_code == 401:
                self._sid = None
                self._no_auth = False
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
