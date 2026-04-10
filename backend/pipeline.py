"""
Streaming pipeline orchestrator for jail call transcription jobs.

Architecture: Assembly-line model where each call flows independently
through four concurrent worker pools connected by asyncio.Queues:

  [Convert workers] → q → [Transcribe workers] → q → [Summarize workers] → q → [PDF workers]

After all calls complete, runs batch index generation and ZIP packaging.
SSE progress is broadcast via a thread-safe queue per job.
"""

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Job, JobStage, CallResult, CallStatus
from .icm_parser import find_icm_report, parse_icm_report
from . import job_store, config as cfg

logger = logging.getLogger(__name__)

# ── SSE event infrastructure (thread-safe) ──

_event_queues: Dict[str, queue.Queue] = {}
_queue_lock = threading.Lock()

_SENTINEL = object()

# Engines that run local models competing for GPU/unified memory
_LOCAL_TRANSCRIPTION_ENGINES = frozenset({"parakeet"})
_LOCAL_SUMMARIZATION_ENGINES = frozenset({"gemma"})


def get_event_queue(job_id: str) -> queue.Queue:
    with _queue_lock:
        if job_id not in _event_queues:
            _event_queues[job_id] = queue.Queue(maxsize=2000)
        return _event_queues[job_id]


def cleanup_event_queue(job_id: str) -> None:
    """Remove event queue for a completed job to prevent memory leaks."""
    _event_queues.pop(job_id, None)


def _emit(job_id: str, event: dict) -> None:
    """Put an event on the job's SSE queue (non-blocking, thread-safe)."""
    q = get_event_queue(job_id)
    try:
        q.put_nowait(event)
    except queue.Full:
        pass


# ── Helpers ──

def _discover_wav_files(input_folder: str) -> List[str]:
    """Find all .wav files in the input folder (recursive)."""
    if not input_folder or not os.path.isdir(input_folder):
        return []
    wav_files = []
    for root, dirs, files in os.walk(input_folder):
        for f in files:
            if f.lower().endswith(".wav"):
                wav_files.append(os.path.join(root, f))
    return sorted(wav_files)


def _call_stem(index: int, filename: str) -> str:
    """Generate an output stem like 001-originalname from the WAV filename."""
    base = filename
    if base.lower().endswith('.wav'):
        base = base[:-4]
    safe = re.sub(r'[^\w.\-]', '_', base)
    safe = re.sub(r'_+', '_', safe).strip('_') or "call"
    return f"{index + 1:03d}-{safe}"


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── Per-call worker functions ──

async def _convert_one(job_id, call, audio_dir, executor):
    """Convert a single call's audio. Returns updated call or None on failure."""
    from .audio_converter import convert_single

    stem = _call_stem(call.index, call.filename)
    job_store.update_call(job_id, call.index, status=CallStatus.CONVERTING)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        executor, convert_single, call.index, call.original_path, audio_dir, stem,
    )

    if result.success:
        job_store.update_call(
            job_id, call.index,
            mp3_path=result.mp3_path,
            duration_seconds=result.duration_seconds,
            repaired=result.repaired,
            status=CallStatus.TRANSCRIBING,
        )
        call.mp3_path = result.mp3_path
        call.duration_seconds = result.duration_seconds
        call.repaired = result.repaired
        call.status = CallStatus.TRANSCRIBING
        return call
    else:
        job_store.update_call(job_id, call.index, status=CallStatus.ERROR, error=result.error)
        call.status = CallStatus.ERROR
        logger.error("Conversion failed for %s: %s", call.filename, result.error)
        return None


async def _transcribe_one(job_id, call, defendant_name, transcription_engine=None):
    """Transcribe a single call using the configured transcription engine."""
    from .transcription import get_engine

    job_store.update_call(job_id, call.index, status=CallStatus.TRANSCRIBING)

    engine_name = transcription_engine or cfg.DEFAULT_TRANSCRIPTION_ENGINE
    engine = get_engine(
        engine_name,
        api_key=cfg.ASSEMBLYAI_API_KEY,
        speech_model=cfg.ASSEMBLYAI_MODEL,
        polling_interval=cfg.ASSEMBLYAI_POLLING_INTERVAL,
    )

    channel_labels = {
        1: call.inmate_name or defendant_name or "INMATE",
        2: "OUTSIDE PARTY",
    }
    turns = await engine.transcribe(call.mp3_path, channel_labels=channel_labels)
    job_store.update_call(job_id, call.index, turns=turns, status=CallStatus.SUMMARIZING)
    call.turns = turns
    call.status = CallStatus.SUMMARIZING
    return call


