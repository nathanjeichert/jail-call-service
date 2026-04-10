"""
PDF generation for jail call transcripts.

Produces a 3-part PDF:
  Page 1: Title page (case info, file metadata)
  Page 2: AI summary
  Pages 3+: Legal-formatted transcript (25 lines/page, Courier, line numbers)

Ported and trimmed from main/backend/transcript_formatting.py.
"""

import io
import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(TTFont("CourierNew", "/System/Library/Fonts/Supplemental/Courier New.ttf"))
pdfmetrics.registerFont(TTFont("CourierNew-Bold", "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"))

from . import design as D
from .models import TranscriptTurn, WordTimestamp

logger = logging.getLogger(__name__)

# Text layout constants
SPEAKER_PREFIX_SPACES = 10
CONTINUATION_SPACES = 5
SPEAKER_COLON = ":   "

# Transcript page geometry (legal deposition style)
PDF_PAGE_WIDTH, PDF_PAGE_HEIGHT = letter
PDF_MARGIN_TOP = 0.75 * inch
PDF_MARGIN_BOTTOM = 0.75 * inch
PDF_LINE_HEIGHT = 25.0
PDF_TEXT_FONT = "CourierNew"
PDF_TEXT_SIZE = 12
PDF_LINE_NUMBER_SIZE = 10
PDF_PAGE_NUMBER_SIZE = 10

# Vertical rule positions
PDF_LINE_NUM_RIGHT = 0.78 * inch      # right edge of line numbers
PDF_RULE_LEFT_OUTER = 0.92 * inch     # left double-line (outer)
PDF_RULE_LEFT_INNER = 0.97 * inch     # left double-line (inner)
PDF_RULE_RIGHT = 7.4 * inch           # right single line

# Derive transcript text block from the ruled corridor, centered between the
# inner left rule and the right rule.
_COURIER_CHAR_W = stringWidth("M", PDF_TEXT_FONT, PDF_TEXT_SIZE)
_TRANSCRIPT_RULE_WIDTH = PDF_RULE_RIGHT - PDF_RULE_LEFT_INNER
MAX_LINE_CHARS = int((_TRANSCRIPT_RULE_WIDTH - 12) / _COURIER_CHAR_W)
PDF_TEXT_BLOCK_WIDTH = MAX_LINE_CHARS * _COURIER_CHAR_W
PDF_TEXT_X = PDF_RULE_LEFT_INNER + ((_TRANSCRIPT_RULE_WIDTH - PDF_TEXT_BLOCK_WIDTH) / 2.0)

# Summary page layout (professional sans-serif)
SUMMARY_FONT = "Helvetica"
SUMMARY_FONT_BOLD = "Helvetica-Bold"
SUMMARY_FONT_OBLIQUE = "Helvetica-Oblique"
SUMMARY_TITLE_SIZE = 15
SUMMARY_HEADING_SIZE = 10.5
SUMMARY_BODY_SIZE = 9.5
SUMMARY_META_SIZE = 8.5
SUMMARY_LINE_HEIGHT = 13
SUMMARY_SECTION_GAP = 10

TIMESTAMP_RE = re.compile(r"(\[(?:\d{1,2}:)?\d{2}:\d{2}\])")


def _safe_text(value: Optional[str]) -> str:
    return str(value or "").strip()


