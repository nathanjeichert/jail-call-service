"""
Estate design system — shared colors, gradients, and drawing helpers.

Palette: cream paper, dark navy primary, muted gold accent.
Uses Pillow to generate gradient images for ReportLab embedding.
"""

import io

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# ── Page geometry ──
PAGE_W, PAGE_H = letter

# ── Estate color palette (ReportLab) ──
PRIMARY = colors.Color(0.106, 0.176, 0.290)        # #1B2D4A dark navy
PRIMARY_LIGHT = colors.Color(0.145, 0.239, 0.369)   # #253D5E
ACCENT = colors.Color(0.72, 0.63, 0.42)             # #B8A06A muted gold
CREAM = colors.Color(0.988, 0.980, 0.961)           # #FCFAF5
DARK = colors.Color(0.10, 0.12, 0.16)               # body headings
BODY = colors.Color(0.20, 0.22, 0.27)               # body text
MUTED = colors.Color(0.42, 0.45, 0.50)              # secondary text
RULE = colors.Color(0.80, 0.78, 0.72)               # divider lines
WARM_BOX = colors.Color(0.95, 0.94, 0.91)           # inset boxes

# RGB tuples for Pillow
PRIMARY_RGB = (27, 45, 74)
PRIMARY_LIGHT_RGB = (37, 61, 94)
CREAM_RGB = (252, 250, 245)

# Relevance badge colors
RELEVANCE_COLORS = {
    "HIGH": colors.Color(0.52, 0.10, 0.10),
    "MEDIUM": colors.Color(0.57, 0.35, 0.02),
    "LOW": colors.Color(0.09, 0.40, 0.20),
}
RELEVANCE_DESC = {
    "HIGH": "Likely case-relevant content identified",
    "MEDIUM": "Indirect references or useful context",
    "LOW": "No apparent case relevance",
}

# ── Estate geometry ──
STRIPE_W = 0.12 * inch  # left stripe width
HEADER_BAR_H = 0.5 * inch
MARGIN_LEFT = 1.1 * inch
MARGIN_RIGHT = 1.1 * inch
TEXT_LEFT = MARGIN_LEFT
TEXT_RIGHT = PAGE_W - MARGIN_RIGHT
TEXT_WIDTH = TEXT_RIGHT - TEXT_LEFT
Y_FLOOR = 0.7 * inch  # minimum y before stopping content

DPI = 144  # image resolution


# ═══════════════════════════════════════════════════════════════
# Pillow image generators → ReportLab ImageReader (no temp files)
# ═══════════════════════════════════════════════════════════════

def _to_reader(img: Image.Image) -> ImageReader:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return ImageReader(buf)


def gradient_image(w_pt: float, h_pt: float,
                   top_rgb: tuple, bottom_rgb: tuple,
                   noise: int = 0) -> ImageReader:
    """Vertical gradient image sized for PDF points (not cached)."""
    return _to_reader(_make_gradient_pil(w_pt, h_pt, top_rgb, bottom_rgb, noise))


_gradient_cache: dict[str, bytes] = {}


def _cached_gradient(key: str) -> bytes:
    """Generate a gradient PNG once, return bytes for reuse."""
    if key in _gradient_cache:
        return _gradient_cache[key]
    if key == "paper":
        bottom = tuple(max(0, c - 6) for c in CREAM_RGB)
        img = _make_gradient_pil(PAGE_W, PAGE_H, CREAM_RGB, bottom, noise=3)
    elif key == "stripe":
        img = _make_gradient_pil(STRIPE_W, PAGE_H, PRIMARY_RGB, PRIMARY_LIGHT_RGB, noise=1)
    elif key == "header_bar":
        img = _make_gradient_pil(PAGE_W - STRIPE_W, HEADER_BAR_H, PRIMARY_RGB, PRIMARY_LIGHT_RGB)
    else:
        raise ValueError(f"Unknown gradient key: {key}")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    _gradient_cache[key] = buf.getvalue()
    return _gradient_cache[key]


