"""Height-based pagination of the transcript PDF's summary sheet(s).

The summary pages are fixed-size sheets, so Python must decide how many
review cues fit on page 1 (below the assessment band and context cards) and
how many spill to overflow pages. Heights are estimated from measured font
advance widths (:mod:`font_metrics`) inflated by a safety factor so the
estimate errs long and Chromium never clips a row.

:mod:`backend.summaries` also uses :func:`paginate_structured_summary` to
trim a summary's note set until it fits the page budget for its relevance
tier, which is why this module lives apart from the PDF builder itself.
"""

from typing import List

from . import font_metrics as FM

inch = 72.0
PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0  # US letter, points

# Sheet geometry ("Record" design language). The mockup sheets are 816px
# wide (96px/in); multiply mockup px by 0.75 for pt.
SUMMARY_LEFT = 46.5
SUMMARY_RIGHT = 40.5
SUMMARY_WIDTH = PAGE_WIDTH - SUMMARY_LEFT - SUMMARY_RIGHT
SUMMARY_CONTENT_TOP = 116.0
SUMMARY_CONTENT_BOTTOM = 56.0
SUMMARY_CONTENT_HEIGHT = PAGE_HEIGHT - SUMMARY_CONTENT_TOP - SUMMARY_CONTENT_BOTTOM
SUMMARY_ASSESSMENT_HEIGHT = 36.0
SUMMARY_SECTION_GAP = 18.0
SUMMARY_CARD_GAP = 14.0
SUMMARY_CONTEXT_TWO_COL_GAP = 16.5
SUMMARY_CARD_TITLE_BLOCK = 20.0   # context-card h4 + rule + gap below
SUMMARY_NOTES_HEADING_HEIGHT = 22.0
SUMMARY_NOTES_TABLE_BOTTOM = 6.0
SUMMARY_NO_NOTES_HEIGHT = 56.0
SUMMARY_CUE_TIME_WIDTH = 42.0
SUMMARY_CUE_CITE_WIDTH = 72.0
SUMMARY_CUE_GRID_GAP = 12.0
SUMMARY_CUE_TEXT_WIDTH = (
    SUMMARY_WIDTH
    - SUMMARY_CUE_TIME_WIDTH
    - SUMMARY_CUE_CITE_WIDTH
    - 2 * SUMMARY_CUE_GRID_GAP
)
SUMMARY_CUE_ROW_PADDING = 18.75
SUMMARY_WRAP_WIDTH_RESERVE = 0.03 * inch
SUMMARY_QUOTE_GLYPHS = "“”"

SUMMARY_CARD_BODY_FONT = "Source Serif 4"
SUMMARY_CARD_BODY_SIZE = 9.75
SUMMARY_CARD_BODY_LINE_HEIGHT = 15.6
SUMMARY_QUOTE_FONT = "Source Serif 4"
SUMMARY_QUOTE_SIZE = 9.4
SUMMARY_QUOTE_LINE_HEIGHT = 14.1
SUMMARY_QUOTE_TOP_GAP = 3.0
SUMMARY_NOTE_FONT = "Source Sans 3 SemiBold"
SUMMARY_NOTE_SIZE = 9.4
SUMMARY_NOTE_LINE_HEIGHT = 13.6
SUMMARY_MONO_LINE_HEIGHT = 10.8   # cue timestamp / cite, single line


def _estimated_width(text: str, font_name: str, font_size: float) -> float:
    """Estimated rendered width with the conservative safety factor applied."""
    return FM.string_width(text, font_name, font_size) * FM.SAFETY_FACTOR


def _wrap_text_to_width(text: str, max_width: float, *, font_name: str, font_size: float) -> List[str]:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return []

    max_width = max(max_width - SUMMARY_WRAP_WIDTH_RESERVE, 1.0)
    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        candidate = " ".join(current + [word]).strip()
        if current and _estimated_width(candidate, font_name, font_size) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [text]


def _estimate_context_card_height(text: str, inner_width: float) -> float:
    if not text:
        return 0.0
    body_lines = max(
        len(_wrap_text_to_width(text, inner_width, font_name=SUMMARY_CARD_BODY_FONT, font_size=SUMMARY_CARD_BODY_SIZE)),
        1,
    )
    return SUMMARY_CARD_TITLE_BLOCK + (body_lines * SUMMARY_CARD_BODY_LINE_HEIGHT)


