import json
import unittest

from backend.delivery.call_view import build_call_views
from backend.delivery.search_html import generate_search_html
from backend.delivery.theme import theme_css
from backend.delivery.viewer import render_viewer
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
    def test_dump_script_safe_json_escapes_inline_script_breakers(self):
        payload = [{"text": "Danger </script> \u2028 \u2029"}]

        escaped = dump_script_safe_json(payload)

        self.assertIn("<\\/script>", escaped)
        self.assertIn("\\u2028", escaped)
        self.assertIn("\\u2029", escaped)
        self.assertEqual(json.loads(escaped.replace("<\\/", "</")), payload)

    def test_search_html_uses_script_safe_json_embedding(self):
        html = generate_search_html(build_call_views([_fixture_call()]), case_name="Case </script>")

        self.assertIn("<\\/script>", html)
        self.assertIn("\\u2028", html)
        self.assertIn("\\u2029", html)

    def test_viewer_html_uses_script_safe_json_and_has_no_remote_script(self):
        html = render_viewer(build_call_views([_fixture_call()]), case_name="Case </script>")

        self.assertIn("<\\/script>", html)
        self.assertIn("\\u2028", html)
        self.assertIn("\\u2029", html)
        self.assertNotIn("https://unpkg.com", html)
        self.assertNotIn("WaveSurfer", html)
        self.assertIn("progressInput.addEventListener('input', scrubToInputValue);", html)
        self.assertIn("const ws = {", html)

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

    def test_search_and_viewer_lowercase_relevance_marker(self):
        views = build_call_views([_fixture_call()])
        for html in (generate_search_html(views), render_viewer(views)):
            self.assertIn("rel.toLowerCase()", html)
            self.assertNotIn("rv--HIGH", html)


if __name__ == "__main__":
    unittest.main()
