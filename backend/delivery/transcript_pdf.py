"""Per-call transcript PDF.

Produces a single-render PDF document:
  Sheet 1: Title page (case info, file metadata)
  Sheet 2+: AI summary sheet(s) (if a summary is provided)
  Remaining sheets: Legal-formatted transcript (25 lines/page, Courier,
                    line numbers)

Every page is an explicit fixed-size 8.5in x 11in sheet in one Jinja
template (``templates/transcript_pdf.html``) rendered by headless Chromium
via :func:`pdf_render.render_pdf`. Pagination is decided entirely in Python
(:mod:`backend.transcript_layout` for the transcript, :mod:`summary_layout`
for the summary sheets); Chromium never makes a page-break decision, and the
rendered page count is asserted against the emitted sheet count after every
render.
"""

import html
import io
import re
from collections import defaultdict
from typing import Dict, List, Optional

from ..formatting import (
    format_display_datetime,
    parse_call_datetime,
    safe_text,
    shorten_middle,
)
from ..models import TranscriptTurn
from ..summaries import parse_summary_sections
from ..transcript_layout import LINES_PER_PAGE, MAX_LINE_CHARS, compute_line_entries, hydrate_review_cues
from .fonts import pdf_font_css
from .pdf_render import render_pdf
from .summary_layout import PAGE_HEIGHT, inch, paginate_structured_summary
from .templates import render_template

RELEVANCE_DESC: Dict[str, str] = {
    "HIGH": "Potentially jury-relevant or case-substantive content",
    "MEDIUM": "Substantive legal or case context, not clearly central",
    "LOW": "Little to no apparent case relevance",
}

# ── Transcript sheet geometry (legal deposition style) ──
PDF_MARGIN_TOP = 0.75 * inch
PDF_MARGIN_BOTTOM = 0.75 * inch
PDF_LINE_HEIGHT = 25.0
PDF_TEXT_SIZE = 12
PDF_PAGE_NUMBER_SIZE = 10

# Vertical rule positions
PDF_LINE_NUM_RIGHT = 0.78 * inch      # right edge of line numbers
PDF_RULE_LEFT_INNER = 0.97 * inch     # left double-line (inner)
PDF_RULE_RIGHT = 7.4 * inch           # right single line

# Courier (and metric-compatible fonts such as the vendored Courier Prime)
# advances exactly 0.6 em per character, i.e. 7.2 pt at 12 pt. The ruled
# corridor must yield exactly ``transcript_layout.MAX_LINE_CHARS`` columns;
# every page:line citation in the product depends on it, so the geometry is
# checked against the pinned constant at import time.
_COURIER_CHAR_W = 7.2
_TRANSCRIPT_RULE_WIDTH = PDF_RULE_RIGHT - PDF_RULE_LEFT_INNER
_GEOMETRY_LINE_CHARS = int((_TRANSCRIPT_RULE_WIDTH - 12) / _COURIER_CHAR_W)
assert _GEOMETRY_LINE_CHARS == MAX_LINE_CHARS, (
    f"Transcript corridor yields {_GEOMETRY_LINE_CHARS} columns but "
    f"transcript_layout pins {MAX_LINE_CHARS}; this would shift every "
    "page:line citation in the product. Fix the arithmetic, not the value."
)
PDF_TEXT_BLOCK_WIDTH = MAX_LINE_CHARS * _COURIER_CHAR_W
PDF_TEXT_X = PDF_RULE_LEFT_INNER + ((_TRANSCRIPT_RULE_WIDTH - PDF_TEXT_BLOCK_WIDTH) / 2.0)

# Chromium line-box baseline offsets for the vendored Courier Prime
# (unitsPerEm 2048, hhea ascent 1600, descent -700). For a line box of
# height L and font size S the baseline sits at
#     (L - S*(asc+desc)) / 2 + S*asc
# from the top of the box.
_COURIER_ASCENT_EM = 1600.0 / 2048.0
_COURIER_DESCENT_EM = 700.0 / 2048.0


def _courier_baseline_in_box(box_height: float, font_size: float) -> float:
    content = font_size * (_COURIER_ASCENT_EM + _COURIER_DESCENT_EM)
    return ((box_height - content) / 2.0) + (font_size * _COURIER_ASCENT_EM)


# ────────────────────────── Cover + summary context ──────────────────────────

