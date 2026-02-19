"""
Batch G.729 WAV -> MP3 converter using native ffmpeg subprocess.

Runs wav_repair first on each file, then converts to 64k MP3.
Uses ThreadPoolExecutor for parallelism (ffmpeg processes).
"""

import os
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional
import multiprocessing

from .wav_repair import repair_file_in_place

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> Optional[str]:
    path = shutil.which('ffmpeg')
    if path:
        return path
    for candidate in ['/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg', '/usr/bin/ffmpeg']:
        if os.path.exists(candidate):
            return candidate
    return None


def _find_ffprobe() -> Optional[str]:
    path = shutil.which('ffprobe')
    if path:
        return path
    for candidate in ['/usr/local/bin/ffprobe', '/opt/homebrew/bin/ffprobe', '/usr/bin/ffprobe']:
        if os.path.exists(candidate):
            return candidate
    return None


FFMPEG_PATH = _find_ffmpeg()
FFPROBE_PATH = _find_ffprobe()


@dataclass
class ConversionResult:
    index: int
    original_path: str
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
) -> ConversionResult:
    """
    Repair header if needed, then convert src_path to MP3 in output_dir.
    stem overrides the output filename stem (without extension).
    """
    result = ConversionResult(index=index, original_path=src_path)

    if not FFMPEG_PATH:
        result.error = "ffmpeg not found on PATH"
        return result

    if not os.path.exists(src_path):
        result.error = f"Source file not found: {src_path}"
        return result

    # Repair header if zeroed
    try:
        repaired = repair_file_in_place(src_path)
        result.repaired = repaired
    except Exception as e:
        logger.warning("Header repair failed for %s: %s", src_path, e)

    # Determine output path
    if stem is None:
        stem = os.path.splitext(os.path.basename(src_path))[0]
    mp3_path = os.path.join(output_dir, f"{stem}.mp3")

    # Run ffmpeg: force G.729 decoder, preserve stereo channels, 64k MP3.
    # Stereo output is intentional: ch1 (left) = inmate, ch2 (right) = outside party.
    # AssemblyAI multichannel transcription requires separate channels — do NOT mix to mono.
    cmd = [
        FFMPEG_PATH,
        '-y',                    # overwrite
        '-f', 'wav',             # force WAV container parse
        '-c:a', 'g729',          # force G.729 decoder
        '-i', src_path,
        '-c:a', 'libmp3lame',
        '-b:a', '64k',
        mp3_path,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            # Retry without forcing decoder (some files may be clean)
            cmd_retry = [
                FFMPEG_PATH, '-y',
                '-i', src_path,
                '-c:a', 'libmp3lame',
                '-b:a', '64k',
                mp3_path,
            ]
            proc2 = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=300)
            if proc2.returncode != 0:
                result.error = f"ffmpeg failed (rc={proc2.returncode}): {proc2.stderr[-500:]}"
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


def batch_convert(
    files: List[str],
    output_dir: str,
    stems: Optional[List[str]] = None,
    max_workers: Optional[int] = None,
    progress_callback=None,
) -> List[ConversionResult]:
    """
    Convert a list of WAV files to MP3 in parallel.

    Args:
        files: List of source file paths
        output_dir: Directory for output MP3 files
        stems: Optional list of output stems (without .mp3). Defaults to source filename stems.
        max_workers: Thread pool size. Defaults to cpu_count.
        progress_callback: Called with (completed, total) after each file finishes.

    Returns:
        List of ConversionResult in original order.
    """
    os.makedirs(output_dir, exist_ok=True)

    if max_workers is None:
        max_workers = max(1, multiprocessing.cpu_count())

    results: List[ConversionResult] = [None] * len(files)
    total = len(files)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i, src in enumerate(files):
            stem = stems[i] if stems else None
            fut = executor.submit(convert_single, i, src, output_dir, stem)
            futures[fut] = i

        completed = 0
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = ConversionResult(
                    index=i,
                    original_path=files[i],
                    error=str(e),
                )
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    return results
