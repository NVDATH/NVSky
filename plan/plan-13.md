# NVSky — plan-13.md

Continuation of plan-11/plan-12. This session focused on two large tracks:
**(1) NVSkySettingsDialog** (replacing the nested NVDASettingsDialog panel)
and **(2) a long round of bug-hunting/fixing across Settings + Chat**, driven
by real testing feedback. Both tracks are now stable and confirmed working
by the user. Nothing outstanding is a confirmed-broken bug as of this
writing — remaining work is refactor + backlog + optional feature gaps.

---

## 1. Confirmed DONE this session (all patches applied + user-tested working)

### Settings dialog rework
- Replaced `NVSkySettingsPanel(SettingsPanel)` (nested inside NVDA's own
  Settings dialog — root cause of ListCtrl double-announcement) with a
  standalone `NVSkySettingsDialog(wx.Dialog)` in `settings.py`.
- Access points: NVDA Preferences menu ("NVSky Settings...") and
  `Ctrl+P` from `MainWindow`.
- `Ctrl+1`–`Ctrl+7` jump directly between the dialog's 7 tabs (Accounts,
  General, Display, Feed manager, Sound, Profile, Muted words), and now
  correctly announce the tab name first (`NVSkySettingsDialog._activatePage`
  centralizes this — individual panels don't need their own "<tab> tab"
  announcement).
- `onPageChanging`/`onPageChanged` mirrors `MainWindow`'s proven pattern:
  move focus to the notebook itself before a page swap, then grant real
  focus deliberately via each panel's own `onTabActivated()`.

### Double-announcement fixes (root-caused and fixed per panel)
- **FeedManagerPanel**: was still using the OLD `EVT_SET_FOCUS` +
  `_pendingFocusIndex` + conditional `HasFocus()` workaround (built to
  solve the *old* nested-notebook bug). Simplified to match
  `MutedWordsPanel`'s proven-simpler pattern: unconditional
  `Focus()/Select()` in `_render()`, unconditional `SetFocus()` in
  `onTabActivated()`. **Confirmed fixed by testing** — no more repeated
  reads on first entry.
- **MutedWordsPanel**: rewritten with `_renderWords()` (shared
  render+focus-restore helper), optimistic add/remove (item appears/
  disappears immediately, rolls back on server error), focus correctly
  returns to `wordList` after remove (was getting stuck on the Remove
  button). Added `&Add`/`&Remove selected` mnemonics. Dropped the
  redundant "Added X" spoken confirmation (NVDA already reads the newly
  focused row); kept "Removed X" (nothing else confirms it after removal).
- **AccountsPanel / ProfilePanel / GeneralPanel / DisplayPanel**: each
  now has `onTabActivated()` granting focus to a sensible first control,
  for consistent UX across every settings tab (previously only
  ListCtrl-backed panels had this).

### client.py fixes
- `get_muted_words` / `_save_muted_words` rewritten to use the existing
  raw-JSON-bypass helpers (`_get_cleaned_preferences`/`_put_preferences`)
  instead of the typed `get_preferences()`/`put_preferences()` calls —
  confirmed via a real 168-error pydantic traceback that the typed path
  chokes on the account's OTHER preference types too, not just muted
  words. **Fixes muted-word add/remove entirely.**
- Added `import time` (was missing — caused `name 'time' is not defined`
  when the rewritten `_save_muted_words` used it).
- `get_client_for_active_account`: added a single retry with a short
  pause for the reported `WinError 10038` ("not a socket") — LOW
  CONFIDENCE mitigation, not a root-cause fix. **See open question below
  — the real root cause may actually have been the ListsWindow threading
  bug (next item), not a transient network glitch.**

### Chat fixes
- `_pushMessageReadToServer` now also writes the post-push local unread
  count back to `convos.unread_count` (new `db.set_convo_unread_count`)
  — fixes "read everything locally, but a refresh brought unread back",
  caused by `reconcile_message_read_state` re-deriving read state from a
  stale cached `unread_count` on every resync.
- Group context menu: "Lock this group" and "Edit name..." now gated
  behind `isAdmin` (confirmed via real `InsufficientRole` 400 errors for
  non-owner members). "Manage members..." left ungated — no confirmed
  error yet, revisit if one shows up.
