"""
PDF user guide generation for the jail call deliverable package.

Produces a 7-page professional guide:
  Page 1: Cover (case name, date, call count)
  Page 2: What's in This Package (folder tree)
  Page 3: Using the Call Viewer (screenshot + instructions)
  Page 4: Using the Search Page (screenshot + instructions)
  Page 5: Using the Excel Index (screenshot + instructions)
  Page 6: Understanding AI Analysis (relevance badges, sections)
  Page 7: Important Notes (disclaimer, tips)
"""

import io
import logging
import os
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from .transcript_formatting import _draw_border, _wrap_text_for_width

logger = logging.getLogger(__name__)

# ── Layout constants ──
PAGE_W, PAGE_H = letter
MARGIN_LEFT = 1.0 * inch
MARGIN_RIGHT = 1.0 * inch
MARGIN_TOP = 0.75 * inch
MARGIN_BOTTOM = 0.75 * inch
BORDER_INSET = 0.33 * inch
BORDER_GAP = 4.0

TEXT_AREA_LEFT = MARGIN_LEFT
TEXT_AREA_RIGHT = PAGE_W - MARGIN_RIGHT
TEXT_AREA_WIDTH = TEXT_AREA_RIGHT - TEXT_AREA_LEFT

# ── Colors (matching transcript_formatting design system) ──
COLOR_DARK = colors.Color(0.12, 0.16, 0.21)
COLOR_MUTED = colors.Color(0.40, 0.45, 0.50)
COLOR_RULE = colors.Color(0.80, 0.83, 0.86)
COLOR_LINK = colors.Color(0.02, 0.39, 0.76)

RELEVANCE_HIGH = colors.Color(0.75, 0.10, 0.10)
RELEVANCE_MEDIUM = colors.Color(0.80, 0.50, 0.02)
RELEVANCE_LOW = colors.Color(0.15, 0.55, 0.15)

# ── Fonts ──
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_OBLIQUE = "Helvetica-Oblique"
FONT_MONO = "Courier"

# ── Screenshot config ──
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "guide_assets")
MAX_IMG_WIDTH = 468.0   # ~6.5" at 72dpi
MAX_IMG_HEIGHT = 280.0  # ~3.9"

SCREENSHOT_FILES = {
    "viewer": "viewer_screenshot.png",
    "search": "search_screenshot.png",
    "excel": "excel_screenshot.png",
}


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _page_number(c: canvas.Canvas, num: int) -> None:
    """Draw centered page number at bottom of page."""
    pn_y = BORDER_INSET + BORDER_GAP / 2 + 8
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT, 10)
    c.drawCentredString(PAGE_W / 2, pn_y, str(num))


def _draw_heading(c: canvas.Canvas, y: float, text: str, size: float = 15) -> float:
    """Draw a section heading with underline rule. Returns new y."""
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_BOLD, size)
    c.drawString(TEXT_AREA_LEFT, y, text)
    y -= 6
    c.setStrokeColor(COLOR_RULE)
    c.setLineWidth(0.6)
    c.line(TEXT_AREA_LEFT, y, TEXT_AREA_RIGHT, y)
    y -= 16
    return y


def _draw_body_text(c: canvas.Canvas, y: float, text: str,
                    font: str = FONT, size: float = 10,
                    line_height: float = 14,
                    color=None, indent: float = 0) -> float:
    """Word-wrap and draw body text. Returns new y."""
    if color is None:
        color = COLOR_DARK
    c.setFillColor(color)
    c.setFont(font, size)
    max_w = TEXT_AREA_WIDTH - indent
    lines = _wrap_text_for_width(text, font, size, max_w)
    for line in lines:
        if y < MARGIN_BOTTOM + 30:
            break
        c.drawString(TEXT_AREA_LEFT + indent, y, line)
        y -= line_height
    return y


