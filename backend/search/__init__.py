"""Search index for the delivery page.

The page's search runs entirely in the browser from ``file://``; this
package builds what it needs at delivery time:

* :mod:`tokenize` - the lexical tokenizer and Porter stemmer. The page
  carries a line-for-line JavaScript port (``templates/index_search.js``);
  ``tests/test_search_browser.py`` pins the two to identical output.
* :mod:`passages` - turn-aware ~100-word windows over a call's line entries.
* :mod:`lexical` - the BM25 index over passages, serialized as typed arrays.
* :mod:`embeddings` - the embedding model registry, the WordPiece tokenizer
  the model needs, and ONNX inference for passage vectors.
* :mod:`assets` - the runtime files the page ships (ONNX Runtime Web and
  the model), downloaded once and pinned by hash.
* :mod:`build` - the ``search/*.js`` files the delivery folder gets.
"""
