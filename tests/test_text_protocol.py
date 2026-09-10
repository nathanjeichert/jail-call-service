import unittest

from backend.summarization.text_protocol import parse_case_report_text, parse_system_audio_response


class SystemAudioTailTests(unittest.TestCase):
    def test_splits_summary_from_marker_line(self):
        raw = (
            "RELEVANCE: LOW\n\nNOTES: NONE\n\nBRIEF SUMMARY:\nHi.\n"
            'SYSTEM_AUDIO: [{"turn": 0, "text": "For English, press 1."}, {"turn": "2", "text": "Bye"}] trailing junk'
        )
        summary, markers = parse_system_audio_response(raw)
        self.assertTrue(summary.endswith("Hi."))
        self.assertEqual(markers, [{"turn": 0, "text": "For English, press 1."}, {"turn": 2, "text": "Bye"}])

    def test_missing_or_broken_marker_line_yields_no_markers(self):
        self.assertEqual(parse_system_audio_response("RELEVANCE: LOW")[1], [])
        summary, markers = parse_system_audio_response("RELEVANCE: LOW\nSYSTEM_AUDIO: not json")
        self.assertEqual(summary, "RELEVANCE: LOW")
        self.assertEqual(markers, [])


class CaseReportBlockTests(unittest.TestCase):
    def test_parses_findings_and_identities_and_drops_malformed_blocks(self):
        text = """
FINDING_START
CALL_ID: 3
HEADLINE: Witness Contact
TIMESTAMP: [04:12]
DETAIL: The defendant asked that a witness not speak to investigators.
FINDING_END

FINDING_START
CALL_ID: 4
HEADLINE: NONE
FINDING_END

FINDING_START
CALL_ID: seven
HEADLINE: Bad Id
DETAIL: dropped
FINDING_END

FINDING_START
CALL_ID: 5
HEADLINE: No Timestamp
TIMESTAMP: NONE
DETAIL: Fine.
FINDING_END

IDENTITY_START
NUMBER: (909) 555-0144
INFERENCE: Tanya, sister
CONFIDENCE: HIGH
IDENTITY_END

IDENTITY_START
NUMBER: (909) 555-0226
INFERENCE: Unknown
CONFIDENCE: maybe
IDENTITY_END

IDENTITY_START
NUMBER:
INFERENCE: nobody
IDENTITY_END
"""
        parsed = parse_case_report_text(text)
        self.assertEqual([(f.call_id, f.headline, f.timestamp) for f in parsed.findings],
                         [(3, "Witness Contact", "[04:12]"), (5, "No Timestamp", None)])
        self.assertEqual([(i.number, i.inference, i.confidence) for i in parsed.identities],
                         [("(909) 555-0144", "Tanya, sister", "HIGH"), ("(909) 555-0226", "Unknown", None)])

    def test_empty_text_is_an_empty_response(self):
        parsed = parse_case_report_text("")
        self.assertEqual(parsed.findings, [])
        self.assertEqual(parsed.identities, [])


if __name__ == "__main__":
    unittest.main()
