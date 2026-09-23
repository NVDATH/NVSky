# NVSky Development Plan (plan-04)

Carried over from the previous chat. Multi-tab architecture was mis-scoped
once already this session (see "Lesson" below) — read this whole doc
before writing structural code.

## Response format & workflow conventions

These are already saved in Claude's memory, restated here for certainty:

- Code changes go as `old_str:`/`new_str:` blocks (not git diff), applied
  via a Notepad++ script the user built with Gemini's help.
  - `old_str` needs 2-3 unique context lines before AND after the changed
    code. Never a bare short/generic line like `try:` alone as the whole
    anchor.
  - Must match the real file's exact whitespace/indentation.
  - A single contiguous change stays ONE block, not split into tiny ones.
  - State the target filename clearly right before each block.
  - When one file has multiple separate edit locations in the same
    message, number them as sub-headings under that file's heading —
    e.g. file heading, then `1.1`, `1.2`, ... before each respective
    block — so it's easy to confirm all edits were applied.
- Always read the actual current source file before proposing code for
  it. Don't rely on a plan doc (including this one) as source of truth —
  the user may have already applied or adjusted patches independently of
  what's described here.
- Chat replies in Thai. Code — including comments and UI strings — in
  English only.
- For multi-part fixes in one reply, use `###` headers to separate each
  fix point.
- `log.info()` used sparingly — only for output genuinely needed for
  debugging (e.g. a value worth copy/pasting back), not routine noise.
- Anything touching a lexicon/endpoint NVSky hasn't exercised before
  (this session: threadgate, postgate, listNotifications, getPosts) gets
  flagged EXPERIMENTAL, with a note to paste back the traceback if it
  errors.
- For anything touching overall window/UI **structure** (not just one
  function or one dialog's internals), restate the intended shape back
  to the user in plain terms before writing code — see lesson below.

## Lesson from this session (important)

Claude built `NotificationsWindow` as a fully separate `wx.Dialog` opened
via its own gesture (NVDA+Alt+N) — but the actual goal was a **single
multi-tab window**, Ctrl+Tab between Home/Notifications/etc inside one
window, opened with one command. This wasn't caught until after the user
had already patched the wrong-shaped code in and gone looking for the
tab that didn't exist. Root cause: Claude inferred the shape from
"extract a shared mixin" language instead of confirming it directly.
Don't repeat this — confirm structural shape explicitly before coding it.

## Confirmed multi-tab architecture

- **One top-level window** (`MainWindow`) containing a `wx.aui.AuiNotebook`
  (part of core wx, no new dependency — also gives tab drag-reorder for
  free, which was already planned separately).
- Opened with a single command (NVDA+Alt+B, replacing the old
  Home-only gesture). No more per-tab-type gestures.
- **Each tab is a `wx.Panel`**, not a `wx.Dialog`, embedded as a notebook
  page. `FeedWindow` and the wrongly-separate `NotificationsWindow` both
  need to be converted from `wx.Dialog` to panel form.
- **Title + status bar are per-tab** — each panel shows its own
  title/status info (not a single shared frame-level status bar). Needs
  a concrete design for how a `wx.Panel` shows "title" (frame title bar
  doesn't apply per-tab) and where each tab's status text renders —
  open implementation detail for the new chat.
- **Home is a permanent tab** — cannot be closed (Ctrl+W is a no-op on
  it, or reassign focus without removing it), but CAN be reordered like
  any other tab.
- **Closable tabs** (can be opened, left open, and removed with Ctrl+W):
  Notifications, and all future feed-like tabs (Explore, Feeds, Lists,
  Saved), AND things that used to be separate list-based dialogs —
  View Thread, User Timeline, Show Followers/Following-list, and any
  future tab of this kind. These are all `wx.ListCtrl`-driven and the
  user should be able to leave them open and refresh them, same as
  Home/Notifications.
- **Single-item info dialogs stay as `wx.Dialog`** — correct as before,
  no change needed. This covers Profile info (`ProfileDialog`) and the
  new Post Info dialog (see below). Rule of thumb: if it's a list the
  user might want to leave open and refresh, it's a tab; if it's a
  one-shot "look and close" detail view, it's a dialog.
- **`FeedListMixin` stays as the shared base** for every tab's
  lazy-load/status-bar/check-for-update/focus-restore scaffolding —
  this part of the design was correct and tested working (Home +
  Notifications data layer both already function against it). Only the
  container (Dialog → Panel) needs to change, not the mixin's logic.

## New tab-level features to add alongside the panel conversion

- **Unified action menu** — rename/merge into a single mixin (e.g.
  `ItemActionMixin`) that any tab can call, adapting which actions are
  offered based on what kind of item is focused: a real post (Like/
  Repost/Quote/Reply/Bookmark/etc. all apply), a notification wrapping a
  real post (same actions on the underlying post), or a follow
  notification with no post (only User action — follow/mute/block —
  applies). Needs `onPostAction`'s current full body (not yet seen in
  full) to design against real code, not guesses.
- **Ctrl+F5 = check every open tab for updates.** Needs a dynamic
  registry in `MainWindow` (list of currently-open tab panels, not a
  fixed set) since tabs can be opened/closed at runtime. Loop calls
  each open tab's own `onCheckForUpdates(None)`.
- **Ctrl+W = close current tab** (except the permanent Home tab).
- **Ctrl+Delete = clear current tab's cache** ("clear timeline") — wipe
  that tab's cached rows (its `feed_key` in `feed_items`, or the whole
  `notifications` table for that tab) and reload empty. Needs a new
  `db.clear_feed_cache(account_id, feed_key)` plus a notifications
  equivalent.
