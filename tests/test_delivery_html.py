import json
import re
import unittest

from backend.delivery.call_view import build_call_views
from backend.delivery.index_html import generate_index_html
from backend.delivery.theme import theme_css
from backend.html_json import dump_script_safe_json
from backend.models import CallResult, CallStatus, TranscriptTurn


def _fixture_call() -> CallResult:
    return CallResult(
        index=0,
        filename="bad </script>.wav",
        original_path="/tmp/bad </script>.wav",
        mp3_path="/tmp/bad </script>.mp3",
        duration_seconds=65.0,
        turns=[
            TranscriptTurn(
                speaker="INMATE",
                timestamp="[00:12]",
                text="Danger </script> line with separators \u2028 and \u2029.",
            )
        ],
        summary="Summary </script> with separators \u2028 and \u2029.",
        status=CallStatus.DONE,
    )


class DeliveryHtmlTests(unittest.TestCase):
    def test_review_identity_tracks_transcripts_but_not_summary_or_generation_date(self):
        call = _fixture_call()

        def identity(case_name="Case", date="January 1, 2026"):
            page = generate_index_html(build_call_views([call]), case_name=case_name, gen_date=date)
            return re.search(r'<meta name="jcs-review-id" content="([a-f0-9]{64})">', page).group(1)

        original = identity()
        call.summary = "A corrected summary"
        self.assertEqual(original, identity(date="February 1, 2026"))
        self.assertNotEqual(original, identity(case_name="Different case"))
        call.turns[0].text = "A different transcript"
        self.assertNotEqual(original, identity())

    def test_dump_script_safe_json_escapes_inline_script_breakers(self):
        payload = [{"text": "Danger </script> \u2028 \u2029"}]

        escaped = dump_script_safe_json(payload)

        self.assertIn("<\\/script>", escaped)
        self.assertIn("\\u2028", escaped)
        self.assertIn("\\u2029", escaped)
        self.assertEqual(json.loads(escaped.replace("<\\/", "</")), payload)

    def test_index_html_uses_script_safe_json_and_has_no_remote_script(self):
        html = generate_index_html(build_call_views([_fixture_call()]), case_name="Case </script>")

        self.assertIn("<\\/script>", html)
        self.assertIn("\\u2028", html)
        self.assertIn("\\u2029", html)
        self.assertNotIn("https://unpkg.com", html)
        self.assertNotIn("WaveSurfer", html)
        self.assertNotIn("<script src=", html)
        # Native <audio> behind the small shim; Web Audio cannot decode local files over file://.
        self.assertIn("progressInput.addEventListener('input', scrubToInputValue);", html)
        self.assertIn("const ws = {", html)

    def test_index_html_embeds_the_transcript_once_and_inlines_courier(self):
        html = generate_index_html(build_call_views([_fixture_call()]), case_name="Case")

        # The page:line entries are the only transcript copy: no compact turns array.
        self.assertIn('"lines": [', html)
        self.assertNotIn('"turns": [', html)
        self.assertEqual(html.count("Danger <\\/script> line with separators"), 2)  # text + rendered_text of one entry
        # The printed pages need Courier Prime; fonts are inlined once.
        self.assertIn('font-family: "Courier Prime";', html)
        self.assertEqual(html.count("data:font/ttf;base64,"), 2)  # regular + bold
        self.assertEqual(html.count("<style>"), 1)
        # Both views and the hash router live in the one page.
        self.assertIn('id="index"', html)
        self.assertIn('id="call"', html)
        self.assertIn("rewriteLegacyQuery", html)

    def test_theme_layers_compose_per_medium(self):
        screen = theme_css("screen")
        print_ = theme_css("print")

        # Same tokens and components in both media.
        for css in (screen, print_):
            self.assertIn("--signal:", css)
            self.assertIn(".rv--high", css)
            self.assertIn(".lbl {", css)
        # Fonts: embedded for the pages, file:// for the PDFs.
        self.assertIn("data:font/woff2;base64,", screen)
        self.assertNotIn("data:font/", print_)
        self.assertIn('url("file://', print_)
        # Medium sizes: print overrides paper to white and uses points.
        self.assertIn("--paper: #FFFFFF", print_)
        self.assertIn("--rv-dot: 6pt", print_)
        self.assertIn("--rv-dot: 8px", screen)
        with self.assertRaises(ValueError):
            theme_css("braille")

    def test_index_html_lowercases_relevance_marker(self):
        html = generate_index_html(build_call_views([_fixture_call()]))
        self.assertIn("rel.toLowerCase()", html)
        self.assertNotIn("rv--HIGH", html)


if __name__ == "__main__":
    unittest.main()
