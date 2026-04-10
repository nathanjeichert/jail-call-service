"""
PDF generation for jail call transcripts.

Produces a 3-part PDF:
  Page 1: Title page (case info, file metadata)
  Page 2: AI summary
  Pages 3+: Legal-formatted transcript (25 lines/page, Courier, line numbers)

Ported and trimmed from main/backend/transcript_formatting.py.
"""

import io
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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

TIMESTAMP_RE = re.compile(r"(\[(?:\d{1,2}:)?\d{2}:\d{2}\])")

# Maximum review cues that fit on the summary page 1 alongside the relevance
# pill, Identity of Outside Party, and Brief Summary blocks. For HIGH-relevance
# calls with more cues than this, the excess spills onto a continuation page
# so that attorney-relevant notes are never clipped.
SUMMARY_PAGE1_CUE_CAP = 7
# Safety ceiling on the continuation page so an unusually long note list can
# still never overflow. In practice Gemini is bounded by a 500-word response
# cap which keeps total cues under ~18, well below this limit.
SUMMARY_PAGE2_CUE_CAP = 14


def _safe_text(value: Optional[str]) -> str:
    return str(value or "").strip()


def _shorten_middle(value: Optional[str], max_chars: int = 44) -> str:
    text = _safe_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 8:
        return text[:max_chars]

    keep = max_chars - 3
    head = (keep + 1) // 2
    tail = keep // 2
    return f"{text[:head]}...{text[-tail:]}"


def _format_display_datetime(value: Optional[str]) -> str:
    """Make XML-style call datetimes readable without inventing timezone data."""
    text = _safe_text(value)
    if not text:
        return ""
    for fmt, size in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            dt = datetime.strptime(text[:size], fmt)
            if fmt == "%Y-%m-%d":
                return f"{dt.strftime('%b.')} {dt.day}, {dt.year}"
            time_text = dt.strftime("%I:%M %p").lstrip("0")
            return f"{dt.strftime('%b.')} {dt.day}, {dt.year} at {time_text}"
        except ValueError:
            continue
    return text


def _timestamp_sort_key(timestamp: str) -> float:
    return timestamp_to_seconds(timestamp)


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


def _parse_review_cue_line(line: str) -> Optional[dict]:
    """Parse a Gemini cue bullet into timestamp/speaker/quote/note fields."""
    clean = line.strip()
    if not clean:
        return None
    clean = re.sub(r'^[-\u2022*]\s*', '', clean)
    clean = re.sub(r'^\d+[.)]\s*', '', clean)
    if re.fullmatch(r'(?i)(none|n/?a|no\s+notes?|no\s+relevant\s+(?:information|moments?)(?:\s+found)?\.?)', clean):
        return None

    ts_match = TIMESTAMP_RE.search(clean)
    if not ts_match:
        return None

    timestamp = ts_match.group(1)
    rest = clean[ts_match.end():].strip(" :-\u2013\u2014")

    speaker = ""
    speaker_match = re.match(r'\[?([A-Z][A-Z0-9 /&.\'-]{1,34})\]?\s*:\s+', rest)
    if speaker_match:
        speaker = speaker_match.group(1).strip()
        rest = rest[speaker_match.end():].strip()

    quote = ""
    quote_match = re.search(r'["\u201c]([^"\u201d]{1,220})["\u201d]', rest)
    if quote_match:
        quote = quote_match.group(1).strip()
        before = rest[:quote_match.start()].strip()
        after = rest[quote_match.end():].strip()
        rest = " ".join(part for part in (before, after) if part)

    rest = re.sub(r'^\s*[-\u2013\u2014:]+\s*', '', rest).strip()
    if quote and rest:
        parts = re.split(r'\s+[-\u2013\u2014]\s+', rest, maxsplit=1)
        note = parts[-1].strip()
    else:
        note = rest

    return {
        "timestamp": timestamp,
        "speaker": speaker,
        "quote": quote,
        "note": note,
    }


