# plan-12.md — NVSky handoff (post-Explore/Feed-manager cleanup session)

Session goal was originally "finish Settings > Feed manager"; scope grew to a full
bug-fixing pass across Feed manager, Home tab, and Explore tab, plus the discovery
of a structural limitation in how Settings is currently hosted. This plan is the
carry-forward for what's left, generated from session memory + this session's own
source audit; plan-1-11.md (the older combined history) was uploaded this session
but came through with broken text encoding (mojibake) and could not be read —
still pending a clean re-upload to cross-check against.

## Confirmed complete and working this session

- **Ctrl+F5 (checkAllOpenTabs) crash** — root cause was `ExploreWindow._syncPage`
  guarding on `self._feedKey` (always truthy, a fixed `"explore"` placeholder)
  instead of `self._sourceQuery` (the real "has a Posts search run yet" signal).
  Fixed. Compounding bug found in the same pass: `mainWindow.py` had no
  `from logHandler import log` at all, so `checkAllOpenTabs`'s own
  `except Exception as e: log.error(...)` handler crashed with `NameError`,
  silently killing the whole bulk-check thread on the first tab that errored.
  Both fixed and confirmed by the user.
- **checkAllOpenTabs coverage audit** — confirmed by full code review that every
  open tab of every kind (Home/Notifications/Saved/Explore/Lists/Chat hub, plus
  any popped-out ConvoTabWindow/ListTabWindow/FeedPreviewTabWindow) is
  automatically included via `getOpenTabs()` scanning the whole shared
  `mainWindow.notebook`, and each panel class correctly implements
  `_syncForBulkCheck`. No further gaps found beyond the Explore bug above.
  (This likely also resolves older backlog items from before this session —
  "incomplete Ctrl+F5 bubble-up fixes for SavedWindow/ListsWindow/ListTabWindow"
  and the "Ctrl+F5 update-all-tabs architecture redesign" — but that wasn't
  explicitly re-verified against those exact old descriptions, worth a sanity
  check if either behavior seems off again.)
- **Settings > Feed manager** — functionally complete: list/reorder/pin/remove,
  per-account local db cache (`db.get_saved_feeds_cache`/`set_saved_feeds_cache`)
  so the panel renders instantly instead of blocking on network, optimistic
  actions with rollback+message on failure, "Browse & add feeds" deliberately
  **removed** (redundant with Explore's own Feeds search + "Add to my feeds" —
  user's call, not a miss). `add_feed_to_saved`'s previous FieldInfo/pydantic
  workaround was refactored into shared module-level helpers in client.py
  (`_get_cleaned_preferences`/`_put_preferences`/etc.), used by every new
  get/remove/pin/reorder function too — the "simplify it" item from the
  previous plan is done.
- **Home tab filter wired to Feed manager** — `wx.RadioBox` replaced with
  `wx.Choice` (supports dynamic add/remove of options), dropdown shows
  Following/Discover plus every saved feed in Feed-manager order. Two real bugs
  found and fixed along the way: (1) a feed_key mismatch (`"feed:{uri}"` vs the
  raw uri that `client.sync_feed_generator_page`'s `_store_feed_item` actually
  keys its DB writes with) that made a custom feed's cache look permanently
  empty and forced a fresh server sync on every single filter switch; (2)
  switching filters no longer force-syncs at all once a feed already has cache
  — only syncs the first time a feed_key's cache is genuinely empty. Feed
  manager notifies an open Home tab to refresh its dropdown via a new
  `get_main_window()` accessor (`__init__.py`), both after user actions and
  after its own first background refresh-from-server.
- **New-post with a link preview failing ("blob too big")** — Bluesky rejects
  any blob over 1,000,000 bytes; the auto-fetched OG-image thumbnail wasn't
  being resized/compressed before upload. Fixed once, centrally, in
  `client.py`'s `_upload_blob_dict` (shared by avatar/banner/attachments/link-
  card thumbnails alike) via a new `_compress_image_for_blob` helper that
  re-encodes as JPEG, lowering quality then dimensions until it fits. Confirmed
  fixed by the user.
- **Explore tab — Alt+A/Alt+U routing** — the generic `ItemActionMixin.onCharHook`
  ties Alt+A/Alt+U to `self.postList`/`self._getFocusedPost()` unconditionally;
  fine for every other tab (one list each) but wrong for Explore (4 separate
  result lists). Fixed via an `ExploreWindow.onCharHook` override that routes to
  the already-correct `onResultAction()` dispatcher for non-Posts result types
  (Posts still falls through to the normal mixin path). Confirmed fixed.
- **Explore tab — Alt+1-9 / Shift+F5** — same class of bug as Alt+A, found during
  the Alt+A fix's audit: `_announceNthNewestPost`/`onFetchPreviousPosts` also
  acted on `self._posts` (Posts-search-only) regardless of the active result
  type. Alt+1-9 generalized to work on whichever result list/data array is
  actually current (`_announceNthResult`); Shift+F5 has no meaningful
  equivalent for the other result types so it's now a safe no-op there instead
  of touching stale data. Confirmed fixed.