- `ConvoTabWindow` now takes `origin_key` and returns to that tab
  (matching `FeedPreviewTabWindow`'s existing pattern) when closed via
  Ctrl+W. `ChatWindow._openInNewTab` passes `origin_key="chat"`;
  `__init__.py`'s temp-tab restore passes it through too.
- `ChatWindow._syncForBulkCheck`: broadened change-detection from "did
  summed unread_count change" to a full per-convo
  `(unread_count, last_message_sent_at)` snapshot diff — the old check
  missed real changes that don't move the unread total (new convo
  already read elsewhere, message landing in an already-read convo).

### ListsWindow fix — likely the most important bug found this session
- `ListsWindow._syncForBulkCheck` was calling `self._syncListsFromServer()`,
  which spawns **its own separate background thread with its own
  separate `client.get_client_for_active_account()` login** — while
  itself already running INSIDE `MainWindow.checkAllOpenTabs`'s shared
  background thread. This is exactly the concurrent-login race the
  shared-thread design was built to avoid (per that method's own
  comment), and is very plausibly the *actual* cause of the
  `WinError 10038` seen during testing (not just a random transient
  glitch). Rewrote `_syncForBulkCheck` to do the list-sync work
  synchronously on the SAME `atprotoClient`/thread the caller already
  holds, and added a matching `_reloadAfterBulkCheck` that rebuilds the
  list tree from the freshly-synced DB data. **Confirmed: this was also
  why Ctrl+F5 neither announced nor updated Lists.**

---

## 2. Open / deferred items (not fixed — explicitly parked)

- **Chat tab content appearing during Home tab navigation after
  Ctrl+F5**, with status bar still showing "Home" and zero real unread
  chats. Reported once, not reproduced again. Code review found no
  explicit `SetFocus()` call anywhere in the Chat reload path that
  should be able to steal real focus from Home — deferred pending a
  reliable repro. **If it recurs, capture**: F5 vs Ctrl+F5, whether
  `NVDA+T` says "Home" or "Chat" during the glitch, and whether it's
  reproducible on demand.
- Given the ListsWindow threading fix above, **retest whether
  `WinError 10038` still occurs** after accepting a group/message
  request — if it's gone, the retry-based mitigation in
  `get_client_for_active_account` may not have been the real fix at all
  (the Lists thread race was).

---

## 3. Priority 1 for next session: finish the `FeedListMixin` refactor

**Important honesty note carried over**: this refactor was *proposed*
very early in this session (a `_buildStandardFeedSizer` /
`_bindStandardFeedEvents` / `_finishStandardFeedInit` design) but was
**never actually applied as real, verified patches** — that was before
the person caught Claude describing changes without real
old_str/new_str diffs against verified local files. So `feedWindow.py`
in the wild is still 100% unrefactored. This is the top priority to
actually finish next.

### Why this first (not a "nice to have")
`FeedWindow` / `SavedWindow` / `ListTabWindow` / `FeedPreviewTabWindow`
each hand-roll nearly identical `__init__` boilerplate (postList +
Post action/User action buttons + statusBar + standard event binds +
cache-load + focus-restore tail). Consolidating this into shared
`FeedListMixin` helpers means:
- Any future `origin_key`-style audit or similar cross-cutting fix
  becomes a single edit instead of 4 near-identical edits (the exact
  kind of drift that let the `ListsWindow` threading bug and the
  `ConvoTabWindow` missing-origin_key gap slip through unnoticed).
- Lower regression risk than it sounds — the boilerplate is already
  byte-for-byte identical across the 4 classes, so wrapping it doesn't
  change behavior, it just removes duplication.

### Concrete plan (design already sketched, needs real verified patches)
Add to `FeedListMixin` in `feedWindow.py`:
- `_buildStandardFeedSizer(extra_top=None, extra_action_widgets=None)`
  — builds postList + action row + statusBar, calls `self.SetSizer()`.
  `extra_top` for `FeedWindow`'s filter-choice row;
  `extra_action_widgets` for `FeedPreviewTabWindow`'s "Add to my feeds"
  button.
- `_bindStandardFeedEvents()` — binds postActionButton/userActionButton/
  postList focus+activate/char hook to the standard handler names every
  host already uses identically.
- `_finishStandardFeedInit(sync_if_empty=False)` — title, cache load,
  no-account message, `_restoreFocusPosition(moveFocus=False)`, optional
  `_syncIfCacheEmpty()`.

Then convert, in this order (simplest → most custom):
1. `SavedWindow` (no filter, no extra buttons — cleanest conversion)
2. `ListTabWindow` (same shape + `sync_if_empty=True`)
3. `FeedPreviewTabWindow` (needs `extra_action_widgets` for the
   conditional "Add to my feeds" button)
