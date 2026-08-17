#!/usr/bin/env python3
"""
End-to-end smoke test for V3_cursor_Cluster.

Prereqs:
  - Postgres running with v3cluster DB
  - Schema applied (scripts/init_db.sql)
  - Gateway running (uvicorn gateway.app:app --port 8770)
  - 1 worker running (PYTHONPATH=. python -m worker.main)

Usage:
  python3 tests/test_smoke.py
  python3 tests/test_smoke.py --gateway http://localhost:8770

Asserts:
  1. Gateway /health returns ok
  2. Admin key can issue uploader + worker keys
  3. Uploader can upload 2 files
  4. Uploader can submit a TC01 job
  5. Worker claims + renders within 30s
  6. Output is valid MP4 with chroma key applied (top-left pixel != green)
  7. Rate limit eventually 429s after burst
"""
from __future__ import annotations
import argparse
import io
import json
import os
import struct
import subprocess
import sys
import time
from typing import Optional
import urllib.request
import urllib.error
import zlib


# --- Minimal PNG (so we don't need PIL in the test) ---

def make_png_rgb(width: int, height: int, rgb: tuple[int,int,int]) -> bytes:
    """Create a solid-color PNG without PIL."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b""
    for _ in range(height):
        raw += b"\x00"  # filter type none
        for _ in range(width):
            raw += struct.pack(">BBB", *rgb)
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def make_mp4_from_pngs(png_paths: list[str], out_path: str, fps: int = 30) -> None:
    """Run ffmpeg to stitch PNGs into a short MP4. Test fixture helper."""
    if not png_paths:
        raise RuntimeError("no pngs")
    # Create concat list
    list_path = out_path + ".list.txt"
    with open(list_path, "w") as f:
        for p in png_paths:
            f.write(f"file '{p}'\nduration 0.1\n")
        f.write(f"file '{png_paths[-1]}'\n")  # repeat last
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), out_path,
    ], check=True, capture_output=True)
    os.unlink(list_path)


def make_green_mp4(out_path: str, seconds: int = 2) -> int:
    """Use ffmpeg lavfi to create a solid-green MP4. Returns file size."""
    proc = subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=#00FF00:s=320x568:d={seconds}:r=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
    ], check=True, capture_output=True)
    return os.path.getsize(out_path)


def make_orange_mp4(out_path: str, seconds: int = 2) -> int:
    proc = subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=#FF6400:s=320x568:d={seconds}:r=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
    ], check=True, capture_output=True)
    return os.path.getsize(out_path)


def read_png_pixel(path: str, x: int, y: int) -> tuple[int,int,int]:
    """Tiny PNG decoder — just enough to read one pixel from a PNG we created."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("not a PNG")
    pos = 8
    width = height = 0
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos+4])[0]
        tag = data[pos+4:pos+8]
        chunk = data[pos+8:pos+8+length]
        if tag == b"IHDR":
            width, height, bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        pos += 8 + length + 4
        if tag == b"IEND":
            break
    raw = zlib.decompress(idat)
    # Each row: 1 filter byte + width*3 bytes (RGB)
    row_size = 1 + width*3
    row_start = y * row_size + 1
    px = raw[row_start + x*3 : row_start + x*3 + 3]
    return struct.unpack(">BBB", px)


# --- HTTP helpers ---

