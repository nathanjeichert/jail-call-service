"""Prompt text for engines that return native structured JSON (Gemini).

The response schemas themselves live in ``schemas.py``; these strings tell
the model how to fill them. The per-call summary prompt body comes from
``backend.prompts``; ``SUMMARY_JSON_INSTRUCTIONS`` is appended to it.
"""

SYSTEM_AUDIO_DETECTION_JSON_PROMPT = (
    "AUTOMATED MESSAGE DETECTION:\n"
    "Identify all automated telecom system messages in the transcript turns below.\n"
    "Return a JSON object matching the provided schema.\n"
    "Each system_audio item must contain:\n"
    '- "turn": the 0-based turn index shown in brackets at the start of the transcript turn\n'
    '- "text": ONLY the automated telecom substring from that turn\n'
    "We are ONLY looking for pre-recorded system messages from the jail call service provider, "
    "such as IVR prompts, monitoring warnings, balance announcements, provider sign-offs, and "
    "time-remaining warnings.\n"
    "Do NOT flag ordinary human conversation, crosstalk, backchanneling, or repeated human "
    'speech just because both speakers say similar words, such as "Hey" / "Hey" or "yeah" / "yeah".\n'
    "Key rule: provider-injected system audio is often played into BOTH otherwise hard-panned "
    "channels at the same or nearly the same timestamp, so it can appear on both the INMATE and "
    "OUTSIDE PARTY turns with the same underlying message.\n"
    "Because the ASR transcript was produced separately for each channel, the two channel versions "
    "of the same automated message may differ slightly in wording, spelling, punctuation, or may "
    "have a missing/extra word or two. That does NOT mean only one turn should be flagged.\n"
    "If the same provider-generated automated message appears on both channels at matching or "
    "near-matching timestamps, include BOTH turn indices as separate system_audio items even if "
    "their transcribed text is not perfectly identical.\n"
    "Automated messages include IVR prompts, call acceptance prompts, monitoring warnings, "
    "balance announcements, provider sign-offs, and time-remaining warnings.\n"
    "These are not human speech.\n"
    "For mixed turns containing both human speech and system audio, include only the "
    "automated substring, not the full turn.\n"
    "If there are no automated telecom messages, return an empty system_audio array."
)

SUMMARY_JSON_INSTRUCTIONS = (
    "OUTPUT OVERRIDE:\n"
    "Ignore any earlier output-format instructions that conflict with this section.\n"
    "Return a JSON object matching the provided schema.\n"
    "For each note, provide ONLY:\n"
    '- "line_ref": the exact supporting transcript line or short adjacent line range in '
    'Page:Line or Page:Line-Page:Line format\n'
    '- "reason": why that cited moment matters\n'
    '- "importance_rank": unique integer rank where 1 is the strongest note in the response\n'
    "Do NOT include timestamps, speakers, or quoted text in the JSON notes. "
    "The application derives those from the cited lines.\n"
    "Order the notes array from most important to least important. The application will "
    "re-sort kept notes chronologically for display.\n"
    "Choose the shortest cited range that both directly supports the note and, when possible, "
    "will read coherently when rendered as a standalone pull quote. Prefer a single cited line "
    "when it fully supports the point; use 2-3 adjacent lines only when necessary.\n"
    "Use an empty notes array when there is nothing attorney-relevant to note.\n"
    "For LOW calls, usually return 0-3 notes. "
    "For MEDIUM calls, do not exceed 6 notes. "
    "For HIGH calls, usually keep the note count to about 12 so the strongest material fits cleanly "
    "in two summary pages, but for unusually dense and highly relevant calls you may return more "
    "when warranted, up to 21 notes total. "
    "If more moments seem arguable, omit weaker or redundant ones so the strongest notes fit first.\n"
    "Avoid em dashes in reason, identity, and brief-summary prose; use colons, commas, or separate sentences instead.\n"
    "Set identity_of_outside_party to null when the caller cannot be reasonably identified.\n"
    "Ignore any transcript lines spoken by AUTOMATED MESSAGE when choosing notes or writing the brief summary."
)

CASE_REPORT_SYNTHESIS_JSON_PROMPT = """You are synthesizing analysis of multiple jail phone call transcripts for a legal team.

CASE CONTEXT:
{case_context}

You have TWO tasks. Complete BOTH in a single JSON object matching the provided schema.

TASK 1 — findings
- From INPUT_CALLS below, identify between {min_findings} and {max_findings} findings that are most consequential for case strategy.
- Write in a neutral, objective, reader-agnostic tone suitable for either defense or prosecution review.
- Narrate in third-person neutral past-tense factual prose.
- No recommendations or legal advice.
- Avoid em dashes in headline and detail prose; use colons, commas, or separate sentences instead.
- Each finding must include:
  - call_id: integer call id from INPUT_CALLS
  - headline: 4-9 word title
  - timestamp: a [MM:SS] timestamp that already appears in that call's notes, or null
  - detail: one to three sentences explaining what was said and why it matters
- If nothing warrants attention, return an empty findings array.

TASK 2 — identities
- For each number in INPUT_NUMBERS below, synthesize the single best inferred outside-party identity from the supplied per-call descriptions.
- inference should be short and conservative.
- confidence must be HIGH, MEDIUM, or LOW.
- If nothing reliable can be said, use inference = "Unknown".

Return only the JSON object matching the schema.

INPUT_CALLS
{calls_block}

INPUT_NUMBERS
{numbers_block}
"""
