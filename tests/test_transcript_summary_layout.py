import io
import unittest

from pypdf import PdfReader

from backend.gemini_structured import SummaryNote, SummaryResponse, render_summary_text
from backend.models import TranscriptTurn
from backend.pdf_utils import parse_summary_sections
from backend.summary_normalization import (
    SUMMARY_NOTE_LIMITS,
    normalize_structured_summary,
    normalize_summary_text,
)
from backend.transcript_formatting import (
    compute_line_entries,
    create_pdf,
    hydrate_review_cues,
    paginate_structured_summary,
)


def _fixture_001_1646962560_5000_13_159_593():
    turns = []
    for idx in range(18):
        speaker = "INMATE" if idx % 2 == 0 else "OUTSIDE PARTY"
        minute = idx
        text = (
            f"Fixture transcript segment {idx} discusses the arrest, witness statements, "
            f"video evidence, attorney strategy, phone logistics, and a detailed description "
            f"of what allegedly happened at the scene so the wrapped transcript lines are long "
            f"enough to produce stable multi-line quote excerpts for summary cue {idx}."
        )
        turns.append(
            TranscriptTurn(
                speaker=speaker,
                timestamp=f"[{minute:02d}:00]",
                text=text,
            )
        )
    return turns


def _line_ref_blocks(line_entries, count, *, span=3):
    refs = []
    cursor = 0
    while len(refs) < count and cursor + span - 1 < len(line_entries):
        start = line_entries[cursor]
        end = line_entries[cursor + span - 1]
        refs.append(f"{start['page']}:{start['line']}-{end['page']}:{end['line']}")
        cursor += span
    return refs