def _choose_context_layout(speakers: str, call_summary: str) -> str:
    has_speakers = bool(speakers)
    has_summary = bool(call_summary)
    if has_speakers and has_summary:
        two_col_inner_width = (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
        speaker_height = _estimate_context_card_height(speakers, two_col_inner_width)
        summary_height = _estimate_context_card_height(call_summary, two_col_inner_width)
        if max(speaker_height, summary_height) <= 2.2 * inch:
            return "side-by-side"
        return "stacked"
    if has_speakers or has_summary:
        return "single"
    return "none"


def _estimate_context_height(layout: str, speakers: str, call_summary: str) -> float:
    if layout == "none":
        return 0.0

    if layout == "single":
        text = speakers or call_summary
        return _estimate_context_card_height(text, SUMMARY_WIDTH)

    if layout == "stacked":
        total = 0.0
        if speakers:
            total += _estimate_context_card_height(speakers, SUMMARY_WIDTH)
        if call_summary:
            if total:
                total += SUMMARY_CARD_GAP
            total += _estimate_context_card_height(call_summary, SUMMARY_WIDTH)
        return total

    two_col_inner_width = (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
    return max(
        _estimate_context_card_height(speakers, two_col_inner_width),
        _estimate_context_card_height(call_summary, two_col_inner_width),
    )


def _estimate_cue_height(cue: dict) -> float:
    """Estimated rendered height of one note row (time | body | cite grid)."""
    text_height = 0.0
    if cue.get("note"):
        note_lines = len(
            _wrap_text_to_width(cue.get("note", ""), SUMMARY_CUE_TEXT_WIDTH, font_name=SUMMARY_NOTE_FONT, font_size=SUMMARY_NOTE_SIZE)
        )
        text_height += note_lines * SUMMARY_NOTE_LINE_HEIGHT
    if cue.get("quote"):
        # The speaker prefix renders in small sans caps ahead of the serif
        # quote; measuring the whole line as serif italic over-predicts,
        # which is the safe direction.
        prefix = f"{cue.get('speaker', '')}: " if cue.get("speaker") else ""
        quote_text = f"{prefix}{SUMMARY_QUOTE_GLYPHS[0]}{cue.get('quote', '')}{SUMMARY_QUOTE_GLYPHS[1]}"
        quote_lines = len(
            _wrap_text_to_width(quote_text, SUMMARY_CUE_TEXT_WIDTH, font_name=SUMMARY_QUOTE_FONT, font_size=SUMMARY_QUOTE_SIZE)
        )
        text_height += SUMMARY_QUOTE_TOP_GAP + (quote_lines * SUMMARY_QUOTE_LINE_HEIGHT)

    content_height = max(SUMMARY_MONO_LINE_HEIGHT, text_height, SUMMARY_NOTE_LINE_HEIGHT)
    return content_height + SUMMARY_CUE_ROW_PADDING


def paginate_structured_summary(
    review_cues: List[dict],
    *,
    speakers: str = "",
    call_summary: str = "",
) -> dict:
    """Pack summary notes by estimated rendered height instead of fixed counts."""
    context_layout = _choose_context_layout(speakers, call_summary)
    context_height = _estimate_context_height(context_layout, speakers, call_summary)

    page1_budget = SUMMARY_CONTENT_HEIGHT - SUMMARY_ASSESSMENT_HEIGHT - SUMMARY_SECTION_GAP
    if context_height:
        page1_budget -= context_height + SUMMARY_SECTION_GAP
    page1_budget -= SUMMARY_NOTES_HEADING_HEIGHT
    if review_cues:
        page1_budget -= SUMMARY_NOTES_TABLE_BOTTOM
    else:
        page1_budget -= SUMMARY_NO_NOTES_HEIGHT

    overflow_budget = SUMMARY_CONTENT_HEIGHT - SUMMARY_NOTES_HEADING_HEIGHT - SUMMARY_NOTES_TABLE_BOTTOM
    page_budgets = [max(page1_budget, 0.0)]
    pages: List[List[dict]] = [[]]

    current_page = 0
    remaining = page_budgets[0]
    for cue in review_cues:
        cue_height = _estimate_cue_height(cue)
        if pages[current_page] and cue_height > remaining:
            pages.append([])
            current_page += 1
            page_budgets.append(max(overflow_budget, 0.0))
            remaining = page_budgets[current_page]

        pages[current_page].append(cue)
        remaining -= cue_height

    return {
        "context_layout": context_layout,
        "page1_review_cues": pages[0] if pages else [],
        "overflow_review_cue_pages": [
            {"review_cues": page_cues}
            for page_cues in pages[1:]
            if page_cues
        ],
    }
