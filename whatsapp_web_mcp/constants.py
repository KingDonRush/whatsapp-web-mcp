from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def configured_path(env_name: str, default: Path | str) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser().resolve() if value else Path(default).expanduser().resolve()


DATA_ROOT = configured_path(
    "WHATSAPP_MCP_DATA_DIR",
    Path.home() / ".local/share/whatsapp-web-mcp",
)
STATE_ROOT = configured_path("WHATSAPP_MCP_STATE_DIR", DATA_ROOT / "state")
DEFAULT_OUTPUT_ROOT = configured_path("WHATSAPP_MCP_OUTPUT_DIR", DATA_ROOT / "exports")

FFMPEG_BIN = configured_path(
    "FFMPEG_BIN",
    shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
)
WHISPERX_PYTHON = configured_path(
    "WHISPERX_PYTHON",
    sys.executable,
)
WHISPERX_MODEL_DIR = configured_path(
    "WHISPERX_MODEL_DIR",
    Path.home() / ".cache/whisperx",
)

AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
}

VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}

WHISPERX_OUTPUT_FORMATS = {"all", "srt", "vtt", "txt", "tsv", "json", "aud"}
