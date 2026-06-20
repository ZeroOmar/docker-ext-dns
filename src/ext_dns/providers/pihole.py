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
            trust_env=False,
        )

    async def _ensure_auth(self) -> None:
        if self._no_auth or self._sid:
            return
        async with self._auth_lock:
            # Re-check after acquiring lock: another coroutine may have auth'd first.
            if self._no_auth or self._sid:
                return

            log.info("Pi-hole authenticating to %s", self._url)
            async with httpx.AsyncClient(
                base_url=self._url, timeout=10, verify=not self._insecure, trust_env=False
            ) as client:
                try:
                    resp = await client.get("/api/auth")
                    log.debug("Pi-hole GET /api/auth → %d", resp.status_code)
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
                    else:
                        log.warning(
                            "Pi-hole GET /api/auth returned %d — proceeding to login",
                            resp.status_code,
                        )
                except Exception as exc:
                    log.warning(
                        "Pi-hole GET /api/auth at %s failed (%s: %s) — proceeding to login",
                        self._url, type(exc).__name__, exc,
                    )

                if not self._password:
                    raise RuntimeError(
                        "Pi-hole requires authentication but no password is configured"
                    )
                log.info("Pi-hole POST /api/auth at %s", self._url)
                try:
                    login = await client.post("/api/auth", json={"password": self._password})
                except Exception as exc:
                    raise RuntimeError(
                        f"Pi-hole connection to {self._url} failed: {type(exc).__name__}: {exc}"
                    ) from exc
                log.debug("Pi-hole POST /api/auth → %d", login.status_code)
                if login.status_code == 401:
                    log.warning("Pi-hole POST /api/auth 401 body: %s", login.text[:400])
                    raise RuntimeError(
                        "Pi-hole authentication failed — check the configured password"
                    )
                login.raise_for_status()
                self._sid = login.json()["session"]["sid"]
                log.info(
                    "Authenticated to Pi-hole (sid: %s…)",
                    self._sid[:8] if self._sid else "?",
                )

    def _log_response(self, method: str, path: str, resp: httpx.Response) -> None:
        level = logging.DEBUG if resp.status_code < 400 else logging.WARNING
        log.log(level, "Pi-hole %s %s → %d", method, path, resp.status_code)
        if resp.status_code >= 400:
            try:
                log.warning("Pi-hole response body: %s", resp.text[:800])
            except Exception:
                pass

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response:
        async with self._client() as client:
            resp = await client.request(method, path, **kwargs)
            self._log_response(method, path, resp)
            if resp.status_code == 401:
                self._sid = None
                self._no_auth = False
                await self._ensure_auth()
                async with self._client() as retry_client:
                    resp = await retry_client.request(method, path, **kwargs)
                    self._log_response(method, path, resp)
            return resp

    async def list_records(self) -> list[DNSRecord]:
        await self._ensure_auth()
        records: list[DNSRecord] = []

        resp = await self._request("GET", f"/api/config/{_HOSTS_ELEMENT}")
        resp.raise_for_status()
        hosts: list[str] = (
            resp.json().get("config", {}).get("dns", {}).get("hosts", []) or []
        )
        for entry in hosts:
            parts = entry.split()
            if len(parts) >= 2:
                ip, hostname = parts[0], parts[1]
                records.append(
                    DNSRecord(hostname=hostname, record_type=RecordType.A, value=ip)
                )
        log.debug("Pi-hole A records: %d entries", len(hosts))

        resp = await self._request("GET", f"/api/config/{_CNAME_ELEMENT}")
        resp.raise_for_status()
        cnames: list[str] = (
            resp.json().get("config", {}).get("dns", {}).get("cnameRecords", []) or []
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
        log.debug("Pi-hole CNAME records: %d entries", len(cnames))

        return records

    async def _purge_name(
        self, hostname: str, records: list[DNSRecord] | None = None
    ) -> None:
        """Delete every existing record for `hostname` — both A (dns/hosts) and
        CNAME (dns/cnameRecords). docker-ext-dns is the source of truth for the
        names it manages, so a name is fully cleared before a record is (re)created.
        This also prevents a stale A/CNAME lingering when a record changes type."""
        if records is None:
            records = await self.list_records()
        for r in records:
            if r.hostname != hostname:
                continue
            if r.record_type == RecordType.A:
                element = _HOSTS_ELEMENT
                value_str = urllib.parse.quote(f"{r.value} {hostname}", safe="")
            else:
                element = _CNAME_ELEMENT
                value_str = urllib.parse.quote(f"{hostname},{r.value}", safe="")
            path = f"/api/config/{element}/{value_str}"
            log.debug("Pi-hole purge DELETE %s", path)
            resp = await self._request("DELETE", path, params={"restart": "false"})
            if resp.status_code not in (204, 404):
                resp.raise_for_status()

    async def create_record(self, record: DNSRecord) -> None:
        await self._ensure_auth()
        await self._purge_name(record.hostname)
        element, value_str = self._encode(record)
        path = f"/api/config/{element}/{value_str}"
        log.debug("Pi-hole PUT %s (type=%s value=%s)", path, record.record_type, record.value)
        resp = await self._request("PUT", path, params={"restart": "false"})
        if resp.status_code not in (201, 400):
            resp.raise_for_status()
        if resp.status_code == 400:
            body = resp.json()
            if "already" not in body.get("error", {}).get("message", "").lower():
                resp.raise_for_status()

    async def update_record(self, record: DNSRecord) -> None:
        # create_record purges any existing record of this name first (both
        # elements), so it correctly handles value changes and A<->CNAME flips.
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

        path = f"/api/config/{element}/{value_str}"
        log.debug("Pi-hole DELETE %s", path)
        resp = await self._request("DELETE", path, params={"restart": "false"})
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    async def restart_dns(self) -> None:
        await self._ensure_auth()
        log.info("Pi-hole restarting DNS")
        resp = await self._request("POST", "/api/action/restartdns")
        if resp.status_code not in (200, 204):
            log.warning("Pi-hole restartdns returned %d: %s", resp.status_code, resp.text[:200])

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
