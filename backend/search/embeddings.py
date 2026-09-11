"""Embedding models for the semantic index.

One :class:`EmbeddingSpec` per model the page can run: the ONNX file and
vocabulary to ship, the dimensions, the token limit, and the prefixes the
model was trained with. The operator side embeds passages with the same
quantized ONNX file the page ships (through ``onnxruntime``), so query and
passage vectors come from identical weights.

:class:`WordPiece` is the BERT tokenizer those models need, written to
mirror the port in ``templates/index_search.js``; the parity test compares
the two on the synthetic corpus.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np


@dataclass(frozen=True)
class RemoteFile:
    url: str
    sha256: str
    name: str  # file name inside the assets directory


@dataclass(frozen=True)
class EmbeddingSpec:
    id: str
    label: str
    dims: int
    max_tokens: int
    query_prefix: str
    doc_prefix: str
    lowercase: bool
    onnx: RemoteFile
    vocab: RemoteFile
    license: str


_HF = "https://huggingface.co/{repo}/resolve/main/{path}"

EMBEDDING_MODELS: Dict[str, EmbeddingSpec] = {
    "minilm-l6": EmbeddingSpec(
        id="minilm-l6",
        label="all-MiniLM-L6-v2 (int8)",
        dims=384,
        max_tokens=256,
        query_prefix="",
        doc_prefix="",
        lowercase=True,
        onnx=RemoteFile(
            url=_HF.format(repo="Xenova/all-MiniLM-L6-v2", path="onnx/model_quantized.onnx"),
            sha256="afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1",
            name="minilm-l6.onnx",
        ),
        vocab=RemoteFile(
            url=_HF.format(repo="Xenova/all-MiniLM-L6-v2", path="vocab.txt"),
            sha256="07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
            name="minilm-l6.vocab.txt",
        ),
        license="Apache-2.0",
    ),
}
DEFAULT_EMBEDDING_MODEL = "minilm-l6"


# --- WordPiece (BERT) tokenizer; mirror of the JS port ---

def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    cat = unicodedata.category(ch)
    return cat[0] in ("P", "S")


class WordPiece:
    """``encode`` returns ``[CLS] tokens… [SEP]`` ids, truncated to ``max_tokens``."""

    def __init__(self, vocab: Sequence[str], lowercase: bool = True):
        self.ids = {tok: i for i, tok in enumerate(vocab)}
        self.lowercase = lowercase
        self.cls = self.ids["[CLS]"]
        self.sep = self.ids["[SEP]"]
        self.unk = self.ids["[UNK]"]
        self.pad = self.ids.get("[PAD]", 0)

    @classmethod
    def from_file(cls, path: Path, lowercase: bool = True) -> "WordPiece":
        vocab = path.read_text(encoding="utf-8").split("\n")
        if vocab and vocab[-1] == "":
            vocab.pop()
        return cls(vocab, lowercase)

    def basic_tokens(self, text: str) -> List[str]:
        if self.lowercase:
            text = text.lower()
        text = unicodedata.normalize("NFD", text)
        out: List[str] = []
        cur: List[str] = []
        for ch in text:
            if unicodedata.category(ch) == "Mn":
                continue
            if ch.isspace():
                if cur:
                    out.append("".join(cur))
                    cur = []
            elif _is_punctuation(ch):
                if cur:
                    out.append("".join(cur))
                    cur = []
                out.append(ch)
            else:
                cur.append(ch)
        if cur:
            out.append("".join(cur))
        return out

    def wordpiece(self, token: str) -> List[int]:
        if len(token) > 100:
            return [self.unk]
        ids: List[int] = []
        start = 0
        while start < len(token):
            end = len(token)
            found = -1
            while start < end:
                sub = ("##" if start > 0 else "") + token[start:end]
                if sub in self.ids:
                    found = self.ids[sub]
                    break
                end -= 1
            if found == -1:
                return [self.unk]
            ids.append(found)
            start = end
        return ids

    def encode(self, text: str, max_tokens: int) -> List[int]:
        ids = [self.cls]
        for tok in self.basic_tokens(text):
            ids.extend(self.wordpiece(tok))
        ids = ids[: max_tokens - 1]
        ids.append(self.sep)
        return ids


# --- inference ---

class Embedder:
    """Mean-pooled, L2-normalized sentence vectors from the ONNX model."""

    def __init__(self, spec: EmbeddingSpec, onnx_path: Path, vocab_path: Path):
        import onnxruntime as ort  # heavy import, only when embedding

        self.spec = spec
        self.tokenizer = WordPiece.from_file(vocab_path, spec.lowercase)
        self.session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        self._output = self.session.get_outputs()[0].name

    def encode(self, texts: Sequence[str], *, prefix: str = "", batch_size: int = 32) -> np.ndarray:
        """Vectors in input order; batches group similar lengths to keep padding low."""
        encoded = [self.tokenizer.encode(prefix + t, self.spec.max_tokens) for t in texts]
        order = sorted(range(len(encoded)), key=lambda i: len(encoded[i]))
        out = np.zeros((len(texts), self.spec.dims), dtype=np.float32)
        for start in range(0, len(order), batch_size):
            rows = order[start:start + batch_size]
            width = max(len(encoded[i]) for i in rows)
            input_ids = np.full((len(rows), width), self.tokenizer.pad, dtype=np.int64)
            mask = np.zeros((len(rows), width), dtype=np.int64)
            for row, i in enumerate(rows):
                input_ids[row, : len(encoded[i])] = encoded[i]
                mask[row, : len(encoded[i])] = 1
            hidden = self.session.run([self._output], {
                "input_ids": input_ids,
                "attention_mask": mask,
                "token_type_ids": np.zeros_like(input_ids),
            })[0]
            pooled = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
            out[rows] = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
        return out

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self.encode(texts, prefix=self.spec.doc_prefix)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text], prefix=self.spec.query_prefix)[0]


def quantize_int8(vectors: np.ndarray):
    """Per-vector absmax int8 quantization: ``(int8 matrix, float32 scales)``.

    Lossless for ranking in the benchmark; the page multiplies back by the
    scale when it scores.
    """
    scales = np.maximum(np.abs(vectors).max(axis=1), 1e-9) / 127.0
    q = np.clip(np.round(vectors / scales[:, None]), -127, 127).astype(np.int8)
    return q, scales.astype(np.float32)
