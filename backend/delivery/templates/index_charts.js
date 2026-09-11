  // ═══════════════════════════════════════════════════════════════════════
  // Charts: the call index's calendar and timeline modes.
  //
  // This file is a Jinja include; it lands inside index.html's IIFE right
  // after the shared helpers, so it can use MONTHS_SHORT / MONTHS_LONG and
  // nothing else from the page. Everything here is plain DOM and inline SVG
  // (no library, works from file://). Colors and sizes come from the design
  // layer: the .mk tier marks and .tint-N ramp in components.css, the
  // --tint / --chart-mute / --signal / --rule tokens in tokens.css.
  //
  // Contract (both renderers):
  //   calls   the calls to draw (the index's current filtered set)
  //   extent  {start, end} 'YYYY-MM-DD' of ALL dated calls in the delivery
  //           (Charts.extent(Data.calls)); the frame is fixed to it, so
  //           filters change the marks, never the months or the axis
  //   maxPerDay  busiest unfiltered day, so the calendar's tint legend holds
  //   onDay / onRange  click-through: the index applies a date range (and a
  //           tier) and shows the list
  // Calls without a call_date are counted and reported, never drawn.
  //
  // Bucket rules mirror backend/delivery/case_report._build_timeline so the
  // page and the case report agree: days up to 60 days, 7-day weeks up to
  // 392, calendar months up to 1860, calendar years beyond.
  // ═══════════════════════════════════════════════════════════════════════
  const Charts = (function () {
    const DOW = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
    const DOW_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const TIER_RANK = { HIGH: 3, MEDIUM: 2, LOW: 1 };
    const KEY_RE = /^(\d{4})-(\d{2})-(\d{2})/;

    // ── dates: 'YYYY-MM-DD' keys in and out, local Date objects inside ──
    function keyToDate(key) {
      const m = KEY_RE.exec(String(key || ''));
      return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    }
    function dateToKey(d) {
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }
    function addDays(d, n) { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); x.setDate(x.getDate() + n); return x; }
    function dayDiff(a, b) { return Math.round((b - a) / 86400000); }
    function dayLabel(d) { return MONTHS_SHORT[d.getMonth()] + ' ' + d.getDate(); }
    function dayLabelYear(d) { return dayLabel(d) + ', ' + d.getFullYear(); }
    function longDayLabel(d) { return DOW_LONG[d.getDay()] + ', ' + MONTHS_LONG[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear(); }
    function clockLabel(call) {
      const m = /(\d{2}):(\d{2})$/.exec(String(call.datetime || ''));
      if (!m) return '';
      let h = +m[1]; const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
      return h + ':' + m[2] + ' ' + ap;
    }
    function el(tag, cls, text) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function svgEl(tag, attrs) {
      const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      return e;
    }
    function svgText(x, y, text, cls, anchor) {
      const t = svgEl('text', { x, y, 'text-anchor': anchor || 'start' });
      if (cls) t.setAttribute('class', cls);
      t.textContent = text;
      return t;
    }
    function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }
    function mark(tier) { return el('span', 'mk mk--' + (tier ? tier.toLowerCase() : 'none')); }

    // ── data ──
    function dated(calls) { return calls.filter(c => KEY_RE.test(String(c.call_date || ''))); }
    function extent(calls) {
      const keys = dated(calls).map(c => c.call_date.slice(0, 10)).sort();
      return keys.length ? { start: keys[0], end: keys[keys.length - 1] } : null;
    }
    function groupByDay(calls) {
      const map = new Map();
      dated(calls).forEach(c => {
        const k = c.call_date.slice(0, 10);
        if (!map.has(k)) map.set(k, []);
        map.get(k).push(c);
      });
      map.forEach(list => list.sort((a, b) => String(a.datetime).localeCompare(String(b.datetime))));
      return map;
    }
    function maxPerDay(calls) {
      let max = 0;
      groupByDay(calls).forEach(list => { if (list.length > max) max = list.length; });
      return max;
    }
    function tierCounts(list) {
      const t = { n: list.length, high: 0, medium: 0, low: 0 };
      list.forEach(c => { if (c.relevance === 'HIGH') t.high++; else if (c.relevance === 'MEDIUM') t.medium++; else if (c.relevance === 'LOW') t.low++; });
      return t;
    }
    function byTierThenTime(list) {
      return list.slice().sort((a, b) => (TIER_RANK[b.relevance] || 0) - (TIER_RANK[a.relevance] || 0) || String(a.datetime).localeCompare(String(b.datetime)));
    }
    // Tint step 1..4 for n calls on a day: the count itself while the busiest
    // day has four or fewer, else scaled to the busiest day.
    function tintLevel(n, max) {
      if (!n) return 0;
      if (max <= 4) return Math.min(4, n);
      return Math.max(1, Math.round(n / max * 4));
    }

    // ── buckets (same thresholds as case_report._build_timeline) ──
    function granularity(spanDays) {
      if (spanDays <= 60) return 'day';
      if (spanDays <= 392) return 'week';
      if (spanDays <= 1860) return 'month';
      return 'year';
    }
    function buildBuckets(ext) {
      const start = keyToDate(ext.start), end = keyToDate(ext.end);
      const unit = granularity(dayDiff(start, end) + 1);
      const buckets = [];
      if (unit === 'day' || unit === 'week') {
        const step = unit === 'day' ? 1 : 7;
        for (let cur = start; cur <= end; cur = addDays(cur, step)) {
          const last = addDays(cur, step - 1);
          buckets.push({ start: cur, end: last < end ? last : end });
        }
      } else if (unit === 'month') {
        for (let cur = new Date(start.getFullYear(), start.getMonth(), 1); cur <= end; cur = new Date(cur.getFullYear(), cur.getMonth() + 1, 1)) {
          const last = new Date(cur.getFullYear(), cur.getMonth() + 1, 0);
          buckets.push({ start: cur, end: last < end ? last : end });
        }
      } else {
        for (let y = start.getFullYear(); y <= end.getFullYear(); y++) {
          const last = new Date(y, 11, 31);
          buckets.push({ start: new Date(y, 0, 1), end: last < end ? last : end });
        }
      }
      buckets.forEach(b => {
        b.startKey = dateToKey(b.start);
        b.endKey = dateToKey(b.end);
        b.calls = [];
        if (unit === 'day') { b.label = dayLabel(b.start); b.title = longDayLabel(b.start); }
        else if (unit === 'week') { b.label = dayLabel(b.start); b.title = 'Week of ' + dayLabel(b.start) + ' – ' + dayLabelYear(b.end); }
        else if (unit === 'month') { b.label = MONTHS_SHORT[b.start.getMonth()] + ' ' + b.start.getFullYear(); b.title = MONTHS_LONG[b.start.getMonth()] + ' ' + b.start.getFullYear(); }
        else { b.label = String(b.start.getFullYear()); b.title = b.label; }
      });
      return { unit, buckets };
    }
    function bucketize(calls, ext) {
      const built = buildBuckets(ext);
      const byKey = groupByDay(calls);
      built.buckets.forEach(b => {
        for (let cur = b.start; cur <= b.end; cur = addDays(cur, 1)) {
          const list = byKey.get(dateToKey(cur));
          if (list) b.calls.push.apply(b.calls, list);
        }
      });
      return built;
    }

    // ── tooltip: one element for every chart, built with textContent ──
    let tipEl = null;
    function tip() {
      if (!tipEl) { tipEl = el('div', 'chart-tip'); tipEl.hidden = true; document.body.appendChild(tipEl); }
      return tipEl;
    }
    function placeTip(x, y) {
      const t = tip(); const r = t.getBoundingClientRect();
      let left = x + 14, top = y + 16;
      if (left + r.width > window.innerWidth - 10) left = x - r.width - 14;
      if (top + r.height > window.innerHeight - 10) top = y - r.height - 12;
      t.style.left = Math.max(6, left) + 'px';
      t.style.top = Math.max(6, top) + 'px';
    }
    function showTip(x, y, build) {
      const t = tip(); t.replaceChildren(); build(t); t.hidden = false; placeTip(x, y);
    }
    function hideTip() { if (tipEl) tipEl.hidden = true; }
    function bindTip(node, build) {
      node.addEventListener('mouseenter', e => showTip(e.clientX, e.clientY, build));
      node.addEventListener('mousemove', e => placeTip(e.clientX, e.clientY));
      node.addEventListener('mouseleave', hideTip);
      node.addEventListener('focus', () => { const r = node.getBoundingClientRect(); showTip(r.left + r.width / 2, r.bottom, build); });
      node.addEventListener('blur', hideTip);
    }
    function tipCalls(box, title, list, withDate) {
      box.appendChild(el('div', 'ct-head', title));
      const t = tierCounts(list);
      const counts = [];
      if (t.high) counts.push(t.high + ' high');
      if (t.medium) counts.push(t.medium + ' medium');
      if (t.low) counts.push(t.low + ' low');
      box.appendChild(el('div', 'ct-sub', plural(t.n, 'call') + (counts.length ? ' · ' + counts.join(' · ') : '')));
      byTierThenTime(list).slice(0, 4).forEach(c => {
        const row = el('div', 'ct-call');
        const top = el('div', 'ct-top');
        top.appendChild(mark(c.relevance));
        const when = (withDate ? dayLabel(keyToDate(c.call_date)) + ' · ' : '') + (clockLabel(c) || '') + (c.duration_str ? ' · ' + c.duration_str : '');
        top.appendChild(el('span', 'ct-when', when.replace(/^ · /, '')));
        if (c.outside) top.appendChild(el('span', 'ct-who', c.outside_name ? c.outside_name + ' · ' + c.outside : c.outside));
        row.appendChild(top);
        if (c.starred || c.reviewed) row.appendChild(el('div', 'ct-sub', [c.starred ? '★ Starred' : '', c.reviewed ? '✓ Reviewed' : ''].filter(Boolean).join(' · ')));
        if (c.brief) row.appendChild(el('div', 'ct-brief', c.brief));
        box.appendChild(row);
      });
      if (list.length > 4) box.appendChild(el('div', 'ct-more', '… and ' + plural(list.length - 4, 'more call')));
    }

    // ── chart header: a label on the left, the legend on the right ──
    function header(container, title, legendItems) {
      const head = el('div', 'chart-head');
      head.appendChild(el('span', 'lbl lbl--ink', title));
      const legend = el('div', 'legend');
      legendItems.forEach(item => {
        const li = el('span', 'li');
        if (item.ramp) {
          li.append(item.before || '');
          const ramp = el('span', 'ramp');
          for (let i = 1; i <= item.ramp; i++) ramp.appendChild(el('i', 'tint-' + i));
          li.appendChild(ramp);
          li.append(item.after || '');
        } else {
          if (item.tier) li.appendChild(mark(item.tier));
          if (item.swatch) { const sw = el('i', 'swatch'); sw.style.background = item.swatch; li.appendChild(sw); }
          li.append(item.text);
        }
        legend.appendChild(li);
      });
      head.appendChild(legend);
      container.appendChild(head);
      return head;
    }
    function note(container, text) { container.appendChild(el('div', 'chart-note', text)); }
    function undatedNote(container, calls) {
      const n = calls.length - dated(calls).length;
      if (n) note(container, plural(n, 'call') + ' without a date ' + (n === 1 ? 'appears' : 'appear') + ' only in the list.');
    }

    // ═════════════════════════════════════════════════════════════════════
    // Calendar: month grids, one cell per day, tinted by calls that day.
    // A span of up to twelve months is shown whole; a longer one shows one
    // year at a time behind a year switcher (opts.year / opts.onYear), with
    // only the months the span covers.
    // ═════════════════════════════════════════════════════════════════════
    function calendar(container, opts) {
      container.replaceChildren();
      const ext = opts.extent;
      if (!ext) { note(container, 'No call dates in this delivery.'); return; }
      const start = keyToDate(ext.start), end = keyToDate(ext.end);
      const max = Math.max(1, opts.maxPerDay || 0);
      const byDay = groupByDay(opts.calls);

      const legend = [
        max > 1 ? { ramp: Math.min(4, max), before: '1', after: max + ' calls a day' } : { ramp: 1, after: '1 call a day' },
        { tier: 'HIGH', text: 'High call that day' },
        { tier: 'MEDIUM', text: 'Medium' },
      ];
      header(container, 'Calls by day', legend);
      undatedNote(container, opts.calls);
      if (!byDay.size) note(container, 'No calls match the current filters.');

      // Which months: the whole span, or one year of it.
      const months = [];
      for (let cur = new Date(start.getFullYear(), start.getMonth(), 1); cur <= end; cur = new Date(cur.getFullYear(), cur.getMonth() + 1, 1)) months.push(cur);
      let shown = months;
      if (months.length > 12) {
        const years = [];
        for (let y = start.getFullYear(); y <= end.getFullYear(); y++) years.push(y);
        const perYear = new Map(years.map(y => [y, 0]));
        byDay.forEach((list, key) => { const y = +key.slice(0, 4); if (perYear.has(y)) perYear.set(y, perYear.get(y) + list.length); });
        let year = years.indexOf(opts.year) !== -1 ? opts.year : (years.find(y => perYear.get(y) > 0) || years[0]);
        const chips = el('div', 'chip-group year-group');
        years.forEach(y => {
          const b = el('button', 'chip' + (y === year ? ' active' : ''));
          b.type = 'button';
          b.dataset.year = y;
          b.append(String(y));
          b.appendChild(el('small', null, ' · ' + perYear.get(y)));
          b.addEventListener('click', () => { if (opts.onYear) opts.onYear(y); });
          chips.appendChild(b);
        });
        container.appendChild(chips);
        // Only the months inside the span: a first or last year the calls
        // merely touch shows those months, not a page of empty ones.
        shown = months.filter(m => m.getFullYear() === year);
      }

      const grid = el('div', 'months');
      shown.forEach(m => {
        const box = el('div', 'month');
        box.appendChild(el('h3', null, MONTHS_LONG[m.getMonth()] + ' ' + m.getFullYear()));
        const g = el('div', 'month-grid');
        DOW.forEach(d => g.appendChild(el('div', 'dow lbl', d)));
        for (let i = 0; i < m.getDay(); i++) g.appendChild(el('div', 'day pad'));
        const days = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
        for (let d = 1; d <= days; d++) {
          const date = new Date(m.getFullYear(), m.getMonth(), d);
          const key = dateToKey(date);
          const list = byDay.get(key);
          const cell = el('div', 'day');
          cell.dataset.date = key;
          cell.appendChild(el('span', 'n', String(d)));
          if (date < start || date > end) cell.classList.add('out');
          if (list) {
            const t = tierCounts(list);
            cell.classList.add('has', 'tint-' + tintLevel(t.n, max));
            const mks = el('span', 'mks');
            if (t.high) for (let i = 0; i < Math.min(t.high, 3); i++) mks.appendChild(mark('HIGH'));
            else if (t.medium) mks.appendChild(mark('MEDIUM'));
            cell.appendChild(mks);
            cell.tabIndex = 0;
            cell.title = plural(t.n, 'call');
            cell.addEventListener('click', () => { hideTip(); if (opts.onDay) opts.onDay(key, list); });
            cell.addEventListener('keydown', e => { if (e.key === 'Enter') cell.click(); });
            bindTip(cell, box => tipCalls(box, longDayLabel(date), list, false));
          }
          g.appendChild(cell);
        }
        box.appendChild(g);
        grid.appendChild(box);
      });
      container.appendChild(grid);
    }

    // ═════════════════════════════════════════════════════════════════════
    // Timeline: calls per bucket as columns (HIGH in crimson at the base,
    // the rest in the neutral tint), then one lane each for HIGH and MEDIUM
    // with a mark per bucket. Redrawn on resize while visible.
    // ═════════════════════════════════════════════════════════════════════
    const UNIT_WORD = { day: 'day', week: 'week', month: 'month', year: 'year' };

    function timeline(container, opts) {
      container.replaceChildren();
      const ext = opts.extent;
      if (!ext) { note(container, 'No call dates in this delivery.'); return; }
      const built = bucketize(opts.calls, ext);
      header(container, 'Call timeline', [
        { swatch: 'var(--signal)', text: 'High calls' },
        { swatch: 'var(--chart-mute)', text: 'All other calls' },
        { text: 'by ' + UNIT_WORD[built.unit] },
      ]);
      undatedNote(container, opts.calls);
      if (!dated(opts.calls).length) note(container, 'No calls match the current filters.');
      const host = el('div', 'tl');
      container.appendChild(host);
      const draw = () => drawTimeline(host, built, opts);
      draw();
      container._redraw = draw;
    }
    // One listener for every timeline on the page; each container keeps its
    // own draw closure and only redraws while it is laid out.
    let resizeTimer = 0;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        document.querySelectorAll('.tl').forEach(host => {
          const c = host.parentNode;
          if (c && c._redraw && host.clientWidth) c._redraw();
        });
      }, 120);
    });

    function drawTimeline(host, built, opts) {
      host.replaceChildren();
      const buckets = built.buckets, n = buckets.length, unit = built.unit;
      const W = Math.max(640, host.clientWidth || 1000);
      const gutter = 84, padR = 14;
      const x0 = gutter, x1 = W - padR;
      const slot = (x1 - x0) / n;
      const cx = i => x0 + (i + 0.5) * slot;

      const volTop = 22, volH = 70, volBase = volTop + volH;
      const axisH = 36;
      const lanesTop = volBase + axisH, laneH = 26;
      const lanes = [{ tier: 'HIGH', label: 'High' }, { tier: 'MEDIUM', label: 'Medium' }];
      const H = lanesTop + laneH * lanes.length + 6;

      const s = svgEl('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, class: 'tl-svg', role: 'img' });

      // Lane bands and labels
      lanes.forEach((lane, i) => {
        const y = lanesTop + i * laneH;
        if (i % 2 === 0) s.appendChild(svgEl('rect', { x: 0, y, width: W, height: laneH, fill: 'var(--cream)' }));
        s.appendChild(svgText(0, y + laneH / 2 + 4, lane.label.toUpperCase(), 'lane-lbl'));
      });
      s.appendChild(svgText(0, volBase - 3, 'CALLS', 'lane-lbl'));

      // Boundaries: months across days/weeks, years across months
      const boundaries = [];
      buckets.forEach((b, i) => {
        if (i === 0) return;
        const prev = buckets[i - 1].start;
        if ((unit === 'day' && b.start.getDate() === 1) || (unit === 'week' && b.start.getMonth() !== prev.getMonth())) {
          boundaries.push({ i, text: MONTHS_SHORT[b.start.getMonth()].toUpperCase() + (b.start.getMonth() === 0 ? ' ' + b.start.getFullYear() : '') });
        } else if (unit === 'month' && b.start.getMonth() === 0) {
          boundaries.push({ i, text: String(b.start.getFullYear()) });
        }
      });
      boundaries.forEach(bd => {
        const x = x0 + bd.i * slot;
        s.appendChild(svgEl('line', { x1: x, y1: volTop, x2: x, y2: H - 6, stroke: 'var(--rule)', 'stroke-width': 1 }));
        s.appendChild(svgText(x + 5, volBase + 30, bd.text, 'boundary'));
      });

      // Columns
      let maxN = 0;
      buckets.forEach(b => { if (b.calls.length > maxN) maxN = b.calls.length; });
      const bw = Math.min(22, Math.max(3, slot * 0.72));
      let peakDrawn = false;
      buckets.forEach((b, i) => {
        if (!b.calls.length) return;
        const t = tierCounts(b.calls);
        const x = cx(i) - bw / 2;
        const hAll = Math.max(2, t.n / maxN * volH);
        const hHigh = t.high ? Math.max(2, t.high / maxN * volH) : 0;
        const g = svgEl('g', { class: 'col' });
        if (t.n - t.high) {
          const h = t.high ? Math.max(1, hAll - hHigh - 1) : hAll;
          g.appendChild(svgEl('rect', { class: 'bar', x, y: volBase - hAll, width: bw, height: h, rx: 1.5, fill: 'var(--chart-mute)' }));
        }
        if (t.high) g.appendChild(svgEl('rect', { class: 'bar', x, y: volBase - hHigh, width: bw, height: hHigh, rx: 1.5, fill: 'var(--signal)' }));
        if (t.n === maxN && !peakDrawn) { s.appendChild(svgText(cx(i), volBase - hAll - 6, String(t.n), 'peak', 'middle')); peakDrawn = true; }
        const hit = svgEl('rect', { class: 'hit', x: cx(i) - Math.max(bw, 14) / 2, y: volTop - 8, width: Math.max(bw, 14), height: volH + 8, tabindex: 0 });
        bindTip(hit, box => tipCalls(box, b.title, b.calls, unit !== 'day'));
        hit.addEventListener('click', () => { hideTip(); if (opts.onRange) opts.onRange(b.startKey, b.endKey, ''); });
        hit.addEventListener('keydown', e => { if (e.key === 'Enter') hit.dispatchEvent(new Event('click')); });
        g.appendChild(hit);
        s.appendChild(g);
      });

      // Axis: baseline and sparse labels (at most about twelve)
      s.appendChild(svgEl('line', { x1: x0, y1: volBase + 0.5, x2: x1, y2: volBase + 0.5, stroke: 'var(--ink)', 'stroke-width': 1 }));
      const every = Math.max(1, Math.ceil(n / 12));
      buckets.forEach((b, i) => {
        if (i % every) return;
        s.appendChild(svgEl('line', { x1: cx(i), y1: volBase, x2: cx(i), y2: volBase + 5, stroke: 'var(--ink)', 'stroke-width': 1 }));
        s.appendChild(svgText(cx(i), volBase + 17, b.label, 'tick', 'middle'));
      });

      // Tier lanes: one mark per bucket, the count beside it past one
      lanes.forEach((lane, li) => {
        const cy = lanesTop + li * laneH + laneH / 2;
        buckets.forEach((b, i) => {
          const list = b.calls.filter(c => c.relevance === lane.tier);
          if (!list.length) return;
          const x = cx(i);
          const g = svgEl('g', { class: 'm' });
          if (lane.tier === 'HIGH') {
            g.appendChild(svgEl('rect', { class: 'core', x: x - 4.5, y: cy - 4.5, width: 9, height: 9, rx: 1, fill: 'var(--signal)' }));
          } else {
            g.appendChild(svgEl('rect', { class: 'core', x: x - 4, y: cy - 4, width: 8, height: 8, rx: 1, fill: 'var(--paper)', stroke: 'var(--med)', 'stroke-width': 1.5 }));
          }
          if (list.length > 1) s.appendChild(svgText(x + 7, cy + 3.5, String(list.length), 'count'));
          const hit = svgEl('rect', { class: 'hit', x: x - Math.max(11, slot / 2), y: cy - laneH / 2, width: Math.max(22, slot), height: laneH, tabindex: 0 });
          bindTip(hit, box => tipCalls(box, b.title + ' · ' + lane.label, list, unit !== 'day'));
          hit.addEventListener('click', () => { hideTip(); if (opts.onRange) opts.onRange(b.startKey, b.endKey, lane.tier); });
          hit.addEventListener('keydown', e => { if (e.key === 'Enter') hit.dispatchEvent(new Event('click')); });
          g.appendChild(hit);
          s.appendChild(g);
        });
      });

      host.appendChild(s);
    }

    return { calendar, timeline, extent, dated, maxPerDay, granularity, buildBuckets, bucketize, tintLevel };
  })();
