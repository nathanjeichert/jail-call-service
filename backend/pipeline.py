"""Streaming pipeline orchestrator for jail call transcription jobs.

Assembly-line model: each call flows independently through four concurrent
worker pools connected by asyncio queues::

  [convert] -> q -> [transcribe] -> q -> [summarize] -> q -> [pdf]

After every call has passed through, the batch stage builds the delivery
assets (index.html, guide.pdf, case-report.pdf) and zips the
output folder. Progress is broadcast through ``backend.events``.

Resumability: every per-call stage transition is checkpointed in the job
store, and ``_run_pipeline`` routes each call back into the queue matching
its stored status, so a paused or crashed job resumes without re-spending
API credits. Pausing is cooperative: workers poll the job's stage between
items and stop pulling work once it reads "paused".
"""

import asyncio
import logging
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional

from . import config as cfg
from . import job_store
from .audio_converter import convert_single, discover_audio_files
from .call_dates import filename_date_fields
from .events import cleanup_event_queue, emit
from .formatting import format_duration
from .icm_parser import find_icm_report, parse_icm_report
from .job_settings import (
    RuntimeSelection,
    effective_summary_prompt,
    resolve_runtime_selection,
    validate_runtime_selection,
)
from .models import (
    DEFAULT_SPEAKER_ASSIGNMENT,
    OUTSIDE_PARTY_LABEL,
    CallResult,
    CallStatus,
    Job,
    JobStage,
    call_stem,
    normalize_speaker_assignment,
)
from .summaries import DUMMY_SUMMARY_PREFIX
from .summarization import SummarizationEngine, TokenUsage

logger = logging.getLogger(__name__)

_SENTINEL = object()
_QUEUE_POLL_TIMEOUT_SEC = 1.0
_MAX_PDF_WORKERS = 8


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────── Per-call work ──────────────────────────

def _build_channel_labels(
    call: CallResult,
    defendant_name: Optional[str],
    speaker_assignment: Optional[str],
) -> Dict[int, str]:
    """Map stereo channels to speaker labels (channel 1 = left)."""
    inmate_label = call.inmate_name or defendant_name or "INMATE"
    assignment = normalize_speaker_assignment(speaker_assignment or DEFAULT_SPEAKER_ASSIGNMENT)
    if assignment == "right_inmate":
        return {1: OUTSIDE_PARTY_LABEL, 2: inmate_label}
    return {1: inmate_label, 2: OUTSIDE_PARTY_LABEL}


async def _convert_one(job_id, call, audio_dir, executor) -> Optional[CallResult]:
    """Convert a single call's audio. Returns the call, or None on failure."""
    stem = call_stem(call.index, call.filename)
    # Working copies live outside output/ so they never ship in the delivery ZIP.
    working_dir = os.path.join(job_store.job_dir(job_id), "source-working")
    job_store.update_call(job_id, call.index, status=CallStatus.CONVERTING)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        executor, convert_single, call.index, call.original_path, audio_dir, stem, working_dir,
    )

    if not result.success:
        job_store.update_call(job_id, call.index, status=CallStatus.ERROR, error=result.error)
        call.status = CallStatus.ERROR
        logger.error("Conversion failed for %s: %s", call.filename, result.error)
        return None

    call.mp3_path = result.mp3_path
    call.duration_seconds = result.duration_seconds
    call.repaired = result.repaired
    call.status = CallStatus.TRANSCRIBING
    job_store.update_call(
        job_id, call.index,
        mp3_path=call.mp3_path, duration_seconds=call.duration_seconds,
        repaired=call.repaired, status=call.status,
    )
    return call


async def _transcribe_one(job_id, call, defendant_name, speaker_assignment, engine) -> CallResult:
    job_store.update_call(job_id, call.index, status=CallStatus.TRANSCRIBING)
    channel_labels = _build_channel_labels(call, defendant_name, speaker_assignment)
    call.turns = await engine.transcribe(call.mp3_path, channel_labels=channel_labels)
    call.status = CallStatus.SUMMARIZING
    job_store.update_call(job_id, call.index, turns=call.turns, status=call.status)
    return call


