"""
System audio detection and filtering for jail call transcripts.

Gemini uses a dedicated first-pass structured-output request to identify
automated telecom turns before the citation-bearing summary call runs. Gemma
keeps the legacy combined summary + `SYSTEM_AUDIO:` tail format. Both paths
ultimately feed the same filtering helpers here, which either strip or relabel
the detected automated telecom turns (IVR prompts, time warnings, provider
sign-offs).
"""

import json
import logging
import re
from copy import deepcopy
from typing import List, Optional, Tuple

from .models import TranscriptTurn, WordTimestamp

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

SYSTEM_AUDIO_DETECTION_JSON_PROMPT = (
    "AUTOMATED MESSAGE DETECTION:\n"
    "Identify all automated telecom system messages in the transcript turns below.\n"
    "Return a JSON object matching the provided schema.\n"
    "Each system_audio item must contain:\n"
    '- "turn": the 0-based turn index shown in brackets at the start of the transcript turn\n'
    '- "text": ONLY the automated telecom substring from that turn\n'
    "Automated messages include IVR prompts, call acceptance prompts, monitoring warnings, "
    "balance announcements, provider sign-offs, and time-remaining warnings.\n"
    "These are not human speech.\n"
    "For mixed turns containing both human speech and system audio, include only the "
    "automated substring, not the full turn.\n"
    "If there are no automated telecom messages, return an empty system_audio array."
)


def parse_system_audio_response(response_text: str) -> Tuple[str, list]:
    """
    Parse the engine response to split the summary from system audio markers.

    Returns:
        (summary_text, system_audio_markers) where markers is a list of
        {"turn": int, "text": str} dicts, or empty list if none found.
    """
    if not response_text:
        return "", []

    # Find the SYSTEM_AUDIO: line (last occurrence)
    lines = response_text.rstrip().split("\n")
    marker_line_idx = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("SYSTEM_AUDIO:"):
            marker_line_idx = i
            break

    if marker_line_idx is None:
        return response_text.strip(), []

    summary_text = "\n".join(lines[:marker_line_idx]).strip()
    summary_text = re.sub(
        r"\n*\s*AUTOMATED MESSAGE DETECTION:\s*$",
        "",
        summary_text,
        flags=re.IGNORECASE,
    ).strip()
    marker_raw = lines[marker_line_idx].strip()

    # Extract JSON from the line
    json_start = marker_raw.find("[")
    if json_start < 0:
        logger.warning("SYSTEM_AUDIO line found but no JSON array: %s", marker_raw[:200])
        return summary_text, []

    json_str = marker_raw[json_start:]
    # Handle potential trailing text after the JSON array
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


def _timestamp_to_seconds(timestamp: Optional[str]) -> float:
    if not timestamp:
        return 0.0
    match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})", timestamp)
    if not match:
        return 0.0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def _normalized_tokens(text: str) -> set[str]:
    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
        "was", "with", "you", "your",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in stop_words
    }


def remove_system_audio_notes(
    summary_text: str,
    markers: list,
    turns: List[TranscriptTurn],
) -> str:
    """Remove NOTES bullets that summarize automated telecom messages."""
    if not summary_text or not markers:
        return summary_text

    marker_data = []
    for marker in markers:
        try:
            turn = turns[int(marker["turn"])]
        except (IndexError, KeyError, TypeError, ValueError):
            turn = None

        marker_text = str(marker.get("text", ""))
        normalized = re.sub(r"[^a-z0-9]+", " ", marker_text.lower()).strip()
        marker_data.append({
            "seconds": _timestamp_to_seconds(getattr(turn, "timestamp", "")),
            "normalized": normalized,
            "tokens": _normalized_tokens(marker_text),
        })

    def is_system_note(line: str) -> bool:
        if not re.match(r"^\s*[-\u2022*]\s*\[", line):
            return False

        line_seconds = _timestamp_to_seconds(line)
        line_normalized = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        line_tokens = _normalized_tokens(line)

        for marker in marker_data:
            marker_normalized = marker["normalized"]
            if marker_normalized and (
                marker_normalized in line_normalized
                or (len(line_normalized) > 24 and line_normalized in marker_normalized)
            ):
                return True

            close_in_time = abs(line_seconds - marker["seconds"]) <= 3
            if close_in_time and len(line_tokens & marker["tokens"]) >= 2:
                return True

        return False

    kept_lines = [line for line in summary_text.splitlines() if not is_system_note(line)]
    return "\n".join(kept_lines).strip()