- **`_markSelectedRead` crash reappeared a second time** — `AttributeError:
  'ExploreWindow' object has no attribute '_markSelectedRead'`. This is the
  exact bug from earlier in *this same session's* handoff (previously "fixed"
  by moving it from `FeedWindow` onto the shared `ItemActionMixin`) — turned
  out that fix never actually landed in the file (see meta-lesson below), so it
  silently reverted. Re-applied and this time confirmed by the user via a
  direct grep check (`def _markSelectedRead` appearing exactly once in the
  file) before retesting. Explore's select-all → Mark Read now genuinely marks
  items read (previously the crash meant nothing ever got marked, even though
  the menu appeared to "work").
- **Recurring process/meta-lesson from this session, worth remembering going
  forward**: when a patch is described as applied/ready without the real
  content actually pasted into the `old_str`/`new_str` fence (e.g. writing
  "(content from file X above)" instead of the literal text), the patch
  silently never happens, and the underlying bug can resurface identically
  much later — this happened at least twice this session (the `FeedWindow`
  RadioBox→Choice change, and the `_markSelectedRead` move above). Always paste
  real content, never reference it.

## Root-caused, fix decided, NOT yet started

- **Nested-notebook double-announcement bug in Settings** — confirmed root
  cause (matches outside research the user did independently): `NVSkySettingsPanel`
  is a `wx.Notebook` nested inside NVDA's own multi-category Settings dialog
  (itself a notebook-like category switcher). Native Tab-key traversal from our
  notebook's own tab strip into a page's first control is real OS-driven focus
  transfer with no hook this codebase can intercept — unlike MainWindow's own
  tabs, where real focus is *always* granted by explicit app code
  (`onTabActivated()`/`_restoreFocusPosition(moveFocus=True)`, fired from
  MainWindow's own `EVT_NOTEBOOK_PAGE_CHANGED` handler), never via native
  Tab-key traversal from a tab strip. Confirmed present on Feed manager;
  **the user has now also confirmed the Accounts list has the identical
  problem**, and expects (not yet tested) Muted words does too — anything
  backed by a `wx.ListCtrl` inside this nested notebook. Multiple in-session
  fix attempts (Freeze/Thaw, `EVT_SET_FOCUS` deferral) reduced but never
  eliminated it.
  **Decided fix**: stop nesting inside NVDA's Settings dialog entirely.
    1. Build `NVSkySettingsDialog(wx.Dialog)` as NVSky's own standalone dialog,
       hosting the same `wx.Notebook` of panels (Accounts/General/Display/
       Feed manager/Sound/Profile/Muted words) — the panels themselves barely
       change, this is a re-host, not a rewrite.
    2. Bind `EVT_NOTEBOOK_PAGE_CHANGED` on that dialog's own notebook and reuse
       MainWindow's proven `onTabActivated`/`_restoreFocusPosition` pattern
       verbatim.
    3. Access via `gui.mainFrame.sysTrayIcon.preferencesMenu` — NVDA's standard
       Preferences-menu convention for add-on settings (confirmed by the user;
       a launcher button left inside NVDA's own Settings dialog, Claude's first
       guess, is *not* the convention other add-ons use).
    4. Remove `NVDASettingsDialog.categoryClasses` registration for
       `NVSkySettingsPanel` entirely — no entry kept in NVDA's own Settings
       dialog at all.
    5. Repoint `mainWindow.py`'s existing "Settings" toolbar action to open the
       same new dialog.
  Explicitly saved as a plan for a later session — this is the next big task.

## Still open / never independently verified (older backlog, carried forward)

- **`SUPPORTS_FOCUS_NEXT_UNREAD` (jump-to-unread) in `FeedPreviewTabWindow`** —
  added a while back but never actually tested end-to-end; was blocked by the
  `_markSelectedRead` crash at the time. That crash is now fixed (twice over,
  see above) — this should be testable now.
- **"Remember where I came from on close" (`origin_key`) across every other
  pop-out tab type** — only ever implemented/audited for Explore's own
  pop-outs (View feed, Open in new tab). ConvoTabWindow's "Open in new tab",
  followers/following lists, List tab pop-outs, etc. have not been audited for
  this behavior.
- **`FeedPreviewTabWindow` ("Open in new tab" pop-out) select-all → mark-read
  still broken** — deliberately deferred earlier in the project; user expects
  it may resolve itself once a later refactor pass happens, but hasn't been
  retested since the `_markSelectedRead` fix landed for real this time — worth
  a quick check, might already be fixed as a side effect.
- **Advanced search field names** (`since`/`until`/`author`/`lang` on
  `search_posts`) — still LOW CONFIDENCE, never independently verified
  field-by-field against a real response; user only ever confirmed filtering
  "works correctly" at a high level.
- **Old backfill items whose status is unclear** (pre-date this session's
  memory, never explicitly re-checked): hiding the old "Find lists by user..."
  button in `ListsWindow` after the MainWindow toolbar version was added.

## Structural / code-quality backlog (explicitly NOT to mix with bug fixes)

- User has flagged more than once that `FeedWindow`/`SavedWindow`/
  `ListTabWindow`/`FeedPreviewTabWindow` have heavily duplicated `__init__`
  boilerplate (toolbar, `postList`, action buttons, statusBar, binds,
  `onTabActivated`, `_dbGetPage`, `_dbGetUnreadCount` — differing in practice
  only by `_feedKey` source and `_syncPage`'s target call) and that new
  features have sometimes been added as a new near-duplicate class/block
  instead of a shared one (e.g. the short-lived `FeedBrowseDialog`, later
  removed). Agreed direction: dedup the shared `__init__` into a
  `FeedListMixin` helper method once, and centralize the "feed result →
  context menu (View feed / Add / Open on bsky.app)" builder that currently
  only lives in `ExploreWindow` so future features reuse it directly. Agreed
  explicitly to do this as its **own isolated pass**, not mixed into bug-fix
  patches, given the blast radius (touches every tab class at once).

## Cross-checked against plan-1-11.md (older combined history, now readable)

Read in full and compared against session memory + a few direct source checks.
Most of plan-01's original stub list (Reply/Repost/Quote, Follow/Unfollow,
Mute/Unmute, Block/Unblock, Save/Unsave a post, Edit-who-can-reply) was
confirmed **done** by later plans ("ครบทุกรายการ ทดสอบผ่านหมด" — all complete,
all tested passing) — no action needed, just correcting the record since plan-12's
first draft didn't have visibility into that. Same for Profile editing
(display name/bio) — confirmed done, `ProfilePanel` has a real implementation,
not a stub. Same for "View Thread / followers-following as separate dialogs,
never converted to tabs" — by plan-11 these are already pop-out tabs (only the
origin_key audit is still outstanding, already listed above).

