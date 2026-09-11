"""The search package on its own: tokenizer, stemmer, passages, BM25 index.

The JavaScript port is checked against these same functions in
``test_search_browser.py``; this file pins the Python side.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from backend.search import lexical
from backend.search.build import build_passage_set, write_search_sidecars
from backend.search.embeddings import WordPiece, quantize_int8
from backend.search.lexical import LexicalIndex
from backend.search.passages import (
    KIND_SUMMARY,
    SPEAKER_ANY,
    SPEAKER_DEFENDANT,
    SPEAKER_OUTSIDE,
    build_passages,
    summary_text,
)
from backend.search.tokenize import index_terms, stem, stem_tokens, tokenize

# Porter's published examples (Porter, 1980), a fixed regression list.
PORTER_PAIRS = """caresses caress ponies poni ties ti caress caress cats cat feed feed agreed agre plastered plaster
motoring motor sing sing conflated conflat troubled troubl sized size hopping hop tanned tan falling fall
hissing hiss fizzed fizz failing fail filing file happy happi sky sky relational relat conditional condit
rational ration valenci valenc hesitanci hesit digitizer digit conformabli conform radicalli radic
differentli differ vileli vile analogousli analog vietnamization vietnam predication predic operator oper
feudalism feudal decisiveness decis hopefulness hope callousness callous formaliti formal sensitiviti sensit
sensibiliti sensibl triplicate triplic formative form formalize formal electriciti electr electrical electr
hopeful hope goodness good revival reviv allowance allow inference infer airliner airlin gyroscopic gyroscop
adjustable adjust defensible defens irritant irrit replacement replac adjustment adjust dependent depend
adoption adopt homologou homolog communism commun activate activ angulariti angular homologous homolog
effective effect bowdlerize bowdler probate probat rate rate cease ceas controll control roll roll
generalizations gener oscillators oscil moving move moved move gun gun guns gun witness wit witnesses wit
lawyer lawyer lawyers lawyer talking talk talked talk""".split()


class TestTokenizer:
    def test_porter_examples(self):
        mismatches = [(a, b, stem(a)) for a, b in zip(PORTER_PAIRS[::2], PORTER_PAIRS[1::2]) if stem(a) != b]
        assert mismatches == []

    def test_contractions_phones_and_case(self):
        text = "Call me at (530) 555-0192 or 555-0192, I'm gonna tell Mike's brother he can't. It's $200 cash, um, y'all"
        assert tokenize(text) == [
            "call", "me", "at", "5305550192", "or", "5550192", "i", "am", "going", "to", "tell", "mike",
            "brother", "he", "can", "not", "it", "200", "cash", "um", "you", "all",
        ]

    def test_index_terms_drop_stopwords_and_add_digit_tails(self):
        assert index_terms("did he tell anyone to get rid of the gun") == ["tell", "anyon", "rid", "gun"]
        assert index_terms("(530) 555-0192") == ["5305550192", "5550192", "0192"]
        assert index_terms("555-0192") == ["5550192", "0192"]
        assert index_terms("um, uh, the") == []

    def test_stem_tokens_keep_stopwords_for_phrases(self):
        assert stem_tokens("we were at Mike's all night") == ["we", "were", "at", "mike", "all", "night"]

    def test_accents_and_curly_apostrophes_fold(self):
        assert tokenize("José’s café") == ["jose", "cafe"]


def _lines(*turns):
    """Line entries for a scripted call: one line per (speaker, text)."""
    return [{"turn_index": i, "speaker": speaker, "text": text, "start": float(i * 10)}
            for i, (speaker, text) in enumerate(turns)]


class TestPassages:
    def test_windows_cut_on_turns_and_overlap(self):
        lines = _lines(*[("INMATE" if i % 2 == 0 else "OUTSIDE PARTY", " ".join(["word"] * 20)) for i in range(20)])
        passages = build_passages(3, lines)
        assert all(p.call == 3 for p in passages)
        assert passages[0].first_line == 0 and len(passages[0].text.split()) == 100
        assert passages[1].first_line >= 2 and passages[1].first_line < passages[0].last_line  # overlap
        assert passages[-1].last_line == len(lines) - 1
        assert all(p.speakers == SPEAKER_DEFENDANT | SPEAKER_OUTSIDE for p in passages)
        assert passages[0].start == 0.0 and passages[1].start == passages[1].first_line * 10.0
        assert passages[0].pieces[0] == (lines[0]["text"], SPEAKER_DEFENDANT)
        assert passages[0].pieces[1][1] == SPEAKER_OUTSIDE
        assert passages[0].text == " ".join(lines[i]["text"] for i in range(passages[0].last_line + 1))

    def test_short_call_is_one_passage(self):
        passages = build_passages(0, _lines(("INMATE", "hey"), ("OUTSIDE PARTY", "hi there")))
        assert len(passages) == 1 and passages[0].text == "hey hi there"

    def test_summary_text_joins_payload_fields(self):
        payload = {"outside": "(530) 555-0192", "inmate": "John", "identity": "girlfriend", "brief": "Talks.",
                   "cues": [{"note": "Asks about the car", "quote": "did they take it"}]}
        assert summary_text(payload) == "(530) 555-0192 · John · girlfriend · Talks. · Asks about the car · did they take it"
        summary = build_passage_set([payload])[0]
        assert summary.kind == KIND_SUMMARY and summary.speakers == SPEAKER_ANY and summary.text == summary_text(payload)


def _payloads():
    return [
        {"lines": _lines(("INMATE", "Did you talk to the lawyer about the car"), ("OUTSIDE PARTY", "The lawyer calls tomorrow")),
         "brief": "Lawyer logistics.", "outside": "(530) 555-0192", "cues": []},
        {"lines": _lines(("INMATE", "Tell Darnell to keep quiet"), ("OUTSIDE PARTY", "He was at Mike's all night")),
         "brief": "", "outside": "(916) 555-0117", "cues": [{"note": "Witness instruction", "quote": "keep quiet"}]},
        {"lines": _lines(("INMATE", "Happy birthday Lily"), ("OUTSIDE PARTY", "She loved the bike")), "brief": "", "cues": []},
    ]


class TestLexicalIndex:
    def test_build_scores_and_serializes(self):
        index = LexicalIndex.build(build_passage_set(_payloads()))
        # summary passages come first per call, then transcript passages
        kinds = [p.kind for p in index.passages]
        assert kinds.count(KIND_SUMMARY) == 2  # the third call has no summary text
        assert "lawyer" in index.vocab and "darnel" in index.vocab and "0192" in index.vocab
        hits = index.score("lawyer")
        assert {index.passages[pid].call for pid in hits} == {0}
        # phone number in any format finds the call through its summary passage
        for q in ("555-0192", "(530) 555-0192", "5305550192", "0192"):
            assert {index.passages[pid].call for pid in index.score(q)} == {0}, q
        # phrases are verified on the stemmed stream, stopwords included
        assert {index.passages[pid].call for pid in index.score('"at Mike\'s all night"')} == {1}
        assert index.score('"all night at Mike\'s"') == {}
        # exact mode needs every content term
        assert index.score("lawyer bike", exact=True) == {}
        assert {index.passages[pid].call for pid in index.score("lawyer bike")} == {0, 2}
        # speaker restriction uses the line the term is on; a summary passage is said by everyone
        assert {index.passages[pid].call for pid in index.score("lawyer", speakers=SPEAKER_OUTSIDE)} == {0}
        assert index.score("darnell", speakers=SPEAKER_OUTSIDE) == {}
        assert {index.passages[pid].kind for pid in index.score("quiet", speakers=SPEAKER_OUTSIDE)} == {KIND_SUMMARY}

        fields = index.sidecar_fields()
        assert fields["n"] == len(index.passages)
        off = np.frombuffer(base64.b64decode(fields["termOff"]), dtype="<u4")
        assert len(off) == len(index.vocab) + 1 and off[-1] == len(index.postings)
        assert np.frombuffer(base64.b64decode(fields["pKind"]), dtype="u1").tolist() == kinds

    def test_relative_cutoff_keeps_the_strong_calls(self):
        scores = {0: {"score": 10.0}, 1: {"score": 4.0}, 2: {"score": 1.0}}
        passages = [SimpleNamespace(call=i) for i in range(3)]
        assert set(lexical.call_scores(scores, passages)) == {0, 1}
        assert set(lexical.call_scores(scores, passages, cutoff=0)) == {0, 1, 2}

    def test_write_sidecars_keyword_only(self, tmp_path: Path):
        index = write_search_sidecars(_payloads(), tmp_path)
        assert sorted(p.name for p in (tmp_path / "app-assets").iterdir()) == ["index.js"]
        text = (tmp_path / "app-assets" / "index.js").read_text(encoding="utf-8")
        assert text.startswith("window.JCS_SEARCH = {") and text.rstrip().endswith("};")
        fields = json.loads(text[len("window.JCS_SEARCH = "):].rstrip().rstrip(";"))
        assert fields["semantic"] is False and fields["n"] == len(index.passages)


class TestEmbeddings:
    def test_wordpiece_matches_bert_rules(self):
        vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "did", "he", "talk", "about", "the", "gun", "##s", "un", "##know", "##n", "."]
        tok = WordPiece(vocab)
        assert tok.encode("Did he talk about the guns.", 32) == [2, 4, 5, 6, 7, 8, 9, 10, 14, 3]
        assert tok.encode("unknown zzz", 32) == [2, 11, 12, 13, 1, 3]
        assert tok.encode("did he talk about the gun", 4) == [2, 4, 5, 3]  # truncation keeps [SEP]

    def test_int8_quantization_preserves_ranking(self):
        rng = np.random.default_rng(0)
        v = rng.normal(size=(50, 16)).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        q, s = quantize_int8(v)
        back = q.astype(np.float32) * s[:, None]
        assert q.dtype == np.int8 and s.dtype == np.float32
        assert np.allclose((back * v).sum(axis=1), 1.0, atol=0.01)
