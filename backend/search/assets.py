"""The embedding model the related-words table is built with, fetched once and pinned.

The model and its vocabulary come from Hugging Face into
:data:`backend.config.SEARCH_ASSETS_DIR`, verified by SHA-256, the way the
local transcription models live outside the repo. Nothing ships to the
delivery: the model runs only at build time (:mod:`related`), and the
lexicon vectors it produces are cached next to it. Nothing here is imported
at server start; the delivery stage calls :func:`ensure_search_assets` and
treats a failure as "keyword search only".
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config as cfg
from .embeddings import DEFAULT_EMBEDDING_MODEL, EMBEDDING_MODELS, EmbeddingSpec, RemoteFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchAssets:
    directory: Path
    spec: EmbeddingSpec

    @property
    def model_onnx(self) -> Path:
        return self.directory / self.spec.onnx.name

    @property
    def model_vocab(self) -> Path:
        return self.directory / self.spec.vocab.name

    @property
    def lexicon_cache(self) -> Path:
        """Vectors of the model's whole-word vocabulary, computed once per model."""
        return self.directory / f"{self.spec.id}.lexicon.npz"

    def present(self) -> bool:
        return self.model_onnx.is_file() and self.model_vocab.is_file()


def search_assets() -> SearchAssets:
    return SearchAssets(directory=Path(cfg.SEARCH_ASSETS_DIR), spec=EMBEDDING_MODELS[DEFAULT_EMBEDDING_MODEL])


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(remote: RemoteFile) -> bytes:
    logger.info("Downloading %s", remote.url)
    with urllib.request.urlopen(remote.url, timeout=120) as resp:  # noqa: S310 (pinned https URLs)
        data = resp.read()
    digest = _sha256(data)
    if digest != remote.sha256:
        raise RuntimeError(f"{remote.name}: SHA-256 {digest} does not match the pinned {remote.sha256}")
    return data


def _fetch_file(remote: RemoteFile, dest: Path) -> None:
    if dest.is_file() and _sha256(dest.read_bytes()) == remote.sha256:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_download(remote))


def ensure_search_assets() -> SearchAssets:
    """Return the assets, downloading whatever is missing or fails its hash."""
    assets = search_assets()
    _fetch_file(assets.spec.onnx, assets.model_onnx)
    _fetch_file(assets.spec.vocab, assets.model_vocab)
    return assets


if __name__ == "__main__":  # python -m backend.search.assets
    logging.basicConfig(level=logging.INFO)
    got = ensure_search_assets()
    print(f"search assets ready in {got.directory}")