class TranscriptSummaryLayoutTests(unittest.TestCase):
    def _build_summary_fixture(self, note_count):
        turns = _fixture_001_1646962560_5000_13_159_593()
        line_entries = compute_line_entries(turns, 18 * 60.0)
        line_refs = _line_ref_blocks(line_entries, note_count, span=3)
        summary = SummaryResponse(
            relevance="HIGH",
            notes=[
                SummaryNote(
                    line_ref=line_ref,
                    reason=(
                        "Flags a detailed attorney-review moment about witness credibility, "
                        "charging posture, evidence, or defense preparation."
                    ),
                )
                for line_ref in line_refs
            ],
            identity_of_outside_party=(
                "The outside party is the defendant's father, addressed as Dad early in the call, "
                "and he appears to be coordinating family support and legal logistics."
            ),
            brief_summary=(
                "The defendant disputes the allegations, discusses witness statements and video evidence, "
                "and lays out legal and financial steps for mounting a defense while in custody."
            ),
        )
        return turns, line_entries, summary

    def test_normalized_structured_summary_caps_high_notes(self):
        turns, line_entries, summary = self._build_summary_fixture(30)
        normalized = normalize_structured_summary(summary, line_entries)
        self.assertLessEqual(len(normalized.notes), SUMMARY_NOTE_LIMITS["HIGH"]["hard"])
        self.assertGreater(len(normalized.notes), 0)

        rendered = render_summary_text(normalized, line_entries)
        sections = parse_summary_sections(rendered)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(len(cues), len(normalized.notes))
        self.assertIn("IDENTITY OF OUTSIDE PARTY", rendered)
        self.assertIn("BRIEF SUMMARY", rendered)

    def test_normalize_summary_text_strips_reasoning_preamble(self):
        turns, line_entries, _ = self._build_summary_fixture(6)
        raw = (
            "<|channel>thought\n"
            "Thinking Process:\n"
            "Long internal reasoning that should never reach the exported summary.\n\n"
            "RELEVANCE: LOW\n\n"
            "NOTES:\n"
            "- [00:00] INMATE [1:1] — First low-relevance note.\n"
            "- [01:00] OUTSIDE PARTY [1:2] — Second low-relevance note.\n"
            "- [02:00] INMATE [1:3] — Third low-relevance note.\n"
            "- [03:00] OUTSIDE PARTY [1:4] — Fourth low-relevance note that should be trimmed.\n\n"
            "IDENTITY OF OUTSIDE PARTY:\n"
            "A caller who appears to be a family contact helping with logistics.\n\n"
            "BRIEF SUMMARY:\n"
            "A mostly personal call with a small amount of logistical discussion."
        )

        normalized = normalize_summary_text(raw, line_entries)
        sections = parse_summary_sections(normalized)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertTrue(normalized.startswith("RELEVANCE: LOW"))
        self.assertNotIn("Thinking Process", normalized)
        self.assertEqual(len(cues), SUMMARY_NOTE_LIMITS["LOW"]["hard"])

    def test_paginator_preserves_every_kept_cue(self):
        turns, line_entries, summary = self._build_summary_fixture(19)
        normalized = normalize_structured_summary(summary, line_entries)
        rendered = render_summary_text(normalized, line_entries)
        sections = parse_summary_sections(rendered)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        pagination = paginate_structured_summary(
            cues,
            speakers=sections.get("speakers", ""),
            call_summary=sections.get("call_summary", ""),
        )
        paged_cues = pagination["page1_review_cues"] + [
            cue
            for page in pagination["overflow_review_cue_pages"]
            for cue in page["review_cues"]
        ]

        self.assertEqual(len(paged_cues), len(cues))
        self.assertTrue(pagination["overflow_review_cue_pages"])

    def test_pdf_regression_keeps_context_on_page_one_and_removes_continued_chrome(self):
        turns, line_entries, summary = self._build_summary_fixture(19)
        normalized = normalize_structured_summary(summary, line_entries)
        rendered_summary = render_summary_text(normalized, line_entries)

        pdf_bytes = create_pdf(
            {
                "CASE_NAME": "People v. Fixture",
                "FILE_NAME": "1646962560_5000_13_159_593.wav",
                "FILE_DURATION": "16:30",
                "CALL_DATETIME": "2022-03-07 19:27",
                "INMATE_NAME": "Fixture Defendant",
                "OUTSIDE_NUMBER_FMT": "(555) 010-1234",
            },
            turns,
            summary=rendered_summary,
            audio_duration=18 * 60.0,
        )

        reader = PdfReader(io.BytesIO(pdf_bytes))
        cover_text = reader.pages[0].extract_text() or ""
        summary1_text = reader.pages[1].extract_text() or ""
        summary_more_text = "\n".join((reader.pages[i].extract_text() or "") for i in range(2, 4))

        self.assertIn("DURATION", cover_text)
        self.assertNotIn("16:30", summary1_text)
        self.assertIn("IDENTITY OF OUTSIDE PARTY", summary1_text)
        self.assertIn("BRIEF SUMMARY", summary1_text)
        self.assertNotIn("IDENTITY OF OUTSIDE PARTY", summary_more_text)
        self.assertNotIn("BRIEF SUMMARY", summary_more_text)
        self.assertIn("AUDIO TIMESTAMP", summary1_text)
        self.assertIn("TRANSCRIPT CITE", summary1_text)
        self.assertNotIn("Notes, continued", summary_more_text)
        self.assertNotIn("CONTINUED FROM PAGE 02", summary_more_text)
        self.assertNotIn("02 / 03", summary_more_text)

        timestamps = [
            cue["timestamp"]
            for cue in hydrate_review_cues(parse_summary_sections(rendered_summary).get("review_cue_items"), line_entries)
        ]
        combined_summary_text = summary1_text + "\n" + summary_more_text
        for timestamp in timestamps:
            self.assertIn(timestamp, combined_summary_text)

    def test_pdf_allows_third_summary_page_for_dense_high_call(self):
        turns, line_entries, summary = self._build_summary_fixture(21)
        normalized = normalize_structured_summary(summary, line_entries)
        rendered_summary = render_summary_text(normalized, line_entries)

        pdf_bytes = create_pdf(
            {
                "CASE_NAME": "People v. Dense Fixture",
                "FILE_NAME": "dense-high-call.wav",
                "FILE_DURATION": "18:00",
                "CALL_DATETIME": "2022-03-07 19:27",
                "INMATE_NAME": "Fixture Defendant",
                "OUTSIDE_NUMBER_FMT": "(555) 010-1234",
            },
            turns,
            summary=rendered_summary,
            audio_duration=18 * 60.0,
        )

        reader = PdfReader(io.BytesIO(pdf_bytes))
        self.assertGreaterEqual(len(reader.pages), 5)
        third_summary_text = reader.pages[3].extract_text() or ""
        timestamps = [
            cue["timestamp"]
            for cue in hydrate_review_cues(parse_summary_sections(rendered_summary).get("review_cue_items"), line_entries)
        ]
        self.assertIn(timestamps[-1], third_summary_text)

    def test_parser_ignores_section_words_inside_note_bodies(self):
        summary_text = (
            "RELEVANCE: HIGH\n\n"
            "NOTES:\n"
            "- [00:00] INMATE [1:1] — This note mentions the summary page layout system and should remain a note.\n"
            "- [01:00] OUTSIDE PARTY [1:2] — This note mentions speaker notes and should also remain a note.\n\n"
            "IDENTITY OF OUTSIDE PARTY:\n"
            "Father.\n\n"
            "BRIEF SUMMARY:\n"
            "Actual brief summary."
        )

        sections = parse_summary_sections(summary_text)
        cues = sections.get("review_cue_items") or []

        self.assertEqual(len(cues), 2)
        self.assertEqual(sections.get("speakers"), "Father.")
        self.assertEqual(sections.get("call_summary"), "Actual brief summary.")
        self.assertIn("summary page layout system", cues[0]["note"])
        self.assertIn("speaker notes", cues[1]["note"])


if __name__ == "__main__":
    unittest.main()
