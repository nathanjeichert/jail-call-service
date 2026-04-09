"""
Base protocol and shared utilities for transcription engines.
"""

from collections import Counter
from dataclasses import dataclass
import logging
import re
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Protocol, Tuple

from ..models import TranscriptTurn, WordTimestamp

logger = logging.getLogger(__name__)


# ── Shared utilities ──

_SPEAKER_LETTER_RE = re.compile(r"^[A-Z]$")
_SPEAKER_NUMERIC_RE = re.compile(r"^[0-9]+$")
_MATCH_INITIAL_WINDOW_SEC = 45.0
_MATCH_CLUSTER_GAP_SEC = 30.0
_MAX_SPAN_GAP_SEC = 18.0
_SHORT_MATCH_THRESHOLD = 0.92
_AUDIO_SUPPORT_BUFFER_SEC = 2.0
_SYSTEM_DUP_MAX_START_GAP_SEC = 1.25
_SYSTEM_DUP_MAX_END_GAP_SEC = 2.5
_SYSTEM_DUP_MAX_DURATION_SEC = 18.0
_SYSTEM_DUP_MIN_OVERLAP_TOKENS = 4
_SYSTEM_DUP_MIN_SCORE = 0.88
_SYSTEM_DUP_MAX_SPAN_TURNS = 2
_SYSTEM_EDGE_START_FALLBACK_SCAN_SEC = 60.0
_SYSTEM_EDGE_START_BUFFER_SEC = 20.0
_SYSTEM_EDGE_TAIL_SCAN_SEC = 25.0
_SYSTEM_EDGE_MIN_CHAR_COUNT = 18
_SYSTEM_EDGE_CUE_THRESHOLD = 3

_SYSTEM_CUE_TOKENS = frozenset({
    "<num>",
    "accept",
    "balance",
    "call",
    "calling",
    "collect",
    "connected",
    "connection",
    "correctional",
    "debit",
    "english",
    "facility",
    "hold",
    "inmate",
    "minute",
    "minutes",
    "monitored",
    "number",
    "operator",
    "phone",
    "pin",
    "prepaid",
    "press",
    "recorded",
    "refuse",
    "remaining",
    "thank",
    "using",
    "wait",
})

_SYSTEM_STRONG_PATTERNS = (
    re.compile(r"\bthank you for using\b"),
    re.compile(r"\b(?:you have )?<num> minute(?:s)? remaining\b"),
    re.compile(r"\bplease hold\b"),
    re.compile(r"\bplease wait\b"),
    re.compile(r"\bprepaid call\b"),
    re.compile(r"\bcollect call\b"),
    re.compile(r"\bdebit call\b"),
    re.compile(r"\baccept this call\b"),
    re.compile(r"\brefuse this call\b"),
    re.compile(r"\bcurrent balance\b"),
    re.compile(r"\bpin number\b"),
    re.compile(r"\bphone number\b"),
    re.compile(r"\bcorrectional facility\b"),
    re.compile(r"\bcall is being connected\b"),
    re.compile(r"\bcall may be recorded\b"),
    re.compile(r"\bcall is from\b"),
)


@dataclass(frozen=True)
class _Span:
    indices: Tuple[int, ...]
    speaker: str
    start_sec: float
    end_sec: float
    norm_text: str
    tokens: Counter[str]
    token_count: int
    char_count: int


@dataclass(frozen=True)
class _SpanMatch:
    left: _Span
    right: _Span
    score: float
    start_sec: float
    end_sec: float
    audio_supported: bool
    substantive: bool


def normalize_speaker_label(raw_value: object, fallback: str = "SPEAKER A") -> str:
    fallback_value = str(fallback or "").strip().upper() or "SPEAKER A"
    candidate = str(raw_value or "").strip()
    candidate = re.sub(r":+$", "", candidate).strip().upper()

    if not candidate or candidate == "UNKNOWN":
        return fallback_value

    if candidate.startswith("SPEAKER"):
        suffix = candidate[len("SPEAKER"):].strip()
        return f"SPEAKER {suffix}" if suffix else "SPEAKER"

    if _SPEAKER_LETTER_RE.fullmatch(candidate) or _SPEAKER_NUMERIC_RE.fullmatch(candidate):
        return f"SPEAKER {candidate}"

    return candidate


