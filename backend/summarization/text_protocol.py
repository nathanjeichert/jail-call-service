"""Block-delimited text protocol for engines without native JSON output (Gemma).

The model is asked for plain text in fixed shapes, and the parsers here turn
that text into the same typed results (``schemas.py``) the JSON engines
return. Three pieces:

* per-call summary: the canonical summary text itself (parsed later by
  ``backend.summaries``), optionally followed by one trailing
  ``SYSTEM_AUDIO: [...]`` line when automated-message detection is requested
  inline (:func:`parse_system_audio_response` splits it back off).
* case report: ``FINDING_START … FINDING_END`` and
  ``IDENTITY_START … IDENTITY_END`` blocks (:func:`parse_case_report_text`).
"""

import json
import logging
import re
from typing import Dict, List, Tuple

from .schemas import CaseReportFinding, CaseReportIdentity, CaseReportResponse

logger = logging.getLogger(__name__)

SYSTEM_AUDIO_DETECTION_PROMPT = (
    "AUTOMATED MESSAGE DETECTION:\n"
    "In addition to your summary above, identify all automated telecom system messages "
    "in this transcript. These are pre-recorded IVR prompts from the jail phone system, "
    "not human speech. Do not include these automated messages in the NOTES or BRIEF SUMMARY; "
    "identify them only in the final SYSTEM_AUDIO JSON line.\n\n"
    "Key rule: Automated messages are played into BOTH sides of the call simultaneously, "
    "so they appear on both the INMATE and OUTSIDE PARTY channels at the same (or very "
    "close) timestamps with semantically identical content. Due to ASR processing each "
    "channel independently, the exact wording may differ slightly between channels (e.g. "
    '"Global Tel-Link" vs "GlobalTel Link") — treat semantically equivalent text at '
    "matching timestamps as the same automated message.\n\n"
    "Common automated messages in jail/correctional calls include:\n"
    '- Language selection: "For English, press 1", "Para español, oprima 2"\n'
    '- Call type/PIN prompts: "For a collect call, press 0", "Please enter your PIN number"\n'
    '- Connection messages: "Please hold", "Please wait while your call is being connected"\n'
    '- Facility identification: "Hello, this is a prepaid call from an inmate at [facility name]"\n'
    '- Call acceptance: "To accept this call, press 0. To refuse this call..."\n'
    '- Monitoring warnings: "This call is from a correctional facility and is subject to '
    'monitoring, recording, and disclosure..."\n'
    '- Balance announcements: "Your current balance is $XX.XX"\n'
    '- Provider sign-offs: "Thank you for using Global Tel-Link"\n'
    '- Time warnings: "You have X minute(s) remaining"\n\n'
    "IMPORTANT: Some turns contain BOTH automated text and real human speech. For example:\n"
    '- Turn text: "but no, you got Max amped up, so they\'re like— You have 1 minute remaining."\n'
    '  → Only "You have 1 minute remaining." is the automated portion\n'
    '- Turn text: "you have 1 minute remaining. Oh yeah, they\'re playing us, bro."\n'
    '  → Only "you have 1 minute remaining." is the automated portion\n'
    "For these mixed turns, output ONLY the automated text substring, not the full turn text.\n\n"
    "On the FINAL line of your response, output exactly one line in this format:\n"
    'SYSTEM_AUDIO: [{"turn": 0, "text": "For English, press 1."}, '
    '{"turn": 1, "text": "For English, press 1."}, ...]\n'
    "Include every automated turn/segment. Use the turn numbers shown in brackets at the "
    "start of each transcript line."
)

