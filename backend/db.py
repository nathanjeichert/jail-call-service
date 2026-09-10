"""SQLite persistence: engine setup and the ORM tables for jobs and calls.

SQLite is configured with WAL mode and a 30-second busy timeout so the
parallel pipeline stages (conversion threads plus async transcription and
summarization workers) can write concurrently. ``job_store`` is the only
module that should touch these tables directly.
"""

import os

from sqlalchemy import (
    JSON, Boolean, Column, Float, ForeignKey, Integer, String, Text,
    create_engine, event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config as cfg

DATABASE_URL = f"sqlite:///{os.path.join(cfg.JOBS_DIR, 'jail_calls.db')}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class DBJob(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, index=True)
    case_name = Column(String, nullable=False)
    input_folder = Column(String, nullable=False)
    summary_prompt = Column(String, default="")
    stage = Column(String, default="created")
    created_at = Column(String, nullable=False)
    started_at = Column(String, nullable=True)
    completed_at = Column(String, nullable=True)
    zip_path = Column(String, nullable=True)
    error = Column(String, nullable=True)
    defendant_name = Column(String, nullable=True)
    skip_summary = Column(Boolean, default=False)

    file_paths = Column(JSON, nullable=True)
    xml_metadata_path = Column(String, nullable=True)
    transcription_engine = Column(String, nullable=True)
    summarization_engine = Column(String, nullable=True)
    auto_message_mode = Column(String, nullable=True)
    speaker_assignment = Column(String, nullable=True)

    calls = relationship(
        "DBCall", back_populates="job", cascade="all, delete-orphan", order_by="DBCall.index",
    )


class DBCall(Base):
    __tablename__ = "calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, ForeignKey("jobs.id"), index=True)
    index = Column(Integer, nullable=False)

    filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False)
    mp3_path = Column(String, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    turns = Column(JSON, nullable=True)  # List[TranscriptTurn] as dicts
    summary = Column(Text, nullable=True)
    pdf_path = Column(String, nullable=True)
    status = Column(String, default="pending")
    error = Column(String, nullable=True)
    repaired = Column(Boolean, default=False)

    # ICM metadata
    inmate_name = Column(String, nullable=True)
    inmate_pin = Column(String, nullable=True)
    outside_number = Column(String, nullable=True)
    outside_number_fmt = Column(String, nullable=True)
    call_date = Column(String, nullable=True)
    call_time = Column(String, nullable=True)
    call_datetime_str = Column(String, nullable=True)
    facility = Column(String, nullable=True)
    call_outcome = Column(String, nullable=True)
    call_type = Column(String, nullable=True)
    xml_duration_seconds = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)

    # Token usage from summarization
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    thinking_tokens = Column(Integer, nullable=True)

    job = relationship("DBJob", back_populates="calls")
