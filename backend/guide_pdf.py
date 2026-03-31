"""
PDF user guide generation — Estate design.

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

from . import design as D
from .transcript_formatting import _wrap_text_for_width

logger = logging.getLogger(__name__)

# ── Screenshot config ──
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "guide_assets")
MAX_IMG_WIDTH = 468.0
MAX_IMG_HEIGHT = 280.0

SCREENSHOT_FILES = {
    "viewer": "viewer_screenshot.png",
    "search": "search_screenshot.png",
    "excel": "excel_screenshot.png",
}


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _draw_body_text(c: canvas.Canvas, y: float, text: str,
                    font: str = "Helvetica", size: float = 10,
                    line_height: float = 14,
                    color=None, indent: float = 0) -> float:
    if color is None:
        color = D.BODY
    c.setFillColor(color)
    c.setFont(font, size)
    max_w = D.TEXT_WIDTH - indent
    lines = _wrap_text_for_width(text, font, size, max_w)
    for line in lines:
        if y < D.Y_FLOOR:
            break
        c.drawString(D.TEXT_LEFT + indent, y, line)
        y -= line_height
    return y


def _draw_numbered_item(c: canvas.Canvas, y: float, number: int, text: str,
                        size: float = 10, line_height: float = 14) -> float:
    prefix = f"{number}."
    c.setFillColor(D.ACCENT)
    c.setFont("Helvetica-Bold", size)
    c.drawString(D.TEXT_LEFT, y, prefix)
    indent = 20
    c.setFillColor(D.BODY)
    c.setFont("Helvetica", size)
    max_w = D.TEXT_WIDTH - indent
    lines = _wrap_text_for_width(text, "Helvetica", size, max_w)
    for line in lines:
        if y < D.Y_FLOOR:
            break
        c.drawString(D.TEXT_LEFT + indent, y, line)
        y -= line_height
    y -= 2
    return y


def _draw_screenshot(c: canvas.Canvas, y: float, key: str, caption: str) -> float:
    img_path = os.path.join(ASSETS_DIR, SCREENSHOT_FILES.get(key, ""))
    has_image = os.path.isfile(img_path)

    if has_image:
        try:
            from reportlab.lib.utils import ImageReader
            img = ImageReader(img_path)
            iw, ih = img.getSize()
            scale = min(MAX_IMG_WIDTH / iw, MAX_IMG_HEIGHT / ih, 1.0)
            draw_w = iw * scale
            draw_h = ih * scale
        except Exception as e:
            logger.warning("Failed to load screenshot %s: %s", img_path, e)
            has_image = False

    if not has_image:
        draw_w = MAX_IMG_WIDTH
        draw_h = 160

    x = D.TEXT_LEFT + (D.TEXT_WIDTH - draw_w) / 2
    img_y = y - draw_h

    if has_image:
        c.drawImage(img, x, img_y, width=draw_w, height=draw_h,
                     preserveAspectRatio=True, anchor='c')
        c.setStrokeColor(D.RULE)
        c.setLineWidth(0.5)
        c.rect(x, img_y, draw_w, draw_h, stroke=1, fill=0)
    else:
        c.setStrokeColor(D.MUTED)
        c.setLineWidth(0.8)
        c.setDash(4, 4)
        c.rect(x, img_y, draw_w, draw_h, stroke=1, fill=0)
        c.setDash()
        c.setFillColor(D.MUTED)
        c.setFont("Helvetica-Oblique", 11)
        c.drawCentredString(x + draw_w / 2, img_y + draw_h / 2 - 5,
                            f"[{caption} \u2014 screenshot not available]")

    y = img_y - 6
    c.setFillColor(D.MUTED)
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawCentredString(D.PAGE_W / 2, y, caption)
    y -= 18
    return y


def _estate_page_start(c: canvas.Canvas, title: str) -> float:
    """Set up an Estate inner page: bg + stripe + header bar. Returns starting y."""
    D.draw_estate_page_bg(c)
    return D.draw_header_bar(c, title)


# ═══════════════════════════════════════════════════════════════
# Page renderers
# ═══════════════════════════════════════════════════════════════

def _page_cover(c: canvas.Canvas, case_name: str, call_count: int,
                gen_date: str) -> None:
    D.draw_estate_page_bg(c)

    # Forest gradient band — top portion
    band_h = D.PAGE_H * 0.38
    band_y = D.PAGE_H - band_h
    bar = D.gradient_image(D.PAGE_W - D.STRIPE_W, band_h,
                           D.PRIMARY_RGB, D.PRIMARY_LIGHT_RGB, noise=1)
    c.drawImage(bar, D.STRIPE_W, band_y, D.PAGE_W - D.STRIPE_W, band_h)

    cx = D.PAGE_W / 2 + D.STRIPE_W / 2
    y = D.PAGE_H - 1.0 * inch

    # Gold diamonds
    for dx in [-12, 0, 12]:
        c.saveState()
        c.translate(cx + dx, y + 0.3 * inch)
        c.rotate(45)
        c.setFillColor(D.ACCENT)
        c.rect(-2.5, -2.5, 5, 5, fill=1, stroke=0)
        c.restoreState()

    # Label
    c.setFillColor(D.ACCENT)
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, y, "CONFIDENTIAL")
    y -= 0.55 * inch

    # Title
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(cx, y, "User Guide")
    y -= 0.45 * inch

    # Gold rule
    c.setStrokeColor(D.ACCENT)
    c.setLineWidth(0.8)
    c.line(cx - inch, y, cx + inch, y)
    y -= 0.45 * inch

    # Case name
    c.setFillColor(colors.Color(0.85, 0.88, 0.85))
    c.setFont("Helvetica", 15)
    c.drawCentredString(cx, y, case_name)

    # Below the band — on cream
    y = band_y - 0.65 * inch

    meta_items = [
        ("Generated", gen_date),
        ("Total Calls", f"{call_count} call{'s' if call_count != 1 else ''} processed"),
        ("Contents", "Audio, Transcripts, AI Analysis, Index"),
    ]
    for label, value in meta_items:
        c.setFillColor(D.MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawCentredString(cx, y, label.upper())
        y -= 15
        c.setFillColor(D.BODY)
        c.setFont("Helvetica", 11)
        c.drawCentredString(cx, y, value)
        y -= 30

    c.setFillColor(D.MUTED)
    c.setFont("Helvetica-Oblique", 9)
    c.drawCentredString(cx, 0.65 * inch, "Open viewer/index.html to get started")


def _page_package_contents(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "What\u2019s in This Package")

    tree_lines = [
        "\u251c\u2500\u2500 viewer/index.html",
        "\u251c\u2500\u2500 search.html",
        "\u251c\u2500\u2500 call-index.xlsx",
        "\u251c\u2500\u2500 transcripts/",
        "\u251c\u2500\u2500 transcripts-no-summary/",
        "\u251c\u2500\u2500 audio/",
        "\u2514\u2500\u2500 guide.pdf",
    ]

    box_x = D.TEXT_LEFT
    line_h = 16
    box_h = len(tree_lines) * line_h + 16
    box_y = y - box_h + 4

    c.setFillColor(D.WARM_BOX)
    c.roundRect(box_x, box_y, D.TEXT_WIDTH, box_h, 4, fill=1, stroke=0)
    c.setStrokeColor(D.RULE)
    c.setLineWidth(0.3)
    c.roundRect(box_x, box_y, D.TEXT_WIDTH, box_h, 4, fill=0, stroke=1)

    c.setFillColor(D.DARK)
    c.setFont("Courier", 9.5)
    ty = y - 8
    for line in tree_lines:
        c.drawString(box_x + 12, ty, line)
        ty -= line_h

    y = box_y - 20

    descriptions = [
        ("viewer/index.html",
         "Interactive call viewer \u2014 play audio with synced, clickable transcripts. "
         "This is the main way to review calls."),
        ("search.html",
         "Full-text search across every transcript. Find names, phrases, or topics "
         "instantly and jump to the matching call."),
        ("call-index.xlsx",
         "Excel spreadsheet listing every call with date, duration, phone number, "
         "AI relevance rating, summary, and full transcript text."),
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
        if y < D.Y_FLOOR:
            break
        c.setFillColor(D.DARK)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(D.TEXT_LEFT, y, name)
        y -= 14
        y = _draw_body_text(c, y, desc, size=9.5, line_height=13, indent=10)
        y -= 6

    D.draw_page_number(c, 2)


def _page_viewer(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "Using the Call Viewer")
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
        if y < D.Y_FLOOR:
            break
        y = _draw_numbered_item(c, y, i, text)

    D.draw_page_number(c, 3)


def _page_search(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "Using the Search Page")
    y = _draw_screenshot(c, y, "search", "The Search Page interface")

    instructions = [
        "Type a name, phrase, or keyword into the search bar and press Enter to search across all transcripts.",
        "Use the date and phone number filters to narrow results to specific calls.",
        "Matching text is highlighted in the results so you can quickly see the context around each match.",
        "Double-click any result to open that call in the viewer, positioned at the matching section.",
        "Results are paginated \u2014 use the page controls at the bottom to browse through all matches.",
    ]
    for i, text in enumerate(instructions, 1):
        if y < D.Y_FLOOR:
            break
        y = _draw_numbered_item(c, y, i, text)

    D.draw_page_number(c, 4)


def _page_excel(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "Using the Excel Index")
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
        if y < D.Y_FLOOR:
            break
        y = _draw_numbered_item(c, y, i, text)

    D.draw_page_number(c, 5)


def _page_ai_analysis(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "Understanding AI Analysis")

    y = _draw_body_text(
        c, y,
        "Page 2 of each transcript PDF contains an AI-generated analysis of the call. "
        "This page is designed to help you quickly assess the call\u2019s potential relevance "
        "to your case without listening to the entire recording.",
    )
    y -= 6

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(D.DARK)
    c.drawString(D.TEXT_LEFT, y, "Relevance Ratings")
    y -= 18

    badges = [
        (D.RELEVANCE_COLORS["HIGH"], "HIGH",
         "Contains case-related discussion, mentions of charges, co-defendants, "
         "witnesses, or potentially significant admissions."),
        (D.RELEVANCE_COLORS["MEDIUM"], "MEDIUM",
         "Indirect references to the case, court dates, legal proceedings, "
         "or conversations that may provide useful context."),
        (D.RELEVANCE_COLORS["LOW"], "LOW",
         "Personal or routine conversation with no apparent connection to the case."),
    ]

    for badge_color, label, description in badges:
        if y < D.Y_FLOOR:
            break
        badge_w = 90
        badge_h = 16
        c.setFillColor(badge_color)
        c.roundRect(D.TEXT_LEFT, y - badge_h + 4, badge_w, badge_h, 3, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(D.TEXT_LEFT + badge_w / 2, y - badge_h + 9, label)

        c.setFillColor(D.BODY)
        c.setFont("Helvetica", 9.5)
        desc_x = D.TEXT_LEFT + badge_w + 12
        desc_w = D.TEXT_RIGHT - desc_x
        desc_lines = _wrap_text_for_width(description, "Helvetica", 9.5, desc_w)
        dy = y
        for dl in desc_lines:
            c.drawString(desc_x, dy - badge_h + 9, dl)
            dy -= 13
        y = min(y - badge_h - 4, dy - 4)
        y -= 6

    y -= 8

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(D.DARK)
    c.drawString(D.TEXT_LEFT, y, "Analysis Sections")
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
        if y < D.Y_FLOOR:
            break
        c.setFillColor(D.DARK)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(D.TEXT_LEFT + 10, y, section_name)
        y -= 14
        y = _draw_body_text(c, y, section_desc, size=9.5, line_height=13, indent=10)
        y -= 8

    D.draw_page_number(c, 6)


def _page_important_notes(c: canvas.Canvas) -> None:
    y = _estate_page_start(c, "Important Notes")

    # Disclaimer box
    disclaimer_text = (
        "This package was generated using automated speech recognition and AI analysis. "
        "Transcripts may contain errors, especially with names, technical terms, slang, "
        "or overlapping speech. AI relevance ratings and summaries are provided as a starting "
        "point and should not be relied upon as definitive. Always verify critical details "
        "by listening to the original audio recording."
    )

    box_lines = _wrap_text_for_width(disclaimer_text, "Helvetica", 9.5, D.TEXT_WIDTH - 24)
    box_line_h = 13
    box_h = len(box_lines) * box_line_h + 20
    box_y = y - box_h + 4

    # Warm cream fill with gold-tinted border
    c.setFillColor(colors.Color(0.99, 0.97, 0.93))
    c.setStrokeColor(colors.Color(0.85, 0.75, 0.60))
    c.setLineWidth(0.8)
    c.roundRect(D.TEXT_LEFT, box_y, D.TEXT_WIDTH, box_h, 4, fill=1, stroke=1)

    c.setFillColor(colors.Color(0.55, 0.40, 0.15))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(D.TEXT_LEFT + 12, y - 6, "IMPORTANT DISCLAIMER")

    c.setFillColor(D.BODY)
    c.setFont("Helvetica", 9.5)
    ty = y - 22
    for line in box_lines:
        c.drawString(D.TEXT_LEFT + 12, ty, line)
        ty -= box_line_h

    y = box_y - 28

    c.setFillColor(D.DARK)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(D.TEXT_LEFT, y, "Tips for Efficient Review")
    y -= 18

    tips = [
        "Start with the Excel index sorted by the Relevance column to prioritize HIGH-rated calls.",
        "Use the search page to find specific names, phrases, or topics across all calls at once.",
        "Timestamps in transcripts are clickable \u2014 in the viewer, click any line to jump to that moment in the audio.",
        "Keyboard shortcuts in the viewer: Space (play/pause), Left/Right arrows (skip 5s), Up/Down (change speed).",
        "This package works entirely offline \u2014 no internet connection is needed. Just open the HTML files in any modern browser.",
    ]

    for i, tip in enumerate(tips, 1):
        if y < D.Y_FLOOR:
            break
        y = _draw_numbered_item(c, y, i, tip, size=9.5, line_height=13)

    D.draw_page_number(c, 7)


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def generate_guide_pdf(case_name: str, call_count: int,
                       gen_date: Optional[str] = None) -> bytes:
    if not gen_date:
        gen_date = datetime.now().strftime("%B %d, %Y")

    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=letter, pageCompression=1)

    _page_cover(c, case_name, call_count, gen_date)
    c.showPage()

    _page_package_contents(c)
    c.showPage()

    _page_viewer(c)
    c.showPage()

    _page_search(c)
    c.showPage()

    _page_excel(c)
    c.showPage()

    _page_ai_analysis(c)
    c.showPage()

    _page_important_notes(c)
    c.showPage()

    c.save()
    output.seek(0)
    return output.read()