async def _summarize_one(job_id, call, summary_prompt, skip_summary, engine, auto_message_mode=None) -> CallResult:
    """Summarize a single call's transcript using the provided engine instance."""
    job_store.update_call(job_id, call.index, status=CallStatus.SUMMARIZING)

    if skip_summary:
        summary_text = (
            f"{DUMMY_SUMMARY_PREFIX} FOR {call.filename}**\n\n"
            f"- The user requested to skip AI processing for this test job.\n"
            f"- Call duration: {call.duration_seconds} sec."
        )
        summary_json = None
        usage = TokenUsage()
        await asyncio.sleep(0.1)
    else:
        if engine is None:
            raise RuntimeError("Summarization engine not initialized")
        summary_text, summary_json, usage = await _summarize_with_engine(
            job_id, call, engine, summary_prompt or cfg.DEFAULT_SUMMARY_PROMPT, auto_message_mode,
        )

    call.summary = summary_text
    call.summary_json = summary_json
    call.status = CallStatus.GENERATING_PDF
    job_store.update_call(
        job_id, call.index, summary=summary_text, summary_json=summary_json, status=call.status, **usage.as_dict(),
    )
    return call


async def _summarize_with_engine(job_id, call, engine: SummarizationEngine, prompt, auto_message_mode):
    """Run one call through the engine; return ``(canonical_text, summary_json, usage)``.

    ``summary_json`` is the structured twin of the text
    (:func:`summaries.build_summary_json`): the normalized response plus the
    parsed cue items, or ``None`` when the text is unstructured.

    Automated-message filtering happens here because it mutates the
    transcript: prepass engines filter before the summary sees the turns;
    inline-detecting engines return markers with the summary and the
    transcript is filtered afterwards.
    """
    from .summaries import (
        build_summary_json,
        normalize_structured_summary,
        normalize_summary_text,
        render_summary_text,
    )
    from .system_audio import FILTER_MODES, apply_system_audio_filter, remove_system_audio_notes
    from .transcript_layout import compute_line_entries

    metadata = {"filename": call.filename, "duration_seconds": call.duration_seconds}
    duration = call.duration_seconds or 0.0
    filtering = auto_message_mode in FILTER_MODES
    usage = TokenUsage()

    def _apply_filter(markers):
        call.turns = apply_system_audio_filter(call.turns, markers, auto_message_mode)
        job_store.update_call(job_id, call.index, turns=call.turns)
        logger.info(
            "Applied system audio filter (%s) to %s: %d markers",
            auto_message_mode, call.filename, len(markers),
        )

    if filtering and engine.system_audio_prepass:
        markers, detect_usage = await engine.detect_system_audio(call.turns, metadata)
        usage += detect_usage
        if markers:
            _apply_filter(markers)

    result = await engine.summarize_call(
        call.turns, prompt, metadata,
        detect_system_audio=filtering and not engine.system_audio_prepass,
    )
    usage += result.usage

    if result.structured is not None:
        line_entries = compute_line_entries(call.turns, duration)
        normalized = normalize_structured_summary(result.structured, line_entries)
        summary_text = render_summary_text(normalized, line_entries)
        return summary_text, build_summary_json(summary_text, line_entries, structured=normalized), usage

    summary_text = result.text or ""
    if result.system_audio_markers:
        summary_text = remove_system_audio_notes(summary_text, result.system_audio_markers, call.turns)
        _apply_filter(result.system_audio_markers)
    line_entries = compute_line_entries(call.turns, duration)
    # Text-protocol engines (Gemma): text -> parsed -> structured, stored as JSON too.
    summary_text = normalize_summary_text(summary_text, line_entries)
    return summary_text, build_summary_json(summary_text, line_entries), usage


def _transcript_title_data(call: CallResult, job: Job) -> dict:
    """Cover-sheet facts for a call. The defendant falls back to the job's
    name when the ICM report did not supply one, matching the speaker labels."""
    stem = call_stem(call.index, call.filename)
    return {
        "CASE_NAME": job.case_name,
        "FILE_NAME": call.filename,
        "AUDIO_FILENAME": os.path.basename(call.mp3_path) if call.mp3_path else f"{stem}.mp3",
        "FILE_DURATION": format_duration(call.duration_seconds),
        "INMATE_NAME": call.inmate_name or job.defendant_name or "",
        "CALL_DATETIME": call.call_datetime_str or "",
        "FACILITY": call.facility or "",
        "OUTSIDE_NUMBER_FMT": call.outside_number_fmt or "",
        "CALL_OUTCOME": call.call_outcome or "",
        "NOTES": call.notes or "",
    }