def strip_preamble(
    turns: List[TranscriptTurn],
    *,
    correlation_boundary_sec: Optional[float] = None,
    correlation_regions: Optional[List[Tuple[float, float]]] = None,
    similarity_threshold: float = 0.6,
    max_time_gap_sec: float = 3.0,
    max_scan_sec: float = 180.0,
    max_span_turns: int = 3,
) -> List[TranscriptTurn]:
    """
    Remove the automated robocall preamble that appears duplicated on both
    channels at the start of jail calls.

    Scans turns within the preamble window and matches short spans of turns
    across speakers. This handles ASR segmentation drift where one channel
    may merge or split automated prompts differently than the other.
    Unmatched turns are preserved by default so one-sided real dialogue
    during the preamble is not dropped.

    Args:
        turns: Merged, chronologically sorted turns from both channels.
        correlation_boundary_sec: If provided (from audio cross-correlation),
            only consider turns up to this timestamp for removal.
        correlation_regions: Optional list of (start_sec, end_sec) windows
            where both channels carried highly correlated audio. Used to
            support short prompt fragments in Parakeet mode without relying
            on aggressive blanket gap-fill.
        similarity_threshold: Minimum score for a substantive cross-channel
            span match.
        max_time_gap_sec: Maximum padding (seconds) allowed between spans
            when considering them the same prompt.
        max_scan_sec: Stop scanning for preamble after this many seconds
            into the call (safety limit).
        max_span_turns: Maximum number of consecutive same-speaker turns to
            combine into a candidate span for matching.
    """
    if len(turns) < 2:
        return turns

    scan_limit = min(correlation_boundary_sec or max_scan_sec, max_scan_sec)
    spans_by_speaker = _build_spans_by_speaker(
        turns,
        scan_limit=scan_limit,
        max_span_turns=max_span_turns,
    )
    candidates = _build_span_match_candidates(
        spans_by_speaker,
        correlation_regions=correlation_regions,
        similarity_threshold=similarity_threshold,
        max_time_gap_sec=max_time_gap_sec,
    )
    if not candidates:
        return turns

    strong_candidates = [c for c in candidates if c.substantive]
    short_candidates = [c for c in candidates if not c.substantive]
    selected_matches = _select_prefix_cluster(strong_candidates, scan_limit=scan_limit)

    # Short exact prompt fragments like "Please hold." are only stripped when
    # the audio evidence says they overlap shared robocall regions.
    if selected_matches and correlation_regions:
        selected_matches = _expand_cluster_with_short_matches(
            selected_matches,
            short_candidates,
        )

    if not selected_matches:
        return turns

    preamble_indices = _collect_match_indices(selected_matches)

    if preamble_indices:
        logger.info(
            "Preamble: stripped %d turns (up to ~%.0fs into call)",
            len(preamble_indices),
            max(_turn_end_sec(turns[i]) or _turn_start_sec(turns[i]) or 0 for i in preamble_indices),
        )

    return [t for i, t in enumerate(turns) if i not in preamble_indices]


