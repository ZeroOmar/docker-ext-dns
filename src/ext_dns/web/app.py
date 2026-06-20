from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ext_dns.models import ContainerRecord, DNSVerificationStatus, InstanceStatus

_STATIC_DIR = Path(__file__).parent / "static"


def build_app(reconciler) -> FastAPI:
    app = FastAPI(title="docker-ext-dns", docs_url=None, redoc_url=None)

    @app.get("/api/health", response_model=InstanceStatus)
    async def health() -> InstanceStatus:
        records = reconciler.state
        healthy = True
        return InstanceStatus(
            url="",
            healthy=healthy,
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

    @app.get("/api/instances", response_model=list[InstanceStatus])
    async def get_instances() -> list[InstanceStatus]:
        records = reconciler.state
        return [
            InstanceStatus(
                url="",
                healthy=True,
                record_count=len(records),
                providers=reconciler.provider_names,
                last_reconcile=reconciler.last_reconcile,
            )
        ]

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
