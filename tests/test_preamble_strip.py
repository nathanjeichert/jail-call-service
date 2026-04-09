import unittest

from backend.models import TranscriptTurn, WordTimestamp
from backend.transcription.base import (
    strip_edge_system_turns,
    strip_preamble,
    strip_shared_system_turns,
)


def make_turn(speaker: str, text: str, start_sec: float, duration_sec: float = 1.0) -> TranscriptTurn:
    minutes = int(start_sec // 60)
    seconds = int(start_sec % 60)
    return TranscriptTurn(
        speaker=speaker,
        text=text,
        timestamp=f"[{minutes:02d}:{seconds:02d}]",
        words=[
            WordTimestamp(
                text=text.split()[0] if text.split() else "...",
                start=start_sec * 1000,
                end=(start_sec + duration_sec) * 1000,
                speaker=speaker,
            )
        ],
    )


class StripPreambleTests(unittest.TestCase):
    def test_removes_one_to_many_segmented_robocall_span(self):
        turns = [
            make_turn("INMATE", "For English press for a collect call please enter your PIN number now.", 0.0),
            make_turn("OUTSIDE PARTY", "For English press for a collect call please enter your PIN number now.", 0.2),
            make_turn("INMATE", "Please enter the area code and phone number you are calling now.", 15.0),
            make_turn("OUTSIDE PARTY", "Please enter the area code and phone number you are calling now.", 15.2),
            make_turn("OUTSIDE PARTY", "Please hold. Please wait while your call is being connected.", 30.0),
            make_turn("INMATE", "Please hold. Please wait while your call is being connected.", 30.2),
            make_turn(
                "INMATE",
                "Please hold. Hello, this is a prepaid call from Jules, an inmate at the Santa Clara County Main Jail to accept this call.",
                40.0,
                8.0,
            ),
            make_turn("OUTSIDE PARTY", "Please hold. Hello, this is a prepaid call from", 40.1, 3.0),
            make_turn(
                "OUTSIDE PARTY",
                "an inmate at the Santa Clara County Main Jail to accept this call, press zero to refuse this call.",
                48.0,
                8.0,
            ),
            make_turn("OUTSIDE PARTY", "Okay.", 91.0),
            make_turn("INMATE", "We watched that movie already.", 94.0),
        ]

        kept = strip_preamble(
            turns,
            correlation_boundary_sec=94.0,
            correlation_regions=[(0.0, 89.0)],
        )

        self.assertEqual([turn.text for turn in kept[:2]], ["Okay.", "We watched that movie already."])

    def test_preserves_unmatched_one_sided_dialogue_inside_preamble_window(self):
        turns = [
            make_turn("INMATE", "For English press 1. For a collect call press 0.", 0.0),
            make_turn("OUTSIDE PARTY", "For English press 1. For a collect call press 0.", 0.2),
            make_turn("OUTSIDE PARTY", "Hello can you hear me yet?", 7.0),
            make_turn("INMATE", "Please enter the area code and phone number you are calling now.", 15.0),
            make_turn("OUTSIDE PARTY", "Please enter the area code and phone number you are calling now.", 15.3),
            make_turn("INMATE", "I can hear you now.", 30.0),
        ]

        kept = strip_preamble(
            turns,
            correlation_boundary_sec=20.0,
            correlation_regions=[(0.0, 18.0)],
        )

        self.assertEqual(
            [turn.text for turn in kept],
            ["Hello can you hear me yet?", "I can hear you now."],
        )

    def test_preserves_short_generic_duplicate_dialogue_without_audio_support(self):
        turns = [
            make_turn("INMATE", "For English press 1. For a collect call press 0.", 0.0),
            make_turn("OUTSIDE PARTY", "For English press 1. For a collect call press 0.", 0.2),
            make_turn("INMATE", "Hello?", 2.0),
            make_turn("OUTSIDE PARTY", "Hello?", 2.4),
            make_turn("INMATE", "Please enter the area code and phone number you are calling now.", 5.0),
            make_turn("OUTSIDE PARTY", "Please enter the area code and phone number you are calling now.", 5.2),
            make_turn("INMATE", "Now we can talk.", 20.0),
        ]

        kept = strip_preamble(turns)

        self.assertEqual(
            [turn.text for turn in kept],
            ["Hello?", "Hello?", "Now we can talk."],
        )

    def test_matches_numeric_variants_across_channels(self):
        turns = [
            make_turn("INMATE", "For English press 1. For a collect call press 0.", 0.0),
            make_turn("OUTSIDE PARTY", "For English press 1. For a collect call press 0.", 0.2),
            make_turn(
                "INMATE",
                "Your current balance is $20.00. Please enter the area code and phone number you are calling now.",
                15.0,
                4.0,
            ),
            make_turn(
                "OUTSIDE PARTY",
                "Your current balance is $20.0 Please enter the area code and phone number you are calling now.",
                15.1,
                4.0,
            ),
            make_turn("INMATE", "The call is connected now.", 40.0),
        ]

        kept = strip_preamble(turns)

        self.assertEqual([turn.text for turn in kept], ["The call is connected now."])

    def test_short_prompt_requires_audio_support(self):
        turns = [
            make_turn("INMATE", "For English press 1. For a collect call press 0.", 0.0),
            make_turn("OUTSIDE PARTY", "For English press 1. For a collect call press 0.", 0.2),
            make_turn("INMATE", "Please hold.", 10.0),
            make_turn("OUTSIDE PARTY", "Please hold.", 10.2),
            make_turn("INMATE", "The call is connected now.", 20.0),
        ]

        kept_without_audio = strip_preamble(turns)
        kept_with_audio = strip_preamble(
            turns,
            correlation_boundary_sec=12.0,
            correlation_regions=[(0.0, 12.0)],
        )

        self.assertEqual(
            [turn.text for turn in kept_without_audio],
            ["Please hold.", "Please hold.", "The call is connected now."],
        )
        self.assertEqual(
            [turn.text for turn in kept_with_audio],
            ["The call is connected now."],
        )

    def test_removes_midcall_countdown_warning_on_both_channels(self):
        turns = [
            make_turn("INMATE", "We need to call your mom back after this.", 120.0, 2.0),
            make_turn("OUTSIDE PARTY", "Okay, I will call her after we hang up.", 123.0, 2.0),
            make_turn("INMATE", "You have one minute remaining.", 240.0, 2.0),
            make_turn("OUTSIDE PARTY", "You have 1 minute remaining.", 240.3, 2.0),
            make_turn("INMATE", "All right, I love you.", 245.0, 1.0),
        ]

        kept = strip_shared_system_turns(turns)

        self.assertEqual(
            [turn.text for turn in kept],
            [
                "We need to call your mom back after this.",
                "Okay, I will call her after we hang up.",
                "All right, I love you.",
            ],
        )

    def test_removes_generic_provider_outro_without_provider_specific_logic(self):
        turns = [
            make_turn("INMATE", "I love you too.", 500.0, 1.0),
            make_turn("OUTSIDE PARTY", "Okay bye.", 501.0, 1.0),
            make_turn("INMATE", "Thank you for using Global Tel Link.", 503.0, 2.0),
            make_turn("OUTSIDE PARTY", "Thank you for using Global Tellink.", 503.4, 2.0),
        ]

        kept = strip_shared_system_turns(turns)

        self.assertEqual(
            [turn.text for turn in kept],
            ["I love you too.", "Okay bye."],
        )

    def test_preserves_short_mirrored_human_acknowledgments(self):
        turns = [
            make_turn("INMATE", "Yeah.", 180.0, 0.5),
            make_turn("OUTSIDE PARTY", "Yeah.", 180.2, 0.5),
            make_turn("INMATE", "I heard you.", 182.0, 1.0),
        ]

        kept = strip_shared_system_turns(turns)

        self.assertEqual(
            [turn.text for turn in kept],
            ["Yeah.", "Yeah.", "I heard you."],
        )

    def test_removes_leading_one_sided_system_balance_prompt(self):
        turns = [
            make_turn(
                "INMATE",
                "Your current balance is $20.00. This call is from a correctional facility and may be recorded.",
                82.0,
                4.0,
            ),
            make_turn("OUTSIDE PARTY", "Hello? Can you hear me now?", 88.0, 1.0),
        ]

        kept = strip_edge_system_turns(turns, correlation_boundary_sec=84.0)

        self.assertEqual([turn.text for turn in kept], ["Hello? Can you hear me now?"])

    def test_removes_trailing_one_sided_provider_outro(self):
        turns = [
            make_turn("INMATE", "All right, I love you.", 300.0, 1.0),
            make_turn("OUTSIDE PARTY", "Bye.", 301.0, 0.5),
            make_turn("OUTSIDE PARTY", "Thank you for using Global Tel Link.", 304.0, 2.0),
        ]

        kept = strip_edge_system_turns(turns)

        self.assertEqual(
            [turn.text for turn in kept],
            ["All right, I love you.", "Bye."],
        )


if __name__ == "__main__":
    unittest.main()
