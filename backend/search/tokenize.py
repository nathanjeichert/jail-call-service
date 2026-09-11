"""Lexical tokenizer shared by the index builder and the page.

``templates/index_search.js`` carries a line-for-line port of everything
here; ``tests/test_search_browser.py`` pins both to identical output over
the synthetic corpus and a fixed word list. Change the two together.

Pipeline: fold to lowercase ASCII, join phone-number digit groups into one
run, expand a fixed contraction table, split on anything that is not a
letter, digit, or an apostrophe inside a word, then stem. ``index_terms``
adds the digit-run variants (a 10-digit number also indexes as its last 7
and last 4 digits) and drops stopwords and filler so ``555-0192`` and
``(530) 555-0192`` meet, ``gonna`` meets ``going``, and ``um`` costs nothing.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import List

STOPWORDS = frozenset("""
a about all am an and any are as at be because been but by can could did do
does for from get go going got had has have he her here him his how i if in
into is it its just like me my no not of oh okay on or our out over right she
should so some than that the their them then there they this to up us was we
well were what when where which who why will with would yeah yes you your
""".split())

# Spoken filler carried by the transcripts; indexed as nothing, displayed as-is.
FILLER = frozenset({"um", "uh", "mm", "hmm", "mhm", "er", "ah"})

# Whole-token contractions and slang, expanded before splitting.
CONTRACTIONS = {
    "gonna": "going to", "wanna": "want to", "gotta": "got to", "kinda": "kind of",
    "sorta": "sort of", "outta": "out of", "lemme": "let me", "gimme": "give me",
    "dunno": "do not know", "ain't": "is not", "can't": "can not", "won't": "will not",
    "shan't": "shall not", "i'm": "i am", "let's": "let us", "y'all": "you all",
    "'em": "them", "'cause": "because",
}
_SUFFIXES = (("n't", " not"), ("'ll", " will"), ("'re", " are"), ("'ve", " have"),
             ("'d", " would"), ("'s", ""), ("s'", "s"))

_PHONE_RE = re.compile(r"\(?(\d{3})\)?[-. ]?(\d{3})[-. ]?(\d{4})\b|\b(\d{3})[-. ](\d{4})\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")


def fold(text: str) -> str:
    """Lowercase, strip accents, and unify apostrophes."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().replace("’", "'").replace("‘", "'")


def join_phone_numbers(text: str) -> str:
    """``(530) 555-0192`` and ``555-0192`` become one digit run."""
    return _PHONE_RE.sub(lambda m: "".join(g for g in m.groups() if g), text)


def expand_contractions(word: str) -> str:
    if word in CONTRACTIONS:
        return CONTRACTIONS[word]
    for suffix, repl in _SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix):
            return word[: -len(suffix)] + repl
    return word


def tokenize(text: str) -> List[str]:
    """Surface tokens, lowercased, contractions expanded, no stemming."""
    out: List[str] = []
    for raw in _TOKEN_RE.findall(join_phone_numbers(fold(text))):
        for word in expand_contractions(raw).split(" "):
            if word:
                out.append(word.replace("'", ""))
    return out


def digit_variants(token: str) -> List[str]:
    """A long digit run also indexes as its tail so partial numbers hit."""
    if not token.isdigit() or len(token) < 7:
        return [token]
    variants = [token, token[-7:], token[-4:]]
    return list(dict.fromkeys(variants))


def index_terms(text: str) -> List[str]:
    """Stems to index or query: stopwords and filler removed, digit tails added."""
    terms: List[str] = []
    for tok in tokenize(text):
        if tok in STOPWORDS or tok in FILLER:
            continue
        for variant in digit_variants(tok):
            terms.append(variant if variant.isdigit() else stem(variant))
    return terms


def stem_tokens(text: str) -> List[str]:
    """Every token stemmed, stopwords kept: the stream phrase matching compares."""
    return [tok if tok.isdigit() else stem(tok) for tok in tokenize(text)]


# --- Porter stemmer (Porter, 1980), written to mirror the JS port exactly ---

