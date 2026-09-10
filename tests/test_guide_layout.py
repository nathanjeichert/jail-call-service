import io
import unittest

from pypdf import PdfReader

from backend.delivery.guide_pdf import generate_guide_pdf


def _page_text_entries(page):
    entries = []

    def visitor_text(text, _cm, tm, _font_dict, _font_size):
        stripped = " ".join(text.split())
        if stripped:
            entries.append((stripped, tm[5]))

    page.extract_text(visitor_text=visitor_text)
    return entries


def _footer_clearance(page, footer_section: str, page_num: str):
    # Footer text is CSS-uppercased; Chromium bakes the transform into the
    # extracted text, so tokens are matched in their rendered uppercase form.
    entries = _page_text_entries(page)
    footer_y = max(y for text, y in entries if text == "USER GUIDE")
    footer_tokens = {"USER GUIDE", "·", footer_section, page_num}
    content_y = max(y for text, y in entries if text not in footer_tokens)
    return footer_y - content_y


class GuideLayoutTests(unittest.TestCase):
    def test_guide_stays_six_pages_and_keeps_body_text_above_footer(self):
        pdf_bytes = generate_guide_pdf(
            case_name="State v. Marcus Reeves",
            call_count=248,
            gen_date="April 21, 2026",
        )

        reader = PdfReader(io.BytesIO(pdf_bytes))

        self.assertEqual(len(reader.pages), 6)
        self.assertGreaterEqual(
            _footer_clearance(reader.pages[2], "USING THE CALL VIEWER", "03"),
            8.0,
        )
        self.assertGreaterEqual(
            _footer_clearance(reader.pages[3], "USING THE SEARCH PAGE", "04"),
            8.0,
        )
