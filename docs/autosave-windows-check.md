# Folder autosave: recipient-machine check

The attorney review features (stars, reviewed marks, pinned notes, relevance
changes, number names) save to `case-review.json` in the extracted delivery
folder through the File System Access API, with a browser recovery copy in
IndexedDB. The browser tests (`tests/test_review_browser.py`) exercise this
with a simulated directory handle; they cannot show the native folder picker,
the permission prompts, or how a managed Windows profile treats a remembered
handle. Run this checklist once on a representative locked-down Windows
machine (Edge, then Chrome if both are in use) before promising the behavior
to a client. It needs no installation and no code execution.

Record the browser name and version, whether the profile is managed, and the
result of each step.

1. **Initial setup.** Extract the zip to Documents, open `index.html`, star a
   call. The status bar should offer autosave. Choose **Enable autosave** and
   select the extracted folder in the picker. Confirm the status bar names
   that folder (`Saved to <folder>/case-review.json`) and that the file
   appears in the folder.
2. **Every edit kind.** Change a relevance tier, mark a call reviewed, pin a
   note, name a number. Confirm the JSON's modified time advances after each.
3. **Close and reopen.** Quit the browser completely (not just the tab).
   Reopen `index.html`. Expect either the review restored with autosave
   connected, or a **Reconnect autosave** prompt; after reconnecting, the
   edits from step 2 must all be present. Note which of the two happened:
   Chromium only keeps a handle's permission across restarts when the
   profile allows persistent permissions, so the guide says "if prompted on a
   later visit, reconnect the folder" rather than promising a one-time setup.
4. **Cancel the picker.** Make an edit, choose Enable/Reconnect autosave,
   cancel the dialog. The edit must stay on screen and the status bar must
   say the change is saved in this browser only (or not saved, when the
   recovery copy is unavailable).
5. **Unwritable folder.** Select a folder the account cannot write to (for
   example under Program Files). Expect a clear failure message and the
   change kept in the browser; no silent success.
6. **Move or copy the delivery.** Copy the extracted folder elsewhere and
   open the copy's `index.html`. Confirm the status bar names the copy when
   connected, and that edits go to the copy's `case-review.json`, not the
   original's.
7. **Interrupted save.** Make an edit and immediately close the tab. Reopen;
   the edit should come back from the browser recovery copy or the file.
8. **Sharing.** Confirm that the review edits are in the extracted folder's
   `case-review.json` and not in the original zip; a reviewed case is shared
   by copying or re-zipping the whole extracted folder.

Anything that fails here is a product issue, not a machine issue: report the
step, the browser, and the exact status-bar text.
