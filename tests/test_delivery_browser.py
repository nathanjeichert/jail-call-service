"""Browser tests for the delivery page (``index.html``) with Playwright.

Builds the synthetic package once (no audio, pinned date), opens
``index.html`` from a ``file://`` URL in headless Chromium, and drives both
views the way a reviewer does: search, filter, expand a row, open a cue,
go back, deep-link, present mode. Every test also asserts that no console
error or uncaught exception occurred.

The package has no MP3s, so the one expected console line is the audio
element's failed resource load; it is filtered out by its URL.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_test_package import build_package  # noqa: E402

DEEP_LINK_CALL = "002-20260313_123302_9095550226.mp3"


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("browser") / "REEVES_TEST_PACKAGE"
    calls = build_package(out, 10, with_audio=False, gen_date="January 15, 2026")
    return {"url": (out / "index.html").as_uri(), "calls": calls}


@pytest.fixture(scope="module")
def index_url(package) -> str:
    return package["url"]


@pytest.fixture(scope="module")
def browser(index_url):
    # The package builds first: build_package runs the delivery stage with
    # asyncio.run, which cannot start once sync Playwright's loop is running.
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, index_url):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = context.new_page()
    problems: list[str] = []

    def on_console(msg):
        if msg.type != "error":
            return
        if (msg.location or {}).get("url", "").endswith(".mp3"):
            return  # the no-audio package: the <audio> element's missing MP3
        problems.append(f"console.error: {msg.text}")

    pg.on("console", on_console)
    pg.on("pageerror", lambda err: problems.append(f"pageerror: {err}"))
    pg.goto(index_url)
    pg.wait_for_function("document.fonts.status === 'loaded'")
    yield pg
    context.close()
    assert problems == [], problems


def _rows(page):
    return page.locator("#rows .row")


def _expand_first_row(page):
    # Click the date cell: the summary cell holds excerpts that open the call.
    _rows(page).first.locator(".date").click()
    expect(page.locator("#rows .detail")).to_have_count(1)
    return page.locator("#rows .detail")


def _search(page, query: str):
    page.fill("#searchInput", query)


class TestIndexView:
    def test_renders_all_rows_and_stat_counts(self, page):
        expect(page.locator("#index")).to_be_visible()
        expect(page.locator("#call")).to_be_hidden()
        expect(_rows(page)).to_have_count(10)
        assert page.locator("#statCalls").inner_text() == "10"
        assert page.locator("#statNumbers").inner_text() == "4"
        assert page.locator("#statCues").inner_text() == "15"  # 3 HIGH x 3 cues + 3 MEDIUM x 2
        assert [page.locator(f"#{i}").inner_text() for i in ("tallyHigh", "tallyMed", "tallyLow")] == ["3", "3", "2"]
        assert page.title() == "State v. Marcus Reeves — Call Index"

    def test_query_filters_rows_and_highlights_matches(self, page):
        _search(page, "Darnell")
        expect(_rows(page)).to_have_count(4)
        expect(page.locator("#searchCount")).to_have_text("4 calls · 4 mentions")
        marks = page.locator("#rows mark")
        assert marks.count() >= 4
        assert {m.strip().lower() for m in marks.all_inner_texts()} == {"darnell"}
        _search(page, "no such phrase anywhere")
        expect(_rows(page)).to_have_count(0)
        expect(page.locator("#noResults")).to_be_visible()

    def test_relevance_chip_filters(self, page):
        page.click(".chip[data-rel=HIGH]")
        expect(_rows(page)).to_have_count(3)
        expect(_rows(page).locator(".rv--high")).to_have_count(3)
        page.click(".chip[data-rel=LOW]")
        expect(_rows(page)).to_have_count(2)
        page.click("#relAll")
        expect(_rows(page)).to_have_count(10)

    def test_expanding_a_row_shows_cues_with_page_line_cites(self, page):
        detail = _expand_first_row(page)
        cues = detail.locator(".cues tr")
        expect(cues).to_have_count(3)
        for cite in cues.locator("td.cite").all_inner_texts():
            assert re.match(r"^Tr\. \d+:\d+(-\d+:\d+)? · PDF p\.\d+$", cite), cite
        assert re.match(r"^\d\d:\d\d$", cues.first.locator("td.ts").inner_text())
        expect(detail.locator(".trans-body .turn")).to_have_count(11)


class TestNavigation:
    def test_clicking_a_cue_opens_the_call_view_at_that_time(self, page):
        detail = _expand_first_row(page)
        cue = detail.locator(".cues tr").first
        cue_time = cue.locator("td.ts").inner_text()      # "00:21"
        cite = cue.locator("td.cite").inner_text()         # "Tr. 1:4-1:6 · PDF p.1"
        first_line = int(re.match(r"Tr\. \d+:(\d+)", cite).group(1))
        cue.click()

        expect(page.locator("#call")).to_be_visible()
        expect(page.locator("#index")).to_be_hidden()
        assert page.evaluate("location.hash") == f"#call=001-20260303_093302_9095550144.mp3&t={cue_time}"
        assert page.title() == "State v. Marcus Reeves — Call Viewer"
        assert page.locator("#callPos").text_content() == "Call 001 of 10"
        active = page.locator("#call .trans-line.active")
        expect(active).to_have_count(1)
        assert int(active.locator(".line-num").inner_text()) == first_line
        assert page.locator("#timeCurrent").inner_text() == "0:21"
        assert page.evaluate("document.getElementById('audio').paused") is True

    def test_back_returns_to_the_index_with_the_query_applied(self, page):
        _search(page, "Darnell")
        expect(_rows(page)).to_have_count(4)
        page.click(".chip[data-rel=HIGH]")
        expect(_rows(page)).to_have_count(1)
        _expand_first_row(page).locator(".cues tr").first.click()
        expect(page.locator("#call")).to_be_visible()

        page.go_back()
        expect(page.locator("#index")).to_be_visible()
        expect(page.locator("#call")).to_be_hidden()
        assert page.evaluate("location.hash") in ("", "#index")
        assert page.input_value("#searchInput") == "Darnell"
        expect(page.locator(".chip[data-rel=HIGH]")).to_have_class(re.compile(r"\bactive\b"))
        expect(_rows(page)).to_have_count(1)
        expect(page.locator("#rows .detail")).to_have_count(1)
        expect(page.locator("#rows mark").first).to_have_text("Darnell")

        page.go_forward()
        expect(page.locator("#call")).to_be_visible()

    def test_viewer_button_and_back_control(self, page):
        _rows(page).nth(2).locator(".btn[data-action=viewer]").click()
        expect(page.locator("#call")).to_be_visible()
        assert page.evaluate("location.hash") == "#call=003-20260323_153302_7605550317.mp3"
        assert page.locator("#callPos").text_content() == "Call 003 of 10"
        page.click("#backToIndex")
        expect(page.locator("#index")).to_be_visible()
        assert page.evaluate("location.hash") == "#index"

    def test_deep_link_opens_paused_at_the_line(self, page, index_url):
        page.goto(f"{index_url}#call={DEEP_LINK_CALL}&t=01:14")
        expect(page.locator("#call")).to_be_visible()
        expect(page.locator("#index")).to_be_hidden()
        assert page.locator("#callPos").text_content() == "Call 002 of 10"
        assert page.locator("#timeCurrent").inner_text() == "1:14"
        assert page.evaluate("document.getElementById('audio').paused") is True
        active = page.locator("#call .trans-line.active")
        expect(active).to_have_count(1)
        assert active.evaluate("el => el.dataset.start") and float(active.evaluate("el => el.dataset.start")) >= 74
        # The cue at 01:14 is the current one in the analysis rail.
        expect(page.locator("#summaryBody .side-cue.now .t")).to_have_text("01:14")

    def test_legacy_query_form_redirects_to_the_hash_form(self, page, index_url):
        page.goto(f"{index_url}?call={DEEP_LINK_CALL}&t=01%3A14")
        expect(page.locator("#call")).to_be_visible()
        assert page.evaluate("location.search") == ""
        assert page.evaluate("location.hash") == f"#call={DEEP_LINK_CALL}&t=01:14"
        assert page.locator("#timeCurrent").inner_text() == "1:14"


class TestCallView:
    def test_present_mode_toggles(self, page, index_url):
        page.goto(f"{index_url}#call={DEEP_LINK_CALL}")
        expect(page.locator("#call")).to_be_visible()
        expect(page.locator("#railCalls")).to_be_visible()
        page.keyboard.press("p")
        expect(page.locator("body")).to_have_class(re.compile(r"\bpresent\b"))
        expect(page.locator("#railCalls")).to_be_hidden()
        expect(page.locator("#summaryPane")).to_be_hidden()
        expect(page.locator("#call .band")).to_be_hidden()
        assert page.locator("#presentBtn").text_content() == "Exit"
        page.keyboard.press("Escape")
        expect(page.locator("body")).not_to_have_class(re.compile(r"\bpresent\b"))
        expect(page.locator("#railCalls")).to_be_visible()
        # Leaving the call view in present mode (the band is hidden, so via
        # the hash) never leaks it into the index.
        page.keyboard.press("p")
        expect(page.locator("#call .band")).to_be_hidden()
        page.evaluate("location.hash = '#index'")
        expect(page.locator("#index")).to_be_visible()
        expect(page.locator("body")).not_to_have_class(re.compile(r"\bpresent\b"))

    def test_rails_collapse_and_words_are_seekable(self, page, index_url):
        page.goto(f"{index_url}#call={DEEP_LINK_CALL}")
        page.click("#callsToggle")
        page.click("#summaryCollapseBtn")
        expect(page.locator("#railCalls")).to_have_class(re.compile(r"\bcollapsed\b"))
        expect(page.locator("#summaryPane")).to_have_class(re.compile(r"\bcollapsed\b"))
        assert page.evaluate("[localStorage.getItem('jcs_calls_collapsed'), localStorage.getItem('jcs_summary_collapsed')]") == ["1", "1"]
        words = page.locator("#call .trans-page.active span.word-ts[data-ws][data-we]")
        assert words.count() > 50


class TestChartModes:
    """The index's calendar and timeline modes (index_charts.js)."""

    def test_mode_switch_is_offered_and_starts_on_the_list(self, page):
        expect(page.locator("#modeGroup")).to_be_visible()
        expect(page.locator("#modeGroup [data-mode]")).to_have_count(3)
        expect(page.locator("#modeGroup .chip.active")).to_have_text("List")
        expect(page.locator("#modeCalendar")).to_be_hidden()
        expect(page.locator("#modeTimeline")).to_be_hidden()

    def test_calendar_draws_the_span_and_a_day_click_narrows_the_list(self, page):
        page.click('#modeGroup [data-mode="calendar"]')
        expect(page).to_have_url(re.compile(r"#calendar$"))
        expect(page.locator("#modeList")).to_be_hidden()
        expect(page.locator("#modeCalendar .month")).to_have_count(3)      # Mar, Apr, May 2026
        expect(page.locator("#modeCalendar .day.has")).to_have_count(10)   # one call per day
        expect(page.locator("#modeCalendar .day.has .mk--high")).to_have_count(3)
        expect(page.locator("#modeCalendar .day.has .mk--medium")).to_have_count(3)
        expect(page.locator("#modeGroup .chip.active")).to_have_text("Calendar")

        day = page.locator('#modeCalendar .day[data-date="2026-03-13"]')
        day.hover()
        expect(page.locator(".chart-tip")).to_be_visible()
        expect(page.locator(".chart-tip .ct-head")).to_have_text("Friday, March 13, 2026")
        expect(page.locator(".chart-tip .ct-call")).to_have_count(1)

        day.click()
        expect(page).to_have_url(re.compile(r"#index$"))
        expect(page.locator("#modeList")).to_be_visible()
        expect(page.locator("#dateFrom")).to_have_value("2026-03-13")
        expect(page.locator("#dateTo")).to_have_value("2026-03-13")
        expect(_rows(page)).to_have_count(1)
        expect(_rows(page).first.locator(".date b")).to_have_text("Mar 13, 2026")

        # Back returns to the calendar, still narrowed to that day.
        page.go_back()
        expect(page).to_have_url(re.compile(r"#calendar$"))
        expect(page.locator("#modeCalendar")).to_be_visible()
        expect(page.locator("#modeCalendar .day.has")).to_have_count(1)

    def test_search_scopes_the_calendar(self, page):
        _search(page, "impound")
        expect(_rows(page)).not_to_have_count(10)  # the search input is debounced
        matched = _rows(page).count()
        assert 0 < matched < 10
        page.click('#modeGroup [data-mode="calendar"]')
        expect(page.locator("#modeCalendar .day.has")).to_have_count(matched)
        page.click("#clearFilters")
        expect(page.locator("#modeCalendar .day.has")).to_have_count(10)

    def test_timeline_buckets_agree_with_the_case_report(self, page, package):
        from backend.delivery.case_report import _build_timeline

        expected = _build_timeline(package["calls"])
        page.click('#modeGroup [data-mode="timeline"]')
        expect(page).to_have_url(re.compile(r"#timeline$"))
        expect(page.locator("#modeTimeline .tl-svg")).to_be_visible()
        built = page.evaluate("(() => { const b = JCS.Charts.buildBuckets(JCS.Data.dateExtent); return [b.unit, b.buckets.length]; })()")
        assert built == [expected["granularity"], expected["tick_count"]]
        expect(page.locator("#modeTimeline .legend")).to_contain_text("by " + expected["granularity"])
        expect(page.locator("#modeTimeline .tl-svg g.col")).to_have_count(sum(1 for b in expected["buckets"] if b["count"]))
        expect(page.locator("#modeTimeline .tl-svg g.m")).to_have_count(6)  # 3 High marks + 3 Medium marks

        # A High-lane mark narrows the list to that bucket and tier.
        page.locator("#modeTimeline .tl-svg g.m .hit").first.click()
        expect(page).to_have_url(re.compile(r"#index$"))
        expect(page.locator('.chip[data-rel="HIGH"]')).to_have_class(re.compile(r"\bactive\b"))
        first = expected["buckets"][0]
        expect(page.locator("#dateFrom")).to_have_value(first["start"].isoformat())
        expect(page.locator("#dateTo")).to_have_value(first["end"].isoformat())
        expect(_rows(page)).to_have_count(1)
        expect(_rows(page).first.locator(".rv")).to_have_text("High")

    def test_bucket_rules_and_undated_calls(self, page):
        units = page.evaluate("[60, 61, 392, 393, 1860, 1861].map(d => JCS.Charts.granularity(d))")
        assert units == ["day", "week", "week", "month", "month", "year"]
        assert page.evaluate("JCS.Charts.extent([{call_date: ''}, {call_date: '2026-01-05'}, {call_date: '2025-12-30 09:00'}])") == {
            "start": "2025-12-30", "end": "2026-01-05",
        }
        assert page.evaluate("JCS.Charts.extent([{call_date: ''}, {}])") is None
        note = page.evaluate(
            "(() => { const d = document.createElement('div');"
            " JCS.Charts.calendar(d, {calls: [{call_date: '', relevance: 'LOW'},"
            "   {call_date: '2026-03-03', datetime: '2026-03-03 09:33', relevance: 'HIGH'}],"
            "   extent: {start: '2026-03-03', end: '2026-03-03'}, maxPerDay: 1});"
            " return [d.querySelector('.chart-note').textContent, d.querySelectorAll('.day.has').length]; })()"
        )
        assert note == ["1 call without a date appears only in the list.", 1]
