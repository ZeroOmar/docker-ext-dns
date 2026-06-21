from pathlib import Path
from typing import Annotated, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ext_dns.config import AppConfig
from ext_dns.models import (
    ContainerRecord,
    DNSVerificationStatus,
    InstanceStatus,
    RemoteInstanceInfo,
)

_STATIC_DIR = Path(__file__).parent / "static"


def build_app(reconciler, config: AppConfig) -> FastAPI:
    app = FastAPI(title="docker-ext-dns", docs_url=None, redoc_url=None)

    _remote_by_name = {inst.name: inst for inst in config.instances}

    @app.get("/api/health", response_model=InstanceStatus)
    async def health() -> InstanceStatus:
        records = reconciler.state
        return InstanceStatus(
            url="",
            healthy=True,
            record_count=len(records),
            providers=reconciler.provider_names,
            last_reconcile=reconciler.last_reconcile,
        )

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
