  // Review presentation never edits transcript lines, summaries, or PDF links.
  const ReviewUI = (function () {
    const callKey = call => call.audio_filename || call.filename;
    const byKey = new Map(CALLS.map(c => [callKey(c), c]));
    let namingCall = null;
    const setup = document.getElementById('reviewSetup');
    const nameDialog = document.getElementById('phoneNameDialog');
    let notesPage = 1, notesSignature = '';
    let savePromptShown = false;
    const button = (action, call, pressed, text, extra) =>
      '<button type="button" class="review-button" data-review="' + action + '" data-call-key="' + esc(callKey(call))
      + '" aria-pressed="' + pressed + '"' + (extra || '') + '>' + text + '</button>';

    function quick(call, full) {
      return '<span class="review-quick">'
        + button('star', call, !!call.starred, full ? (call.starred ? '★ Starred' : '☆ Star') : (call.starred ? '★' : '☆'), ' aria-label="Star call" title="Star call"')
        + button('reviewed', call, !!call.reviewed, full ? (call.reviewed ? '✓ Reviewed' : 'Mark reviewed') : '✓', ' aria-label="Mark call reviewed" title="Mark call reviewed"') + '</span>';
    }
    function controls(call) {
      return '<div class="review-controls">' + quick(call, true)
        + '<label>Relevance <select class="review-relevance" data-review="relevance" data-call-key="' + esc(callKey(call)) + '">'
        + [['', 'Original (' + (call.original_relevance || 'unrated').toLowerCase() + ')'], ['HIGH','High'], ['MEDIUM','Medium'], ['LOW','Low']]
          .map(([value, label]) => '<option value="' + value + '"' + (Review.get(call, 'relevance', '') === value ? ' selected' : '') + '>' + esc(label) + '</option>').join('')
        + '</select></label>'
        + (Review.get(call, 'relevance', '') ? '<span class="original-rating">Original: ' + esc(call.original_relevance || 'unrated') + '</span>' : '')
        + (Review.phoneKey(call) ? '<button type="button" class="review-button" data-review="name" data-call-key="' + esc(callKey(call)) + '">'
          + (call.outside_name ? 'Edit number name' : 'Name number') + '</button>' : '')
        + '</div>';
    }
    function pinButton(call, cue) {
      const pinned = Review.pinned(call, cue);
      return '<button type="button" class="review-button pin-button" data-review="pin" data-call-key="' + esc(callKey(call))
        + '" data-cue-key="' + esc(cue.id) + '" aria-pressed="' + pinned + '" aria-label="'
        + (pinned ? 'Unpin' : 'Pin') + ' note at ' + esc(cue.ts) + '">' + (pinned ? 'Pinned' : 'Pin note') + '</button>';
    }
    function apply() {
      CALLS.forEach(call => {
        call.relevance = Review.get(call, 'relevance', '') || call.original_relevance;
        call.rel_rank = REL_RANK[call.relevance] || 0;
        call.starred = Review.get(call, 'star', false);
        call.reviewed = Review.get(call, 'reviewed', false);
        call.outside_name = Review.name(call);
      });
    }
    function refresh() {
      const focused = document.activeElement;
      const focusKey = focused && focused.dataset.review ? {...focused.dataset} : null;
      apply();
      document.getElementById('pinCount').textContent = CALLS.reduce((n, c) => n + c.cues.filter(q => Review.pinned(c, q)).length, 0);
      document.getElementById('reviewProgress').textContent = CALLS.filter(c => c.reviewed).length + ' of ' + CALLS.length + ' calls reviewed';
      IndexView.refreshReview();
      CallView.refreshReview();
      if (focusKey) {
        const replacement = Array.from(document.querySelectorAll('[data-review]')).find(el =>
          el.dataset.review === focusKey.review && el.dataset.callKey === focusKey.callKey
          && el.dataset.cueKey === focusKey.cueKey && el.getClientRects().length);
        if (replacement) replacement.focus({preventScroll: true});
      }
    }
    function renderNotes(root, calls) {
      const pins = [];
      calls.forEach(call => call.cues.forEach(cue => { if (Review.pinned(call, cue)) pins.push({call, cue}); }));
      const signature = pins.map(p => callKey(p.call) + ':' + p.cue.id).join('|');
      if (signature !== notesSignature) { notesPage = 1; notesSignature = signature; }
      const pages = Math.max(1, Math.ceil(pins.length / 50));
      notesPage = Math.min(notesPage, pages);
      const shown = pins.slice((notesPage - 1) * 50, notesPage * 50);
      root.innerHTML = '<div class="pinned-header"><h2>Pinned notes</h2><span>' + pins.length + ' matching note' + (pins.length === 1 ? '' : 's') + '</span></div>'
        + (pins.length ? shown.map(({call, cue}) => '<article class="pinned-card">'
          + '<header>' + relMark(call.relevance) + '<span>Call ' + String(call.index + 1).padStart(3, '0') + ' · '
          + esc(fmtDate(call.datetime || call.call_date).d) + ' · ' + esc(call.outside_name ? call.outside_name + ' · ' + call.outside : call.outside || call.filename)
          + '</span>' + pinButton(call, cue) + '</header>'
          + '<h3>' + esc(cue.note || 'Transcript passage') + '</h3>'
          + (cue.quote ? '<blockquote>“' + esc(cue.quote) + '”</blockquote>' : '')
          + '<a href="' + esc(Router.callHash(call, cue.t)) + '">Listen at ' + esc(cue.ts) + '</a>'
          + (cue.line_cite ? ' · Tr. ' + esc(cue.line_cite) : '')
          + (call.pdf_filename ? ' · <a href="' + esc(pdfUrl(call, cue.page)) + '" target="_blank" rel="noopener">Transcript PDF</a>' : '')
          + '</article>').join('')
          : '<p class="pinned-empty">No pinned notes match these filters. Pin a note from a call’s analysis to collect it here.</p>');
      if (pages > 1) {
        const nav = document.createElement('div');
        nav.className = 'pagination';
        nav.innerHTML = '<button data-note-page="-1"' + (notesPage === 1 ? ' disabled' : '') + '>Previous</button>'
          + '<span>Page ' + notesPage + ' of ' + pages + '</span>'
          + '<button data-note-page="1"' + (notesPage === pages ? ' disabled' : '') + '>Next</button>';
        nav.addEventListener('click', e => {
          if (!e.target.dataset.notePage) return;
          notesPage += Number(e.target.dataset.notePage);
          renderNotes(root, calls);
          root.scrollIntoView({block: 'start'});
        });
        root.appendChild(nav);
      }
    }
    function status() {
      const s = Review.status();
      const bar = document.getElementById('reviewSaveBar');
      document.getElementById('reviewSaveStatus').textContent = s.message;
      bar.dataset.error = String(s.error);
      const connect = document.getElementById('reviewConnect');
      connect.textContent = s.connected ? 'Autosave settings' : s.remembered ? 'Reconnect autosave' : 'Enable autosave';
      connect.disabled = !s.supported || s.busy;
      document.getElementById('reviewSetupChoose').hidden = !s.remembered;
      document.getElementById('reviewSetupChoose').disabled = s.busy;
      document.getElementById('reviewSetupOther').disabled = s.busy;
      document.documentElement.style.setProperty('--review-save-height', bar.getBoundingClientRect().height + 'px');
    }
    function showSetup() {
      document.getElementById('reviewSetupError').textContent = '';
      document.getElementById('reviewSetupHelp').textContent = Review.setupHelp;
      if (!setup.open) setup.showModal();
    }
    function ensureSave() {
      const s = Review.status();
      if (!savePromptShown && !s.connected && s.supported) { savePromptShown = true; showSetup(); }
    }
    function init() {
      CALLS.forEach(c => { c.original_relevance = c.relevance; });
      Review.onChange(refresh);
      Review.onStatus(status);
      // Capture review controls before row expansion, cue seeking, or player shortcuts.
      document.addEventListener('click', e => {
        const control = e.target.closest('[data-review]');
        if (!control) return;
        e.stopPropagation();
        const call = byKey.get(control.dataset.callKey);
        if (!call) return;
        const action = control.dataset.review;
        if (action === 'star') Review.set(call, 'star', !call.starred);
        if (action === 'reviewed') Review.set(call, 'reviewed', !call.reviewed);
        if (action === 'pin') {
          const cue = call.cues.find(q => q.id === control.dataset.cueKey);
          if (cue) Review.setPin(call, cue, !Review.pinned(call, cue));
        }
        if (['star', 'reviewed', 'pin'].includes(action)) ensureSave();
        if (action === 'name') {
          namingCall = call;
          document.getElementById('phoneNameNumber').textContent = call.outside;
          document.getElementById('phoneNameScope').value = Review.get(call, 'name', null) === null ? 'number' : 'call';
          updateNameInput();
          nameDialog.showModal();
          document.getElementById('phoneNameInput').focus();
        }
      }, true);
      document.addEventListener('change', e => {
        if (e.target.dataset.review !== 'relevance') return;
        const call = byKey.get(e.target.dataset.callKey);
        if (call) { Review.set(call, 'relevance', e.target.value); ensureSave(); }
      });
      document.getElementById('phoneNameForm').addEventListener('submit', e => {
        e.preventDefault();
        if (namingCall) {
          const name = document.getElementById('phoneNameInput').value.trim();
          if (document.getElementById('phoneNameScope').value === 'call') Review.set(namingCall, 'name', name);
          else Review.setName(namingCall, name);
        }
        nameDialog.close();
        ensureSave();
      });
      function updateNameInput() {
        if (!namingCall) return;
        const perCall = document.getElementById('phoneNameScope').value === 'call';
        document.getElementById('phoneNameInput').value = perCall ? Review.name(namingCall) : Review.numberName(namingCall);
        document.getElementById('phoneNameDefault').hidden = !perCall;
        document.getElementById('phoneNameHelp').textContent = perCall
          ? 'This name takes precedence for this call. Leave blank to show no assigned name, or use the number default.'
          : 'This is the default for calls using this number. Individual call names take precedence. Leave blank to remove the default.';
      }
      document.getElementById('phoneNameScope').addEventListener('change', updateNameInput);
      document.getElementById('phoneNameDefault').addEventListener('click', () => {
        if (namingCall) Review.set(namingCall, 'name', null);
        nameDialog.close();
        ensureSave();
      });
      document.getElementById('phoneNameCancel').addEventListener('click', () => nameDialog.close());
      document.getElementById('reviewConnect').addEventListener('click', showSetup);
      document.getElementById('reviewSetupCancel').addEventListener('click', () => setup.close());
      async function connect(chooseFolder) {
        const error = document.getElementById('reviewSetupError');
        error.textContent = '';
        try { if (await Review.connect(chooseFolder)) setup.close(); }
        catch (e) { error.textContent = e.message; }
      }
      document.getElementById('reviewSetupChoose').addEventListener('click', () => connect(false));
      document.getElementById('reviewSetupOther').addEventListener('click', () => connect(true));
      window.addEventListener('resize', status);
      Review.init();
    }
    return { init, quick, controls, pinButton, renderNotes, refresh };
  })();