def _draw_numbered_item(c: canvas.Canvas, y: float, number: int, text: str,
                        size: float = 10, line_height: float = 14) -> float:
    """Draw a numbered instruction item with hanging indent."""
    prefix = f"{number}."
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_BOLD, size)
    c.drawString(TEXT_AREA_LEFT, y, prefix)
    indent = 20
    c.setFont(FONT, size)
    max_w = TEXT_AREA_WIDTH - indent
    lines = _wrap_text_for_width(text, FONT, size, max_w)
    for line in lines:
        if y < MARGIN_BOTTOM + 30:
            break
        c.drawString(TEXT_AREA_LEFT + indent, y, line)
        y -= line_height
    y -= 2  # extra gap between items
    return y


def _draw_screenshot(c: canvas.Canvas, y: float, key: str, caption: str) -> float:
    """Draw a screenshot image or placeholder. Returns new y."""
    img_path = os.path.join(ASSETS_DIR, SCREENSHOT_FILES.get(key, ""))
    has_image = os.path.isfile(img_path)

    if has_image:
        try:
            from reportlab.lib.utils import ImageReader
            img = ImageReader(img_path)
            iw, ih = img.getSize()
            # Scale to fit
            scale = min(MAX_IMG_WIDTH / iw, MAX_IMG_HEIGHT / ih, 1.0)
            draw_w = iw * scale
            draw_h = ih * scale
        except Exception as e:
            logger.warning("Failed to load screenshot %s: %s", img_path, e)
            has_image = False

    if not has_image:
        draw_w = MAX_IMG_WIDTH
        draw_h = 160  # shorter placeholder

    # Center horizontally
    x = TEXT_AREA_LEFT + (TEXT_AREA_WIDTH - draw_w) / 2
    img_y = y - draw_h

    if has_image:
        c.drawImage(img, x, img_y, width=draw_w, height=draw_h,
                     preserveAspectRatio=True, anchor='c')
        # Thin border
        c.setStrokeColor(COLOR_RULE)
        c.setLineWidth(0.5)
        c.rect(x, img_y, draw_w, draw_h, stroke=1, fill=0)
    else:
        # Dashed placeholder rectangle
        c.setStrokeColor(COLOR_MUTED)
        c.setLineWidth(0.8)
        c.setDash(4, 4)
        c.rect(x, img_y, draw_w, draw_h, stroke=1, fill=0)
        c.setDash()
        # "Screenshot" label
        c.setFillColor(COLOR_MUTED)
        c.setFont(FONT_OBLIQUE, 11)
        c.drawCentredString(x + draw_w / 2, img_y + draw_h / 2 - 5,
                            f"[{caption} — screenshot not available]")

    y = img_y - 6
    # Caption
    c.setFillColor(COLOR_MUTED)
    c.setFont(FONT_OBLIQUE, 8.5)
    c.drawCentredString(PAGE_W / 2, y, caption)
    y -= 18
    return y


# ═══════════════════════════════════════════════════════════════════
# Page renderers
# ═══════════════════════════════════════════════════════════════════

def _page_cover(c: canvas.Canvas, case_name: str, call_count: int,
                gen_date: str) -> None:
    """Page 1: Cover page."""
    _draw_border(c)
    cx = PAGE_W / 2
    y = PAGE_H - 3.0 * inch

    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_BOLD, 28)
    c.drawCentredString(cx, y, "User Guide")
    y -= 0.6 * inch

    c.setFont(FONT, 14)
    c.drawCentredString(cx, y, case_name)
    y -= 0.4 * inch

    c.setFont(FONT, 11)
    c.setFillColor(COLOR_MUTED)
    c.drawCentredString(cx, y, f"Generated {gen_date}")
    y -= 0.3 * inch
    c.drawCentredString(cx, y, f"{call_count} call{'s' if call_count != 1 else ''} processed")

    # Bottom prompt
    y = MARGIN_BOTTOM + 1.2 * inch
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_OBLIQUE, 11)
    c.drawCentredString(cx, y, "Open viewer/index.html to get started")


