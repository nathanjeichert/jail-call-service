"""The summarize stage must produce the same canonical summary text and
filtered transcript regardless of which output protocol the engine speaks."""

import asyncio
import unittest
from unittest.mock import patch

from backend.models import CallResult, TranscriptTurn, WordTimestamp
from backend.pipeline import _summarize_with_engine
from backend.summaries import parse_summary_sections
from backend.summarization.base import CallSummary, SummarizationEngine, TokenUsage
from backend.summarization.schemas import SummaryNote, SummaryResponse
from backend.summarization.text_protocol import parse_system_audio_response
from backend.transcript_layout import compute_line_entries

OPENER = "This call is from a correctional facility and is subject to monitoring."
LINE = "Tell Darnell not to say anything about that night until the lawyer calls him."


def _turn(speaker, text, start_sec):
    words = []
    for i, w in enumerate(text.split()):
        s = start_sec * 1000 + i * 300.0
        words.append(WordTimestamp(text=w, start=s, end=s + 250.0))
    return TranscriptTurn(speaker=speaker, text=text, timestamp=f"[{int(start_sec)//60:02d}:{int(start_sec)%60:02d}]", words=words)


def _call():
    return CallResult(
        index=0,
        filename="call.wav",
        original_path="/in/call.wav",
        duration_seconds=120.0,
        turns=[
            _turn("INMATE", OPENER, 0),
            _turn("OUTSIDE PARTY", OPENER, 1),
            _turn("INMATE", LINE, 10),
            _turn("OUTSIDE PARTY", "He already knows.", 20),
        ],
    )


class StructuredEngine(SummarizationEngine):
    """Prepass engine (Gemini-shaped): separate detection, then JSON summary."""
    name = "fake-json"
    system_audio_prepass = True

    async def detect_system_audio(self, turns, metadata=None):
        return [{"turn": 0, "text": OPENER}, {"turn": 1, "text": OPENER}], TokenUsage(10, 1, 0)

    async def summarize_call(self, turns, prompt, metadata=None, *, detect_system_audio=False):
        # By now the transcript is filtered: cite the substantive line by its real position.
        entries = compute_line_entries(turns, 120.0)
        row = next(e for e in entries if e["text"].startswith("Tell Darnell"))
        return CallSummary(
            usage=TokenUsage(100, 20, 5),
            structured=SummaryResponse(
                relevance="HIGH",
                notes=[SummaryNote(line_ref=f"{row['page']}:{row['line']}", reason="Witness instruction.", importance_rank=1)],
                identity_of_outside_party="Sister.",
                brief_summary="Defendant asks the outside party to keep a witness quiet.",
            ),
        )

    async def synthesize_case_report(self, inputs):
        raise NotImplementedError


class TextEngine(SummarizationEngine):
    """Inline-detecting engine (Gemma-shaped): summary text with a SYSTEM_AUDIO tail."""
    name = "fake-text"
    system_audio_prepass = False

    async def summarize_call(self, turns, prompt, metadata=None, *, detect_system_audio=False):
        assert detect_system_audio, "pipeline must ask inline engines to detect when filtering is on"
        entries = compute_line_entries(turns, 120.0)
        row = next(e for e in entries if e["text"].startswith("Tell Darnell"))
        text = (
            "RELEVANCE: HIGH\n\nNOTES:\n"
            f"- [00:10] INMATE [{row['page']}:{row['line']}] — Witness instruction.\n"
            "- [00:00] INMATE [1:1] — Recorded call from a correctional facility.\n\n"
            "IDENTITY OF OUTSIDE PARTY:\nSister.\n\n"
            "BRIEF SUMMARY:\nDefendant asks the outside party to keep a witness quiet.\n"
            'SYSTEM_AUDIO: [{"turn": 0, "text": "' + OPENER + '"}, {"turn": 1, "text": "' + OPENER + '"}]'
        )
        # Like the Gemma engine, split the inline detection tail off ourselves.
        text, markers = parse_system_audio_response(text)
        return CallSummary(usage=TokenUsage(100, 20, 0), text=text, system_audio_markers=markers)

    async def synthesize_case_report(self, inputs):
        raise NotImplementedError


class SummarizeStageTests(unittest.TestCase):
    def _run(self, engine, mode="label"):
        call = _call()
        with patch("backend.pipeline.job_store.update_call") as update_call:
            text, usage = asyncio.run(_summarize_with_engine("job", call, engine, "PROMPT", mode))
        return call, text, usage, update_call

    def test_prepass_engine_filters_before_summary_and_sums_usage(self):
        call, text, usage, update_call = self._run(StructuredEngine())
        self.assertEqual(usage, TokenUsage(110, 21, 5))
        self.assertEqual([t.speaker for t in call.turns][:1], ["AUTOMATED MESSAGE"])
        self.assertEqual(len(call.turns), 3, "both channel copies of the opener collapse to one labeled turn")
        sections = parse_summary_sections(text)
        self.assertEqual(sections["relevance"], "HIGH")
        cues = sections["review_cue_items"]
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["note"], "Witness instruction.")
        self.assertTrue(cues[0]["line_ref"])
        update_call.assert_called()  # filtered turns were persisted

    def test_inline_engine_strips_system_notes_and_filters_after(self):
        call, text, usage, _ = self._run(TextEngine())
        self.assertEqual(usage, TokenUsage(100, 20, 0))
        self.assertEqual(len(call.turns), 3)
        self.assertEqual(call.turns[0].speaker, "AUTOMATED MESSAGE")
        sections = parse_summary_sections(text)
        notes = [c["note"] for c in sections["review_cue_items"]]
        self.assertEqual(notes, ["Witness instruction."], "the note restating the recorded-call notice is removed")
        self.assertEqual(sections["speakers"], "Sister.")

    def test_no_filtering_when_mode_is_off(self):
        call, text, _, update_call = self._run(NoDetectTextEngine(), mode=None)
        self.assertEqual(len(call.turns), 4)
        update_call.assert_not_called()
        self.assertIn("RELEVANCE: HIGH", text)


class NoDetectTextEngine(TextEngine):
    async def summarize_call(self, turns, prompt, metadata=None, *, detect_system_audio=False):
        assert not detect_system_audio, "detection must not be requested when filtering is off"
        return CallSummary(usage=TokenUsage(), text="RELEVANCE: HIGH\n\nNOTES: NONE\n\nBRIEF SUMMARY:\nQuiet call.")


if __name__ == "__main__":
    unittest.main()
