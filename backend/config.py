"""Runtime settings, read once from the environment (and .env)."""

import logging
import os

from dotenv import load_dotenv

from .prompts import DEFAULT_SUMMARY_PROMPT  # noqa: F401  re-exported as cfg.DEFAULT_SUMMARY_PROMPT

load_dotenv()

logger = logging.getLogger(__name__)

# API Keys
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")


# Paths
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
JOBS_DIR = os.path.join(_REPO_ROOT, "jobs")
UPLOADS_DIR = os.path.join(_REPO_ROOT, "uploads")
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
# ONNX Runtime Web and the embedding model the delivery page ships for meaning
# search; downloaded once by backend/search/assets.py, outside the repo.
SEARCH_ASSETS_DIR = os.path.expanduser(
    os.getenv("SEARCH_ASSETS_DIR", "~/Library/Application Support/JailCallService/search")
)

# Concurrency. 50 keeps AssemblyAI submission bursts well under the 100/min new-stream rate
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

# Case documents (PDF / Word / text files attached to a job) are pasted into the
# per-call summary prompt and the case-report synthesis prompt as context; this
# caps the total document text those prompts carry (see backend/case_documents.py).
MAX_CASE_DOCUMENT_CHARS = int(os.getenv("MAX_CASE_DOCUMENT_CHARS", "120000"))

# Summarization engine: "gemini" (cloud) or "gemma" (local)
DEFAULT_SUMMARIZATION_ENGINE = os.getenv("DEFAULT_SUMMARIZATION_ENGINE", "gemini")
MAX_GEMMA_CONCURRENT = 1
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "unsloth/gemma-4-E2B-it-UD-MLX-4bit")
GEMMA_MAX_TOKENS = int(os.getenv("GEMMA_MAX_TOKENS", "10240"))
