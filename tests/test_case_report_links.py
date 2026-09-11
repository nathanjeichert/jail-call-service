"""Unit tests for the case-report local-link extraction and rewriter."""

import io
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from backend.delivery.case_report import _extract_local_target, _rewrite_local_links


class ExtractLocalTargetTests(unittest.TestCase):
    def test_absolute_chromium_temp_uri_index_with_call_hash(self):
        uri = (
            "file:///var/folders/ab/T/tmp123xyz.html/../"
            "index.html#call=001-webb_call_01.mp3&t=00%3A31"
        )
        self.assertEqual(
            _extract_local_target(uri),
            "index.html#call=001-webb_call_01.mp3&t=00%3A31",
        )

    def test_index_hash_keeps_ampersand_and_unencoded_colon(self):
        uri = "file:///tmp/t/index.html#call=002-call.mp3&t=01:14"
        self.assertEqual(_extract_local_target(uri), "index.html#call=002-call.mp3&t=01:14")

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
            _extract_local_target("index.html#call=a.mp3&t=01%3A02"),
            "index.html#call=a.mp3&t=01%3A02",
        )
        self.assertEqual(_extract_local_target("index.html"), "index.html")
        self.assertEqual(_extract_local_target("index.html#index"), "index.html#index")
        self.assertEqual(
            _extract_local_target("transcripts/001-a.pdf"), "transcripts/001-a.pdf"
        )

    def test_percent_encoding_preserved_verbatim(self):
        uri = "file:///tmp/t/index.html#call=001-We%20bb.mp3&t=12%3A05"
        self.assertEqual(
            _extract_local_target(uri),
            "index.html#call=001-We%20bb.mp3&t=12%3A05",
        )

    def test_non_local_uris_return_none(self):
        self.assertIsNone(_extract_local_target("https://example.com/page"))
        self.assertIsNone(_extract_local_target("https://example.com/transcripts/001-a.pdf"))
        self.assertIsNone(_extract_local_target("file:///tmp/t/other.html"))
        # The retired two-page form is not produced any more and is not a local target.
        self.assertIsNone(_extract_local_target("file:///tmp/t/viewer.html?call=a.mp3"))
        self.assertIsNone(_extract_local_target("file:///tmp/t/audio/001.mp3"))
        self.assertIsNone(_extract_local_target(""))
        self.assertIsNone(_extract_local_target("transcripts/nested/001.pdf"))


def _pdf_with_links(uris, *, named_dest=None):
    """Two blank pages; link annotations on page 1, optionally a named
    destination to page 2 plus a GoTo link that uses it (the contents page)."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    for i, uri in enumerate(uris):
        writer.add_annotation(
            page_number=0,
            annotation=Link(rect=(10, 10 + 20 * i, 100, 25 + 20 * i), url=uri),
        )
    if named_dest:
        writer.add_named_destination(named_dest, 1)
        action = DictionaryObject({
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): TextStringObject(named_dest),
        })
        annot = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(10), NumberObject(200), NumberObject(100), NumberObject(215)]),
            NameObject("/A"): action,
        })
        writer.add_annotation(page_number=0, annotation=annot)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _actions(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for annot in reader.pages[0]["/Annots"]:
        a = annot.get_object()["/A"].get_object()
        out.append({k: a.get(k) for k in ("/S", "/F", "/URI", "/D", "/NewWindow")})
    return reader, out


class RewriteLocalLinksTests(unittest.TestCase):
    def test_viewer_links_become_dot_relative_uri_actions(self):
        local = (
            "file:///var/folders/zz/tmpq1w2.html/../"
            "index.html#call=001-call.mp3&t=00%3A31"
        )
        external = "https://example.com/docs"
        _, actions = _actions(_rewrite_local_links(_pdf_with_links([local, external])))

        self.assertEqual([str(a["/S"]) for a in actions], ["/URI", "/URI"])
        # A bare "index.html#..." is fixed up as a hostname by Chromium's
        # viewer; "./" forces resolution against the PDF's own location.
        self.assertEqual(actions[0]["/URI"], "./index.html#call=001-call.mp3&t=00%3A31")
        self.assertIsNone(actions[0]["/F"])
        self.assertEqual(actions[1]["/URI"], "https://example.com/docs")

    def test_transcript_links_keep_their_page_fragment(self):
        pdf = _pdf_with_links([
            "file:///tmp/x/transcripts/002-call.pdf#page=5",
            "file:///tmp/x/transcripts/003-a%20call.pdf",
        ])
        _, actions = _actions(_rewrite_local_links(pdf))

        self.assertEqual([str(a["/S"]) for a in actions], ["/URI", "/URI"])
        self.assertEqual(actions[0]["/URI"], "./transcripts/002-call.pdf#page=5")
        self.assertEqual(actions[1]["/URI"], "./transcripts/003-a%20call.pdf")

    def test_named_destinations_survive_the_rewrite(self):
        pdf = _pdf_with_links(["file:///tmp/x/index.html"], named_dest="sec-findings")
        reader, actions = _actions(_rewrite_local_links(pdf))

        self.assertIn("sec-findings", reader.named_destinations)
        goto = [a for a in actions if str(a["/S"]) == "/GoTo"]
        self.assertEqual(len(goto), 1)
        self.assertEqual(goto[0]["/D"], "sec-findings")
        self.assertEqual(len(reader.pages), 2)


if __name__ == "__main__":
    unittest.main()
