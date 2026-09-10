"""The stored ``summary_json`` is the structured twin of the summary text.

A view built from the JSON must equal a view built from the same summary's
text (older jobs only have the text), the JSON must be absent for dummy and
unstructured summaries, and an operator edit must rebuild it from the
normalized text.
"""

import unittest

from backend.delivery.call_view import build_call_view
from backend.models import CallResult, TranscriptTurn, WordTimestamp
from backend.summaries import (
    DUMMY_SUMMARY_PREFIX,
    build_summary_json,
    normalize_structured_summary,
    normalize_summary_text,
    parse_summary_sections,
    render_summary_text,
    sections_from_summary_json,
)
from backend.summarization.schemas import SummaryNote, SummaryResponse
from backend.transcript_layout import compute_line_entries

SCRIPT = [
    ("INMATE", "Hey, it's me. Did you get the paperwork from Castillo's office yet?"),
    ("OUTSIDE PARTY", "It came yesterday. Your dad signed where they flagged it."),
    ("INMATE", "When you talk to Darnell, tell him not to say anything about that night until the lawyer calls him first."),
    ("OUTSIDE PARTY", "He already knows. The investigator came by his apartment Tuesday."),
]


def _turns():
    turns = []
    for i, (speaker, text) in enumerate(SCRIPT):
        start = 20.0 * (i + 1)
        words = [
            WordTimestamp(text=w, start=start * 1000 + j * 300.0, end=start * 1000 + j * 300.0 + 250.0)
            for j, w in enumerate(text.split())
        ]
        turns.append(TranscriptTurn(speaker=speaker, text=text, timestamp=f"[{int(start)//60:02d}:{int(start)%60:02d}]", words=words))
    return turns


def _call(summary, summary_json=None, turns=None):
    return CallResult(
        index=0, filename="call.wav", original_path="/in/call.wav", mp3_path="/out/001-call.mp3",
        duration_seconds=120.0, turns=turns if turns is not None else _turns(),
        summary=summary, summary_json=summary_json, status="done",
    )


def _line_ref(entries, turn_index):
    rows = [e for e in entries if e["turn_index"] == turn_index]
    return f"{rows[0]['page']}:{rows[0]['line']}-{rows[-1]['page']}:{rows[-1]['line']}"


def _structured_summary():
    entries = compute_line_entries(_turns(), 120.0)
    response = SummaryResponse(
        relevance="HIGH",
        notes=[
            SummaryNote(line_ref=_line_ref(entries, 2), reason="Witness instruction before counsel contact.", importance_rank=1),
            SummaryNote(line_ref=_line_ref(entries, 3), reason="Investigator visited the witness.", importance_rank=2),
        ],
        identity_of_outside_party="Sister; addressed as 'Tanya'.",
        brief_summary="The defendant asks the outside party to keep a witness quiet.",
    )
    normalized = normalize_structured_summary(response, entries)
    text = render_summary_text(normalized, entries)
    return text, build_summary_json(text, entries, structured=normalized), entries


# Everything a delivery surface reads from a view's summary: the call view's
# readers, the hydrated cues, and the raw NOTES body the case report feeds
# to synthesis. The text parser also keeps the raw text, which nothing reads.
SECTION_KEYS = ("structured", "relevance", "review_cue_items", "review_cues", "speakers", "call_summary")


def _view_shape(view):
    return {
        "structured": view.structured,
        "relevance": view.relevance,
        "brief": view.brief,
        "identity": view.identity,
        "cues": view.cues,
        "sections": {k: view.sections.get(k) for k in SECTION_KEYS},
    }


