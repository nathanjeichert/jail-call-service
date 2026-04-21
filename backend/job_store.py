"""
Job state persistence via SQLite.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import joinedload

from .models import (
    DEFAULT_SPEAKER_ASSIGNMENT,
    Job,
    JobStage,
    CallResult,
    CallStatus,
    normalize_speaker_assignment,
)
from . import config as cfg
from .db import SessionLocal, engine, Base
from .db_models import DBJob, DBCall

logger = logging.getLogger(__name__)

# Initialize DB tables
Base.metadata.create_all(bind=engine)


def _ensure_schema_columns() -> None:
    """Add lightweight columns that older local SQLite files may be missing."""
    with engine.begin() as conn:
        job_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(jobs)"))
        }
        if "speaker_assignment" not in job_columns:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN speaker_assignment VARCHAR"))


_ensure_schema_columns()

def _job_dir(job_id: str) -> str:
    d = os.path.join(cfg.JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_turns(turns):
    if turns is None:
        return None
    return [t.model_dump() if hasattr(t, "model_dump") else t for t in turns]


def _apply_job_fields(db_job: DBJob, job: Job) -> None:
    db_job.case_name = job.case_name
    db_job.input_folder = job.input_folder
    db_job.summary_prompt = job.summary_prompt
    db_job.stage = job.stage.value if isinstance(job.stage, JobStage) else job.stage
    db_job.created_at = job.created_at
    db_job.started_at = job.started_at
    db_job.completed_at = job.completed_at
    db_job.zip_path = job.zip_path
    db_job.error = job.error
    db_job.defendant_name = job.defendant_name
    db_job.skip_summary = job.skip_summary
    db_job.file_paths = job.file_paths
    db_job.xml_metadata_path = job.xml_metadata_path
    db_job.transcription_engine = job.transcription_engine
    db_job.summarization_engine = job.summarization_engine
    db_job.auto_message_mode = job.auto_message_mode
    db_job.speaker_assignment = normalize_speaker_assignment(job.speaker_assignment)


def _apply_call_fields(db_call: DBCall, call: CallResult) -> None:
    db_call.index = call.index
    db_call.filename = call.filename
    db_call.original_path = call.original_path
    db_call.mp3_path = call.mp3_path
    db_call.duration_seconds = call.duration_seconds
    db_call.turns = _serialize_turns(call.turns)
    db_call.summary = call.summary
    db_call.pdf_path = call.pdf_path
    db_call.status = call.status.value if isinstance(call.status, CallStatus) else call.status
    db_call.error = call.error
    db_call.repaired = call.repaired
    db_call.inmate_name = call.inmate_name
    db_call.inmate_pin = call.inmate_pin
    db_call.outside_number = call.outside_number
    db_call.outside_number_fmt = call.outside_number_fmt
    db_call.call_date = call.call_date
    db_call.call_time = call.call_time
    db_call.call_datetime_str = call.call_datetime_str
    db_call.facility = call.facility
    db_call.call_outcome = call.call_outcome
    db_call.call_type = call.call_type
    db_call.xml_duration_seconds = call.xml_duration_seconds
    db_call.notes = call.notes
    db_call.input_tokens = call.input_tokens
    db_call.output_tokens = call.output_tokens
    db_call.thinking_tokens = call.thinking_tokens


def _job_query(include_turns: bool):
    options = [joinedload(DBJob.calls)]
    if not include_turns:
        options = [joinedload(DBJob.calls).defer(DBCall.turns)]
    return options


def _map_single_call(c: DBCall, *, include_turns: bool = True) -> CallResult:
    """Map a single DB Call to a Pydantic CallResult."""
    return CallResult(
        index=c.index,
        filename=c.filename,
        original_path=c.original_path,
        mp3_path=c.mp3_path,
        duration_seconds=c.duration_seconds,
        turns=c.turns if include_turns else None,
        summary=c.summary,
        pdf_path=c.pdf_path,
        status=c.status,
        error=c.error,
        repaired=c.repaired,
        inmate_name=c.inmate_name,
        inmate_pin=c.inmate_pin,
        outside_number=c.outside_number,
        outside_number_fmt=c.outside_number_fmt,
        call_date=c.call_date,
        call_time=c.call_time,
        call_datetime_str=c.call_datetime_str,
        facility=c.facility,
        call_outcome=c.call_outcome,
        call_type=c.call_type,
        xml_duration_seconds=c.xml_duration_seconds,
        notes=c.notes,
        input_tokens=c.input_tokens,
        output_tokens=c.output_tokens,
        thinking_tokens=c.thinking_tokens,
    )


def _map_to_pydantic(db_job: DBJob, *, include_turns: bool = True) -> Job:
    """Map DB Job (with calls) to Pydantic Job."""
    calls = [_map_single_call(c, include_turns=include_turns) for c in db_job.calls]

    return Job(
        id=db_job.id,
        case_name=db_job.case_name,
        input_folder=db_job.input_folder,
        summary_prompt=db_job.summary_prompt,
        stage=db_job.stage,
        created_at=db_job.created_at,
        started_at=db_job.started_at,
        completed_at=db_job.completed_at,
        zip_path=db_job.zip_path,
        error=db_job.error,
        defendant_name=db_job.defendant_name,
        skip_summary=db_job.skip_summary,
        file_paths=db_job.file_paths,
        xml_metadata_path=db_job.xml_metadata_path,
        transcription_engine=db_job.transcription_engine,
        summarization_engine=db_job.summarization_engine,
        auto_message_mode=db_job.auto_message_mode,
        speaker_assignment=normalize_speaker_assignment(
            getattr(db_job, "speaker_assignment", None) or DEFAULT_SPEAKER_ASSIGNMENT
        ),
        calls=calls,
    )


def create_job(case_name: str, input_folder: str, summary_prompt: str, defendant_name: Optional[str] = None, skip_summary: bool = False, file_paths: Optional[List[str]] = None, xml_metadata_path: Optional[str] = None, transcription_engine: Optional[str] = None, summarization_engine: Optional[str] = None, auto_message_mode: Optional[str] = None, speaker_assignment: Optional[str] = None) -> Job:
    job_id = str(uuid.uuid4())
    _job_dir(job_id)  # ensure the folder exists for output

    with SessionLocal() as db:
        new_job = DBJob(
            id=job_id,
            case_name=case_name,
            input_folder=input_folder,
            summary_prompt=summary_prompt,
            stage=JobStage.CREATED.value,
            created_at=_utc_now_iso(),
            defendant_name=defendant_name,
            skip_summary=skip_summary,
            file_paths=file_paths,
            xml_metadata_path=xml_metadata_path,
            transcription_engine=transcription_engine,
            summarization_engine=summarization_engine,
            auto_message_mode=auto_message_mode,
            speaker_assignment=normalize_speaker_assignment(speaker_assignment),
        )
        db.add(new_job)
        db.commit()
        db.refresh(new_job)
        return _map_to_pydantic(new_job)


def get_job(job_id: str) -> Optional[Job]:
    with SessionLocal() as db:
        db_job = (
            db.query(DBJob)
            .options(*_job_query(include_turns=True))
            .filter(DBJob.id == job_id)
            .first()
        )
        if not db_job:
            return None
        return _map_to_pydantic(db_job)


def get_call(job_id: str, call_index: int) -> Optional[CallResult]:
    """Fetch a single call without loading the entire job's data."""
    with SessionLocal() as db:
        db_call = db.query(DBCall).filter(
            DBCall.job_id == job_id, DBCall.index == call_index
        ).first()
        if not db_call:
            return None
        return _map_single_call(db_call)


