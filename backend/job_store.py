"""Job and call persistence (SQLite via SQLAlchemy).

The pipeline checkpoints every per-call stage transition here so a crash or
pause resumes without re-spending API credits. Readers pick the cheapest
query: ``get_job_lite`` / ``list_jobs`` skip the heavy transcript JSON,
``get_call`` loads one call, ``get_job`` loads everything.
"""

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import joinedload

from . import config as cfg
from .db import Base, DBCall, DBJob, SessionLocal, engine
from .models import CallResult, Job, JobStage, normalize_speaker_assignment

logger = logging.getLogger(__name__)

_JOB_FIELDS = [c.name for c in DBJob.__table__.columns if c.name != "id"]
_CALL_FIELDS = [c.name for c in DBCall.__table__.columns if c.name not in ("id", "job_id")]
_ACTIVE_STAGES = {"converting", "transcribing", "summarizing", "generating", "packaging"}


def _ensure_schema() -> None:
    """Create tables, and add any columns an older local database is missing."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in (DBJob.__table__, DBCall.__table__):
            present = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table.name})"))}
            for column in table.columns:
                if column.name not in present:
                    col_type = column.type.compile(engine.dialect)
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"))
                    logger.info("Added missing column %s.%s", table.name, column.name)


_ensure_schema()


# ────────────────────────── Paths ──────────────────────────

def job_dir(job_id: str) -> str:
    d = os.path.join(cfg.JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def get_job_output_dir(job_id: str) -> str:
    d = os.path.join(job_dir(job_id), "output")
    os.makedirs(d, exist_ok=True)
    return d


def _remove_job_dir(job_id: str) -> None:
    d = os.path.join(cfg.JOBS_DIR, job_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────── Mapping ──────────────────────────

def _db_value(name: str, value):
    if isinstance(value, Enum):
        return value.value
    if name == "turns" and value is not None:
        return [t.model_dump() if hasattr(t, "model_dump") else t for t in value]
    if name == "speaker_assignment":
        return normalize_speaker_assignment(value)
    return value


def _apply_fields(db_row, model, names) -> None:
    for name in names:
        setattr(db_row, name, _db_value(name, getattr(model, name)))


def _to_call(c: DBCall, *, include_turns: bool = True) -> CallResult:
    data = {name: getattr(c, name) for name in _CALL_FIELDS}
    if not include_turns:
        data["turns"] = None
    return CallResult(**data)


def _to_job(db_job: DBJob, *, include_turns: bool = True) -> Job:
    data = {name: getattr(db_job, name) for name in _JOB_FIELDS}
    data["speaker_assignment"] = normalize_speaker_assignment(data.get("speaker_assignment"))
    return Job(
        id=db_job.id,
        calls=[_to_call(c, include_turns=include_turns) for c in db_job.calls],
        **data,
    )


def _job_query_options(include_turns: bool):
    load = joinedload(DBJob.calls)
    return [load if include_turns else load.defer(DBCall.turns)]


# ────────────────────────── Jobs ──────────────────────────

def create_job(**fields) -> Job:
    """Create a job row from ``Job`` field values (case_name, input_folder, summary_prompt, ...)."""
    job_id = str(uuid.uuid4())
    job_dir(job_id)
    fields["speaker_assignment"] = normalize_speaker_assignment(fields.get("speaker_assignment"))
    with SessionLocal() as db:
        db_job = DBJob(
            id=job_id,
            stage=JobStage.CREATED.value,
            created_at=datetime.now(timezone.utc).isoformat(),
            **fields,
        )
        db.add(db_job)
        db.commit()
        db.refresh(db_job)
        return _to_job(db_job)


def get_job(job_id: str) -> Optional[Job]:
    with SessionLocal() as db:
        db_job = db.query(DBJob).options(*_job_query_options(True)).filter(DBJob.id == job_id).first()
        return _to_job(db_job) if db_job else None


def get_job_lite(job_id: str) -> Optional[Job]:
    """Job + call metadata WITHOUT the heavy turns JSON column."""
    with SessionLocal() as db:
        db_job = db.query(DBJob).options(*_job_query_options(False)).filter(DBJob.id == job_id).first()
        return _to_job(db_job, include_turns=False) if db_job else None


def list_jobs() -> List[Job]:
    with SessionLocal() as db:
        rows = (
            db.query(DBJob)
            .options(*_job_query_options(False))
            .order_by(DBJob.created_at.desc())
            .all()
        )
        return [_to_job(j, include_turns=False) for j in rows]


def get_job_stage(job_id: str) -> Optional[str]:
    """Only the stage column; the pipeline polls this between items."""
    with SessionLocal() as db:
        row = db.query(DBJob.stage).filter(DBJob.id == job_id).first()
        return row[0] if row else None


def update_job_stage(job_id: str, stage) -> None:
    with SessionLocal() as db:
        db_job = db.query(DBJob).filter(DBJob.id == job_id).first()
        if db_job:
            db_job.stage = _db_value("stage", stage)
            db.commit()


def update_job(job: Job) -> None:
    """Write back a job and upsert all its calls."""
    with SessionLocal() as db:
        db_job = db.query(DBJob).options(joinedload(DBJob.calls)).filter(DBJob.id == job.id).first()
        if not db_job:
            logger.warning("Tried to update non-existent job: %s", job.id)
            return

        _apply_fields(db_job, job, _JOB_FIELDS)
        existing = {c.index: c for c in db_job.calls}
        for call in job.calls:
            db_call = existing.get(call.index)
            if db_call is None:
                db_call = DBCall(job_id=job.id)
                db.add(db_call)
            _apply_fields(db_call, call, _CALL_FIELDS)
        db.commit()


def pause_orphaned_jobs() -> List[str]:
    """On startup, mark jobs that were mid-flight at the last shutdown as paused.

    Background tasks die with the server but the DB still shows the job in
    its last active stage; without this the job would look like it is running
    and a stray click could resume expensive API calls. Returns the paused ids.
    """
    with SessionLocal() as db:
        rows = db.query(DBJob).filter(DBJob.stage.in_(_ACTIVE_STAGES)).all()
        ids = [r.id for r in rows]
        for r in rows:
            r.stage = JobStage.PAUSED.value
        if ids:
            db.commit()
    return ids


def _delete_jobs(db, job_ids: List[str]) -> None:
    # Bulk statements: avoid loading every call's transcript JSON just to delete it.
    db.query(DBCall).filter(DBCall.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(DBJob).filter(DBJob.id.in_(job_ids)).delete(synchronize_session=False)
    db.commit()


def delete_job(job_id: str) -> bool:
    """Delete a job, its calls, and its output directory. Returns False if not found."""
    with SessionLocal() as db:
        if not db.query(DBJob.id).filter(DBJob.id == job_id).first():
            return False
        _delete_jobs(db, [job_id])
    _remove_job_dir(job_id)
    return True


def delete_completed_jobs() -> int:
    """Delete every job in stage done/error (and its files). Returns the count."""
    with SessionLocal() as db:
        job_ids = [row[0] for row in db.query(DBJob.id).filter(DBJob.stage.in_(["done", "error"])).all()]
        if job_ids:
            _delete_jobs(db, job_ids)
    for jid in job_ids:
        _remove_job_dir(jid)
    return len(job_ids)


# ────────────────────────── Calls ──────────────────────────

def get_call(job_id: str, call_index: int) -> Optional[CallResult]:
    """One call, transcript included, without loading the rest of the job."""
    with SessionLocal() as db:
        db_call = db.query(DBCall).filter(DBCall.job_id == job_id, DBCall.index == call_index).first()
        return _to_call(db_call) if db_call else None


def update_call(job_id: str, call_index: int, **fields) -> None:
    """Granular update of one call's columns."""
    with SessionLocal() as db:
        db_call = db.query(DBCall).filter(DBCall.job_id == job_id, DBCall.index == call_index).first()
        if not db_call:
            return
        for name, value in fields.items():
            if name not in _CALL_FIELDS:
                logger.warning("Ignoring unknown call field update: %s", name)
                continue
            setattr(db_call, name, _db_value(name, value))
        db.commit()
