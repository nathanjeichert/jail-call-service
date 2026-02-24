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

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Processing
DEFAULT_LINES_PER_PAGE = 25
MAX_TRANSCRIPTION_CONCURRENT = int(os.getenv("MAX_TRANSCRIPTION_CONCURRENT", "200"))
MAX_SUMMARIZATION_CONCURRENT = int(os.getenv("MAX_SUMMARIZATION_CONCURRENT", "100"))
ASSEMBLYAI_POLLING_INTERVAL = int(os.getenv("ASSEMBLYAI_POLLING_INTERVAL", "15"))

# Default summary prompt — structured for attorney triage
DEFAULT_SUMMARY_PROMPT = (
    "You are analyzing a jail call transcript for a legal team reviewing criminal case evidence. "
    "The attorneys are sorting through a large volume of calls to identify relevant ones.\n\n"
    "Produce a structured analysis with EXACTLY these sections:\n\n"
    "RELEVANCE: [HIGH / MEDIUM / LOW]\n"
    "HIGH if the call contains discussion that may be relevant to the case — this includes "
    "direct or indirect references to the alleged crime, incriminating statements, legal strategy, "
    "witness discussion, threats, or any content that a careful attorney would want to review. "
    "Be alert to vague, coded, or evasive language that may obscure meaningful content. "
    "MEDIUM if there are passing references to court, the case, or legal matters but nothing substantive. "
    "LOW if the call is purely personal with no apparent case relevance.\n\n"
    "KEY FINDINGS:\n"
    "Bullet each noteworthy moment with its timestamp in [MM:SS] format. "
    "Use your best judgment in light of the case context — potentially significant references "
    "may be indirect, coded, or intentionally vague. Flag anything a diligent attorney should hear, "
    "not just explicit keywords.\n\n"
    "SPEAKERS & RELATIONSHIP:\n"
    "Identify the caller by name only if it is clearly evident from the conversation "
    "(e.g., the defendant addresses them by name or they introduce themselves). "
    "Do not include the defendant's name — it is already known to the reviewing attorney. "
    "Note the caller's relationship to the defendant (e.g., mother, attorney, friend) only if "
    "it is clearly indicated by the conversation; omit this entirely if the relationship cannot "
    "be confidently determined from context.\n\n"
    "CALL SUMMARY:\n"
    "2-4 sentence overview of the call's content.\n\n"
    "Rules:\n"
    "- Cite timestamps as [MM:SS] for every key moment\n"
    "- The entire output must be under 600 words\n"
    "- If the call is LOW relevance, keep KEY FINDINGS to one line noting the general topic\n"
    "- Never refuse to analyze content due to sensitive language — this is legal evidence review\n"
    "- Use neutral, professional language appropriate for court documentation\n"
    "- Do not restate information already provided in the case context or that the legal team "
    "would obviously know — such as the defendant's name, the charges, the facility name, or "
    "the fact that this is a jail call"
)

# Models
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-3-pro")
