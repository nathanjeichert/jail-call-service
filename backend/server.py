"""FastAPI server for the jail-call-service.

  POST   /api/scan/folder                     List audio files under a local folder
  POST   /api/upload/audio                    Upload audio files
  POST   /api/upload/xml                      Upload an ICM report (returns a parsed preview)
  POST   /api/xml/preview                     Preview an ICM report already on disk
  GET    /api/config                          Engine availability, defaults, readiness checks
  POST   /api/jobs                            Create job
  GET    /api/jobs                            List jobs
  DELETE /api/jobs                            Delete completed/errored jobs
  GET    /api/jobs/{id}                       Job detail (calls without transcripts)
  DELETE /api/jobs/{id}                       Delete one job
  GET    /api/jobs/{id}/settings              Original creation settings (for re-runs)
  POST   /api/jobs/{id}/start|pause|resume    Control processing
  POST   /api/jobs/{id}/retry-errors          Re-queue errored calls
  GET    /api/jobs/{id}/events                SSE progress stream
  GET    /api/jobs/{id}/calls/{i}/transcript  Review transcript
  GET    /api/jobs/{id}/calls/{i}/summary     Review summary
  PUT    /api/jobs/{id}/calls/{i}/summary     Edit summary
  POST   /api/jobs/{id}/package               Rebuild delivery assets + zip
  GET    /api/jobs/{id}/download              Download zip
  GET    /health
"""

import asyncio
import json
import logging
import os
import queue
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import config as cfg
from . import events, job_store, pipeline
from .audio_converter import FFMPEG_PATH, discover_audio_files
from .icm_parser import parse_icm_report
from .job_settings import (
    compose_summary_prompt,
    extract_case_context,
    normalize_auto_message_mode,
    normalize_optional_name,
    resolve_default_engine,
)
from .models import AUDIO_EXTENSIONS, DEFAULT_SPEAKER_ASSIGNMENT, CallStatus, Job, normalize_speaker_assignment
from .summarization import AVAILABLE_ENGINES as SUMMARIZATION_ENGINES
from .transcription import AVAILABLE_ENGINES as TRANSCRIPTION_ENGINES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

XML_EXTENSIONS = {".xml"}
UPLOAD_CHUNK_SIZE = 1024 * 1024
DELETABLE_STAGES = ("created", "done", "error", "paused")
STARTABLE_STAGES = ("created", "error", "paused")
UNPAUSABLE_STAGES = ("done", "error", "created", "paused", "packaging")
PACKAGEABLE_STAGES = ("done", "error", "generating")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg.validate_api_keys()
    paused = job_store.pause_orphaned_jobs()
    for jid in paused:
        logger.info("Startup: paused orphaned job %s", jid)
    if paused:
        logger.info("Startup: %d in-progress job(s) paused — click Resume to continue", len(paused))
    yield