def _parse_review_cues(body: str) -> List[dict]:
    cues: List[dict] = []
    for line in body.splitlines():
        cue = _parse_review_cue_line(line)
        if cue:
            cues.append(cue)
    cues.sort(key=lambda cue: _timestamp_sort_key(cue.get("timestamp", "")))
    return cues


def _line_cite_for_timestamp(timestamp: str, line_entries: Optional[List[dict]]) -> str:
    """Return a transcript page:line cite for the line nearest a cue timestamp."""
    if not timestamp or not line_entries:
        return ""

    target = timestamp_to_seconds(timestamp)
    best: Optional[dict] = None
    best_distance = float("inf")

    for entry in line_entries:
        try:
            start = float(entry.get("start", 0) or 0)
            end = float(entry.get("end", start) or start)
        except (TypeError, ValueError):
            continue

        if start <= target <= max(end, start):
            best = entry
            break

        distance = min(abs(target - start), abs(target - end))
        if distance < best_distance:
            best = entry
            best_distance = distance

    if not best:
        return ""

    page = best.get("page")
    line = best.get("line")
    if not page or not line:
        return ""
    return f"{int(page)}:{int(line)}"


def _parse_summary_sections(summary: str) -> dict:
    """Parse structured Gemini output into renderable sections.

    New summaries use NOTES as the primary section. The parser also accepts older
    REVIEW CUES / KEY FINDINGS output so previously generated summaries still render.
    """
    sections = {"raw": summary}
    text = summary.strip()

    rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    if rel_match:
        sections["relevance"] = rel_match.group(1).upper()

    header_patterns = [
        ("review_cues", r"NOTES:?"),
        ("review_cues", r"REVIEW\s+CUES:?"),
        ("review_cues", r"NOTABLE\s+MOMENTS:?"),
        ("review_cues", r"KEY\s+MOMENTS:?"),
        ("review_cues", r"PULL\s+QUOTES:?"),
        ("key_findings", r"KEY\s+FINDINGS:?"),
        ("speakers", r"IDENTITY\s+OF\s+OUTSIDE\s+PARTY:?"),
        ("speakers", r"SPEAKERS?\s*(?:&|AND)\s*RELATIONSHIP:?"),
        ("speakers", r"SPEAKER\s+NOTES:?"),
        ("call_summary", r"BRIEF\s+SUMMARY:?"),
        ("call_summary", r"CALL\s+SUMMARY:?"),
        ("call_summary", r"SUMMARY:?"),
    ]

    positions = []
    for key, pattern in header_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if any(not (match.end() <= start or match.start() >= end) for start, end, _ in positions):
                continue
            positions.append((match.start(), match.end(), key))
            break
    positions.sort()

    seen_keys = set()
    for i, (start, end, key) in enumerate(positions):
        if key in seen_keys and key not in {"review_cues"}:
            continue
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[end:next_start].strip()
        if key == "review_cues" and sections.get("review_cues"):
            sections["review_cues"] += "\n" + body
        else:
            sections[key] = body
        seen_keys.add(key)

    cue_body = sections.get("review_cues") or sections.get("key_findings") or ""
    if cue_body:
        sections["review_cue_items"] = _parse_review_cues(cue_body)

    sections["structured"] = bool(
        sections.get("relevance")
        and (
            sections.get("review_cue_items")
            or sections.get("call_summary")
            or sections.get("speakers")
        )
    )

    return sections


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


