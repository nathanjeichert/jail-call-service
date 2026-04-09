import unittest

from backend.viewer import render_viewer


class _DummyCall:
    def __init__(self):
        self.index = 0
        self.filename = "call.wav"
        self.mp3_path = "/tmp/call.mp3"
        self.duration_seconds = 12.0
        self.summary = "dummy"
        self.inmate_name = "Test Defendant"
        self.outside_number_fmt = "(555) 555-5555"
        self.call_datetime_str = "2026-04-09 10:00"
        self.facility = "Unit A"
        self.call_outcome = "Completed"
        self.status = "done"
        self.turns = []


class ViewerRenderTests(unittest.TestCase):
    def test_render_viewer_replaces_spaced_calls_json_placeholder(self):
        html = render_viewer([_DummyCall()], case_name="Viewer Test")
        self.assertNotIn("{{ CALLS_JSON }}", html)
        self.assertIn("const CALLS = [", html)
        self.assertIn("Viewer Test", html)
        self.assertIn("goToPage(activePage)", html)
        self.assertNotIn('class="line-ts"', html)


if __name__ == "__main__":
    unittest.main()
