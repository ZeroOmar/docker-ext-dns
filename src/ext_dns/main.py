import asyncio
import logging

import uvicorn

from ext_dns.config import load_config
from ext_dns.docker_watcher import DockerWatcher
from ext_dns.providers import load_providers
from ext_dns.reconciler import Reconciler
from ext_dns.web.app import build_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def _main() -> None:
    config = load_config()
    providers = load_providers(config.plugins)
    reconciler = Reconciler(providers, interval=config.interval)
    watcher = DockerWatcher(on_state_change=reconciler.trigger_reconcile)
    reconciler.set_watcher(watcher)

    app = build_app(reconciler)
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.web.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(server_config)

    log.info(
        "Starting docker-ext-dns on port %d with providers: %s",
        config.web.port,
        list(config.plugins.keys()) or ["none"],
    )

    await asyncio.gather(
        reconciler.run(),
        watcher.watch(),
        server.serve(),
    )


def main_sync() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