def _distribute_words_to_lines(
    words: Optional[List[WordTimestamp]],
    all_lines: List[str],
) -> List[List[dict]]:
    """Map word timestamps to wrapped lines by matching word text sequentially."""
    result: List[List[dict]] = [[] for _ in all_lines]
    if not words or not all_lines:
        return result

    word_idx = 0
    for line_idx, line_text in enumerate(all_lines):
        # Walk through words, assigning to this line while they match
        line_remaining = line_text.strip()
        while word_idx < len(words) and line_remaining:
            w = words[word_idx]
            wtext = w.text.strip()
            if not wtext:
                word_idx += 1
                continue
            # Check if this word appears at the start of remaining text
            if line_remaining.lower().startswith(wtext.lower()):
                result[line_idx].append({
                    "t": wtext,
                    "s": round(w.start / 1000.0, 3),
                    "e": round(w.end / 1000.0, 3),
                })
                line_remaining = line_remaining[len(wtext):].lstrip()
                word_idx += 1
            else:
                # Skip non-matching characters (punctuation differences, etc.)
                # Try advancing past one character in line_remaining
                stripped = line_remaining.lstrip(" ,;:!?.'\"—-–()")
                if stripped != line_remaining:
                    line_remaining = stripped
                else:
                    break
    return result


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

        words_per_line = _distribute_words_to_lines(turn.words, all_lines)

        for line_idx, line_text in enumerate(all_lines):
            is_cont_line = is_continuation or line_idx > 0
            if line_idx == 0 and not is_continuation:
                rendered = speaker_prefix + line_text
            else:
                rendered = " " * CONTINUATION_SPACES + line_text

            line_words = words_per_line[line_idx]
            if line_words:
                line_start = line_words[0]["s"]
                line_end = line_words[-1]["e"]
            else:
                line_start = start_sec
                line_end = start_sec

            pgln = page * 100 + line_in_page
            entry = {
                "id": f"{turn_idx}-{line_idx}",
                "turn_index": turn_idx,
                "line_index": line_idx,
                "speaker": speaker_name,
                "text": line_text,
                "rendered_text": rendered,
                "start": line_start,
                "end": line_end,
                "page": page,
                "line": line_in_page,
                "pgln": pgln,
                "is_continuation": is_cont_line,
            }
            if line_words:
                entry["words"] = line_words
            line_entries.append(entry)

            line_in_page += 1
            if line_in_page > lines_per_page:
                page += 1
                line_in_page = 1

    return line_entries


