import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ext_dns.config import AppConfig
from ext_dns.models import (
    ComponentHealth,
    ContainerRecord,
    DNSVerificationStatus,
    InstanceStatus,
    RemoteInstanceInfo,
)

_STATIC_DIR = Path(__file__).parent / "static"

# The health check probes providers (login + list) and the Docker socket, so it
# is not free. Docker's HEALTHCHECK and the UI/peer polling can call it often;
# cache the computed result briefly to avoid hammering the DNS backends.
_HEALTH_TTL = 5.0


def build_app(reconciler, config: AppConfig) -> FastAPI:
    app = FastAPI(title="docker-ext-dns", docs_url=None, redoc_url=None)

    _remote_by_name = {inst.name: inst for inst in config.instances}

    _health_cache: dict[str, object] = {"at": 0.0, "value": None}
    _health_lock = asyncio.Lock()

    def _app_health() -> ComponentHealth:
        """The app is 'running fine' if the reconcile loop is alive. A stuck loop
        leaves last_reconcile growing stale even while the web server keeps
        answering, so flag that."""
        last = reconciler.last_reconcile
        if last is None:
            return ComponentHealth(ok=True, detail="starting up; no reconcile completed yet")
        age = (datetime.now(timezone.utc) - last).total_seconds()
        limit = reconciler.interval * 2 + 30
        if age > limit:
            return ComponentHealth(
                ok=False,
                detail=f"last reconcile {int(age)}s ago (>{int(limit)}s; loop may be stuck)",
            )
        return ComponentHealth(ok=True, detail=f"last reconcile {int(age)}s ago")

    async def _compute_health() -> InstanceStatus:
        app_h = _app_health()

        watcher = reconciler.watcher
        if watcher is None:
            docker_h = ComponentHealth(ok=False, detail="docker watcher not initialised")
        else:
            d_ok, d_detail = await watcher.ping()
            docker_h = ComponentHealth(ok=d_ok, detail=d_detail)

        providers = reconciler.providers
        results = await asyncio.gather(
            *(p.check_health() for p in providers), return_exceptions=True
        )
        provider_health: dict[str, ComponentHealth] = {}
        for provider, res in zip(providers, results):
            if isinstance(res, Exception):
                provider_health[provider.name] = ComponentHealth(ok=False, detail=str(res))
            else:
                ok, detail = res
                provider_health[provider.name] = ComponentHealth(ok=ok, detail=detail)

        healthy = (
            app_h.ok
            and docker_h.ok
            and all(c.ok for c in provider_health.values())
        )

        return InstanceStatus(
            name=config.name,
            url="",
            healthy=healthy,
            record_count=len(reconciler.state),
            providers=reconciler.provider_names,
            last_reconcile=reconciler.last_reconcile,
            app=app_h,
            docker=docker_h,
            provider_health=provider_health,
        )

    async def _health_cached() -> InstanceStatus:
        now = time.monotonic()
        cached = _health_cache["value"]
        if cached is not None and (now - float(_health_cache["at"])) < _HEALTH_TTL:
            return cached  # type: ignore[return-value]
        async with _health_lock:
            now = time.monotonic()
            cached = _health_cache["value"]
            if cached is not None and (now - float(_health_cache["at"])) < _HEALTH_TTL:
                return cached  # type: ignore[return-value]
            value = await _compute_health()
            _health_cache["value"] = value
            _health_cache["at"] = time.monotonic()
            return value

    @app.get("/api/health", response_model=InstanceStatus)
    async def health() -> InstanceStatus:
        return await _health_cached()

    @app.get("/api/records", response_model=list[ContainerRecord])
    async def get_records(
        plugin: Annotated[Optional[str], Query(pattern=r"^[a-zA-Z0-9_-]+$")] = None,
        dns_status: Annotated[Optional[DNSVerificationStatus], Query()] = None,
    ) -> list[ContainerRecord]:
        records = reconciler.state
        if plugin is not None:
            records = [r for r in records if r.plugin == plugin]
        if dns_status is not None:
            records = [r for r in records if r.dns_status == dns_status]
        return records

    @app.get("/api/instances", response_model=list[RemoteInstanceInfo])
    async def get_instances() -> list[RemoteInstanceInfo]:
        return [
            RemoteInstanceInfo(
                name=inst.name,
                url=inst.url,
                insecure=inst.insecure,
                proxied=True,
            )
            for inst in config.instances
        ]

    async def _proxy_get(name: str, path: str) -> JSONResponse:
        inst = _remote_by_name.get(name)
        if inst is None:
            raise HTTPException(status_code=404, detail=f"Instance '{name}' not configured")
        try:
            async with httpx.AsyncClient(
                verify=not inst.insecure,
                timeout=8,
            ) as client:
                resp = await client.get(f"{inst.url}{path}")
                resp.raise_for_status()
                return JSONResponse(content=resp.json())
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"Instance '{name}' timed out")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Instance '{name}' returned {exc.response.status_code}")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Instance '{name}' unreachable: {exc}")

    @app.get("/api/instances/health")
    async def all_instances_health() -> JSONResponse:
        """Aggregated health across this instance and every connected peer, in one
        call. Each entry is {name, url, reachable, health|error}; unreachable peers
        report reachable=false with the error rather than failing the whole call."""
        async def one(inst) -> dict:
            try:
                async with httpx.AsyncClient(verify=not inst.insecure, timeout=8) as client:
                    resp = await client.get(f"{inst.url}/api/health")
                    resp.raise_for_status()
                    return {"name": inst.name, "url": inst.url, "reachable": True, "health": resp.json()}
            except Exception as exc:
                return {"name": inst.name, "url": inst.url, "reachable": False, "error": str(exc)}

        local = await _health_cached()
        peers = await asyncio.gather(*(one(inst) for inst in config.instances))
        return JSONResponse(
            content={
                "local": {"name": config.name, "reachable": True, "health": local.model_dump(mode="json")},
                "instances": list(peers),
            }
        )

    @app.get("/api/instances/{name}/records")
    async def get_instance_records(name: str) -> JSONResponse:
        return await _proxy_get(name, "/api/records")

    @app.get("/api/instances/{name}/health")
    async def get_instance_health(name: str) -> JSONResponse:
        return await _proxy_get(name, "/api/health")

    @app.post("/api/reconcile", status_code=202)
    async def trigger_reconcile() -> JSONResponse:
        await reconciler.trigger_reconcile()
        return JSONResponse({"status": "triggered"})

    @app.get("/", include_in_schema=False)
    async def serve_ui() -> FileResponse:
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="UI not found")
        return FileResponse(index)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app
