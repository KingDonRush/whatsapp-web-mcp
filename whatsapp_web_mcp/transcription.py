from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .constants import (
    AUDIO_EXTENSIONS,
    FFMPEG_BIN,
    VIDEO_EXTENSIONS,
    WHISPERX_MODEL_DIR,
    WHISPERX_OUTPUT_FORMATS,
    WHISPERX_PYTHON,
)


def path_exists(path: Path) -> bool:
    return path.exists() and path.is_file()


def media_input_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "unknown"


def ensure_tool(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def python_module_available(python: Path, module: str) -> bool:
    if not python.exists():
        return False
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, sys; "
                    f"sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
                ),
            ],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def run_command(cmd: list[str], timeout_seconds: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )


def select_whisperx_device(
    requested_device: str = "auto",
    max_gpu_memory_ratio: float = 0.65,
) -> tuple[str, dict[str, Any]]:
    """Use CUDA when available and not heavily occupied; otherwise use CPU."""
    if requested_device != "auto":
        return requested_device, {
            "requested_device": requested_device,
            "selected_device": requested_device,
            "reason": "explicit_device",
        }

    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "cpu", {
            "requested_device": requested_device,
            "selected_device": "cpu",
            "reason": "nvidia_smi_not_found",
        }

    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception as exc:
        return "cpu", {
            "requested_device": requested_device,
            "selected_device": "cpu",
            "reason": f"gpu_probe_failed:{type(exc).__name__}",
        }

    best_ratio: float | None = None
    best_total: int | None = None
    best_used: int | None = None
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            total = int(parts[0])
            used = int(parts[1])
        except ValueError:
            continue
        ratio = used / total if total else 1.0
        if best_ratio is None or ratio < best_ratio:
            best_ratio = ratio
            best_total = total
            best_used = used

    if best_ratio is None:
        return "cpu", {
            "requested_device": requested_device,
            "selected_device": "cpu",
            "reason": "gpu_probe_empty",
        }
    if best_ratio <= max_gpu_memory_ratio:
        return "cuda", {
            "requested_device": requested_device,
            "selected_device": "cuda",
            "reason": "gpu_available",
            "memory_total_mb": best_total,
            "memory_used_mb": best_used,
            "memory_used_ratio": round(best_ratio, 4),
            "max_gpu_memory_ratio": max_gpu_memory_ratio,
        }
    return "cpu", {
        "requested_device": requested_device,
        "selected_device": "cpu",
        "reason": "gpu_busy",
        "memory_total_mb": best_total,
        "memory_used_mb": best_used,
        "memory_used_ratio": round(best_ratio, 4),
        "max_gpu_memory_ratio": max_gpu_memory_ratio,
    }


