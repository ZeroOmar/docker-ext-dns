import os

import yaml
from pydantic import BaseModel, Field


class WebConfig(BaseModel):
    port: int = 8080


class RemoteInstanceConfig(BaseModel):
    name: str
    url: str
    insecure: bool = False


class AppConfig(BaseModel):
    interval: int = Field(30, ge=5)
    plugins: dict[str, dict] = Field(default_factory=dict)
    web: WebConfig = Field(default_factory=WebConfig)
    instances: list[RemoteInstanceConfig] = Field(default_factory=list)
    # Throttle how fast record changes are applied so a large diff does not
    # overload the DNS backend (e.g. Pi-hole).
    change_concurrency: int = Field(2, ge=1)  # max simultaneous change operations
    change_delay: float = Field(0.0, ge=0)  # seconds to pause after each operation


def load_config() -> AppConfig:
    raw = os.environ.get("EXT_DNS_CONFIG")
    if not raw:
        raise ValueError("EXT_DNS_CONFIG environment variable is not set")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("EXT_DNS_CONFIG must be a YAML mapping")
    return AppConfig.model_validate(data)