CASE_REPORT_SYNTHESIS_PROMPT = """You are synthesizing analysis of multiple jail phone call transcripts for a legal team.

CASE CONTEXT:
{case_context}

You have TWO tasks. Complete BOTH in your single response.

═══════════════════════════════════════════════════════════════════
TASK 1 — TOP FINDINGS
═══════════════════════════════════════════════════════════════════

From the calls in INPUT_CALLS below, identify between {min_findings} and {max_findings} findings that are MOST consequential for case strategy — the things a reviewing attorney must know first when picking up this case. Quality over quantity. Each finding should tie to a specific moment in a specific call when possible.

VOICE FOR HEADLINE AND DETAIL:
- The audience is a legal professional reviewing the case — this may be DEFENSE counsel OR PROSECUTION. Write in a neutral, objective, reader-agnostic tone. Do not take sides.
- Narrate in third person, neutral past-tense factual prose. Refer to participants by role or name (e.g. "the defendant", "Lowe", "the outside party", "Seth").
- NEVER use the second person ("you", "your", "yours"). The defendant is NEVER "you" — even when the transcript has the defendant speaking in the first person. If the outside party told the defendant something, write "the outside party told the defendant…", NOT "the outside party informs you…".
- NEVER use imperatives directed at the reader ("review this", "note that", "see", "click here").
- Do not include recommendations, legal advice, strategic suggestions, or commentary about the report itself. Just state what happened and why it matters.
- Avoid em dashes in headlines and detail prose; use colons, commas, or separate sentences instead.

CRITERIA (what makes a finding important):
- Discussion of charges, alleged offense, or related criminal conduct
- Admissions, statements against interest, contradictions of prior statements
- Mentions of co-defendants, witnesses, victims, evidence, or alleged accomplices
- Discussion of legal strategy, plea offers, defense theories, or counsel
- Statements bearing on credibility, intent, motive, or state of mind
- Statements about confinement, custody status, bail, or release conditions
- Statements that contradict or corroborate the prosecution's theory of the case
- Coded, evasive, or guarded language that appears tied to the case
- DO NOT highlight routine personal conversation, family logistics, or banter unless it directly bears on the above

Output each finding using EXACTLY this structure, in priority order, separated by a single blank line:

FINDING_START
CALL_ID: <integer call id from INPUT_CALLS>
HEADLINE: <4-9 word title in title case>
TIMESTAMP: <[MM:SS] from that call's notes if a specific moment, otherwise NONE>
DETAIL: <one to three sentences explaining what was said and why it matters>
FINDING_END

The TIMESTAMP must be one that already appears in that call's NOTES, or NONE. Do not invent timestamps. If absolutely nothing in the input warrants attorney attention, return a single FINDING_START / FINDING_END block with HEADLINE: NONE.

═══════════════════════════════════════════════════════════════════
TASK 2 — OUTSIDE PARTY IDENTITY INFERENCE
═══════════════════════════════════════════════════════════════════

For each unique outside number listed in INPUT_NUMBERS below, the per-call analysis pass produced one or more candidate identity descriptions. Synthesize the SINGLE best inferred identity that balances accuracy and usefulness across all of that number's descriptions.

GUIDELINES:
- Where the descriptions clearly support BOTH a name AND a role/relationship, return both, e.g. "Kate, significant other" or "Sandra (mother)".
- Where only a role is supportable, return just the role, e.g. "Defense attorney" or "Mother of defendant".
- Where only a name is supportable, return just the name.
- When descriptions disagree, prefer the more conservative inference. Do not invent details.
- If nothing reliable can be said about a number, return INFERENCE: Unknown.
- CONFIDENCE: HIGH only when multiple descriptions converge on the same identity. MEDIUM when one or two descriptions support the inference but with some ambiguity. LOW when the inference is a guess.
- Keep INFERENCE strings short — under 60 characters.

Output each inferred identity using EXACTLY this structure:

IDENTITY_START
NUMBER: <the phone number string, copied verbatim from INPUT_NUMBERS>
INFERENCE: <name and/or role, or Unknown>
CONFIDENCE: <HIGH | MEDIUM | LOW>
IDENTITY_END

═══════════════════════════════════════════════════════════════════
OUTPUT ORDER
═══════════════════════════════════════════════════════════════════

First emit ALL FINDING blocks (Task 1), then emit ALL IDENTITY blocks (Task 2). Nothing else before, between, or after — your entire response is just blocks.

═══════════════════════════════════════════════════════════════════
INPUT_CALLS
═══════════════════════════════════════════════════════════════════
{calls_block}

═══════════════════════════════════════════════════════════════════
INPUT_NUMBERS
═══════════════════════════════════════════════════════════════════
{numbers_block}
"""


# ────────────────────────── System audio tail ──────────────────────────

