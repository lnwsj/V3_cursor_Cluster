"""
Shared settings models for renderers. Used by both gateway (for validation) and
worker (for execution).
"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
import re

# Encoder whitelist — keep in sync with ffmpeg_runner
ENCODERS = {
    "libx264":         {"platform": "cpu",  "default_preset": "medium"},
    "libx265":         {"platform": "cpu",  "default_preset": "medium"},
    "h264_nvenc":      {"platform": "nvenc","default_preset": "p4"},
    "hevc_nvenc":      {"platform": "nvenc","default_preset": "p4"},
    "h264_qsv":        {"platform": "qsv",  "default_preset": "medium"},
    "hevc_qsv":        {"platform": "qsv",  "default_preset": "medium"},
    "h264_videotoolbox":{"platform": "vt",  "default_preset": "medium"},
    "hevc_videotoolbox":{"platform": "vt",  "default_preset": "medium"},
}

EncoderName = Literal[
    "libx264","libx265",
    "h264_nvenc","hevc_nvenc",
    "h264_qsv","hevc_qsv",
    "h264_videotoolbox","hevc_videotoolbox",
]


def _hex_color(v: str) -> str:
    """Normalize hex color to 0xRRGGBB (with 0x prefix). Accepts #RRGGBB, RRGGBB, 0xRRGGBB."""
    s = v.strip().lstrip("#").lower()
    if s.startswith("0x"):
        s = s[2:]
    if not re.fullmatch(r"[0-9a-f]{6}", s):
        raise ValueError(f"key_color must be 6-digit hex, got {v!r}")
    return "0x" + s.upper()


class TC01Settings(BaseModel):
    """Chroma key (green screen) settings for a single product."""
    # Output geometry
    width:  int = Field(1080, ge=64, le=3840)
    height: int = Field(1920, ge=64, le=3840)
    fps:    int = Field(30,   ge=1,  le=120)

    # Encoder
    encoder: EncoderName = "h264_nvenc"
    preset:  Optional[str] = None           # auto-fill from encoder default
    bitrate: str = Field("6000k", pattern=r"^\d+[kKmM]?$")
    pix_fmt: str = Field("yuv420p")

    # Chroma
    key_color:  str = Field("0x00FF00")     # 0xRRGGBB or #RRGGBB
    similarity: float = Field(0.29, ge=0.0, le=1.0)
    blend:      float = Field(0.04, ge=0.0, le=1.0)
    despill:    float = Field(0.32, ge=0.0, le=1.0)

    # Input roles (set by gateway from input_file_ids order)
    # 0 = product (foreground), 1 = background, 2 = audio (optional)
    # Worker re-binds by index in input_file_ids.

    @field_validator("key_color")
    @classmethod
    def _v_color(cls, v: str) -> str:
        return _hex_color(v)

    @field_validator("width","height")
    @classmethod
    def _v_even(cls, v: int) -> int:
        if v % 2 != 0:
            raise ValueError(f"must be even (got {v})")
        return v

    def with_encoder_defaults(self) -> "TC01Settings":
        if self.preset is None:
            self.preset = ENCODERS[self.encoder]["default_preset"]
        return self
