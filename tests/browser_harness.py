"""Helpers for driving index.html in headless Chromium from ``file://``."""

from __future__ import annotations

from pathlib import Path


def open_page(browser, index_html: Path):
    """Open the page in a fresh context; returns (context, page, problems).

    ``problems`` collects console errors and uncaught exceptions; the
    no-audio packages' missing MP3 is the one expected error and is ignored.
    The page is ready once fonts have loaded and the search index has either
    loaded or been found missing.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = context.new_page()
    problems: list[str] = []

    def on_console(msg):
        if msg.type != "error":
            return
        url = (msg.location or {}).get("url", "")
        if url.endswith(".mp3"):
            return
        problems.append(f"console.error: {msg.text} ({url})")

    pg.on("console", on_console)
    pg.on("pageerror", lambda err: problems.append(f"pageerror: {err}"))
    pg.goto(index_html.as_uri())
    pg.wait_for_function("document.fonts.status === 'loaded'")
    pg.wait_for_function("window.JCS.Search.state.lexical !== 'loading'")
    return context, pg, problems


def rows(page):
    return page.locator("#rows .row")


def search(page, query: str, settle_ms: int = 300):
    page.fill("#searchInput", query)
    page.wait_for_timeout(settle_ms)