def get_job_lite(job_id: str) -> Optional[Job]:
    """Load job + call metadata WITHOUT the heavy turns JSON column."""
    with SessionLocal() as db:
        db_job = (
            db.query(DBJob)
            .options(*_job_query(include_turns=False))
            .filter(DBJob.id == job_id)
            .first()
        )
        if not db_job:
            return None
        return _map_to_pydantic(db_job, include_turns=False)


def list_jobs() -> List[Job]:
    with SessionLocal() as db:
        db_jobs = (
            db.query(DBJob)
            .options(*_job_query(include_turns=False))
            .order_by(DBJob.created_at.desc())
            .all()
        )
        return [_map_to_pydantic(j, include_turns=False) for j in db_jobs]

_ACTIVE_STAGES = {"converting", "transcribing", "summarizing", "generating", "packaging"}


def pause_orphaned_jobs() -> List[str]:
    """
    On server startup, mark any in-progress jobs as paused.

    When the server shuts down mid-pipeline, background tasks are killed but
    the DB still shows the job in its last active stage.  Without this, the
    job appears to be running in the UI and a stray click can resume expensive
    API calls.  Calling this at startup forces an explicit Resume to continue.

    Returns the list of job IDs that were paused.
    """
    with SessionLocal() as db:
        rows = db.query(DBJob).filter(DBJob.stage.in_(_ACTIVE_STAGES)).all()
        ids = [r.id for r in rows]
        for r in rows:
            r.stage = "paused"
        if ids:
            db.commit()
    return ids