class SummaryJsonTests(unittest.TestCase):
    def test_view_from_json_equals_view_from_text(self):
        text, summary_json, _ = _structured_summary()
        self.assertIsNotNone(summary_json)

        from_text = build_call_view(_call(text))
        from_json = build_call_view(_call(text, summary_json))

        self.assertTrue(from_json.structured)
        self.assertEqual(len(from_json.cues), 2)
        self.assertEqual(_view_shape(from_json), _view_shape(from_text))
        # The JSON was actually used: the text parser is not consulted.
        self.assertEqual(sections_from_summary_json(summary_json)["review_cue_items"], summary_json["review_cue_items"])

    def test_json_carries_response_fields_and_parsed_cues(self):
        text, summary_json, _ = _structured_summary()
        sections = parse_summary_sections(text)
        self.assertEqual(summary_json["version"], 1)
        self.assertEqual(summary_json["relevance"], "HIGH")
        self.assertEqual(summary_json["review_cue_items"], sections["review_cue_items"])
        self.assertEqual(summary_json["identity_of_outside_party"], sections["speakers"])
        self.assertEqual(summary_json["brief_summary"], sections["call_summary"])
        # Notes follow the text's (time) order; ranks come from the engine's response.
        self.assertEqual([n["importance_rank"] for n in summary_json["notes"]], [1, 2])
        self.assertEqual([n["line_ref"] for n in summary_json["notes"]], [c["line_ref"] for c in sections["review_cue_items"]])
        # Round trip: the JSON's response fields still validate as a SummaryResponse.
        SummaryResponse(**{k: summary_json[k] for k in ("relevance", "notes", "identity_of_outside_party", "brief_summary")})

    def test_json_is_absent_for_dummy_empty_and_unstructured_text(self):
        self.assertIsNone(build_summary_json("", []))
        self.assertIsNone(build_summary_json(None, []))
        self.assertIsNone(build_summary_json(f"{DUMMY_SUMMARY_PREFIX} FOR call.wav**\n\n- skipped", []))
        self.assertIsNone(build_summary_json("Summary unavailable for this call.", []))
        self.assertEqual(sections_from_summary_json(None), {})
        self.assertEqual(sections_from_summary_json({"structured": False}), {})
        self.assertEqual(sections_from_summary_json({"structured": True, "relevance": ""}), {})

    def test_dummy_summary_is_no_summary_even_with_stale_json(self):
        text, summary_json, _ = _structured_summary()
        view = build_call_view(_call(f"{DUMMY_SUMMARY_PREFIX} FOR call.wav**", summary_json))
        self.assertTrue(view.is_dummy)
        self.assertFalse(view.structured)
        self.assertEqual(view.cues, [])

    def test_older_job_without_json_still_parses_text(self):
        text, _, _ = _structured_summary()
        view = build_call_view(_call(text, None))
        self.assertTrue(view.structured)
        self.assertEqual(view.relevance, "HIGH")
        self.assertEqual(len(view.cues), 2)

    def test_operator_edit_rebuilds_json_from_normalized_text(self):
        # What the PUT summary route does: normalize the edited text, rebuild the JSON from it.
        text, _, entries = _structured_summary()
        edited = text.replace("Witness instruction before counsel contact.", "Instruction to a witness (edited).")
        stored_text = normalize_summary_text(edited, entries)
        stored_json = build_summary_json(stored_text, entries)
        self.assertIn("Instruction to a witness (edited).", stored_text)
        self.assertEqual([n["reason"] for n in stored_json["notes"]][0], "Instruction to a witness (edited).")
        self.assertEqual(_view_shape(build_call_view(_call(stored_text, stored_json))),
                         _view_shape(build_call_view(_call(stored_text))))
        # Free text without a RELEVANCE line stays as written and has no JSON.
        free = normalize_summary_text("Operator note: nothing of interest.", entries)
        self.assertEqual(free, "Operator note: nothing of interest.")
        self.assertIsNone(build_summary_json(free, entries))

    def test_hydration_does_not_mutate_the_stored_json(self):
        text, summary_json, _ = _structured_summary()
        before = [dict(item) for item in summary_json["review_cue_items"]]
        build_call_view(_call(text, summary_json))
        self.assertEqual(summary_json["review_cue_items"], before)


if __name__ == "__main__":
    unittest.main()
