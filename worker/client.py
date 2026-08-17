"""
Async HTTP client to the gateway. All requests carry the worker bearer token.
"""
from __future__ import annotations
import logging
from typing import Any, Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import get_settings

log = logging.getLogger("v3cluster.worker.client")


class GatewayError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class GatewayClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(30.0, connect=10.0, read=300.0),
        )

    async def close(self):
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        try:
            r = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GatewayError(0, f"network: {e!r}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise GatewayError(r.status_code, detail)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r

    async def register_worker(self, payload: dict) -> dict:
        return await self._req("POST", "/api/v1/workers/register", json=payload)

    async def heartbeat(self, worker_id: str, payload: dict) -> dict:
        return await self._req("POST", f"/api/v1/workers/{worker_id}/heartbeat", json=payload)

    async def claim(self, worker_id: str) -> dict:
        return await self._req("POST", "/api/v1/jobs/claim", json={"worker_id": worker_id})

    async def progress(self, job_id: str, payload: dict) -> dict:
        return await self._req("POST", f"/api/v1/jobs/{job_id}/progress", json=payload)

    async def complete(self, job_id: str, payload: dict) -> dict:
        return await self._req("POST", f"/api/v1/jobs/{job_id}/complete", json=payload)

    async def download_file(self, file_id: str, dest_path: str) -> int:
        """Stream a file to disk. Returns bytes downloaded."""
        url = f"/api/v1/files/{file_id}"
        total = 0
        async with self._client.stream("GET", url) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise GatewayError(r.status_code, body.decode("utf-8", "replace"))
            with open(dest_path, "wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
        return total

    async def upload_output(self, file_path: str, original_name: str) -> dict:
        """Upload a file as a role=output. Returns {file_id, size_bytes, sha256, ...}."""
        with open(file_path, "rb") as f:
            files = {"upload": (original_name, f, "application/octet-stream")}
            data = {"role": "output"}
            return await self._req("POST", "/api/v1/files/upload", data=data, files=files)

    async def cancel_requested(self, job_id: str) -> bool:
        """Returns True if user requested cancel."""
        try:
            job = await self._req("GET", f"/api/v1/jobs/{job_id}")
        except GatewayError as e:
            if e.status == 404:
                return True
            raise
        return bool(job.get("cancel_requested", False))
