# NVSky — plan-19.md

Continuation from plan-18.md. This session was almost entirely bug-hunting,
hardening, and settings-dialog rearchitecture ahead of the first public
release -- no new user-facing features beyond what's listed below.

---

## 1. Confirmed DONE this session

### Pre-release audit and fixes
Full pass over every file for crash bugs, release-readiness risks, and
leftover debug code. Fixed: missing `import gui` in exploreWindow.py
(NameError on Escape/Close in starter pack dialog), missing `_announce_now`
import, a Hide()-only Advanced-search button still firing its mnemonic,
`_announce_now = getattr(client, ...)` placeholder bug in feedTabs.py
(TypeError on Copy post text), `db.remove_account` not clearing
notifications/lists/ui_state (stale data resurfacing if the freed account
id gets reused), `_rebuildTabs` not stopping panel timers before
`DeleteAllPages()` (crash risk), Escape closing the whole app while typing
in Chat's compose box, `onCancelReply` firing from a hidden button's
mnemonic.

### Session token caching (rate-limit fix)
`get_client_for_active_account()` was calling `createSession` (full login)
on every single background sync tick and every read-message push --
confirmed to risk hitting Bluesky's createSession rate limit under normal
use (background sync every 1-2 min, all day). Now caches the session
string in memory per account DID via `on_session_change`/
`export_session_string`, and only re-logs-in with the App Password if the
cached session is rejected.

### DB key / schema-version hardening
- `init_db()` now stamps `PRAGMA user_version` from the add-on's own
  version number (format `yyyy.mm.dd` -> e.g. `2026.09.20` -> `20260920`),
  so any future schema migration has a clean version number to gate on.
  Nothing is migrated yet -- this session only added the stamp.
- If the DPAPI key or the DB file itself can't be read (corrupted key,
  moved to another machine/user), NVSky now renames the old files to
  `.bak` and starts a fresh DB instead of crashing on every launch
  forever. This does NOT recover old data (cache only; the encrypted App
  Password is unreadable either way) -- it only keeps the add-on usable;
  the user re-logs-in. Confirmed working as designed in testing.

### Chat capability check hardening
`check_chat_supported` no longer permanently hides the Chat tab on a
transient 5xx/network error during login -- only a real 4xx from the
server marks an account as chat-unsupported.

### Optional permanent tabs (Settings > Display)
Notifications, Explore, Saved, Likes, Chat, and Lists are now all
optional permanent tabs via a checklist in Settings > Display (Home is
always shown, not listed). Saved and Likes default OFF; the other four
default ON (matching what used to be always-on). New `Likes` tab (own
likes, newest-liked-first via `listRecords` since `getActorLikes` has no
usable ordering) sits alongside a `Saved` tab that used to be the only
optional-feed permanent tab. Background sync skips a hidden tab's
category entirely (Notifications/Chat/Lists), except Chat/Lists still
sync if a popped-out conversation/list temp tab is open independently.

### Content label defaults + master Adult Content switch
- Added the missing master "Adult content" on/off switch
  (`app.bsky.actor.defs#adultContentPref`), which the original 4-category
  audit missed entirely -- while off, porn/sexual/graphic-media are
  force-hidden (nudity is NOT adult-only per Bluesky's own model and
  stays independently controllable).
- Per-category defaults for a label the account never explicitly set now
  match the official app: porn=hide, sexual=warn, nudity=show,
  graphic-media=warn (previously wrongly defaulted to warn for all 4).
  A value the account HAS set on the server always wins over these
  defaults -- never overwritten.
- Fixed `app.bsky.embed.gallery#view` (5+ image posts, including
  quote+gallery) being unrecognized and showing as "Quote + media" with
  no embed sound/menu at all -- now treated identically to a plain
  images embed.

