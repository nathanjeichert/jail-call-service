"""Headless-Chromium PDF renderer (singleton) built on Playwright.

Architecture
------------
This module exposes a *synchronous* facade (``render_pdf``) over Playwright's
*async* API. Playwright's sync API cannot be used here because callers invoke
``render_pdf`` from ``ThreadPoolExecutor`` workers inside an asyncio (FastAPI)
application, and the sync API refuses to run when an event loop is present on
the calling thread - and must never touch the caller's loop anyway.

Instead, a single dedicated daemon thread runs its own private asyncio event
loop for the lifetime of the process. All Playwright objects (the playwright
driver, the shared Chromium browser, contexts, pages) live exclusively on that
loop. ``render_pdf`` submits coroutines to it with
``asyncio.run_coroutine_threadsafe`` and blocks on the returned future, which
makes it safe to call concurrently from any thread without interacting with
any caller-side event loop.

One Chromium browser instance is shared across all renders; each render gets
a fresh, isolated browser context + page (contexts are cheap). Concurrency is
gated by an ``asyncio.Semaphore`` sized from the CPU count. If the browser
crashes mid-render (``TargetClosedError`` and friends) it is relaunched once
and the render retried a single time before the error propagates.

Paged.js support: with ``paged=True`` the vendored Paged.js polyfill
(``delivery/assets/vendor/paged.polyfill.js``) is injected after page load with
``window.PagedConfig = { auto: false }`` pre-set, then pagination is driven
explicitly via ``window.PagedPolyfill.preview()`` so we know exactly when
fragmentation has finished before printing.

Public API (frozen contract):
    render_pdf(html: str, *, paged: bool = False) -> bytes
    shutdown_renderer() -> None
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent / "assets" / "vendor"
_PAGED_POLYFILL_PATH = _VENDOR_DIR / "paged.polyfill.js"

#: Maximum simultaneous in-flight renders on the shared browser.
_MAX_CONCURRENT_RENDERS = min(8, max(2, (os.cpu_count() or 4) - 1))

#: Generous ceiling for Paged.js pagination of very large documents.
_PAGED_TIMEOUT_S = 120.0

#: Timeout for plain (non-paged) page setup steps.
_NAV_TIMEOUT_MS = 60_000

_INSTALL_REMEDY = "python -m playwright install chromium"

_PAGED_PREVIEW_JS = """
() => {
    return window.PagedPolyfill.preview().then(() => {
        window.__pagedDone = true;
        return document.querySelectorAll('.pagedjs_page').length;
    });
}
"""


class _Renderer:
    """Owns the dedicated event-loop thread and the shared Chromium browser.

    Every public method is called from arbitrary (non-loop) threads; every
    ``_async``-suffixed coroutine runs exclusively on the private loop.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="pdf-render-loop", daemon=True
        )
        self._playwright = None
        self._browser = None
        # Loop-agnostic at construction (Python 3.10+); first awaited on the
        # private loop, which is the only loop that ever touches them.
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RENDERS)
        self._browser_lock = asyncio.Lock()
        self._thread.start()

    # ── thread / loop plumbing ──────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def render(self, html: str, *, paged: bool = False) -> bytes:
        future = asyncio.run_coroutine_threadsafe(
            self._render_async(html, paged=paged), self._loop
        )
        return future.result()

    def shutdown(self) -> None:
        if self._loop.is_closed() or not self._thread.is_alive():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_async(), self._loop).result(
                timeout=15
            )
        except Exception:
            logger.warning("Error while closing Chromium renderer", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    # ── browser lifecycle (loop thread only) ────────────────────────────

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                logger.warning("Chromium browser disconnected; relaunching")
                self._browser = None
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch()
            except Exception as exc:
                message = str(exc)
                if "Executable doesn't exist" in message or "playwright install" in message:
                    raise RuntimeError(
                        "Chromium is not installed for Playwright. "
                        f"Run: {_INSTALL_REMEDY}"
                    ) from exc
                raise
            logger.info(
                "Launched shared Chromium browser (max %d concurrent renders)",
                _MAX_CONCURRENT_RENDERS,
            )
            return self._browser

    async def _close_async(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("Browser close failed", exc_info=True)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None

    # ── rendering (loop thread only) ────────────────────────────────────

    async def _render_async(self, html: str, *, paged: bool) -> bytes:
        await self._ensure_browser()
        async with self._semaphore:
            try:
                return await self._render_once(html, paged=paged)
            except RuntimeError:
                raise
            except Exception as exc:
                if not self._is_browser_dead_error(exc):
                    raise
                logger.warning(
                    "Render failed with a closed/crashed browser (%s); "
                    "restarting Chromium and retrying once",
                    exc,
                )
                await self._restart_browser()
                return await self._render_once(html, paged=paged)

    def _is_browser_dead_error(self, exc: Exception) -> bool:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TargetClosedError
        except ImportError:  # pragma: no cover - playwright is a hard dep
            return False
        if isinstance(exc, TargetClosedError):
            return True
        if self._browser is not None and not self._browser.is_connected():
            return True
        if isinstance(exc, PlaywrightError):
            message = str(exc)
            return any(
                marker in message
                for marker in (
                    "Target closed",
                    "browser has been closed",
                    "Browser closed",
                    "Connection closed",
                )
            )
        return False

    async def _restart_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("Closing dead browser failed", exc_info=True)
            self._browser = None
        await self._ensure_browser()

    async def _render_once(self, html: str, *, paged: bool) -> bytes:
        browser = await self._ensure_browser()
        tmp_path: Optional[Path] = None
        context = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".html",
                dir=tempfile.gettempdir(),
                delete=False,
            ) as tmp:
                tmp.write(html)
                tmp_path = Path(tmp.name)

            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(_NAV_TIMEOUT_MS)
            await page.goto(tmp_path.as_uri(), wait_until="load")
            await page.evaluate("() => document.fonts.ready")

            if paged:
                await self._paginate(page)

            return await page.pdf(prefer_css_page_size=True, print_background=True)
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    logger.debug("Context close failed", exc_info=True)
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.debug("Temp HTML cleanup failed for %s", tmp_path)

    async def _paginate(self, page) -> None:
        """Run Paged.js on an already-loaded page and wait for completion."""
        if not _PAGED_POLYFILL_PATH.is_file():
            raise RuntimeError(
                f"Vendored Paged.js polyfill not found at {_PAGED_POLYFILL_PATH}"
            )
        # The polyfill auto-runs on load unless PagedConfig.auto is false; the
        # flag must be in place before the script tag is evaluated.
        await page.evaluate("() => { window.PagedConfig = { auto: false }; }")
        await page.add_script_tag(path=str(_PAGED_POLYFILL_PATH))
        try:
            page_count = await asyncio.wait_for(
                page.evaluate(_PAGED_PREVIEW_JS), timeout=_PAGED_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Paged.js pagination did not finish within {_PAGED_TIMEOUT_S:.0f}s"
            ) from None
        # Belt and braces: ensure paginated output actually exists in the DOM.
        await page.wait_for_function(
            "window.__pagedDone === true"
            " && document.querySelectorAll('.pagedjs_page').length > 0",
            timeout=int(_PAGED_TIMEOUT_S * 1000),
        )
        logger.debug("Paged.js produced %s pages", page_count)


_lock = threading.Lock()
_renderer: Optional[_Renderer] = None


def _get_renderer() -> _Renderer:
    global _renderer
    renderer = _renderer
    if renderer is None:
        with _lock:
            renderer = _renderer
            if renderer is None:
                renderer = _renderer = _Renderer()
    return renderer


def render_pdf(html: str, *, paged: bool = False) -> bytes:
    """Render an HTML string to PDF bytes with headless Chromium.

    Safe to call concurrently from any thread (including ThreadPoolExecutor
    workers inside an asyncio app); never touches the caller's event loop.

    Args:
        html: Complete HTML document to render.
        paged: When True, run the vendored Paged.js polyfill so ``@page``
            margin boxes, page counters, and fragmentation are applied
            before printing.

    Returns:
        The PDF file contents.

    Raises:
        RuntimeError: If Chromium is not installed for Playwright
            (remedy: ``python -m playwright install chromium``).
        TimeoutError: If Paged.js pagination exceeds its time budget.
    """
    return _get_renderer().render(html, paged=paged)


def shutdown_renderer() -> None:
    """Shut down the shared browser, Playwright driver, and event loop.

    Idempotent; also registered with ``atexit``.
    """
    global _renderer
    with _lock:
        renderer = _renderer
        _renderer = None
    if renderer is not None:
        renderer.shutdown()
        logger.info("Chromium PDF renderer shut down")


atexit.register(shutdown_renderer)
