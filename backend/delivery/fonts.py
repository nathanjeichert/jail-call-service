"""Embedded font assets for the delivery artifacts.

All client-facing artifacts share one type system (SIL OFL 1.1, license
texts alongside the font files in ``delivery/assets/fonts/``):

* **Source Serif 4** (variable, wght 200–900 + opsz 8–60, roman + italic) —
  display and reading serif for document titles, summaries, and large
  numerals. The optical-size axis matters: large headings use the display
  cut, running text the text cut. Note the axis tops out at 60, so an
  ``opsz`` above that is clamped.
* **Source Sans 3** (variable, wght 200–900, roman + italic) — UI and data
  face for tables, labels, and chrome.
* **IBM Plex Mono** (static 400/600) — timestamps, phone numbers, and
  page:line cites.
* **Courier Prime** (TTF) — transcript sheets only; metric-locked, the
  62-char line geometry in ``transcript_layout.py`` depends on it.

Two delivery mechanisms, because the artifacts have different constraints:

* PDFs are rendered by local headless Chromium, so ``pdf_font_css`` points
  ``@font-face`` at ``file://`` URIs (same pattern the transcript template
  has always used for Courier Prime).
* ``index.html`` must stay single-file and work from
  ``file://`` on locked-down machines with no network, so
  ``embedded_font_css`` inlines the woff2 binaries as base64 data URIs.
"""

import base64
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Tuple

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"

# (css family name, css font-weight value, style, file name)
_FACES: List[Tuple[str, str, str, str]] = [
    ("Source Serif 4", "200 900", "normal", "SourceSerif4-VF.woff2"),
    ("Source Serif 4", "200 900", "italic", "SourceSerif4-Italic-VF.woff2"),
    ("Source Sans 3", "200 900", "normal", "SourceSans3-VF.woff2"),
    ("Source Sans 3", "200 900", "italic", "SourceSans3-Italic-VF.woff2"),
    ("IBM Plex Mono", "400", "normal", "IBMPlexMono-Regular.woff2"),
    ("IBM Plex Mono", "600", "normal", "IBMPlexMono-SemiBold.woff2"),
    ("Courier Prime", "400", "normal", "CourierPrime-Regular.ttf"),
    ("Courier Prime", "700", "normal", "CourierPrime-Bold.ttf"),
]

DEFAULT_FAMILIES = ("Source Serif 4", "Source Sans 3", "IBM Plex Mono")


def _format_for(file_name: str) -> str:
    return "woff2" if file_name.endswith(".woff2") else "truetype"


def _face_rule(family: str, weight: str, style: str, src: str) -> str:
    return (
        "@font-face {\n"
        f'    font-family: "{family}";\n'
        f"    src: {src};\n"
        f"    font-weight: {weight};\n"
        f"    font-style: {style};\n"
        "}"
    )


def _selected(families: Iterable[str]) -> List[Tuple[str, str, str, str]]:
    wanted = set(families)
    return [face for face in _FACES if face[0] in wanted]


def pdf_font_css(families: Iterable[str] = DEFAULT_FAMILIES) -> str:
    """``@font-face`` rules pointing at local font files, for PDF templates."""
    rules = []
    for family, weight, style, file_name in _selected(families):
        uri = (FONTS_DIR / file_name).as_uri()
        rules.append(
            _face_rule(family, weight, style, f'url("{uri}") format("{_format_for(file_name)}")')
        )
    return "\n".join(rules)


@lru_cache(maxsize=None)
def _data_uri(file_name: str) -> str:
    data = (FONTS_DIR / file_name).read_bytes()
    mime = "font/woff2" if file_name.endswith(".woff2") else "font/ttf"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def embedded_font_css(families: Iterable[str] = DEFAULT_FAMILIES) -> str:
    """``@font-face`` rules with base64 data URIs, for self-contained HTML."""
    rules = []
    for family, weight, style, file_name in _selected(families):
        rules.append(
            _face_rule(family, weight, style, f'url("{_data_uri(file_name)}") format("{_format_for(file_name)}")')
        )
    return "\n".join(rules)