### Settings dialog: OK / Cancel / Apply rearchitecture
Root-caused the "ticking Saved/Likes yanks focus to MainWindow and
Settings doesn't actually close" bug: toggling an optional tab used to
call `rebuild_main_window_tabs()` (which tears down and rebuilds every
tab) immediately on every checkbox change, while Settings itself never
actually closed (Raise()'d again next open, stuck on the same tab).

Redesigned the whole dialog to match NVDA's own Settings behavior:
- OK / Cancel / Apply buttons (no more auto-apply-on-change, no more bare
  Close). Cancel and Escape discard everything unsaved, silently, no
  confirmation.
- Every panel implements `apply()` (for local ui_state) and/or
  `pendingTasks()` (a list of staged server-side changes, each with a
  label/run/commit) -- OK/Apply commits all local changes synchronously,
  then runs every pending server task in one background thread, with a
  progress sound. A failure leaves the dialog open and reports which
  task(s) failed by label; successful tasks are still committed.
- Tab rebuild (from optional-tabs changes) happens once, after the dialog
  actually closes, restoring focus to whichever tab was open in
  MainWindow beforehand -- never mid-edit, never stealing focus while
  Settings is still open.
- Feed manager, Muted words, content labels, notifications, and
  muted/blocked-actor undo are all now staged-and-reversible via
  Cancel, using the SAME already-tested single-action write functions
  under the hood (add_muted_word/remove_muted_word,
  remove_feed_from_saved/set_feed_pinned/reorder_saved_feeds,
  set_content_label_pref/set_adult_content_enabled,
  set_notification_category) rather than new untested bulk-write
  functions -- those bulk-write drafts were written, then explicitly
  discarded in favor of the proven per-action calls once it became clear
  large parts of this rewrite couldn't be tested before release (only
  one test account, no willingness to actually rename the profile, etc).
- Display tab reordered (Sort order, Author display, Time format, Tabs
  checklist, Content labels, Notifications) and its two RadioBoxes
  (author display, sort order) converted to labeled Choice dropdowns for
  clearer screen-reader reading. General's bgsync interval fields and
  Accounts' button order also re-sequenced to match tab/importance order.
- Muted/Blocked users split back into two separate tabs (was merged
  earlier in anticipation of a third "subscribed" sub-tab that never
  materialized) -- 9 total Settings tabs, still within Ctrl+1-9 reach.
- Removed the redundant "Save display name & bio" button from Profile
  (OK/Apply covers it now); Profile edits (name, bio; avatar/banner via
  file picker, staged and uploaded on OK/Apply) confirmed working via
  direct testing of the bio-edit path.

### Progress sound coverage + refcounting
- Added a `uiutil.start_worker(worker, progress=True)` helper (starts the
  progress sound, guarantees it stops via `finally`) and used it to add a
  progress sound to ~30 previously-silent network actions across
  feedWindow/exploreWindow/feedTabs/listsWindow/chatWindow/mainWindow/
  compose/__init__ (view profile, follow/mute/block status checks, pin
  checks, reply-permission load/save, image/video download, all
  Explore/Lists/Chat dialog network calls, Ctrl+F5, posting, first-login
  sync). Startup temp-tab restoration explicitly excluded
  (`progress=False`) so opening NVSky doesn't play a chorus of progress
  sounds for every restored followers/following tab.
