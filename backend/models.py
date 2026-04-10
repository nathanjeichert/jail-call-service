import re
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from enum import Enum


def call_stem(index: int, filename: str) -> str:
    """Canonical output stem (e.g. ``042-original_name``) for a call.

    This is the single source of truth for how a call's output files
    (transcript PDFs, per-call audio, viewer deep-links) are named. The
    pipeline, the case report, and the search page all build links using
    this helper so the names stay in lockstep.
    """
    base = filename
    if base.lower().endswith(".wav"):
        base = base[:-4]
    safe = re.sub(r"[^\w.\-]", "_", base)
    safe = re.sub(r"_+", "_", safe).strip("_") or "call"
    return f"{index + 1:03d}-{safe}"


class WordTimestamp(BaseModel):
    text: str
    start: float  # milliseconds
    end: float    # milliseconds
    confidence: Optional[float] = None
    speaker: Optional[str] = None


class TranscriptTurn(BaseModel):
    speaker: str
    text: str
    timestamp: Optional[str] = None
    words: Optional[List[WordTimestamp]] = None
    is_continuation: bool = False


class CallStatus(str, Enum):
    PENDING = "pending"
    REPAIRING = "repairing"
    CONVERTING = "converting"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    GENERATING_PDF = "generating_pdf"
    DONE = "done"
    ERROR = "error"


class CallResult(BaseModel):
    index: int
    filename: str
    original_path: str
    mp3_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    turns: Optional[List[TranscriptTurn]] = None
    summary: Optional[str] = None
    pdf_path: Optional[str] = None
    status: CallStatus = CallStatus.PENDING
    error: Optional[str] = None
    repaired: bool = False

    # ICM metadata (None when no XML is present)
    inmate_name: Optional[str] = None
    inmate_pin: Optional[str] = None
    outside_number: Optional[str] = None        # raw digits
    outside_number_fmt: Optional[str] = None    # formatted (XXX) XXX-XXXX
    call_date: Optional[str] = None             # YYYY-MM-DD
    call_time: Optional[str] = None             # HH:MM
    call_datetime_str: Optional[str] = None     # "YYYY-MM-DD HH:MM"
    facility: Optional[str] = None              # housing unit
    call_outcome: Optional[str] = None          # "Inmate Hungup" etc.
    call_type: Optional[str] = None             # "Prepay (Public)" etc.
    xml_duration_seconds: Optional[int] = None
    notes: Optional[str] = None                 # non-empty only when meaningful

    # Token usage from Gemini summarization
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    thinking_tokens: Optional[int] = None

    class Config:
        use_enum_values = True


class JobStage(str, Enum):
    CREATED = "created"
    CONVERTING = "converting"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    GENERATING = "generating"
    PACKAGING = "packaging"
    DONE = "done"
    ERROR = "error"
    PAUSED = "paused"


class Job(BaseModel):
    id: str
    case_name: str
    input_folder: str
    summary_prompt: str
    stage: JobStage = JobStage.CREATED
    calls: List[CallResult] = []
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    zip_path: Optional[str] = None
    error: Optional[str] = None
    defendant_name: Optional[str] = None
    skip_summary: bool = False
    file_paths: Optional[List[str]] = None
    xml_metadata_path: Optional[str] = None
    transcription_engine: Optional[str] = None
    summarization_engine: Optional[str] = None
    auto_message_mode: Optional[str] = None  # "exclude", "label", or None (keep)

    class Config:
        use_enum_values = True
