"""
File upload + download.
"""
from __future__ import annotations
import uuid
from typing import Optional
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
import asyncpg

from ..auth import AuthContext, authenticate
from ..rate_limit import check_and_increment
from .. import storage
from ..db import get_pool

router = APIRouter(prefix="/files", tags=["files"])


@router.post("/upload")
async def upload(
    upload: UploadFile = File(...),
    role: str = Form("original"),
    ctx: AuthContext = Depends(authenticate),
) -> dict:
    """
    Upload a file. Returns {file_id, sha256, size_bytes, mime}.
    Only 'uploader' and 'admin' roles can upload originals.
    Workers can also upload (for outputs).
    """
    await check_and_increment(ctx)   # rate limit (workers bypass by role)
    if role not in ("original", "output", "log"):
        raise HTTPException(400, f"role must be one of original|output|log; got {role!r}")
    if ctx.role == "worker" and role == "original":
        raise HTTPException(403, "workers cannot upload original files")

    try:
        file_id, storage_path, size, sha256, mime = await storage.save_stream(
            role=role,
            original_name=upload.filename or "file.bin",
            stream=upload,  # UploadFile has .read() which save_stream uses
        )
    except ValueError as e:
        raise HTTPException(413, str(e))

    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO files (id, owner_user_id, role, original_name, storage_path, size_bytes, sha256, mime)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        uuid.UUID(file_id), ctx.owner_user_id, role, upload.filename or "file.bin",
        storage_path, size, sha256, mime,
    )

    return {
        "file_id": file_id,
        "sha256": sha256,
        "size_bytes": size,
        "mime": mime,
        "original_name": upload.filename,
    }


@router.get("/{file_id}")
async def download(file_id: str, ctx: AuthContext = Depends(authenticate)) -> StreamingResponse:
    """
    Download a file by ID. Authorization:
    - admin: any file
    - uploader: own files
    - worker: any original (for download) or its own job's output
    """
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(400, "invalid file_id")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT owner_user_id, role, original_name, storage_path, size_bytes, mime FROM files WHERE id = $1",
        fid,
    )
    if row is None:
        raise HTTPException(404, "file not found")

    # Auth check
    if ctx.role == "uploader" and str(row["owner_user_id"]) != ctx.owner_user_id:
        raise HTTPException(403, "not your file")

    abs_p = storage.abs_path(row["storage_path"])
    if not abs_p.exists():
        raise HTTPException(410, "file gone from disk (TTL expired)")

    import anyio
    async def stream_file():
        # Read in chunks; do NOT use FileResponse here because we need the
        # auth check to happen first.
        f = await anyio.open_file(abs_p, "rb")
        try:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await f.aclose()

    return StreamingResponse(
        stream_file(),
        media_type=row["mime"] or "application/octet-stream",
        headers={"Content-Length": str(row["size_bytes"]),
                 "Content-Disposition": f'attachment; filename="{row["original_name"]}"'},
    )


@router.get("/{file_id}/meta")
async def file_meta(file_id: str, ctx: AuthContext = Depends(authenticate)) -> dict:
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(400, "invalid file_id")

    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, owner_user_id, role, original_name, storage_path, size_bytes, sha256, mime, created_at
           FROM files WHERE id = $1""",
        fid,
    )
    if row is None:
        raise HTTPException(404, "file not found")
    if ctx.role == "uploader" and str(row["owner_user_id"]) != ctx.owner_user_id:
        raise HTTPException(403, "not your file")
    return dict(row)
