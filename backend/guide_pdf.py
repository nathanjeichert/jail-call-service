"""
PDF user guide generation — HTML/CSS via WeasyPrint.

Renders a 7-page guide that mirrors the title/summary page aesthetic
(Avenir Next + Georgia, ink rules, teal accents, soft washes) by sharing
the same design language as ``pdf_cover_template.html``:

  Page 1: Cover (case name, date, call count)
  Page 2: What's in This Package (folder tree + descriptions)
  Page 3: Using the Call Viewer
  Page 4: Using the Search Page
  Page 5: Using the Excel Index
  Page 6: Understanding AI Analysis (relevance pills + sections)
  Page 7: Important Notes (disclaimer + tips)
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "guide_assets"
TEMPLATE_PATH = Path(__file__).parent / "guide_template.html"

SCREENSHOT_FILES = {
    "viewer": "viewer_screenshot.png",
    "search": "search_screenshot.png",
    "excel":  "excel_screenshot.png",
}


def _shorten_case_name(name: str, max_chars: int = 38) -> str:
    name = (name or "").strip()
    if len(name) <= max_chars:
        return name
    return name[: max_chars - 1].rstrip() + "\u2026"


def _shot_url(key: str) -> Optional[str]:
    filename = SCREENSHOT_FILES.get(key)
    if not filename:
        return None
    path = ASSETS_DIR / filename
    if path.is_file():
        return path.as_uri()
    return None


def generate_guide_pdf(case_name: str,
                       call_count: int,
                       gen_date: Optional[str] = None) -> bytes:
    from jinja2 import Template
    from weasyprint import HTML

    if not gen_date:
        gen_date = datetime.now().strftime("%B %d, %Y")

    case_name = (case_name or "Case").strip() or "Case"
    call_count_display = f"{call_count:,} call{'s' if call_count != 1 else ''}"

    ctx = {
        "case_name": case_name,
        "case_name_short": _shorten_case_name(case_name),
        "gen_date": gen_date,
        "call_count": call_count,
        "call_count_display": call_count_display,
        "viewer_shot_url": _shot_url("viewer"),
        "search_shot_url": _shot_url("search"),
        "excel_shot_url":  _shot_url("excel"),
    }

    template = Template(TEMPLATE_PATH.read_text())
    html_str = template.render(**ctx)

    return HTML(string=html_str, base_url=str(TEMPLATE_PATH.parent)).write_pdf()