4. `FeedWindow` (needs `extra_top` for the filter row, and must call
   `_buildFilterChoices()` — which sets `self._feedKey` — before
   `_finishStandardFeedInit()`, not after)

**Do NOT** fold `ListsWindow` or `ChatWindow` into this — both have a
genuinely different split-tree layout, not worth forcing into this
mixin (matches the original plan-12 assessment).

### Process reminder for next session
Per the standing patch rules: recreate the actual files
(`feedWindow.py` full, not partial) in the sandbox first, apply every
patch there with `str_replace`, verify syntax, THEN hand over
old_str/new_str blocks — one fix per fence, file always named first.
After each confirmed-working patch, keep the sandbox copy in sync.

---

## 4. Group A — direct fixes, no refactor needed (do after or alongside §3)

- **`origin_key` audit, remaining spots**: followers/following list
  dialog (`UserListDialog` opened via `showFollowers`/`showFollowing`),
  and `ListTabWindow` (opened from Lists' "Show in new tab" — currently
  has no return-to-Lists-tab behavior on close). Once §3's refactor
  lands, this becomes much more mechanical to add consistently.
- **Retest**: `FeedPreviewTabWindow` select-all → mark-read, and
  `SUPPORTS_FOCUS_NEXT_UNREAD` (jump-to-unread) on that same class —
  flagged in plan-12 as possibly already fixed as a side effect of an
  unrelated bug fix, never actually re-verified.
- **Clear cache scope**: currently only clears `posts` (Home). Doesn't
  touch notifications, convos/messages, lists, or other feed_items.
  Needs either documenting the current scope clearly in the UI label,
  or actually widening `db.clear_cache()`.
- **`Ctrl+Delete`** (clear current tab's cache only): not implemented.
  Needs `db.clear_feed_cache(account_id, feed_key)` (doesn't exist yet)
  plus a notifications-equivalent.
- **Edit-who-can-reply dialog initial focus**: never verified whether
  it lands on the radio box or the OK button.
- **Manage members... admin gating**: not gated behind `isAdmin` (see
  §1's Chat fixes) — no confirmed `InsufficientRole` error for this one
  yet, so left alone. Gate it if/when one shows up in testing.

---

## 5. Optional feature gaps found via SDK/Bluesky research this session

Researched current (mid-2026) Bluesky features against what
`client.py` covers. Findings, roughly prioritized:

| Feature | Status | Recommendation |
|---|---|---|
| Self-labeling own posts (adult/graphic content warnings at compose time) | Not implemented at all | **Worth doing before calling this "done"** — real user-facing gap, moderate-small effort (`app.bsky.feed.post`'s `labels` field, a small checkbox group in `ComposeDialog`) |
| Labeler subscriptions (third-party moderation services) | Not implemented | v2 roadmap — genuinely a new feature area (UI to browse/subscribe to labeler DIDs), not a quick patch |
| Verification badge display on profile | Not implemented | v2 roadmap — small/read-only, low priority |
| "Live Now" status | Not implemented | Skip — niche |
| Dislikes | N/A | Skip — Bluesky itself still testing this, no stable endpoint to target yet |
| List-based threadgate/postgate rules | Not implemented (already documented as a known gap in existing code comments) | Leave as-is, already flagged honestly in-code |

---

## 6. Full backlog carried over unchanged from plan-12 (still not started)

1. Sound system — pure placeholder, no implementation.
2. Background fetch scheduler — `wx.Timer`-driven periodic sync; config
   fields already exist (`fetch_interval_home_minutes` /
   `fetch_interval_dm_minutes`) but nothing reads them yet. Note: the
   person raised a related idea this session — moving Feed
   manager/Profile/Muted words in Settings to cache-only-on-open with
   sync handled entirely by this future background scheduler instead of
   an on-open/on-tab-activate fetch. Worth designing together when this
   item is actually picked up.
3. Video attachment support in `ComposeDialog` — undecided whether to
   pursue at all.
4. Full i18n — only `__init__.py` wraps strings in `_()`; every other
   file hardcodes English.
5. DB portability — DPAPI key is machine+Windows-user locked by design;
   known limitation for "copy config to portable NVDA" use case.

---

## Suggested order for the next session

1. `FeedListMixin` refactor (§3) — finish what was promised but never
   actually delivered.
2. Group A direct fixes (§4), leaning on the now-shared init helpers
   from step 1 where applicable (origin_key audit especially).
3. Self-label-on-post (§5's top item) if there's still appetite before
   calling this release-ready.
4. Everything else in §6 stays backlog — explicitly not blocking a
   first release.