async def _summarize_one(job_id, call, summary_prompt, skip_summary, engine, auto_message_mode=None):
    """Summarize a single call's transcript using the provided engine instance."""
    from .system_audio import (
        SYSTEM_AUDIO_DETECTION_PROMPT,
        parse_system_audio_response,
        apply_system_audio_filter,
    )

    job_store.update_call(job_id, call.index, status=CallStatus.SUMMARIZING)

    if skip_summary:
        summary_text = (
            f"**DUMMY SUMMARY FOR {call.filename}**\n\n"
            f"- The user requested to skip AI processing for this test job.\n"
            f"- Call duration: {call.duration_seconds} sec."
        )
        token_kwargs = {}
        await asyncio.sleep(0.1)
    else:
        effective_prompt = summary_prompt or cfg.DEFAULT_SUMMARY_PROMPT
        if auto_message_mode in ("exclude", "label"):
            effective_prompt += "\n\n" + SYSTEM_AUDIO_DETECTION_PROMPT

        result = await engine.summarize(
            call.turns,
            prompt=effective_prompt,
            metadata={"filename": call.filename, "duration_seconds": call.duration_seconds},
        )
        raw_text = result["text"]
        token_kwargs = {
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "thinking_tokens": result["thinking_tokens"],
        }

        # Parse system audio markers and apply filtering
        if auto_message_mode in ("exclude", "label"):
            summary_text, markers = parse_system_audio_response(raw_text)
            if markers:
                call.turns = apply_system_audio_filter(call.turns, markers, auto_message_mode)
                job_store.update_call(job_id, call.index, turns=call.turns)
                logger.info(
                    "Applied system audio filter (%s) to %s: %d markers",
                    auto_message_mode, call.filename, len(markers),
                )
        else:
            summary_text = raw_text

    job_store.update_call(job_id, call.index, summary=summary_text, status=CallStatus.GENERATING_PDF, **token_kwargs)
    call.summary = summary_text
    call.status = CallStatus.GENERATING_PDF
    return call


async def _generate_pdf_one(job_id, call, case_name, transcripts_dir, transcripts_no_summary_dir, executor):
    """Generate both PDF variants (with and without summary) for a single call."""
    from .transcript_formatting import create_pdf

    stem = _call_stem(call.index, call.filename)
    audio_filename = os.path.basename(call.mp3_path) if call.mp3_path else f"{stem}.mp3"
    title_data = {
        "CASE_NAME": case_name,
        "FILE_NAME": call.filename,
        "AUDIO_FILENAME": audio_filename,
        "FILE_DURATION": _format_duration(call.duration_seconds),
        "INMATE_NAME": call.inmate_name or "",
        "CALL_DATETIME": call.call_datetime_str or "",
        "FACILITY": call.facility or "",
        "OUTSIDE_NUMBER_FMT": call.outside_number_fmt or "",
        "CALL_OUTCOME": call.call_outcome or "",
        "NOTES": call.notes or "",
    }

    loop = asyncio.get_event_loop()
    # Capture values for thread safety
    turns = call.turns
    summary = call.summary
    duration = call.duration_seconds or 0

    def _gen():
        pdf_path = os.path.join(transcripts_dir, f"{stem}.pdf")
        pdf_bytes = create_pdf(
            title_data=title_data, turns=turns, summary=summary, audio_duration=duration,
        )
        with open(pdf_path, 'wb') as f:
            f.write(pdf_bytes)

        pdf_no_summary_path = os.path.join(transcripts_no_summary_dir, f"{stem}.pdf")
        pdf_clean_bytes = create_pdf(
            title_data=title_data, turns=turns, summary=None, audio_duration=duration,
        )
        with open(pdf_no_summary_path, 'wb') as f:
            f.write(pdf_clean_bytes)

        return pdf_path

    pdf_path = await loop.run_in_executor(executor, _gen)
    job_store.update_call(job_id, call.index, pdf_path=pdf_path, status=CallStatus.DONE)
    call.pdf_path = pdf_path
    call.status = CallStatus.DONE
    return call


