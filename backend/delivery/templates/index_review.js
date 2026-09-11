  // Versioned, case-bound review data. Original delivery payloads are never saved here.
  const Review = (function () {
    const FORMAT = 'jcs-review';
    const VERSION = 1;
    const FILE_NAME = 'case-review.json';
    const CACHE_KEY = 'jcs_review_' + REVIEW_ID;
    const MAX_BYTES = 16 * 1024 * 1024;
    const callKey = call => call.audio_filename || call.filename;
    const keyFor = (call, field) => JSON.stringify(['call', callKey(call), field]);
    const pinKey = (call, cue) => keyFor(call, 'pin:' + cue.id);
    const phoneKey = call => {
      const digits = String(call.outside || '').replace(/\D/g, '');
      return digits.length === 11 && digits[0] === '1' ? digits.slice(1) : digits;
    };
    const nameKey = call => JSON.stringify(['phone', phoneKey(call)]);
    const validators = new Map();
    const callIds = new Set(CALLS.map(callKey));
    const isBool = value => typeof value === 'boolean';
    CALLS.forEach(call => {
      validators.set(keyFor(call, 'star'), isBool);
      validators.set(keyFor(call, 'reviewed'), isBool);
      validators.set(keyFor(call, 'relevance'), value => ['', 'HIGH', 'MEDIUM', 'LOW'].includes(value));
      validators.set(keyFor(call, 'name'), value => value === null || (typeof value === 'string' && value.length <= 120));
      call.cues.forEach(cue => validators.set(pinKey(call, cue), isBool));
      if (phoneKey(call)) validators.set(nameKey(call), value => typeof value === 'string' && value.length <= 120);
    });
    let records = Object.create(null);
    let clock = 0;
    const actor = Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
    let fileHandle = null;
    let directoryHandle = null;
    let ready = false;
    let dirty = false;
    let busy = false;
    let writable = false;
    let backupOK = false;
    let failure = '';
    let timer = null;
    let connectionAttempt = 0;
    let changeListener = () => {};
    let statusListener = () => {};
    const supported = typeof window.showDirectoryPicker === 'function';

    function documentData() {
      return { format: FORMAT, version: VERSION, caseId: REVIEW_ID, caseName: CASE_NAME, records };
    }
    function validate(data) {
      if (!data || data.format !== FORMAT || data.version !== VERSION) throw new Error('This is not a supported case review file. The file has not been changed.');
      if (data.caseId !== REVIEW_ID) throw new Error('This review file belongs to a different delivery. Select this case’s extracted folder.');
      if (!data.records || typeof data.records !== 'object' || Array.isArray(data.records)) throw new Error('The review file is damaged. The file has not been changed.');
      const clean = Object.create(null);
      const entries = Object.entries(data.records);
      if (entries.length > CALLS.length * 300 + 100) throw new Error('The review file contains unexpected records. The file has not been changed.');
      entries.forEach(([key, cell]) => {
        let check = validators.get(key);
        // Repackaging after a summary edit can replace a cue. Preserve its old
        // pin harmlessly, without displaying it against a different note.
        if (!check) {
          let parts;
          try { parts = JSON.parse(key); } catch (e) { parts = []; }
          if (Array.isArray(parts) && parts.length === 3 && parts[0] === 'call' && callIds.has(parts[1]) && /^pin:[a-f0-9]{24}$/.test(parts[2])) check = isBool;
        }
        if (!check || !Array.isArray(cell) || cell.length !== 3 || !Number.isSafeInteger(cell[0]) || cell[0] < 0
            || cell[0] >= Number.MAX_SAFE_INTEGER - 1 || typeof cell[1] !== 'string' || !/^[a-f0-9]{32}$/.test(cell[1]) || !check(cell[2])) {
          throw new Error('The review file contains invalid data. The file has not been changed.');
        }
        clean[key] = cell;
      });
      return clean;
    }
    function parse(text) {
      if (text.length > MAX_BYTES) throw new Error('The review file is too large. The file has not been changed.');
      let data;
      try { data = JSON.parse(text); }
      catch (e) { throw new Error('The review file is not readable JSON. The file has not been changed.'); }
      return validate(data);
    }
    async function readFile(handle) {
      const file = await handle.getFile();
      if (file.size > MAX_BYTES) throw new Error('The review file is too large. The file has not been changed.');
      return parse(await file.text());
    }
    function backup() {
      try {
        localStorage.setItem(CACHE_KEY, JSON.stringify(documentData()));
        backupOK = true;
      } catch (e) { backupOK = false; }
    }
    function value(key, fallback) { return records[key] ? records[key][2] : fallback; }
    function setValue(key, val) {
      if (!ready) return;
      const check = validators.get(key);
      if (!check || !check(val)) throw new Error('Invalid review change.');
      try { const cached = localStorage.getItem(CACHE_KEY); if (cached) merge(parse(cached)); } catch (e) { /* File/session state remains usable. */ }
      if (records[key] && records[key][2] === val) return;
      clock = Math.max(Date.now(), clock + 1);
      records[key] = [clock, actor, val];
      dirty = true;
      backup();
      changeListener();
      statusListener();
      schedule();
    }
    // A total order on field edits makes merging idempotent; false/empty values
    // are tombstones, so an old backup cannot resurrect an unstar or removed name.
    function merge(incoming) {
      let changed = false;
      Object.keys(incoming).forEach(key => {
        const cell = incoming[key], old = records[key];
        clock = Math.max(clock, cell[0]);
        if (!old || cell[0] > old[0] || (cell[0] === old[0] && cell[1] > old[1])) {
          records[key] = cell;
          changed = true;
        }
      });
      return changed;
    }
    function sameRecords(a, b) {
      const keys = Object.keys(a);
      return keys.length === Object.keys(b).length && keys.every(k => b[k] && JSON.stringify(a[k]) === JSON.stringify(b[k]));
    }
    // File handles are structured-cloned by IndexedDB; JSON cannot store them.
    let dbPromise;
    function database() {
      if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
        const request = indexedDB.open('jcs-review-handles', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('handles');
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error('Browser storage is busy.'));
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => db.close();
          resolve(db);
        };
      });
      return dbPromise;
    }
    async function rememberedHandle(handle) {
      const db = await database();
      return new Promise((resolve, reject) => {
        const tx = db.transaction('handles', handle ? 'readwrite' : 'readonly');
        const request = handle ? tx.objectStore('handles').put(handle, REVIEW_ID) : tx.objectStore('handles').get(REVIEW_ID);
        tx.oncomplete = () => resolve(request.result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error('Browser storage was interrupted.'));
      });
    }
    const locked = fn => navigator.locks ? navigator.locks.request(CACHE_KEY, fn) : fn();
    let writing = null;
    function schedule() {
      clearTimeout(timer);
      if (writable && dirty) timer = setTimeout(() => flush(), 350);
    }
    async function flush() {
      clearTimeout(timer);
      if (writing) return writing;
      if (!writable || !fileHandle || !dirty) return;
      writing = Promise.resolve().then(() => locked(async () => {
        busy = true;
        statusListener();
        try {
          // Merge the on-disk version under a same-browser lock before writing.
          // createWritable stages its output and commits it only on close().
          const disk = await readFile(fileHandle);
          const changed = merge(disk);
          if (changed) { backup(); changeListener(); }
          const snapshot = JSON.stringify(documentData());
          let stream;
          try {
            stream = await fileHandle.createWritable({mode: 'exclusive'});
            await stream.write(snapshot);
            await stream.close();
          } catch (e) {
            if (stream) { try { await stream.abort(); } catch (ignored) {} }
            throw e;
          }
          dirty = snapshot !== JSON.stringify(documentData());
          failure = '';
          backup();
        } catch (e) {
          writable = false;
          failure = 'Autosave stopped: ' + e.message + ' Reconnect to resume saving.';
        } finally {
          busy = false;
          statusListener();
        }
      })).catch(e => {
        writable = false;
        busy = false;
        failure = 'Autosave stopped: ' + e.message + ' Reconnect to resume saving.';
        statusListener();
      });
      try { await writing; } finally { writing = null; schedule(); }
    }
    async function verifyFolder(folder) {
      let index;
      try { index = await (await folder.getFileHandle('index.html')).getFile(); }
      catch (e) { throw new Error('Select the extracted delivery folder containing index.html, audio, and transcripts.'); }
      const header = await index.slice(0, 1024).text();
      if (!header.includes('<meta name="jcs-review-id" content="' + REVIEW_ID + '">')) {
        throw new Error('That folder does not contain this delivery’s index.html. Select the folder you opened this page from.');
      }
    }
    async function attach(folder, create) {
      await verifyFolder(folder);
      await locked(async () => {
        let handle, isNew = false;
        try { handle = await folder.getFileHandle(FILE_NAME); }
        catch (e) {
          if (e.name !== 'NotFoundError' || !create) throw e;
          handle = await folder.getFileHandle(FILE_NAME, {create: true});
          isNew = true;
        }
        // A crash during the very first write can leave an empty file. An
        // explicit folder reconnection may initialize it from the recovery copy.
        if (create && (await handle.getFile()).size === 0) isNew = true;
        if (isNew) {
          const stream = await handle.createWritable({mode: 'exclusive'});
          try { await stream.write(JSON.stringify(documentData())); await stream.close(); }
          catch (e) { try { await stream.abort(); } catch (ignored) {} throw e; }
        }
        const disk = await readFile(handle);
        merge(disk);
        dirty = !sameRecords(records, disk);
        fileHandle = handle;
        directoryHandle = folder;
        writable = true;
        failure = '';
        backup();
        changeListener();
      });
      try { await rememberedHandle(folder); } catch (e) { /* File saving still works; reconnect by selecting the folder. */ }
      statusListener();
      await flush();
    }
    async function connect(chooseFolder) {
      if (busy || writing) return false;
      connectionAttempt++;
      busy = true;
      statusListener();
      try {
        let folder = directoryHandle;
        if (!folder || chooseFolder) folder = await window.showDirectoryPicker({mode: 'readwrite', id: 'jcs-review'});
        else if (await folder.requestPermission({mode: 'readwrite'}) !== 'granted') {
          throw new Error('Folder access was not granted. Select the folder and allow saving to enable autosave.');
        }
        await attach(folder, true);
        return writable;
      } catch (e) {
        if (e.name === 'AbortError') return false;
        failure = e.message;
        throw e;
      } finally { busy = false; statusListener(); }
    }
    function status() {
      let message;
      if (!ready) message = 'Preparing review storage…';
      else if (failure) message = failure + (backupOK ? ' Browser recovery copy available.' : '');
      else if (busy) message = 'Saving review…';
      else if (writable && !dirty) message = 'Saved to ' + directoryHandle.name + '/' + FILE_NAME + (backupOK ? ' · Browser backup saved' : ' · Browser backup unavailable');
      else if (writable) message = 'Saving review…';
      else if (!supported) message = 'File autosave is unavailable in this browser. Open this delivery in desktop Edge or Chrome.';
      else message = directoryHandle ? 'Reconnect your review folder to resume autosave.' : 'Enable autosave to keep your review in the delivery folder.';
      if (!writable && dirty) message += backupOK ? ' Changes saved in this browser only.' : ' Changes are not saved; keep this page open.';
      return { message, folderName: directoryHandle ? directoryHandle.name : '', connected: writable, remembered: !!directoryHandle, supported, busy: busy || !ready, error: !!failure || (dirty && !writable), dirty, backupOK, ready };
    }
    async function init() {
      try {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) { merge(parse(cached)); dirty = Object.keys(records).length > 0; }
        backup();
      } catch (e) { failure = 'The browser recovery copy could not be read. Connect the delivery folder to recover your saved review.'; }
      ready = true;
      changeListener(); statusListener();
      window.addEventListener('storage', e => {
        if (e.key !== CACHE_KEY || !e.newValue) return;
        try {
          if (merge(parse(e.newValue))) { dirty = true; backup(); changeListener(); statusListener(); schedule(); }
        } catch (err) { /* A malformed browser copy must not replace this session. */ }
      });
      window.addEventListener('beforeunload', e => {
        if (!dirty && !writing) return;
        // A synchronous browser copy is the recovery path if the page exits mid-write.
        backup();
        e.preventDefault(); e.returnValue = '';
      });
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') { backup(); flush(); }
      });
      if (!supported) return;
      try {
        const folder = await rememberedHandle();
        if (connectionAttempt) return;
        if (folder) {
          directoryHandle = folder;
          const permission = await folder.queryPermission({mode: 'readwrite'});
          if (connectionAttempt) return;
          if (permission === 'granted') {
            busy = true;
            statusListener();
            try { await attach(folder, false); } finally { busy = false; }
          }
        }
      } catch (e) {
        if (directoryHandle) failure = 'Could not reconnect the saved review folder. Select the delivery folder again.';
      }
      statusListener();
    }
    return {
      init, connect, flush, status, phoneKey,
      setupHelp: 'Select the extracted folder containing this case’s index.html, audio, and transcripts. Your stars, reviewed marks, pinned notes, relevance changes, and number names will save automatically to case-review.json in that folder. Keep that file with the delivery. The browser may ask you to reconnect on a later visit.',
      get: (call, field, fallback) => value(keyFor(call, field), fallback),
      set: (call, field, val) => setValue(keyFor(call, field), val),
      name: call => value(keyFor(call, 'name'), null) === null ? value(nameKey(call), '') : value(keyFor(call, 'name'), ''),
      numberName: call => value(nameKey(call), ''),
      setName: (call, name) => setValue(nameKey(call), name),
      pinned: (call, cue) => value(pinKey(call, cue), false),
      setPin: (call, cue, pinned) => setValue(pinKey(call, cue), pinned),
      onChange: listener => { changeListener = listener; },
      onStatus: listener => { statusListener = listener; },
    };
  })();
