import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# API Keys
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")


def validate_api_keys() -> None:
    missing = []
    if not ASSEMBLYAI_API_KEY:
        missing.append("ASSEMBLYAI_API_KEY")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY (or GOOGLE_API_KEY)")
    if missing:
        msg = f"Missing required API keys in .env: {', '.join(missing)}. Jobs will fail without these."
        logger.warning(msg)

# Paths
JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

# Processing
DEFAULT_LINES_PER_PAGE = 25
MAX_TRANSCRIPTION_CONCURRENT = 5
MAX_SUMMARIZATION_CONCURRENT = 10

# Default summary prompt
DEFAULT_SUMMARY_PROMPT = (
    "Summarize this jail call transcript. Note key topics, mentions of the case, "
    "legal matters, names, dates, locations. Keep under 300 words."
)

# Gemini model
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