# ── Main pipeline ──

async def run_job(job_id: str) -> None:
    """Main pipeline entry point. Call from a background task."""
    job = job_store.get_job(job_id)
    if not job:
        logger.error("Job not found: %s", job_id)
        return

    try:
        await _run_pipeline(job)
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        job = job_store.get_job(job_id)
        job.stage = JobStage.ERROR
        job.error = str(e)
        job_store.update_job(job)
        _emit(job_id, {"type": "error", "message": str(e)})
    finally:
        cleanup_event_queue(job_id)


async def _run_pipeline(job: Job) -> None:
    job_id = job.id
    output_dir = job_store.get_job_output_dir(job_id)
    audio_dir = os.path.join(output_dir, "audio")
    transcripts_dir = os.path.join(output_dir, "transcripts")
    transcripts_no_summary_dir = os.path.join(output_dir, "transcripts-no-summary")
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(transcripts_dir, exist_ok=True)
    os.makedirs(transcripts_no_summary_dir, exist_ok=True)

    # ── Stage 0: Discover files & initialize call records ──
    _emit(job_id, {"type": "stage", "stage": "discovering"})

    wav_files = job.file_paths if job.file_paths else _discover_wav_files(job.input_folder)
    if not wav_files:
        raise RuntimeError(f"No WAV files found in: {job.input_folder} and no files explicitly provided.")

    _emit(job_id, {"type": "discovered", "count": len(wav_files)})
    logger.info("Discovered %d WAV files for job %s", len(wav_files), job_id)

    # Load ICM report metadata if present
    if job.xml_metadata_path and os.path.exists(job.xml_metadata_path):
        icm_xml = job.xml_metadata_path
    else:
        icm_xml = find_icm_report(job.input_folder) if job.input_folder else None

    icm_map = {}
    if icm_xml:
        try:
            icm_map = parse_icm_report(icm_xml)
        except Exception as e:
            logger.warning("ICM XML parsing failed: %s", e)
            _emit(job_id, {"type": "warning", "message": f"Metadata file found but could not be read: {os.path.basename(icm_xml)}. Call details (inmate name, phone, date) will be blank."})
    if icm_map:
        logger.info("ICM report loaded: %d records", len(icm_map))
    elif icm_xml:
        _emit(job_id, {"type": "warning", "message": f"Metadata file ({os.path.basename(icm_xml)}) contained no matching call records. Call details will be blank."})

    # Initialize call records (skip already-existing ones for resumability)
    existing = {c.original_path: c for c in job.calls}
    calls_to_add = []
    for i, wav_path in enumerate(wav_files):
        if wav_path not in existing:
            meta = icm_map.get(os.path.basename(wav_path))
            calls_to_add.append(CallResult(
                index=i,
                filename=os.path.basename(wav_path),
                original_path=wav_path,
                inmate_name=meta.inmate_name if meta else None,
                inmate_pin=meta.inmate_pin if meta else None,
                outside_number=meta.outside_number if meta else None,
                outside_number_fmt=meta.outside_number_fmt if meta else None,
                call_date=meta.call_date if meta else None,
                call_time=meta.call_time if meta else None,
                call_datetime_str=meta.call_datetime_str if meta else None,
                facility=meta.facility if meta else None,
                call_outcome=meta.call_outcome if meta else None,
                call_type=meta.call_type if meta else None,
                xml_duration_seconds=meta.xml_duration_seconds if meta else None,
                notes=meta.notes if meta else None,
            ))

    if calls_to_add:
        job.calls.extend(calls_to_add)

    job.stage = JobStage.CONVERTING
    job.started_at = datetime.now(timezone.utc).isoformat()
    job_store.update_job(job)

    # Reload to get DB-synced copies
    job = job_store.get_job(job_id)
    total_calls = len(job.calls)

    # ── Route calls to appropriate starting queues ──
    convert_q: asyncio.Queue = asyncio.Queue()
    transcribe_q: asyncio.Queue = asyncio.Queue()
    summarize_q: asyncio.Queue = asyncio.Queue()
    pdf_q: asyncio.Queue = asyncio.Queue()

    for call in job.calls:
        if call.status in (CallStatus.PENDING, CallStatus.CONVERTING):
            await convert_q.put(call)
        elif call.status == CallStatus.TRANSCRIBING and call.mp3_path and not call.turns:
            await transcribe_q.put(call)
        elif call.status == CallStatus.SUMMARIZING and call.turns and not call.summary:
            await summarize_q.put(call)
        elif call.status == CallStatus.GENERATING_PDF and call.turns:
            await pdf_q.put(call)
        # DONE or ERROR calls: skip

    # ── Worker counts ──
    n_convert = max(1, (os.cpu_count() or 2) - 1)
    transcription_engine_name = (job.transcription_engine or cfg.DEFAULT_TRANSCRIPTION_ENGINE).lower()
    if transcription_engine_name == "parakeet":
        n_transcribe = min(cfg.MAX_PARAKEET_CONCURRENT, max(1, total_calls))
    else:
        n_transcribe = min(cfg.MAX_TRANSCRIPTION_CONCURRENT, max(1, total_calls))
    summarization_engine_name = (job.summarization_engine or cfg.DEFAULT_SUMMARIZATION_ENGINE).lower()
    if summarization_engine_name == "gemma":
        n_summarize = min(cfg.MAX_GEMMA_CONCURRENT, max(1, total_calls))
    else:
        n_summarize = min(cfg.MAX_SUMMARIZATION_CONCURRENT, max(1, total_calls))
    n_pdf = min(4, max(1, total_calls))

    all_local = (
        transcription_engine_name in _LOCAL_TRANSCRIPTION_ENGINES
        and summarization_engine_name in _LOCAL_SUMMARIZATION_ENGINES
    )

    # Create summarization engine once (reused across all calls — critical for local model loading)
    from .summarization import get_engine as get_summarization_engine
    summarization_engine = get_summarization_engine(summarization_engine_name) if not job.skip_summary else None

    # Seed convert_q with sentinels (one per worker)
    for _ in range(n_convert):
        await convert_q.put(_SENTINEL)

    # Progress counters (safe — asyncio is single-threaded between awaits)
    progress = {"converting": 0, "transcribing": 0, "summarizing": 0, "generating_pdf": 0}
    stage_entered: set = set()

    def _advance_stage(stage: JobStage):
        """Update job.stage when first call enters a new pipeline stage."""
        if stage.value not in stage_entered:
            stage_entered.add(stage.value)
            job_store.update_job_stage(job_id, stage)
            _emit(job_id, {"type": "stage", "stage": stage.value, "total": total_calls})

    def _is_paused() -> bool:
        return job_store.get_job_stage(job_id) == JobStage.PAUSED.value

    # ── Worker loops ──

    async def convert_loop(executor):
        while True:
            item = await convert_q.get()
            if item is _SENTINEL:
                return
            if _is_paused():
                continue
            _advance_stage(JobStage.CONVERTING)
            try:
                result = await _convert_one(job_id, item, audio_dir, executor)
                progress["converting"] += 1
                if result:
                    _emit(job_id, {
                        "type": "call_update", "index": result.index,
                        "status": result.status, "stage": "converting",
                        "completed": progress["converting"], "total": total_calls,
                    })
                    await transcribe_q.put(result)
                else:
                    _emit(job_id, {
                        "type": "call_update", "index": item.index,
                        "status": item.status, "stage": "converting",
                        "completed": progress["converting"], "total": total_calls,
                    })
            except Exception as e:
                logger.error("Convert error for %s: %s", item.filename, e)
                job_store.update_call(job_id, item.index, status=CallStatus.ERROR, error=str(e))
                progress["converting"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": item.index,
                    "status": "error", "stage": "converting",
                    "completed": progress["converting"], "total": total_calls,
                })

    async def transcribe_loop():
        while True:
            item = await transcribe_q.get()
            if item is _SENTINEL:
                return
            if _is_paused():
                continue
            _advance_stage(JobStage.TRANSCRIBING)
            try:
                result = await _transcribe_one(job_id, item, job.defendant_name, job.transcription_engine)
                progress["transcribing"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": result.index,
                    "status": result.status, "stage": "transcribing",
                    "completed": progress["transcribing"], "total": total_calls,
                })
                await summarize_q.put(result)
            except Exception as e:
                logger.error("Transcription error for %s: %s", item.filename, e)
                job_store.update_call(
                    job_id, item.index,
                    status=CallStatus.ERROR, error=f"Transcription failed: {e}",
                )
                progress["transcribing"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": item.index,
                    "status": "error", "stage": "transcribing",
                    "completed": progress["transcribing"], "total": total_calls,
                })

    async def summarize_loop():
        while True:
            item = await summarize_q.get()
            if item is _SENTINEL:
                return
            if _is_paused():
                continue
            _advance_stage(JobStage.SUMMARIZING)
            try:
                result = await _summarize_one(job_id, item, job.summary_prompt, job.skip_summary, summarization_engine, job.auto_message_mode)
                progress["summarizing"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": result.index,
                    "status": result.status, "stage": "summarizing",
                    "completed": progress["summarizing"], "total": total_calls,
                })
                await pdf_q.put(result)
            except Exception as e:
                logger.error("Summarization error for %s: %s", item.filename, e)
                err_msg = "Summary unavailable for this call."
                job_store.update_call(
                    job_id, item.index, summary=err_msg, status=CallStatus.GENERATING_PDF,
                )
                item.summary = err_msg
                item.status = CallStatus.GENERATING_PDF
                progress["summarizing"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": item.index,
                    "status": item.status, "stage": "summarizing",
                    "completed": progress["summarizing"], "total": total_calls,
                })
                await pdf_q.put(item)

    async def pdf_loop(executor):
        while True:
            item = await pdf_q.get()
            if item is _SENTINEL:
                return
            if _is_paused():
                continue
            _advance_stage(JobStage.GENERATING)
            try:
                result = await _generate_pdf_one(
                    job_id, item, job.case_name,
                    transcripts_dir, transcripts_no_summary_dir, executor,
                )
                progress["generating_pdf"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": result.index,
                    "status": result.status, "stage": "generating_pdf",
                    "completed": progress["generating_pdf"], "total": total_calls,
                })
            except Exception as e:
                logger.error("PDF error for %s: %s", item.filename, e)
                job_store.update_call(job_id, item.index, error=f"PDF failed: {e}")
                progress["generating_pdf"] += 1
                _emit(job_id, {
                    "type": "call_update", "index": item.index,
                    "status": "error", "stage": "generating_pdf",
                    "completed": progress["generating_pdf"], "total": total_calls,
                })

    # ── Stage group coordinator ──

    async def run_stage_group(workers, downstream_q, n_downstream):
        """Run all workers in a group, then send sentinels downstream."""
        await asyncio.gather(*workers)
        if downstream_q is not None:
            for _ in range(n_downstream):
                await downstream_q.put(_SENTINEL)

    # ── Execute streaming pipeline ──

    _advance_stage(JobStage.CONVERTING)

    convert_executor = ThreadPoolExecutor(max_workers=n_convert)
    pdf_executor = ThreadPoolExecutor(max_workers=n_pdf)

    try:
        if all_local:
            # Two-phase mode: avoid loading both local models simultaneously on 8 GB.
            # Phase 1: Convert + Transcribe (Parakeet memory freed when phase ends)
            logger.info("All-local mode: running two-phase pipeline for job %s", job_id)
            await asyncio.gather(
                run_stage_group(
                    [convert_loop(convert_executor) for _ in range(n_convert)],
                    transcribe_q, n_transcribe,
                ),
                run_stage_group(
                    [transcribe_loop() for _ in range(n_transcribe)],
                    summarize_q, n_summarize,
                ),
            )

            if _is_paused():
                return

            # Phase 2: Summarize + PDF (summarize_q already has items + sentinels)
            await asyncio.gather(
                run_stage_group(
                    [summarize_loop() for _ in range(n_summarize)],
                    pdf_q, n_pdf,
                ),
                run_stage_group(
                    [pdf_loop(pdf_executor) for _ in range(n_pdf)],
                    None, 0,
                ),
            )
        else:
            # Standard streaming pipeline: all four stages run concurrently
            await asyncio.gather(
                run_stage_group(
                    [convert_loop(convert_executor) for _ in range(n_convert)],
                    transcribe_q, n_transcribe,
                ),
                run_stage_group(
                    [transcribe_loop() for _ in range(n_transcribe)],
                    summarize_q, n_summarize,
                ),
                run_stage_group(
                    [summarize_loop() for _ in range(n_summarize)],
                    pdf_q, n_pdf,
                ),
                run_stage_group(
                    [pdf_loop(pdf_executor) for _ in range(n_pdf)],
                    None, 0,
                ),
            )
    finally:
        convert_executor.shutdown(wait=False)
        pdf_executor.shutdown(wait=False)
        # Unload local summarization model to free memory
        if summarization_engine and hasattr(summarization_engine, "unload"):
            summarization_engine.unload()

    # ── Check for pause before finishing ──
    if _is_paused():
        return

    # ── Post-pipeline: indexes + packaging ──
    _emit(job_id, {"type": "stage", "stage": "generating_indexes"})
    job = job_store.get_job(job_id)
    await _stage_generate_indexes(job, output_dir, audio_dir)

    if _is_paused():
        return

    job.stage = JobStage.PACKAGING
    job_store.update_job(job)
    _emit(job_id, {"type": "stage", "stage": "packaging"})
    zip_path = await _stage_package(job, output_dir)

    job.stage = JobStage.DONE
    job.zip_path = zip_path
    job.completed_at = datetime.now(timezone.utc).isoformat()
    job_store.update_job(job)
    _emit(job_id, {"type": "done", "zip_path": zip_path})
    logger.info("Job %s completed. Zip: %s", job_id, zip_path)