def _normalize_token(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lower())


def _find_word_boundary(words: List[WordTimestamp], system_text: str) -> int:
    """Find the word index where system_text starts within a word list.
    Returns -1 if not found."""
    system_tokens = [_normalize_token(t) for t in system_text.split() if _normalize_token(t)]
    word_tokens = [_normalize_token(w.text) for w in words]

    if not system_tokens or len(system_tokens) > len(word_tokens):
        return -1

    for i in range(len(word_tokens) - len(system_tokens) + 1):
        match = True
        for j, st in enumerate(system_tokens):
            if word_tokens[i + j] != st:
                match = False
                break
        if match:
            return i
    return -1


def _is_full_turn(turn_text: str, system_text: str) -> bool:
    """Check if system_text covers essentially the entire turn."""
    t = turn_text.strip().lower()
    s = system_text.strip().lower()
    if t == s:
        return True
    # Check if removing system text leaves very little real content
    remainder = t.replace(s, "", 1).strip(" .,!?;:—-–")
    return len(remainder) < 5


def _strip_system_from_turn(
    turn: TranscriptTurn,
    system_text: str,
) -> Optional[TranscriptTurn]:
    """Remove system text from a turn. Returns None if nothing remains."""
    turn_text = turn.text.strip()

    if _is_full_turn(turn_text, system_text):
        return None

    sys_lower = system_text.strip().lower()
    turn_lower = turn_text.lower()
    idx = turn_lower.find(sys_lower)

    if idx < 0:
        # Fuzzy fallback: try matching with looser punctuation
        sys_clean = re.sub(r"[^\w\s]", "", sys_lower)
        turn_clean = re.sub(r"[^\w\s]", "", turn_lower)
        idx_clean = turn_clean.find(sys_clean)
        if idx_clean < 0:
            logger.debug("System text not found in turn, keeping as-is: %s", system_text[:80])
            return deepcopy(turn)
        # Map back to original string position (approximate)
        idx = idx_clean

    remaining_text = (turn_text[:idx] + turn_text[idx + len(system_text):]).strip()
    remaining_text = re.sub(r"^[\s.,!?;:—\-–]+|[\s.,!?;:—\-–]+$", "", remaining_text)

    if not remaining_text or len(remaining_text) < 3:
        return None

    new_turn = deepcopy(turn)
    new_turn.text = remaining_text

    if turn.words:
        boundary = _find_word_boundary(turn.words, system_text)
        if boundary >= 0:
            sys_word_count = len([t for t in system_text.split() if _normalize_token(t)])
            new_turn.words = list(turn.words[:boundary]) + list(turn.words[boundary + sys_word_count:])
            if not new_turn.words:
                return None
            # Update timestamp from first remaining word
            if new_turn.words:
                first_word = new_turn.words[0]
                start_sec = first_word.start / 1000.0
                minutes = int(start_sec // 60)
                seconds = int(start_sec % 60)
                new_turn.timestamp = f"[{minutes:02d}:{seconds:02d}]"

    return new_turn


def _make_system_turn(
    turn: TranscriptTurn,
    system_text: str,
) -> TranscriptTurn:
    """Create an AUTOMATED MESSAGE turn from system text within a turn."""
    new_turn = deepcopy(turn)
    new_turn.speaker = "AUTOMATED MESSAGE"
    new_turn.text = system_text.strip()
    new_turn.is_continuation = False

    if turn.words:
        boundary = _find_word_boundary(turn.words, system_text)
        if boundary >= 0:
            sys_word_count = len([t for t in system_text.split() if _normalize_token(t)])
            new_turn.words = list(turn.words[boundary:boundary + sys_word_count])
            if new_turn.words:
                first_word = new_turn.words[0]
                start_sec = first_word.start / 1000.0
                minutes = int(start_sec // 60)
                seconds = int(start_sec % 60)
                new_turn.timestamp = f"[{minutes:02d}:{seconds:02d}]"

    return new_turn


def apply_system_audio_filter(
    turns: List[TranscriptTurn],
    markers: list,
    mode: str,
) -> List[TranscriptTurn]:
    """
    Apply system audio filtering to transcript turns.

    Args:
        turns: Original transcript turns
        markers: List of {"turn": int, "text": str} from the engine
        mode: "exclude" (remove) or "label" (mark as AUTOMATED MESSAGE)

    Returns:
        Filtered/modified list of TranscriptTurn
    """
    if not markers or mode not in ("exclude", "label"):
        return turns

    # Group markers by turn index
    turn_markers: dict = {}
    for m in markers:
        idx = m["turn"]
        if idx not in turn_markers:
            turn_markers[idx] = []
        turn_markers[idx].append(m["text"])

    result: List[TranscriptTurn] = []

    for i, turn in enumerate(turns):
        if i not in turn_markers:
            result.append(turn)
            continue

        system_texts = turn_markers[i]
        is_full = all(_is_full_turn(turn.text, st) for st in system_texts)

        if is_full:
            if mode == "exclude":
                continue  # drop entirely
            else:  # label
                labeled = deepcopy(turn)
                labeled.speaker = "AUTOMATED MESSAGE"
                result.append(labeled)
        else:
            # Partial turn — has both real speech and system text
            if mode == "exclude":
                remaining = deepcopy(turn)
                for st in system_texts:
                    stripped = _strip_system_from_turn(remaining, st)
                    if stripped is None:
                        remaining = None
                        break
                    remaining = stripped
                if remaining:
                    result.append(remaining)
            else:  # label
                # Split into real speech + automated message turns
                remaining = deepcopy(turn)
                automated_turns = []
                for st in system_texts:
                    auto_turn = _make_system_turn(remaining, st)
                    automated_turns.append(auto_turn)
                    stripped = _strip_system_from_turn(remaining, st)
                    if stripped is None:
                        remaining = None
                        break
                    remaining = stripped

                # Add in chronological order
                parts = []
                if remaining:
                    parts.append(remaining)
                parts.extend(automated_turns)
                # Sort by timestamp
                parts.sort(key=lambda t: _turn_start_ms(t))
                result.extend(parts)

    # Collapse consecutive AUTOMATED MESSAGE turns into one.
    # First dedup same-timestamp pairs (both channels), then merge runs.
    if mode == "label":
        # Pass 1: dedup same-timestamp duplicates (keep longer text)
        deduped: List[TranscriptTurn] = []
        for turn in result:
            if (
                turn.speaker == "AUTOMATED MESSAGE"
                and deduped
                and deduped[-1].speaker == "AUTOMATED MESSAGE"
                and deduped[-1].timestamp == turn.timestamp
            ):
                if len(turn.text) > len(deduped[-1].text):
                    deduped[-1] = turn
                continue
            deduped.append(turn)

        # Pass 2: merge consecutive AUTOMATED MESSAGE turns into one
        merged: List[TranscriptTurn] = []
        for turn in deduped:
            if (
                turn.speaker == "AUTOMATED MESSAGE"
                and merged
                and merged[-1].speaker == "AUTOMATED MESSAGE"
            ):
                prev = merged[-1]
                prev.text = prev.text.rstrip() + " " + turn.text.lstrip()
                if prev.words is not None and turn.words is not None:
                    prev.words = list(prev.words) + list(turn.words)
                elif turn.words is not None:
                    prev.words = list(turn.words)
                continue
            merged.append(turn)
        result = merged

    logger.info(
        "System audio filter (%s): %d turns → %d turns (%d markers applied)",
        mode, len(turns), len(result), len(markers),
    )
    return result


def _turn_start_ms(turn: TranscriptTurn) -> float:
    """Get turn start time in ms for sorting."""
    if turn.words:
        valid = [w.start for w in turn.words if w.start is not None and w.start >= 0]
        if valid:
            return min(valid)
    if turn.timestamp:
        match = re.match(r"\[(\d+):(\d+)\]", turn.timestamp)
        if match:
            return (int(match.group(1)) * 60 + int(match.group(2))) * 1000
    return 0.0
