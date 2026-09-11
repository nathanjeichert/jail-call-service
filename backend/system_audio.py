"""Filtering of automated telecom messages out of transcripts.

Detection is the summarization engine's job (a separate structured pass for
engines that support it, an inline ``SYSTEM_AUDIO`` tail otherwise); both
produce the same marker shape, ``{"turn": int, "text": str}``, which the
helpers here use to either strip ("exclude") or relabel ("label") the
detected turns (IVR prompts, time warnings, provider sign-offs).
"""

import logging
import re
from copy import deepcopy
from typing import List, Optional

from .formatting import format_timestamp
from .models import AUTOMATED_SPEAKER, TranscriptTurn, WordTimestamp

logger = logging.getLogger(__name__)

FILTER_MODES = ("exclude", "label")

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "was", "with", "you", "your",
}


def _first_timestamp_seconds(text: Optional[str]) -> float:
    """Seconds for the first ``[H:]MM:SS`` timestamp found anywhere in *text*."""
    if not text:
        return 0.0
    match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if not match:
        return 0.0
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def _normalized_tokens(text: str) -> set:
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def remove_system_audio_notes(summary_text: str, markers: list, turns: List[TranscriptTurn]) -> str:
    """Remove NOTES bullets that merely summarize automated telecom messages."""
    if not summary_text or not markers:
        return summary_text

    marker_data = []
    for marker in markers:
        try:
            turn = turns[int(marker["turn"])]
        except (IndexError, KeyError, TypeError, ValueError):
            turn = None
        marker_text = str(marker.get("text", ""))
        marker_data.append({
            "seconds": _first_timestamp_seconds(getattr(turn, "timestamp", "")),
            "normalized": re.sub(r"[^a-z0-9]+", " ", marker_text.lower()).strip(),
            "tokens": _normalized_tokens(marker_text),
        })

    def is_system_note(line: str) -> bool:
        if not re.match(r"^\s*[-•*]\s*\[", line):
            return False
        line_seconds = _first_timestamp_seconds(line)
        line_normalized = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        line_tokens = _normalized_tokens(line)
        for marker in marker_data:
            marker_normalized = marker["normalized"]
            if marker_normalized and (
                marker_normalized in line_normalized
                or (len(line_normalized) > 24 and line_normalized in marker_normalized)
            ):
                return True
            if abs(line_seconds - marker["seconds"]) <= 3 and len(line_tokens & marker["tokens"]) >= 2:
                return True
        return False

    return "\n".join(line for line in summary_text.splitlines() if not is_system_note(line)).strip()


# ────────────────────────── Turn surgery ──────────────────────────

def _normalize_token(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lower())


def _system_word_count(system_text: str) -> int:
    return len([t for t in system_text.split() if _normalize_token(t)])


def _find_word_boundary(words: List[WordTimestamp], system_text: str) -> int:
    """Index of the word where system_text starts within a word list, or -1."""
    system_tokens = [_normalize_token(t) for t in system_text.split() if _normalize_token(t)]
    word_tokens = [_normalize_token(w.text) for w in words]
    if not system_tokens or len(system_tokens) > len(word_tokens):
        return -1
    for i in range(len(word_tokens) - len(system_tokens) + 1):
        if all(word_tokens[i + j] == st for j, st in enumerate(system_tokens)):
            return i
    return -1


def _is_full_turn(turn_text: str, system_text: str) -> bool:
    """True when system_text covers essentially the entire turn."""
    t = turn_text.strip().lower()
    s = system_text.strip().lower()
    if t == s:
        return True
    remainder = t.replace(s, "", 1).strip(" .,!?;:—-–")
    return len(remainder) < 5


def _turn_start_ms(turn: TranscriptTurn) -> float:
    if turn.words:
        valid = [w.start for w in turn.words if w.start is not None and w.start >= 0]
        if valid:
            return min(valid)
    return _first_timestamp_seconds(turn.timestamp) * 1000


def _retime_from_words(turn: TranscriptTurn) -> None:
    if turn.words:
        turn.timestamp = format_timestamp(turn.words[0].start / 1000.0)


