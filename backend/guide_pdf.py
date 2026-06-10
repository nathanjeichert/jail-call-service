"""
PDF user guide generation — HTML/CSS via headless Chromium (backend.pdf_render).

Renders a 7-page guide in the shared "Record" design language (Fraunces /
Public Sans / IBM Plex Mono, ink spine, one signal color), as explicit
fixed-size sheets like the transcript PDF:

  Page 1: Cover (case name, date, call count, offline note)
  Page 2: Contents of This Delivery (file table + keep-intact note)
  Page 3: Using the Call Viewer
  Page 4: Using the Call Index (search.html)
  Page 5: Using the Case Report
  Page 6: Reading the Analysis (relevance tiers + summary sections)
  Page 7: Important Notes (disclaimer + technical notes)
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import pdf_utils as U
from .design_fonts import pdf_font_css

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "guide_assets"

SCREENSHOT_FILES = {
    "viewer": "viewer_screenshot.png",
    "search": "search_screenshot.png",
}


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
    from .pdf_render import render_pdf

    if not gen_date:
        gen_date = datetime.now().strftime("%B %d, %Y")

    case_name = (case_name or "Case").strip() or "Case"
    call_count_display = f"{call_count:,} call{'s' if call_count != 1 else ''}"

    ctx = {
        "fonts_css": pdf_font_css(),
        "case_name": case_name,
        "case_name_short": U.shorten(case_name, 38),
        "gen_date": gen_date,
        "call_count": call_count,
        "call_count_display": call_count_display,
        "viewer_shot_url": _shot_url("viewer"),
        "search_shot_url": _shot_url("search"),
    }

    template = U.get_jinja_env().get_template("guide_template.html")
    html_str = template.render(**ctx)

    # Screenshots are referenced by absolute file:// URIs (_shot_url), so no
    # base URL is needed for resource resolution. Every page is an explicit
    # fixed-size sheet, so the plain (non-Paged.js) render path applies.
    return render_pdf(html_str)
