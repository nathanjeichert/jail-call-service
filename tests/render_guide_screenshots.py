"""Regenerate the user guide's screenshots from the synthetic package.

    python tests/render_guide_screenshots.py

Builds the synthetic delivery (10 calls, no audio, pinned date) in a temp
directory, opens ``index.html`` in headless Chromium at the guide's fixed
viewport, and writes four PNGs into ``backend/delivery/assets/guide/``:

* ``index_view_screenshot.png``: the index view as it opens.
* ``calendar_view_screenshot.png`` / ``timeline_view_screenshot.png``: the
  index's two chart modes (``#calendar`` / ``#timeline``) at a shorter
  viewport, since they sit side by side on the guide's page.
* ``call_view_screenshot.png``: the call view with call 002 open at its
  first review cue (the ``#call=…&t=01:14`` deep link), so the cue is lit in
  the analysis rail and the cited line is highlighted on the printed page.

The case is "State v. Marcus Reeves", synthetic and safe to ship. Run this
after any visual change to index.html, then check ``guide.pdf`` pages 3 to
5 (``tests/test_guide_layout.py`` pins the page count).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_test_package import build_package  # noqa: E402

from backend.delivery.guide_pdf import SCREENSHOT_FILES, SCREENSHOTS_DIR  # noqa: E402

VIEWPORT = {"width": 1600, "height": 1040}
CHART_VIEWPORT = {"width": 1600, "height": 760}  # masthead, filter bar, and the chart
GEN_DATE = "January 15, 2026"
CALL_VIEW_HASH = "#call=002-20260313_123302_9095550226.mp3&t=01:14"


def render(out_dir: Path = SCREENSHOTS_DIR) -> list[Path]:
    written: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "REEVES_TEST_PACKAGE"
        build_package(package, 10, with_audio=False, gen_date=GEN_DATE)
        url = (package / "index.html").as_uri()

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)

            page.goto(url)
            page.wait_for_function("document.fonts.status === 'loaded'")
            expect(page.locator("#rows .row")).to_have_count(10)
            target = out_dir / SCREENSHOT_FILES["index_view"]
            page.screenshot(path=str(target))
            written.append(target)

            page.set_viewport_size(CHART_VIEWPORT)
            page.goto(url + "#calendar")
            expect(page.locator("#modeCalendar .month")).to_have_count(3)
            target = out_dir / SCREENSHOT_FILES["calendar_view"]
            page.screenshot(path=str(target))
            written.append(target)

            page.goto(url + "#timeline")
            expect(page.locator("#modeTimeline .tl-svg")).to_be_visible()
            target = out_dir / SCREENSHOT_FILES["timeline_view"]
            page.screenshot(path=str(target))
            written.append(target)
            page.set_viewport_size(VIEWPORT)

            page.goto(url + CALL_VIEW_HASH)
            expect(page.locator("#call")).to_be_visible()
            expect(page.locator("#summaryBody .side-cue.now")).to_have_count(1)
            # No audio in the package: wait for the failed load to settle and
            # drop its toast so the capture shows the view, not the error.
            expect(page.locator("#loadingOverlay")).to_be_hidden()
            page.evaluate("document.getElementById('toast').className = 'toast'")
            page.wait_for_timeout(350)  # the toast's fade transition
            target = out_dir / SCREENSHOT_FILES["call_view"]
            page.screenshot(path=str(target))
            written.append(target)
            browser.close()
    return written


if __name__ == "__main__":
    for path in render():
        print("wrote", path)
