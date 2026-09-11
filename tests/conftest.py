"""Shared fixtures: the synthetic delivery packages and a headless browser.

Every package-driven test module (golden, delivery browser, search browser)
uses the same ten-call, no-audio package with a pinned date, so it is built
once per session. The meaning-search package is built too when the runtime
assets are present (``python -m backend.search.assets``); without them
``semantic_dir`` is None and the tests that need it skip.

Both packages build before Playwright starts, and the browser is scoped
to the module: ``build_package`` (and other tests) run ``asyncio.run``,
which cannot start while sync Playwright's loop is open.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_harness import open_page  # noqa: E402
from make_test_package import build_package  # noqa: E402

from backend.search.assets import search_assets  # noqa: E402

GEN_DATE = "January 15, 2026"


@pytest.fixture(scope="session")
def package(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("package") / "REEVES_TEST_PACKAGE"
    calls = build_package(out, 10, with_audio=False, gen_date=GEN_DATE)
    return {"dir": out, "url": (out / "index.html").as_uri(), "calls": calls}


@pytest.fixture(scope="session")
def package_dir(package) -> Path:
    return package["dir"]


@pytest.fixture(scope="session")
def index_url(package) -> str:
    return package["url"]


@pytest.fixture(scope="session")
def semantic_dir(tmp_path_factory) -> Path | None:
    if not search_assets().present():
        return None
    out = tmp_path_factory.mktemp("semantic") / "REEVES_TEST_PACKAGE"
    build_package(out, 10, with_audio=False, gen_date=GEN_DATE, semantic=True)
    return out


@pytest.fixture(scope="module")
def browser(package, semantic_dir):
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, package_dir):
    """The keyword-search package's index.html, asserting no console errors on teardown."""
    context, pg, problems = open_page(browser, package_dir / "index.html")
    yield pg
    context.close()
    assert problems == [], problems
