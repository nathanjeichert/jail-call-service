import unittest

from backend.gemini_structured import SummaryNote, SummaryResponse, render_summary_text
from backend.models import TranscriptTurn
from backend.pdf_utils import parse_summary_sections
from backend.transcript_formatting import compute_line_entries, hydrate_review_cues


class QuoteLineRefTests(unittest.TestCase):
    def test_line_ref_drives_deterministic_quote(self):
        turns = [
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[00:00]",
                text=(
                    "This is a deterministic quote extraction test for the summary layer "
                    "that should wrap across multiple transcript lines so the cited line "
                    "range can be rendered back into an exact quote."
                ),
            )
        ]
        line_entries = compute_line_entries(turns, 0.0)
        self.assertGreaterEqual(len(line_entries), 2)

        line_ref = (
            f"{line_entries[0]['page']}:{line_entries[0]['line']}"
            f"-{line_entries[1]['page']}:{line_entries[1]['line']}"
        )
        summary = (
            "RELEVANCE: MEDIUM\n\n"
            "NOTES:\n"
            f"- [00:00] INMATE [{line_ref}] — Example note.\n\n"
            "BRIEF SUMMARY:\n"
            "Example."
        )

        sections = parse_summary_sections(summary)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(cues[0]["line_cite"], line_ref)
        expected_quote = f"{line_entries[0]['text']} {line_entries[1]['text']}".strip()
        if len(line_entries) > 2:
            expected_quote += "..."
        self.assertEqual(cues[0]["quote"], expected_quote)

    def test_timestamp_only_cue_still_gets_line_cite(self):
        turns = [
            TranscriptTurn(
                speaker="OUTSIDE PARTY",
                timestamp="[01:23]",
                text="Hello there from the outside party.",
            )
        ]
        line_entries = compute_line_entries(turns, 0.0)
        summary = (
            "RELEVANCE: LOW\n\n"
            "NOTES:\n"
            '- [01:23] OUTSIDE PARTY — "Hello there" greeting only.\n\n'
            "BRIEF SUMMARY:\n"
            "Example."
        )

        sections = parse_summary_sections(summary)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(cues[0]["line_cite"], "1:1")
        self.assertEqual(cues[0]["quote"], "Hello there")

    def test_line_ref_keeps_quoted_words_in_note_text(self):
        turns = [
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[00:10]",
                text="This line exists only to supply a valid line cite for the note.",
            )
        ]
        line_entries = compute_line_entries(turns, 0.0)
        line_ref = f"{line_entries[0]['page']}:{line_entries[0]['line']}"
        summary = (
            "RELEVANCE: MEDIUM\n\n"
            "NOTES:\n"
            f'- [00:10] INMATE [{line_ref}] — Says the case is \"bullshit\" and wants counsel.\n\n'
            "BRIEF SUMMARY:\n"
            "Example."
        )

        sections = parse_summary_sections(summary)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(cues[0]["line_cite"], line_ref)
        self.assertEqual(cues[0]["note"], "Says the case is bullshit and wants counsel.")

    def test_long_line_ref_still_extracts_bounded_quote_excerpt(self):
        turns = [
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[00:00]",
                text=(
                    "This excerpt should wrap across several transcript lines so the hydration "
                    "step can still produce a deterministic bounded quote even if the cited "
                    "line range is longer than the display budget allows."
                ),
            )
        ]
        line_entries = compute_line_entries(turns, 0.0)
        self.assertGreaterEqual(len(line_entries), 4)

        line_ref = (
            f"{line_entries[0]['page']}:{line_entries[0]['line']}"
            f"-{line_entries[3]['page']}:{line_entries[3]['line']}"
        )
        summary = (
            "RELEVANCE: MEDIUM\n\n"
            "NOTES:\n"
            f"- [00:00] INMATE [{line_ref}] — Example note.\n\n"
            "BRIEF SUMMARY:\n"
            "Example."
        )

        sections = parse_summary_sections(summary)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(cues[0]["line_cite"], line_ref)
        expected_quote = " ".join(line_entries[i]["text"] for i in range(3)).strip() + "..."
        self.assertEqual(cues[0]["quote"], expected_quote)

    def test_mid_sentence_line_ref_gets_ellipses_on_both_sides(self):
        turns = [
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[00:00]",
                text=(
                    "This is a single continuous sentence with enough detail to wrap across "
                    "multiple transcript lines so a middle cited line should gain ellipses on "
                    "both sides when the extractor shows only that mid sentence fragment."
                ),
            )
        ]
        line_entries = compute_line_entries(turns, 0.0)
        self.assertGreaterEqual(len(line_entries), 3)

        middle_entry = line_entries[1]
        line_ref = f"{middle_entry['page']}:{middle_entry['line']}"
        summary = (
            "RELEVANCE: MEDIUM\n\n"
            "NOTES:\n"
            f"- [00:00] INMATE [{line_ref}] - Example note.\n\n"
            "BRIEF SUMMARY:\n"
            "Example."
        )

        sections = parse_summary_sections(summary)
        cues = hydrate_review_cues(sections.get("review_cue_items"), line_entries)

        self.assertEqual(cues[0]["quote"], f"...{middle_entry['text']}...")

    def test_structured_summary_renders_derived_timestamp_and_speaker(self):
        turns = [
            TranscriptTurn(
                speaker="OUTSIDE PARTY",
                timestamp="[02:10]",
                text="I talked to Roman and they still have not turned over discovery yet.",
            ),
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[02:18]",
                text="Okay keep checking on that because it matters for the case.",
            ),
        ]
        line_entries = compute_line_entries(turns, 0.0)
        first_turn_entry = next(entry for entry in line_entries if entry["turn_index"] == 0)
        second_turn_entry = next(entry for entry in line_entries if entry["turn_index"] == 1)
        later_line_ref = f"{second_turn_entry['page']}:{second_turn_entry['line']}"
        earlier_line_ref = f"{first_turn_entry['page']}:{first_turn_entry['line']}"

        summary = SummaryResponse(
            relevance="MEDIUM",
            notes=[
                SummaryNote(
                    line_ref=later_line_ref,
                    reason="Follow-up instruction about discovery.",
                    importance_rank=2,
                ),
                SummaryNote(
                    line_ref=earlier_line_ref,
                    reason="Outside party reports discovery has not been produced.",
                    importance_rank=1,
                ),
            ],
            brief_summary="Discovery status is discussed.",
        )

        rendered = render_summary_text(summary, line_entries)
        sections = parse_summary_sections(rendered)
        cues = sections.get("review_cue_items") or []

        self.assertEqual(cues[0]["timestamp"], "[02:10]")
        self.assertEqual(cues[0]["speaker"], "OUTSIDE PARTY")
        self.assertEqual(cues[0]["line_ref"], earlier_line_ref)
        self.assertEqual(cues[1]["timestamp"], "[02:18]")
        self.assertEqual(cues[1]["speaker"], "INMATE")
        self.assertEqual(cues[1]["line_ref"], later_line_ref)


if __name__ == "__main__":
    unittest.main()
