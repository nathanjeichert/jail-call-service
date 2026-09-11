"""Build the ``app-assets/`` sidecar from the page's call payloads.

Input is the list of per-call dicts ``index_html.build_call_payload`` embeds
in the page (the same ``lines``, ``brief``, ``cues``… the page reads), so
the index points at exactly the data the page holds.

One file is written, ``<output_dir>/app-assets/index.js``, a classic script
that assigns ``window.JCS_SEARCH`` (``file://`` allows no other data
channel): the passages, the BM25 postings, and, when the embedding model is
available, the related-words table (``related``) that gives Smart search
its meaning layer. Without the model the page runs keyword search only.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional, Sequence

from .assets import SearchAssets
from .embeddings import Embedder
from .lexical import LexicalIndex
from .passages import Passage, build_passages, summary_passage
from .related import build_related, lexicon_vectors

logger = logging.getLogger(__name__)

SEARCH_DIR = "app-assets"


def build_passage_set(payloads: Sequence[dict]) -> List[Passage]:
    """Every call's summary passage followed by its transcript passages."""
    passages: List[Passage] = []
    for call_index, payload in enumerate(payloads):
        summary = summary_passage(call_index, payload)
        if summary:
            passages.append(summary)
        passages.extend(build_passages(call_index, payload.get("lines") or []))
    return passages


def _script(global_name: str, fields: dict) -> str:
    return f"window.{global_name} = {json.dumps(fields, separators=(',', ':'))};\n"


def write_search_sidecars(payloads: Sequence[dict], output_dir: Path, *,
                          assets: Optional[SearchAssets] = None) -> LexicalIndex:
    """Write ``app-assets/index.js`` under *output_dir*; the related-words table only with *assets*."""
    search_dir = Path(output_dir) / SEARCH_DIR
    search_dir.mkdir(parents=True, exist_ok=True)
    passages = build_passage_set(payloads)
    index = LexicalIndex.build(passages)
    fields = index.sidecar_fields()
    if assets is not None:
        t0 = time.time()
        embedder = Embedder(assets.spec, assets.model_onnx, assets.model_vocab)
        lexicon = lexicon_vectors(assets.lexicon_cache, assets.model_vocab, embedder)
        fields["related"] = build_related(index, embedder, lexicon)
        logger.info("Related words for %d terms with %s in %.1fs", len(fields["related"]), assets.spec.id, time.time() - t0)
    (search_dir / "index.js").write_text(_script("JCS_SEARCH", fields), encoding="utf-8")
    logger.info("Search index: %d passages, %d terms, related words %s",
                len(passages), len(index.vocab), "on" if assets else "off")
    return index
