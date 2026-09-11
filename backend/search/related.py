"""Related words: the meaning layer of Smart search, one word at a time.

A query word expands to the corpus words that mean roughly the same thing
("cop" reaches "police", "detective", "sheriff"; "attorney" reaches
"lawyer"), computed here at delivery time with the embedding model and
shipped in ``index.js`` as a table the page reads. Nothing runs a model in
the browser, and every expansion is a real word in a real transcript that
the page marks, so a hit is never silent.

Why words and not passages: mean-pooled sentence vectors over ~100-word
passages cannot separate one mention of "police" from the rest of the
passage (measured on the demo corpus, the best passage for "cop" scored
0.34 while "police" passages scored 0.28 to 0.31, below unrelated ones),
but word-to-word cosines are clean (cop/police 0.68, attorney/lawyer 0.86,
noise under about 0.5). ``docs/search-plan.md`` section 7 has the numbers.

Two word lists feed the table: the corpus's own surface words (what the
transcripts and summaries say) and a fixed lexicon (the whole words of the
model's own vocabulary, ~20k common English words), so a query word the
corpus never uses still finds its neighbours. The lexicon's vectors are
computed once per model and cached beside the model file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np

from .embeddings import Embedder
from .lexical import LexicalIndex
from .tokenize import FILLER, STOPWORDS, stem, tokenize

logger = logging.getLogger(__name__)

RELATED_MIN = 0.62   # cosine a corpus word needs to count as related to a query word
RELATED_MAX = 6      # neighbours kept per query stem
MIN_WORD_LEN = 3

RelatedTable = Dict[str, List[Tuple[int, int]]]  # query stem -> [(vocab index, cosine * 100)]


def _is_word(token: str) -> bool:
    return (len(token) >= MIN_WORD_LEN and token.isalpha() and token.isascii()
            and token not in STOPWORDS and token not in FILLER)


def corpus_words(index: LexicalIndex) -> Dict[str, str]:
    """The index's surface words (as spoken, lowercased) mapped to their stem."""
    words: Dict[str, str] = {}
    for passage in index.passages:
        for text, _ in passage.pieces:
            for tok in tokenize(text):
                if tok not in words and _is_word(tok):
                    words[tok] = stem(tok)
    return words


def lexicon_words(vocab_path: Path) -> List[str]:
    """Whole, alphabetic words of the model vocabulary (no ``##`` pieces, no specials)."""
    seen = set()
    out: List[str] = []
    for line in vocab_path.read_text(encoding="utf-8").split("\n"):
        tok = line.strip()
        if tok and not tok.startswith("##") and not tok.startswith("[") and _is_word(tok) and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def lexicon_vectors(cache_path: Path, vocab_path: Path, embedder: Embedder) -> Tuple[List[str], np.ndarray]:
    """The lexicon and its vectors, computed once per model and cached at *cache_path*."""
    words = lexicon_words(vocab_path)
    if cache_path.is_file():
        data = np.load(cache_path, allow_pickle=False)
        cached = data["words"].tolist()
        if cached == words:
            return words, data["vectors"]
    vectors = embedder.encode(words)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache_path, words=np.asarray(words), vectors=vectors)
    return words, vectors


def _neighbours(query_words: Sequence[str], query_vectors: np.ndarray,
                corpus: Sequence[str], corpus_stems: Sequence[str], corpus_vectors: np.ndarray,
                vocab_pos: Dict[str, int], table: RelatedTable, *,
                minimum: float, chunk: int = 512) -> None:
    """Fold each query word's corpus neighbours (cosine >= *minimum*) into *table*."""
    stems = np.asarray(corpus_stems)
    for start in range(0, len(query_words), chunk):
        rows = range(start, min(start + chunk, len(query_words)))
        sims = query_vectors[rows] @ corpus_vectors.T
        for r, qi in enumerate(rows):
            key = stem(query_words[qi])
            hits = np.nonzero(sims[r] >= minimum)[0]
            if not len(hits):
                continue
            best: Dict[str, float] = {}
            for j in hits:
                other = stems[j]
                if other == key or other not in vocab_pos:
                    continue
                if sims[r, j] > best.get(other, 0.0):
                    best[other] = float(sims[r, j])
            if not best:
                continue
            slot = dict(table.get(key, ()))
            for other, cos in best.items():
                slot[vocab_pos[other]] = max(slot.get(vocab_pos[other], 0), int(round(cos * 100)))
            table[key] = list(slot.items())


def build_related(index: LexicalIndex, embedder: Embedder, lexicon: Tuple[Sequence[str], np.ndarray], *,
                  minimum: float = RELATED_MIN, limit: int = RELATED_MAX) -> RelatedTable:
    """The related-words table for *index*: ``{query stem: [(vocab index, cosine*100)…]}``,
    each list sorted strongest first and capped at *limit*."""
    words = corpus_words(index)
    if not words:
        return {}
    lex_words, lex_vectors = lexicon
    lex_pos = {w: i for i, w in enumerate(lex_words)}
    corpus = sorted(words)
    corpus_stems = [words[w] for w in corpus]
    # Corpus words already in the lexicon reuse its vectors; the rest are embedded now.
    corpus_vectors = np.zeros((len(corpus), lex_vectors.shape[1]), dtype=np.float32)
    fresh = [w for w in corpus if w not in lex_pos]
    if fresh:
        fresh_vectors = embedder.encode(fresh)
        fresh_pos = {w: i for i, w in enumerate(fresh)}
    for i, w in enumerate(corpus):
        corpus_vectors[i] = lex_vectors[lex_pos[w]] if w in lex_pos else fresh_vectors[fresh_pos[w]]

    table: RelatedTable = {}
    _neighbours(corpus, corpus_vectors, corpus, corpus_stems, corpus_vectors, index.vocab_pos, table, minimum=minimum)
    lex_only = [i for i, w in enumerate(lex_words) if w not in words]
    if lex_only:
        _neighbours([lex_words[i] for i in lex_only], lex_vectors[lex_only], corpus, corpus_stems, corpus_vectors,
                    index.vocab_pos, table, minimum=minimum)
    for key, pairs in table.items():
        pairs.sort(key=lambda p: (-p[1], index.vocab[p[0]]))
        del pairs[limit:]
    logger.info("Related words: %d query stems over %d corpus words (min cosine %.2f)", len(table), len(corpus), minimum)
    return {k: table[k] for k in sorted(table)}


def expansions(table: RelatedTable, vocab: Sequence[str], term: str) -> Iterable[Tuple[str, float]]:
    """``(related stem, weight)`` pairs for a query *term*; the reference scorer's view of the table."""
    for vi, cos in table.get(term, ()):
        yield vocab[vi], cos / 100.0