def _raw_summary_blocks(body: str) -> list:
    """Split an unstructured summary into text/bullet blocks for the fallback sheet."""
    raw_blocks: list = []
    for para in re.split(r'\n{2,}', body):
        para = para.strip()
        if not para:
            continue
        bullets: list = []
        texts: list = []
        for line in (raw.strip() for raw in para.split("\n") if raw.strip()):
            if re.match(r'^[-•*]\s', line):
                if texts:
                    raw_blocks.append({"type": "text", "text": " ".join(texts)})
                    texts = []
                bullets.append(re.sub(r'^[-•*]\s*', '', line))
            else:
                if bullets:
                    raw_blocks.append({"type": "bullet", "bullets": bullets})
                    bullets = []
                texts.append(line)
        if bullets:
            raw_blocks.append({"type": "bullet", "bullets": bullets})
        if texts:
            raw_blocks.append({"type": "text", "text": " ".join(texts)})
    return raw_blocks


def _build_cover_context(
    title_data: dict,
    summary: Optional[str],
    line_entries: Optional[List[dict]] = None,
) -> dict:
    """Build the Jinja context for the title sheet and summary sheet(s)."""
    case_name = safe_text(title_data.get("CASE_NAME"))
    file_name = safe_text(title_data.get("FILE_NAME"))
    call_datetime = safe_text(title_data.get("CALL_DATETIME"))
    display_datetime = format_display_datetime(call_datetime)
    file_duration = safe_text(title_data.get("FILE_DURATION"))
    inmate_name = safe_text(title_data.get("INMATE_NAME"))
    outside_number = safe_text(title_data.get("OUTSIDE_NUMBER_FMT"))
    firm_name = safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME"))

    # Cover title: "Call of April 14, 2026" with the time as a separate fact.
    parsed_dt = parse_call_datetime(call_datetime)
    if parsed_dt is not None:
        cover_title = f"Call of {parsed_dt.strftime('%B')} {parsed_dt.day}, {parsed_dt.year}"
        has_time = len(call_datetime.strip()) > 10
        cover_time = parsed_dt.strftime("%I:%M %p").lstrip("0") if has_time else ""
    else:
        cover_title = display_datetime or call_datetime or "Recorded Call"
        cover_time = ""

    cover_facts = [
        {"label": "Case", "value": case_name},
        {"label": "Defendant", "value": inmate_name},
        {"label": "Outside Number", "value": outside_number, "mono": True},
        {"label": "Recorded", "value": cover_time},
        {"label": "Source File", "value": file_name, "mono": True},
        {"label": "Prepared For", "value": firm_name},
    ]

    ctx: dict = {
        "case_name": case_name,
        "file_name": file_name,
        "display_datetime": display_datetime or call_datetime,
        "file_duration": file_duration,
        "inmate_name": inmate_name,
        "outside_number": outside_number,
        "cover_title": cover_title,
        "cover_facts": [f for f in cover_facts if f["value"]],
        "firm_name": firm_name,
        "has_summary": bool(summary),
        "overflow_review_cue_pages": [],
    }

    if not summary:
        return ctx

    # The sheet title already carries the date; the meta line adds the
    # source file, time of day, and duration.
    ctx["summary_meta_file"] = shorten_middle(file_name, 34)
    ctx["summary_meta_details"] = " · ".join(p for p in (cover_time, file_duration) if p)

    sections = parse_summary_sections(summary)
    ctx["is_structured"] = sections.get("structured", False)

    if sections.get("structured"):
        rel = sections.get("relevance", "")
        ctx["relevance"] = rel
        ctx["relevance_desc"] = RELEVANCE_DESC.get(rel, "")
        ctx["review_cues"] = hydrate_review_cues(sections.get("review_cue_items", []), line_entries)
        ctx["cue_count"] = len(ctx["review_cues"])

        spk = sections.get("speakers", "")
        ctx["speakers"] = spk.replace("\n", " ").strip() if spk else ""
        cs = sections.get("call_summary", "")
        ctx["call_summary"] = cs.replace("\n", " ").strip() if cs else ""

        pagination = paginate_structured_summary(
            ctx["review_cues"], speakers=ctx["speakers"], call_summary=ctx["call_summary"],
        )
        ctx["page1_review_cues"] = pagination["page1_review_cues"]
        ctx["overflow_review_cue_pages"] = pagination["overflow_review_cue_pages"]
        ctx["context_layout"] = pagination["context_layout"]
    else:
        rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", summary, re.IGNORECASE)
        if rel_match:
            rel = rel_match.group(1).upper()
            ctx["relevance"] = rel
            ctx["relevance_desc"] = RELEVANCE_DESC.get(rel, "")
            body = summary[rel_match.end():].strip()
        else:
            body = summary.strip()
        ctx["raw_body"] = body
        ctx["raw_blocks"] = _raw_summary_blocks(body)

    return ctx


# ────────────────────────── Transcript sheets ──────────────────────────

