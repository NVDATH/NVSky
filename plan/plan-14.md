# NVSky — plan-14.md

Continuation of plan-13. This was an extremely large session covering: finishing
the `FeedListMixin` refactor, converting three read-only dialogs (Thread,
UserTimeline, followers/following) into full removable tabs with cache-first
persistence, building a complete background sync scheduler from scratch, and
adding end-to-end video upload support to `ComposeDialog`. Nearly everything
below is confirmed working by the user via real testing — this was a very
debug-heavy session with several real production bugs found and fixed live
(focus races, a Windows-freezing log bug, wrong auth scopes, a NULL sort bug).

---

## 1. Confirmed DONE this session

### FeedListMixin refactor (finishing plan-13 §3)
- Added `_buildStandardFeedSizer(extra_top=None, extra_action_widgets=None)`,
  `_bindStandardFeedEvents()`, `_finishStandardFeedInit(sync_if_empty=False)`
  to `FeedListMixin` — collapses the previously-duplicated `__init__`
  boilerplate across `FeedWindow`/`SavedWindow`/`ListTabWindow`/
  `FeedPreviewTabWindow`.
- Converted all four classes to use the new helpers. **Confirmed working** —
  tab names, F5/Ctrl+F5, filter switching all unaffected.

### Bug fixes found via real testing (not part of a planned feature)
- **`db.get_convos()` NULL-sort bug**: a brand-new empty group (no
  `last_message_sent_at` yet) sorted to the very BOTTOM of the conversation
  tree instead of the top, because SQLite treats NULL as the lowest value in
  `ORDER BY ... DESC`. Fixed with a `CASE WHEN ... IS NULL THEN 0 ELSE 1 END`
  prefix sort key.
- **`ListTabWindow`/`FeedPreviewTabWindow` missing `origin_key`**: didn't
  return to the tab they were opened from when closed via Ctrl+W. Added
  `origin_key` param + `db.add_open_temp_tab` persistence to both, matching
  `ConvoTabWindow`'s existing pattern.
- **`FeedPreviewTabWindow` missing `SUPPORTS_SELECT_ALL = True`** — Ctrl+A
  mark-read never worked on this class since it launched. Added.
- **Settings > General "Clear cache" button** relabeled to "Clear Home
  cache" — it only ever cleared the Home feed, label was misleading.