def strip_shared_system_turns(
    turns: List[TranscriptTurn],
    *,
    similarity_threshold: float = _SYSTEM_DUP_MIN_SCORE,
    min_overlap_tokens: int = _SYSTEM_DUP_MIN_OVERLAP_TOKENS,
    max_start_gap_sec: float = _SYSTEM_DUP_MAX_START_GAP_SEC,
    max_end_gap_sec: float = _SYSTEM_DUP_MAX_END_GAP_SEC,
    max_duration_sec: float = _SYSTEM_DUP_MAX_DURATION_SEC,
    max_span_turns: int = _SYSTEM_DUP_MAX_SPAN_TURNS,
) -> List[TranscriptTurn]:
    """
    Remove duplicated short system-style prompts that appear on both channels
    outside the call preamble, such as countdown warnings or provider outro
    messages.

    This pass is intentionally strict:
    - timestamps must line up very closely across channels
    - texts must share several words and high similarity
    - spans must stay relatively short

    That keeps normal human exchanges like both speakers saying "yeah" from
    being stripped.
    """
    if len(turns) < 2:
        return turns

    spans_by_speaker = _build_spans_by_speaker(
        turns,
        scan_limit=float("inf"),
        max_span_turns=max_span_turns,
    )
    candidates = _build_span_match_candidates(
        spans_by_speaker,
        correlation_regions=None,
        similarity_threshold=similarity_threshold,
        max_time_gap_sec=max_start_gap_sec,
    )

    selected: List[_SpanMatch] = []
    used_indices: set[int] = set()
    for candidate in candidates:
        if _match_overlaps_used(candidate, used_indices):
            continue
        if not _is_system_duplicate_candidate(
            candidate,
            min_overlap_tokens=min_overlap_tokens,
            max_start_gap_sec=max_start_gap_sec,
            max_end_gap_sec=max_end_gap_sec,
            max_duration_sec=max_duration_sec,
            similarity_threshold=similarity_threshold,
        ):
            continue
        selected.append(candidate)
        used_indices.update(candidate.left.indices)
        used_indices.update(candidate.right.indices)

    if not selected:
        return turns

    duplicate_indices = _collect_match_indices(selected)
    logger.info(
        "Shared system audio: stripped %d turns across %d matched spans",
        len(duplicate_indices),
        len(selected),
    )
    return [t for i, t in enumerate(turns) if i not in duplicate_indices]


def strip_edge_system_turns(
    turns: List[TranscriptTurn],
    *,
    correlation_boundary_sec: Optional[float] = None,
    tail_scan_sec: float = _SYSTEM_EDGE_TAIL_SCAN_SEC,
    start_buffer_sec: float = _SYSTEM_EDGE_START_BUFFER_SEC,
) -> List[TranscriptTurn]:
    """
    Remove strong machine-style prompts that survive only on one channel at the
    very start or end of the call.

    This is intentionally narrower than duplicate stripping: it only trims
    contiguous edge spans that look strongly like telecom/jail-call system
    language. That keeps genuine one-sided speech such as "hello?" intact.
    """
    if not turns:
        return turns

    remove_indices: set[int] = set()
    start_scan_limit = (
        (correlation_boundary_sec + start_buffer_sec)
        if correlation_boundary_sec is not None
        else _SYSTEM_EDGE_START_FALLBACK_SCAN_SEC
    )

    for idx, turn in enumerate(turns):
        start_sec = _turn_start_sec(turn)
        if start_sec is None or start_sec > start_scan_limit:
            break
        if not _looks_like_system_message(turn):
            break
        remove_indices.add(idx)

    call_end_sec = max((_turn_end_sec(turn) or 0.0) for turn in turns)
    for idx in range(len(turns) - 1, -1, -1):
        turn = turns[idx]
        start_sec = _turn_start_sec(turn)
        if start_sec is None or start_sec < call_end_sec - tail_scan_sec:
            break
        if not _looks_like_system_message(turn):
            break
        remove_indices.add(idx)

    if remove_indices:
        logger.info("Edge system audio: stripped %d one-sided edge turns", len(remove_indices))

    return [turn for idx, turn in enumerate(turns) if idx not in remove_indices]