def _build_transcript_sheets(line_entries: List[dict], lines_per_page: int) -> List[dict]:
    """Convert precomputed line entries into per-sheet row geometry.

    25 rows at exactly 25 pt pitch, vertically centered between the
    half-margin content bounds, with the page number centered below.
    """
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    content_top = PAGE_HEIGHT - PDF_MARGIN_TOP / 2   # from bottom of sheet
    content_bottom = PDF_MARGIN_BOTTOM / 2
    available_height = content_top - content_bottom
    line_block_height = ((max(lines_per_page, 1) - 1) * PDF_LINE_HEIGHT) + PDF_TEXT_SIZE
    vertical_padding = max((available_height - line_block_height) / 2.0, 0)
    top_baseline = content_top - vertical_padding - PDF_TEXT_SIZE

    row_baseline_offset = _courier_baseline_in_box(PDF_LINE_HEIGHT, PDF_TEXT_SIZE)

    sheets: List[dict] = []
    for page_number in sorted(pages):
        rows: List[dict] = []
        sorted_entries = sorted(
            pages[page_number],
            key=lambda e: (int(e.get("line", 0) or 0), e.get("id", "")),
        )
        for entry in sorted_entries:
            try:
                line_number = int(entry.get("line", 0) or 0)
            except (TypeError, ValueError):
                continue
            if line_number <= 0 or line_number > lines_per_page:
                continue
            baseline = top_baseline - (line_number - 1) * PDF_LINE_HEIGHT
            if baseline < content_bottom:
                continue

            # Convert the baseline (pt from sheet bottom) into a CSS top
            # offset for a 25pt line box whose baseline must land there.
            row_top = (PAGE_HEIGHT - baseline) - row_baseline_offset
            rows.append({
                "line": line_number,
                "top": f"{row_top:.3f}",
                "text": html.escape(str(entry.get("rendered_text", "")), quote=False),
            })
        sheets.append({"number": page_number, "rows": rows})
    return sheets


# ────────────────────────── Entry point ──────────────────────────

def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = LINES_PER_PAGE,
) -> bytes:
    """Render the per-call transcript PDF.

    All sheets are rendered in a single headless-Chromium pass. The rendered
    page count is asserted against the emitted sheet count; a mismatch raises
    ``RuntimeError`` because every page:line citation in the product depends
    on the transcript pagination being exactly what Python computed.
    """
    from pypdf import PdfReader

    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)

    ctx = _build_cover_context(title_data, summary, line_entries=line_entries)
    ctx["transcript_sheets"] = _build_transcript_sheets(line_entries, lines_per_page)
    ctx["fonts_css"] = pdf_font_css(("Fraunces", "Public Sans", "IBM Plex Mono", "Courier Prime"))

    cover_stats = []
    if ctx["file_duration"]:
        cover_stats.append({"n": ctx["file_duration"], "lbl": "Duration"})
    sheet_count = len(ctx["transcript_sheets"])
    cover_stats.append({
        "n": str(sheet_count),
        "lbl": "Transcript Page" if sheet_count == 1 else "Transcript Pages",
    })
    if ctx["has_summary"] and ctx.get("is_structured"):
        cover_stats.append({"n": str(ctx.get("cue_count", 0)), "lbl": "Review Cues"})
    ctx["cover_stats"] = cover_stats

    # Transcript text geometry (pt): Python stays the layout source of truth.
    ctx["t_line_num_width"] = f"{PDF_LINE_NUM_RIGHT:.3f}"
    ctx["t_text_indent"] = f"{PDF_TEXT_X - PDF_LINE_NUM_RIGHT:.3f}"
    pn_baseline = PDF_MARGIN_BOTTOM / 2 - 0.1 * inch  # from sheet bottom
    pn_box = _courier_baseline_in_box(PDF_LINE_HEIGHT, PDF_PAGE_NUMBER_SIZE)
    ctx["t_pageno_top"] = f"{(PAGE_HEIGHT - pn_baseline) - pn_box:.3f}"

    summary_sheets = (1 + len(ctx.get("overflow_review_cue_pages") or [])) if ctx["has_summary"] else 0
    expected_sheets = 1 + summary_sheets + len(ctx["transcript_sheets"])

    pdf_bytes = render_pdf(render_template("transcript_pdf.html", **ctx))

    actual_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    if actual_pages != expected_sheets:
        raise RuntimeError(
            f"Transcript PDF page-count mismatch: emitted {expected_sheets} "
            f"sheet divs but Chromium rendered {actual_pages} pages "
            f"(file: {safe_text(title_data.get('FILE_NAME'))!r}). "
            "Line citations would be unreliable; refusing to deliver."
        )
    return pdf_bytes
