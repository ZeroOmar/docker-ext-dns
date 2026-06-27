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


def _provider_traefik(pcfg, global_hostname: str | None) -> tuple[bool, str | None]:
    """Resolve a provider's Traefik integration to (enabled, hostname). Enabled by
    default; a provider opts out with `traefik: false` (or `traefik: {enabled: false}`)
    and may override the CNAME target with `traefik: {hostname: ...}`."""
    t = pcfg.get("traefik") if isinstance(pcfg, dict) else None
    if t is None:
        return True, global_hostname
    if isinstance(t, bool):
        return t, global_hostname
    if isinstance(t, dict):
        return bool(t.get("enabled", True)), t.get("hostname") or global_hostname
    return True, global_hostname


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
    reconciler = Reconciler(
        providers,
        interval=config.interval,
        change_concurrency=config.change_concurrency,
        change_delay=config.change_delay,
    )

    # Traefik integration is provider-independent: when globally enabled (the
    # default) it applies to every configured provider, unless a provider opts out
    # with `plugins.<name>.traefik: false`. The CNAME target is the global
    # `traefik.hostname` (auto-discovered if unset), overridable per provider.
    traefik_cfg: dict[str, dict] = {}
    if config.traefik.enabled:
        for plugin_name, pcfg in config.plugins.items():
            enabled, hostname = _provider_traefik(pcfg, config.traefik.hostname)
            if enabled:
                traefik_cfg[plugin_name] = {"hostname": hostname}

    watcher = DockerWatcher(
        on_state_change=reconciler.trigger_reconcile, traefik=traefik_cfg
    )
    reconciler.set_watcher(watcher)
    if traefik_cfg:
        log.info("Traefik integration enabled for providers: %s", list(traefik_cfg))
    else:
        log.info("Traefik integration disabled")

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
