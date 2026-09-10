"""
PDF user guide generation via headless Chromium (pdf_render).

Renders a 6-page guide in the shared "Record" design language (Fraunces /
Public Sans / IBM Plex Mono, ink spine, one signal color), as explicit
fixed-size sheets like the transcript PDF:

  Page 1: Cover (case name, date, call count)
  Page 2: Contents of This Delivery (file table + keep-intact note)
  Page 3: Using the Call Viewer
  Page 4: Using the Call Index (search.html)
  Page 5: Using the Case Report
  Page 6: Reading the Analysis (relevance tiers + summary sections)
"""

import logging
from typing import Optional

from ..formatting import format_generated_date, shorten
from .pdf_render import render_pdf
from .templates import ASSETS_DIR, render_template
from .theme import theme_css

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = ASSETS_DIR / "guide"

SCREENSHOT_FILES = {
    "viewer": "viewer_screenshot.png",
    "search": "search_screenshot.png",
}


def _shot_url(key: str) -> Optional[str]:
    filename = SCREENSHOT_FILES.get(key)
    if not filename:
        return None
    path = SCREENSHOTS_DIR / filename
    if path.is_file():
        return path.as_uri()
    return None


def generate_guide_pdf(case_name: str,
                       call_count: int,
                       gen_date: Optional[str] = None) -> bytes:
    gen_date = gen_date or format_generated_date()
    case_name = (case_name or "Case").strip() or "Case"
    call_count_display = f"{call_count:,} call{'s' if call_count != 1 else ''}"

    ctx = {
        "theme_css": theme_css("print"),
        "case_name": case_name,
        "case_name_short": shorten(case_name, 38),
        "gen_date": gen_date,
        "call_count": call_count,
        "call_count_display": call_count_display,
        "viewer_shot_url": _shot_url("viewer"),
        "search_shot_url": _shot_url("search"),
    }

    html_str = render_template("guide.html", **ctx)

    # Screenshots are referenced by absolute file:// URIs (_shot_url), so no
    # base URL is needed for resource resolution. Every page is an explicit
    # fixed-size sheet, so the plain (non-Paged.js) render path applies.
    return render_pdf(html_str)
