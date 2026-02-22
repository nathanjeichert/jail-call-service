import os
from dotenv import load_dotenv

load_dotenv()

# API Keys
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

# Paths
JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Processing
DEFAULT_LINES_PER_PAGE = 25
MAX_TRANSCRIPTION_CONCURRENT = 5
MAX_SUMMARIZATION_CONCURRENT = 10

# Default summary prompt
DEFAULT_SUMMARY_PROMPT = (
    "Summarize this jail call transcript. Note key topics, mentions of the case, "
    "legal matters, names, dates, locations. Keep under 300 words. "
    "IMPORTANT: When referring to specific quotes or key statements, cite the timestamp "
    "in bracket format matching the transcript (e.g. [01:23])."
)

# Models
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-3-pro")
