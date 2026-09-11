# Hybrid search for the delivery page: research and implementation plan

Status: **implemented 2026-09-10** (Phases 1 and 2). Section 6 records the decisions taken and how the build differs from the plan; sections 1 to 4 are the research and the plan as reviewed. `AGENTS.md` ("Search") is the maintenance reference for the code.

## 1. What the evidence says

### 1.1 Today's search is not usable for the questions lawyers ask

Benchmark: the 100-call *People v. John Smith* synthetic corpus (67k words, 1,003 passages of ~100 words) with 30 lawyer-style queries and a hand-written answer key (`scratchpad/bench/bench.py`; numbers are the share of relevant calls found in the top 5 / top 10 of the ranked call list, and mean reciprocal rank of the first hit).

| Search | Recall@5 | Recall@10 | MRR |
|---|---|---|---|
| Today (case-insensitive substring over the whole call) | 0.03 | 0.03 | 0.03 |
| BM25 keyword search over passages | 0.68 | 0.78 | 0.61 |
| Static embedding only (potion-base-8M, 7.6 MB table, no runtime) | 0.63 | 0.69 | 0.54 |
| Small transformer only (all-MiniLM-L6-v2, 23 MB quantized) | 0.66 | 0.79 | 0.63 |
| Small transformer only (e5-small-v2, 34 MB quantized) | 0.74 | 0.84 | 0.74 |
| **Hybrid: BM25 + MiniLM, min-max fusion 0.5/0.5** | **0.79** | **0.89** | 0.65 |
| Hybrid: BM25 + e5-small-v2, min-max fusion | 0.74 | 0.89 | 0.74 |
| Hybrid: BM25 + potion-8M, min-max fusion | 0.67 | 0.81 | 0.65 |

Today's search only "works" when the lawyer happens to type a string that appears verbatim ("nine years"). Every natural-language query fails. Two thirds of the fix is a proper keyword engine; the embedding model adds the last third, concentrated on exactly the queries that matter most:

| Query | BM25 rank of first relevant call | MiniLM rank | Hybrid |
|---|---|---|---|
| "firearm" (calls only ever say "gun", "the tool", "the groceries") | 81 | 4 | 4 |
| "told his mother not to talk to anyone" | 23 | 1 | 1 |
| "did he tell anyone to get rid of the gun" (coded language) | 40 | 6 | 6 |
| "pressuring the store clerk about the lineup" | 3 | 14 | 3 |
| "plea deal offer from the district attorney" | 1 | 20 | 1 |

Keyword and meaning search fail on different queries, which is why fusing them beats either alone. Caveat: 30 queries on 100 calls; differences under ~0.05 are noise. The robust conclusions are the ones above, plus: int8 storage of the passage vectors is lossless, truncating 384-dim vectors to 256 costs nothing measurable, and passages of 80-110 words with 50 % overlap beat 150-200-word passages for these short, turn-based calls.

### 1.2 The runtime path works from `file://` with no flags

Spike (`scratchpad/spike-ort/`): ONNX Runtime Web 1.29 (`ort.wasm.min.js`, 50 KB), its 14 MB wasm, the 23 MB quantized MiniLM model, and the WordPiece vocab, all shipped as base64 inside plain `<script src>` sidecar files, with a hand-rolled 60-line tokenizer. Opened from a `file://` URL in Playwright's three engines:

| Engine | Model ready after script parse | Per-query embed | Vector match vs. Python |
|---|---|---|---|
| Chromium 148 | 0.2 s | 7 ms | cosine 0.99, identical tokens |
| WebKit 26 (Safari) | 0.6 s | 9 ms | same |
| Firefox 150 | 0.7 s | 75 ms | same |

