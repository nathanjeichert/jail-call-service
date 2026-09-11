"""Turn-aware passages over a call's line entries.

A passage is the unit both search indexes score: ~100 words of consecutive
transcript, cut on turn boundaries, with 50 % overlap so a moment that
straddles a boundary is whole in one of its two passages. Passages point
at the page's ``lines`` array (first and last line index) rather than
shipping text, so the transcript ships once; the page rebuilds a passage's
text from the same entries the index was built from. A passage also carries
its ``pieces`` (text, speaker bits), one per line, which is all the index
builder reads, so a call's summary passage (one piece, every speaker bit)
needs no special handling downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..models import AUTOMATED_SPEAKER, OUTSIDE_PARTY_LABEL

TARGET_WORDS = 100   # a passage grows past this only to finish its last turn
STEP_WORDS = 50      # the next passage starts about this many words in
MAX_TURN_WORDS = 200  # a single turn longer than this is split by line

SPEAKER_DEFENDANT = 1
SPEAKER_OUTSIDE = 2
SPEAKER_AUTOMATED = 4
SPEAKER_ANY = SPEAKER_DEFENDANT | SPEAKER_OUTSIDE | SPEAKER_AUTOMATED

KIND_TRANSCRIPT = 0
KIND_SUMMARY = 1

# Payload fields that make up a call's summary passage, in order. The page
# joins the same fields (``Search.summaryText``) to rebuild the text.
SUMMARY_FIELDS = ("outside", "inmate", "identity", "facility", "outcome", "call_type", "brief")


def speaker_bit(label: str) -> int:
    """Channel labels are fixed for the outside party and the phone system;
    every other label is the defendant's channel."""
    if label == OUTSIDE_PARTY_LABEL:
        return SPEAKER_OUTSIDE
    if label == AUTOMATED_SPEAKER:
        return SPEAKER_AUTOMATED
    return SPEAKER_DEFENDANT


@dataclass
class Passage:
    call: int                          # index into the page's CALLS array
    first_line: int                    # index into that call's ``lines`` (0 for a summary passage)
    last_line: int                     # inclusive
    start: float                       # seconds, from the first line
    speakers: int                      # OR of the pieces' speaker bits
    pieces: List[Tuple[str, int]]      # (text, speaker bits), one per non-blank line
    kind: int = KIND_TRANSCRIPT

    @property
    def text(self) -> str:
        """What the page rebuilds for this passage: the pieces joined by a space."""
        return " ".join(text for text, _ in self.pieces)


def summary_text(payload: dict) -> str:
    """The searchable text of a call's metadata, summary, and cues."""
    parts = [str(payload.get(f) or "").strip() for f in SUMMARY_FIELDS]
    for cue in payload.get("cues") or []:
        parts.append(str(cue.get("note") or "").strip())
        parts.append(str(cue.get("quote") or "").strip())
    return " · ".join(p for p in parts if p)


def summary_passage(call_index: int, payload: dict) -> Optional[Passage]:
    """One passage for the call's metadata and summary, said by nobody in
    particular, so every speaker filter keeps it."""
    text = summary_text(payload)
    if not text:
        return None
    return Passage(call=call_index, first_line=0, last_line=0, start=0.0, speakers=SPEAKER_ANY,
                   pieces=[(text, SPEAKER_ANY)], kind=KIND_SUMMARY)


def _turn_spans(lines: Sequence[dict], words: Sequence[int]) -> List[List[int]]:
    """Consecutive line indices per turn, long turns split near MAX_TURN_WORDS."""
    spans: List[List[int]] = []
    cur: List[int] = []
    cur_words = 0
    cur_turn = None
    for i, line in enumerate(lines):
        if not words[i]:
            continue
        if cur and (line.get("turn_index") != cur_turn or cur_words + words[i] > MAX_TURN_WORDS):
            spans.append(cur)
            cur, cur_words = [], 0
        cur_turn = line.get("turn_index")
        cur.append(i)
        cur_words += words[i]
    if cur:
        spans.append(cur)
    return spans


def build_passages(call_index: int, lines: Sequence[dict]) -> List[Passage]:
    """Window the call's lines into passages (see the module docstring)."""
    texts = [(line.get("text") or "").strip() for line in lines]
    words = [len(t.split()) for t in texts]
    spans = _turn_spans(lines, words)
    span_words = [sum(words[i] for i in span) for span in spans]
    passages: List[Passage] = []
    start = 0
    while start < len(spans):
        end = start
        count = 0
        while end < len(spans) and (count == 0 or count + span_words[end] <= TARGET_WORDS):
            count += span_words[end]
            end += 1
        if end < len(spans) and count < TARGET_WORDS:
            # Finish on the turn that crosses the target rather than stop short.
            end += 1
        idx = [i for span in spans[start:end] for i in span]
        pieces = [(texts[i], speaker_bit(lines[i].get("speaker") or "")) for i in idx]
        bits = 0
        for _, bit in pieces:
            bits |= bit
        passages.append(Passage(
            call=call_index, first_line=idx[0], last_line=idx[-1],
            start=float(lines[idx[0]].get("start") or 0.0), speakers=bits, pieces=pieces,
        ))
        if end >= len(spans):
            break
        advance, skipped = start, 0
        while advance < end - 1 and skipped < STEP_WORDS:
            skipped += span_words[advance]
            advance += 1
        start = max(advance, start + 1)
    return passages
