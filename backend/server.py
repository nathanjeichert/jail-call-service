"""
FastAPI server for the jail-call-service.

Endpoints:
  POST   /api/jobs                          Create job
  GET    /api/jobs                          List jobs
  GET    /api/jobs/{id}                     Job detail
  POST   /api/jobs/{id}/start               Start processing
  GET    /api/jobs/{id}/events              SSE progress stream
  GET    /api/jobs/{id}/calls/{i}/transcript  Review transcript text
  GET    /api/jobs/{id}/calls/{i}/summary     Review summary
  PUT    /api/jobs/{id}/calls/{i}/summary     Edit summary
  POST   /api/jobs/{id}/package             Re-package zip
  GET    /api/jobs/{id}/download            Download zip
"""

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import job_store, pipeline
from .models import Job, CallStatus
from . import config as cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Jail Call Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response models ──

class CreateJobRequest(BaseModel):
    case_name: str
    input_folder: Optional[str] = ""
    summary_prompt: Optional[str] = None
    defendant_name: Optional[str] = None
    skip_summary: bool = False
    file_paths: Optional[list[str]] = None
    xml_metadata_path: Optional[str] = None


class UpdateSummaryRequest(BaseModel):
    summary: str


# ── Helpers ──

def _job_or_404(job_id: str) -> Job:
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _call_or_404(job: Job, call_index: int):
    call = next((c for c in job.calls if c.index == call_index), None)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


def _job_summary(job: Job) -> dict:
    total = len(job.calls)
    done = sum(1 for c in job.calls if c.status == CallStatus.DONE)
    errors = sum(1 for c in job.calls if c.status == CallStatus.ERROR)
    return {
        "id": job.id,
        "case_name": job.case_name,
        "input_folder": job.input_folder,
        "stage": job.stage,
        "total_calls": total,
        "done_calls": done,
        "error_calls": errors,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "has_zip": job.zip_path is not None and os.path.exists(job.zip_path or ""),
        "error": job.error,
        "defendant_name": job.defendant_name,
    }


def _call_summary(call) -> dict:
    return {
        "index": call.index,
        "filename": call.filename,
        "status": call.status,
        "duration_seconds": call.duration_seconds,
        "has_transcript": call.turns is not None and len(call.turns) > 0,
        "has_summary": bool(call.summary),
        "repaired": call.repaired,
        "error": call.error,
        "inmate_name": call.inmate_name,
        "call_datetime_str": call.call_datetime_str,
        "outside_number_fmt": call.outside_number_fmt,
        "facility": call.facility,
        "call_outcome": call.call_outcome,
    }


# ── Endpoints ──