- **Explore's `wx.CollapsiblePane` double-announces on first focus** —
  confirmed via testing (and independently corroborated by another local
  project's own notes) that `wx.CollapsiblePane` fires TWO Windows UIA
  accessibility events on Windows (its own wrapper + the native disclosure
  triangle child), unfixable from the app side. **Replaced entirely with a
  plain `wx.Button` + `wx.Panel`** (atomic control, single announcement,
  confirmed fixed).
- **`FeedListMixin._restoreFocusPosition`**: `Select()`/`Focus()` called
  BEFORE `SetFocus()` left `GetSelectedItemCount()` reporting 0 until the
  user pressed an arrow key — broke Alt+U/Alt+A right after opening/
  switching to a tab. Fixed by moving real focus first via
  `wx.CallAfter`-deferred `_applyFocusPosition`, and by explicitly clearing
  any stray prior selection before selecting the target row (root cause of
  a related "User action needs a single post selected" false-positive,
  confirmed via a debug log showing `selectedCount=2`).
- **Space jump-to-unread wrong direction**: both `chatWindow.py`'s
  `_focusNextUnreadMessage` and `feedWindow.py`'s `FeedListMixin.
  _focusNextUnread` iterated the on-screen array order directly, which
  under newest-first sort meant Space jumped to the NEWEST unread item
  first instead of the oldest. Fixed to always progress chronologically
  (oldest-unread-first) regardless of sort order, in both files.
- **`ChatWindow._reloadAfterBulkCheck`** assumed `_selectConvoById`
  (`TreeCtrl.SelectItem`) never grabs real OS focus — false: rebuilding
  `convoTree` (`DeleteAllItems`+re-append) pulls real focus onto itself
  even from a background/hidden panel. Fixed by explicitly capturing/
  restoring whichever control had real focus before the reload.

### `UserListMixin` (new) + three dialog→tab conversions
User explicitly redefined project direction mid-session: **ListCtrl views
with actions belong as removable, cache-first, persisted, dedup-on-reopen
tabs; pure read-only info views stay as dialogs.** Converted accordingly:

- **`UserListMixin`** (new, `feedWindow.py`) — shared render/focus/action
  logic for any "browsable list of users" view (columns: Handle/Display
  name/Bio). Used by `UserListTabWindow` and Explore's People tab.
- **`UserListTabWindow`** (replaces `UserListDialog`) — covers THREE kinds:
  `followers`, `following` (fixed to one account's did), and `search`
  (Explore People → "Open in new tab", dedup key `f"search:{query}"`).
  Cache-first via new `db.get_user_list_cache`/`set_user_list_cache`
  (silent background refresh, only re-renders/announces if data actually
  changed). Persists across sessions via `open_temp_tabs` type
  `"user_list"`; cache deleted on Ctrl+W close.
- **`UserTimelineTabWindow`** (replaces `UserTimelineDialog`) — full
  `FeedListMixin` host (unlike the other two, this one genuinely fits
  chronological pagination). New `client.sync_author_feed_page()` writes
  into `feed_items`/`posts` under `feed_key = f"user_timeline:{did}"` —
  `get_author_feed()` (old, non-caching) kept for any other caller. Gets
  real "load older" via Shift+F5 for free.
- **`ThreadTabWindow`** (replaces `ThreadDialog`) — deliberately NOT a
  `FeedListMixin` host (a thread is a tree, not paginated chronology; F5
  always re-fetches the WHOLE thread). Cache-first via the same
  `user_list_cache` helper (key `f"thread:{root_uri}"`, reused since the
  shape — a plain list of post dicts — is identical). On refresh, restores
  focus to the same post uri if still present (a genuinely new reply CAN
  appear mid-refresh), falling back to the original target post.
- **`RemovableTabMixin`** (new) — extracted `_updateTitle` (was missing the
  account-label suffix, fixed both here AND separately in `ConvoTabWindow`
  which has its own copy) and `_jumpBackToOrigin`/`onTabRenamed` stub,
  deduplicating logic that had been copy-pasted across `ListTabWindow`,
  `FeedPreviewTabWindow`, `UserListTabWindow`, `UserTimelineTabWindow`,
  `ThreadTabWindow`, and `chatWindow.py`'s `ConvoTabWindow`.
- **`db.get_followers`/`get_follows`**: now also return `description` (bio)
  — was already available on the API response, just never read. Bio now
  shows consistently everywhere (followers/following/people-search).
- **`MainWindow.focusTabByIdentity(identity)`** (new) — dedup helper, no
  fallback to index 0 (unlike `findTabIndexByIdentity`), used by all three
  new tab types before creating a duplicate.

All confirmed working via real testing: open/dedup/reopen, origin_key
jump-back, cache-first + silent background refresh, cross-session persist,
Ctrl+W cache cleanup.

### Background sync scheduler (`bgsync.py`, new — entirely new subsystem)
Fully designed and implemented from scratch this session, in direct
response to the user wanting this promoted from backlog item #2.

- **8 categories**, each with its own interval setting (minutes; 0 =
  disabled) and its own smart default:

  | Category | Covers | Default |
  |---|---|---|
  | Home | Home tab, respecting the user's ACTUAL selected filter (see below) | 5 |
  | Chat | Chat tab + every open `ConvoTabWindow` | 2 |
  | Notifications | Notifications tab | 3 |
  | Saved | Saved tab | 0 |
  | Lists | Lists tab + open `ListTabWindow`s | 10 |
  | Search | Open `FeedPreviewTabWindow`s (both feed-preview AND post-search — user explicitly merged these, reasoning: someone following a hashtag/topic wants it just as fresh as a raw search) | 3 |
  | Profile | Open `UserListTabWindow` (followers/following/people-search) + `UserTimelineTabWindow` | 15 |
  | Thread | Open `ThreadTabWindow`s (full re-fetch each time, no incremental option exists) | 5 |
- **`db.get_home_active_filter`/`set_home_active_filter`**: `FeedWindow`
  persists its currently-selected filter (Following/Discover/custom feed
  uri) on every change AND at construction, specifically so background
  sync — which may run while `FeedWindow` isn't even constructed — knows
  which feed the user actually cares about right now instead of always
  assuming plain "Following".
- **`bgsync.py`**: one `sync_*(atprotoClient, account_id, ...)` function per
  category, pure functions with no wx/panel access at all (so they work
  identically whether `MainWindow` is open or not). Each returns
  `(changed: bool, names: list[str])` — the actual tab/conversation/list
  NAMES that changed, not a generic category label (user explicitly asked
  for this after finding "New activity in Chat" unhelpfully vague).
- **`GlobalPlugin`**: one `wx.Timer` ticking every 60s, checks each
  category's `db.get_bg_sync_last`/`get_bg_sync_interval`, and — if any are
  due — runs ONE shared background thread that logs in ONCE and reuses that
  same `atprotoClient` across every due category in that tick (deliberately
  avoiding the concurrent-login race that plan-13 identified as the
  probable real cause of a prior `WinError 10038`).
- **Speaks per-category, immediately after each sync finishes** (not
  batched into one combined announcement at the very end) — user explicitly
  preferred this ("วุ่นวายดี 5555 สุดท้ายผู้ใช้ก็จะปิด").
- **Only reloads the tab actually ON SCREEN**, never a hidden/background
  panel — confirmed via testing that reloading a hidden `ChatWindow` (while
  a `ConvoTabWindow` was the visible tab) silently stole real OS focus away
  from the visible panel, because rebuilding `convoTree`'s native items
  pulls focus onto itself even while not shown at all.
- **Settings > General**: reordered to Enter-action-choice → Speak
  checkbox → 8 interval spinners (labeled, 0=off) → Clear Home cache
  button. `onTabActivated` focuses the first control.
- **Critical production bug found+fixed**: `db._get_connection()`
  deliberately keeps one sqlite connection alive per OS THREAD for that
  thread's whole lifetime (a documented, intentional perf optimization) —
  but `bgsync`'s worker spawned a FRESH `threading.Thread` every single
  tick, so every tick leaked one more never-closed connection. Confirmed
  via a real traceback (`MemoryError` inside `db.set_bg_sync_last`) after
  enough ticks accumulated. **Fixed** with `db.close_all_connections()` in
  a `finally:` block wrapping the whole per-tick sync loop.
- **Critical production bug found+fixed (freeze/hang)**: `client.
  sync_convos`'s exception handler used to `log.info(f"...= {convo!r}")` —
  dumping a full raw `ConvoView` object (deeply nested, 30KB+ as a single
  line) straight into the NVDA log on ANY sync failure (e.g. a transient
  502 from Bluesky). NVDA's log uses a shared lock across the whole
  process; one huge synchronous write held it long enough to freeze NVDA
  speech and, per the user's own real-world report, apparently Windows
  itself for roughly a minute. This had always been a latent risk (any
  manual F5 failure could trigger it) but was rare — background sync
  hitting `sync_convos` automatically every ~2 minutes made it much more
  likely to actually occur. **Fixed** by removing the raw-object dump
  entirely (kept just the short error message); explicitly chosen over
  redirecting to `debug_dump()` (a file write) after the user pushed back
  that a transient 502 doesn't warrant a dump at all — log the message,
  move on.

### New-post optimistic insert into Home cache (feature, not a bug fix)
- `client.create_post` now returns `{"uri","cid","created_at","text"}`
  (was previously discarding the response entirely).
- `compose.py`'s `_onPostDone` calls a new `_insertOptimisticPost()` that
  writes the just-created post directly into the Home ("following")
  feed_items/posts cache and quietly re-renders an already-open Home tab
  IN PLACE (`moveFocus=False`-equivalent — never steals focus) — so a
  freshly posted item (including video, once that landed — see below) is
  visible immediately without waiting for the next sync/F5. Any failure
  here is non-fatal (swallowed) since the post itself already succeeded
  server-side regardless.

### Video upload support (large new feature, end-to-end in `ComposeDialog`)
Extensive live debugging session against real (LOW CONFIDENCE,
never-before-exercised) Bluesky video endpoints — several real production
bugs found and fixed via actual HTTP error codes and NVDA log captures,
not guesses. Final state confirmed fully working by the user.

- **`client.validate_video_file()`**: extension check (`.mp4/.mpeg/.mpg/
  .mov/.webm`), file-size check, and best-effort MP4/MOV duration via a
  hand-written ISOBMFF `moov`/`mvhd` box parser (`struct`, no external
  library) — WebM duration deliberately NOT parsed (different container
  family, user confirmed low priority). Current known limits hardcoded as
  `VIDEO_MAX_DURATION_SECONDS = 600` / `VIDEO_MAX_BYTES = 300MB`, with an
  explicit LOW-CONFIDENCE comment noting Bluesky has changed this limit
  repeatedly (60s→3min→10min) and will likely change it again — server is
  always the final authority regardless.
- **`client.get_video_upload_limits()`**: queries
  `app.bsky.video.getUploadLimits` for the account's real daily quota
  (`canUpload`/`remainingDailyBytes`/`remainingDailyVideos`) — confirmed
  no duration/size/format limits are exposed via this endpoint, only quota.
- **Auth — two real production bugs found and fixed live**:
  1. First attempt used one shared service-auth helper for all three video
     endpoints — got a real 401. Root cause (confirmed via GitHub
     discussions #4437/#400): `uploadVideo` needs `aud` = the user's OWN
     PDS (resolved via `client._session.pds_endpoint`, NOT
     `client.me.did_doc` which doesn't exist on this SDK version — also
     confirmed via a real `AttributeError`) with `lxm =
     "com.atproto.repo.uploadBlob"` regardless of which endpoint is
     actually being called; `getUploadLimits`/`getJobStatus` need the
     OPPOSITE — `aud = "did:web:video.bsky.app"` with `lxm` = the real
     endpoint name. Split into `_get_upload_video_auth()` and
     `_get_video_query_auth(lxm)`.
  2. **409 Conflict is not a real failure**: re-uploading the exact same
     video bytes returns HTTP 409, but the response BODY (readable via
     `HTTPError.read()`) contains a valid `JOB_STATE_COMPLETED` job status
     with a real usable blob — confirmed via a real 409 in testing,
     matching community documentation. `upload_video()` now catches this
     specifically and treats it as success.
- **`client.upload_video(client, path, progress_callback, cancel_event)`**:
  uploads, then polls `getJobStatus` every 3s (up to ~10 min) until
  `JOB_STATE_COMPLETED`/`JOB_STATE_FAILED`. Confirmed via a real captured
  log that `progress` IS a genuine, usable percent (not always 0) — seen
  climbing 0→70→100 across real states `JOB_STATE_ENCODING` →
  `JOB_STATE_UPLOADING` → `JOB_STATE_COMPLETED`, plus a previously-unseen
  `JOB_STATE_SCANNING` state discovered live during testing.
- **`VideoUploadDialog`** (new, `compose.py`): shows real upload/processing
  progress — gauge uses the real percent from the API when available;
  status text AND dialog title AND a `wx.StatusBar` all show the same
  current-phase message (title/statusbar were both explicitly requested
  additions, reasoning: NVDA may auto-announce a title change, and a
  status bar reads as more discoverable than a plain label). 7-level
  ascending `tones.beep()` sequence via a continuous 1s-interval
  `wx.Timer` (NOT a one-shot beep per state change — user explicitly
  called out that this needed to keep beeping continuously while waiting,
  matching `feedWindow.py`'s existing `_startLoadingBeep` pattern), one
  distinct pitch per phase (dialog-open → uploading → queued → scanning →
  encoding → uploading-to-bluesky → completed), with an explicit note that
  the phase LABELS are chosen for a clear sense of forward progress for
  the user rather than to exactly mirror server-side semantics ("ให้ผู้ใช้
  พอรู้ความคืบหน้า ข้อความชัดเจน ถึงจะไม่ตรงหลังบ้านเป๊ะๆ"). An unrecognized
  future state falls back to a generic "Processing {filename}..." phrase
  (never shows the raw state string to the user) and logs the real value
  for future `_STATE_INFO` additions.
- **Cancel semantics** (explicitly redesigned mid-session per user
  feedback): closes the dialog IMMEDIATELY (`EndModal` right away, doesn't
  wait for the background thread to notice) — no spoken "Cancelling..."
  (the user pressed Cancel themselves, no need to confirm it verbally).
  `_onDone` checks the cancelled flag FIRST and discards ANY result
  (including a fully successful blob) if cancellation was requested,
  regardless of how far the upload/processing had gotten. No server-side
  video-job-cancel endpoint exists (LOW CONFIDENCE, none found) — this is
  purely a client-side discard.
- **`ComposeDialog`**: single **"Attach media..."** button (image+video
  unified per explicit user request, rather than two separate buttons) —
  `wx.FileDialog` with a combined image+video wildcard. Mutual exclusivity
  enforced via `wx.MessageBox` dialogs (NOT passive status-label text —
  user explicitly asked for this since a spoken status label can be gone
  before the user catches up): images+video can't mix, only one video per
  post. Alt text for video prompted the same way images already are, after
  a successful upload. Oversized-image rejection also converted from a
  status label to `wx.MessageBox` for the same reason.
- **`_insertOptimisticPost`**: extended to build a proper
  `{"$type": "app.bsky.embed.video", "alt": ...}` embed_json for the
  optimistic Home-feed row (was initially missing both the embed entirely
  AND, in a follow-up fix, the alt text specifically — found via two
  separate rounds of live testing). `client._extract_embed_info`/
  `_fill_media_info` extended to also carry through `video_alt` on a real
  synced post (was previously only extracting `video_url`/
  `video_thumb_url`). `feedWindow.py`'s `_describe_embed` now shows
  `"Video: <alt text>"` in the Embed column when present.
- **`_buildViewEmbedMenu`**: a video post that was JUST optimistically
  inserted (no `video_url` yet, only the embed `$type`) used to silently
  produce NO embed menu at all when Alt+A was pressed (confirmed crashed
  once too, via a `NameError` from a variable-name typo in an early
  version of this fix — `embed_type` vs the correct `embed.get("$type")`,
  now fixed). Now shows one explicit menu item: "Video not ready yet
  (Check for updates first)".

### Confirmed NOT a gap (protocol limitation, closed)
- **Chat/DM attachments**: researched via `atproto` GitHub discussions
  #3326/#3490/#9393/#5276 — Bluesky's chat backend **intentionally does
  not support image/video embeds in DMs at all** ("This was an intentional
  policy decision for the service/infrastructure that we operate" — direct
  Bluesky team quote). `sendMessage`'s only supported embed type is
  `app.bsky.embed.record` (quoting/sharing an existing post). Confirmed
  still true as of the most recent linked issue (Nov 2025). **No NVSky
  work needed here** — this was on the backlog as an open question this
  session and is now definitively closed as impossible, not deferred.

---

## 2. Debugging methodology notes (worth remembering for next session)

Several bugs this session were initially mis-diagnosed by reasoning from
memory/assumption instead of real evidence, then correctly root-caused
once actual NVDA logs or debug print statements were captured. Established
pattern going forward, per explicit user preference: **when a bug's cause
isn't obvious from a single read of the code, add temporary debug logging
and ask for a real captured log BEFORE proposing a fix**, rather than
guessing and iterating blind. This directly resolved: the video 401 (wrong
`aud`/`lxm` — found via error text, not guessed), the "Alt+U says no post
selected" bug (found to be `selectedCount=2` via a debug log, not a focus-
timing theory), and the video progress percent question (confirmed real
API behavior via a raw job-status log dump before designing the UI around
it).

---

## 3. Open items for next session

### Priority 1 (user's stated next step): Ctrl+Delete — clear-current-tab-cache, planned as a full shortcut audit
User wants this planned as "a shortcut layout for every page" rather than
a one-off patch — go through every tab type and decide what Ctrl+Delete
(and any other currently-missing standard shortcut) should do there.
Needs, at minimum:
- `db.clear_feed_key_cache(account_id, feed_key)` — already exists (was
  added earlier this session for `UserTimelineTabWindow.onTabRemoved`,
  reusable here) — confirm scope: clears `feed_items` rows for one
  feed_key, not the underlying `posts` table (shared across feeds).
- A notifications-equivalent clear (`notifications` table has no per-key
  scoping the way feed_items does — needs its own helper, doesn't exist
  yet).
- Decide behavior per tab type: Home/Saved/Lists/ListTabWindow/
  FeedPreviewTabWindow/UserTimelineTabWindow (all `FeedListMixin`,
  feed_key-scoped, straightforward) vs Notifications (own table, needs new
  helper) vs Chat/ConvoTabWindow (messages table, per-convo — does
  "clear cache" even make sense here, or should it be excluded?) vs
  UserListTabWindow/ThreadTabWindow (already have their own
  `user_list_cache` clear via `db.delete_user_list_cache`, but that
  currently only fires on Ctrl+W close, not on-demand) vs Lists tab itself
  (the permanent tab, not a temp `ListTabWindow`) vs Explore (four
  different result types in one panel — which cache would Ctrl+Delete
  even target here?).

### Also requested: full method/feature audit ("dump all method recheck")
User asked whether a full pass over the codebase to find anything still
missing is warranted, given how much has shipped this session. Suggested
approach for next session: systematically walk `feedWindow.py`,
`chatWindow.py`, `client.py`, `db.py`, `mainWindow.py`, `settings.py`
class-by-class and cross-check against (a) plan-13's Group A/backlog items
not yet addressed, (b) any TODO/LOW CONFIDENCE comments left in the code
from this session that were never revisited, (c) keyboard shortcut parity
across tab types (ties directly into the Ctrl+Delete audit above).

### Explicitly deferred, not blocking
- **FastSMRW / soundpack collaboration question**: user raised, then
  explicitly decided to continue NVSky rather than pivot away from it
  ("ยังไงฉันก็คิดว่าส่วนเสริม feautures UI UX ต่างๆ ที่ฉันทำเอง ก็ยังถูกใจฉัน
  มากกว่าแอพพลิเคชันที่คนอื่นทำ ... เรามาทำสิ่งนี้ให้เสร็จ"). No action item —
  noted here only so this isn't re-litigated from scratch next session if
  it comes up again. If pursued later, it's a licensing question
  (soundpack) and a separate, optional outreach question (feature parity
  suggestions to the FastSMRW maintainer), not an NVSky code change.
- **`docs.bsky.app`'s "upload video like an image via uploadBlob"
  mention** — found via a late search, potentially a simpler alternative
  upload path than the full `video.bsky.app` job-polling flow NVSky now
  uses (which is confirmed working end-to-end). NOT investigated further
  this session — deliberately left alone rather than risk breaking a
  working implementation to chase an unconfirmed shortcut. Worth a quick
  look someday if video upload ever needs revisiting, but not urgent.

---

## 4. Backlog carried over unchanged from plan-13 (still not started)

1. Sound system — pure placeholder. Now has slightly more shape though:
   user has floated a soundpack-with-per-event-toggle design (checkbox per
   sound within a pack) in passing, not yet speced out.
2. Video attachment in `ComposeDialog` — **DONE this session**, remove
   from backlog (was item #3 in plan-13).
3. Full i18n — only `__init__.py` wraps strings in `_()`.
4. DB portability — DPAPI key is machine+Windows-user locked, known
   limitation.
5. plan-13's Group A leftovers not yet covered above: "Manage members..."
   admin gating (still no confirmed `InsufficientRole` error to justify
   it — leave alone until one shows up); self-labeling posts (adult/
   graphic content at compose time) from plan-13 §5 — still not started,
   still considered a real user-facing gap worth doing eventually.

---

## Suggested order for next session

1. Ctrl+Delete shortcut audit (§3 priority 1) — the user's explicit next
   step, framed as "finish up, wrap up loose ends."
2. Full method/feature re-check pass, likely combined with #1 since a
   shortcut audit naturally surfaces gaps elsewhere too.
3. Everything else stays backlog, not blocking.