def _make_gradient_pil(w_pt, h_pt, top_rgb, bottom_rgb, noise=0):
    """Generate a PIL Image gradient (internal, not cached)."""
    w_px = max(1, int(w_pt * DPI / 72))
    h_px = max(1, int(h_pt * DPI / 72))
    img = Image.new("RGB", (w_px, h_px))
    draw = ImageDraw.Draw(img)
    r1, g1, b1 = top_rgb
    r2, g2, b2 = bottom_rgb
    for row in range(h_px):
        t = row / max(h_px - 1, 1)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        draw.line([(0, row), (w_px, row)], fill=(r, g, b))
    if noise:
        import random
        px = img.load()
        for x in range(0, w_px, 2):
            for y in range(0, h_px, 2):
                rv, gv, bv = px[x, y]
                d = random.randint(-noise, noise)
                px[x, y] = (
                    max(0, min(255, rv + d)),
                    max(0, min(255, gv + d)),
                    max(0, min(255, bv + d)),
                )
    return img


def _reader_from_cache(key: str) -> ImageReader:
    """Get a cached gradient as a fresh ImageReader."""
    return ImageReader(io.BytesIO(_cached_gradient(key)))


def paper_background() -> ImageReader:
    """Warm cream paper-like background with subtle grain (cached)."""
    return _reader_from_cache("paper")


def primary_stripe() -> ImageReader:
    """Left-side primary gradient stripe (cached)."""
    return _reader_from_cache("stripe")


def primary_header_bar() -> ImageReader:
    """Primary gradient header bar for inner pages (cached)."""
    return _reader_from_cache("header_bar")


# ═══════════════════════════════════════════════════════════════
# Shared drawing helpers
# ═══════════════════════════════════════════════════════════════

def draw_estate_page_bg(c: canvas.Canvas, include_stripe: bool = True) -> None:
    """Draw cream paper background + optional primary stripe + accent pinstripe."""
    c.drawImage(paper_background(), 0, 0, PAGE_W, PAGE_H)
    if include_stripe:
        c.drawImage(primary_stripe(), 0, 0, STRIPE_W, PAGE_H)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.6)
        c.line(STRIPE_W + 1, 0, STRIPE_W + 1, PAGE_H)


def draw_header_bar(c: canvas.Canvas, title: str, right_text: str = "") -> float:
    """Draw primary gradient header bar with title. Returns y below bar."""
    c.drawImage(primary_header_bar(), STRIPE_W, PAGE_H - HEADER_BAR_H,
                PAGE_W - STRIPE_W, HEADER_BAR_H)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(TEXT_LEFT, PAGE_H - 0.33 * inch, title)

    if right_text:
        c.setFillColor(colors.Color(0.65, 0.68, 0.75))
        c.setFont("Helvetica", 8.5)
        c.drawRightString(TEXT_RIGHT, PAGE_H - 0.31 * inch, right_text)

    return PAGE_H - HEADER_BAR_H - 0.28 * inch


def draw_section_heading(c: canvas.Canvas, y: float, title: str) -> float:
    """Accent bar + heading text + underline. Returns new y."""
    c.setFillColor(ACCENT)
    c.rect(TEXT_LEFT, y - 11 + 6, 2.5, 12, fill=1, stroke=0)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(TEXT_LEFT + 10, y - 3, title)
    tw = stringWidth(title, "Helvetica-Bold", 10.5)
    c.setStrokeColor(ACCENT)
    c.setLineWidth(0.4)
    c.line(TEXT_LEFT + 10, y - 7, TEXT_LEFT + 10 + tw + 8, y - 7)
    return y - 24


def draw_page_number(c: canvas.Canvas, num: int) -> None:
    """Draw centered page number at bottom."""
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 9)
    c.drawCentredString(PAGE_W / 2, 0.4 * inch, str(num))