def _page_package_contents(c: canvas.Canvas) -> None:
    """Page 2: What's in This Package."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "What\u2019s in This Package")

    # Folder tree in a light gray box
    tree_lines = [
        "\u251c\u2500\u2500 viewer/index.html",
        "\u251c\u2500\u2500 search.html",
        "\u251c\u2500\u2500 call-index.xlsx",
        "\u251c\u2500\u2500 transcripts/",
        "\u251c\u2500\u2500 transcripts-no-summary/",
        "\u251c\u2500\u2500 audio/",
        "\u2514\u2500\u2500 guide.pdf",
    ]

    box_x = TEXT_AREA_LEFT
    box_w = TEXT_AREA_WIDTH
    line_h = 16
    box_h = len(tree_lines) * line_h + 16
    box_y = y - box_h + 4

    c.setFillColor(colors.Color(0.95, 0.95, 0.96))
    c.roundRect(box_x, box_y, box_w, box_h, 4, fill=1, stroke=0)

    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_MONO, 9.5)
    ty = y - 8
    for line in tree_lines:
        c.drawString(box_x + 12, ty, line)
        ty -= line_h

    y = box_y - 20

    # Descriptions
    descriptions = [
        ("viewer/index.html",
         "Interactive call viewer \u2014 play audio with synced, clickable transcripts. "
         "This is the main way to review calls."),
        ("search.html",
         "Full-text search across every transcript. Find names, phrases, or topics "
         "instantly and jump to the matching call."),
        ("call-index.xlsx",
         "Excel spreadsheet listing every call with date, duration, phone number, "
         "AI relevance rating, summary, and full transcript text. Sort and filter freely."),
        ("transcripts/",
         "Individual PDF transcripts. Page 1 is the call header, page 2 is AI analysis, "
         "and remaining pages are the formatted transcript with line numbers."),
        ("transcripts-no-summary/",
         "Same transcripts without the AI analysis page \u2014 clean copies for filing or printing."),
        ("audio/",
         "Converted MP3 audio files. You can also play these directly in the viewer."),
        ("guide.pdf",
         "This document."),
    ]

    for name, desc in descriptions:
        if y < MARGIN_BOTTOM + 30:
            break
        c.setFillColor(COLOR_DARK)
        c.setFont(FONT_BOLD, 9.5)
        c.drawString(TEXT_AREA_LEFT, y, name)
        y -= 14
        y = _draw_body_text(c, y, desc, size=9.5, line_height=13, indent=10)
        y -= 6

    _page_number(c, 2)


def _page_viewer(c: canvas.Canvas) -> None:
    """Page 3: Using the Call Viewer."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "Using the Call Viewer")

    y = _draw_screenshot(c, y, "viewer", "The Call Viewer interface")

    instructions = [
        "Select a call from the sidebar on the left to load its audio and transcript.",
        "Press Space to play or pause the audio. Use the left/right arrow keys to skip back or forward 5 seconds.",
        "Click any line in the transcript to jump the audio to that point in the conversation.",
        "Use the search box at the top right to search within the current call\u2019s transcript.",
        "Adjust playback speed with the speed control (0.5x to 2x) for faster or slower review.",
        "The transcript highlights the current line as audio plays, so you always know where you are.",
    ]

    for i, text in enumerate(instructions, 1):
        if y < MARGIN_BOTTOM + 30:
            break
        y = _draw_numbered_item(c, y, i, text)

    _page_number(c, 3)


