"""
Batch processing orchestrator for jail call transcription jobs.

Stages:
  0. Discover WAV files in input folder
  1. Repair + convert (parallel, ThreadPoolExecutor)
  2. Transcribe (parallel, asyncio Semaphore)
  3. Summarize (parallel, asyncio Semaphore)
  4. Generate PDFs (sequential, fast)
  5. Generate Excel, search HTML, viewer, README
  6. Package zip

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Job, JobStage, CallResult, CallStatus
from .icm_parser import find_icm_report, parse_icm_report
from . import job_store, config as cfg

logger = logging.getLogger(__name__)

# Per-job SSE event queues using thread-safe stdlib queue.Queue.
# The pipeline runs in a background thread (via asyncio.run in BackgroundTasks),
# while the SSE endpoint reads from the server's main event loop — so we
# cannot use asyncio.Queue which is bound to a single loop.
_event_queues: Dict[str, queue.Queue] = {}
_queue_lock = threading.Lock()


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


async def run_job(job_id: str) -> None:
    """
    Main pipeline coroutine. Runs all stages and updates job state.
    Call this from a background task.
    """
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

    # ── Stage 0: Discover files ──
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

    # Initialize call records (skip already-done ones for resumability)
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
    
    # Immediately reload to get DB-synced copies (avoid detachment issues)
    job = job_store.get_job(job_id)

    # ── Stage 1: Convert ──
    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
    _emit(job_id, {"type": "stage", "stage": "converting", "total": len(wav_files)})
    await _stage_convert(job_id, audio_dir)
    
    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
    job = job_store.get_job(job_id)
    job.stage = JobStage.TRANSCRIBING
    job_store.update_job(job)

    # ── Stage 2: Transcribe ──
    _emit(job_id, {"type": "stage", "stage": "transcribing", "total": len(wav_files)})
    await _stage_transcribe(job_id)

    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
    job = job_store.get_job(job_id)
    job.stage = JobStage.SUMMARIZING
    job_store.update_job(job)

    # ── Stage 3: Summarize ──
    _emit(job_id, {"type": "stage", "stage": "summarizing", "total": len(wav_files)})
    await _stage_summarize(job_id)
    
    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
    job = job_store.get_job(job_id)
    job.stage = JobStage.GENERATING
    job_store.update_job(job)

    # ── Stage 4: Generate PDFs ──
    _emit(job_id, {"type": "stage", "stage": "generating_pdfs", "total": len(wav_files)})
    await _stage_generate_pdfs(job_id, transcripts_dir, transcripts_no_summary_dir)

    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
    # ── Stage 5: Generate indexes ──
    _emit(job_id, {"type": "stage", "stage": "generating_indexes"})
    job = job_store.get_job(job_id)
    await _stage_generate_indexes(job, output_dir, audio_dir)

    # ── Stage 6: Package zip ──
    if job_store.get_job(job_id).stage == JobStage.PAUSED: return
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


async def _stage_convert(job_id: str, audio_dir: str) -> None:
    import multiprocessing
    from .audio_converter import convert_single

    job = job_store.get_job(job_id)
    loop = asyncio.get_event_loop()
    pending = [c for c in job.calls if c.status == CallStatus.PENDING or c.status == CallStatus.CONVERTING]
    completed_count = sum(1 for c in job.calls if c.status not in (CallStatus.PENDING, CallStatus.CONVERTING, CallStatus.ERROR))

    workers = max(1, multiprocessing.cpu_count() - 1)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for call in pending:
            job_store.update_call(job_id, call.index, status=CallStatus.CONVERTING)
            stem = _call_stem(call.index, call.filename)
            fut = executor.submit(convert_single, call.index, call.original_path, audio_dir, stem)
            futures[fut] = call

        for fut in as_completed(futures):
            if job_store.get_job(job_id).stage == JobStage.PAUSED:
                for pending_fut in futures:
                    pending_fut.cancel()
                return
            call = futures[fut]
            try:
                result = fut.result()
                if result.success:
                    job_store.update_call(job_id, call.index, 
                                          mp3_path=result.mp3_path, 
                                          duration_seconds=result.duration_seconds,
                                          repaired=result.repaired,
                                          status=CallStatus.TRANSCRIBING)
                    call.status = CallStatus.TRANSCRIBING
                else:
                    job_store.update_call(job_id, call.index, 
                                          status=CallStatus.ERROR, 
                                          error=result.error)
                    call.status = CallStatus.ERROR
                    logger.error("Conversion failed for %s: %s", call.filename, result.error)
            except Exception as e:
                job_store.update_call(job_id, call.index, 
                                      status=CallStatus.ERROR, 
                                      error=str(e))
                call.status = CallStatus.ERROR

            completed_count += 1
            _emit(job_id, {
                "type": "call_update",
                "index": call.index,
                "status": call.status,
                "stage": "converting",
                "completed": completed_count,
                "total": len(job.calls),
            })


async def _stage_transcribe(job_id: str) -> None:
    from .transcriber import transcribe_multichannel

    job = job_store.get_job(job_id)
    sem = asyncio.Semaphore(cfg.MAX_TRANSCRIPTION_CONCURRENT)
    loop = asyncio.get_event_loop()

    async def transcribe_one(call: CallResult) -> None:
        if job_store.get_job(job_id).stage == JobStage.PAUSED:
            return
        if not call.mp3_path or call.status == CallStatus.ERROR:
            return
        job_store.update_call(job_id, call.index, status=CallStatus.TRANSCRIBING)
        call.status = CallStatus.TRANSCRIBING

        async with sem:
            try:
                channel_labels = {
                    1: call.inmate_name or job.defendant_name or "INMATE",
                    2: "OUTSIDE PARTY",
                }
                turns = await loop.run_in_executor(
                    None,
                    lambda: transcribe_multichannel(
                        call.mp3_path,
                        api_key=cfg.ASSEMBLYAI_API_KEY,
                        channel_labels=channel_labels,
                        speech_model=cfg.ASSEMBLYAI_MODEL,
                    )
                )
                job_store.update_call(job_id, call.index, turns=turns, status=CallStatus.SUMMARIZING)
                call.status = CallStatus.SUMMARIZING
            except Exception as e:
                job_store.update_call(job_id, call.index, status=CallStatus.ERROR, error=f"Transcription failed: {e}")
                call.status = CallStatus.ERROR
                logger.error("Transcription failed for %s: %s", call.filename, e)

        _emit(job_id, {"type": "call_update", "index": call.index, "status": call.status, "stage": "transcribing"})

    eligible = [c for c in job.calls if c.status in (CallStatus.TRANSCRIBING, CallStatus.SUMMARIZING)
                and not c.turns]
    await asyncio.gather(*[transcribe_one(c) for c in eligible])


async def _stage_summarize(job_id: str) -> None:
    from .summarizer import summarize_transcript

    job = job_store.get_job(job_id)
    sem = asyncio.Semaphore(cfg.MAX_SUMMARIZATION_CONCURRENT)
    loop = asyncio.get_event_loop()

    async def summarize_one(call: CallResult) -> None:
        if job_store.get_job(job_id).stage == JobStage.PAUSED:
            return
        if not call.turns or call.status == CallStatus.ERROR:
            return
        job_store.update_call(job_id, call.index, status=CallStatus.SUMMARIZING)
        call.status = CallStatus.SUMMARIZING

        async with sem:
            try:
                if job.skip_summary:
                    summary = f"**DUMMY SUMMARY FOR {call.filename}**\n\n- The user requested to skip Gemini processing for this test job.\n- Call duration: {call.duration_seconds} sec."
                    await asyncio.sleep(0.1) # Simulate just a tiny bit of asynchronous IO
                else:
                    summary = await summarize_transcript(
                        call.turns,
                        prompt=job.summary_prompt or cfg.DEFAULT_SUMMARY_PROMPT,
                        metadata={"filename": call.filename, "duration_seconds": call.duration_seconds},
                    )
                job_store.update_call(job_id, call.index, summary=summary, status=CallStatus.GENERATING_PDF)
                call.status = CallStatus.GENERATING_PDF
            except Exception as e:
                logger.error("Summarization failed for %s: %s", call.filename, e)
                err_msg = "Summary unavailable for this call."
                job_store.update_call(job_id, call.index, summary=err_msg, status=CallStatus.GENERATING_PDF)
                call.status = CallStatus.GENERATING_PDF

        _emit(job_id, {"type": "call_update", "index": call.index, "status": call.status, "stage": "summarizing"})

    eligible = [c for c in job.calls if c.turns and not c.summary and c.status != CallStatus.ERROR]
    await asyncio.gather(*[summarize_one(c) for c in eligible])


async def _stage_generate_pdfs(job_id: str, transcripts_dir: str, transcripts_no_summary_dir: str) -> None:
    from .transcript_formatting import create_pdf

    job = job_store.get_job(job_id)
    loop = asyncio.get_event_loop()

    def gen_pdf(call: CallResult) -> None:
        if job_store.get_job(job_id).stage == JobStage.PAUSED:
            return
        if not call.turns:
            return
        stem = _call_stem(call.index, call.filename)
        audio_filename = os.path.basename(call.mp3_path) if call.mp3_path else f"{stem}.mp3"
        title_data = {
            "CASE_NAME": job.case_name,
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

        # PDF with summary
        pdf_path = os.path.join(transcripts_dir, f"{stem}.pdf")
        pdf_bytes = create_pdf(
            title_data=title_data,
            turns=call.turns,
            summary=call.summary,
            audio_duration=call.duration_seconds or 0,
        )
        with open(pdf_path, 'wb') as f:
            f.write(pdf_bytes)

        # PDF without summary
        pdf_no_summary_path = os.path.join(transcripts_no_summary_dir, f"{stem}.pdf")
        pdf_clean_bytes = create_pdf(
            title_data=title_data,
            turns=call.turns,
            summary=None,
            audio_duration=call.duration_seconds or 0,
        )
        with open(pdf_no_summary_path, 'wb') as f:
            f.write(pdf_clean_bytes)

        call.status = CallStatus.DONE
        job_store.update_call(job_id, call.index, pdf_path=pdf_path, status=CallStatus.DONE)

    eligible = [c for c in job.calls if c.turns and c.status != CallStatus.ERROR]
    for call in eligible:
        try:
            await loop.run_in_executor(None, gen_pdf, call)
        except Exception as e:
            logger.error("PDF generation failed for %s: %s", call.filename, e)
            call.error = f"PDF failed: {e}"
            job_store.update_call(job_id, call.index, error=call.error)

        _emit(job_id, {"type": "call_update", "index": call.index, "status": call.status, "stage": "generating_pdf"})


async def _stage_generate_indexes(job: Job, output_dir: str, audio_dir: str) -> None:
    from .excel_report import generate_excel
    from .search_html import generate_search_html
    from .viewer import render_viewer

    loop = asyncio.get_event_loop()
    done_calls = [c for c in job.calls if c.status == CallStatus.DONE]

    # Excel
    def write_excel():
        data = generate_excel(done_calls)
        with open(os.path.join(output_dir, "call-index.xlsx"), 'wb') as f:
            f.write(data)

    # Search HTML
    def write_search():
        html = generate_search_html(done_calls, case_name=job.case_name)
        with open(os.path.join(output_dir, "search.html"), 'w', encoding='utf-8') as f:
            f.write(html)

    # Viewer
    def write_viewer():
        viewer_dir = os.path.join(output_dir, "viewer")
        os.makedirs(viewer_dir, exist_ok=True)
        html = render_viewer(done_calls, case_name=job.case_name)
        with open(os.path.join(viewer_dir, "index.html"), 'w', encoding='utf-8') as f:
            f.write(html)

    # README
    def write_readme():
        readme = _build_readme(job, done_calls)
        with open(os.path.join(output_dir, "README.txt"), 'w', encoding='utf-8') as f:
            f.write(readme)

    for fn in [write_excel, write_search, write_viewer, write_readme]:
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


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _build_readme(job: Job, done_calls: list) -> str:
    return f"""CASE: {job.case_name}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Total calls: {len(done_calls)}

