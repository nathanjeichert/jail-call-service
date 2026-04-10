"""
SQLAlchemy models for Job and Call persistence.
"""

from sqlalchemy import Column, String, Integer, Float, Boolean, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from .db import Base

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

    # One-to-many relationship with calls
    calls = relationship("DBCall", back_populates="job", cascade="all, delete-orphan", order_by="DBCall.index")


class DBCall(Base):
    __tablename__ = "calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, ForeignKey("jobs.id"), index=True)
    index = Column(Integer, nullable=False)
    
    filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False)
    mp3_path = Column(String, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    
    # Store transcript turns as JSON
    turns = Column(JSON, nullable=True)
    summary = Column(Text, nullable=True)
    pdf_path = Column(String, nullable=True)
    status = Column(String, default="pending")
    error = Column(String, nullable=True)
    repaired = Column(Boolean, default=False)

    # ICM Metadata
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

    # Token usage from Gemini summarization
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    thinking_tokens = Column(Integer, nullable=True)

    job = relationship("DBJob", back_populates="calls")