def _page_search(c: canvas.Canvas) -> None:
    """Page 4: Using the Search Page."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "Using the Search Page")

    y = _draw_screenshot(c, y, "search", "The Search Page interface")

    instructions = [
        "Type a name, phrase, or keyword into the search bar and press Enter to search across all transcripts.",
        "Use the date and phone number filters to narrow results to specific calls.",
        "Matching text is highlighted in the results so you can quickly see the context around each match.",
        "Double-click any result to open that call in the viewer, positioned at the matching section.",
        "Results are paginated \u2014 use the page controls at the bottom to browse through all matches.",
    ]

    for i, text in enumerate(instructions, 1):
        if y < MARGIN_BOTTOM + 30:
            break
        y = _draw_numbered_item(c, y, i, text)

    _page_number(c, 4)


def _page_excel(c: canvas.Canvas) -> None:
    """Page 5: Using the Excel Index."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "Using the Excel Index")

    y = _draw_screenshot(c, y, "excel", "The Excel Index spreadsheet")

    instructions = [
        "Open call-index.xlsx in Excel or Google Sheets. Each row represents one call.",
        "Use the header dropdowns to sort by date, duration, relevance, or any other column. "
        "Filter to show only HIGH relevance calls, a specific phone number, or a date range.",
        "Click a filename in the File column to open that call directly in the viewer.",
        "The Summary column contains a brief AI-generated overview of each call\u2019s content.",
        "The Full Transcript column has the complete text, useful for searching within Excel using Ctrl+F.",
    ]

    for i, text in enumerate(instructions, 1):
        if y < MARGIN_BOTTOM + 30:
            break
        y = _draw_numbered_item(c, y, i, text)

    _page_number(c, 5)


