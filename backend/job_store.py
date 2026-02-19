"""
In-memory job state with JSON persistence.

Jobs are stored in memory and also persisted to JOBS_DIR/<job_id>/state.json
after each significant state change. This allows resuming if the process crashes.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import Job, JobStage, CallResult, CallStatus
from . import config as cfg

logger = logging.getLogger(__name__)

# In-memory store
_jobs: Dict[str, Job] = {}


def _job_dir(job_id: str) -> str:
    d = os.path.join(cfg.JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(job_id: str) -> str:
    return os.path.join(_job_dir(job_id), "state.json")


def _save(job: Job) -> None:
    try:
        with open(_state_path(job.id), 'w') as f:
            json.dump(job.model_dump(), f, indent=2, default=str)
    except Exception as e:
        logger.warning("Failed to persist job %s: %s", job.id, e)


def _load_from_disk(job_id: str) -> Optional[Job]:
    path = _state_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return Job.model_validate(data)
    except Exception as e:
        logger.warning("Failed to load job %s from disk: %s", job_id, e)
        return None


def _ensure_loaded() -> None:
    """Load any persisted jobs from disk that aren't in memory yet."""
    if not os.path.exists(cfg.JOBS_DIR):
        return
    for entry in os.scandir(cfg.JOBS_DIR):
        if entry.is_dir() and entry.name not in _jobs:
            job = _load_from_disk(entry.name)
            if job:
                _jobs[job.id] = job


def create_job(case_name: str, input_folder: str, summary_prompt: str) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        case_name=case_name,
        input_folder=input_folder,
        summary_prompt=summary_prompt,
        stage=JobStage.CREATED,
        calls=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _jobs[job.id] = job
    _save(job)
    return job


def get_job(job_id: str) -> Optional[Job]:
    if job_id not in _jobs:
        job = _load_from_disk(job_id)
        if job:
            _jobs[job.id] = job
    return _jobs.get(job_id)


def list_jobs() -> List[Job]:
    _ensure_loaded()
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


def update_job(job: Job) -> None:
    _jobs[job.id] = job
    _save(job)


def update_call(job_id: str, call_index: int, **kwargs) -> Optional[Job]:
    job = get_job(job_id)
    if not job:
        return None

    call = next((c for c in job.calls if c.index == call_index), None)
    if not call:
        return job

    for k, v in kwargs.items():
        setattr(call, k, v)

    update_job(job)
    return job


def get_job_output_dir(job_id: str) -> str:
    d = os.path.join(_job_dir(job_id), "output")
    os.makedirs(d, exist_ok=True)
    return d
