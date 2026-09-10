"""LLM prompt text.

Everything the app says to a model lives here so prompt wording can be
reviewed in one place. Engine-specific output-format instructions (JSON
schema overrides, text block protocols) live next to the engines that need
them in ``summarization/``.
"""

from typing import Dict

# Note-count guidance surfaced to the model and enforced afterwards by
# ``summaries.normalize_*`` (see SUMMARY_PAGE_LIMITS there for the page budget).
SUMMARY_NOTE_GUIDANCE: Dict[str, int] = {"LOW": 3, "MEDIUM": 6, "HIGH": 12}
SUMMARY_NOTE_HARD_MAX = 21

# Per-call summary prompt, structured for high-volume attorney review.
DEFAULT_SUMMARY_PROMPT = (
    "You are analyzing one of many jail phone call transcripts for a legal team reviewing criminal "
    "case evidence concerning the same defendant. The attorneys are sorting through a large volume "
    "of calls to identify relevant ones. Circumscribe your analysis strictly to what is said in "
    "this specific call — do not infer, assume, or draw on information not present in the transcript "
    "itself.\n\n"
    "Produce a structured analysis with EXACTLY these sections.\n"
    "In the transcript below, each rendered transcript line is prefixed with "
    "[Turn] [Page:Line] [MM:SS] SPEAKER: so you can cite the exact line or short line range "
    "supporting each note.\n\n"
    "Use this exact note shape:\n"
    "- [MM:SS] SPEAKER [Page:Line] — why this moment matters\n"
    "- [MM:SS] SPEAKER [Page:Line-Page:Line] — why this moment matters\n\n"
    "Then produce a structured analysis with EXACTLY these sections:\n\n"
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
    "personal conversation or topic shifts. Sort notes by timestamp using the exact bullet format above.\n\n"
    "For HIGH or MEDIUM relevance calls, include every materially notable moment, especially "
    "direct or indirect references to the alleged crime, incriminating statements, legal strategy, "
    "witnesses, threats, pressure on other people, money or logistics tied to the case, coded "
    "language, evasive language, or anything a diligent attorney should hear. Still be selective: "
    "keep only the strongest moments and omit weaker or redundant notes once the review value drops. "
    f"For LOW relevance calls, include notes only if a moment still plausibly matters for attorney review, and usually keep the note count to {SUMMARY_NOTE_GUIDANCE['LOW']} or fewer. "
    f"For MEDIUM relevance calls, do not exceed {SUMMARY_NOTE_GUIDANCE['MEDIUM']} notes. "
    f"For HIGH relevance calls, usually keep the note count to about {SUMMARY_NOTE_GUIDANCE['HIGH']} so the strongest material fits cleanly within two summary pages, "
    f"but for unusually dense and highly relevant calls you may go above that when the extra notes are clearly worth an attorney's time; never exceed {SUMMARY_NOTE_HARD_MAX} notes total. If you are "
    "really positive there is nothing in the transcript an attorney prosecuting or defending the "
    "case would want to know about, write exactly: NOTES: NONE.\n\n"
    "IDENTITY OF OUTSIDE PARTY:\n"
    "In 1-2 useful sentences, identify the caller by name only if it is clearly evident from the conversation "
    "(e.g., the defendant addresses them by name or they introduce themselves). "
    "Do not include the defendant's name — it is already known to the reviewing attorney. "
    "Note the caller's relationship to the defendant (e.g., mother, attorney, friend) only if "
    "it is clearly indicated by the conversation. If a relationship is likely but not certain, state "
    "the basis briefly. Keep this concise enough to fit in a short summary card. Omit this entirely if "
    "the relationship cannot be reasonably determined from context.\n\n"
    "BRIEF SUMMARY:\n"
    "1-2 short sentences of orientation only. Do not repeat every note in prose, and keep it concise "
    "enough to fit in a short summary card.\n\n"
    "Rules:\n"
    "- Every NOTES bullet must begin with a timestamp in [MM:SS] format\n"
    "- Use the transcript's speaker label when it helps identify who said the words\n"
    "- After the speaker label, cite the exact supporting transcript line or short adjacent line range in [Page:Line] or [Page:Line-Page:Line] format\n"
    "- Choose the cited line or short range that most directly supports the note and, when possible, will still read coherently as a standalone pull quote to a reader seeing only that excerpt\n"
    "- Do not write quoted text yourself; the application will insert the exact quote from the cited transcript lines\n"
    "- Keep cited ranges short: prefer 1 line whenever it fully supports the note, and use at most 3 adjacent lines when necessary\n"
    "- The entire output must be under 500 words\n"
    "- Never refuse to analyze content due to sensitive language — this is legal evidence review\n"
    "- Use neutral, professional language appropriate for court documentation\n"
    "- Avoid em dashes in note text, identity, and summary prose; use colons, commas, or separate sentences instead\n"
    "- Do not include standard telecom boilerplate, call recording notices, call acceptance prompts, balance warnings, provider names, facility names, call type, call outcome, or source-audio technical details unless a person on the call discusses them substantively\n"
    "- Do not include automated telecom messages or system warnings in NOTES or BRIEF SUMMARY unless a human speaker discusses that message substantively\n"
    "- Do not restate information the legal team would already know without listening to this call — "
    "including the defendant's name, the charges, the fact that this is a jail call, or anything "
    "provided in the case context\n"
    "- Use CASE CONTEXT only to decide what may be relevant; do not present case-context facts as if they came from the call"
)