def _strip_system_from_turn(turn: TranscriptTurn, system_text: str) -> Optional[TranscriptTurn]:
    """Remove system text from a turn. Returns None if nothing meaningful remains."""
    turn_text = turn.text.strip()
    if _is_full_turn(turn_text, system_text):
        return None

    sys_lower = system_text.strip().lower()
    turn_lower = turn_text.lower()
    idx = turn_lower.find(sys_lower)
    if idx < 0:
        # Fuzzy fallback: match with punctuation stripped (position is approximate).
        sys_clean = re.sub(r"[^\w\s]", "", sys_lower)
        turn_clean = re.sub(r"[^\w\s]", "", turn_lower)
        idx = turn_clean.find(sys_clean)
        if idx < 0:
            logger.debug("System text not found in turn, keeping as-is: %s", system_text[:80])
            return deepcopy(turn)

    remaining_text = (turn_text[:idx] + turn_text[idx + len(system_text):]).strip()
    remaining_text = re.sub(r"^[\s.,!?;:—\-–]+|[\s.,!?;:—\-–]+$", "", remaining_text)
    if not remaining_text or len(remaining_text) < 3:
        return None

    new_turn = deepcopy(turn)
    new_turn.text = remaining_text
    if turn.words:
        boundary = _find_word_boundary(turn.words, system_text)
        if boundary >= 0:
            count = _system_word_count(system_text)
            new_turn.words = list(turn.words[:boundary]) + list(turn.words[boundary + count:])
            if not new_turn.words:
                return None
            _retime_from_words(new_turn)
    return new_turn


def _make_system_turn(turn: TranscriptTurn, system_text: str) -> TranscriptTurn:
    """Create an AUTOMATED MESSAGE turn from the system text within a turn."""
    new_turn = deepcopy(turn)
    new_turn.speaker = AUTOMATED_SPEAKER
    new_turn.text = system_text.strip()
    new_turn.is_continuation = False
    if turn.words:
        boundary = _find_word_boundary(turn.words, system_text)
        if boundary >= 0:
            new_turn.words = list(turn.words[boundary:boundary + _system_word_count(system_text)])
            _retime_from_words(new_turn)
    return new_turn


def _collapse_automated_runs(turns: List[TranscriptTurn]) -> List[TranscriptTurn]:
    """Dedup same-timestamp automated pairs (both channels) and merge consecutive runs."""
    deduped: List[TranscriptTurn] = []
    for turn in turns:
        if (
            turn.speaker == AUTOMATED_SPEAKER
            and deduped
            and deduped[-1].speaker == AUTOMATED_SPEAKER
            and deduped[-1].timestamp == turn.timestamp
        ):
            if len(turn.text) > len(deduped[-1].text):
                deduped[-1] = turn
            continue
        deduped.append(turn)

    merged: List[TranscriptTurn] = []
    for turn in deduped:
        if turn.speaker == AUTOMATED_SPEAKER and merged and merged[-1].speaker == AUTOMATED_SPEAKER:
            prev = merged[-1]
            prev.text = prev.text.rstrip() + " " + turn.text.lstrip()
            if prev.words is not None and turn.words is not None:
                prev.words = list(prev.words) + list(turn.words)
            elif turn.words is not None:
                prev.words = list(turn.words)
            continue
        merged.append(turn)
    return merged


def apply_system_audio_filter(turns: List[TranscriptTurn], markers: list, mode: str) -> List[TranscriptTurn]:
    """Strip (``"exclude"``) or relabel (``"label"``) the turns flagged by *markers*.

    Partial turns (real speech plus system text) are split: the system
    substring is removed, or broken out into its own AUTOMATED MESSAGE turn.
    """
    if not markers or mode not in FILTER_MODES:
        return turns

    turn_markers: dict = {}
    for m in markers:
        turn_markers.setdefault(m["turn"], []).append(m["text"])

    result: List[TranscriptTurn] = []
    for i, turn in enumerate(turns):
        if i not in turn_markers:
            result.append(turn)
            continue

        system_texts = turn_markers[i]
        if all(_is_full_turn(turn.text, st) for st in system_texts):
            if mode == "label":
                labeled = deepcopy(turn)
                labeled.speaker = AUTOMATED_SPEAKER
                result.append(labeled)
            continue

        # Partial turn: real speech plus system text.
        remaining: Optional[TranscriptTurn] = deepcopy(turn)
        automated_turns: List[TranscriptTurn] = []
        for st in system_texts:
            if mode == "label":
                automated_turns.append(_make_system_turn(remaining, st))
            remaining = _strip_system_from_turn(remaining, st)
            if remaining is None:
                break
        parts = ([remaining] if remaining else []) + automated_turns
        parts.sort(key=_turn_start_ms)
        result.extend(parts)

    if mode == "label":
        result = _collapse_automated_runs(result)

    logger.info(
        "System audio filter (%s): %d turns → %d turns (%d markers applied)",
        mode, len(turns), len(result), len(markers),
    )
    return result