async def _generate_pdf_one(job_id, call, job, transcripts_dir, transcripts_no_summary_dir, executor) -> CallResult:
    """Generate both PDF variants (with and without summary) for a single call."""
    from .delivery.call_view import build_call_view
    from .delivery.transcript_pdf import create_pdf

    title_data = _transcript_title_data(call, job)

    def _gen() -> str:
        view = build_call_view(call)
        pdf_path = os.path.join(transcripts_dir, f"{view.stem}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(create_pdf(view, title_data))
        with open(os.path.join(transcripts_no_summary_dir, f"{view.stem}.pdf"), "wb") as f:
            f.write(create_pdf(view, title_data, include_summary=False))
        return pdf_path

    call.pdf_path = await asyncio.get_event_loop().run_in_executor(executor, _gen)
    call.status = CallStatus.DONE
    job_store.update_call(job_id, call.index, pdf_path=call.pdf_path, status=call.status)
    return call


def _record_pdf_failure(job_id: str, call: CallResult, error: Exception | str) -> None:
    """Persist a PDF generation failure and keep the in-memory call in sync."""
    message = f"PDF failed: {error}"
    job_store.update_call(job_id, call.index, status=CallStatus.ERROR, error=message)
    call.status = CallStatus.ERROR
    call.error = message


# ────────────────────────── Job entry points ──────────────────────────

async def run_job(job_id: str) -> None:
    """Main pipeline entry point. Run from a background thread."""
    job = job_store.get_job(job_id)
    if not job:
        logger.error("Job not found: %s", job_id)
        return

    try:
        await _run_pipeline(job)
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        job = job_store.get_job(job_id)
        if job:
            job.stage = JobStage.ERROR
            job.error = str(e)
            job_store.update_job(job)
        emit(job_id, {"type": "error", "message": str(e)})
    finally:
        cleanup_event_queue(job_id)


async def repackage_job(job_id: str) -> None:
    """Regenerate the delivery assets and zip for a finished job (e.g. after editing summaries)."""
    job = job_store.get_job(job_id)
    if not job:
        return
    output_dir = job_store.get_job_output_dir(job_id)

    # Re-create the same summarization engine the job used so case-report
    # synthesis stays consistent between the original run and a re-package.
    runtime = _runtime_selection(job)
    engine = _make_summarization_engine(runtime, job.skip_summary)
    try:
        try:
            await _stage_generate_delivery_assets(job, output_dir, engine)
            zip_path = await _stage_package(job, output_dir)
        finally:
            if engine:
                engine.unload()
        job.zip_path = zip_path
        job_store.update_job(job)
        emit(job_id, {"type": "packaged", "zip_path": zip_path})
    except Exception as e:
        logger.error("Repackage failed: %s", e)
        emit(job_id, {"type": "error", "message": str(e)})


def _runtime_selection(job: Job) -> RuntimeSelection:
    return resolve_runtime_selection(
        job.transcription_engine,
        job.summarization_engine,
        skip_summary=job.skip_summary,
        auto_message_mode=job.auto_message_mode,
    )


def _make_summarization_engine(runtime: RuntimeSelection, skip_summary: bool) -> Optional[SummarizationEngine]:
    from .summarization import get_engine
    return None if skip_summary else get_engine(runtime.summarization_engine)


# ────────────────────────── Pipeline ──────────────────────────

@dataclass
class _Stage:
    """One worker pool in the assembly line."""
    name: str                      # progress key and SSE stage label
    job_stage: JobStage
    workers: int
    process: Callable[[CallResult], Awaitable[Optional[CallResult]]]
    # Called when `process` raises. Returns the call to forward downstream
    # (soft failure) or None to stop the call here.
    on_error: Callable[[CallResult, Exception], Optional[CallResult]]
    queue: asyncio.Queue
    downstream: Optional["_Stage"] = None


def _init_calls(job: Job, audio_files: List[str], icm_map: dict) -> None:
    """Add call records for files the job has not seen yet (resume keeps existing ones)."""
    existing = {c.original_path for c in job.calls}
    for i, path in enumerate(audio_files):
        if path in existing:
            continue
        meta = icm_map.get(os.path.basename(path))
        fields = asdict(meta) if meta else {}
        if not fields.get("call_date"):
            # No ICM record (or one without a date): the filename usually
            # carries the call's start time. Only the date fields are inferred.
            fields.update(filename_date_fields(os.path.basename(path)))
        job.calls.append(CallResult(
            index=i,
            filename=os.path.basename(path),
            original_path=path,
            **fields,
        ))


def _load_icm_metadata(job: Job) -> dict:
    if job.xml_metadata_path and os.path.exists(job.xml_metadata_path):
        icm_xml = job.xml_metadata_path
    else:
        icm_xml = find_icm_report(job.input_folder) if job.input_folder else None
    if not icm_xml:
        return {}

    try:
        icm_map = parse_icm_report(icm_xml)
    except Exception as e:
        logger.warning("ICM XML parsing failed: %s", e)
        emit(job.id, {"type": "warning", "message": (
            f"Metadata file found but could not be read: {os.path.basename(icm_xml)}. "
            "Call details (inmate name, phone, date) will be blank."
        )})
        return {}
    if icm_map:
        logger.info("ICM report loaded: %d records", len(icm_map))
    else:
        emit(job.id, {"type": "warning", "message": (
            f"Metadata file ({os.path.basename(icm_xml)}) contained no matching call records. "
            "Call details will be blank."
        )})
    return icm_map


async def _run_pipeline(job: Job) -> None:
    job_id = job.id
    output_dir = job_store.get_job_output_dir(job_id)
    audio_dir = os.path.join(output_dir, "audio")
    transcripts_dir = os.path.join(output_dir, "transcripts")
    transcripts_no_summary_dir = os.path.join(output_dir, "transcripts-no-summary")
    for d in (audio_dir, transcripts_dir, transcripts_no_summary_dir):
        os.makedirs(d, exist_ok=True)

    # ── Discover files & initialize call records ──
    emit(job_id, {"type": "stage", "stage": "discovering"})
    audio_files = job.file_paths or discover_audio_files(job.input_folder)
    if not audio_files:
        raise RuntimeError(
            f"No supported audio files found in: {job.input_folder} and no files explicitly provided."
        )
    emit(job_id, {"type": "discovered", "count": len(audio_files)})
    logger.info("Discovered %d audio files for job %s", len(audio_files), job_id)

    _init_calls(job, audio_files, _load_icm_metadata(job))
    job.stage = JobStage.CONVERTING
    job.started_at = job.started_at or _utc_now()
    job_store.update_job(job)
    job = job_store.get_job(job_id)  # reload DB-synced copies
    total_calls = len(job.calls)

    # ── Engines & worker counts ──
    runtime = _runtime_selection(job)
    validate_runtime_selection(runtime)
    from .transcription import get_engine as get_transcription_engine
    transcription_engine = get_transcription_engine(runtime.transcription_engine)
    summarization_engine = _make_summarization_engine(runtime, job.skip_summary)

    n_convert = max(1, (os.cpu_count() or 2) - 1)
    n_pdf = min(_MAX_PDF_WORKERS, max(1, total_calls))
    convert_executor = ThreadPoolExecutor(max_workers=n_convert)
    pdf_executor = ThreadPoolExecutor(max_workers=n_pdf)

    # ── Stage definitions ──
    def _fail(status_error_prefix: str):
        def handler(call: CallResult, exc: Exception) -> Optional[CallResult]:
            job_store.update_call(
                job_id, call.index, status=CallStatus.ERROR, error=f"{status_error_prefix}{exc}",
            )
            return None
        return handler

    def _summary_soft_fail(call: CallResult, exc: Exception) -> CallResult:
        # A failed summary must not block the transcript PDF: record the
        # error, stamp a placeholder summary, and keep the call moving.
        call.summary = "Summary unavailable for this call."
        call.summary_json = None
        call.status = CallStatus.GENERATING_PDF
        call.error = f"Summarization failed: {exc}"
        job_store.update_call(
            job_id, call.index, summary=call.summary, summary_json=None, status=call.status, error=call.error,
        )
        return call

    def _pdf_fail(call: CallResult, exc: Exception) -> None:
        _record_pdf_failure(job_id, call, exc)
        return None

    pdf = _Stage(
        "generating_pdf", JobStage.GENERATING, n_pdf,
        lambda c: _generate_pdf_one(job_id, c, job, transcripts_dir, transcripts_no_summary_dir, pdf_executor),
        _pdf_fail, asyncio.Queue(),
    )
    summary_prompt = effective_summary_prompt(job)  # stored prompt + case documents block
    summarize = _Stage(
        "summarizing", JobStage.SUMMARIZING, runtime.summarization_workers(total_calls),
        lambda c: _summarize_one(
            job_id, c, summary_prompt, job.skip_summary, summarization_engine,
            runtime.effective_auto_message_mode,
        ),
        _summary_soft_fail, asyncio.Queue(), downstream=pdf,
    )
    transcribe = _Stage(
        "transcribing", JobStage.TRANSCRIBING, runtime.transcription_workers(total_calls),
        lambda c: _transcribe_one(job_id, c, job.defendant_name, job.speaker_assignment, transcription_engine),
        _fail("Transcription failed: "), asyncio.Queue(), downstream=summarize,
    )
    convert = _Stage(
        "converting", JobStage.CONVERTING, n_convert,
        lambda c: _convert_one(job_id, c, audio_dir, convert_executor),
        _fail(""), asyncio.Queue(), downstream=transcribe,
    )

    # ── Route each call to the queue matching its checkpointed status ──
    for call in job.calls:
        if call.status in (CallStatus.PENDING, CallStatus.CONVERTING):
            await convert.queue.put(call)
        elif call.status == CallStatus.TRANSCRIBING and call.mp3_path and not call.turns:
            await transcribe.queue.put(call)
        elif call.status == CallStatus.SUMMARIZING and call.turns and not call.summary:
            await summarize.queue.put(call)
        elif call.status == CallStatus.GENERATING_PDF and call.turns:
            await pdf.queue.put(call)
        # DONE or ERROR calls: skip
    for _ in range(n_convert):
        await convert.queue.put(_SENTINEL)

    # ── Shared worker machinery ──
    progress = {stage.name: 0 for stage in (convert, transcribe, summarize, pdf)}
    stages_entered: set = set()

    def _advance_stage(stage: JobStage) -> None:
        """Update job.stage when the first call enters a new pipeline stage."""
        if stage.value not in stages_entered:
            stages_entered.add(stage.value)
            job_store.update_job_stage(job_id, stage)
            emit(job_id, {"type": "stage", "stage": stage.value, "total": total_calls})

    def _is_paused() -> bool:
        return job_store.get_job_stage(job_id) == JobStage.PAUSED.value

    async def _next_item(work_q: asyncio.Queue):
        """Next call, the sentinel, or None once the job is paused."""
        while True:
            if _is_paused():
                return None
            try:
                item = await asyncio.wait_for(work_q.get(), timeout=_QUEUE_POLL_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                continue
            if item is not _SENTINEL and _is_paused():
                await work_q.put(item)
                return None
            return item

    async def _worker(stage: _Stage) -> None:
        while True:
            item = await _next_item(stage.queue)
            if item is None or item is _SENTINEL:
                return
            _advance_stage(stage.job_stage)
            try:
                forward = await stage.process(item)
                status = forward.status if forward else item.status
            except Exception as e:
                logger.error("%s error for %s: %s", stage.name, item.filename, e)
                forward = stage.on_error(item, e)
                status = forward.status if forward else "error"
            progress[stage.name] += 1
            emit(job_id, {
                "type": "call_update", "index": item.index, "status": status,
                "stage": stage.name, "completed": progress[stage.name], "total": total_calls,
            })
            if _is_paused():
                return
            if forward is not None and stage.downstream is not None:
                await stage.downstream.queue.put(forward)

    async def _run_stage(stage: _Stage) -> None:
        """Run a stage's workers to completion, then release the downstream pool."""
        await asyncio.gather(*[_worker(stage) for _ in range(stage.workers)])
        if stage.downstream is not None:
            for _ in range(stage.downstream.workers):
                await stage.downstream.queue.put(_SENTINEL)

    # ── Execute ──
    # All-local mode runs in two phases so Parakeet and Gemma never share
    # unified memory; otherwise all four stages stream concurrently.
    if runtime.all_local:
        logger.info("All-local mode: running two-phase pipeline for job %s", job_id)
        phases = [(convert, transcribe), (summarize, pdf)]
    else:
        phases = [(convert, transcribe, summarize, pdf)]

    _advance_stage(JobStage.CONVERTING)
    try:
        for phase in phases:
            await asyncio.gather(*[_run_stage(stage) for stage in phase])
            if _is_paused():
                break
    finally:
        convert_executor.shutdown(wait=False)
        pdf_executor.shutdown(wait=False)

    if _is_paused():
        # Release a resident local model rather than holding memory while paused.
        if summarization_engine:
            summarization_engine.unload()
        return

    # ── Delivery assets + packaging ──
    # The summarization engine stays loaded: case-report synthesis reuses it.
    emit(job_id, {"type": "stage", "stage": "generating_indexes"})
    job = job_store.get_job(job_id)
    try:
        await _stage_generate_delivery_assets(job, output_dir, summarization_engine)
    finally:
        if summarization_engine:
            summarization_engine.unload()

    if _is_paused():
        return

    job.stage = JobStage.PACKAGING
    job_store.update_job(job)
    emit(job_id, {"type": "stage", "stage": "packaging"})
    zip_path = await _stage_package(job, output_dir)

    job.stage = JobStage.DONE
    job.zip_path = zip_path
    job.completed_at = _utc_now()
    job_store.update_job(job)
    emit(job_id, {"type": "done", "zip_path": zip_path})

    clean = sum(1 for c in job.calls if c.status == CallStatus.DONE and not c.error)
    partial = sum(1 for c in job.calls if c.status == CallStatus.DONE and c.error)
    errored = sum(1 for c in job.calls if c.status == CallStatus.ERROR)
    logger.info(
        "Job %s completed: %d clean, %d partial (summary failed), %d errored. Zip: %s",
        job_id, clean, partial, errored, zip_path,
    )


# ────────────────────────── Batch stages ──────────────────────────

async def _stage_generate_delivery_assets(
    job: Job,
    output_dir: str,
    summarization_engine: Optional[SummarizationEngine] = None,
    gen_date: Optional[str] = None,
    search_related: bool = True,
) -> None:
    """Write index.html, guide.pdf, case-report.pdf, and the app-assets/ indexes.

    The writers run in parallel: the shared Chromium renderer is safe to
    call from concurrent threads and gates real render concurrency with its
    own semaphore, so wall time is close to the slowest single asset. A
    failed asset is reported as a warning and does not fail the job.
    ``gen_date`` overrides the "Generated" stamp (tests pin it for the
    golden package); production leaves it as today. ``search_related``
    adds the related-words table (built with the embedding model) to the
    search index; the keyword index always ships.
    """
    from .delivery.call_view import build_call_views
    from .delivery.case_report import generate_case_report_pdf
    from .delivery.guide_pdf import generate_guide_pdf
    from .delivery.index_html import generate_index_html
    from .delivery.search_index import generate_search_index

    loop = asyncio.get_event_loop()
    done_calls = [c for c in job.calls if c.status == CallStatus.DONE]
    # One parse + layout + cue hydration per call, shared by every surface.
    views = await loop.run_in_executor(None, build_call_views, done_calls)

    def _write(name: str, data) -> None:
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(os.path.join(output_dir, name), mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
            f.write(data)

    writers = {
        "index.html": lambda: _write(
            "index.html", generate_index_html(views, case_name=job.case_name, gen_date=gen_date)
        ),
        "guide.pdf": lambda: _write(
            "guide.pdf", generate_guide_pdf(case_name=job.case_name, call_count=len(views), gen_date=gen_date)
        ),
        "case-report.pdf": lambda: _write(
            "case-report.pdf",
            generate_case_report_pdf(job=job, views=views, engine=summarization_engine, gen_date=gen_date),
        ),
        "app-assets/": lambda: generate_search_index(views, output_dir, related=search_related),
    }

    failures: List[str] = []

    async def _run(name: str, fn) -> None:
        try:
            await loop.run_in_executor(None, fn)
        except Exception as e:
            logger.error("Delivery asset generation failed (%s): %s", name, e)
            failures.append(name)
            emit(job.id, {"type": "warning", "message": f"{name} failed: {e}"})

    await asyncio.gather(*[_run(name, fn) for name, fn in writers.items()])
    if failures:
        logger.warning("Delivery asset generation completed with %d failure(s): %s", len(failures), ", ".join(failures))


def _safe_case_name(case_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in case_name).strip()
    return safe or "Jail Calls"


async def _stage_package(job: Job, output_dir: str) -> str:
    """Zip the output folder as ``<CaseName>/...`` and return the zip path."""
    def make_zip() -> str:
        safe_name = _safe_case_name(job.case_name)
        zip_path = os.path.join(job_store.job_dir(job.id), f"{safe_name}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(output_dir):
                for file in files:
                    abs_path = os.path.join(root, file)
                    zf.write(abs_path, os.path.join(safe_name, os.path.relpath(abs_path, output_dir)))
        return zip_path

    return await asyncio.get_event_loop().run_in_executor(None, make_zip)
