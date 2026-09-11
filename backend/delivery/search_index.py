"""The delivery's ``search/`` folder: the page's lexical and semantic indexes.

Adapter between the delivery stage and :mod:`backend.search`: turns the
call views into the payload dicts the page embeds (the same
``build_call_payload`` output, so the index and the page agree on every
line), fetches the runtime assets when meaning search is wanted, and writes
the sidecars. The lexical index always ships; the semantic layer is skipped,
with a warning, when the assets cannot be fetched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from ..search.assets import ensure_search_assets
from ..search.build import write_search_sidecars
from .call_view import CallView
from .index_html import build_call_payload

logger = logging.getLogger(__name__)


def generate_search_index(views: Sequence[CallView], output_dir: str, *, semantic: bool = True) -> None:
    """Write ``<output_dir>/search/`` for the views."""
    assets = None
    if semantic:
        try:
            assets = ensure_search_assets()
        except Exception as e:  # network, hash mismatch: the page degrades to keyword search
            logger.warning("Meaning search assets unavailable, shipping keyword search only: %s", e)
    write_search_sidecars([build_call_payload(v) for v in views], Path(output_dir), assets=assets)
