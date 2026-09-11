"""Offline review UI and persistence contract from file://. A file-handle double
stages writes until close and persists between navigations; native IndexedDB is
used with a serializable stand-in for the handle. Native OS picker/permission
dialogs require a separate manual check on the recipient browser.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

FAKE_FOLDER = r"""
(() => {
  class ReviewFile {
    constructor(name) { this.name = name; this.kind = 'file'; }
    async getFile() {
      const text = sessionStorage.getItem('test-file-' + this.name);
      if (text === null) throw new DOMException('Missing file', 'NotFoundError');
      return new File([text], this.name);
    }
    async createWritable() {
      if (window.failReviewWrites) throw new DOMException('Disk write denied', 'NotAllowedError');
      let pending = '';
      const name = this.name;
      return {
        write: async text => { pending = text; if(window.testWriteDelay) await new Promise(r => setTimeout(r, window.testWriteDelay)); },
        close: async () => { sessionStorage.setItem('test-file-' + name, pending); },
        abort: async () => {},
      };
    }
  }
  const folder = {
    kind: 'directory', name: 'delivery',
    queryPermission: async () => window.testPermission || 'granted',
    requestPermission: async () => window.testPermission || 'granted',
    getFileHandle: async (name, options) => {
      if (name === 'index.html' && sessionStorage.getItem('test-file-index.html') === null) {
        sessionStorage.setItem('test-file-index.html', document.head.innerHTML);
      }
      if (sessionStorage.getItem('test-file-' + name) === null) {
        if (!options || !options.create) throw new DOMException('Missing file', 'NotFoundError');
        sessionStorage.setItem('test-file-' + name, '');
      }
      return new ReviewFile(name);
    },
  };
  window.testReviewFolder = folder;
  window.showDirectoryPicker = async () => folder;
  const put = IDBObjectStore.prototype.put, get = IDBObjectStore.prototype.get;
  const resultGetter = Object.getOwnPropertyDescriptor(IDBRequest.prototype, 'result').get;
  IDBObjectStore.prototype.put = function(value, key) {
    return put.call(this, value === folder ? {testFolder: true} : value, key);
  };
  IDBObjectStore.prototype.get = function(key) {
    const request = get.call(this, key);
    Object.defineProperty(request, 'result', {get() {
      const value = resultGetter.call(request);
      return value && value.testFolder ? folder : value;
    }});
    return request;
  };
})();
"""


@pytest.fixture
def review_page(page):
    page.wait_for_function("JCS.Review.status().ready")
    page.context.add_init_script(FAKE_FOLDER)
    page.reload()
    page.wait_for_function("JCS.Review.status().ready")
    return page


def connect(page):
    page.click('#reviewConnect')
    page.click('#reviewSetupOther')
    expect(page.locator('#reviewSetup')).not_to_be_visible()
    page.wait_for_function("JCS.Review.status().connected && !JCS.Review.status().dirty")


def disk(page):
    return page.evaluate("""async () => JSON.parse(await (await (await testReviewFolder.getFileHandle('case-review.json')).getFile()).text())""")


def first_row(page):
    return page.locator('#rows .row').first


def open_detail(page):
    first_row(page).locator('.date').click()
    return page.locator('#rows .detail')


def set_name(page, name, scope='number'):
    page.locator('[data-review=name]:visible').first.click()
    page.select_option('#phoneNameScope', scope)
    page.fill('#phoneNameInput', name)
    page.click('#phoneNameForm button[type=submit]')


def test_direct_save_reload_and_browser_backup(review_page):
    page = review_page
    connect(page)
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    saved = disk(page)
    assert any(cell[2] is True for cell in saved['records'].values())
    cached = page.evaluate("JSON.parse(localStorage.getItem('jcs_review_' + document.querySelector('[name=jcs-review-id]').content))")
    assert cached == saved
    # The stored handle is restored through IndexedDB without invoking a picker.
    page.reload()
    page.wait_for_function('JCS.Review.status().connected && !JCS.Review.status().dirty')
    expect(first_row(page).locator('[data-review=star]')).to_have_attribute('aria-pressed', 'true')


def test_marks_filters_and_original_relevance(review_page):
    page = review_page
    connect(page)
    row = first_row(page)
    row.locator('[data-review=star]').click()
    row.locator('[data-review=reviewed]').click()
    page.check('#starFilter')
    expect(page.locator('#rows .row')).to_have_count(1)
    page.select_option('#reviewFilter', 'unreviewed')
    expect(page.locator('#rows .row')).to_have_count(0)
    page.select_option('#reviewFilter', 'reviewed')
    detail = open_detail(page)
    detail.locator('[data-review=relevance]').select_option('LOW')
    expect(page.locator('#tallyHigh')).to_have_text('2')
    expect(page.locator('#tallyLow')).to_have_text('3')
    expect(detail.locator('.original-rating')).to_have_text('Original: HIGH')
    detail.locator('[data-review=relevance]').select_option('')
    expect(page.locator('#tallyHigh')).to_have_text('3')
    page.click('#clearFilters')
    expect(page.locator('#rows .row')).to_have_count(10)
    first_row(page).locator('[data-review=reviewed]').click()
    expect(page.locator('#reviewProgress')).to_have_text('0 of 10 calls reviewed')


def test_pins_have_separate_view_and_do_not_seek(review_page):
    page = review_page
    connect(page)
    detail = open_detail(page)
    detail.locator('[data-review=pin]').first.click()
    expect(page).not_to_have_url(re.compile('#call='))
    page.click('[data-mode=notes]')
    expect(page.locator('#modeNotes .pinned-card')).to_have_count(1)
    page.locator('#modeNotes a').first.click()
    expect(page.locator('#call')).to_be_visible()
    expect(page.locator('#summaryBody [data-review=pin]').first).to_have_attribute('aria-pressed', 'true')
    old_time = page.locator('#timeCurrent').inner_text()
    page.locator('#summaryBody [data-review=pin]').first.click()
    assert page.locator('#timeCurrent').inner_text() == old_time
    page.click('#call a[href="#notes"]')
    expect(page.locator('#modeNotes .pinned-card')).to_have_count(0)


def test_names_and_exceptions_update_charts_not_transcripts(review_page):
    page = review_page
    connect(page)
    original = page.evaluate('JSON.stringify(JCS.Data.calls.map(c => c.lines))')
    detail = open_detail(page)
    set_name(page, 'Jane <Smith>')
    matching = page.evaluate('JCS.Data.calls.filter(c => c.outside === JCS.Data.calls[0].outside).map(c => c.outside_name)')
    assert len(matching) > 1 and set(matching) == {'Jane <Smith>'}
    set_name(page, 'Different Person', 'call')
    assert page.evaluate('JCS.Data.calls[0].outside_name') == 'Different Person'
    assert 'Jane <Smith>' in page.locator('#phoneFilter').inner_text()
    detail.locator('[data-review=relevance]').select_option('LOW')
    page.click('[data-mode=calendar]')
    day = page.locator('[data-date="2026-03-03"]')
    expect(day.locator('.mk--high')).to_have_count(0)
    day.hover()
    expect(page.locator('.chart-tip')).to_contain_text('Different Person')
    expect(page.locator('.chart-tip')).to_contain_text('1 low')
    page.click('[data-mode=timeline]')
    assert page.evaluate('JCS.Data.calls[0].rel_rank') == 1
    page.click('[data-mode=list]')
    page.fill('#searchInput', 'Different Person')
    expect(page.locator('#rows .row')).to_have_count(1)
    assert page.evaluate('JSON.stringify(JCS.Data.calls.map(c => c.lines))') == original
    # Quoted / all-word metadata queries must not match a name on just one token.
    page.fill('#searchInput', 'Different nonexistenttoken')
    expect(page.locator('#rows .row')).to_have_count(0)


def test_wrong_folder_and_damaged_file_are_never_overwritten(review_page):
    page = review_page
    page.evaluate("""async () => {
      const f = await testReviewFolder.getFileHandle('index.html');
      const w = await f.createWritable(); await w.write('Wrong case'); await w.close();
    }""")
    page.click('#reviewConnect')
    page.click('#reviewSetupOther')
    expect(page.locator('#reviewSetupError')).to_contain_text('does not contain this delivery')
    assert page.evaluate("async () => {try {await testReviewFolder.getFileHandle('case-review.json'); return true;} catch(e) {return false;}}") is False
    page.evaluate("""async () => {
      const f = await testReviewFolder.getFileHandle('index.html');
      let w = await f.createWritable(); await w.write(document.head.innerHTML); await w.close();
      const bad = await testReviewFolder.getFileHandle('case-review.json', {create:true});
      w = await bad.createWritable(); await w.write('{broken'); await w.close();
    }""")
    page.click('#reviewSetupOther')
    expect(page.locator('#reviewSetupError')).to_contain_text('not readable JSON')
    assert page.evaluate("async () => (await (await testReviewFolder.getFileHandle('case-review.json')).getFile()).text()") == '{broken'


def test_failed_write_keeps_browser_recovery_copy(review_page):
    page = review_page
    connect(page)
    before = disk(page)
    page.evaluate("window.failReviewWrites = true")
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    expect(page.locator('#reviewSaveStatus')).to_contain_text('Autosave stopped')
    assert disk(page) == before
    state = page.evaluate('JCS.Review.status()')
    assert state['dirty'] and state['backupOK'] and not state['connected']


def test_merges_disk_changes_and_retains_unstar_tombstone(review_page):
    page = review_page
    connect(page)
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    saved = disk(page)
    star_key = next(k for k in saved['records'] if 'star' in k)
    assert saved['records'][star_key][2] is False
    # An independently edited field on disk survives the next browser write.
    page.evaluate("""async () => {
      const f = await testReviewFolder.getFileHandle('case-review.json');
      const d = JSON.parse(await (await f.getFile()).text());
      const call = JCS.Data.calls[0];
      d.records[JSON.stringify(['call', call.audio_filename, 'relevance'])] = [Date.now(), 'a'.repeat(32), 'LOW'];
      const w = await f.createWritable(); await w.write(JSON.stringify(d)); await w.close();
    }""")
    first_row(page).locator('[data-review=reviewed]').click()
    page.evaluate('JCS.Review.flush()')
    assert page.evaluate('JCS.Data.calls[0].relevance') == 'LOW'
    assert disk(page)['records'][star_key][2] is False


def test_playback_requires_coverage_and_respects_manual_unmark(review_page):
    page = review_page
    connect(page)
    first_row(page).locator('[data-action=viewer]').click()
    page.wait_for_function("document.getElementById('loadingOverlay').classList.contains('hidden')")
    # Model the media element's played TimeRanges, not elapsed wall time or playhead position.
    page.evaluate("""() => {
      const audio = document.getElementById('audio');
      Object.defineProperty(audio, 'duration', {configurable:true, value:100});
      Object.defineProperty(audio, 'played', {configurable:true, value:{length:1,start:()=>90,end:()=>100}});
      audio.dispatchEvent(new Event('ended'));
    }""")
    expect(page.locator('#callReviewControls [data-review=reviewed]')).to_have_attribute('aria-pressed', 'false')
    page.evaluate("""() => {
      const a = document.getElementById('audio');
      Object.defineProperty(a, 'played', {configurable:true, value:{length:1,start:()=>0,end:()=>100}});
      a.dispatchEvent(new Event('ended'));
    }""")
    expect(page.locator('#callReviewControls [data-review=reviewed]')).to_have_attribute('aria-pressed', 'true')
    page.click('#callReviewControls [data-review=reviewed]')
    page.evaluate("document.getElementById('audio').dispatchEvent(new Event('ended'))")
    expect(page.locator('#callReviewControls [data-review=reviewed]')).to_have_attribute('aria-pressed', 'false')


def test_edits_during_write_are_saved_in_order(review_page):
    page = review_page
    connect(page)
    page.evaluate("""() => {
      window.testWriteDelay = 150;
      const c = JCS.Data.calls[0];
      JCS.Review.set(c, 'star', true);
      JCS.Review.flush();
      setTimeout(() => JCS.Review.set(c, 'reviewed', true), 50);
    }""")
    page.wait_for_function("JCS.Review.get(JCS.Data.calls[0], 'reviewed', false) && !JCS.Review.status().dirty")
    values = disk(page)['records']
    assert len(values) == 2 and all(c[2] is True for c in values.values())


def test_file_saves_when_browser_backup_is_blocked(review_page):
    page = review_page
    connect(page)
    page.evaluate("""() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key.startsWith('jcs_review_')) throw new DOMException('Storage full', 'QuotaExceededError');
        return original.call(this, key, value);
      };
    }""")
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    expect(page.locator('#reviewSaveStatus')).to_contain_text('Browser backup unavailable')
    assert not page.evaluate('JCS.Review.status().dirty')
    assert any(c[2] is True for c in disk(page)['records'].values())


@pytest.mark.parametrize('damage', ['case', 'version', 'field', 'type'])
def test_incompatible_files_are_rejected_without_writes(review_page, damage):
    page = review_page
    connect(page)
    page.evaluate("""async damage => {
      const f = await testReviewFolder.getFileHandle('case-review.json');
      const d = JSON.parse(await (await f.getFile()).text());
      if (damage === 'case') d.caseId = 'other';
      if (damage === 'version') d.version = 999;
      if (damage === 'field') Object.defineProperty(d.records, '__proto__', {value:[1, 'a'.repeat(32), true], enumerable:true});
      if (damage === 'type') d.records[JSON.stringify(['call', JCS.Data.calls[0].audio_filename, 'star'])] = [1, 'a'.repeat(32), 'yes'];
      const w = await f.createWritable(); await w.write(JSON.stringify(d)); await w.close();
    }""", damage)
    before = disk(page)
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    assert disk(page) == before
    assert page.evaluate('JCS.Review.status().error')


def test_denied_permission_keeps_backup_and_allows_reconnect(review_page):
    page = review_page
    connect(page)
    first_row(page).locator('[data-review=star]').click()
    page.evaluate('JCS.Review.flush()')
    page.context.add_init_script("window.testPermission = 'prompt';")
    page.reload()
    page.wait_for_function('JCS.Review.status().remembered')
    assert not page.evaluate('JCS.Review.status().connected')
    expect(first_row(page).locator('[data-review=star]')).to_have_attribute('aria-pressed', 'true')
    page.click('#reviewConnect')
    page.click('#reviewSetupChoose')
    expect(page.locator('#reviewSetupError')).to_contain_text('not granted')
    page.evaluate("window.testPermission = 'granted'")
    page.click('#reviewSetupChoose')
    expect(page.locator('#reviewSetup')).not_to_be_visible()
    page.wait_for_function('JCS.Review.status().connected')


def test_clear_call_exception_restores_number_default(review_page):
    page = review_page
    connect(page)
    open_detail(page)
    set_name(page, 'Number default')
    set_name(page, 'Call exception', 'call')
    page.locator('[data-review=name]:visible').first.click()
    page.click('#phoneNameDefault')
    assert page.evaluate('JCS.Data.calls[0].outside_name') == 'Number default'
    set_name(page, '', 'call')
    assert page.evaluate('JCS.Data.calls[0].outside_name') == ''


def test_review_actions_do_not_change_transcript_or_player_position(review_page):
    page = review_page
    connect(page)
    first_row(page).locator('[data-action=viewer]').click()
    expect(page.locator('#call')).to_be_visible()
    expect(page.locator('#transScroll .trans-line').first).to_be_visible()
    page.wait_for_function("document.getElementById('loadingOverlay').classList.contains('hidden')")
    before = page.locator('#transScroll').inner_html()
    page.click('#callReviewControls [data-review=star]')
    page.select_option('#callReviewControls [data-review=relevance]', 'MEDIUM')
    set_name(page, 'Name with spaces and P')
    assert page.locator('#transScroll').inner_html() == before
    expect(page.locator('body')).not_to_have_class(re.compile(r'\bpresent\b'))


def test_first_edit_prompts_for_folder_and_cancel_preserves_change(review_page):
    page = review_page
    first_row(page).locator('[data-review=star]').click()
    expect(page.locator('#reviewSetup')).to_be_visible()
    expect(page.locator('#reviewSetupHelp')).to_contain_text('extracted folder')
    page.click('#reviewSetupCancel')
    expect(first_row(page).locator('[data-review=star]')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('#reviewSaveStatus')).to_contain_text('this browser only')
    connect(page)
    assert any(c[2] is True for c in disk(page)['records'].values())


def test_reconnect_repairs_empty_initial_file_from_backup(review_page):
    page = review_page
    first_row(page).locator('[data-review=star]').click()
    page.evaluate("testReviewFolder.getFileHandle('case-review.json', {create:true})")
    page.click('#reviewSetupOther')
    expect(page.locator('#reviewSetup')).not_to_be_visible()
    assert any(c[2] is True for c in disk(page)['records'].values())


def test_unavailable_file_api_reports_browser_only_storage(page):
    page.context.add_init_script('window.showDirectoryPicker = undefined;')
    page.reload()
    page.wait_for_function('JCS.Review.status().ready')
    first_row(page).locator('[data-review=star]').click()
    expect(page.locator('#reviewConnect')).to_be_disabled()
    expect(page.locator('#reviewSetup')).not_to_be_visible()
    expect(page.locator('#reviewSaveStatus')).to_contain_text('this browser only')
