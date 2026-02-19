"""
Job state persistence via SQLite.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from .models import Job, JobStage, CallResult, CallStatus
from . import config as cfg
from .db import SessionLocal, engine, Base
from .db_models import DBJob, DBCall

logger = logging.getLogger(__name__)

# Initialize DB tabales
Base.metadata.create_all(bind=engine)

def _job_dir(job_id: str) -> str:
    d = os.path.join(cfg.JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d

def _map_to_pydantic(db_job: DBJob) -> Job:
    """Map DB Job (with calls) to Pydantic Job."""
    calls = []
    for c in db_job.calls:
        calls.append(CallResult(
            index=c.index,
            filename=c.filename,
            original_path=c.original_path,
            mp3_path=c.mp3_path,
            duration_seconds=c.duration_seconds,
            turns=c.turns,
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
            notes=c.notes
        ))

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
        calls=calls
    )

def create_job(case_name: str, input_folder: str, summary_prompt: str, defendant_name: Optional[str] = None, skip_summary: bool = False) -> Job:
    job_id = str(uuid.uuid4())
    _job_dir(job_id) # ensure the folder exists for output
    
    with SessionLocal() as db:
        new_job = DBJob(
            id=job_id,
            case_name=case_name,
            input_folder=input_folder,
            summary_prompt=summary_prompt,
            stage=JobStage.CREATED.value,
            created_at=datetime.now(timezone.utc).isoformat(),
            defendant_name=defendant_name,
            skip_summary=skip_summary
        )
        db.add(new_job)
        db.commit()
        db.refresh(new_job)
        return _map_to_pydantic(new_job)

def get_job(job_id: str) -> Optional[Job]:
    with SessionLocal() as db:
        db_job = db.query(DBJob).filter(DBJob.id == job_id).first()
        if not db_job:
            return None
        return _map_to_pydantic(db_job)

def list_jobs() -> List[Job]:
    with SessionLocal() as db:
        db_jobs = db.query(DBJob).order_by(DBJob.created_at.desc()).all()
        return [_map_to_pydantic(j) for j in db_jobs]

def update_job(job: Job) -> None:
    """Updates a job and all its calls using the passed Pydantic Job model."""
    with SessionLocal() as db:
        db_job = db.query(DBJob).filter(DBJob.id == job.id).first()
        if not db_job:
            logger.warning("Tried to update non-existent job: %s", job.id)
            return
            
        # Update top level fields
        db_job.stage = job.stage.value if isinstance(job.stage, JobStage) else job.stage
        db_job.started_at = job.started_at
        db_job.completed_at = job.completed_at
        db_job.zip_path = job.zip_path
        db_job.error = job.error
        db_job.defendant_name = job.defendant_name
        db_job.skip_summary = job.skip_summary
        
        # Upsert calls
        existing_calls = {c.index: c for c in db_job.calls}
        
        for c in job.calls:
            if c.index in existing_calls:
                db_c = existing_calls[c.index]
                db_c.mp3_path = c.mp3_path
                db_c.duration_seconds = c.duration_seconds
                db_c.turns = [t.model_dump() for t in c.turns] if c.turns is not None else None
                db_c.summary = c.summary
                db_c.pdf_path = c.pdf_path
                db_c.status = c.status.value if isinstance(c.status, CallStatus) else c.status
                db_c.error = c.error
                db_c.repaired = c.repaired
            else:
                db_c = DBCall(
                    job_id=job.id,
                    index=c.index,
                    filename=c.filename,
                    original_path=c.original_path,
                    mp3_path=c.mp3_path,
                    duration_seconds=c.duration_seconds,
                    turns=[t.model_dump() for t in c.turns] if c.turns is not None else None,
                    summary=c.summary,
                    pdf_path=c.pdf_path,
                    status=c.status.value if isinstance(c.status, CallStatus) else c.status,
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
                    notes=c.notes
                )
                db.add(db_c)
                
        db.commit()

def update_call(job_id: str, call_index: int, **kwargs) -> Optional[Job]:
    """Granular update of a specific call without fetching the full job Pydantic object."""
    with SessionLocal() as db:
        db_call = db.query(DBCall).filter(DBCall.job_id == job_id, DBCall.index == call_index).first()
        if not db_call:
            return None
            
        for k, v in kwargs.items():
            if k == 'turns' and v is not None:
                # Ensure turns are dicts for JSON
                setattr(db_call, k, [t.model_dump() if hasattr(t, 'model_dump') else t for t in v])
            elif k == 'status' and hasattr(v, 'value'):
                setattr(db_call, k, v.value)
            else:
                setattr(db_call, k, v)
                
        db.commit()
        
        # Return updated job
        job = get_job(job_id)
        return job

def get_job_output_dir(job_id: str) -> str:
    d = os.path.join(_job_dir(job_id), "output")
    os.makedirs(d, exist_ok=True)
    return d

