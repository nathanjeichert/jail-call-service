import logging
import os
from dotenv import load_dotenv

from .prompts import DEFAULT_SUMMARY_PROMPT  # noqa: F401  (re-exported for callers using cfg.DEFAULT_SUMMARY_PROMPT)

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

# 50 keeps AssemblyAI submission bursts well under the 100/min new-stream rate
# and Gemini Flash Tier 1's 300 RPM with headroom for retries.
MAX_TRANSCRIPTION_CONCURRENT = int(os.getenv("MAX_TRANSCRIPTION_CONCURRENT", "50"))
MAX_SUMMARIZATION_CONCURRENT = int(os.getenv("MAX_SUMMARIZATION_CONCURRENT", "50"))
ASSEMBLYAI_POLLING_INTERVAL = int(os.getenv("ASSEMBLYAI_POLLING_INTERVAL", "15"))
ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC = int(os.getenv("ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC", "900"))

# Models
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_SYSTEM_AUDIO_THINKING_LEVEL = os.getenv("GEMINI_SYSTEM_AUDIO_THINKING_LEVEL", "low").strip().lower()
GEMINI_SUMMARY_THINKING_LEVEL = os.getenv("GEMINI_SUMMARY_THINKING_LEVEL", "medium").strip().lower()
GEMINI_CASE_REPORT_THINKING_LEVEL = os.getenv("GEMINI_CASE_REPORT_THINKING_LEVEL", "medium").strip().lower()
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-3-pro")

# Transcription engine: "assemblyai" (cloud) or "parakeet" (local)
DEFAULT_TRANSCRIPTION_ENGINE = os.getenv("DEFAULT_TRANSCRIPTION_ENGINE", "assemblyai")
MAX_PARAKEET_CONCURRENT = int(os.getenv("MAX_PARAKEET_CONCURRENT", "2"))

# Summarization engine: "gemini" (cloud) or "gemma" (local)
DEFAULT_SUMMARIZATION_ENGINE = os.getenv("DEFAULT_SUMMARIZATION_ENGINE", "gemini")
MAX_GEMMA_CONCURRENT = 1
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "unsloth/gemma-4-E2B-it-UD-MLX-4bit")
GEMMA_MAX_TOKENS = int(os.getenv("GEMMA_MAX_TOKENS", "10240"))
