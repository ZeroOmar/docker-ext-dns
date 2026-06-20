from ext_dns.providers.base import DNSProvider
from ext_dns.providers.pihole import PiholeProvider

_REGISTRY: dict[str, type[DNSProvider]] = {
    "pihole": PiholeProvider,
}


def load_providers(plugins_config: dict[str, dict]) -> list[DNSProvider]:
    providers = []
    for name, cfg in plugins_config.items():
        if name not in _REGISTRY:
            raise KeyError(f"Unknown DNS provider: '{name}'. Available: {list(_REGISTRY)}")
        providers.append(_REGISTRY[name](cfg))
    return providers