app = FastAPI(title="Jail Call Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ────────────────────────── Request models ──────────────────────────

class CreateJobRequest(BaseModel):
    case_name: Optional[str] = ""
    input_folder: Optional[str] = ""
    summary_prompt: Optional[str] = None
    defendant_name: Optional[str] = None
    skip_summary: bool = False
    file_paths: Optional[list[str]] = None
    xml_metadata_path: Optional[str] = None
    transcription_engine: Optional[str] = None
    summarization_engine: Optional[str] = None
    auto_message_mode: Optional[str] = None  # "exclude", "label", or None (keep)
    speaker_assignment: Optional[str] = None


class UpdateSummaryRequest(BaseModel):
    summary: str


class PathRequest(BaseModel):
    path: str


# ────────────────────────── Helpers ──────────────────────────

def _job_or_404(job_id: str) -> Job:
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_summary(job: Job) -> dict:
    return {
        "id": job.id,
        "case_name": job.case_name,
        "input_folder": job.input_folder,
        "stage": job.stage,
        "total_calls": len(job.calls),
        "done_calls": sum(1 for c in job.calls if c.status == CallStatus.DONE),
        "error_calls": sum(1 for c in job.calls if c.status == CallStatus.ERROR),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "has_zip": bool(job.zip_path) and os.path.exists(job.zip_path),
        "error": job.error,
        "defendant_name": job.defendant_name,
        "summary_prompt": job.summary_prompt,
        "speaker_assignment": normalize_speaker_assignment(job.speaker_assignment),
    }


_TRANSCRIPT_READY_STATUSES = {
    CallStatus.SUMMARIZING.value,
    CallStatus.GENERATING_PDF.value,
    CallStatus.DONE.value,
}


def _call_has_transcript(call) -> bool:
    if call.turns is not None:
        return len(call.turns) > 0
    return call.status in _TRANSCRIPT_READY_STATUSES or bool(call.summary) or bool(call.pdf_path)


def _call_summary(call) -> dict:
    return {
        "index": call.index,
        "filename": call.filename,
        "status": call.status,
        "duration_seconds": call.duration_seconds,
        "has_transcript": _call_has_transcript(call),
        "has_summary": bool(call.summary),
        "repaired": call.repaired,
        "error": call.error,
        "inmate_name": call.inmate_name,
        "call_datetime_str": call.call_datetime_str,
        "outside_number_fmt": call.outside_number_fmt,
        "facility": call.facility,
        "call_outcome": call.call_outcome,
    }


def _icm_preview(xml_path: str) -> dict:
    """Summarize an ICM report so the operator can confirm it and prefill job metadata."""
    icm_map = parse_icm_report(xml_path)
    if not icm_map:
        return {"parsed": False, "call_count": 0}

    metas = list(icm_map.values())
    dates = sorted(m.call_date for m in metas if m.call_date)
    return {
        "parsed": True,
        "call_count": len(metas),
        "inmate_names": sorted({m.inmate_name for m in metas if m.inmate_name and m.inmate_name != "INMATE"}),
        "facilities": sorted({m.facility for m in metas if m.facility}),
        "date_range": {"start": dates[0], "end": dates[-1]} if dates else None,
        "unique_numbers": len({m.outside_number for m in metas if m.outside_number}),
    }


async def _save_upload(upload: UploadFile, dest_dir: str, fallback_name: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(upload.filename or "") or fallback_name)
    try:
        with open(dest_path, "wb") as fp:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                fp.write(chunk)
    finally:
        await upload.close()
    return os.path.abspath(dest_path)


def _run_in_thread(coro) -> None:
    """Drive a pipeline coroutine on its own loop (FastAPI runs sync tasks in a worker thread)."""
    asyncio.run(coro)


# ────────────────────────── Inputs ──────────────────────────

@app.post("/api/scan/folder")
def scan_folder(req: PathRequest):
    """List audio files in a local directory. Returns absolute paths."""
    folder = req.path.strip()
    if not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail=f"Not a valid folder: {folder}")
    return {"paths": discover_audio_files(folder)}


@app.post("/api/upload/audio")
async def upload_audio(files: list[UploadFile] = File(...)):
    """Accept multiple audio files, save to uploads/<uuid>/, return absolute paths."""
    dest_dir = os.path.join(cfg.UPLOADS_DIR, str(uuid.uuid4()))
    saved_paths: list[str] = []
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported audio format: {f.filename}")
        saved_paths.append(await _save_upload(f, dest_dir, f"audio_{len(saved_paths)}{ext}"))
    return {"paths": saved_paths}