- **Check-for-updates / Fetch-previous-posts (Shift+F5) messages must
  use each tab's own name** — already works via `self.TAB_NAME`, just
  needs to keep working after the Dialog→Panel conversion.
- **Post Info** (new) — dialog showing: author, full post text, time,
  reply count, repost count, like count — mirroring what the feed
  screen already shows at a glance, laid out for a one-shot read. Add
  as a new item in the unified action menu.

## Before writing any of this code

Ask for **current full `feedWindow.py` + `__init__.py`** again at the
start of the next chat — the user has already hand-applied several
rounds of patches (including working around the Dialog/Panel mixup by
patching what was sent), so Claude's last-known copy of these files is
stale relative to what's actually on disk. Read fresh before proposing
any structural changes, especially `onPostAction`'s full body.

## Already shipped and working (don't redo)

- Home timeline: lazy-load, status bar, check-for-updates, mark-read,
  jump-to-user, select-all — all tested working, including the repost
  feed-ordering fix (feed_items.indexed_at uses the repost's own
  timestamp for reposts, not the original post's indexed_at) and the
  Shift+F5 manual "fetch previous posts" replacement for the old
  auto-lazy-load-on-scroll (which had UX confusion issues).
- `feed_items` mapping table (account_id, feed_key, uri, indexed_at) —
  decouples "post content cache" (`posts`, deduped by uri) from
  "which feed(s) contain this uri and in what order" — this is what
  makes multiple post-based tabs (Explore/Feeds/Lists/Saved) safe to
  add later without collisions.
- Edit-who-can-reply redesign (threadgate + postgate) — tested working.
  One small polish item still open: initial dialog focus should land on
  the radio box, not the OK button (may already be fixed — verify).
- Notifications data layer (db table, `sync_notifications`,
  `resolve_posts` for always showing the original liked/reposted/
  replied/mentioned/quoted message text) — tested working data-wise.
  ONLY the UI container is wrong (separate Dialog instead of a tab) —
  the db.py and client.py pieces should NOT need to be rebuilt, just
  re-wired into a panel instead of a dialog.
- Version numbering restarted at 1.01 as of the start of the multi-tab
  phase.

## Still separately open (not part of this multi-tab push, don't lose track)

- Discover/For You feed filter (stub)
- Sound system, Background fetch scheduler, i18n pass, General
  fetch-interval/Display sort-order wiring — none started
- Enter-key quick-action — coded, may need revisiting once the full
  action inventory (including the new unified ItemActionMixin) is
  finalized
- Mute words & tags, Profile editing — coded, believed working
- Video attachment support for compose (or confirming nothing else is
  missing there) — open question
- Chat/DM tab — deliberately last, needs its own `chat.bsky.*` lexicon
  research from scratch
