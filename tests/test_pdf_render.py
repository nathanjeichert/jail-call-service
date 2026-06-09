"""Smoke tests for backend.pdf_render — real Chromium renders, no mocking."""

import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader

from backend.pdf_render import render_pdf

LETTER_WIDTH_PTS = 612.0
LETTER_HEIGHT_PTS = 792.0


def _reader(pdf_bytes: bytes) -> PdfReader:
    assert pdf_bytes.startswith(b"%PDF"), "output is not a PDF"
    return PdfReader(io.BytesIO(pdf_bytes))


def _all_text(reader: PdfReader) -> str:
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _two_sheet_doc() -> str:
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page { size: letter; margin: 0; }
  html, body { margin: 0; padding: 0; }
  .sheet {
    width: 8.5in;
    height: 11in;
    box-sizing: border-box;
    break-after: page;
    background: #e8f0fe;
    padding: 1in;
    font-family: monospace;
  }
</style>
</head>
<body>
  <div class="sheet"><p>SHEET ONE CONTENT</p></div>
  <div class="sheet"><p>SHEET TWO CONTENT</p></div>
</body>
</html>"""


def _flowing_paged_doc(paragraphs: int = 120) -> str:
    body = "\n".join(
        f"<p>Paragraph {i}: the quick brown fox jumps over the lazy dog, "
        f"again and again, while the court reporter keeps typing entry "
        f"number {i} into the record without pause.</p>"
        for i in range(1, paragraphs + 1)
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: letter;
    margin: 1in;
    @bottom-right {{ content: counter(page) " of " counter(pages); }}
  }}
  body {{ font-family: monospace; font-size: 12pt; line-height: 1.5; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def test_two_sheets_render_two_letter_pages():
    pdf = render_pdf(_two_sheet_doc())
    reader = _reader(pdf)
    assert len(reader.pages) == 2
    for page in reader.pages:
        box = page.mediabox
        assert float(box.width) == pytest.approx(LETTER_WIDTH_PTS, abs=1)
        assert float(box.height) == pytest.approx(LETTER_HEIGHT_PTS, abs=1)


def test_render_is_not_blank_and_backgrounds_print():
    pdf = render_pdf(_two_sheet_doc())
    reader = _reader(pdf)
    text = _all_text(reader)
    assert "SHEET ONE CONTENT" in text
    assert "SHEET TWO CONTENT" in text
    # print_background=True embeds the background fill; a blank/no-background
    # page would carry far less content in its streams.
    assert len(pdf) > 1000


def test_paged_mode_page_counters_span_three_pages():
    pdf = render_pdf(_flowing_paged_doc(), paged=True)
    reader = _reader(pdf)
    assert len(reader.pages) >= 3
    text = _all_text(reader)
    assert "2 of" in text, "Paged.js page-counter margin box text missing"


def test_concurrent_renders_from_thread_pool():
    def make_doc(i: int) -> str:
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>@page {{ size: letter; margin: 0.5in; }}</style>
</head><body><h1>CONCURRENT DOC {i}</h1><p>body {i}</p></body></html>"""

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: (i, render_pdf(make_doc(i))), range(6)))

    assert len(results) == 6
    for i, pdf in results:
        reader = _reader(pdf)
        assert len(reader.pages) >= 1
        assert f"CONCURRENT DOC {i}" in _all_text(reader)