def prepare_audio(
    source: Path,
    out_dir: Path,
    sample_rate: int = 16000,
    mono: bool = True,
    force: bool = False,
) -> Path:
    ensure_tool(FFMPEG_BIN, "ffmpeg")
    if not source.exists():
        raise FileNotFoundError(f"source media not found: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "mono16k" if mono and sample_rate == 16000 else f"{sample_rate}hz"
    dest = out_dir / f"{source.stem}.{suffix}.wav"
    if dest.exists() and not force and dest.stat().st_mtime >= source.stat().st_mtime:
        return dest
    cmd = [
        str(FFMPEG_BIN),
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1" if mono else "2",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    run_command(cmd)
    return dest


def read_text_if_possible(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def normalize_whisperx_language(language: str | None) -> str | None:
    if not language or language == "auto":
        return None
    aliases = {"portuguese": "pt", "english": "en", "spanish": "es"}
    return aliases.get(language.lower(), language)


def transcribe_with_whisperx(
    source: Path,
    out_dir: Path,
    language: str | None = "pt",
    diarize: bool = False,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    model: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
    output_format: str = "json",
    hf_token: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    ensure_tool(WHISPERX_PYTHON, "WhisperX python")
    if not python_module_available(WHISPERX_PYTHON, "whisperx"):
        raise RuntimeError(
            "WhisperX is not installed in WHISPERX_PYTHON. "
            "Install the transcription extra or point WHISPERX_PYTHON to a WhisperX environment."
        )
    if output_format not in WHISPERX_OUTPUT_FORMATS:
        raise ValueError(
            f"WhisperX output_format must be one of {sorted(WHISPERX_OUTPUT_FORMATS)}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    selected_device, device_decision = select_whisperx_device(device)
    selected_compute_type = (
        "float16" if compute_type == "auto" and selected_device == "cuda"
        else "int8" if compute_type == "auto"
        else compute_type
    )
    cmd = [
        str(WHISPERX_PYTHON),
        "-m",
        "whisperx",
        str(source),
        "--model",
        model,
        "--output_dir",
        str(out_dir),
        "--output_format",
        output_format,
        "--device",
        selected_device,
        "--compute_type",
        selected_compute_type,
        "--verbose",
        "False",
    ]
    if WHISPERX_MODEL_DIR.exists():
        cmd.extend(["--model_dir", str(WHISPERX_MODEL_DIR), "--model_cache_only", "True"])
    normalized_language = normalize_whisperx_language(language)
    if normalized_language:
        cmd.extend(["--language", normalized_language])
    if diarize:
        cmd.append("--diarize")
        if min_speakers is not None:
            cmd.extend(["--min_speakers", str(min_speakers)])
        if max_speakers is not None:
            cmd.extend(["--max_speakers", str(max_speakers)])
        token = hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        if token:
            cmd.extend(["--hf_token", token])

    env = os.environ.copy()
    if WHISPERX_MODEL_DIR.exists():
        env.setdefault("HF_HOME", str(WHISPERX_MODEL_DIR))
        env.setdefault("TRANSFORMERS_CACHE", str(WHISPERX_MODEL_DIR))
    started = time.time()
    completed = subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        env=env,
    )

    expected_json = out_dir / f"{source.stem}.json"
    payload: dict[str, Any] | None = None
    text: str | None = None
    if expected_json.exists():
        payload = json.loads(expected_json.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            if isinstance(payload.get("text"), str):
                text = payload["text"]
            elif isinstance(payload.get("segments"), list):
                text = "\n".join(
                    str(segment.get("text", "")).strip()
                    for segment in payload["segments"]
                    if isinstance(segment, dict)
                ).strip()
    transcript_file = expected_json if expected_json.exists() else out_dir
    return {
        "backend": "whisperx",
        "source_file": str(source),
        "transcript_file": str(transcript_file),
        "format": output_format,
        "language": normalized_language,
        "diarize": diarize,
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
        "model": model,
        "device": selected_device,
        "requested_device": device,
        "device_decision": device_decision,
        "compute_type": selected_compute_type,
        "requested_compute_type": compute_type,
        "duration_seconds": round(time.time() - started, 3),
        "text": text,
        "json": payload,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def transcribe_file(
    file_path: str,
    out_dir: str | None = None,
    backend: str = "whisperx",
    language: str = "portuguese",
    diarize: bool = False,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    prepare: bool = True,
    force_prepare: bool = False,
    whisperx_model: str = "large-v3",
    whisperx_device: str = "auto",
    whisperx_compute_type: str = "auto",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    if backend != "whisperx":
        raise ValueError("Only WhisperX is supported as transcription backend")
    source = Path(file_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"file not found: {source}")
    base_out = Path(out_dir).expanduser().resolve() if out_dir else source.parent / "transcripts"
    base_out.mkdir(parents=True, exist_ok=True)
    category = media_input_category(source)
    prepared_audio: Path | None = None
    transcription_source = source
    if prepare or category == "video":
        prepared_audio = prepare_audio(source, base_out / "prepared-audio", force=force_prepare)
        transcription_source = prepared_audio

    result = transcribe_with_whisperx(
        transcription_source,
        base_out,
        language=language,
        diarize=diarize,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        model=whisperx_model,
        device=whisperx_device,
        compute_type=whisperx_compute_type,
        timeout_seconds=timeout_seconds,
    )

    result.update(
        {
            "original_file": str(source),
            "input_category": category,
            "prepared_audio_file": str(prepared_audio) if prepared_audio else None,
            "prepared_with": "ffmpeg" if prepared_audio else None,
            "ffmpeg": str(FFMPEG_BIN) if shutil.which(str(FFMPEG_BIN)) or FFMPEG_BIN.exists() else None,
        }
    )
    return result
