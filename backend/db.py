"""
Database setup for Jail Call Service using SQLite.
Stores jobs and calls to track large batch state persistently.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from . import config as cfg

DATABASE_URL = f"sqlite:///{os.path.join(cfg.JOBS_DIR, 'jail_calls.db')}"

# Ensure jobs directory exists so db can be created
os.makedirs(cfg.JOBS_DIR, exist_ok=True)

engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} # SQLite specific
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