Page memory: ~150 MB. Expect 2-5x slower on an old government laptop, still under two seconds to ready and well under 100 ms per query. Recipe that made it work: `ort.env.wasm.wasmBinary = bytes`, `numThreads = 1`, the ES-module glue passed as a `blob:` URL (dynamic `import()` of a `file://` module is blocked by CORS in every browser), session created on the main thread. The second research agent independently reproduced this with ONNX Runtime 1.27 on Chrome 152 (session in 180 ms, 6 ms per query) and found that from 1.27 the `ort.wasm.bundle.min.mjs` build embeds the glue, which removes the `blob:` step if the bundle text is inlined; either route is fine, and a `data:` URL for the glue does not work (the glue's `new URL(import.meta.url)` throws).

Platform verification (separate agent, empirical, Chromium 148 / Firefox 150 stock prefs / WebKit 26):

* Classic `<script src>` sidecars load everywhere; a 30 MB one blocks the parser 0.2-0.4 s (Chromium), a 60 MB one 0.4-1 s. Load them one at a time after the UI paints. Modules, `fetch`, XHR, and file-path workers are blocked everywhere; `blob:` classic workers work everywhere.
* `Uint8Array.fromBase64` decodes 30 MB in ~15 ms but needs Chrome/Edge 140, Firefox 133, Safari 18.2; the `atob` loop fallback costs ~130 ms. Never use synchronous `new WebAssembly.Module` on a buffer over 8 MB in Chrome.
* IndexedDB works and persists on `file://` (Chrome: one shared origin for every local file, so key by package id; Firefox: per file path). Use it only as a best-effort cache.
* **Real enterprise risks:** (1) `DefaultJavaScriptJitSetting` = disabled removes `WebAssembly` entirely in Chrome (Edge falls back to a slow interpreter). The page must feature-detect and fall back to keyword-only search with a visible notice. (2) A `URLBlocklist` on `file://` blocks the whole page; nothing we ship can work around it and it already affects today's product. No DISA STIG rule for Chrome or Edge touches JavaScript, WebAssembly, or `file://`. Mark-of-the-Web does not change how Chromium treats a local HTML page.
* Windows Explorer's zip preview extracts only the double-clicked file, so sidecars (and the audio) are missing; the page should detect a missing sidecar and say "extract the zip first" instead of failing silently.

### 1.3 Package cost

| Asset | In the zip | Extracted |
|---|---|---|
| Lexical index (postings + positions, typed arrays, ~3.6M words) | ~6 MB | ~13 MB |
| Passage vectors, int8, 384-d (~40k passages) | ~15 MB | ~20 MB |
| ONNX Runtime wasm (base64) | 5.5 MB | 18.6 MB |
| MiniLM quantized model (base64) + vocab | 20 MB | 31 MB |

A 100-call job adds ~28 MB to the zip for the semantic layer; a real job's MP3s are hundreds of MB to several GB, so the model is a rounding error there, but it is the entire budget for tiny jobs. The static-embedding alternative (potion-8M) needs no wasm runtime and costs ~8 MB total, at the quality shown in 1.1.

### 1.4 What legal-review and transcript tools do (survey of Relativity, Everlaw, DISCO, Logikcull, Reveal, Nuix, Trint, Rev, Otter, Descript, Securus, ViaPath, LEO Verus, JusticeText)

Expected everywhere: hit highlighting with next/previous and a counter, exact phrase via quotes, filters with live counts, saved/recent searches, per-term hit reports, tags and notes, CSV export, click-to-play at the word, reviewed/unreviewed tracking. Differentiators: plain-language questions with citations, "similar passages", timelines, explanation of AI results. Every AI feature in the serious tools (Relativity aiR, Everlaw, JusticeText) ships the verbatim passage and a citation with the result; ABA Formal Opinion 512 makes verification the lawyer's duty, so a "meaning" hit with no visible passage is unusable. Usability research (Nielsen Norman): most users cannot use Boolean syntax; mean query length is two words.

## 2. Architecture

### 2.1 Operator side (Python, at delivery-asset time)

New module `backend/delivery/search_index.py`, fed by the same `CallView` list as every other surface:

1. **Passages.** Turn-aware windows of ~100 words, stride ~50 (50 % overlap), never splitting a turn under 200 words. Each passage records call index, first/last line id (so Tr. page:line and the printed-page highlight come for free), start/end seconds, and the speakers present. ~40k passages for a 2,000-call job.
2. **Tokenizer spec** shared with the page (one small pure function, ported line-for-line to JS, with a parity test): lowercase, ASCII-fold, split on non-alphanumerics, keep apostrophes inside words, expand a fixed contraction table (`gonna` -> `going to`, `don't` -> `do not`), normalize digit runs (`(530) 555-0192`, `530-555-0192`, `5305550192` all index as `5305550192` plus `5550192` and `0192` so partial numbers hit), light suffix stemming with the surface form kept alongside so the UI always shows the actual matched word. Filler (`um`, `uh`) is dropped from the index, never from display.
3. **Lexical index.** BM25 postings per stem (doc id + term frequency, delta-varint), positions per posting (for phrase verification and word highlighting), sorted vocabulary (for prefix and typo tolerance). Written as `search/index.js` = `window.JCS_SEARCH = { ... base64 typed arrays ... }`. Build cost measured by the research agent: ~3 s for 3.9M words; page-side load 40-70 ms. A library (MiniSearch) was the runner-up; rejected because none gives BM25 + positions + a controllable tokenizer in a script-tag build, and because building in Python keeps Node out of the pipeline.
4. **Passage vectors.** Embedded with the *same quantized ONNX model the page ships*, via `onnxruntime` in Python, so query and passage numerics match exactly. Stored int8 with a per-vector scale (`search/vectors.js`). ~200 s for 40k passages on the M2.
5. **Runtime assets** copied from `backend/delivery/assets/search/`: `ort.wasm.min.js`, `ort.js` (wasm + glue as base64), `model.js` (model + vocab as base64). The model file itself (23 MB) lives outside the repo, downloaded once into the app-support directory like the FluidAudio models, pinned by SHA-256.

Registry: an `EmbeddingSpec` in `backend/search/` (id, HF repo, ONNX file, dims, query prefix, max tokens) so the model is swappable without touching the page or the builder.

### 2.2 Page side (`templates/index.html`, new `Search` part of the IIFE)

1. **Progressive load.** The page renders and keyword search is live as soon as `search/index.js` parses (well under a second). Then the page injects `vectors.js`, `ort.js`, `model.js` one at a time, decodes, drops the base64 globals, creates the ONNX session on the main thread, and flips a small "meaning search ready" state in the search box. If `WebAssembly` is missing (JIT policy) or a sidecar fails to load, the page stays keyword-only and says so in the search hint. No worker in v1: session creation is the only stall and it is a one-time 0.2-2 s.
2. **Query pipeline.** Parse quotes and digit runs. Lexical: BM25 over passages, phrase verification via positions, prefix for the last term while typing, typo tolerance (edit distance 1 for terms of 5+ letters, never for digits). Semantic: embed the query, dot product over the int8 matrix (measured: 40k x 384 in plain JS is 14-19 ms in Chromium, WebKit, and Firefox, 60 ms with 4x CPU throttling; top-200 selection 1-3 ms). Fusion: min-max normalize each side over its top 200, weighted sum (0.5/0.5 default). Query-shape rules: quoted or all-digit queries are lexical only; zero lexical hits means semantic only and the results say "no exact matches, showing related passages".
3. **Call ranking.** Score of a call = its best passage (MaxP), tie-break on how many other passages score within half of it. While a query is active the list orders by match strength; the column headers still override.
4. **Evidence, always.** Each result row shows its best passage with the matched words bolded and a badge: **Exact words** when lexical terms hit, **Similar meaning** when only the vector matched (no fake bolding). Clicking the passage opens the call view at that second, as excerpts do today. The expanded row and the call view's transcript search reuse the same tokenizer and highlighter, so stems and number formats highlight consistently.

## 3. Search quality-of-life features

Already shipped and kept: hit stepping with a counter, click-to-play at the moment, date / phone / relevance filters, clickable relevance tally, column sorting, in-call transcript search.

| # | Feature | Class | Cost | Notes |
|---|---|---|---|---|
| 1 | Result passages with bolded matches and the Exact / Similar badge | expected | M | The heart of the feature; without it semantic hits are untrustworthy |
| 2 | Exact phrase with quotes, shown as a hint under the box | expected | S | The only syntax worth supporting |
| 3 | Phone-number-aware search (any format, last 7 or 4 digits) | differentiator | S | Identifier queries are where keyword search wins by a mile |
| 4 | Typo tolerance and stem matching with the real matched word shown | expected | S | Comes with the lexical engine |
| 5 | Recent searches under the box (localStorage, best effort) | expected | S | Lawyers re-run the same ten queries |
| 6 | "Said by" chips: Defendant / Outside party | differentiator | S | Passages know their speakers; unique to two-party calls |
| 7 | Live counts on the phone dropdown; duration and outcome filters | expected | S | Filter carousel pattern |
| 8 | "Exact words only" toggle chip | expected | S | Lets a skeptical reviewer switch the model off |
| 9 | Export results (CSV and a printable hit list with call, time, Tr. page:line, quote) | expected | S-M | For memos and motions |
| 10 | Term watch-list: per-term hit counts per call, persistent color highlights in every transcript | expected in e-discovery | M | Names, nicknames, places; must be labelled "as transcribed" |
| 11 | Tags / flags and notes per call and per moment, saved to a JSON file the lawyer exports and re-imports | expected | M-L | `file://` storage is unreliable (Firefox blocks localStorage, Chrome shares one origin); the export file has to be the source of truth |
| 12 | "Find similar moments" from any passage | differentiator | M | Nearest neighbours in vector space; must be labelled similarity, not evidence |
| 13 | Hits-over-time strip above the results (click a day to filter) | differentiator | M | Cheap given call dates; the case report already has a timeline |
| 14 | Suggested starting queries (chips under the box: weapon, money, witness, police, alibi, drugs, phone, car) | differentiator | S | Zero-cost onboarding for someone who has never searched by meaning |

Anti-patterns to keep out: Boolean or proximity syntax as the interface, any exposed knob (weights, tolerance), raw scores or percentages, semantic hits that look like quotes, silent stemming or alias matching, "not discussed" wording when we mean "no transcript text matches" (transcripts miss words; a zero-hit list is not proof of absence), and building the index in the page on open.

## 4. Phases and verification

**Phase 1: lexical engine and the new results UI** (independently shippable)
Python builder + tokenizer + tests; JS tokenizer parity test (same corpus tokenized in Python and in Playwright, identical output); `search/index.js` sidecar; BM25 + phrase + prefix + typo; result passages with bolding; features 2-8 and 14; missing-sidecar detection. Golden test gains the new sidecar digest; browser tests gain paraphrase queries with expected calls (the synthetic package gets three or four scripted calls written for that); guide copy and screenshots regenerated.

**Phase 2: semantic layer**
Model asset management (hash-pinned download to app support); `onnxruntime` + `tokenizers` added to `pyproject.toml`; passage embedding at delivery time; `vectors.js`, `ort.js`, `model.js`; page loader with feature detection and fallback; fusion and badges; feature 1 complete. Verification: browser-vs-Python vector parity (cosine > 0.98 on 50 passages); the demo-corpus benchmark re-run through the real pipeline (target recall@10 >= 0.85); timing and memory under Playwright CPU throttling (4x) to stand in for a government laptop; the three-engine `file://` matrix from the spike as a test.

**Phase 3: review tooling** (after the demo)
Features 9-13 in whatever order the demo feedback suggests.

## 5. Open decisions (`DECISION`)

1. **Scope for the demo.** Phase 1 only, or Phases 1 + 2? Recommendation: both; Phase 1 is the safety net if Phase 2 slips.
2. **Model.** `all-MiniLM-L6-v2` (23 MB, Apache-2, best-known, same size as `snowflake-arctic-embed-xs`) vs `e5-small-v2` (34 MB, MIT, slightly stronger alone, equal in hybrid) vs `potion-base-8M` static (8 MB, no wasm, noticeably weaker on one-word synonyms like "firearm"). Recommendation: MiniLM, with the registry making a later swap a one-line change.
3. **Package size.** Accept +28 MB zipped / +50 MB extracted for the semantic layer on every delivery? An alternative is an operator-side toggle per job (skip it for tiny jobs that must fit an email).
4. **Ordering while searching.** Rank by match strength when a query is active (recommended) vs keep date order and only filter.
5. **Presenting meaning hits.** Interleaved with exact hits, each badged, plus an "Exact words only" chip (recommended, better recall) vs a separate "Related calls" section under the exact matches (safer optics, worse ranking).
6. **Phase 1 feature set.** Recommended: 1-8 and 14. Anything from 9-13 to pull forward?
7. **Data payload growth (separate but related).** Each call's line entries cost ~90 bytes per transcript word inside `index.html` (every line carries its rendered text and metadata), so a 2,000-call job is a ~300 MB page before any search index. Chrome copes but opens slowly. Recommendation: while the loader is being reworked, move the call data into its own sidecar and compact the line entries (arrays, no `rendered_text`), roughly a 3x reduction. Do it in this pass, or park it?

## 6. What was built (2026-09-10)

Decisions (Nathan): both phases; MiniLM; lean on keyword search because the demo corpus is cleaner than real transcripts and false positives cost more than misses; the mode switch must be obvious, named **Smart search** / **Exact words only**; features 1 to 8 only; rank by match strength while searching; meaning hits interleaved but tagged. Decision 7 (compacting the call payload) was not taken up and stays parked.

Where the build differs from the plan above:

* **Smart-mode matching is OR, not "half the words".** Measured on the demo corpus with the real tokenizer: requiring half of the query's content terms cut recall@5 from 0.69 to 0.48; scoring every passage with any content term and dropping calls under 30 % of the top call's score keeps recall@5 at 0.69 and recall@10 at 0.73 while cutting the average list from 52 calls to 18 (`RELATIVE_CUTOFF`).
* **Fusion leans lexical and gates meaning-only calls.** With the real index and embedder: keyword weight 0.7, meaning-only calls admitted to a keyword list only above cosine 0.45 (at 0.35 they were 16 wrong to 1 right in the top ten across 30 queries; at 0.45, 2 to 0), and a labeled fallback (top ten above 0.30, hint "No call contains these words. Showing calls with a similar meaning instead.") only when a query has no keyword hit at all. Hybrid recall@5 / @10 / MRR: 0.73 / 0.82 / 0.69 against 0.69 / 0.73 / 0.64 for keywords alone; the meaning layer's value is mostly re-ranking calls that also have keyword hits.
* **A summary passage per call** (metadata, brief, identity, cue notes and quotes) keeps phone numbers, names, and summary text searchable; the plan only indexed transcript passages. A summary counts as said by every speaker, so a Said-by chip keeps summary hits (labeled SUMMARY) and drops only transcript lines the other party said.
* **Evidence points at the best line, not the passage start**: the line carrying most of the query's words sets the timestamp and the snippet window, so an automated "this call is recorded" line never wins over the real hit.
* **Stopwords were widened** (about, not, get, going, just, okay…) after the demo showed them dominating multi-word queries.
* **Runtime assets are downloaded, not vendored**: ONNX Runtime Web 1.29 from npm and the model from Hugging Face, SHA-256 pinned, into the app-support directory; CI caches them. The synthetic package and the golden ship keyword search only.
* **No worker**: session creation on the main thread measured 0.2 to 0.7 s across engines; the ten-call synthetic package reaches keyword search in 0.13 s and meaning search in 0.6 s in headless Chromium at 148 MB heap.

Verification: 117 tests including tokenizer parity between Python and the page over every synthetic transcript line and Porter's published word list, the browser-versus-Python query vector (cosine > 0.98), WordPiece id parity, Smart versus Exact, phrases, phone formats, typos, Said-by, match tags, ranking, filters, recent searches, the missing-index and no-WebAssembly fallbacks, and the labeled similar-meaning fallback. The golden package gains `search/index.js`.

Parked for Phase 3: features 9 to 14 (export, term watch-list, tags and notes, similar moments, hits-over-time, suggested queries) and the payload compaction (decision 7). Benchmark and spike scripts from the research live outside the repo (`scratchpad/bench`, `scratchpad/spike-ort` of the 2026-09-10 session); the reference scorer in `backend/search/lexical.py` and `Embedder` reproduce the measurements from the demo corpus on the external drive.
