import unittest

from backend.models import TranscriptTurn, WordTimestamp
from backend.transcript_formatting import compute_line_entries
from backend.transcription.parakeet_engine import _interleave_words_to_turns


def make_word(text: str, start_sec: float, end_sec: float, speaker: str) -> WordTimestamp:
    return WordTimestamp(
        text=text,
        start=start_sec * 1000,
        end=end_sec * 1000,
        speaker=speaker,
    )


class TranscriptLayoutTests(unittest.TestCase):
    def test_merges_consecutive_same_speaker_turns_for_pdf_layout(self):
        turns = [
            TranscriptTurn(
                speaker="INMATE",
                text="Tell your mom thank you",
                timestamp="[00:00]",
                words=[
                    make_word("Tell", 0.0, 0.1, "INMATE"),
                    make_word("your", 0.1, 0.2, "INMATE"),
                    make_word("mom", 0.2, 0.3, "INMATE"),
                    make_word("thank", 0.3, 0.4, "INMATE"),
                    make_word("you", 0.4, 0.5, "INMATE"),
                ],
            ),
            TranscriptTurn(
                speaker="INMATE",
                text="tell her I said hi",
                timestamp="[00:02]",
                words=[
                    make_word("tell", 2.0, 2.1, "INMATE"),
                    make_word("her", 2.1, 2.2, "INMATE"),
                    make_word("I", 2.2, 2.3, "INMATE"),
                    make_word("said", 2.3, 2.4, "INMATE"),
                    make_word("hi", 2.4, 2.5, "INMATE"),
                ],
            ),
            TranscriptTurn(
                speaker="OUTSIDE PARTY",
                text="Okay I will",
                timestamp="[00:04]",
                words=[
                    make_word("Okay", 4.0, 4.1, "OUTSIDE PARTY"),
                    make_word("I", 4.1, 4.2, "OUTSIDE PARTY"),
                    make_word("will", 4.2, 4.3, "OUTSIDE PARTY"),
                ],
            ),
        ]

        entries = compute_line_entries(turns, audio_duration=10.0)

        self.assertEqual(len(entries), 2)
        self.assertIn("Tell your mom thank you tell her I said hi", entries[0]["rendered_text"])
        self.assertEqual(entries[0]["start"], 0.0)
        self.assertEqual(entries[0]["end"], 2.5)
        self.assertIn("OUTSIDE PARTY", entries[1]["rendered_text"])

    def test_keeps_short_backchannel_in_time_order(self):
        labels = {1: "INMATE", 2: "OUTSIDE PARTY"}
        ch1_words = [
            {"text": "I", "start": 0.0, "end": 0.1, "confidence": 0.9},
            {"text": "need", "start": 0.1, "end": 0.2, "confidence": 0.9},
            {"text": "you", "start": 0.2, "end": 0.3, "confidence": 0.9},
            {"text": "to", "start": 0.3, "end": 0.4, "confidence": 0.9},
            {"text": "listen", "start": 0.4, "end": 0.5, "confidence": 0.9},
            {"text": "because", "start": 1.0, "end": 1.1, "confidence": 0.9},
            {"text": "this", "start": 1.1, "end": 1.2, "confidence": 0.9},
            {"text": "matters", "start": 1.2, "end": 1.3, "confidence": 0.9},
        ]
        ch2_words = [
            {"text": "yeah", "start": 0.7, "end": 0.85, "confidence": 0.9},
        ]

        turns = _interleave_words_to_turns(ch1_words, ch2_words, labels)

        self.assertEqual([turn.speaker for turn in turns], ["INMATE", "OUTSIDE PARTY", "INMATE"])
        self.assertEqual(turns[0].text, "I need you to listen")
        self.assertEqual(turns[1].text.lower(), "yeah")
        self.assertEqual(turns[2].text, "because this matters")
        starts = [min(word.start for word in turn.words or []) for turn in turns]
        self.assertEqual(starts, sorted(starts))

    def test_switches_speakers_for_real_interruption(self):
        labels = {1: "INMATE", 2: "OUTSIDE PARTY"}
        ch1_words = [
            {"text": "I", "start": 0.0, "end": 0.1, "confidence": 0.9},
            {"text": "need", "start": 0.1, "end": 0.2, "confidence": 0.9},
            {"text": "you", "start": 0.2, "end": 0.3, "confidence": 0.9},
            {"text": "listen", "start": 2.0, "end": 2.1, "confidence": 0.9},
        ]
        ch2_words = [
            {"text": "wait", "start": 0.7, "end": 0.8, "confidence": 0.9},
            {"text": "listen", "start": 0.8, "end": 0.9, "confidence": 0.9},
            {"text": "to", "start": 0.9, "end": 1.0, "confidence": 0.9},
            {"text": "me", "start": 1.0, "end": 1.1, "confidence": 0.9},
        ]

        turns = _interleave_words_to_turns(ch1_words, ch2_words, labels)

        self.assertEqual([turn.speaker for turn in turns], ["INMATE", "OUTSIDE PARTY", "INMATE"])

    def test_prefers_interrupter_when_resumed_word_starts_at_same_time(self):
        labels = {1: "INMATE", 2: "OUTSIDE PARTY"}
        ch1_words = [
            {"text": "Okay", "start": 0.0, "end": 0.1, "confidence": 0.9},
            {"text": "fine", "start": 0.2, "end": 0.3, "confidence": 0.9},
            {"text": "then", "start": 0.4, "end": 0.5, "confidence": 0.9},
        ]
        ch2_words = [
            {"text": "wait", "start": 0.2, "end": 0.35, "confidence": 0.9},
            {"text": "hold", "start": 0.35, "end": 0.5, "confidence": 0.9},
        ]

        turns = _interleave_words_to_turns(ch1_words, ch2_words, labels)

        self.assertEqual([turn.speaker for turn in turns], ["INMATE", "OUTSIDE PARTY", "INMATE"])
        starts = [min(word.start for word in turn.words or []) for turn in turns]
        self.assertEqual(starts, sorted(starts))

    def test_line_entries_remain_time_monotonic_after_interruptions(self):
        labels = {1: "INMATE", 2: "OUTSIDE PARTY"}
        ch1_words = [
            {"text": "So", "start": 10.0, "end": 10.1, "confidence": 0.9},
            {"text": "I'm", "start": 10.1, "end": 10.2, "confidence": 0.9},
            {"text": "gonna", "start": 10.2, "end": 10.3, "confidence": 0.9},
            {"text": "what", "start": 16.0, "end": 16.1, "confidence": 0.9},
            {"text": "are", "start": 16.1, "end": 16.2, "confidence": 0.9},
            {"text": "you", "start": 16.2, "end": 16.3, "confidence": 0.9},
            {"text": "doing", "start": 16.3, "end": 16.4, "confidence": 0.9},
        ]
        ch2_words = [
            {"text": "Okay", "start": 15.7, "end": 15.9, "confidence": 0.9},
        ]

        turns = _interleave_words_to_turns(ch1_words, ch2_words, labels)
        entries = compute_line_entries(turns, audio_duration=30.0)

        starts = [entry["start"] for entry in entries]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