def _page_ai_analysis(c: canvas.Canvas) -> None:
    """Page 6: Understanding AI Analysis."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "Understanding AI Analysis")

    y = _draw_body_text(
        c, y,
        "Page 2 of each transcript PDF contains an AI-generated analysis of the call. "
        "This page is designed to help you quickly assess the call\u2019s potential relevance "
        "to your case without listening to the entire recording.",
    )
    y -= 6

    # Relevance badges
    c.setFont(FONT_BOLD, 10)
    c.setFillColor(COLOR_DARK)
    c.drawString(TEXT_AREA_LEFT, y, "Relevance Ratings")
    y -= 18

    badges = [
        (RELEVANCE_HIGH, "HIGH",
         "Contains case-related discussion, mentions of charges, co-defendants, "
         "witnesses, or potentially significant admissions."),
        (RELEVANCE_MEDIUM, "MEDIUM",
         "Indirect references to the case, court dates, legal proceedings, "
         "or conversations that may provide useful context."),
        (RELEVANCE_LOW, "LOW",
         "Personal or routine conversation with no apparent connection to the case."),
    ]

    for badge_color, label, description in badges:
        if y < MARGIN_BOTTOM + 40:
            break
        # Draw badge
        badge_w = 90
        badge_h = 16
        c.setFillColor(badge_color)
        c.roundRect(TEXT_AREA_LEFT, y - badge_h + 4, badge_w, badge_h, 3,
                    fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(FONT_BOLD, 9)
        c.drawCentredString(TEXT_AREA_LEFT + badge_w / 2, y - badge_h + 9, label)

        # Description next to badge
        c.setFillColor(COLOR_DARK)
        c.setFont(FONT, 9.5)
        desc_x = TEXT_AREA_LEFT + badge_w + 12
        desc_w = TEXT_AREA_RIGHT - desc_x
        desc_lines = _wrap_text_for_width(description, FONT, 9.5, desc_w)
        dy = y
        for dl in desc_lines:
            c.drawString(desc_x, dy - badge_h + 9, dl)
            dy -= 13
        y = min(y - badge_h - 4, dy - 4)
        y -= 6

    y -= 8

    # Analysis sections
    c.setFont(FONT_BOLD, 10)
    c.setFillColor(COLOR_DARK)
    c.drawString(TEXT_AREA_LEFT, y, "Analysis Sections")
    y -= 16

    sections = [
        ("Key Findings",
         "Bullet points highlighting the most notable statements, topics, or events "
         "mentioned during the call."),
        ("Speakers & Relationship",
         "Identifies who is speaking and their apparent relationship to the inmate "
         "(family member, friend, attorney, etc.)."),
        ("Call Summary",
         "A narrative overview of the full conversation, covering the main topics "
         "discussed and any significant details."),
    ]

    for section_name, section_desc in sections:
        if y < MARGIN_BOTTOM + 30:
            break
        c.setFillColor(COLOR_DARK)
        c.setFont(FONT_BOLD, 9.5)
        c.drawString(TEXT_AREA_LEFT + 10, y, section_name)
        y -= 14
        y = _draw_body_text(c, y, section_desc, size=9.5, line_height=13, indent=10)
        y -= 8

    _page_number(c, 6)


def _page_important_notes(c: canvas.Canvas) -> None:
    """Page 7: Important Notes."""
    _draw_border(c)
    y = PAGE_H - MARGIN_TOP - 0.15 * inch
    y = _draw_heading(c, y, "Important Notes")

    # ── Disclaimer box ──
    disclaimer_text = (
        "This package was generated using automated speech recognition and AI analysis. "
        "Transcripts may contain errors, especially with names, technical terms, slang, "
        "or overlapping speech. AI relevance ratings and summaries are provided as a starting "
        "point and should not be relied upon as definitive. Always verify critical details "
        "by listening to the original audio recording."
    )

    # Measure box height
    box_lines = _wrap_text_for_width(disclaimer_text, FONT, 9.5,
                                     TEXT_AREA_WIDTH - 24)
    box_line_h = 13
    box_h = len(box_lines) * box_line_h + 20
    box_y = y - box_h + 4

    # Warm cream fill with border
    c.setFillColor(colors.Color(0.99, 0.97, 0.93))
    c.setStrokeColor(colors.Color(0.85, 0.75, 0.60))
    c.setLineWidth(0.8)
    c.roundRect(TEXT_AREA_LEFT, box_y, TEXT_AREA_WIDTH, box_h, 4,
                fill=1, stroke=1)

    # Disclaimer label
    c.setFillColor(colors.Color(0.65, 0.45, 0.15))
    c.setFont(FONT_BOLD, 9)
    c.drawString(TEXT_AREA_LEFT + 12, y - 6, "IMPORTANT DISCLAIMER")

    # Disclaimer body
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT, 9.5)
    ty = y - 22
    for line in box_lines:
        c.drawString(TEXT_AREA_LEFT + 12, ty, line)
        ty -= box_line_h

    y = box_y - 28

    # ── Tips section ──
    c.setFillColor(COLOR_DARK)
    c.setFont(FONT_BOLD, 12)
    c.drawString(TEXT_AREA_LEFT, y, "Tips for Efficient Review")
    y -= 18

    tips = [
        "Start with the Excel index sorted by the Relevance column to prioritize HIGH-rated calls.",
        "Use the search page to find specific names, phrases, or topics across all calls at once.",
        "Timestamps in transcripts are clickable \u2014 in the viewer, click any line to jump to that moment in the audio.",
        "Keyboard shortcuts in the viewer: Space (play/pause), Left/Right arrows (skip 5s), Up/Down (change speed).",
        "This package works entirely offline \u2014 no internet connection is needed. Just open the HTML files in any modern browser.",
    ]

    for i, tip in enumerate(tips, 1):
        if y < MARGIN_BOTTOM + 30:
            break
        y = _draw_numbered_item(c, y, i, tip, size=9.5, line_height=13)

    _page_number(c, 7)


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def generate_guide_pdf(case_name: str, call_count: int,
                       gen_date: Optional[str] = None) -> bytes:
    """
    Generate a 7-page PDF user guide.

    Args:
        case_name: Case name for the cover page.
        call_count: Number of calls processed.
        gen_date: Optional date string; defaults to today.

    Returns:
        PDF content as bytes.
    """
    if not gen_date:
        gen_date = datetime.now().strftime("%B %d, %Y")

    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=letter, pageCompression=1)

    # Page 1: Cover (no page number)
    _page_cover(c, case_name, call_count, gen_date)
    c.showPage()

    # Page 2: What's in This Package
    _page_package_contents(c)
    c.showPage()

    # Page 3: Using the Call Viewer
    _page_viewer(c)
    c.showPage()

    # Page 4: Using the Search Page
    _page_search(c)
    c.showPage()

    # Page 5: Using the Excel Index
    _page_excel(c)
    c.showPage()

    # Page 6: Understanding AI Analysis
    _page_ai_analysis(c)
    c.showPage()

    # Page 7: Important Notes
    _page_important_notes(c)
    c.showPage()

    c.save()
    output.seek(0)
    return output.read()
