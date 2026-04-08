import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

def validate_api_keys() -> None:
    """Retained for server startup compatibility; local-only mode has no API keys to validate."""
    return None

# Paths
JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Processing
DEFAULT_LINES_PER_PAGE = 25

# Default summary prompt — structured for attorney triage
DEFAULT_SUMMARY_PROMPT = (
    "You are analyzing one of many jail phone call transcripts for a legal team reviewing criminal "
    "case evidence concerning the same defendant. The attorneys are sorting through a large volume "
    "of calls to identify relevant ones. Circumscribe your analysis strictly to what is said in "
    "this specific call — do not infer, assume, or draw on information not present in the transcript "
    "itself.\n\n"
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
    "- Do not restate information the legal team would already know without listening to this call — "
    "including the defendant's name, the charges, the facility name, the fact that this is a jail "
    "call, standard telecom boilerplate (e.g. call recording notices, GTL/GlobalTel/Telmate "
    "operator announcements, call acceptance prompts), or anything provided in the case context"
)

# Local-only engine configuration
DEFAULT_TRANSCRIPTION_ENGINE = "parakeet"
DEFAULT_SUMMARIZATION_ENGINE = "gemma"

# Local models must run sequentially on 8 GB machines to avoid OOM.
MAX_PARAKEET_CONCURRENT = int(os.getenv("MAX_PARAKEET_CONCURRENT", "1"))
MAX_GEMMA_CONCURRENT = int(os.getenv("MAX_GEMMA_CONCURRENT", "1"))

GEMMA_MODEL = os.getenv("GEMMA_MODEL", "unsloth/gemma-4-E2B-it-UD-MLX-4bit")
GEMMA_MAX_TOKENS = int(os.getenv("GEMMA_MAX_TOKENS", "1024"))