def _run_tk_dialog(fn):
    """Run a tkinter dialog on a dedicated thread (tkinter must be on its own thread)."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result(timeout=120)


@app.get("/api/browse/folder")
def browse_folder():
    """Opens a native OS folder/file dialog on the server and returns the path(s)."""
    def _open_dialog():
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        choice = messagebox.askyesno(
            "Selection Type",
            "Do you want to scan an entire folder for WAVs?\n\n"
            "(Click 'Yes' for a folder, or 'No' to pick specific audio files)",
            parent=root,
        )

        if choice:
            path = filedialog.askdirectory(title="Select Folder", parent=root)
        else:
            paths = filedialog.askopenfilenames(
                title="Select Audio Files",
                filetypes=(("WAV Files", "*.wav"), ("Audio Files", "*.mp3 *.m4a"), ("All Files", "*.*")),
                parent=root,
            )
            path = ",\n".join(paths) if paths else ""

        root.destroy()
        return path

    try:
        path = _run_tk_dialog(_open_dialog)
    except Exception:
        path = ""
    return {"path": path}


@app.get("/api/browse/file")
def browse_file():
    """Opens a native OS file dialog on the server and returns the path."""
    def _open_dialog():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askopenfilename(
            title="Select File",
            filetypes=(("XML Files", "*.xml"), ("WAV Files", "*.wav"), ("All Files", "*.*")),
        )
        root.destroy()
        return path

    try:
        path = _run_tk_dialog(_open_dialog)
    except Exception:
        path = ""
    return {"path": path}

@app.post("/api/jobs", status_code=201)
def create_job(req: CreateJobRequest):
    if not req.file_paths and not os.path.isdir(req.input_folder or ""):
        raise HTTPException(status_code=400, detail=f"Input folder not found: {req.input_folder}")
    job = job_store.create_job(
        case_name=req.case_name,
        input_folder=req.input_folder or "",
        summary_prompt=req.summary_prompt or cfg.DEFAULT_SUMMARY_PROMPT,
        defendant_name=req.defendant_name,
        skip_summary=req.skip_summary,
        file_paths=req.file_paths,
        xml_metadata_path=req.xml_metadata_path,
    )
    return _job_summary(job)


@app.get("/api/jobs")
def list_jobs():
    return [_job_summary(j) for j in job_store.list_jobs()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _job_or_404(job_id)
    summary = _job_summary(job)
    summary["calls"] = [_call_summary(c) for c in sorted(job.calls, key=lambda c: c.index)]
    return summary


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, background_tasks: BackgroundTasks):
    job = _job_or_404(job_id)
    if job.stage not in ("created", "error", "paused"):
        raise HTTPException(status_code=409, detail=f"Job is already in stage: {job.stage}")

    background_tasks.add_task(_run_job_async, job_id)
    return {"status": "started", "job_id": job_id}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    job = _job_or_404(job_id)
    if job.stage in ("done", "error", "created", "paused", "packaging"):
        raise HTTPException(status_code=409, detail=f"Cannot pause job in stage: {job.stage}")
    
    pipeline._emit(job_id, {"type": "stage", "stage": "paused"})
    job.stage = "paused"
    job_store.update_job(job)
    return {"status": "paused", "job_id": job_id}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    job = _job_or_404(job_id)
    if job.stage != "paused":
        raise HTTPException(status_code=409, detail=f"Job is not paused, mostly in: {job.stage}")
        
    background_tasks.add_task(_run_job_async, job_id)
    return {"status": "resumed", "job_id": job_id}


@app.post("/api/jobs/{job_id}/retry-errors")
def retry_errors(job_id: str, background_tasks: BackgroundTasks):
    job = _job_or_404(job_id)
    
    # reset all error calls to appropriate stage
    for c in job.calls:
        if c.status == CallStatus.ERROR:
            if not c.mp3_path:
                new_status = CallStatus.PENDING
            elif not c.turns:
                new_status = CallStatus.TRANSCRIBING
            elif not c.summary:
                new_status = CallStatus.SUMMARIZING
            else:
                new_status = CallStatus.GENERATING_PDF
                
            c.status = new_status
            c.error = None
    
    job.stage = "converting" # The pipeline will skip what's already done
    job.error = None
    job_store.update_job(job)
    
    pipeline._emit(job_id, {"type": "stage", "stage": "retrying_errors"})
    background_tasks.add_task(_run_job_async, job_id)
    return {"status": "retrying", "job_id": job_id}


def _run_job_async(job_id: str):
    """Run the pipeline in a new event loop (called from a background thread)."""
    asyncio.run(pipeline.run_job(job_id))


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    _job_or_404(job_id)
    q = pipeline.get_event_queue(job_id)

    async def event_generator():
        import queue as _queue
        loop = asyncio.get_event_loop()
        while True:
            try:
                # q is a thread-safe stdlib queue.Queue — read via executor
                # so we don't block the event loop.
                event = await asyncio.wait_for(
                    loop.run_in_executor(None, q.get, True, 25),
                    timeout=30,
                )
                yield {"data": json.dumps(event)}
                if event.get("type") in ("done", "error"):
                    break
            except (asyncio.TimeoutError, _queue.Empty):
                yield {"data": json.dumps({"type": "ping"})}

    return EventSourceResponse(event_generator())


@app.get("/api/jobs/{job_id}/calls/{call_index}/transcript")
def get_transcript(job_id: str, call_index: int):
    job = _job_or_404(job_id)
    call = _call_or_404(job, call_index)
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
    job = _job_or_404(job_id)
    call = _call_or_404(job, call_index)
    return {
        "index": call.index,
        "filename": call.filename,
        "summary": call.summary or "",
    }


@app.put("/api/jobs/{job_id}/calls/{call_index}/summary")
def update_summary(job_id: str, call_index: int, req: UpdateSummaryRequest):
    job = _job_or_404(job_id)
    call = _call_or_404(job, call_index)
    job_store.update_call(job_id, call_index, summary=req.summary)
    return {"index": call.index, "summary": req.summary}


@app.post("/api/jobs/{job_id}/package")
def package_job(job_id: str, background_tasks: BackgroundTasks):
    """Re-generate output files and zip (useful after editing summaries)."""
    job = _job_or_404(job_id)
    if job.stage not in ("done", "error", "generating"):
        raise HTTPException(status_code=409, detail="Job must be complete before packaging")

    background_tasks.add_task(_repackage_async, job_id)
    return {"status": "packaging", "job_id": job_id}


def _repackage_async(job_id: str):
    asyncio.run(_do_repackage(job_id))


async def _do_repackage(job_id: str):
    from .pipeline import _stage_generate_indexes, _stage_package
    job = job_store.get_job(job_id)
    if not job:
        return
    output_dir = job_store.get_job_output_dir(job_id)
    audio_dir = os.path.join(output_dir, "audio")

    try:
        await _stage_generate_indexes(job, output_dir, audio_dir)
        zip_path = await _stage_package(job, output_dir)
        job.zip_path = zip_path
        job_store.update_job(job)
        pipeline._emit(job_id, {"type": "packaged", "zip_path": zip_path})
    except Exception as e:
        logger.error("Repackage failed: %s", e)
        pipeline._emit(job_id, {"type": "error", "message": str(e)})


@app.get("/api/jobs/{job_id}/download")
def download_zip(job_id: str):
    job = _job_or_404(job_id)
    if not job.zip_path or not os.path.exists(job.zip_path):
        raise HTTPException(status_code=404, detail="Zip not yet generated")
    filename = os.path.basename(job.zip_path)
    return FileResponse(
        job.zip_path,
        media_type="application/zip",
        filename=filename,
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Delete a job and all its output files."""
    job = _job_or_404(job_id)
    if job.stage not in ("created", "done", "error"):
        raise HTTPException(status_code=409, detail="Cannot delete a running job. Pause or wait for it to finish.")
    job_store.delete_job(job_id)
    return {"status": "deleted", "job_id": job_id}


@app.get("/api/config")
def get_config():
    """Return safe config and readiness checks for the frontend."""
    from .audio_converter import FFMPEG_PATH
    return {
        "assemblyai_configured": bool(cfg.ASSEMBLYAI_API_KEY),
        "gemini_configured": bool(cfg.GEMINI_API_KEY),
        "ffmpeg_found": bool(FFMPEG_PATH),
        "ffmpeg_path": FFMPEG_PATH or "",
        "default_summary_prompt": cfg.DEFAULT_SUMMARY_PROMPT,
        "gemini_model": cfg.GEMINI_MODEL,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