class Api:
    def __init__(self, base: str, admin_key: Optional[str] = None):
        self.base = base.rstrip("/")
        self.admin_key = admin_key

    def req(self, method: str, path: str, body=None, files=None, headers=None) -> dict:
        url = self.base + path
        hdrs = dict(headers or {})
        if body is not None and not files:
            hdrs["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        elif files:
            boundary = "----TestBoundary123"
            hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            data = b""
            for k, v in (body or {}).items():
                data += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, (filename, content) in (files or {}).items():
                data += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                data += content + b"\r\n"
            data += f"--{boundary}--\r\n".encode()
        else:
            data = None

        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        if self.admin_key and "Authorization" not in hdrs and "X-Admin-Key" not in hdrs:
            req.add_header("X-Admin-Key", self.admin_key)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                ct = r.headers.get("content-type", "")
                if ct.startswith("application/json"):
                    return {"status": r.status, "body": json.loads(r.read())}
                return {"status": r.status, "body": r.read()}
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = None
            return {"status": e.code, "body": body}


# --- Test steps ---

def step(name: str):
    def deco(fn):
        def wrap(*args, **kwargs):
            print(f"\n=== {name} ===")
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except AssertionError as e:
                print(f"  ❌ FAIL: {e}")
                raise
            dt = time.monotonic() - t0
            print(f"  ✅ OK ({dt:.2f}s)")
            return result
        return wrap
    return deco


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8770"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY"))
    parser.add_argument("--out-dir", default="/tmp/v3cluster-smoke")
    args = parser.parse_args()

    if not args.admin_key:
        print("ERROR: --admin-key or ADMIN_API_KEY required")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    api = Api(args.gateway, admin_key=args.admin_key)

    @step("1. Health check")
    def t1():
        r = api.req("GET", "/api/v1/health")
        assert r["status"] == 200, f"health failed: {r}"
        assert r["body"]["status"] == "ok", f"db not ok: {r['body']}"
        print(f"  gateway: {r['body']['service']} v{r['body']['version']}")
        print(f"  db counts: pending={r['body'].get('pending',0)} running={r['body'].get('running',0)} succeeded={r['body'].get('succeeded',0)}")

    @step("2. Issue uploader + worker keys")
    def t2():
        nonlocal_uploader = {}
        # uploader
        r = api.req("POST", "/api/v1/admin/users", {"email": "smoke@test", "plan": "pro"})
        assert r["status"] in (200, 409), f"create user: {r}"
        r = api.req("POST", "/api/v1/admin/keys", {
            "role": "uploader", "label": "smoke", "owner_email": "smoke@test"
        })
        assert r["status"] == 200, f"issue uploader: {r}"
        nonlocal_uploader["key"] = r["body"]["plaintext"]
        # worker
        r = api.req("POST", "/api/v1/admin/keys", {
            "role": "worker", "label": "smoke", "worker_id": "smoke-worker"
        })
        assert r["status"] == 200, f"issue worker: {r}"
        print(f"  uploader key: {nonlocal_uploader['key'][:12]}…")
        print(f"  worker key:   {r['body']['plaintext'][:12]}…")
        return nonlocal_uploader["key"]

    @step("3. Create test fixtures (green product + orange bg)")
    def t3():
        prod = os.path.join(args.out_dir, "product.mp4")
        bg = os.path.join(args.out_dir, "bg.mp4")
        n1 = make_green_mp4(prod, seconds=2)
        n2 = make_orange_mp4(bg, seconds=2)
        assert n1 > 0 and n2 > 0, "fixture creation failed"
        print(f"  product.mp4: {n1} bytes (green)")
        print(f"  bg.mp4:      {n2} bytes (orange)")

    @step("4. Upload product + bg via API")
    def t4(key: str):
        with open(os.path.join(args.out_dir, "product.mp4"), "rb") as f:
            prod_bytes = f.read()
        with open(os.path.join(args.out_dir, "bg.mp4"), "rb") as f:
            bg_bytes = f.read()
        u = Api(args.gateway)
        r = u.req("POST", "/api/v1/files/upload",
                  body={"role": "original"},
                  files={"upload": ("product.mp4", prod_bytes)},
                  headers={"Authorization": f"Bearer {key}"})
        assert r["status"] == 200, f"upload product: {r}"
        prod_id = r["body"]["file_id"]
        r = u.req("POST", "/api/v1/files/upload",
                  body={"role": "original"},
                  files={"upload": ("bg.mp4", bg_bytes)},
                  headers={"Authorization": f"Bearer {key}"})
        assert r["status"] == 200, f"upload bg: {r}"
        bg_id = r["body"]["file_id"]
        print(f"  product: {prod_id}")
        print(f"  bg:      {bg_id}")
        return prod_id, bg_id

    @step("5. Submit TC01 job")
    def t5(key: str, prod_id: str, bg_id: str):
        u = Api(args.gateway)
        r = u.req("POST", "/api/v1/jobs/render", {
            "tc": "tc01",
            "input_file_ids": [prod_id, bg_id],
            "settings": {
                "width": 320, "height": 568, "fps": 30,
                "encoder": "libx264", "bitrate": "1500k",
                "key_color": "#00FF00", "similarity": 0.4, "blend": 0.1, "despill": 0.4,
            },
        }, headers={"Authorization": f"Bearer {key}"})
        assert r["status"] == 200, f"submit job: {r}"
        job_id = r["body"]["job_id"]
        print(f"  job_id: {job_id}")
        return job_id

    @step("6. Wait for worker to render (up to 60s)")
    def t6(key: str, job_id: str):
        u = Api(args.gateway)
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            r = u.req("GET", f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {key}"})
            if r["status"] != 200:
                time.sleep(1); continue
            j = r["body"]
            if j["status"] != last:
                print(f"  [{int(time.monotonic())}] status: {j['status']} progress: {j['progress_pct']:.0f}% worker: {j.get('claimed_by_worker_id')}")
                last = j["status"]
            if j["status"] in ("succeeded", "failed", "cancelled"):
                assert j["status"] == "succeeded", f"job ended in {j['status']}: {j.get('error_text')}"
                return j
            time.sleep(1)
        raise AssertionError("timed out waiting for job to finish")

    @step("7. Download output and verify chroma applied")
    def t7(key: str, job: dict):
        u = Api(args.gateway)
        out_id = job["output_file_id"]
        r = u.req("GET", f"/api/v1/files/{out_id}", headers={"Authorization": f"Bearer {key}"})
        assert r["status"] == 200, f"download output: {r}"
        out_path = os.path.join(args.out_dir, "output.mp4")
        with open(out_path, "wb") as f:
            f.write(r["body"])
        # ffprobe to verify
        proc = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_name",
            "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1", out_path,
        ], capture_output=True, text=True, check=True)
        print(f"  ffprobe:\n{proc.stdout.strip()}")
        # Extract first frame
        frame = os.path.join(args.out_dir, "frame.png")
        subprocess.run([
            "ffmpeg", "-y", "-i", out_path, "-ss", "0", "-frames:v", "1", frame,
        ], check=True, capture_output=True)
        # Check center pixel — should be orange (the bg, not green)
        pixel = read_png_pixel(frame, 160, 284)
        is_green = pixel[1] > 200 and pixel[0] < 50 and pixel[2] < 50
        is_orange = pixel[0] > 200 and pixel[1] < 150 and pixel[2] < 50
        assert not is_green, f"chroma failed — center pixel is still GREEN: {pixel}"
        assert is_orange, f"unexpected center pixel (expected orange): {pixel}"
        print(f"  center pixel: {pixel} ← ORANGE (chroma removed green ✓)")

    @step("8. Rate limit: burst 70 upload requests in 1s should hit 429")
    def t8(key: str):
        u = Api(args.gateway)
        n_200 = n_429 = 0
        # /files/upload is the rate-limited endpoint. Send 70 tiny 1-byte files.
        for i in range(70):
            r = u.req("POST", "/api/v1/files/upload",
                      body={"role": "log"},
                      files={"upload": ("rl.txt", b"x")},
                      headers={"Authorization": f"Bearer {key}"})
            if r["status"] == 200: n_200 += 1
            elif r["status"] == 429: n_429 += 1
        print(f"  200s: {n_200}  429s: {n_429}")
        assert n_429 > 0, f"rate limit never triggered (limit may be too high or upload bypasses)"

    # Run
    t1()
    uk = t2()
    t3()
    prod_id, bg_id = t4(uk)
    job_id = t5(uk, prod_id, bg_id)
    job = t6(uk, job_id)
    t7(uk, job)
    t8(uk)

    print("\n" + "="*60)
    print("ALL 8 STEPS PASSED ✅")
    print("="*60)


if __name__ == "__main__":
    main()
