import asyncio
import logging
import sys

import uvicorn

from ext_dns.config import load_config
from ext_dns.docker_watcher import DockerWatcher
from ext_dns.providers import load_providers
from ext_dns.reconciler import Reconciler
from ext_dns.web.app import build_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


async def _run_with_restart(coro_fn, label: str, restart_delay: int = 5) -> None:
    while True:
        try:
            await coro_fn()
            log.warning("%s exited unexpectedly — restarting in %ds", label, restart_delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "%s crashed: %s — restarting in %ds",
                label, exc, restart_delay,
                exc_info=True,
            )
        await asyncio.sleep(restart_delay)


async def _main() -> None:
    config = load_config()
    providers = load_providers(config.plugins)
    reconciler = Reconciler(providers, interval=config.interval)
    watcher = DockerWatcher(on_state_change=reconciler.trigger_reconcile)
    reconciler.set_watcher(watcher)

    app = build_app(reconciler, config)
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
        _run_with_restart(reconciler.run, "reconciler"),
        _run_with_restart(watcher.watch, "watcher"),
        server.serve(),
    )


def main_sync() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
