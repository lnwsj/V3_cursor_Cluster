"""
Worker config — loaded from env at startup.
"""
from __future__ import annotations
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("WORKER_ENV_FILE", "/etc/v3cluster/worker.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gateway_url: str = "http://localhost:8770"

    worker_api_key: str
    worker_id:      str
    worker_label:   str = "V3 Cluster Worker"

    work_dir: str = "/opt/v3-cluster/worker"

    max_parallel: int = 1

    heartbeat_interval: int = 10     # seconds
    poll_interval:      int = 3      # seconds
    progress_interval:  int = 5      # seconds between progress pings

    gpu_label: str = "GPU"
    ffmpeg_bin:  str = "/usr/bin/ffmpeg"
    ffprobe_bin: str = "/usr/bin/ffprobe"

    # Auto-detect on startup (writes to workers.gpu_label in DB)
    auto_detect_gpu: bool = True

    # Lease renewal: heartbeat also extends job lease
    lease_renew_threshold_sec: int = 60


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