def _build_spans_by_speaker(
    turns: List[TranscriptTurn],
    *,
    scan_limit: float,
    max_span_turns: int,
) -> Dict[str, List[_Span]]:
    spans: Dict[str, List[_Span]] = {}
    current_speaker = None
    current_run: List[int] = []

    def _flush_run() -> None:
        if not current_run or current_speaker is None:
            return
        speaker_spans = spans.setdefault(current_speaker, [])
        for pos in range(len(current_run)):
            prev_end = None
            span_indices: List[int] = []
            for next_idx in current_run[pos : pos + max_span_turns]:
                turn_start = _turn_start_sec(turns[next_idx])
                turn_end = _turn_end_sec(turns[next_idx])
                if turn_start is None or turn_end is None:
                    break
                if prev_end is not None and turn_start - prev_end > _MAX_SPAN_GAP_SEC:
                    break
                span_indices.append(next_idx)
                prev_end = turn_end
                speaker_spans.append(_make_span(turns, current_speaker, span_indices))

    for idx, turn in enumerate(turns):
        start = _turn_start_sec(turn)
        if start is None:
            continue
        if start > scan_limit:
            break

        speaker = _speaker_key(turn.speaker)
        if speaker != current_speaker:
            _flush_run()
            current_speaker = speaker
            current_run = [idx]
        else:
            current_run.append(idx)

    _flush_run()
    return spans


def _make_span(turns: List[TranscriptTurn], speaker: str, indices: Iterable[int]) -> _Span:
    ordered_indices = tuple(indices)
    text = " ".join(turns[i].text for i in ordered_indices)
    norm_text = _normalize_match_text(text)
    tokens = Counter(norm_text.split()) if norm_text else Counter()
    start_sec = _turn_start_sec(turns[ordered_indices[0]]) or 0.0
    end_sec = _turn_end_sec(turns[ordered_indices[-1]]) or start_sec
    return _Span(
        indices=ordered_indices,
        speaker=speaker,
        start_sec=start_sec,
        end_sec=end_sec,
        norm_text=norm_text,
        tokens=tokens,
        token_count=sum(tokens.values()),
        char_count=len(norm_text),
    )


def _build_span_match_candidates(
    spans_by_speaker: Dict[str, List[_Span]],
    *,
    correlation_regions: Optional[List[Tuple[float, float]]],
    similarity_threshold: float,
    max_time_gap_sec: float,
) -> List[_SpanMatch]:
    speakers = sorted(spans_by_speaker)
    candidates: List[_SpanMatch] = []

    for idx, speaker_a in enumerate(speakers):
        for speaker_b in speakers[idx + 1 :]:
            for span_a in spans_by_speaker[speaker_a]:
                for span_b in spans_by_speaker[speaker_b]:
                    if not _spans_are_time_compatible(span_a, span_b, max_time_gap_sec):
                        continue

                    score, overlap = _match_score(span_a, span_b)
                    if overlap == 0:
                        continue

                    audio_supported = (
                        _span_has_audio_support(span_a, correlation_regions)
                        or _span_has_audio_support(span_b, correlation_regions)
                    )
                    substantive = _is_substantive_span(span_a) or _is_substantive_span(span_b)

                    if substantive:
                        if score < similarity_threshold or overlap < 3:
                            continue
                    else:
                        if not audio_supported or score < _SHORT_MATCH_THRESHOLD or overlap < 2:
                            continue

                    candidates.append(_SpanMatch(
                        left=span_a,
                        right=span_b,
                        score=score,
                        start_sec=min(span_a.start_sec, span_b.start_sec),
                        end_sec=max(span_a.end_sec, span_b.end_sec),
                        audio_supported=audio_supported,
                        substantive=substantive,
                    ))

    candidates.sort(
        key=lambda match: (
            match.start_sec,
            -match.score,
            -(len(match.left.indices) + len(match.right.indices)),
            match.end_sec,
        )
    )
    return candidates