def _render_cover_pages(
    title_data: dict,
    summary: Optional[str],
    line_entries: Optional[List[dict]] = None,
) -> bytes:
    """Render title page (+ optional AI analysis page) as PDF via WeasyPrint.

    Uses an HTML/CSS template for polished, design-quality output, then
    returns raw PDF bytes ready to be merged with the transcript pages.
    """
    from jinja2 import Template
    from weasyprint import HTML

    template_path = Path(__file__).parent / "pdf_cover_template.html"
    template = Template(template_path.read_text())

    case_name = _safe_text(title_data.get("CASE_NAME"))
    file_name = _safe_text(title_data.get("FILE_NAME"))
    call_datetime = _safe_text(title_data.get("CALL_DATETIME"))
    display_datetime = _format_display_datetime(call_datetime)
    file_duration = _safe_text(title_data.get("FILE_DURATION"))
    inmate_name = _safe_text(title_data.get("INMATE_NAME"))
    outside_number = _safe_text(title_data.get("OUTSIDE_NUMBER_FMT"))

    title_meta = [
        ("Defendant", inmate_name),
        ("Outside Number", outside_number),
        ("Case", case_name),
    ]
    title_meta = [{"label": label, "value": value} for label, value in title_meta if value]

    ctx: dict = {
        "case_name": case_name,
        "file_name": file_name,
        "display_datetime": display_datetime or call_datetime,
        "file_duration": file_duration,
        "inmate_name": inmate_name,
        "outside_number": outside_number,
        "title_meta": title_meta,
        "firm_name": _safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME")),
        "has_summary": bool(summary),
    }

    # ── Summary page context ──
    if summary:
        ctx["summary_meta_file"] = _shorten_middle(file_name)
        meta_details = [display_datetime or call_datetime, file_duration]
        ctx["summary_meta_details"] = " \u00b7 ".join(p for p in meta_details if p)

        sections = _parse_summary_sections(summary)
        ctx["is_structured"] = sections.get("structured", False)

        if sections.get("structured"):
            rel = sections.get("relevance", "")
            ctx["relevance"] = rel
            ctx["relevance_desc"] = D.RELEVANCE_DESC.get(rel, "")
            ctx["review_cues"] = sections.get("review_cue_items", [])
            for cue in ctx["review_cues"]:
                cue["line_cite"] = _line_cite_for_timestamp(cue.get("timestamp", ""), line_entries)
            ctx["cue_count"] = len(ctx["review_cues"])

            # Split review cues across two summary pages for HIGH-relevance
            # calls when they exceed what fits on page 1 alongside Identity +
            # Brief Summary. MEDIUM/LOW keep the existing single-page layout.
            all_cues = ctx["review_cues"]
            if rel == "HIGH" and len(all_cues) > SUMMARY_PAGE1_CUE_CAP:
                ctx["page1_review_cues"] = all_cues[:SUMMARY_PAGE1_CUE_CAP]
                ctx["page2_review_cues"] = all_cues[SUMMARY_PAGE1_CUE_CAP:SUMMARY_PAGE1_CUE_CAP + SUMMARY_PAGE2_CUE_CAP]
                ctx["has_continuation"] = True
                ctx["page2_cue_count"] = len(ctx["page2_review_cues"])
                ctx["page2_first_index"] = SUMMARY_PAGE1_CUE_CAP + 1
                ctx["page2_last_index"] = SUMMARY_PAGE1_CUE_CAP + len(ctx["page2_review_cues"])
            else:
                ctx["page1_review_cues"] = all_cues
                ctx["page2_review_cues"] = []
                ctx["has_continuation"] = False

            spk = sections.get("speakers", "")
            ctx["speakers"] = spk.replace("\n", " ").strip() if spk else ""

            cs = sections.get("call_summary", "")
            ctx["call_summary"] = cs.replace("\n", " ").strip() if cs else ""
        else:
            rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", summary, re.IGNORECASE)
            if rel_match:
                rel = rel_match.group(1).upper()
                ctx["relevance"] = rel
                ctx["relevance_desc"] = D.RELEVANCE_DESC.get(rel, "")
                body = summary[rel_match.end():].strip()
            else:
                body = summary.strip()

            ctx["raw_body"] = body
            raw_blocks: list = []
            for para in re.split(r'\n{2,}', body):
                para = para.strip()
                if not para:
                    continue
                bullets: list = []
                texts: list = []
                for line in (l.strip() for l in para.split("\n") if l.strip()):
                    if re.match(r'^[-\u2022*]\s', line):
                        if texts:
                            raw_blocks.append({"type": "text", "text": " ".join(texts)})
                            texts = []
                        bullets.append(re.sub(r'^[-\u2022*]\s*', '', line))
                    else:
                        if bullets:
                            raw_blocks.append({"type": "bullet", "items": bullets})
                            bullets = []
                        texts.append(line)
                if bullets:
                    raw_blocks.append({"type": "bullet", "items": bullets})
                if texts:
                    raw_blocks.append({"type": "text", "text": " ".join(texts)})
            ctx["raw_blocks"] = raw_blocks

    html_str = template.render(**ctx)
    return HTML(string=html_str, base_url=str(template_path.parent)).write_pdf()


def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = 25,
) -> bytes:
    """
    Create a PDF with:
      Page 1: Title page        (WeasyPrint — HTML/CSS)
      Page 2: AI summary        (WeasyPrint — HTML/CSS, if provided)
      Pages 3+: Transcript      (ReportLab  — precise monospace layout)
    """
    from pypdf import PdfReader, PdfWriter

    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)

    # ── Cover pages via WeasyPrint ──
    cover_pdf = _render_cover_pages(title_data, summary, line_entries=line_entries)

    # ── Transcript pages via ReportLab ──
    transcript_buf = io.BytesIO()
    c = canvas.Canvas(transcript_buf, pagesize=letter, pageCompression=1)
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    for page_number in sorted(pages):
        _draw_transcript_page(c, pages[page_number], lines_per_page, page_number)
        c.showPage()

    c.save()
    transcript_buf.seek(0)

    # ── Merge with pypdf ──
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(cover_pdf)).pages:
        writer.add_page(page)
    for page in PdfReader(transcript_buf).pages:
        writer.add_page(page)

    final = io.BytesIO()
    writer.write(final)
    final.seek(0)
    return final.read()
