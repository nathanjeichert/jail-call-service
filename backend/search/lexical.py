"""BM25 index over passages, serialized as the typed arrays the page decodes.

Layout of the sidecar object (``window.JCS_SEARCH``):

* ``vocab``: the sorted stems, newline-joined.
* ``termOff``: Uint32 byte offset of each term's postings (V + 1 entries).
* ``termDf``: Uint32 document frequency per term.
* ``postings``: one byte stream; per term, per passage in id order:
  varint(passage id delta), varint(tf), byte(speaker bits of the pieces the
  term occurs in).
* ``pCall`` / ``pFirst`` / ``pLast`` (Uint32), ``pStart`` (Float32),
  ``pSpk`` / ``pKind`` (Uint8), ``pLen`` (Uint16): the passage table.
* ``n``, ``avgdl``, ``k1``, ``b``: BM25 parameters.

:meth:`LexicalIndex.score` and :func:`call_scores` are the Python reference
of the page's lexical scoring: BM25 over every passage carrying any query
term, calls ranked by their best passage (MaxP), and in Smart mode the list
cut at :data:`RELATIVE_CUTOFF` of the top call's score (measured on the demo
corpus: same recall as no cutoff, a third of the list). Smart mode also
expands each query term with its related words (``related.py``), weighted
by cosine, one slot per query word so a passage takes its best expansion.
Exact mode needs every content term in the passage and verifies quoted
phrases against the stemmed token stream. ``templates/index_search.js``
implements the same rules; the tuning script and the tests use this one.
"""

from __future__ import annotations

import base64
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .passages import Passage
from .tokenize import index_terms, stem_tokens, tokenize

K1 = 1.2
B = 0.75
RELATIVE_CUTOFF = 0.3


def put_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def b64(data) -> str:
    """Base64 of bytes or a typed array (little-endian, as JS reads them)."""
    if isinstance(data, np.ndarray):
        data = np.ascontiguousarray(data).astype(data.dtype.newbyteorder("<"), copy=False).tobytes()
    return base64.b64encode(bytes(data)).decode("ascii")