- `soundpack.py`'s progress indicator is now reference-counted
  (`start_progress`/`stop_progress` pairs correctly nest instead of one
  finishing early cutting off another's sound) and has a 3-minute safety
  timeout that self-resets and logs if some flow never calls
  `stop_progress` -- plus a `reset_progress()` used on NVDA shutdown.
- New `main_open`/`main_close` sound events (played when MainWindow opens
  via NVDA+Shift+Y and closes; NOT played on NVDA shutdown itself) --
  silent until the user supplies matching .wav files in their pack.

### Content warning / embed sound fixes
- Sounds for embed type on focus-navigation now play BLOCKING
  (`nvwave.playWaveFile(..., asynchronous=False)`) instead of debounced
  async, specifically so the sound always finishes before NVDA announces
  the row -- confirmed by the user as the desired order over "faster but
  sound lags behind speech". Requires embed/content-warning sound files
  to stay short since NVDA blocks while they play.
- Ctrl+C on a content-warned post now always copies the REAL text (not
  the "(Content warning: ...)" placeholder) -- reading and choosing to
  copy is treated as an intentional reveal.

### Cross-tab post-state sync (like/repost/save/mute-thread)
Root-caused and fixed: toggling Like/Repost/Save/Mute-thread from one
open tab (e.g. Home) never updated another already-open tab's own
in-memory copy of the same post (e.g. Likes tab, or Home vs Explore) --
each tab held its own dict, only the DB and the acting tab's own list
got updated. Added `feedWindow.propagate_post_state(post)` (copies
viewer_* fields onto every other open tab's matching post-by-uri) and
`sync_like_state(post, account_id)` (writes DB + propagates + adds/
removes the row in any open Likes tab), called from every Like toggle
site including Thread/Quotes tabs (which previously didn't write to the
DB or sound at all for Like).

### Ctrl+C: copy full row (all lists) and Ctrl+J: jump to row
- Ctrl+C now copies the full text of the focused row (not the ~511-char
  truncated ListCtrl display text) across every list/tree showing
  messages: feeds (all FeedListMixin hosts), user lists, Explore result
  types, Thread/Quotes, Chat's message list and conversation tree. Posts
  copy as "author, full text, time (https://bsky.app/profile/.../post/...)"
  -- an explicit share format with a working link back to the post,
  always using the REAL text even under a content warning.
- Ctrl+J (new): asks for a row number via a small dialog, digit entry is
  clamped live to the loaded row count (never lets you type an
  out-of-range number, no after-the-fact error), jumps there and speaks
  just that row. Same list/tree coverage as Ctrl+C above (message-
  bearing views only, not settings/add-user checklists).
- Root-caused why the jump briefly spoke the window title (and, per
  speech log only, an inaudible tab-control/list announcement): the
  focus event from the just-closed row-number dialog arrives at NVDA
  AFTER the row has already been moved and re-focused, not before --
  earlier delay-based attempts (250ms sleep, waiting on NVDA's event
  queue) couldn't fix an ordering problem, only mask it inconsistently.
  Final fix hooks `event_gainFocus` directly: suppresses the one
  re-announcement of the main window's own title, and on the row's own
  focus event, lets NVDA process it normally (so focus/braille state
  stays correct) then cancels speech and speaks only the target row's
  text. Confirmed via a live NVDA log trace before landing on this
  approach -- deliberately not solved by guessing a delay value.

### User action menu: View submenu promoted, Start chat de-risked
Reordered "Show..." (renamed "View...", containing Profile/Timeline/
Followers/Following/Known followers/Lists) to be the FIRST item in the
per-user action menu, with Start chat second -- previously "S" opened
Show's first letter-match, which was easy to confuse with actually
starting an irreversible chat. Investigated whether a separate
Like/Repost browsing feature was feasible (getActorLikes exists but is
own-account-only, no such endpoint for another user's likes at all;
reposts already show inline in a user's own timeline) -- confirmed no
such per-user endpoint exists, so nothing further was built there beyond
the new Likes tab (which already covers "my own likes").

### Debug-dump cleanup
Removed every `debug_dump()` call that fired on ordinary SUCCESSFUL
requests (convo availability, join-link create/edit/enable, edit group,
join-link preview, request-join, list-join-requests, activity-
subscription read/write) -- confirmed via a real dump file the user
found in their own debug_dumps folder, which also contained another
user's DID/handle/avatar (a privacy concern if that folder ever shipped
in a release). Dumps that only fire on a genuinely empty/failed server
response were deliberately left in place, since existing UI text still
references "check debug_dumps for the raw response" for those specific
cases.

---

## 2. Decided / recorded this session

- Version numbers always use `yyyy.mm.dd` (e.g. `2026.09.20`) -- DB
  schema-version stamping depends on this format.
- Releases are built via GitHub Actions; the `.pot` is regenerated at
  release time only (a manually-triggered action extracts it and opens
  an issue mentioning translators) -- nothing built locally, nothing
  needed from the code side beyond keeping every UI string wrapped in
  `_()` with a `# Translators:` comment (already the existing
  convention, unaffected by this session's changes).
- The unused activity-subscription ("bell subscribe" per-account new-
  post notification) code in client.py/db.py stays in place, unused,
  until Bluesky actually ships the feature server-side for real (per
  plan-18.md's original finding) -- everything else this session found
  genuinely unused (three settings-related bulk-write drafts that got
  superseded by per-action calls) was deleted.
- Backgrounds sync now respects which optional tabs are enabled (see
  above) -- decided over leaving it always-on, since a user who hides a
  tab most likely doesn't want its notification sound/announcement
  either.
- Known, accepted release-time limitations (documented, not fixed):
  `list_convos` has no pagination past 50 conversations; temp files are
  only swept on NVDA startup (up to ~24h lifetime); switching accounts
  while Settings is open, and very large chat/notification volumes,
  were not directly testable with the single test account available and
  ship as known issues instead.

---

## 3. Suggested starting point for next session

1. **Documentation** -- explicitly deferred to next session per the
   user's own request. Nothing scoped yet.
2. Optional: a short, targeted code-read pass (not a full re-audit) over
   the newest and least-exercised code from this session specifically --
   settings.py's OK/Cancel/Apply staging, soundpack.py's refcounted
   progress sound, uiutil.py's event_gainFocus hook for Ctrl+J, and
   sync_like_state -- since these haven't had the same multi-round
   scrutiny as the rest of the codebase yet. Not requested outright by
   the user this session; offered, not committed to.
3. No other firm backlog item remains. The add-on is considered
   feature-complete and stability-hardened for a first public release,
   pending: release packaging (no debug_dumps/__pycache__ in the
   package), sourcing licensed sound files for the sound pack, and the
   documentation pass above.