"""The page's search in headless Chromium from ``file://``.

Uses the shared packages from ``conftest.py``: the keyword-only one every
build ships, and (when the runtime assets are present locally; ``python -m
backend.search.assets``) one with the meaning layer. Covers the JavaScript
tokenizer's parity with ``backend.search.tokenize`` over every transcript
line, Smart versus Exact words only, phrases, phone formats, typo tolerance,
the Said-by chips, the match tags on evidence, recent searches, the
missing-index fallback, and for the meaning layer: load state, WordPiece and
vector parity with the Python embedder, and the labeled similar-meaning
fallback.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_harness import open_page  # noqa: E402
from browser_harness import rows as _rows  # noqa: E402
from browser_harness import search as _search  # noqa: E402
from test_search_index import PORTER_PAIRS  # noqa: E402

from backend.search.assets import search_assets  # noqa: E402
from backend.search.embeddings import Embedder, WordPiece  # noqa: E402
from backend.search.passages import summary_text  # noqa: E402
from backend.search.tokenize import index_terms, stem, stem_tokens  # noqa: E402

ASSETS = search_assets()


def _tags(page):
    return [t.strip().lower() for t in page.locator("#rows .row .tag").all_inner_texts()]


class TestParity:
    def test_javascript_tokenizer_matches_python(self, page):
        texts = page.evaluate("window.JCS.Data.calls.flatMap(c => c.lines.map(l => l.text))")
        texts += [
            "Call me at (530) 555-0192 or 555-0192, I'm gonna tell Mike's brother he can't. It's $200 cash, um, y'all",
            "José’s café; the 49ers won't play 'til 2026-01-12", "Running, runs, RAN — hopefully agreed!",
            "", "   ", "'em 'cause dunno lemme",
        ] + PORTER_PAIRS[::2]
        js = page.evaluate("ts => ts.map(t => [window.JCS.Search.indexTerms(t), window.JCS.Search.stemTokens(t)])", texts)
        for text, (js_terms, js_stream) in zip(texts, js):
            assert js_terms == index_terms(text), text
            assert js_stream == stem_tokens(text), text
        assert page.evaluate("ws => ws.map(window.JCS.Search.stem)", PORTER_PAIRS[::2]) == [stem(w) for w in PORTER_PAIRS[::2]]

    def test_summary_passage_text_matches_python(self, page):
        calls = page.evaluate("window.JCS.Data.calls.map(c => ({call: c, text: window.JCS.Search.summaryText(c)}))")
        for item in calls:
            assert item["text"] == summary_text(item["call"])


class TestKeywordSearch:
    def test_index_loads_without_the_meaning_layer(self, page):
        assert page.evaluate("window.JCS.Search.state") == {
            "lexical": "ready", "semantic": "none", "semanticReason": "", "semanticDetail": "",
        }
        expect(page.locator("#searchHint")).to_be_hidden()

    def test_stems_and_partial_matches_are_tagged(self, page):
        _search(page, "lawyer calls him first")
        expect(page.locator("#searchCount")).to_contain_text("best match first")
        tags = _tags(page)
        assert "exact words" in tags and any(t.endswith("of 3 words") for t in tags)
        first = _rows(page).first
        # the top evidence is the line with the phrase, not the passage's first line
        expect(first.locator(".excerpt").first).to_contain_text("until the lawyer calls him first")
        marks = {m.strip().lower() for m in first.locator(".excerpt").first.locator("mark").all_inner_texts()}
        assert marks == {"lawyer", "calls", "first"}

    def test_exact_words_only_needs_every_word(self, page):
        _search(page, "impound birthday")
        smart_rows = _rows(page).count()
        assert smart_rows > 0
        page.click('#searchMode .chip[data-smart="0"]')
        page.wait_for_timeout(250)
        expect(_rows(page)).to_have_count(0)
        expect(page.locator("#noResults")).to_be_visible()
        page.click('#searchMode .chip[data-smart="1"]')
        page.wait_for_timeout(250)
        expect(_rows(page)).to_have_count(smart_rows)

    def test_quoted_phrase_and_word_order(self, page):
        _search(page, '"tell him not to say"')
        assert _rows(page).count() > 0 and set(_tags(page)) == {"exact phrase"}
        _search(page, '"say not to him tell"')
        expect(_rows(page)).to_have_count(0)

    def test_phone_number_in_any_format(self, page):
        counts = set()
        for q in ("(909) 555-0144", "909-555-0144", "9095550144", "555-0144", "0144"):
            _search(page, q)
            counts.add(_rows(page).count())
            expect(_rows(page).first.locator(".party")).to_contain_text("(909) 555-0144")
        assert len(counts) == 1

    def test_typo_tolerance_marks_the_real_word(self, page):
        _search(page, "Darnall")
        assert _rows(page).count() > 0
        assert {m.strip() for m in page.locator("#rows mark").all_inner_texts()} == {"Darnell"}
        page.click('#searchMode .chip[data-smart="0"]')
        page.wait_for_timeout(250)
        expect(_rows(page)).to_have_count(0)

    def test_said_by_chips_restrict_the_speaker(self, page):
        # Only the defendant says "Darnell" in the transcripts; the summaries
        # and cues mention him too, and a summary counts for every speaker.
        _search(page, "Darnell")
        both = _rows(page).count()
        page.click('#saidBy .chip[data-spk="2"]')
        page.wait_for_timeout(250)
        assert set(page.locator("#rows .excerpt .ex-t").all_inner_texts()) == {"SUMMARY"}
        page.click('#saidBy .chip[data-spk="1"]')
        page.wait_for_timeout(250)
        expect(_rows(page)).to_have_count(both)
        assert any(t != "SUMMARY" for t in page.locator("#rows .excerpt .ex-t").all_inner_texts())
        # the chips are inert without a query
        _search(page, "")
        assert page.locator("#ctl").get_attribute("class") == "ctl no-query"

    def test_ranking_yields_to_a_column_heading(self, page):
        _search(page, "impound")
        expect(page.locator("#searchCount")).to_contain_text("best match first")
        assert page.locator("#cols .sorted").count() == 0
        page.click('#cols [data-sort="call_sort"]')
        expect(page.locator("#searchCount")).not_to_contain_text("best match first")
        assert page.locator("#cols .sorted").count() == 1

    def test_filters_with_counts_and_clear(self, page):
        options = page.locator("#phoneFilter option").all_inner_texts()
        assert any("· 1 call" in o or "calls" in o for o in options[1:])
        assert page.locator("#outcomeFilter option").count() > 1
        page.select_option("#lengthFilter", "u1")
        page.wait_for_timeout(200)
        short = _rows(page).count()
        page.click("#clearFilters")
        page.wait_for_timeout(200)
        assert _rows(page).count() > short
        assert page.input_value("#lengthFilter") == ""

    def test_recent_searches_are_offered(self, page):
        page.fill("#searchInput", "impound inventory")
        page.press("#searchInput", "Enter")
        page.locator("#searchInput").blur()
        page.wait_for_timeout(200)
        assert page.locator("#recentSearches option").first.get_attribute("value") == "impound inventory"

    def test_detail_and_call_view_share_the_matcher(self, page):
        _search(page, "impounded")
        _rows(page).first.locator(".date").click()
        expect(page.locator("#rows .detail .turn.is-match").first).to_be_visible()
        expect(page.locator("#navLabel")).to_contain_text("MATCH")
        _rows(page).first.locator(".excerpt").first.click()
        expect(page.locator("#call")).to_be_visible()
        page.fill("#transSearch", "impounded")
        page.wait_for_timeout(300)
        assert page.locator("#call .trans-line mark").count() >= 1


class TestMissingIndex:
    def test_scan_fallback_and_notice(self, browser, package_dir, tmp_path):
        broken = tmp_path / "broken"
        shutil.copytree(package_dir, broken)
        shutil.rmtree(broken / "search")
        context, pg, problems = open_page(browser, broken / "index.html")
        try:
            assert pg.evaluate("window.JCS.Search.state.lexical") == "missing"
            _search(pg, "Darnell")
            expect(_rows(pg)).to_have_count(4)
            assert set(_tags(pg)) == {"exact words"}
            expect(_rows(pg).first.locator("mark").first).to_have_text("Darnell")
            expect(pg.locator("#searchHint")).to_contain_text("extract the whole zip")
        finally:
            context.close()
        # the failed sidecar load is reported by the browser as a console error; nothing else may be
        assert all("search/index.js" in p for p in problems), problems


class TestMeaningSearch:
    @pytest.fixture
    def spage(self, browser, semantic_dir):
        if semantic_dir is None:
            pytest.skip("search runtime assets not downloaded")
        context, pg, problems = open_page(browser, semantic_dir / "index.html")
        pg.wait_for_function("['ready','unavailable'].includes(window.JCS.Search.state.semantic)", timeout=60000)
        yield pg
        context.close()
        assert problems == [], problems

    def test_model_loads_from_file_url(self, spage):
        assert spage.evaluate("window.JCS.Search.state.semantic") == "ready"

    def test_wordpiece_and_vectors_match_python(self, spage):
        tok = WordPiece.from_file(ASSETS.model_vocab, ASSETS.spec.lowercase)
        for text in ("did he talk about the gun", "Anyone mention money for the witness?", "José's café"):
            assert spage.evaluate("t => window.JCS.Search.encodeQuery(t)", text) == tok.encode(text, ASSETS.spec.max_tokens)
        embedder = Embedder(ASSETS.spec, ASSETS.model_onnx, ASSETS.model_vocab)
        query = "conversations about moving the car"
        spage.fill("#searchInput", query)
        spage.wait_for_function("q => window.JCS.Search.vectorReady(q)", arg=query, timeout=10000)
        js = np.asarray(spage.evaluate("q => window.JCS.Search.queryVector(q)", query), dtype=np.float32)
        assert float(np.dot(js, embedder.encode_query(query))) > 0.98

    def test_similar_meaning_fallback_is_labeled(self, spage):
        spage.fill("#searchInput", "attorney")  # the scripts only ever say "lawyer"
        spage.wait_for_function("window.JCS.Search.vectorReady('attorney')", timeout=10000)
        spage.wait_for_timeout(300)
        assert _rows(spage).count() > 0
        expect(spage.locator("#searchHint")).to_contain_text("similar meaning")
        assert set(_tags(spage)) == {"similar meaning"}
        assert spage.locator("#rows mark").count() == 0
        # Exact words only never shows meaning matches
        spage.click('#searchMode .chip[data-smart="0"]')
        spage.wait_for_timeout(250)
        expect(_rows(spage)).to_have_count(0)

    def test_keyword_hits_lead_and_are_tagged_by_words(self, spage):
        spage.fill("#searchInput", "lawyer calls him first")
        spage.wait_for_function("window.JCS.Search.vectorReady('lawyer calls him first')", timeout=10000)
        spage.wait_for_timeout(300)
        first_tags = [t.strip().lower() for t in _rows(spage).first.locator(".tag").all_inner_texts()]
        assert first_tags[0] == "exact words"
        expect(spage.locator("#searchHint")).to_be_hidden()
