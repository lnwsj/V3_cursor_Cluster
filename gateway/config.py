"""
Gateway config — loaded from env at startup.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8770
    gateway_public_url: str = "http://localhost:8770"

    admin_api_key: str
    database_url: str = "postgresql://v3cluster:v3cluster@127.0.0.1:5432/v3cluster"

    storage_root: str = "/opt/v3-cluster/storage"

    rate_limit_per_minute: int = 60

    trust_forwarded_headers: bool = False

    # Upload limits
    max_upload_mb: int = 1024  # 1GB per file

    # Job lease — if worker doesn't ping within this, job is reaped
    worker_lease_seconds: int = 120

    # Job retention for completed/failed jobs (days)
    job_retention_days: int = 7


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
