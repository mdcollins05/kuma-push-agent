import os
import pathlib
import secrets
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_app_version() -> str:
    """Resolve the runtime version. Package metadata is the source of truth; env var
    overrides for local hot-reload or one-off testing; falls back to 'dev'."""
    override = os.environ.get("APP_VERSION")
    if override:
        return override
    try:
        return _pkg_version("kuma-push-agent")
    except PackageNotFoundError:
        return "dev"


APP_VERSION: str = _resolve_app_version()


class Settings(BaseSettings):
    DATA_DIR: str = "/data"
    CONFIG_DIR: str = "/config"

    model_config = SettingsConfigDict(env_file=".env")

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.DATA_DIR}/kuma_push_agent.db"

    @property
    def seed_file(self) -> str:
        return f"{self.CONFIG_DIR}/monitors.yaml"

    @property
    def session_secret(self) -> str:
        secret_file = pathlib.Path(self.DATA_DIR) / ".session_secret"
        if secret_file.exists():
            return secret_file.read_text().strip()
        secret = secrets.token_hex(32)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret)
        return secret


settings = Settings()