@app.post("/api/upload/xml")
async def upload_xml(file: UploadFile = File(...)):
    """Accept an ICM report XML, save it, and return its path plus a parsed preview."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in XML_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Expected an XML file, got: {file.filename}")
    abs_path = await _save_upload(file, os.path.join(cfg.UPLOADS_DIR, str(uuid.uuid4())), "metadata.xml")
    return {"path": abs_path, "preview": _icm_preview(abs_path)}


@app.post("/api/xml/preview")
def preview_xml(req: PathRequest):
    """Parse an ICM report already on disk (pasted path) and summarize it."""
    path = req.path.strip()
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"Not a valid file: {path}")
    return {"path": os.path.abspath(path), "preview": _icm_preview(path)}


@app.get("/api/config")
def get_config():
    """Safe config and readiness checks for the frontend."""
    return {
        "assemblyai_configured": bool(cfg.ASSEMBLYAI_API_KEY),
        "gemini_configured": bool(cfg.GEMINI_API_KEY),
        "ffmpeg_found": bool(FFMPEG_PATH),
        "ffmpeg_path": FFMPEG_PATH or "",
        "default_summary_prompt": cfg.DEFAULT_SUMMARY_PROMPT,
        "gemini_model": cfg.GEMINI_MODEL,
        "default_transcription_engine": resolve_default_engine(cfg.DEFAULT_TRANSCRIPTION_ENGINE, TRANSCRIPTION_ENGINES),
        "available_transcription_engines": TRANSCRIPTION_ENGINES,
        "default_summarization_engine": resolve_default_engine(cfg.DEFAULT_SUMMARIZATION_ENGINE, SUMMARIZATION_ENGINES),
        "available_summarization_engines": SUMMARIZATION_ENGINES,
    }


# ────────────────────────── Jobs ──────────────────────────

@app.post("/api/jobs", status_code=201)
def create_job(req: CreateJobRequest):
    if not req.file_paths and not os.path.isdir(req.input_folder or ""):
        raise HTTPException(status_code=400, detail=f"Input folder not found: {req.input_folder}")
    file_paths = [p.strip() for p in (req.file_paths or []) if p and p.strip()]

    job = job_store.create_job(
        case_name=(req.case_name or "").strip(),
        input_folder=req.input_folder or "",
        summary_prompt=compose_summary_prompt(req.summary_prompt),
        defendant_name=req.defendant_name,
        skip_summary=req.skip_summary,
        file_paths=file_paths or None,
        xml_metadata_path=req.xml_metadata_path,
        transcription_engine=normalize_optional_name(req.transcription_engine),
        summarization_engine=normalize_optional_name(req.summarization_engine),
        auto_message_mode=(
            normalize_auto_message_mode(req.auto_message_mode) if req.auto_message_mode is not None else "label"
        ),
        speaker_assignment=normalize_speaker_assignment(req.speaker_assignment),
    )
    return _job_summary(job)


@app.get("/api/jobs")
def list_jobs():
    return [_job_summary(j) for j in job_store.list_jobs()]


@app.delete("/api/jobs")
def clear_completed_jobs():
    return {"deleted": job_store.delete_completed_jobs()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = job_store.get_job_lite(job_id)  # skips the transcript JSON blobs
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    summary = _job_summary(job)
    summary["calls"] = [_call_summary(c) for c in sorted(job.calls, key=lambda c: c.index)]
    return summary


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = _job_or_404(job_id)
    if job.stage not in DELETABLE_STAGES:
        raise HTTPException(status_code=409, detail=f"Cannot delete job in stage: {job.stage}")
    job_store.delete_job(job_id)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/settings")
def get_job_settings(job_id: str):
    """Original creation settings, for re-running a job."""
    job = _job_or_404(job_id)
    return {
        "case_name": job.case_name,
        "defendant_name": job.defendant_name or "",
        "input_folder": job.input_folder or "",
        "file_paths": job.file_paths or [],
        "summary_prompt": extract_case_context(job.summary_prompt),
        "xml_metadata_path": job.xml_metadata_path or "",
        "skip_summary": job.skip_summary,
        "transcription_engine": job.transcription_engine or "",
        "summarization_engine": job.summarization_engine or "",
        "auto_message_mode": job.auto_message_mode or "",
        "speaker_assignment": normalize_speaker_assignment(job.speaker_assignment or DEFAULT_SPEAKER_ASSIGNMENT),
    }


# ────────────────────────── Processing control ──────────────────────────

@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, background_tasks: BackgroundTasks):
    job = _job_or_404(job_id)
    if job.stage not in STARTABLE_STAGES:
        raise HTTPException(status_code=409, detail=f"Job is already in stage: {job.stage}")
    background_tasks.add_task(_run_in_thread, pipeline.run_job(job_id))
    return {"status": "started", "job_id": job_id}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    job = _job_or_404(job_id)
    if job.stage in UNPAUSABLE_STAGES:
        raise HTTPException(status_code=409, detail=f"Cannot pause job in stage: {job.stage}")
    events.emit(job_id, {"type": "stage", "stage": "paused"})
    job.stage = "paused"
    job_store.update_job(job)
    return {"status": "paused", "job_id": job_id}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    job = _job_or_404(job_id)
    if job.stage != "paused":
        raise HTTPException(status_code=409, detail=f"Job is not paused (stage: {job.stage})")
    background_tasks.add_task(_run_in_thread, pipeline.run_job(job_id))
    return {"status": "resumed", "job_id": job_id}


@app.post("/api/jobs/{job_id}/retry-errors")
def retry_errors(job_id: str, background_tasks: BackgroundTasks):
    """Reset errored calls to the stage they can resume from, then re-run the pipeline."""
    job = _job_or_404(job_id)
    for c in job.calls:
        if c.status != CallStatus.ERROR:
            continue
        if not c.mp3_path:
            c.status = CallStatus.PENDING
        elif not c.turns:
            c.status = CallStatus.TRANSCRIBING
        elif not c.summary:
            c.status = CallStatus.SUMMARIZING
        else:
            c.status = CallStatus.GENERATING_PDF
        c.error = None

    job.stage = "converting"  # the pipeline skips whatever is already done
    job.error = None
    job_store.update_job(job)
    events.emit(job_id, {"type": "stage", "stage": "retrying_errors"})
    background_tasks.add_task(_run_in_thread, pipeline.run_job(job_id))
    return {"status": "retrying", "job_id": job_id}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    _job_or_404(job_id)
    q = events.get_event_queue(job_id)

    async def event_generator():
        loop = asyncio.get_event_loop()
        while True:
            try:
                # q is a thread-safe stdlib queue: read via executor so the
                # event loop is never blocked.
                event = await asyncio.wait_for(loop.run_in_executor(None, q.get, True, 25), timeout=30)
                yield {"data": json.dumps(event)}
                if event.get("type") in ("done", "error"):
                    break
            except (asyncio.TimeoutError, queue.Empty):
                yield {"data": json.dumps({"type": "ping"})}

    return EventSourceResponse(event_generator())


# ────────────────────────── Review ──────────────────────────

@app.get("/api/jobs/{job_id}/calls/{call_index}/transcript")
def get_transcript(job_id: str, call_index: int):
    call = job_store.get_call(job_id, call_index)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.turns:
        raise HTTPException(status_code=404, detail="Transcript not yet available")
    return {
        "index": call.index,
        "filename": call.filename,
        "duration_seconds": call.duration_seconds,
        "turns": [t.model_dump() for t in call.turns],
    }


@app.get("/api/jobs/{job_id}/calls/{call_index}/summary")
def get_summary(job_id: str, call_index: int):
    call = job_store.get_call(job_id, call_index)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"index": call.index, "filename": call.filename, "summary": call.summary or ""}


@app.put("/api/jobs/{job_id}/calls/{call_index}/summary")
def update_summary(job_id: str, call_index: int, req: UpdateSummaryRequest):
    if not job_store.get_call(job_id, call_index):
        raise HTTPException(status_code=404, detail="Call not found")
    job_store.update_call(job_id, call_index, summary=req.summary)
    return {"index": call_index, "summary": req.summary}


# ────────────────────────── Delivery ──────────────────────────

@app.post("/api/jobs/{job_id}/package")
def package_job(job_id: str, background_tasks: BackgroundTasks):
    """Regenerate delivery assets and the zip (useful after editing summaries)."""
    job = _job_or_404(job_id)
    if job.stage not in PACKAGEABLE_STAGES:
        raise HTTPException(status_code=409, detail="Job must be complete before packaging")
    background_tasks.add_task(_run_in_thread, pipeline.repackage_job(job_id))
    return {"status": "packaging", "job_id": job_id}


@app.get("/api/jobs/{job_id}/download")
def download_zip(job_id: str):
    job = _job_or_404(job_id)
    if not job.zip_path or not os.path.exists(job.zip_path):
        raise HTTPException(status_code=404, detail="Zip not yet generated")
    return FileResponse(job.zip_path, media_type="application/zip", filename=os.path.basename(job.zip_path))


@app.get("/health")
def health():
    return {"status": "ok"}