_VOWELS = "aeiou"


def _is_consonant(word: str, i: int) -> bool:
    ch = word[i]
    if ch in _VOWELS:
        return False
    if ch == "y":
        return i == 0 or not _is_consonant(word, i - 1)
    return True


def _measure(stem_: str) -> int:
    """The number of VC sequences in ``stem_``."""
    m = 0
    i = 0
    n = len(stem_)
    while i < n and _is_consonant(stem_, i):
        i += 1
    while i < n:
        while i < n and not _is_consonant(stem_, i):
            i += 1
        if i >= n:
            break
        m += 1
        while i < n and _is_consonant(stem_, i):
            i += 1
    return m


def _has_vowel(stem_: str) -> bool:
    return any(not _is_consonant(stem_, i) for i in range(len(stem_)))


def _ends_double_consonant(word: str) -> bool:
    return len(word) >= 2 and word[-1] == word[-2] and _is_consonant(word, len(word) - 1)


def _cvc(word: str) -> bool:
    """Ends consonant-vowel-consonant, the last not w, x, or y."""
    n = len(word)
    if n < 3:
        return False
    return (_is_consonant(word, n - 1) and not _is_consonant(word, n - 2)
            and _is_consonant(word, n - 3) and word[-1] not in "wxy")


def _replace_if(word: str, suffix: str, repl: str, min_measure: int):
    """``(word[:-len(suffix)] + repl, True)`` when the rule fires."""
    if not word.endswith(suffix):
        return word, False
    stem_ = word[: len(word) - len(suffix)]
    if _measure(stem_) > min_measure:
        return stem_ + repl, True
    return word, True  # suffix matched: the step is done even if m was too small


_STEP2 = (("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"),
          ("izer", "ize"), ("abli", "able"), ("alli", "al"), ("entli", "ent"), ("eli", "e"),
          ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"), ("ator", "ate"),
          ("alism", "al"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"),
          ("aliti", "al"), ("iviti", "ive"), ("biliti", "ble"))
_STEP3 = (("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"),
          ("ful", ""), ("ness", ""))
_STEP4 = ("al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement", "ment", "ent",
          "ion", "ou", "ism", "ate", "iti", "ous", "ive", "ize")


@lru_cache(maxsize=None)
def stem(word: str) -> str:
    """Porter-stem a lowercase word; words under three letters are returned as-is."""
    if len(word) < 3:
        return word
    w = word

    # Step 1a
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("ss"):
        pass
    elif w.endswith("s"):
        w = w[:-1]

    # Step 1b
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    else:
        fired = False
        if w.endswith("ed") and _has_vowel(w[:-2]):
            w = w[:-2]
            fired = True
        elif w.endswith("ing") and _has_vowel(w[:-3]):
            w = w[:-3]
            fired = True
        if fired:
            if w.endswith(("at", "bl", "iz")):
                w += "e"
            elif _ends_double_consonant(w) and w[-1] not in "lsz":
                w = w[:-1]
            elif _measure(w) == 1 and _cvc(w):
                w += "e"

    # Step 1c
    if w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"

    # Step 2
    for suffix, repl in _STEP2:
        if w.endswith(suffix):
            w, _ = _replace_if(w, suffix, repl, 0)
            break

    # Step 3
    for suffix, repl in _STEP3:
        if w.endswith(suffix):
            w, _ = _replace_if(w, suffix, repl, 0)
            break

    # Step 4
    for suffix in _STEP4:
        if w.endswith(suffix):
            stem_ = w[: -len(suffix)]
            if suffix == "ion":
                if _measure(stem_) > 1 and stem_ and stem_[-1] in "st":
                    w = stem_
            elif _measure(stem_) > 1:
                w = stem_
            break

    # Step 5a
    if w.endswith("e"):
        stem_ = w[:-1]
        m = _measure(stem_)
        if m > 1 or (m == 1 and not _cvc(stem_)):
            w = stem_

    # Step 5b
    if _measure(w) > 1 and _ends_double_consonant(w) and w.endswith("l"):
        w = w[:-1]

    return w