def _is_system_duplicate_candidate(
    match: _SpanMatch,
    *,
    min_overlap_tokens: int,
    max_start_gap_sec: float,
    max_end_gap_sec: float,
    max_duration_sec: float,
    similarity_threshold: float,
) -> bool:
    overlap = sum((match.left.tokens & match.right.tokens).values())
    if overlap < min_overlap_tokens:
        return False

    if match.score < similarity_threshold:
        return False

    start_gap = abs(match.left.start_sec - match.right.start_sec)
    end_gap = abs(match.left.end_sec - match.right.end_sec)
    if start_gap > max_start_gap_sec or end_gap > max_end_gap_sec:
        return False

    if (
        _span_duration_sec(match.left) > max_duration_sec
        or _span_duration_sec(match.right) > max_duration_sec
    ):
        return False

    if (
        max(match.left.token_count, match.right.token_count) < min_overlap_tokens
        or max(match.left.char_count, match.right.char_count) < 18
    ):
        return False

    return True


def _select_prefix_cluster(
    candidates: List[_SpanMatch],
    *,
    scan_limit: float,
) -> List[_SpanMatch]:
    if not candidates:
        return []

    selected: List[_SpanMatch] = []
    used_indices: set[int] = set()
    cluster_end = None
    initial_window = min(_MATCH_INITIAL_WINDOW_SEC, scan_limit)

    while True:
        eligible: List[_SpanMatch] = []
        for candidate in candidates:
            if _match_overlaps_used(candidate, used_indices):
                continue
            if cluster_end is None:
                if candidate.start_sec > initial_window:
                    break
                eligible.append(candidate)
            else:
                if candidate.start_sec > cluster_end + _MATCH_CLUSTER_GAP_SEC:
                    break
                eligible.append(candidate)

        if not eligible:
            break

        earliest_start = min(match.start_sec for match in eligible)
        bucket = [
            match for match in eligible
            if match.start_sec <= earliest_start + 1.0
        ]
        chosen = max(
            bucket,
            key=lambda match: (
                match.score,
                len(match.left.indices) + len(match.right.indices),
                match.end_sec,
            ),
        )
        selected.append(chosen)
        used_indices.update(chosen.left.indices)
        used_indices.update(chosen.right.indices)
        cluster_end = max(cluster_end or 0.0, chosen.end_sec)

    return selected


def _expand_cluster_with_short_matches(
    selected: List[_SpanMatch],
    candidates: List[_SpanMatch],
) -> List[_SpanMatch]:
    expanded = list(selected)
    used_indices = _collect_match_indices(expanded)
    cluster_end = max(match.end_sec for match in expanded)

    while True:
        eligible = [
            match for match in candidates
            if match.audio_supported
            and not _match_overlaps_used(match, used_indices)
            and match.start_sec <= cluster_end + _MATCH_CLUSTER_GAP_SEC
        ]
        if not eligible:
            break

        earliest_start = min(match.start_sec for match in eligible)
        bucket = [
            match for match in eligible
            if match.start_sec <= earliest_start + 1.0
        ]
        chosen = max(
            bucket,
            key=lambda match: (
                match.score,
                len(match.left.indices) + len(match.right.indices),
                match.end_sec,
            ),
        )
        expanded.append(chosen)
        used_indices.update(chosen.left.indices)
        used_indices.update(chosen.right.indices)
        cluster_end = max(cluster_end, chosen.end_sec)

    return expanded


def _collect_match_indices(matches: Iterable[_SpanMatch]) -> set[int]:
    indices: set[int] = set()
    for match in matches:
        indices.update(match.left.indices)
        indices.update(match.right.indices)
    return indices


def _match_overlaps_used(match: _SpanMatch, used_indices: set[int]) -> bool:
    return any(idx in used_indices for idx in match.left.indices + match.right.indices)


def _spans_are_time_compatible(span_a: _Span, span_b: _Span, padding_sec: float) -> bool:
    return (
        span_a.start_sec <= span_b.end_sec + padding_sec
        and span_b.start_sec <= span_a.end_sec + padding_sec
    )


def _is_substantive_span(span: _Span) -> bool:
    return span.token_count >= 4 or span.char_count >= 20


