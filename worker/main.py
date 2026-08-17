"""
Worker main loop.

Lifecycle:
  1. On boot: register with gateway (idempotent)
  2. Spawn heartbeat task (every WORKER_HEARTBEAT_INTERVAL)
  3. Spawn N parallel "render slots" (concurrency = max_parallel)
     Each slot polls /jobs/claim every WORKER_POLL_INTERVAL, picks up a job,
     runs TC01, reports progress + completion, loop
  4. On SIGTERM/SIGINT: graceful shutdown (let in-flight jobs finish)

Capacity: the gateway tracks current_jobs; worker only claims when
current_jobs < max_parallel.
"""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from .config import get_settings, Settings
from .client import GatewayClient, GatewayError
from .tc01 import render_tc01

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("v3cluster.worker")


class Worker:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = GatewayClient(settings.gateway_url, settings.worker_api_key)
        self._current_jobs: dict[str, asyncio.Task] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # current_jobs counter used in heartbeats
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        s = self.s
        # 1. Register
        try:
            await self.client.register_worker({
                "worker_id": s.worker_id,
                "label": s.worker_label,
                "gpu_label": s.gpu_label,
                "max_parallel": s.max_parallel,
                "tc_filter": ["tc01"],
            })
            log.info("Registered with gateway: %s (max_parallel=%d)", s.worker_id, s.max_parallel)
        except GatewayError as e:
            log.error("register failed: %s", e)
            raise

        # 2. Heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 3. Render slot pool
        log.info("Starting %d render slot(s)", s.max_parallel)
        slot_tasks = [
            asyncio.create_task(self._slot_loop(i))
            for i in range(s.max_parallel)
        ]

        # 4. Wait for stop
        await self._stop.wait()

        log.info("Stopping — waiting for in-flight jobs to finish (max 30s)")
        for t in slot_tasks:
            t.cancel()
        await asyncio.gather(*slot_tasks, return_exceptions=True)
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await self.client.close()
        log.info("Worker stopped cleanly")

    def request_stop(self) -> None:
        self._stop.set()

    # ---------- Heartbeat ----------

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._lock:
                    current = self._in_flight
                status = "busy" if current > 0 else "online"
                await self.client.heartbeat(self.s.worker_id, {
                    "current_jobs": current,
                    "status": status,
                })
            except Exception as e:
                log.warning("heartbeat failed: %r", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    # ---------- Render slot ----------

    async def _slot_loop(self, slot_id: int) -> None:
        log.debug("slot %d: started", slot_id)
        while not self._stop.is_set():
            try:
                # Try to claim
                resp = await self.client.claim(self.s.worker_id)
            except GatewayError as e:
                if e.status == 401:
                    log.error("auth failed — re-register? %s", e)
                    await asyncio.sleep(5)
                    continue
                log.warning("claim error: %s — backing off", e)
                await asyncio.sleep(min(10, self.s.poll_interval * 3))
                continue
            except Exception as e:
                log.warning("claim exception: %r", e)
                await asyncio.sleep(5)
                continue

            job = resp.get("job")
            if job is None:
                # Nothing to do — back off
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue

            # Got a job — spawn a task so the slot can immediately try to claim another
            job_id = job["id"]
            log.info("slot %d: claimed job %s (tc=%s)", slot_id, job_id, job.get("tc"))
            task = asyncio.create_task(self._run_job(slot_id, job))
            self._current_jobs[job_id] = task

    async def _run_job(self, slot_id: int, job: dict) -> None:
        job_id = job["id"]
        async with self._lock:
            self._in_flight += 1
        try:
            tc = job.get("tc", "tc01")
            last_ping_t = 0.0

            async def on_progress(pct: float, line: str) -> None:
                nonlocal last_ping_t
                now = time.monotonic()
                if now - last_ping_t >= self.s.progress_interval:
                    try:
                        await self.client.progress(job_id, {
                            "progress_pct": pct,
                            "log_append": line,
                        })
                    except Exception as e:
                        log.warning("progress report failed: %r", e)
                    last_ping_t = now

            async def on_cancel_check() -> bool:
                try:
                    return await self.client.cancel_requested(job_id)
                except Exception as e:
                    log.warning("cancel check failed: %r", e)
                    return False

            # Render
            try:
                if tc == "tc01":
                    result = await render_tc01(self.client, self.s, job, on_progress, on_cancel_check)
                else:
                    result = {"error": f"unknown TC: {tc}", "log": ""}
            except Exception as e:
                log.exception("job %s crashed", job_id)
                result = {"error": f"worker exception: {e!r}", "log": ""}

            # Report completion
            try:
                if "error" in result:
                    await self.client.complete(job_id, {
                        "output_file_id": None,
                        "error": result["error"],
                        "log_append": result.get("log", ""),
                    })
                    log.warning("job %s FAILED: %s", job_id, result["error"][:200])
                else:
                    await self.client.complete(job_id, {
                        "output_file_id": result.get("file_id"),
                        "log_append": result.get("log", "")[-2000:],
                    })
                    log.info("job %s SUCCEEDED (output=%s, %d bytes)",
                             job_id, result.get("file_id"), result.get("size_bytes", 0))
            except Exception as e:
                log.error("complete-report failed for %s: %r", job_id, e)
        finally:
            async with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
            self._current_jobs.pop(job_id, None)


async def _amain() -> None:
    s = get_settings()
    log.info("V3 Cluster Worker starting")
    log.info("  worker_id=%s label=%s", s.worker_id, s.worker_label)
    log.info("  gateway=%s", s.gateway_url)
    log.info("  work_dir=%s", s.work_dir)
    log.info("  max_parallel=%d  ffmpeg=%s", s.max_parallel, s.ffmpeg_bin)

    Path(s.work_dir).mkdir(parents=True, exist_ok=True)

    worker = Worker(s)

    loop = asyncio.get_event_loop()

    def _on_signal():
        log.info("Signal received, stopping...")
        worker.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    await worker.start()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