def parse_system_audio_response(response_text: str) -> Tuple[str, list]:
    """Split a summary response into ``(summary_text, markers)``.

    ``markers`` is a list of ``{"turn": int, "text": str}`` dicts parsed from
    the trailing ``SYSTEM_AUDIO:`` line, or empty when absent/unparseable.
    """
    if not response_text:
        return "", []

    lines = response_text.rstrip().split("\n")
    marker_line_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("SYSTEM_AUDIO:"):
            marker_line_idx = i
            break

    if marker_line_idx is None:
        return response_text.strip(), []

    summary_text = "\n".join(lines[:marker_line_idx]).strip()
    summary_text = re.sub(
        r"\n*\s*AUTOMATED MESSAGE DETECTION:\s*$", "", summary_text, flags=re.IGNORECASE,
    ).strip()
    marker_raw = lines[marker_line_idx].strip()

    json_start = marker_raw.find("[")
    if json_start < 0:
        logger.warning("SYSTEM_AUDIO line found but no JSON array: %s", marker_raw[:200])
        return summary_text, []

    json_str = marker_raw[json_start:]
    # Trim anything after the closing bracket of the top-level array.
    bracket_depth = 0
    json_end = len(json_str)
    for i, ch in enumerate(json_str):
        if ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                json_end = i + 1
                break
    json_str = json_str[:json_end]

    try:
        markers = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse SYSTEM_AUDIO JSON: %s — %s", e, json_str[:300])
        return summary_text, []

    if not isinstance(markers, list):
        return summary_text, []

    valid = []
    for m in markers:
        if isinstance(m, dict) and "turn" in m and "text" in m:
            try:
                valid.append({"turn": int(m["turn"]), "text": str(m["text"])})
            except (ValueError, TypeError):
                continue

    logger.info("Parsed %d system audio markers from engine response", len(valid))
    return summary_text, valid


# ────────────────────────── Case report blocks ──────────────────────────

_FINDING_BLOCK_RE = re.compile(r"FINDING_START\s*(.*?)\s*FINDING_END", re.DOTALL | re.IGNORECASE)
_FINDING_FIELD_RE = re.compile(r"^(CALL_ID|HEADLINE|TIMESTAMP):\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_IDENTITY_BLOCK_RE = re.compile(r"IDENTITY_START\s*(.*?)\s*IDENTITY_END", re.DOTALL | re.IGNORECASE)
_IDENTITY_FIELD_RE = re.compile(r"^(NUMBER|INFERENCE|CONFIDENCE):\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_finding_blocks(text: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    for match in _FINDING_BLOCK_RE.finditer(text):
        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        detail_split = re.split(r"^DETAIL:\s*", block, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)
        if len(detail_split) == 2:
            head, detail = detail_split
            fields["DETAIL"] = detail.strip()
        else:
            head = block
        for fmatch in _FINDING_FIELD_RE.finditer(head):
            fields[fmatch.group(1).upper()] = fmatch.group(2).strip()
        if fields.get("HEADLINE", "").upper() == "NONE":
            continue
        if fields:
            findings.append(fields)
    return findings


def parse_case_report_text(text: str) -> CaseReportResponse:
    """Parse FINDING/IDENTITY blocks into a :class:`CaseReportResponse`.

    Malformed blocks (non-integer call id, missing headline or detail,
    missing number or inference) are dropped rather than rendered blank.
    """
    findings: List[CaseReportFinding] = []
    for fields in _parse_finding_blocks(text or ""):
        try:
            call_id = int((fields.get("CALL_ID") or "").strip())
        except ValueError:
            continue
        headline = fields.get("HEADLINE", "").strip()
        detail = fields.get("DETAIL", "").strip()
        if not headline or not detail:
            continue
        ts_raw = (fields.get("TIMESTAMP") or "").strip()
        timestamp = None if ts_raw.upper() in ("NONE", "N/A", "") else ts_raw
        findings.append(CaseReportFinding(call_id=call_id, headline=headline, timestamp=timestamp, detail=detail))

    identities: List[CaseReportIdentity] = []
    for match in _IDENTITY_BLOCK_RE.finditer(text or ""):
        fields = {m.group(1).upper(): m.group(2).strip() for m in _IDENTITY_FIELD_RE.finditer(match.group(1))}
        number = fields.get("NUMBER", "").strip()
        inference = fields.get("INFERENCE", "").strip()
        if not number or not inference:
            continue
        confidence = fields.get("CONFIDENCE", "").strip().upper()
        identities.append(CaseReportIdentity(
            number=number,
            inference=inference,
            confidence=confidence if confidence in ("HIGH", "MEDIUM", "LOW") else None,
        ))

    return CaseReportResponse(findings=findings, identities=identities)