def _match_score(span_a: _Span, span_b: _Span) -> Tuple[float, int]:
    seq_ratio = SequenceMatcher(None, span_a.norm_text, span_b.norm_text).ratio()
    overlap = sum((span_a.tokens & span_b.tokens).values())
    shorter = min(span_a.token_count, span_b.token_count) or 1
    overlap_ratio = overlap / shorter
    containment = _containment_ratio(span_a.norm_text, span_b.norm_text)
    score = max(
        seq_ratio,
        (seq_ratio * 0.6) + (overlap_ratio * 0.4),
        containment,
    )
    return score, overlap


def _containment_ratio(text_a: str, text_b: str) -> float:
    if not text_a or not text_b:
        return 0.0
    shorter, longer = sorted((text_a, text_b), key=len)
    if shorter in longer:
        return len(shorter) / len(longer)
    return 0.0


def _normalize_match_text(text: str) -> str:
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\$?\d+(?:\.\d+)?", " <num> ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _looks_like_system_message(turn: TranscriptTurn) -> bool:
    norm_text = _normalize_match_text(turn.text)
    if len(norm_text) < _SYSTEM_EDGE_MIN_CHAR_COUNT:
        return False

    if any(pattern.search(norm_text) for pattern in _SYSTEM_STRONG_PATTERNS):
        return True

    tokens = norm_text.split()
    cue_hits = sum(1 for token in tokens if token in _SYSTEM_CUE_TOKENS)
    if cue_hits < _SYSTEM_EDGE_CUE_THRESHOLD:
        return False

    # Prefer obviously templated language over everyday conversation.
    unique_ratio = len(set(tokens)) / max(len(tokens), 1)
    return unique_ratio < 0.95 or "call" in tokens


def _speaker_key(speaker: str) -> str:
    return str(speaker or "").strip().upper()


def _span_has_audio_support(
    span: _Span,
    regions: Optional[List[Tuple[float, float]]],
) -> bool:
    if not regions:
        return False
    for region_start, region_end in regions:
        if span.start_sec <= region_end + _AUDIO_SUPPORT_BUFFER_SEC and span.end_sec >= region_start - _AUDIO_SUPPORT_BUFFER_SEC:
            return True
    return False


def _span_duration_sec(span: _Span) -> float:
    return max(0.0, span.end_sec - span.start_sec)


def _turn_start_sec(turn: TranscriptTurn) -> Optional[float]:
    """Extract turn start time in seconds from words or timestamp string."""
    if turn.words:
        return turn.words[0].start / 1000.0  # ms -> sec
    if turn.timestamp:
        m = re.match(r"\[(\d+):(\d+)\]", turn.timestamp)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _turn_end_sec(turn: TranscriptTurn) -> Optional[float]:
    """Extract turn end time in seconds from words or timestamp string."""
    if turn.words:
        return turn.words[-1].end / 1000.0  # ms -> sec
    return _turn_start_sec(turn)


def mark_continuation_turns(turns: List[TranscriptTurn]) -> List[TranscriptTurn]:
    prev_speaker = None
    for turn in turns:
        normalized = turn.speaker.strip().upper()
        turn.is_continuation = prev_speaker is not None and normalized == prev_speaker
        prev_speaker = normalized
    return turns


# ── Engine protocol ──

class TranscriptionEngine(Protocol):
    """Interface that all transcription engines must implement."""

    async def transcribe(
        self,
        audio_path: str,
        channel_labels: Optional[Dict[int, str]] = None,
    ) -> List[TranscriptTurn]:
        """
        Transcribe a 2-channel audio file and return speaker-attributed turns.

        Args:
            audio_path: Path to the audio file (MP3 or WAV).
            channel_labels: Mapping of channel index to speaker name,
                            e.g. {1: "INMATE", 2: "OUTSIDE PARTY"}.

        Returns:
            Ordered list of TranscriptTurn objects.
        """
        ...
