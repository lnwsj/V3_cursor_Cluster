"""
ffmpeg subprocess wrapper with progress parsing + cancel check.
"""
from __future__ import annotations
import os
import re
import time
import asyncio
import logging
import signal
from pathlib import Path
from typing import Callable, Awaitable, Optional

log = logging.getLogger("v3cluster.worker.runner")


# Match "frame=  120 fps=30 q=28.0 size=    1024kB time=00:00:04.00 bitrate=..."  (ffmpeg -stats)
_RE_FRAME = re.compile(r"frame=\s*(\d+)")
_RE_TIME  = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
_RE_DUR   = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def _hms_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


async def run_ffmpeg(
    ffmpeg_bin: str,
    args: list[str],
    duration_sec: float,
    on_progress: Callable[[float, str], Awaitable[None]],
    on_cancel_check: Callable[[], Awaitable[bool]],
    progress_interval_sec: float = 2.0,
) -> tuple[int, str, str]:
    """
    Run ffmpeg as a subprocess. Periodically parse stderr for progress, call
    on_progress(percent, log_line). Call on_cancel_check() each tick; if True,
    kill the subprocess.

    Returns (returncode, log_text, error_text).
    """
    cmd = [ffmpeg_bin] + args
    log.info("ffmpeg cmd: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    log_lines: list[str] = []
    last_progress_call = 0.0
    last_percent = 0.0
    duration_known = duration_sec > 0
    duration_str = ""

    async def _read_stderr():
        while True:
            line = await proc.stderr.readline()  # type: ignore[union-attr]
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\n")
            log_lines.append(text)

            # Try to parse duration first time
            nonlocal duration_known, duration_str
            if not duration_known:
                m = _RE_DUR.search(text)
                if m:
                    duration_str = m.group(0)
                    # We could parse it but we trust caller's duration_sec
            yield text

    async def _consume():
        async for text in _read_stderr():
            # Parse progress
            nonlocal last_percent, last_progress_call
            now = time.monotonic()
            if duration_known and (now - last_progress_call) >= progress_interval_sec:
                m_time = _RE_TIME.search(text)
                m_frame = _RE_FRAME.search(text)
                if m_time:
                    cur = _hms_to_seconds(*m_time.groups())
                    pct = min(100.0, max(last_percent, (cur / duration_sec) * 100.0))
                    if pct > last_percent + 0.5 or pct >= 99.5:
                        last_percent = pct
                        try:
                            await on_progress(pct, text[:200])
                        except Exception as e:
                            log.warning("on_progress error: %r", e)
                        last_progress_call = now

    consumer = asyncio.create_task(_consume())
    cancelled = False
    error_text = ""

    while True:
        if await on_cancel_check():
            cancelled = True
            log.warning("cancel requested — killing ffmpeg (pid %s)", proc.pid)
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            break

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=1.0)
            break
        except asyncio.TimeoutError:
            continue

    try:
        await asyncio.wait_for(consumer, timeout=5.0)
    except asyncio.TimeoutError:
        consumer.cancel()

    log_text = "\n".join(log_lines[-200:])  # cap at 200 lines
    if cancelled:
        error_text = "cancelled by user"
    elif rc != 0:
        error_text = f"ffmpeg exit code {rc}; last log lines: {log_text[-500:]}"

    return rc, log_text, error_text
