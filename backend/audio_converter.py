"""
Audio discovery and WAV -> MP3 conversion via the ffmpeg CLI.

Copies each source file into a job-local working directory before any repair
attempt so original evidence files are never mutated in place.
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .models import AUDIO_EXTENSIONS
from .wav_repair import repair_file_in_place

logger = logging.getLogger(__name__)


_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def find_binary(name: str, env_var: str) -> Optional[str]:
    """Locate a CLI tool via an env override, then PATH, then common install dirs."""
    env_path = os.getenv(env_var)
    if env_path and os.path.isfile(env_path):
        return env_path
    path = shutil.which(name)
    if path:
        return path
    for d in _COMMON_BIN_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return None


FFMPEG_PATH = find_binary("ffmpeg", "FFMPEG_PATH")
FFPROBE_PATH = find_binary("ffprobe", "FFPROBE_PATH")


def discover_audio_files(folder: Optional[str]) -> list[str]:
    """Sorted absolute paths of every supported audio file under *folder* (recursive)."""
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS:
                found.append(os.path.abspath(os.path.join(root, f)))
    return sorted(found)


@dataclass
class ConversionResult:
    index: int
    original_path: str
    working_path: Optional[str] = None
    mp3_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    repaired: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.mp3_path is not None and self.error is None


def get_duration(file_path: str) -> Optional[float]:
    """Return duration in seconds using ffprobe."""
    if not FFPROBE_PATH:
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE_PATH, '-i', file_path,
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
            ],
            capture_output=True, text=True, timeout=30,
        )
        val = result.stdout.strip()
        if val:
            return float(val)
    except Exception as e:
        logger.debug("ffprobe failed for %s: %s", file_path, e)
    return None


def convert_single(
    index: int,
    src_path: str,
    output_dir: str,
    stem: Optional[str] = None,
    working_dir: Optional[str] = None,
) -> ConversionResult:
    """
    Copy the source into a job-local working directory, repair that working
    copy if needed, then convert it to MP3 in output_dir.
    stem overrides the output filename stem (without extension).
    """
    result = ConversionResult(index=index, original_path=src_path)

    if not FFMPEG_PATH:
        result.error = "ffmpeg not found on PATH"
        return result

    if not os.path.exists(src_path):
        result.error = f"Source file not found: {src_path}"
        return result

    if stem is None:
        stem = os.path.splitext(os.path.basename(src_path))[0]

    src_ext = os.path.splitext(src_path)[1].lower()
    working_dir = working_dir or os.path.join(os.path.dirname(output_dir), "source-working")
    os.makedirs(working_dir, exist_ok=True)
    working_path = os.path.join(working_dir, f"{stem}{src_ext}")
    shutil.copy2(src_path, working_path)
    result.working_path = working_path

    # Repair zeroed WAV headers on the working copy only.
    try:
        if src_ext == ".wav":
            repaired = repair_file_in_place(working_path)
            result.repaired = repaired
    except Exception as e:
        logger.warning("Header repair failed for working copy %s: %s", working_path, e)

    # Determine output path
    mp3_path = os.path.join(output_dir, f"{stem}.mp3")

    # Run ffmpeg against the working copy. For WAV inputs, try the explicit G.729
    # decode path first; for other accepted audio formats, use normal probing.
    cmd = [
        FFMPEG_PATH,
        '-y',
        '-i', working_path,
        '-c:a', 'libmp3lame',
        '-b:a', '64k',
        mp3_path,
    ]
    if src_ext == ".wav":
        cmd = [
            FFMPEG_PATH,
            '-y',
            '-f', 'wav',
            '-c:a', 'g729',
            '-i', working_path,
            '-c:a', 'libmp3lame',
            '-b:a', '64k',
            mp3_path,
        ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 and src_ext == ".wav":
            # Retry without forcing decoder (some files may be clean)
            cmd_retry = [
                FFMPEG_PATH, '-y',
                '-i', working_path,
                '-c:a', 'libmp3lame',
                '-b:a', '64k',
                mp3_path,
            ]
            proc2 = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=300)
            if proc2.returncode != 0:
                result.error = f"ffmpeg failed (rc={proc2.returncode}): {proc2.stderr[-500:]}"
                return result
        elif proc.returncode != 0:
            result.error = f"ffmpeg failed (rc={proc.returncode}): {proc.stderr[-500:]}"
            return result

        result.mp3_path = mp3_path
        result.duration_seconds = get_duration(mp3_path)
        logger.info("Converted [%d] %s -> %s (%.1fs)",
                    index, os.path.basename(src_path), os.path.basename(mp3_path),
                    result.duration_seconds or 0)
    except subprocess.TimeoutExpired:
        result.error = "ffmpeg conversion timed out after 5 minutes"
    except Exception as e:
        result.error = f"Conversion error: {e}"

    return result
