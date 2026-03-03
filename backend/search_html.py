"""
Self-contained searchable HTML page generator.

All transcript data is embedded in a <script> JSON blob.
Vanilla JS search with highlighting and expandable sections.
No external dependencies.
"""

import json
import logging
import os
import re
from typing import List

_RELEVANCE_RE = re.compile(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _build_call_data(calls) -> List[dict]:
    result = []
    for call in calls:
        transcript = ""
        if call.turns:
            transcript = "\n".join(f"{t.speaker}: {t.text}" for t in call.turns)
        mp3_filename = os.path.basename(call.mp3_path) if call.mp3_path else ""
        summary = call.summary or ""
        rel_match = _RELEVANCE_RE.search(summary)
        result.append({
            "index": call.index,
            "filename": call.filename,
            "audio_filename": mp3_filename,
            "duration": call.duration_seconds or 0,
            "summary": summary,
            "relevance": rel_match.group(1).upper() if rel_match else "",
            "transcript": transcript,
            "inmate": call.inmate_name or "",
            "outside": call.outside_number_fmt or "",
            "datetime": call.call_datetime_str or "",
            "call_date": call.call_date or "",
            "facility": call.facility or "",
            "outcome": call.call_outcome or "",
        })
    return result


def generate_search_html(calls, case_name: str = "") -> str:
    call_data = _build_call_data(calls)
    data_json = json.dumps(call_data, ensure_ascii=False)
    title = f"Search – {case_name}" if case_name else "Search Transcripts"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f1f5f9;
      color: #1e293b;
      min-height: 100vh;
    }}
    .header {{
      background: #1e293b;
      color: #fff;
      padding: 20px 32px;
    }}
    .header h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 600; }}
    .header p {{ margin: 0; font-size: 13px; color: rgba(255,255,255,0.6); }}
    .search-bar {{
      padding: 20px 32px;
      background: #fff;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      gap: 12px;
      align-items: center;
    }}
    .search-input {{
      flex: 1;
      padding: 10px 16px;
      font-size: 15px;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      outline: none;
      transition: border-color 0.15s;
    }}
    .search-input:focus {{ border-color: #334155; box-shadow: 0 0 0 3px rgba(51,65,85,.1); }}
    .search-count {{
      font-size: 13px;
      color: #64748b;
      min-width: 120px;
    }}
    .filter-bar {{
      padding: 12px 32px;
      background: #fff;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      gap: 16px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .filter-label {{
      font-size: 12px;
      font-weight: 600;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .filter-input {{
      padding: 6px 10px;
      font-size: 13px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      outline: none;
      color: #1e293b;
    }}
    .filter-input:focus {{ border-color: #334155; }}
    .filter-select {{
      padding: 6px 10px;
      font-size: 13px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      outline: none;
      color: #1e293b;
      background: #fff;
      min-width: 160px;
    }}
    .filter-select:focus {{ border-color: #334155; }}
    .clear-filters-btn {{
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      color: #64748b;
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .clear-filters-btn:hover {{ background: #e2e8f0; color: #334155; }}
    .results {{ padding: 24px 32px; }}
    .call-card {{
      background: #fff;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      margin-bottom: 16px;
      overflow: hidden;
    }}
    .call-header {{
      padding: 14px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      user-select: none;
      gap: 12px;
    }}
    .call-header:hover {{ background: #f8fafc; }}
    .call-title {{
      font-size: 14px;
      font-weight: 600;
      color: #1e293b;
      flex: 1;
    }}
    .call-match-count {{
      font-size: 12px;
      color: #64748b;
      background: #f1f5f9;
      padding: 3px 10px;
      border-radius: 20px;
    }}
    .open-viewer-btn {{
      font-size: 11px;
      color: #3b82f6;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: 6px;
      padding: 3px 10px;
      cursor: pointer;
      text-decoration: none;
      font-weight: 600;
      white-space: nowrap;
      flex-shrink: 0;
      transition: background 0.15s;
    }}
    .open-viewer-btn:hover {{ background: #dbeafe; }}
    .call-chevron {{
      color: #94a3b8;
      transition: transform 0.2s;
      flex-shrink: 0;
    }}
    .call-card.open .call-chevron {{ transform: rotate(180deg); }}
    .call-body {{ display: none; border-top: 1px solid #e2e8f0; padding: 16px 20px; }}
    .call-card.open .call-body {{ display: block; }}
    .summary-box {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 16px;
      font-size: 13px;
      line-height: 1.6;
      color: #334155;
    }}
    .summary-box strong {{ display: block; margin-bottom: 6px; color: #1e293b; }}
    .relevance-badge {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 10px;
      border-radius: 4px;
      margin-bottom: 8px;
      letter-spacing: 0.03em;
    }}
    .relevance-HIGH {{ background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }}
    .relevance-MEDIUM {{ background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }}
    .relevance-LOW {{ background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }}
    .active-filters {{
      font-size: 12px;
      color: #475569;
      padding: 8px 32px 0;
      font-weight: 500;
    }}
    .transcript-lines {{ font-family: 'Courier New', monospace; font-size: 12px; line-height: 1.8; }}
    .transcript-line {{ padding: 2px 0; }}
    .transcript-line .speaker {{ color: #475569; font-weight: 600; }}
    mark {{
      background: #fef08a;
      color: #1e293b;
      border-radius: 2px;
      padding: 0 1px;
    }}
    .no-results {{
      text-align: center;
      padding: 60px 32px;
      color: #94a3b8;
      font-size: 15px;
    }}
    .pagination {{
      padding: 12px 32px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
    }}
    .pagination button {{
      padding: 6px 16px;
      font-size: 13px;
      font-weight: 600;
      color: #334155;
      background: #fff;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .pagination button:hover:not(:disabled) {{ background: #f1f5f9; }}
    .pagination button:disabled {{ opacity: 0.4; cursor: default; }}
    .pagination .page-info {{ font-size: 13px; color: #64748b; }}
    .hidden {{ display: none; }}

    @media print {{
      body {{ background: #fff; }}
      .search-bar, .filter-bar, .pagination, .open-viewer-btn, .call-chevron {{ display: none !important; }}
      .results {{ padding: 0; }}
      .call-card {{ border: 1px solid #ccc; break-inside: avoid; margin-bottom: 12px; }}
      .call-card .call-body {{ display: block !important; border-top: 1px solid #ccc; }}
      .call-header {{ cursor: default; }}
      .transcript-lines {{ font-size: 11px; line-height: 1.5; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>{title}</h1>
    <p>{len(call_data)} calls</p>
  </div>
  <div class="search-bar">
    <input type="text" class="search-input" id="searchInput" placeholder="Search transcripts..." autofocus>
    <div class="search-count" id="searchCount"></div>
  </div>
  <div class="filter-bar">
    <span class="filter-label">Filters:</span>
    <input type="date" class="filter-input" id="dateFrom" title="From date">
    <span style="color:#94a3b8;font-size:13px">to</span>
    <input type="date" class="filter-input" id="dateTo" title="To date">
    <select class="filter-select" id="phoneFilter">
      <option value="">All phone numbers</option>
    </select>
    <select class="filter-select" id="relevanceFilter" style="min-width:140px">
      <option value="">All relevance</option>
      <option value="HIGH">HIGH</option>
      <option value="MEDIUM">MEDIUM</option>
      <option value="LOW">LOW</option>
    </select>
    <button class="clear-filters-btn" id="clearFilters">Clear filters</button>
  </div>
  <div class="active-filters hidden" id="activeFilters"></div>
  <div class="pagination hidden" id="pagination">
    <button id="prevPage">Previous</button>
    <span class="page-info" id="pageInfo"></span>
    <button id="nextPage">Next</button>
  </div>
  <div class="results" id="results"></div>
  <div class="no-results hidden" id="noResults">No matches found.</div>

  <script>
  const CALLS = {data_json};

  function esc(s) {{
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  function highlight(text, query) {{
    if (!query) return esc(text);
    const re = new RegExp('(' + query.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
    return esc(text).replace(re, '<mark>$1</mark>');
  }}

  function countMatches(text, query) {{
    if (!query) return 0;
    const re = new RegExp(query.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&'), 'gi');
    return (text.match(re) || []).length;
  }}

  function relevanceBadge(level) {{
    if (!level) return '';
    return '<span class="relevance-badge relevance-' + level + '">' + level + '</span>';
  }}

  // Populate phone filter dropdown from unique values
  (function populatePhoneFilter() {{
    const phones = new Set();
    CALLS.forEach(c => {{ if (c.outside) phones.add(c.outside); }});
    const sel = document.getElementById('phoneFilter');
    Array.from(phones).sort().forEach(p => {{
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    }});
  }})();

  function getFilteredCalls() {{
    const dateFrom = document.getElementById('dateFrom').value;
    const dateTo = document.getElementById('dateTo').value;
    const phone = document.getElementById('phoneFilter').value;
    const relevance = document.getElementById('relevanceFilter').value;

    return CALLS.filter(call => {{
      if (dateFrom || dateTo) {{
        if (!call.call_date) return false;
        if (dateFrom && call.call_date < dateFrom) return false;
        if (dateTo && call.call_date > dateTo) return false;
      }}
      if (phone && call.outside !== phone) return false;
      if (relevance && call.relevance !== relevance) return false;
      return true;
    }});
  }}

  function updateActiveFilters() {{
    const dateFrom = document.getElementById('dateFrom').value;
    const dateTo = document.getElementById('dateTo').value;
    const phone = document.getElementById('phoneFilter').value;
    const relevance = document.getElementById('relevanceFilter').value;
    const el = document.getElementById('activeFilters');
    const parts = [];
    if (dateFrom) parts.push('From: ' + dateFrom);
    if (dateTo) parts.push('To: ' + dateTo);
    if (phone) parts.push('Phone: ' + phone);
    if (relevance) parts.push('Relevance: ' + relevance);
    if (parts.length) {{
      el.textContent = 'Active filters: ' + parts.join(' · ');
      el.classList.remove('hidden');
    }} else {{
      el.classList.add('hidden');
    }}
  }}

  function openInViewer(audioFilename) {{
    window.open('viewer/index.html?call=' + encodeURIComponent(audioFilename), '_blank');
  }}

  let openStates = {{}};
  const CALLS_PER_PAGE = 25;
  let currentPage = 1;

  function updatePagination(totalItems) {{
    const pag = document.getElementById('pagination');
    const totalPages = Math.ceil(totalItems / CALLS_PER_PAGE);
    if (totalPages <= 1) {{ pag.classList.add('hidden'); return; }}
    pag.classList.remove('hidden');
    document.getElementById('pageInfo').textContent = `Page ${{currentPage}} of ${{totalPages}}`;
    document.getElementById('prevPage').disabled = currentPage <= 1;
    document.getElementById('nextPage').disabled = currentPage >= totalPages;
  }}

  function render(query) {{
    const q = query.trim();
    const container = document.getElementById('results');
    const noResults = document.getElementById('noResults');
    const countEl = document.getElementById('searchCount');
    const filtered = getFilteredCalls();

    let totalMatches = 0;
    let visibleCalls = 0;

    if (!q) {{
      // Show paginated filtered calls collapsed
      const totalPages = Math.ceil(filtered.length / CALLS_PER_PAGE);
      if (currentPage > totalPages) currentPage = Math.max(1, totalPages);
      const startIdx = (currentPage - 1) * CALLS_PER_PAGE;
      const pageSlice = filtered.slice(startIdx, startIdx + CALLS_PER_PAGE);
      updatePagination(filtered.length);

      container.innerHTML = pageSlice.map(call => {{
        const idx = call.index;
        const isOpen = openStates[idx] || false;
        const lines = call.transcript.split('\\n').map(line => {{
          const colonIdx = line.indexOf(':');
          if (colonIdx > 0) {{
            return `<div class="transcript-line"><span class="speaker">${{esc(line.slice(0, colonIdx))}}</span>: ${{esc(line.slice(colonIdx + 1).trim())}}</div>`;
          }}
          return `<div class="transcript-line">${{esc(line)}}</div>`;
        }}).join('');
        const metaParts = [call.datetime, call.inmate, call.facility].filter(Boolean);
        const metaLine = metaParts.length ? `<div style="font-size:11px;color:#64748b;font-weight:400;margin-top:2px">${{esc(metaParts.join(' · '))}}</div>` : '';
        const viewerBtn = call.audio_filename ? `<a class="open-viewer-btn" onclick="event.stopPropagation(); openInViewer('${{call.audio_filename.replace(/'/g, "\\\\'")}}')" title="Open in Viewer">Viewer</a>` : '';
        const rel = call.relevance;
        const badge = relevanceBadge(rel);
        return `<div class="call-card ${{isOpen ? 'open' : ''}}" data-idx="${{idx}}" data-audio="${{esc(call.audio_filename)}}">
          <div class="call-header" onclick="toggleCall(${{idx}}, this.closest('.call-card'))">
            <div class="call-title">${{esc(call.filename)}}${{metaLine}}</div>
            ${{badge}}
            ${{viewerBtn}}
            <svg class="call-chevron" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 6l4 4 4-4"/></svg>
          </div>
          <div class="call-body">
            ${{call.summary ? `<div class="summary-box"><strong>Summary</strong>${{esc(call.summary)}}</div>` : ''}}
            <div class="transcript-lines">${{lines}}</div>
          </div>
        </div>`;
      }}).join('');
      const showStart = filtered.length > 0 ? (currentPage - 1) * CALLS_PER_PAGE + 1 : 0;
      const showEnd = Math.min(currentPage * CALLS_PER_PAGE, filtered.length);
      countEl.textContent = filtered.length <= CALLS_PER_PAGE
        ? `${{filtered.length}} call${{filtered.length === 1 ? '' : 's'}}`
        : `Showing ${{showStart}}-${{showEnd}} of ${{filtered.length}} calls`;
      noResults.classList.add('hidden');
      return;
    }}

    const html = filtered.map(call => {{
      const idx = call.index;
      const searchText = call.transcript + ' ' + call.summary + ' ' + call.inmate + ' ' + call.outside;
      const count = countMatches(searchText, q);
      if (count === 0) return '';
      totalMatches += count;
      visibleCalls++;

      const isOpen = openStates[idx] !== false; // default open when searching

      const lines = call.transcript.split('\\n').map(line => {{
        if (!line.toLowerCase().includes(q.toLowerCase())) {{
          return '';
        }}
        const colonIdx = line.indexOf(':');
        if (colonIdx > 0) {{
          return `<div class="transcript-line"><span class="speaker">${{esc(line.slice(0, colonIdx))}}</span>: ${{highlight(line.slice(colonIdx + 1).trim(), q)}}</div>`;
        }}
        return `<div class="transcript-line">${{highlight(line, q)}}</div>`;
      }}).filter(Boolean).join('');

      const summaryHtml = call.summary
        ? `<div class="summary-box"><strong>Summary</strong>${{highlight(call.summary, q)}}</div>`
        : '';

      const metaPartsS = [call.datetime, call.inmate, call.facility].filter(Boolean);
      const metaLineS = metaPartsS.length ? `<div style="font-size:11px;color:#64748b;font-weight:400;margin-top:2px">${{esc(metaPartsS.join(' · '))}}</div>` : '';
      const viewerBtnS = call.audio_filename ? `<a class="open-viewer-btn" onclick="event.stopPropagation(); openInViewer('${{call.audio_filename.replace(/'/g, "\\\\'")}}')" title="Open in Viewer">Viewer</a>` : '';
      const relS = call.relevance;
      const badgeS = relevanceBadge(relS);
      return `<div class="call-card ${{isOpen ? 'open' : ''}}" data-idx="${{idx}}" data-audio="${{esc(call.audio_filename)}}">
        <div class="call-header" onclick="toggleCall(${{idx}}, this.closest('.call-card'))">
          <div class="call-title">${{esc(call.filename)}}${{metaLineS}}</div>
          ${{badgeS}}
          <span class="call-match-count">${{count}} match${{count === 1 ? '' : 'es'}}</span>
          ${{viewerBtnS}}
          <svg class="call-chevron" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 6l4 4 4-4"/></svg>
        </div>
        <div class="call-body">
          ${{summaryHtml}}
          <div class="transcript-lines">${{lines || '<div style="color:#94a3b8;font-size:12px">Matches in summary only</div>'}}</div>
        </div>
      </div>`;
    }}).filter(Boolean).join('');

    container.innerHTML = html;
    document.getElementById('pagination').classList.add('hidden');

    if (visibleCalls === 0) {{
      noResults.classList.remove('hidden');
      countEl.textContent = '';
    }} else {{
      noResults.classList.add('hidden');
      countEl.textContent = `${{totalMatches}} match${{totalMatches === 1 ? '' : 'es'}} in ${{visibleCalls}} call${{visibleCalls === 1 ? '' : 's'}}`;
    }}
  }}

  function toggleCall(idx, card) {{
    const isOpen = card.classList.toggle('open');
    openStates[idx] = isOpen;
  }}

  // Double-click on a call card opens viewer
  document.getElementById('results').addEventListener('dblclick', function(e) {{
    const card = e.target.closest('.call-card');
    if (!card) return;
    const audio = card.getAttribute('data-audio');
    if (audio) openInViewer(audio);
  }});

  let debounce = null;
  document.getElementById('searchInput').addEventListener('input', e => {{
    clearTimeout(debounce);
    currentPage = 1;
    debounce = setTimeout(() => render(e.target.value), 200);
  }});

  // Pagination buttons
  document.getElementById('prevPage').addEventListener('click', () => {{
    if (currentPage > 1) {{ currentPage--; render(document.getElementById('searchInput').value); }}
  }});
  document.getElementById('nextPage').addEventListener('click', () => {{
    currentPage++;
    render(document.getElementById('searchInput').value);
  }});

  // Filter change listeners (reset page on filter change)
  function onFilterChange() {{ currentPage = 1; updateActiveFilters(); render(document.getElementById('searchInput').value); }}
  document.getElementById('dateFrom').addEventListener('change', onFilterChange);
  document.getElementById('dateTo').addEventListener('change', onFilterChange);
  document.getElementById('phoneFilter').addEventListener('change', onFilterChange);
  document.getElementById('relevanceFilter').addEventListener('change', onFilterChange);
  document.getElementById('clearFilters').addEventListener('click', () => {{
    document.getElementById('dateFrom').value = '';
    document.getElementById('dateTo').value = '';
    document.getElementById('phoneFilter').value = '';
    document.getElementById('relevanceFilter').value = '';
    currentPage = 1;
    updateActiveFilters();
    render(document.getElementById('searchInput').value);
  }});

  render('');
  </script>
</body>
</html>"""