CONTENTS
--------
transcripts/              PDF transcripts with AI analysis on page 2.
transcripts-no-summary/   PDF transcripts without AI analysis (clean copies).
audio/                    Converted MP3 audio files.
viewer/                   Open viewer/index.html in any browser to play calls
                          with synced transcripts.
search.html               Full-text search across all transcripts.
call-index.xlsx           Spreadsheet index (Excel or Google Sheets).

HOW TO USE
----------
1. Open viewer/index.html in Chrome or Safari to browse calls.
2. Click any call in the left panel to load it.
3. Press Space to play/pause. Arrow keys skip 5 seconds.
4. Open search.html to search across all calls at once.
5. call-index.xlsx has summaries and full transcripts for filtering.
6. transcripts/ folder has AI analysis summaries on page 2 of each PDF.
7. transcripts-no-summary/ has clean PDFs without AI analysis.

AI ANALYSIS RELEVANCE RATINGS
------------------------------
Each summary page rates the call as HIGH, MEDIUM, or LOW relevance.
- HIGH: Contains case-related discussion or potentially significant content.
- MEDIUM: Indirect references to the case, court dates, or legal proceedings.
- LOW: Personal conversation with no apparent case relevance.

NOTE: This package was generated by AI transcription software.
Transcripts may contain errors. Always verify critical details against
the original audio recording.
"""