def timestamp_to_seconds(timestamp: Optional[str]) -> float:
    if not timestamp:
        return 0.0
    ts = timestamp.strip('[]').strip()
    parts = ts.split(':')
    try:
        if len(parts) == 3:
            h, m, s = map(float, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(float, parts)
            return m * 60 + s
        return float(ts)
    except ValueError:
        return 0.0


def wrap_text(text: str, max_width: int) -> List[str]:
    if not text:
        return [""]
    if max_width <= 0:
        return [text]
    words = text.split()
    lines, current, length = [], [], 0
    for word in words:
        space = len(word) + (1 if current else 0)
        if length + space <= max_width:
            current.append(word)
            length += space
        else:
            if current:
                lines.append(" ".join(current))
            current, length = [word], len(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _draw_transcript_rules(c: canvas.Canvas) -> None:
    """Draw vertical rules: double line on left (gutter), single on right."""
    rule_top = PDF_PAGE_HEIGHT
    rule_bottom = 0
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    # Double line on left
    c.line(PDF_RULE_LEFT_OUTER, rule_bottom, PDF_RULE_LEFT_OUTER, rule_top)
    c.line(PDF_RULE_LEFT_INNER, rule_bottom, PDF_RULE_LEFT_INNER, rule_top)
    # Single line on right
    c.line(PDF_RULE_RIGHT, rule_bottom, PDF_RULE_RIGHT, rule_top)


def _draw_title_page(c: canvas.Canvas, title_data: dict) -> None:
    # Estate design: cream paper + forest stripe + gold pinstripe
    D.draw_estate_page_bg(c)

    # Forest gradient band — top 38% of page
    band_h = D.PAGE_H * 0.38
    band_y = D.PAGE_H - band_h
    bar = D.gradient_image(D.PAGE_W - D.STRIPE_W, band_h,
                           D.PRIMARY_RGB, D.PRIMARY_LIGHT_RGB, noise=1)
    c.drawImage(bar, D.STRIPE_W, band_y, D.PAGE_W - D.STRIPE_W, band_h)

    cx = D.PAGE_W / 2 + D.STRIPE_W / 2
    y = D.PAGE_H - 0.95 * inch

    # Gold label
    c.setFillColor(D.ACCENT)
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, y, "GENERATED  TRANSCRIPT")
    y -= 0.55 * inch

    # Case name — large, white on dark
    case_name = _safe_text(title_data.get("CASE_NAME"))
    if case_name:
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 24)
        c.drawCentredString(cx, y, case_name)
        y -= 0.45 * inch

    # Gold rule
    rule_w = 1.4 * inch
    c.setStrokeColor(D.ACCENT)
    c.setLineWidth(0.8)
    c.line(cx - rule_w / 2, y, cx + rule_w / 2, y)

    # Metadata below band — on cream
    y = band_y - 0.55 * inch

    left_col = D.TEXT_LEFT + 0.2 * inch
    right_col = D.PAGE_W / 2 + 0.15 * inch

    left_items = [
        ("Inmate", title_data.get("INMATE_NAME")),
        ("Date/Time", title_data.get("CALL_DATETIME")),
        ("Housing Unit", title_data.get("FACILITY")),
        ("Duration", title_data.get("FILE_DURATION")),
    ]
    right_items = [
        ("Outside Party", title_data.get("OUTSIDE_NUMBER_FMT")),
        ("Call Outcome", title_data.get("CALL_OUTCOME")),
        ("Original File", title_data.get("FILE_NAME")),
        ("Notes", title_data.get("NOTES")),
    ]

    row_h = 0.42 * inch
    for col_x, items in [(left_col, left_items), (right_col, right_items)]:
        row_y = y
        for label, value in items:
            v = _safe_text(value)
            if not v:
                continue
            c.setFillColor(D.MUTED)
            c.setFont("Helvetica", 8)
            c.drawString(col_x, row_y, label.upper())
            c.setFillColor(D.DARK)
            c.setFont("Helvetica", 10.5)
            c.drawString(col_x, row_y - 14, v)
            row_y -= row_h

    # Firm name at bottom if present
    firm = _safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME"))
    if firm:
        c.setFillColor(D.MUTED)
        c.setFont("Helvetica-Oblique", 9)
        c.drawCentredString(cx, 0.65 * inch, firm)


def _parse_summary_sections(summary: str) -> dict:
    """Parse structured Gemini output into sections. Falls back to raw if unrecognized."""
    sections = {"raw": summary}
    text = summary.strip()

    # Try to extract RELEVANCE
    rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    if rel_match:
        sections["relevance"] = rel_match.group(1).upper()

    # Section header patterns
    headers = [
        ("key_findings", r"KEY\s+FINDINGS:?"),
        ("speakers", r"SPEAKERS?\s*(?:&|AND)\s*RELATIONSHIP:?"),
        ("call_summary", r"CALL\s+SUMMARY:?"),
    ]

    # Find positions of each section header
    positions = []
    for key, pattern in headers:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            positions.append((m.start(), m.end(), key))
    positions.sort()

    # Extract body text between headers
    for i, (start, end, key) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[end:next_start].strip()
        sections[key] = body

    # Only consider it "structured" if we found at least relevance + one section
    if "relevance" in sections and len(sections) > 2:
        sections["structured"] = True
    else:
        sections["structured"] = False

    return sections