Genuinely still-open items found, some directly verified against the current
source in this session (not just old plan text):

- **Sound system — confirmed still not implemented.** `SoundPanel` in
  `settings.py` is a literal placeholder (`"Sound settings are not
  implemented yet."`), nothing else. Last real design discussion was
  around plan-05/06 (theme subfolder under `sounds/<theme>/`, checklist of
  events, playback via `nvwave`) and never picked back up since.
- **"Clear cache" only clears Home, not Notifications/Saved/Chat/Lists —
  confirmed still true right now.** Read `db.clear_cache()` directly: it
  only deletes from `posts` (shared post-content cache) and Home's
  `lastFocus` ui_state entry. It does not touch `feed_items` (so stale
  membership rows can linger), and doesn't touch notifications, convos/
  messages, or lists tables at all. This was flagged as a bug back around
  plan-05/06 and is still exactly the same today.
- **Ctrl+Delete ("clear current tab's cache") was never implemented** —
  confirmed via `mainWindow.py`'s own header comment: intentionally
  deferred, needs a new `db.clear_feed_cache(account_id, feed_key)` plus a
  notifications equivalent, neither of which exist yet.
- **Background fetch scheduler — still not started.** The interval value is
  configurable in General settings but nothing drives a `wx.Timer` off it;
  fetching only ever happens on explicit F5/Ctrl+F5 or tab open.
- **Video attachment support in Compose** — still an open question as of
  the last time it was discussed (plan-05/06), never decided either way.
- **I18N full pass** — still only `__init__.py` wraps strings in `_()`;
  every other file is hardcoded English. This was always meant to be one
  big pass done together near release, not mixed with other fixes — still
  the plan, just noting it's not started.
- **DB portability** — DPAPI-encrypted `db.key` ties storage to one
  Windows machine + user account; NVDA portable-copy users moving between
  machines was flagged as unresolved back in plan-01 and was never
  revisited (passphrase-based alternative vs. accepting the limitation).
  Low priority, just an old open design decision worth remembering exists.
- **Minor polish, unclear if fixed**: the Edit-who-can-reply dialog's
  initial focus was flagged around plan-06 as possibly landing on the OK
  button instead of the radio box — noted there as "may already be fixed,
  verify," never actually re-confirmed either way since.

## Suggested order for next session

1. Build `NVSkySettingsDialog` per the plan above — this both fixes the
   Accounts/Feed manager/Muted words double-announcement bug for good and
   removes the workarounds (Freeze/Thaw, `EVT_SET_FOCUS`) that only partially
   helped.
2. Quick-retest pass: `FeedPreviewTabWindow` select-all→mark-read, and
   `SUPPORTS_FOCUS_NEXT_UNREAD` jump-to-unread — both may already be fixed as
   side effects of this session's `_markSelectedRead` fix.
3. `origin_key` audit across the remaining pop-out tab types.
4. If there's room: the `FeedListMixin` dedup refactor (structural backlog
   above) — sizable but isolated, good candidate for a session on its own.
5. Decide priority on the newly-surfaced-but-old items above (Sound system,
   scheduler, Clear-cache/Ctrl+Delete cache-scope bug, video attachments,
   i18n, DB portability) — none are new, all were open before this session
   too, just re-surfaced now that plan-1-11.md could finally be read.
