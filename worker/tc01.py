"""
TC01 render — download inputs, run ffmpeg, upload output.

Input file order (from gateway):
- 0: product (foreground)
- 1: background
- 2: audio (optional — if settings has audio_file_id)

Output: 1 MP4 with composite video + (audio from product OR from input[2])
"""
from __future__ import annotations
import os
import json
import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable, Optional

from .client import GatewayClient, GatewayError
from .config import Settings
from .runner import run_ffmpeg
from shared.renderer.settings import TC01Settings
from shared.renderer.tc01_chroma import build_ffmpeg_args

log = logging.getLogger("v3cluster.worker.tc01")


async def ffprobe_duration(client: GatewayClient, file_id: str, dest: str) -> float:
    """Download file to dest, run ffprobe to get duration. Returns seconds."""
    await client.download_file(file_id, dest)
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", dest,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await p.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def render_tc01(
    client: GatewayClient,
    settings: Settings,
    job: dict,
    on_progress: Callable[[float, str], Awaitable[None]],
    on_cancel_check: Callable[[], Awaitable[bool]],
) -> dict:
    """
    Render a TC01 job. Returns a dict suitable for /jobs/{id}/complete.
    """
    job_id = job["id"]
    input_ids: list[str] = job.get("input_file_ids", [])
    if not input_ids:
        return {"error": "no input files", "log": ""}

    workdir = Path(settings.work_dir) / "jobs" / job_id
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. Download inputs
    await on_progress(1.0, f"[worker] downloading {len(input_ids)} input files")
    product_path = workdir / f"input0{Path(input_ids[0]).suffix or '.mp4'}"
    await ffprobe_duration(client, input_ids[0], str(product_path))  # downloads + probes

    background_path = workdir / f"input1{Path(input_ids[1]).suffix or '.mp4'}"
    await ffprobe_duration(client, input_ids[1], str(background_path))

    audio_path = None
    if len(input_ids) >= 3:
        audio_path = workdir / f"input2{Path(input_ids[2]).suffix or '.mp3'}"
        await client.download_file(input_ids[2], str(audio_path))

    # 2. Get product duration (re-probe is fine, file is local now)
    p = await asyncio.create_subprocess_exec(
        settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(product_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await p.communicate()
    try:
        product_dur = float(out.decode().strip())
    except Exception:
        product_dur = 0.0

    # 3. Build ffmpeg args
    try:
        tc01 = TC01Settings(**job["settings"])
    except Exception as e:
        return {"error": f"invalid settings: {e}", "log": ""}

    output_path = workdir / f"output.mp4"
    ffmpeg_args = build_ffmpeg_args(
        settings=tc01,
        product_path=str(product_path),
        background_path=str(background_path),
        audio_path=str(audio_path) if audio_path else None,
        output_path=str(output_path),
    )

    # 4. Run ffmpeg
    await on_progress(2.0, f"[worker] starting ffmpeg (product={product_dur:.2f}s)")

    rc, log_text, error_text = await run_ffmpeg(
        ffmpeg_bin=settings.ffmpeg_bin,
        args=ffmpeg_args,
        duration_sec=product_dur,
        on_progress=on_progress,
        on_cancel_check=on_cancel_check,
        progress_interval_sec=2.0,
    )

    if rc != 0 or not output_path.exists() or output_path.stat().st_size < 1000:
        return {"error": error_text or "ffmpeg produced no output", "log": log_text}

    # 5. Upload output
    await on_progress(99.0, "[worker] uploading output")
    out_meta = await client.upload_output(str(output_path), f"{job_id}.mp4")
    out_meta["log"] = log_text
    return out_meta
