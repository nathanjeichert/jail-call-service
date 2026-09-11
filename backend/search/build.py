"""Build the ``search/`` sidecars from the page's call payloads.

Input is the list of per-call dicts ``index_html.build_call_payload`` embeds
in the page (the same ``lines``, ``brief``, ``cues``… the page reads), so
the index points at exactly the data the page holds.

Files written under ``<output_dir>/search/`` (each a classic script that
assigns one global; ``file://`` allows no other data channel):

* ``index.js``   -> ``window.JCS_SEARCH``: passages and the BM25 index
* ``vectors.js`` -> ``window.JCS_VECTORS``: int8 passage vectors + scales
* ``model.js``   -> ``window.JCS_MODEL``: the ONNX model (base64) and vocab
* ``runtime.js`` -> ONNX Runtime Web's API, then ``window.JCS_ORT`` with
  the Emscripten glue as text and the wasm as base64

``index.js`` is always written; the other three only with the assets.
``model.js`` and ``runtime.js`` are the same for every delivery, so they are
rendered once into the assets directory and copied.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence

from .assets import ORT_API, ORT_GLUE, ORT_VERSION, ORT_WASM, SearchAssets
from .embeddings import Embedder, quantize_int8
from .lexical import LexicalIndex, b64
from .passages import Passage, build_passages, summary_passage

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


def _constant_sidecar(assets: SearchAssets, name: str, render) -> Path:
    """``sidecars/<name>`` in the assets directory, rendered on first use."""
    path = assets.sidecar_dir / name
    if not path.is_file():
        assets.sidecar_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(render(), encoding="utf-8")
    return path


def model_sidecar(assets: SearchAssets) -> Path:
    spec = assets.spec
    return _constant_sidecar(assets, f"model-{spec.id}.js", lambda: _script("JCS_MODEL", {
        # label and license travel with the shipped file as provenance
        "id": spec.id, "label": spec.label, "license": spec.license,
        "maxTokens": spec.max_tokens, "lowercase": spec.lowercase, "queryPrefix": spec.query_prefix,
        "vocab": assets.model_vocab.read_text(encoding="utf-8").rstrip("\n"),
        "onnx": b64(assets.model_onnx.read_bytes()),
    }))


def runtime_sidecar(assets: SearchAssets) -> Path:
    ort_dir = assets.ort_dir
    return _constant_sidecar(assets, f"runtime-{ORT_VERSION}.js", lambda: (
        (ort_dir / ORT_API).read_text(encoding="utf-8").rstrip("\n") + "\n"
        + _script("JCS_ORT", {
            "glue": (ort_dir / ORT_GLUE).read_text(encoding="utf-8"),
            "wasm": b64((ort_dir / ORT_WASM).read_bytes()),
        })
    ))


def write_search_sidecars(payloads: Sequence[dict], output_dir: Path, *,
                          assets: Optional[SearchAssets] = None) -> LexicalIndex:
    """Write ``search/`` under *output_dir*; the semantic files only with *assets*."""
    search_dir = Path(output_dir) / SEARCH_DIR
    search_dir.mkdir(parents=True, exist_ok=True)
    passages = build_passage_set(payloads)

    # Embedding needs only the passage texts and runs outside the GIL, so it
    # overlaps the postings build.
    with ThreadPoolExecutor(max_workers=1) as pool:
        vectors = None
        if assets is not None:
            embedder = Embedder(assets.spec, assets.model_onnx, assets.model_vocab)
            t0 = time.time()
            vectors = pool.submit(embedder.encode_passages, [p.text for p in passages])
        index = LexicalIndex.build(passages)
        fields = index.sidecar_fields()
        fields["semantic"] = assets is not None
        (search_dir / "index.js").write_text(_script("JCS_SEARCH", fields), encoding="utf-8")
        if assets is not None and vectors is not None:
            q, scales = quantize_int8(vectors.result())
            logger.info("Embedded %d passages with %s in %.1fs", len(passages), assets.spec.id, time.time() - t0)
            (search_dir / "vectors.js").write_text(_script("JCS_VECTORS", {
                "model": assets.spec.id, "n": int(q.shape[0]), "d": int(q.shape[1]), "q": b64(q), "s": b64(scales),
            }), encoding="utf-8")
            shutil.copyfile(model_sidecar(assets), search_dir / "model.js")
            shutil.copyfile(runtime_sidecar(assets), search_dir / "runtime.js")
    logger.info("Search index: %d passages, %d terms, model %s",
                len(passages), len(index.vocab), assets.spec.id if assets else "none")
    return index
