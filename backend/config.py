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
# 50 keeps AssemblyAI submission bursts well under the 100/min new-stream rate
# and Gemini Flash Tier 1's 300 RPM with headroom for retries.
MAX_TRANSCRIPTION_CONCURRENT = int(os.getenv("MAX_TRANSCRIPTION_CONCURRENT", "50"))
MAX_SUMMARIZATION_CONCURRENT = int(os.getenv("MAX_SUMMARIZATION_CONCURRENT", "50"))
ASSEMBLYAI_POLLING_INTERVAL = int(os.getenv("ASSEMBLYAI_POLLING_INTERVAL", "15"))
ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC = int(os.getenv("ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC", "900"))

# Default summary prompt - structured for high-volume attorney review
DEFAULT_SUMMARY_PROMPT = (
    "You are analyzing one of many jail phone call transcripts for a legal team reviewing criminal "
    "case evidence concerning the same defendant. The attorneys are sorting through a large volume "
    "of calls to identify relevant ones. Circumscribe your analysis strictly to what is said in "
    "this specific call — do not infer, assume, or draw on information not present in the transcript "
    "itself.\n\n"
    "Produce a structured analysis with EXACTLY these sections:\n\n"
    "RELEVANCE: [HIGH / MEDIUM / LOW]\n"
    "HIGH only if the call contains substantive content that a party, especially the prosecution, "
    "may plausibly want to play for a jury or use in investigation. This includes admissions, "
    "consciousness of guilt, threats, witness pressure, instructions to others, discussion of "
    "evidence, case strategy, coded or evasive discussion that appears tied to the case, or other "
    "potentially incriminating conduct by the defendant. "
    "MEDIUM if the call contains case-related or potentially useful content that is more than a "
    "bare passing mention, but is not clearly incriminating or central. "
    "LOW if the call is personal, logistical, or contains only passing references to court, jail, "
    "custody, routine scheduling, or generic legal status without substantive discussion. "
    "Do not assign HIGH or MEDIUM solely because someone mentions court, jail, incarceration, "
    "charges, or a lawyer in passing.\n\n"
    "NOTES:\n"
    "List only transcript moments that an attorney might plausibly want to know about. "
    "Include a note only if the moment bears on the charges, evidence, witnesses, case strategy, "
    "the defendant's confinement, potentially criminal or incriminating conduct, or is important "
    "for some other case-review reason. Do not create notes merely to orient the reader to routine "
    "personal conversation or topic shifts. Sort notes by timestamp. Use this exact bullet format:\n"
    "- [MM:SS] SPEAKER: \"short verbatim quote\" — why this moment matters\n\n"
    "For HIGH or MEDIUM relevance calls, include every materially notable moment, especially "
    "direct or indirect references to the alleged crime, incriminating statements, legal strategy, "
    "witnesses, threats, pressure on other people, money or logistics tied to the case, coded "
    "language, evasive language, or anything a diligent attorney should hear. For LOW relevance "
    "calls, include notes only if a moment still plausibly matters for attorney review. If you are "
    "really positive there is nothing in the transcript an attorney prosecuting or defending the "
    "case would want to know about, write exactly: NOTES: NONE.\n\n"
    "IDENTITY OF OUTSIDE PARTY:\n"
    "In 1-2 useful sentences, identify the caller by name only if it is clearly evident from the conversation "
    "(e.g., the defendant addresses them by name or they introduce themselves). "
    "Do not include the defendant's name — it is already known to the reviewing attorney. "
    "Note the caller's relationship to the defendant (e.g., mother, attorney, friend) only if "
    "it is clearly indicated by the conversation. If a relationship is likely but not certain, state "
    "the basis briefly. Omit this entirely if the relationship cannot be reasonably determined from context.\n\n"
    "BRIEF SUMMARY:\n"
    "1-2 sentence orientation only. Do not repeat every note in prose.\n\n"
    "Rules:\n"
    "- Every NOTES bullet must begin with a timestamp in [MM:SS] format\n"
    "- Use the transcript's speaker label when it helps identify who said the quoted words\n"
    "- Quotes must be verbatim from the transcript and under 18 words; if no useful quote exists, omit the quote and still explain the note\n"
    "- The entire output must be under 500 words\n"
    "- Never refuse to analyze content due to sensitive language — this is legal evidence review\n"
    "- Use neutral, professional language appropriate for court documentation\n"
    "- Do not include standard telecom boilerplate, call recording notices, call acceptance prompts, balance warnings, provider names, facility names, call type, call outcome, or source-audio technical details unless a person on the call discusses them substantively\n"
    "- Do not include automated telecom messages or system warnings in NOTES or BRIEF SUMMARY unless a human speaker discusses that message substantively\n"
    "- Do not restate information the legal team would already know without listening to this call — "
    "including the defendant's name, the charges, the fact that this is a jail call, or anything "
    "provided in the case context\n"
    "- Use CASE CONTEXT only to decide what may be relevant; do not present case-context facts as if they came from the call"
)

# Models
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-3-pro")

# Transcription engine: "assemblyai" (cloud) or "parakeet" (local)
DEFAULT_TRANSCRIPTION_ENGINE = os.getenv("DEFAULT_TRANSCRIPTION_ENGINE", "assemblyai")
# Parakeet must run sequentially on 8 GB machines to avoid OOM
MAX_PARAKEET_CONCURRENT = 1

# Summarization engine: "gemini" (cloud) or "gemma" (local)
DEFAULT_SUMMARIZATION_ENGINE = os.getenv("DEFAULT_SUMMARIZATION_ENGINE", "gemini")
# Gemma must run sequentially on 8 GB machines to avoid OOM
MAX_GEMMA_CONCURRENT = 1
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "unsloth/gemma-4-E2B-it-UD-MLX-4bit")
GEMMA_MAX_TOKENS = int(os.getenv("GEMMA_MAX_TOKENS", "1024"))
