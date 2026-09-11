  // ═══════════════════════════════════════════════════════════════════════
  // Search: the index page's keyword (BM25) search with related-word
  // expansion. A Jinja include that lands inside the page's IIFE; it uses
  // CALLS, esc, and secondsToLabel from the page. The data comes from one
  // sidecar (a classic script, the only data channel file:// allows):
  //   app-assets/index.js -> window.JCS_SEARCH   passages, BM25 postings,
  //                          and the related-words table when the package
  //                          was built with the embedding model
  // Search is live as soon as index.js parses. The tokenizer, stemmer, and
  // scoring rules mirror backend/search/ (tokenize.py, lexical.py,
  // related.py); change them together.
  // ═══════════════════════════════════════════════════════════════════════
  const Search = (function () {
    // ── Tokenizer (port of backend/search/tokenize.py) ──────────────────
    const STOPWORDS = new Set(('a about all am an and any are as at be because been but by can could did do ' +
      'does for from get go going got had has have he her here him his how i if in into is it its just like me ' +
      'my no not of oh okay on or our out over right she should so some than that the their them then there ' +
      'they this to up us was we well were what when where which who why will with would yeah yes you your').split(' '));
    const FILLER = new Set(['um', 'uh', 'mm', 'hmm', 'mhm', 'er', 'ah']);
    const CONTRACTIONS = {
      'gonna': 'going to', 'wanna': 'want to', 'gotta': 'got to', 'kinda': 'kind of',
      'sorta': 'sort of', 'outta': 'out of', 'lemme': 'let me', 'gimme': 'give me',
      'dunno': 'do not know', "ain't": 'is not', "can't": 'can not', "won't": 'will not',
      "shan't": 'shall not', "i'm": 'i am', "let's": 'let us', "y'all": 'you all',
      "'em": 'them', "'cause": 'because',
    };
    const SUFFIXES = [["n't", ' not'], ["'ll", ' will'], ["'re", ' are'], ["'ve", ' have'],
                      ["'d", ' would'], ["'s", ''], ["s'", 's']];
    const PHONE_RE = /\(?(\d{3})\)?[-. ]?(\d{3})[-. ]?(\d{4})\b|\b(\d{3})[-. ](\d{4})\b/g;
    const TOKEN_RE = /[a-z0-9]+(?:'[a-z0-9]+)*/g;

    function fold(text) {
      return text.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
        .replace(/’/g, "'").replace(/‘/g, "'");
    }
    function joinPhoneNumbers(text) {
      return text.replace(PHONE_RE, (m, a, b, c, d, e) => [a, b, c, d, e].filter(Boolean).join(''));
    }
    function expandContractions(word) {
      if (Object.prototype.hasOwnProperty.call(CONTRACTIONS, word)) return CONTRACTIONS[word];
      for (const [suffix, repl] of SUFFIXES) {
        if (word.endsWith(suffix) && word.length > suffix.length) return word.slice(0, -suffix.length) + repl;
      }
      return word;
    }
    function tokenize(text) {
      const out = [];
      const raw = joinPhoneNumbers(fold(text)).match(TOKEN_RE) || [];
      for (const r of raw) {
        for (const word of expandContractions(r).split(' ')) {
          if (word) out.push(word.replace(/'/g, ''));
        }
      }
      return out;
    }
    const isDigits = s => /^[0-9]+$/.test(s);
    function digitVariants(token) {
      if (!isDigits(token) || token.length < 7) return [token];
      return Array.from(new Set([token, token.slice(-7), token.slice(-4)]));
    }
    function indexTerms(text) {
      const terms = [];
      for (const tok of tokenize(text)) {
        if (STOPWORDS.has(tok) || FILLER.has(tok)) continue;
        for (const v of digitVariants(tok)) terms.push(isDigits(v) ? v : stem(v));
      }
      return terms;
    }
    function stemTokens(text) {
      return tokenize(text).map(t => (isDigits(t) ? t : stem(t)));
    }

    // Porter stemmer (Porter, 1980); mirrors tokenize.stem step for step.
    const VOWELS = 'aeiou';
    function isCons(w, i) {
      const ch = w[i];
      if (VOWELS.indexOf(ch) !== -1) return false;
      if (ch === 'y') return i === 0 || !isCons(w, i - 1);
      return true;
    }
    function measure(s) {
      let m = 0, i = 0; const n = s.length;
      while (i < n && isCons(s, i)) i++;
      while (i < n) {
        while (i < n && !isCons(s, i)) i++;
        if (i >= n) break;
        m++;
        while (i < n && isCons(s, i)) i++;
      }
      return m;
    }
    function hasVowel(s) { for (let i = 0; i < s.length; i++) if (!isCons(s, i)) return true; return false; }
    function endsDouble(w) { return w.length >= 2 && w[w.length - 1] === w[w.length - 2] && isCons(w, w.length - 1); }
    function cvc(w) {
      const n = w.length;
      if (n < 3) return false;
      return isCons(w, n - 1) && !isCons(w, n - 2) && isCons(w, n - 3) && 'wxy'.indexOf(w[n - 1]) === -1;
    }
    function replaceIf(w, suffix, repl, minM) {
      const s = w.slice(0, w.length - suffix.length);
      return measure(s) > minM ? s + repl : w;
    }
    const STEP2 = [['ational', 'ate'], ['tional', 'tion'], ['enci', 'ence'], ['anci', 'ance'], ['izer', 'ize'],
      ['abli', 'able'], ['alli', 'al'], ['entli', 'ent'], ['eli', 'e'], ['ousli', 'ous'], ['ization', 'ize'],
      ['ation', 'ate'], ['ator', 'ate'], ['alism', 'al'], ['iveness', 'ive'], ['fulness', 'ful'],
      ['ousness', 'ous'], ['aliti', 'al'], ['iviti', 'ive'], ['biliti', 'ble']];
    const STEP3 = [['icate', 'ic'], ['ative', ''], ['alize', 'al'], ['iciti', 'ic'], ['ical', 'ic'], ['ful', ''], ['ness', '']];
    const STEP4 = ['al', 'ance', 'ence', 'er', 'ic', 'able', 'ible', 'ant', 'ement', 'ment', 'ent', 'ion', 'ou',
      'ism', 'ate', 'iti', 'ous', 'ive', 'ize'];
    function stem(word) {
      if (word.length < 3) return word;
      let w = word;
      // Step 1a
      if (w.endsWith('sses')) w = w.slice(0, -2);
      else if (w.endsWith('ies')) w = w.slice(0, -2);
      else if (w.endsWith('ss')) { /* keep */ }
      else if (w.endsWith('s')) w = w.slice(0, -1);
      // Step 1b
      if (w.endsWith('eed')) {
        if (measure(w.slice(0, -3)) > 0) w = w.slice(0, -1);
      } else {
        let fired = false;
        if (w.endsWith('ed') && hasVowel(w.slice(0, -2))) { w = w.slice(0, -2); fired = true; }
        else if (w.endsWith('ing') && hasVowel(w.slice(0, -3))) { w = w.slice(0, -3); fired = true; }
        if (fired) {
          if (w.endsWith('at') || w.endsWith('bl') || w.endsWith('iz')) w += 'e';
          else if (endsDouble(w) && 'lsz'.indexOf(w[w.length - 1]) === -1) w = w.slice(0, -1);
          else if (measure(w) === 1 && cvc(w)) w += 'e';
        }
      }
      // Step 1c
      if (w.endsWith('y') && hasVowel(w.slice(0, -1))) w = w.slice(0, -1) + 'i';
      // Step 2
      for (const [suffix, repl] of STEP2) { if (w.endsWith(suffix)) { w = replaceIf(w, suffix, repl, 0); break; } }
      // Step 3
      for (const [suffix, repl] of STEP3) { if (w.endsWith(suffix)) { w = replaceIf(w, suffix, repl, 0); break; } }
      // Step 4
      for (const suffix of STEP4) {
        if (w.endsWith(suffix)) {
          const s = w.slice(0, w.length - suffix.length);
          if (suffix === 'ion') { if (measure(s) > 1 && s && 'st'.indexOf(s[s.length - 1]) !== -1) w = s; }
          else if (measure(s) > 1) w = s;
          break;
        }
      }
      // Step 5a
      if (w.endsWith('e')) {
        const s = w.slice(0, -1); const m = measure(s);
        if (m > 1 || (m === 1 && !cvc(s))) w = s;
      }
      // Step 5b
      if (measure(w) > 1 && endsDouble(w) && w.endsWith('l')) w = w.slice(0, -1);
      return w;
    }

    // ── Sidecar decoding ────────────────────────────────────────────────
    function bytesOf(b64) {
      if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(b64);
      const bin = atob(b64); const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }
    // Both bytesOf paths return a fresh, offset-0 buffer, so a view is aligned.
    function typed(b64, Ctor) { return new Ctor(bytesOf(b64).buffer); }

    // ── Passages, the unit the index scores ─────────────────────────────
    const KIND_SUMMARY = 1;
    const SUMMARY_FIELDS = ['outside', 'inmate', 'identity', 'facility', 'outcome', 'call_type', 'brief'];
    // Mirrors passages.summary_text: what a call's summary passage holds.
    function summaryText(call) {
      const parts = SUMMARY_FIELDS.map(f => String(call[f] || '').trim());
      (call.cues || []).forEach(c => { parts.push(String(c.note || '').trim()); parts.push(String(c.quote || '').trim()); });
      return parts.filter(Boolean).join(' · ');
    }
    const isSummary = pid => L.pKind[pid] === KIND_SUMMARY;
    // A passage's non-blank lines (empty for a summary passage).
    function passageLines(pid) {
      if (isSummary(pid)) return [];
      const lines = CALLS[L.pCall[pid]].lines;
      const out = [];
      for (let i = L.pFirst[pid]; i <= L.pLast[pid]; i++) {
        if (lines[i] && (lines[i].text || '').trim()) out.push(lines[i]);
      }
      return out;
    }
    function passageText(pid) {
      if (isSummary(pid)) return summaryText(CALLS[L.pCall[pid]]);
      return passageLines(pid).map(l => l.text.trim()).join(' ');
    }

    // ── The index ───────────────────────────────────────────────────────
    let L = null;          // decoded window.JCS_SEARCH
    // lexical: 'loading' | 'ready' | 'missing' (zip opened without extracting);
    // related: 'none' (package built without the model) | 'ready'.
    const state = { lexical: 'loading', related: 'none' };
    const listeners = [];
    function notify() { listeners.forEach(fn => { try { fn(state); } catch (e) { /* listener error */ } }); }

    function loadLexical(raw) {
      const vocab = raw.vocab ? raw.vocab.split('\n') : [];
      L = {
        n: raw.n, avgdl: raw.avgdl, k1: raw.k1, b: raw.b,
        vocab, termIndex: new Map(vocab.map((t, i) => [t, i])),
        termOff: typed(raw.termOff, Uint32Array), termDf: typed(raw.termDf, Uint32Array),
        postings: bytesOf(raw.postings),
        pCall: typed(raw.pCall, Uint32Array), pFirst: typed(raw.pFirst, Uint32Array), pLast: typed(raw.pLast, Uint32Array),
        pStart: typed(raw.pStart, Float32Array), pSpk: bytesOf(raw.pSpk), pKind: bytesOf(raw.pKind),
        pLen: typed(raw.pLen, Uint16Array),
        // query stem -> [[vocab index, cosine * 100], ...], strongest first
        related: raw.related || null,
        postingsCache: new Map(),   // term index -> decoded postings
        streams: new Map(),         // pid -> stemmed token stream (phrase checks)
      };
      state.lexical = 'ready';
      state.related = L.related ? 'ready' : 'none';
    }
    // A term's postings as parallel arrays {n, pid, tf, spk}, decoded once.
    function postingsOf(ti) {
      let p = L.postingsCache.get(ti);
      if (p) return p;
      const n = L.termDf[ti];
      p = { n, pid: new Uint32Array(n), tf: new Uint16Array(n), spk: new Uint8Array(n) };
      const bytes = L.postings; let pos = L.termOff[ti]; let pid = 0;
      for (let k = 0; k < n; k++) {
        let delta = 0, shift = 0, b;
        do { b = bytes[pos++]; delta |= (b & 0x7f) << shift; shift += 7; } while (b & 0x80);
        let tf = 0; shift = 0;
        do { b = bytes[pos++]; tf |= (b & 0x7f) << shift; shift += 7; } while (b & 0x80);
        pid += delta;
        p.pid[k] = pid; p.tf[k] = tf; p.spk[k] = bytes[pos++];
      }
      L.postingsCache.set(ti, p);
      return p;
    }
    function idf(ti) { return Math.log(1 + (L.n - L.termDf[ti] + 0.5) / (L.termDf[ti] + 0.5)); }
    function streamOf(pid) {
      let s = L.streams.get(pid);
      if (!s) { s = stemTokens(passageText(pid)); L.streams.set(pid, s); }
      return s;
    }

    // Vocabulary neighbours for a term the index does not know: one edit
    // away (typo tolerance, five letters or more, never digits), or the
    // words it prefixes (the term still being typed).
    function editDistanceOne(a, b) {
      if (Math.abs(a.length - b.length) > 1) return false;
      let i = 0, j = 0, edits = 0;
      while (i < a.length && j < b.length) {
        if (a[i] === b[j]) { i++; j++; continue; }
        if (++edits > 1) return false;
        if (a.length > b.length) i++; else if (a.length < b.length) j++; else { i++; j++; }
      }
      return edits + (a.length - i) + (b.length - j) <= 1;
    }
    function nearTerms(term, isLast) {
      const out = [];
      if (isLast && term.length >= 3) {
        // Sorted vocab: binary search to the first term with the prefix.
        let lo = 0, hi = L.vocab.length;
        while (lo < hi) { const mid = (lo + hi) >> 1; if (L.vocab[mid] < term) lo = mid + 1; else hi = mid; }
        for (let i = lo; i < L.vocab.length && L.vocab[i].startsWith(term) && out.length < 12; i++) out.push(i);
        if (out.length) return out;
      }
      if (term.length >= 5 && !isDigits(term)) {
        for (let i = 0; i < L.vocab.length; i++) {
          const v = L.vocab[i];
          if (v[0] === term[0] && editDistanceOne(term, v)) out.push(i);
        }
      }
      return out;
    }

    // Quoted phrases and the rest (mirrors lexical._split_phrases).
    function splitPhrases(query) {
      const parts = query.replace(/[“”]/g, '"').split('"');
      const phrases = [], rest = [];
      parts.forEach((p, i) => { if (i % 2 === 1 && tokenize(p).length >= 2) phrases.push(p); else rest.push(p); });
      return { phrases, rest: rest.join(' ') };
    }
    function hasPhrases(stream, needles) {
      return needles.every(needle => {
        const n = needle.length;
        outer: for (let i = 0; i + n <= stream.length; i++) {
          for (let j = 0; j < n; j++) if (stream[i + j] !== needle[j]) continue outer;
          return true;
        }
        return false;
      });
    }

    const FUZZY_WEIGHT = 0.7;   // a typo or prefix expansion counts this much of an exact word

    // The parsed query: content terms, each with its expansions into the
    // vocabulary ({ti, weight, related}: the term itself, else its typo or
    // prefix neighbours; in Smart mode its related words too, weighted by
    // cosine), and the stemmed phrases. One entry is cached: the query and
    // the matcher parse the same text within a render.
    let parseCache = { key: null, parsed: null };
    function parseQuery(query, smart) {
      const key = (smart ? 's:' : 'e:') + query + (L ? '|L' : '');
      if (parseCache.key === key) return parseCache.parsed;
      const { phrases, rest } = splitPhrases(query);
      const seen = new Set();
      const terms = [];
      indexTerms(rest).concat(...phrases.map(indexTerms)).forEach(t => { if (!seen.has(t)) { seen.add(t); terms.push(t); } });
      const phraseStreams = phrases.map(stemTokens).filter(p => p.length);
      const phraseTerms = new Set([].concat(...phrases.map(indexTerms)));
      const groups = L ? terms.map((term, i) => {
        const expansions = [];
        if (L.termIndex.has(term)) expansions.push({ ti: L.termIndex.get(term), weight: 1, related: false });
        else if (smart) {
          nearTerms(term, i === terms.length - 1 && !phrases.length)
            .forEach(ti => expansions.push({ ti, weight: FUZZY_WEIGHT, related: false }));
        }
        if (smart && L.related && !phraseTerms.has(term)) {
          (L.related[term] || []).forEach(([ti, cos]) => expansions.push({ ti, weight: cos / 100, related: true }));
        }
        return { term, expansions };
      }) : [];
      const parsed = { terms, groups, phrases: phraseStreams, phraseTerms };
      parseCache = { key, parsed };
      return parsed;
    }

    // BM25 over passages. Returns Map(pid -> {score, matched: Set(term),
    // related: Set(term)}): the query words the passage holds itself, and
    // those it holds only through a related word.
    function lexicalScores(parsed, opts) {
      const acc = new Map();
      const speakers = opts.speakers || 0;
      parsed.groups.forEach(g => {
        // Every expansion of one query word shares its slot: a passage
        // takes the best, and a word it holds itself keeps the slot exact.
        // A related word never weighs more than the typed word (rare
        // related words would otherwise outrank the word that was typed).
        const exact = g.expansions.find(x => x.weight === 1 && !x.related);
        const cap = exact ? idf(exact.ti) : Infinity;
        const best = new Map();  // pid -> {s, related}
        g.expansions.forEach(x => {
          const w = (x.related ? Math.min(idf(x.ti), cap) : idf(x.ti)) * x.weight;
          if (w <= 0) return;
          const p = postingsOf(x.ti);
          for (let k = 0; k < p.n; k++) {
            if (speakers && !(p.spk[k] & speakers)) continue;
            const pid = p.pid[k];
            const dl = L.pLen[pid];
            const s = w * p.tf[k] * (L.k1 + 1) / (p.tf[k] + L.k1 * (1 - L.b + L.b * dl / L.avgdl));
            const cur = best.get(pid);
            if (!cur) best.set(pid, { s, related: x.related });
            else if (s > cur.s) best.set(pid, { s, related: x.related && cur.related });
          }
        });
        best.forEach((v, pid) => {
          let slot = acc.get(pid);
          if (!slot) { slot = { score: 0, matched: new Set(), related: new Set() }; acc.set(pid, slot); }
          slot.score += v.s;
          (v.related ? slot.related : slot.matched).add(g.term);
        });
      });
      const needed = opts.exact ? parsed.terms.length : 1;
      const hits = new Map();
      acc.forEach((s, pid) => {
        if (s.matched.size + s.related.size < needed) return;
        if (parsed.phrases.length) {
          // Only a passage carrying every phrase word itself can hold the phrase.
          for (const t of parsed.phraseTerms) if (!s.matched.has(t)) return;
          if (!hasPhrases(streamOf(pid), parsed.phrases)) return;
        }
        hits.set(pid, s);
      });
      return hits;
    }

    // ── Ranking (the rules tuned on the demo corpus) ────────────────────
    const LEXICAL_CUTOFF = 0.3;      // calls under this share of the top call are dropped (Smart mode)
    const EVIDENCE_PER_CALL = 3;

    /**
     * Run a query. Returns { calls: Map(call index -> {score, evidence}) }.
     * Evidence items are {pid, match, matched, related, needed} (match:
     * 'phrase' | 'words' | 'partial' | 'related'; matched and related count
     * the query words the passage holds itself and through related words);
     * without a search index the page's own data is scanned with the
     * matcher and the items carry {text, start, speaker} instead of a pid.
     * One entry is memoized, so re-renders that do not change the query
     * are free.
     */
    let queryCache = { key: null, result: null };
    function query(text, opts) {
      opts = opts || {};
      const smart = opts.smart !== false;
      const speakers = opts.speakers || 0;
      text = String(text || '').trim();
      const key = [text, smart, speakers, state.lexical].join(' ');
      if (queryCache.key === key) return queryCache.result;
      const result = L ? indexedQuery(text, smart, speakers) : scanQuery(text);
      queryCache = { key, result };
      return result;
    }

    function indexedQuery(text, smart, speakers) {
      const parsed = parseQuery(text, smart);
      if (!parsed.terms.length) return { calls: new Map() };
      const lex = lexicalScores(parsed, { exact: !smart, speakers });

      // Best passage per call (MaxP), cut below a share of the top.
      const best = new Map();
      lex.forEach((s, pid) => {
        const c = L.pCall[pid]; const slot = best.get(c);
        if (!slot || s.score > slot.score) best.set(c, { score: s.score, pid });
      });
      let top = 0; best.forEach(v => { if (v.score > top) top = v.score; });
      if (smart) best.forEach((v, c) => { if (v.score < LEXICAL_CUTOFF * top) best.delete(c); });

      // Evidence per call: the best passages, one per region (passages
      // overlap), those holding the typed words before related-only ones.
      const byCall = new Map();
      lex.forEach((s, pid) => {
        const c = L.pCall[pid];
        if (!best.has(c)) return;
        let arr = byCall.get(c); if (!arr) { arr = []; byCall.set(c, arr); }
        arr.push({ pid, score: s.score, matched: s.matched.size, related: s.related.size });
      });
      const needed = parsed.terms.length;
      const calls = new Map();
      best.forEach((v, c) => {
        const kept = [];
        (byCall.get(c) || []).sort((a, b) => (b.matched - a.matched) || (b.score - a.score)).forEach(e => {
          if (kept.length >= EVIDENCE_PER_CALL) return;
          const overlaps = !isSummary(e.pid) && kept.some(k => !isSummary(k.pid)
            && L.pFirst[e.pid] <= L.pLast[k.pid] && L.pLast[e.pid] >= L.pFirst[k.pid]);
          if (!overlaps) kept.push(e);
        });
        const evidence = kept.map(e => ({
          pid: e.pid, matched: e.matched, related: e.related, needed,
          match: parsed.phrases.length ? 'phrase' : e.related ? 'related' : e.matched < needed ? 'partial' : 'words',
        }));
        calls.set(c, { score: v.score / top, evidence });
      });
      return { calls };
    }

    // No search index (the zip was opened without extracting): scan the
    // page's own data with the matcher. Same result shape, one word tier.
    function scanQuery(text) {
      const mk = matcher(text);
      const calls = new Map();
      if (!mk) return { calls };
      const needed = Math.max(mk.terms.length, 1);
      CALLS.forEach((call, idx) => {
        const evidence = [];
        for (const t of Data.turnsOf(call)) {
          if (evidence.length >= EVIDENCE_PER_CALL) break;
          if (mk.test(t.text)) evidence.push({ text: t.text, start: t.start, speaker: t.speaker, matched: needed, related: 0, needed, match: 'words' });
        }
        const summary = summaryText(call);
        if (evidence.length < EVIDENCE_PER_CALL && mk.test(summary)) {
          evidence.push({ text: summary, start: null, speaker: 'SUMMARY', matched: needed, related: 0, needed, match: 'words' });
        }
        if (evidence.length) calls.set(idx, { score: evidence.length, evidence });
      });
      return { calls };
    }

    // ── Highlighting shared by the index rows, the detail panel, and the
    //    call view's transcript search ─────────────────────────────────
    const WORD_RE = /\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}|\d{3}[-. ]\d{4}|[A-Za-z0-9À-ɏ’']+/g;
    // A matcher for a query: hit(word) says how a display word matches
    // ('' for no match, 'exact' for one of the query's words or its typo
    // and prefix expansions, 'related' for a related word; memoized:
    // display vocabularies are small), test(text) whether a text matches,
    // mark(text) wraps the hits in <mark> (related words get .is-related).
    // A query with no content terms (only stopwords) matches as a substring.
    function matcher(text) {
      const raw = String(text || '').trim();
      if (!raw) return null;
      const parsed = parseQuery(raw, true);
      const exactSet = new Set(parsed.terms);
      const relatedSet = new Set();
      // Expansions mark the vocabulary word they reached.
      parsed.groups.forEach(g => g.expansions.forEach(x => (x.related ? relatedSet : exactSet).add(L.vocab[x.ti])));
      relatedSet.forEach(t => { if (exactSet.has(t)) relatedSet.delete(t); });
      const lower = raw.toLowerCase();
      const memo = new Map();
      const hit = word => {
        const w = word.toLowerCase();
        let h = memo.get(w);
        if (h === undefined) {
          h = '';
          if (!STOPWORDS.has(w) && !FILLER.has(w)) {
            const terms = indexTerms(w);
            if (terms.some(t => exactSet.has(t))) h = 'exact';
            else if (terms.some(t => relatedSet.has(t))) h = 'related';
          }
          memo.set(w, h);
        }
        return h;
      };
      if (!exactSet.size) {
        return { terms: [], hit: () => '',
                 test: s => String(s || '').toLowerCase().indexOf(lower) !== -1,
                 mark: s => highlight(String(s == null ? '' : s), raw) };
      }
      return {
        terms: Array.from(exactSet), hit,
        test(s) { return (String(s || '').match(WORD_RE) || []).some(hit); },
        mark(s) {
          const str = String(s == null ? '' : s);
          let out = ''; let last = 0; let m;
          WORD_RE.lastIndex = 0;
          while ((m = WORD_RE.exec(str)) !== null) {
            out += esc(str.slice(last, m.index));
            const h = hit(m[0]);
            out += h ? '<mark' + (h === 'related' ? ' class="is-related"' : '') + '>' + esc(m[0]) + '</mark>' : esc(m[0]);
            last = m.index + m[0].length;
          }
          return out + esc(str.slice(last));
        },
      };
    }

    const SNIPPET_LEN = 200;
    // The first hit in `text` at or after `from`: an exact hit when there is
    // one, else a related-word hit; -1 for none.
    function firstHit(text, mk, from) {
      let related = -1, m;
      WORD_RE.lastIndex = Math.max(0, from || 0);
      while ((m = WORD_RE.exec(text)) !== null) {
        const h = mk.hit(m[0]);
        if (h === 'exact') return m.index;
        if (h && related === -1) related = m.index;
      }
      return related;
    }
    // A window of `text` around the first hit at or after `at` (whole text
    // when short), with the hits marked.
    function snippet(text, mk, at) {
      at = mk && mk.terms.length ? firstHit(text, mk, at) : -1;
      if (at === -1 || text.length <= SNIPPET_LEN) {
        const cut = text.length > SNIPPET_LEN ? text.slice(0, SNIPPET_LEN).replace(/\s+\S*$/, '') + '…' : text;
        return mk ? mk.mark(cut) : esc(cut);
      }
      let lo = Math.max(0, at - Math.floor(SNIPPET_LEN * 0.35)); let hi = Math.min(text.length, lo + SNIPPET_LEN);
      if (lo > 0) lo = text.indexOf(' ', lo) + 1 || lo;
      if (hi < text.length) hi = text.lastIndexOf(' ', hi);
      return (lo > 0 ? '…' : '') + mk.mark(text.slice(lo, hi)) + (hi < text.length ? '…' : '');
    }
    // Where an evidence item points and what it shows: {start, speaker,
    // html}. For an indexed passage the anchor is the line carrying most of
    // the query's words (a typed word outweighs any number of related
    // words); a summary passage or a scanned summary has no time (start null).
    function evidence(item, mk) {
      if (item.pid == null) return { start: item.start, speaker: item.speaker, html: snippet(item.text, mk, 0) };
      const pid = item.pid;
      if (isSummary(pid)) return { start: null, speaker: 'SUMMARY', html: snippet(passageText(pid), mk, 0) };
      const lines = passageLines(pid);
      let line = lines[0], best = -1, offset = 0, at = 0;
      lines.forEach(l => {
        const t = l.text.trim();
        const n = mk ? (t.match(WORD_RE) || []).reduce((a, w) => { const h = mk.hit(w); return a + (h === 'exact' ? 1000 : h ? 1 : 0); }, 0) : 0;
        if (n > best) { best = n; line = l; at = offset; }
        offset += t.length + 1;
      });
      const text = lines.map(l => l.text.trim()).join(' ');
      return { start: parseFloat(line && line.start) || 0, speaker: line ? line.speaker : '', html: snippet(text, mk, at) };
    }

    function init() {
      const el = document.createElement('script');
      el.src = 'app-assets/index.js';
      el.onload = () => {
        el.remove();
        const raw = window.JCS_SEARCH; window.JCS_SEARCH = null;
        loadLexical(raw);
        notify();
      };
      el.onerror = () => {
        el.remove();
        state.lexical = 'missing';
        notify();
      };
      document.head.appendChild(el);
    }

    return {
      init, query, matcher, evidence, state,
      onChange(fn) { listeners.push(fn); },
      // exposed for the parity and browser tests
      tokenize, indexTerms, stemTokens, stem, summaryText,
      relatedWords(term) { return L && L.related ? (L.related[term] || []).map(([ti, cos]) => [L.vocab[ti], cos]) : []; },
    };
  })();
