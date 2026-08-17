"""
TC01 (single-product chroma key) — ffmpeg arg builder.

Output: composite foreground (product) over background, with chroma key + despill.
Mirrors the filter chain from V3_cursor (WebApp v1.0.0.20 B320 GPU variant +
CPU fallback), but simplified for v1 (no transpose_cuda, no pre-scale; just
straight chroma + scale + overlay + despill).

Used by:
- gateway: validate settings + build preview (optional)
- worker:   execute render
"""
from __future__ import annotations
from typing import List
from .settings import TC01Settings


def build_ffmpeg_args(
    settings: TC01Settings,
    product_path: str,
    background_path: str,
    audio_path: str | None,
    output_path: str,
) -> List[str]:
    """
    Build the full ffmpeg arg list for TC01.
    Caller is responsible for prepending the ffmpeg binary path.
    """
    s = settings.with_encoder_defaults()
    enc = s.encoder
    preset = s.preset or "medium"

    args: List[str] = [
        "-y",                                    # overwrite output
        "-hide_banner", "-loglevel", "info", "-stats",

        # --- Input 0: product (foreground) — may or may not have audio
        "-i", product_path,
    ]

    # --- Input 1: background
    args += ["-i", background_path]

    # --- Input 2: audio (optional) — if absent, stream-copy from product
    if audio_path:
        args += ["-i", audio_path]
        audio_map = ["-map", "2:a:0?"]           # optional
    else:
        audio_map = ["-map", "0:a:0?"]           # take from product if present

    # --- Filter graph ---
    # CPU path (works on all platforms). NVENC output; chroma done in CPU.
    # For GPU chroma chain, see V3_cursor green_render.build_render_command.
    color = s.key_color
    sim   = f"{s.similarity:.3f}"
    blend = f"{s.blend:.3f}"
    despill = f"{s.despill:.3f}"

    if audio_path:
        # 3 inputs: 0=product, 1=bg, 2=audio
        filter_complex = (
            f"[0:v]scale={s.width}:{s.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format={s.pix_fmt},"
            f"chromakey=color={color}:similarity={sim}:blend={blend}[fg];"
            f"[1:v]scale={s.width}:{s.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format={s.pix_fmt}[bg];"
            f"[bg][fg]overlay=eof_action=pass[ov];"
            f"[ov]despill=type=green:mix={despill}[vout]"
        )
    else:
        filter_complex = (
            f"[0:v]scale={s.width}:{s.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format={s.pix_fmt},"
            f"chromakey=color={color}:similarity={sim}:blend={blend}[fg];"
            f"[1:v]scale={s.width}:{s.height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format={s.pix_fmt}[bg];"
            f"[bg][fg]overlay=eof_action=pass[ov];"
            f"[ov]despill=type=green:mix={despill}[vout]"
        )

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ] + audio_map + [
        "-c:v", enc, "-preset", preset, "-b:v", s.bitrate, "-pix_fmt", s.pix_fmt,
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]
    return args


def estimate_duration_ms(settings: TC01Settings, input_seconds: float) -> int:
    """Rough estimate for UI display. Actual time is much more dependent on
    encoder/preset/disk than on input length, so this is just a placeholder."""
    return int(input_seconds * 1000 * 0.25)  # ~4x realtime on GPU
