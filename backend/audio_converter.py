"""
Batch audio -> MP3 converter using native ffmpeg subprocess.

Copies each source file into a job-local working directory before any repair
attempt so original evidence files are never mutated in place.
"""

import os
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional
from .wav_repair import repair_file_in_place

logger = logging.getLogger(__name__)


def _find_binary(name: str, env_var: str) -> Optional[str]:
    """Search for an ffmpeg/ffprobe binary via env var, PATH, and common locations."""
    # 1. Explicit env override
    env_path = os.getenv(env_var)
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. System PATH
    path = shutil.which(name)
    if path:
        return path

    # 3. Common install locations
    candidates = [
        '/usr/local/bin', '/opt/homebrew/bin', '/usr/bin',
        r'C:\ffmpeg\bin',
    ]

    # 4. Auto-discover ffmpeg installs under user home (Windows)
    if os.name == 'nt':
        home = os.path.expanduser("~")
        for search_dir in [home, os.path.join(home, "Downloads")]:
            if not os.path.isdir(search_dir):
                continue
            try:
                for entry in os.scandir(search_dir):
                    if not entry.is_dir() or 'ffmpeg' not in entry.name.lower():
                        continue
                    bin_dir = os.path.join(entry.path, 'bin')
                    if os.path.isdir(bin_dir):
                        candidates.append(bin_dir)
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_dir() and 'build' in sub.name.lower():
                                nested_bin = os.path.join(sub.path, 'bin')
                                if os.path.isdir(nested_bin):
                                    candidates.append(nested_bin)
                    except OSError:
                        pass
            except OSError:
                pass

    ext = '.exe' if os.name == 'nt' else ''
    for d in candidates:
        full = os.path.join(d, f'{name}{ext}')
        if os.path.isfile(full):
            return full
    return None


def _find_ffmpeg() -> Optional[str]:
    return _find_binary('ffmpeg', 'FFMPEG_PATH')


def _find_ffprobe() -> Optional[str]:
    return _find_binary('ffprobe', 'FFPROBE_PATH')


FFMPEG_PATH = _find_ffmpeg()
FFPROBE_PATH = _find_ffprobe()


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


def batch_convert(
    files: List[str],
    output_dir: str,
    stems: Optional[List[str]] = None,
    working_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    progress_callback=None,
) -> List[ConversionResult]:
    """
    Convert a list of audio files to MP3 in parallel.

    Args:
        files: List of source file paths
        output_dir: Directory for output MP3 files
        stems: Optional list of output stems (without .mp3). Defaults to source filename stems.
        working_dir: Optional directory for copied source-working files.
        max_workers: Thread pool size. Defaults to cpu_count.
        progress_callback: Called with (completed, total) after each file finishes.

    Returns:
        List of ConversionResult in original order.
    """
    os.makedirs(output_dir, exist_ok=True)

    if max_workers is None:
        max_workers = max(1, os.cpu_count() or 1)

    results: List[ConversionResult] = [None] * len(files)
    total = len(files)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i, src in enumerate(files):
            stem = stems[i] if stems else None
            fut = executor.submit(convert_single, i, src, output_dir, stem, working_dir)
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
