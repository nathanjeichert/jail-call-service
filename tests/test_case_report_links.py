"""Unit tests for the case-report local-link extraction and /Launch rewriter."""

import io
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

from backend.delivery.case_report import (
    _extract_local_target,
    _rewrite_local_links_to_launch_actions,
)


class ExtractLocalTargetTests(unittest.TestCase):
    def test_absolute_chromium_temp_uri_viewer_with_query(self):
        uri = (
            "file:///var/folders/ab/T/tmp123xyz.html/../"
            "viewer.html?call=001-webb_call_01.mp3&t=00%3A31"
        )
        self.assertEqual(
            _extract_local_target(uri),
            "viewer.html?call=001-webb_call_01.mp3&t=00%3A31",
        )

    def test_absolute_uri_transcript_pdf(self):
        uri = "file:///private/tmp/tmpabc/transcripts/042-some_call.pdf"
        self.assertEqual(
            _extract_local_target(uri), "transcripts/042-some_call.pdf"
        )

    def test_transcript_pdf_with_fragment(self):
        uri = "file:///tmp/x/transcripts/001-call.pdf#page=3"
        self.assertEqual(
            _extract_local_target(uri), "transcripts/001-call.pdf#page=3"
        )

    def test_relative_uris_pass_through_unchanged(self):
        self.assertEqual(
            _extract_local_target("viewer.html?call=a.mp3&t=01%3A02"),
            "viewer.html?call=a.mp3&t=01%3A02",
        )
        self.assertEqual(_extract_local_target("viewer.html"), "viewer.html")
        self.assertEqual(
            _extract_local_target("transcripts/001-a.pdf"), "transcripts/001-a.pdf"
        )

    def test_percent_encoding_preserved_verbatim(self):
        uri = "file:///tmp/t/viewer.html?call=001-We%20bb.mp3&t=12%3A05"
        self.assertEqual(
            _extract_local_target(uri),
            "viewer.html?call=001-We%20bb.mp3&t=12%3A05",
        )

    def test_non_local_uris_return_none(self):
        self.assertIsNone(_extract_local_target("https://example.com/page"))
        self.assertIsNone(_extract_local_target("file:///tmp/t/other.html"))
        self.assertIsNone(_extract_local_target("file:///tmp/t/audio/001.mp3"))
        self.assertIsNone(_extract_local_target(""))
        self.assertIsNone(_extract_local_target("transcripts/nested/001.pdf"))


class RewriteLaunchActionTests(unittest.TestCase):
    def _pdf_with_links(self, uris):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        for i, uri in enumerate(uris):
            writer.add_annotation(
                page_number=0,
                annotation=Link(rect=(10, 10 + 20 * i, 100, 25 + 20 * i), url=uri),
            )
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def test_local_links_become_relative_launch_actions(self):
        local = (
            "file:///var/folders/zz/tmpq1w2.html/../"
            "viewer.html?call=001-call.mp3&t=00%3A31"
        )
        external = "https://example.com/docs"
        pdf = self._pdf_with_links([local, external])

        out = _rewrite_local_links_to_launch_actions(pdf)
        reader = PdfReader(io.BytesIO(out))

        actions = []
        for annot in reader.pages[0]["/Annots"]:
            a = annot.get_object()["/A"].get_object()
            actions.append((str(a.get("/S")), a.get("/F"), a.get("/URI")))

        launch = [a for a in actions if a[0] == "/Launch"]
        uri = [a for a in actions if a[0] == "/URI"]
        self.assertEqual(len(launch), 1)
        self.assertEqual(launch[0][1], "viewer.html?call=001-call.mp3&t=00%3A31")
        self.assertIsNone(launch[0][2])
        # The external link must remain an untouched URI action.
        self.assertEqual(len(uri), 1)
        self.assertEqual(uri[0][2], "https://example.com/docs")


if __name__ == "__main__":
    unittest.main()
