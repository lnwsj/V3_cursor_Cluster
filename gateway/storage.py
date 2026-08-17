"""
File storage — abstracts originals + outputs under STORAGE_ROOT.
Each file gets a UUID, stored on disk at storage/<role>/<uuid>.<ext>.
"""
from __future__ import annotations
import os
import uuid
import hashlib
import mimetypes
import aiofiles
from pathlib import Path
from typing import Optional, Tuple

from .config import get_settings


def _root() -> Path:
    return Path(get_settings().storage_root).expanduser().resolve()


def _abs_path(storage_path: str) -> Path:
    # safety: storage_path must be relative, no ..
    p = (_root() / storage_path).resolve()
    if not str(p).startswith(str(_root())):
        raise ValueError(f"path escapes storage root: {storage_path}")
    return p


def safe_ext(original_name: str) -> str:
    ext = Path(original_name).suffix.lower()
    if not ext or len(ext) > 8:
        return ".bin"
    if not all(c.isalnum() or c in "._-" for c in ext):
        return ".bin"
    return ext


async def save_stream(
    role: str,            # 'original' | 'output' | 'log'
    original_name: str,
    stream,
    max_size_mb: Optional[int] = None,
) -> Tuple[str, str, int, str]:
    """
    Stream a file to disk, return (file_id, storage_path, size_bytes, sha256).
    Raises if file exceeds max_size_mb.
    """
    s = get_settings()
    max_bytes = (max_size_mb or s.max_upload_mb) * 1024 * 1024
    ext = safe_ext(original_name)
    file_id = str(uuid.uuid4())
    storage_path = f"{role}/{file_id}{ext}"
    abs_path = _abs_path(storage_path)
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    total = 0
    async with aiofiles.open(abs_path, "wb") as f:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                # Clean up partial
                try:
                    abs_path.unlink()
                except Exception:
                    pass
                raise ValueError(f"file exceeds {max_size_mb or s.max_upload_mb} MB")
            h.update(chunk)
            await f.write(chunk)

    mime, _ = mimetypes.guess_type(original_name)
    return file_id, storage_path, total, h.hexdigest(), (mime or "application/octet-stream")


def open_read(storage_path: str):
    """Open file for reading. Returns a file-like object."""
    return open(_abs_path(storage_path), "rb")


def abs_path(storage_path: str) -> Path:
    return _abs_path(storage_path)


def init_root() -> None:
    _root().mkdir(parents=True, exist_ok=True)
    (_root() / "original").mkdir(exist_ok=True)
    (_root() / "output").mkdir(exist_ok=True)
    (_root() / "log").mkdir(exist_ok=True)