# ── Batch stages (kept for repackaging and index generation) ──

async def _stage_generate_indexes(job: Job, output_dir: str, audio_dir: str) -> None:
    from .excel_report import generate_excel
    from .search_html import generate_search_html
    from .viewer import render_viewer

    loop = asyncio.get_event_loop()
    done_calls = [c for c in job.calls if c.status == CallStatus.DONE]
    error_calls = [c for c in job.calls if c.status == CallStatus.ERROR]

    def write_excel():
        data = generate_excel(done_calls, error_calls=error_calls or None)
        with open(os.path.join(output_dir, "call-index.xlsx"), 'wb') as f:
            f.write(data)

    def write_search():
        html = generate_search_html(done_calls, case_name=job.case_name)
        with open(os.path.join(output_dir, "search.html"), 'w', encoding='utf-8') as f:
            f.write(html)

    def write_viewer():
        viewer_dir = os.path.join(output_dir, "viewer")
        os.makedirs(viewer_dir, exist_ok=True)
        html = render_viewer(done_calls, case_name=job.case_name)
        with open(os.path.join(viewer_dir, "index.html"), 'w', encoding='utf-8') as f:
            f.write(html)

    def write_guide():
        from .guide_pdf import generate_guide_pdf
        guide_bytes = generate_guide_pdf(case_name=job.case_name, call_count=len(done_calls))
        with open(os.path.join(output_dir, "guide.pdf"), 'wb') as f:
            f.write(guide_bytes)

    for fn in [write_excel, write_search, write_viewer, write_guide]:
        try:
            await loop.run_in_executor(None, fn)
        except Exception as e:
            logger.error("Index generation failed (%s): %s", fn.__name__, e)


async def _stage_package(job: Job, output_dir: str) -> str:
    loop = asyncio.get_event_loop()

    def make_zip() -> str:
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in job.case_name).strip()
        zip_path = os.path.join(job_store._job_dir(job.id), f"{safe_name}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(output_dir):
                for file in files:
                    abs_path = os.path.join(root, file)
                    arcname = os.path.join(safe_name, os.path.relpath(abs_path, output_dir))
                    zf.write(abs_path, arcname)
        return zip_path

    return await loop.run_in_executor(None, make_zip)