@dataclass
class LexicalIndex:
    passages: List[Passage]
    vocab: List[str]                                   # sorted stems
    df: List[int]                                      # per term
    postings: bytes                                    # the varint stream
    term_offsets: List[int]                            # V + 1 byte offsets into ``postings``
    lengths: List[int]                                 # indexed terms per passage
    postings_map: Dict[str, Dict[int, List[int]]]      # term -> {passage: [tf, speaker bits]}
    vocab_pos: Dict[str, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.vocab_pos = {t: i for i, t in enumerate(self.vocab)}

    @property
    def avgdl(self) -> float:
        return sum(self.lengths) / len(self.lengths) if self.lengths else 0.0

    @classmethod
    def build(cls, passages: Sequence[Passage]) -> "LexicalIndex":
        postings: Dict[str, Dict[int, List[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        lengths: List[int] = []
        terms_of: Dict[str, List[str]] = {}  # a line's terms, once: overlapping passages share lines
        for pid, passage in enumerate(passages):
            count = 0
            for text, bit in passage.pieces:
                terms = terms_of.get(text)
                if terms is None:
                    terms = terms_of[text] = index_terms(text)
                for term in terms:
                    entry = postings[term][pid]
                    entry[0] += 1
                    entry[1] |= bit
                count += len(terms)
            lengths.append(count)
        vocab = sorted(postings)
        stream = bytearray()
        offsets = [0]
        df: List[int] = []
        for term in vocab:
            plist = postings[term]
            prev = 0
            for pid, (tf, bits) in plist.items():  # insertion order is passage order
                put_varint(stream, pid - prev)
                put_varint(stream, tf)
                stream.append(bits)
                prev = pid
            offsets.append(len(stream))
            df.append(len(plist))
        return cls(passages=list(passages), vocab=vocab, df=df, postings=bytes(stream),
                   term_offsets=offsets, lengths=lengths, postings_map=dict(postings))

    # --- serialization ---

    def sidecar_fields(self) -> dict:
        return {
            "n": len(self.passages),
            "avgdl": round(self.avgdl, 3),
            "k1": K1,
            "b": B,
            "vocab": "\n".join(self.vocab),
            "termOff": b64(np.asarray(self.term_offsets, dtype=np.uint32)),
            "termDf": b64(np.asarray(self.df, dtype=np.uint32)),
            "postings": b64(self.postings),
            "pCall": b64(np.asarray([p.call for p in self.passages], dtype=np.uint32)),
            "pFirst": b64(np.asarray([p.first_line for p in self.passages], dtype=np.uint32)),
            "pLast": b64(np.asarray([p.last_line for p in self.passages], dtype=np.uint32)),
            "pStart": b64(np.asarray([p.start for p in self.passages], dtype=np.float32)),
            "pSpk": b64(np.asarray([p.speakers for p in self.passages], dtype=np.uint8)),
            "pKind": b64(np.asarray([p.kind for p in self.passages], dtype=np.uint8)),
            "pLen": b64(np.asarray(self.lengths, dtype=np.uint16)),
        }

    # --- reference scorer (mirrors the page) ---

    def idf(self, term: str) -> float:
        i = self.vocab_pos.get(term, -1)
        if i < 0:
            return 0.0
        n = len(self.passages)
        return math.log(1.0 + (n - self.df[i] + 0.5) / (self.df[i] + 0.5))

    def score(self, query: str, *, exact: bool = False, speakers: int = 0,
              related: Optional[Dict[str, List[Tuple[int, int]]]] = None) -> Dict[int, dict]:
        """Passage scores for a query.

        Returns ``{passage id: {"score", "matched": set of terms, "related":
        set of terms}}``: ``matched`` are the query terms the passage holds
        itself, ``related`` those it holds only through a related word. Smart
        mode scores every passage carrying any content term (or, with a
        *related* table, a related word of one, weighted by cosine); exact
        mode needs every term itself. Quoted phrases are verified against the
        stemmed token stream in both. ``speakers`` (bit mask) restricts a
        term's contribution to the pieces those speakers said.
        """
        phrases, rest = _split_phrases(query)
        terms = list(dict.fromkeys(index_terms(rest) + [t for ph in phrases for t in index_terms(ph)]))
        if not terms:
            return {}
        phrase_terms = {t for ph in phrases for t in index_terms(ph)}
        needed = len(terms) if exact else 1
        acc: Dict[int, dict] = {}
        avgdl = self.avgdl
        for term in terms:
            expansions = [(term, 1.0, False)]
            if related and not exact and term not in phrase_terms:
                expansions += [(self.vocab[vi], cos / 100.0, True) for vi, cos in related.get(term, ())]
            best: Dict[int, Tuple[float, bool]] = {}   # passage -> (score, via a related word)
            cap = self.idf(term) if term in self.vocab_pos else float("inf")   # a related word never outweighs the typed one
            for word, weight, via_related in expansions:
                idf = (min(self.idf(word), cap) if via_related else self.idf(word)) * weight
                if idf <= 0:
                    continue
                for pid, (tf, bits) in self.postings_map[word].items():
                    if speakers and not (bits & speakers):
                        continue
                    dl = self.lengths[pid]
                    s = idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl))
                    cur = best.get(pid)
                    if cur is None:
                        best[pid] = (s, via_related)
                    elif s > cur[0]:
                        best[pid] = (s, via_related and cur[1])  # a word the passage holds itself stays exact
            for pid, (s, via_related) in best.items():
                slot = acc.setdefault(pid, {"score": 0.0, "matched": set(), "related": set()})
                slot["score"] += s
                slot["related" if via_related else "matched"].add(term)
        hits = {pid: s for pid, s in acc.items() if len(s["matched"]) + len(s["related"]) >= needed}
        if phrases:
            hits = {pid: s for pid, s in hits.items()
                    if phrase_terms <= s["matched"] and self._has_phrases(pid, phrases)}
        return hits

    def _has_phrases(self, pid: int, phrases: List[str]) -> bool:
        stream = stem_tokens(self.passages[pid].text)
        for phrase in phrases:
            needle = stem_tokens(phrase)
            if not needle:
                continue
            n = len(needle)
            if not any(stream[i:i + n] == needle for i in range(len(stream) - n + 1)):
                return False
        return True


def _split_phrases(query: str) -> Tuple[List[str], str]:
    """Quoted phrases and the rest of the query."""
    phrases: List[str] = []
    rest: List[str] = []
    parts = query.replace("“", '"').replace("”", '"').split('"')
    for i, part in enumerate(parts):
        if i % 2 == 1 and len(tokenize(part)) >= 2:
            phrases.append(part)
        else:
            rest.append(part)
    return phrases, " ".join(rest)


def call_scores(passage_scores: Dict[int, dict], passages: Sequence[Passage], *,
                cutoff: float = RELATIVE_CUTOFF) -> Dict[int, float]:
    """MaxP: a call scores as its best passage; calls under ``cutoff`` of the
    top call are dropped (pass 0 to keep every call)."""
    best: Dict[int, float] = {}
    for pid, s in passage_scores.items():
        call = passages[pid].call
        if s["score"] > best.get(call, 0.0):
            best[call] = s["score"]
    if not best:
        return best
    floor = cutoff * max(best.values())
    return {call: score for call, score in best.items() if score >= floor}