def update_job_stage(job_id: str, stage) -> None:
    """Lightweight update: only change the job stage column without touching calls."""
    with SessionLocal() as db:
        db_job = db.query(DBJob).filter(DBJob.id == job_id).first()
        if db_job:
            db_job.stage = stage.value if isinstance(stage, JobStage) else stage
            db.commit()


def get_job_stage(job_id: str) -> Optional[str]:
    """Fetch only the stage column — avoids loading calls or transcript blobs."""
    with SessionLocal() as db:
        row = db.query(DBJob.stage).filter(DBJob.id == job_id).first()
        return row[0] if row else None


def update_job(job: Job) -> None:
    """Updates a job and all its calls using the passed Pydantic Job model."""
    with SessionLocal() as db:
        db_job = (
            db.query(DBJob)
            .options(joinedload(DBJob.calls))
            .filter(DBJob.id == job.id)
            .first()
        )
        if not db_job:
            logger.warning("Tried to update non-existent job: %s", job.id)
            return

        _apply_job_fields(db_job, job)

        # Upsert calls
        existing_calls = {c.index: c for c in db_job.calls}

        for c in job.calls:
            if c.index in existing_calls:
                _apply_call_fields(existing_calls[c.index], c)
            else:
                db_c = DBCall(job_id=job.id)
                _apply_call_fields(db_c, c)
                db.add(db_c)

        db.commit()


_CALL_FIELDS = {column.name for column in DBCall.__table__.columns}


def update_call(job_id: str, call_index: int, **kwargs) -> None:
    """Granular update of a specific call without fetching the full job Pydantic object."""
    with SessionLocal() as db:
        db_call = db.query(DBCall).filter(DBCall.job_id == job_id, DBCall.index == call_index).first()
        if not db_call:
            return

        for k, v in kwargs.items():
            if k not in _CALL_FIELDS:
                logger.warning("Ignoring unknown call field update: %s", k)
                continue
            if k == "turns":
                setattr(db_call, k, _serialize_turns(v))
            elif k == 'status' and hasattr(v, 'value'):
                setattr(db_call, k, v.value)
            else:
                setattr(db_call, k, v)

        db.commit()

def delete_job(job_id: str) -> bool:
    """Delete a job and all its calls from the database. Returns True if found and deleted."""
    import shutil
    with SessionLocal() as db:
        db_job = db.query(DBJob).filter(DBJob.id == job_id).first()
        if not db_job:
            return False
        db.query(DBCall).filter(DBCall.job_id == job_id).delete()
        db.delete(db_job)
        db.commit()

    # Clean up the job's output directory
    job_dir = os.path.join(cfg.JOBS_DIR, job_id)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)

    return True


def delete_completed_jobs() -> int:
    """Delete all jobs with stage 'done' or 'error'. Returns count deleted."""
    import shutil
    with SessionLocal() as db:
        completed = db.query(DBJob).filter(DBJob.stage.in_(["done", "error"])).all()
        job_ids = [j.id for j in completed]
        if not job_ids:
            return 0
        db.query(DBCall).filter(DBCall.job_id.in_(job_ids)).delete(synchronize_session=False)
        db.query(DBJob).filter(DBJob.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()

    # Clean up output directories
    for jid in job_ids:
        job_dir = os.path.join(cfg.JOBS_DIR, jid)
        if os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)

    return len(job_ids)



def get_job_output_dir(job_id: str) -> str:
    d = os.path.join(_job_dir(job_id), "output")
    os.makedirs(d, exist_ok=True)
    return d