def _wrap_text_for_width(text: str, font: str, size: float, max_width: float) -> List[str]:
    """Word-wrap text to fit within max_width pixels using the given font metrics."""
    if not text:
        return [""]
    words = text.split()
    lines, current = [], []
    current_width = 0.0
    space_width = _approx_char_width(font, size)

    for word in words:

        word_w = stringWidth(word, font, size)
        gap = space_width if current else 0
        if current_width + gap + word_w <= max_width:
            current.append(word)
            current_width += gap + word_w
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
            current_width = word_w
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _approx_char_width(font: str, size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    return stringWidth(" ", font, size)


def _draw_text_with_timestamps(
    c: canvas.Canvas, x: float, y: float,
    text: str, font: str, size: float,
    title_data: dict, max_width: float,
) -> float:
    """Draw text with blue timestamp hyperlinks. Returns new y position."""
    audio_fn = _safe_text(title_data.get("AUDIO_FILENAME")) or _safe_text(title_data.get("FILE_NAME"))
    lines = _wrap_text_for_width(text, font, size, max_width)
    y_floor = PDF_MARGIN_BOTTOM + 0.4 * inch

    for line in lines:
        if y < y_floor:
            break
        current_x = x
        last_idx = 0
        c.setFont(font, size)

        for match in TIMESTAMP_RE.finditer(line):
            ts_str = match.group(1)
            prefix = line[last_idx:match.start()]
            if prefix:
                c.setFillColor(colors.black)
                c.drawString(current_x, y, prefix)
                current_x += c.stringWidth(prefix, font, size)

            # Blue timestamp link
            c.setFillColor(colors.Color(0.02, 0.39, 0.76))
            c.drawString(current_x, y, ts_str)
            x1 = current_x
            current_x += c.stringWidth(ts_str, font, size)
            if audio_fn:
                url = f"../viewer/index.html?call={audio_fn}&t={ts_str[1:-1]}"
                c.linkURL(url, (x1, y - 2, current_x, y + size), relative=1)
            last_idx = match.end()

        remainder = line[last_idx:]
        if remainder:
            c.setFillColor(colors.black)
            c.drawString(current_x, y, remainder)

        y -= SUMMARY_LINE_HEIGHT

    return y


def _draw_summary_page(c: canvas.Canvas, summary: str, title_data: dict) -> None:
    """Draw Estate-styled AI analysis page."""
    # Estate background + header bar
    D.draw_estate_page_bg(c)
    y = D.draw_header_bar(c, "AI Analysis", _safe_text(title_data.get("CASE_NAME")))

    left = D.TEXT_LEFT
    right = D.TEXT_RIGHT
    text_width = D.TEXT_WIDTH
    y_floor = PDF_MARGIN_BOTTOM + 0.4 * inch
    truncated = False

    def _draw_wrapped(text: str, font: str, size: float, indent: float = 0,
                      line_height: float = SUMMARY_LINE_HEIGHT, color=None,
                      emdash_bullet: bool = False):
        nonlocal y, truncated
        if color is None:
            color = D.BODY
        max_w = text_width - indent
        if emdash_bullet:
            max_w -= 16
        lines = _wrap_text_for_width(text, font, size, max_w)
        for i, line in enumerate(lines):
            if y < y_floor:
                truncated = True
                break
            c.setFont(font, size)
            x = left + indent
            if emdash_bullet and i == 0:
                c.setFillColor(D.ACCENT)
                c.drawString(x + 6, y, "\u2014")
                c.setFillColor(color)
                x += 22
            elif emdash_bullet:
                x += 22
            else:
                c.setFillColor(color)
            c.drawString(x, y, line)
            y -= line_height

    # ── Metadata line ──
    meta_parts = []
    for key in ("FILE_NAME", "FILE_DURATION", "CALL_DATETIME", "INMATE_NAME"):
        v = _safe_text(title_data.get(key))
        if v:
            meta_parts.append(v)
    if meta_parts:
        c.setFont(SUMMARY_FONT, SUMMARY_META_SIZE)
        c.setFillColor(D.MUTED)
        c.drawString(left, y, " \u00b7 ".join(meta_parts))
        y -= 22
    else:
        y -= 8

    sections = _parse_summary_sections(summary)

    if sections.get("structured"):
        # ── Relevance badge ──
        relevance = sections.get("relevance", "")
        if relevance in D.RELEVANCE_COLORS:
            badge_color = D.RELEVANCE_COLORS[relevance]
            badge_h = 26
            c.setFillColor(badge_color)
            c.roundRect(left, y - badge_h + 4, text_width, badge_h, 4, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont(SUMMARY_FONT_BOLD, 12)
            c.drawString(left + 14, y - badge_h + 11, f"RELEVANCE: {relevance}")
            desc = D.RELEVANCE_DESC.get(relevance, "")
            if desc:
                c.setFont(SUMMARY_FONT, 9)
                c.setFillColor(colors.Color(1.0, 0.78, 0.78) if relevance == "HIGH"
                               else colors.Color(1.0, 0.88, 0.72) if relevance == "MEDIUM"
                               else colors.Color(0.80, 1.0, 0.85))
                c.drawRightString(right - 14, y - badge_h + 12, desc)
            y -= badge_h + 16

        # ── Key Findings ──
        findings = sections.get("key_findings", "")
        if findings and y > y_floor:
            y = D.draw_section_heading(c, y, "Key Findings")
            for bl in findings.split("\n"):
                if y < y_floor:
                    break
                bl = re.sub(r'^[-\u2022*]\s*', '', bl.strip())
                if bl:
                    _draw_wrapped(bl, SUMMARY_FONT, SUMMARY_BODY_SIZE, indent=0, emdash_bullet=True)
                    y -= 3
            y -= SUMMARY_SECTION_GAP

        # ── Speakers & Relationship ──
        speakers = sections.get("speakers", "")
        if speakers and y > y_floor:
            y = D.draw_section_heading(c, y, "Speakers & Relationship")
            speaker_text = speakers.replace("\n", " ").strip()
            box_lines = _wrap_text_for_width(speaker_text, SUMMARY_FONT, SUMMARY_BODY_SIZE, text_width - 28)
            box_h = len(box_lines) * SUMMARY_LINE_HEIGHT + 16
            if y - box_h > y_floor:
                c.setFillColor(D.WARM_BOX)
                c.roundRect(left, y - box_h + 6, text_width, box_h, 4, fill=1, stroke=0)
                c.setStrokeColor(D.RULE)
                c.setLineWidth(0.3)
                c.roundRect(left, y - box_h + 6, text_width, box_h, 4, fill=0, stroke=1)
                for line in box_lines:
                    c.setFillColor(D.BODY)
                    c.setFont(SUMMARY_FONT, SUMMARY_BODY_SIZE)
                    c.drawString(left + 14, y - 4, line)
                    y -= SUMMARY_LINE_HEIGHT
                y -= 10 + SUMMARY_SECTION_GAP

        # ── Call Summary ──
        call_summary = sections.get("call_summary", "")
        if call_summary and y > y_floor:
            y = D.draw_section_heading(c, y, "Call Summary")
            _draw_wrapped(call_summary.replace("\n", " "), SUMMARY_FONT, SUMMARY_BODY_SIZE + 0.5,
                          indent=0, line_height=SUMMARY_LINE_HEIGHT + 1)

    else:
        # ── Fallback: raw summary ──
        rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", summary, re.IGNORECASE)
        if rel_match:
            relevance = rel_match.group(1).upper()
            if relevance in D.RELEVANCE_COLORS:
                badge_color = D.RELEVANCE_COLORS[relevance]
                badge_h = 26
                c.setFillColor(badge_color)
                c.roundRect(left, y - badge_h + 4, text_width, badge_h, 4, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont(SUMMARY_FONT_BOLD, 12)
                c.drawString(left + 14, y - badge_h + 11, f"RELEVANCE: {relevance}")
                y -= badge_h + 16
            body = summary[rel_match.end():].strip()
        else:
            body = summary.strip()

        if body:
            y = D.draw_section_heading(c, y, "Analysis")
            for para in re.split(r'\n{2,}', body):
                para = para.strip()
                if not para:
                    continue
                for pl in para.split("\n"):
                    pl = pl.strip()
                    if not pl:
                        continue
                    if re.match(r'^[-\u2022*]\s', pl):
                        pl = re.sub(r'^[-\u2022*]\s*', '', pl)
                        _draw_wrapped(pl, SUMMARY_FONT, SUMMARY_BODY_SIZE, indent=0, emdash_bullet=True)
                        y -= 2
                    else:
                        _draw_wrapped(pl, SUMMARY_FONT, SUMMARY_BODY_SIZE)
                y -= 6

    # Truncation notice
    if truncated:
        notice_y = PDF_MARGIN_BOTTOM + 0.15 * inch
        c.setFillColor(D.MUTED)
        c.setFont(SUMMARY_FONT_OBLIQUE, 8)
        c.drawCentredString(PDF_PAGE_WIDTH / 2, notice_y,
                            "Analysis truncated \u2014 see full summary in call-index.xlsx or the viewer")

    D.draw_page_number(c, 2)


def _draw_transcript_page(
    c: canvas.Canvas,
    page_entries: List[dict],
    lines_per_page: int,
    page_number: int,
) -> None:
    _draw_transcript_rules(c)

    content_top = PDF_PAGE_HEIGHT - PDF_MARGIN_TOP / 2
    content_bottom = PDF_MARGIN_BOTTOM / 2
    available_height = content_top - content_bottom
    line_block_height = ((max(lines_per_page, 1) - 1) * PDF_LINE_HEIGHT) + PDF_TEXT_SIZE
    vertical_padding = max((available_height - line_block_height) / 2.0, 0)
    top_baseline = content_top - vertical_padding - PDF_TEXT_SIZE

    # Page number at bottom center
    pn_y = content_bottom - 0.1 * inch
    c.setFillColor(colors.black)
    c.setFont(PDF_TEXT_FONT, PDF_PAGE_NUMBER_SIZE)
    c.drawCentredString(PDF_PAGE_WIDTH / 2, pn_y, str(page_number))

    sorted_entries = sorted(page_entries, key=lambda e: (int(e.get("line", 0) or 0), e.get("id", "")))
    for entry in sorted_entries:
        try:
            line_number = int(entry.get("line", 0) or 0)
        except (TypeError, ValueError):
            continue
        if line_number <= 0 or line_number > lines_per_page:
            continue
        y = top_baseline - (line_number - 1) * PDF_LINE_HEIGHT
        if y < content_bottom:
            continue

        c.setFillColor(colors.black)
        c.setFont(PDF_TEXT_FONT, PDF_LINE_NUMBER_SIZE)
        c.drawRightString(PDF_LINE_NUM_RIGHT, y, str(line_number))

        c.setFont(PDF_TEXT_FONT, PDF_TEXT_SIZE)
        c.drawString(PDF_TEXT_X, y, str(entry.get("rendered_text", "")))


def compute_line_entries(
    turns: List[TranscriptTurn],
    audio_duration: float,
    lines_per_page: int = 25,
) -> List[dict]:
    """Build page/line layout data from transcript turns."""
    line_entries: List[dict] = []
    page = 1
    line_in_page = 1

    for turn_idx, turn in enumerate(turns):
        start_sec = timestamp_to_seconds(turn.timestamp)
        if turn.words:
            word_starts = [w.start for w in turn.words if w.start is not None and w.start >= 0]
            word_ends = [w.end for w in turn.words if w.end is not None and w.end >= 0]
            if word_starts and word_ends:
                start_sec = min(word_starts) / 1000.0

        is_continuation = getattr(turn, 'is_continuation', False)
        speaker_name = turn.speaker.upper()
        text = turn.text.strip()

        speaker_prefix = " " * SPEAKER_PREFIX_SPACES + speaker_name + SPEAKER_COLON
        max_cont_width = MAX_LINE_CHARS - CONTINUATION_SPACES

        if is_continuation:
            max_first_line = max_cont_width
        else:
            max_first_line = MAX_LINE_CHARS - len(speaker_prefix)

        wrapped = wrap_text(text, max_first_line)
        if not wrapped:
            wrapped = [""]

        cont_text = " ".join(wrapped[1:])
        cont_lines = wrap_text(cont_text, max_cont_width) if cont_text else []
        all_lines = [wrapped[0]] + cont_lines

        for line_idx, line_text in enumerate(all_lines):
            is_cont_line = is_continuation or line_idx > 0
            if line_idx == 0 and not is_continuation:
                rendered = speaker_prefix + line_text
            else:
                rendered = " " * CONTINUATION_SPACES + line_text

            pgln = page * 100 + line_in_page
            line_entries.append({
                "id": f"{turn_idx}-{line_idx}",
                "turn_index": turn_idx,
                "line_index": line_idx,
                "speaker": speaker_name,
                "text": line_text,
                "rendered_text": rendered,
                "start": start_sec,
                "end": start_sec,
                "page": page,
                "line": line_in_page,
                "pgln": pgln,
                "is_continuation": is_cont_line,
            })

            line_in_page += 1
            if line_in_page > lines_per_page:
                page += 1
                line_in_page = 1

    return line_entries


def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = 25,
) -> bytes:
    """
    Create a PDF with:
      Page 1: Title page
      Page 2: AI summary (if provided)
      Pages 3+: Transcript
    """
    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=letter, pageCompression=1)

    # Page 1: Title
    _draw_title_page(c, title_data)
    c.showPage()

    # Page 2: Summary
    if summary:
        _draw_summary_page(c, summary, title_data)
        c.showPage()

    # Transcript pages
    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    for page_number in sorted(pages):
        _draw_transcript_page(c, pages[page_number], lines_per_page, page_number)
        c.showPage()

    c.save()
    output.seek(0)
    return output.read()
