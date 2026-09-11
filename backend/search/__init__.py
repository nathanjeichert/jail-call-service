"""Search index for the delivery page.

The page's search runs entirely in the browser from ``file://``; this
package builds what it needs at delivery time:

* :mod:`tokenize` - the lexical tokenizer and Porter stemmer. The page
  carries a line-for-line JavaScript port (``templates/index_search.js``);
  ``tests/test_search_browser.py`` pins the two to identical output.
* :mod:`passages` - turn-aware ~100-word windows over a call's line entries.
* :mod:`lexical` - the BM25 index over passages, serialized as typed arrays.
* :mod:`related` - the related-words table (the meaning layer): for every
  query word, the corpus words the embedding model says mean the same.
* :mod:`embeddings` - the embedding model registry, its WordPiece
  tokenizer, and ONNX inference.
* :mod:`assets` - the model files, downloaded once and pinned by hash.
* :mod:`build` - the ``app-assets/index.js`` file the delivery folder gets.
"""
