"""The delivery design system ("Record"), composed into one CSS payload.

Every client-facing artifact gets the same four layers, in order, at the top
of its ``<style>`` block:

1. ``@font-face`` rules from :mod:`fonts` (``file://`` for PDFs, base64 for
   the self-contained HTML pages),
2. ``assets/css/tokens.css`` - palette and type stacks,
3. ``assets/css/print.css`` or ``screen.css`` - sizes for the medium, the
   paper color, and (print) the sheet chrome the fixed-page PDFs share,
4. ``assets/css/components.css`` - the relevance marker, labels, eyebrows,
   stat numerals: pieces that look the same everywhere.

A template's own CSS follows and may override any of it. Change the look of
the deliverables in these files; touch a template only for its layout.
"""

from functools import lru_cache
from typing import Iterable, Literal

from .fonts import DEFAULT_FAMILIES, embedded_font_css, pdf_font_css
from .templates import ASSETS_DIR

CSS_DIR = ASSETS_DIR / "css"

Medium = Literal["print", "screen"]


@lru_cache(maxsize=None)
def _layer(name: str) -> str:
    return (CSS_DIR / name).read_text(encoding="utf-8").strip()


def theme_css(medium: Medium, families: Iterable[str] = DEFAULT_FAMILIES) -> str:
    """Fonts + tokens + medium sizes + shared components, for one artifact."""
    if medium not in ("print", "screen"):
        raise ValueError(f"unknown medium {medium!r}; expected 'print' or 'screen'")
    fonts = pdf_font_css(families) if medium == "print" else embedded_font_css(families)
    return "\n\n".join([fonts, _layer("tokens.css"), _layer(f"{medium}.css"), _layer("components.css")])
