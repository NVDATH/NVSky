# NVSky — plan-15.md

Continuation of plan-14. This session covered: a full Ctrl+Delete cache-clear
audit across every tab type, a first-run/login flow overhaul (auto-prompt
login, no more MainWindow flashing empty before a fresh account exists), a
full Post action menu redesign with reorganized submenus and consistent
mnemonics, and a systematic feature audit of that menu (A through G) that
shipped seven real features: thread mute/unmute toggle, report user, cached
follow/mute/block (no more network-round-trip-before-confirm), pin/unpin
post to profile, view likes/view reposts, view quotes (+ detach-from-quote),
and share-post-to-chat. Several real bugs were found and fixed along the
way, most via live testing rather than code review alone. Everything below
is confirmed working by the user via real testing unless noted otherwise.

---

## 1. Confirmed DONE this session

### Ctrl+Delete cache-clear audit
Scoped deliberately, not applied everywhere:
- **In scope**: Home, Saved, ListTabWindow, FeedPreviewTabWindow,
  UserTimelineTabWindow (all via `FeedListMixin.onClearCache`, using
  existing `db.clear_feed_key_cache`), ListsWindow (clears only the
  selected curation list's timeline, never the `lists` table itself),
  Notifications (new `db.clear_notifications_cache`), UserListTabWindow
  and ThreadTabWindow (via existing `db.delete_user_list_cache`, now
  callable on-demand instead of only on Ctrl+W).
- **Explicitly out of scope**: Chat/ConvoTabWindow (messages are data the
  user wants to keep, not a re-fetchable cache; local read-state is
  entangled with server-pushed reconciliation, too risky to clear
  casually) and Explore (nothing persistent to clear — People/Starter
  packs/Feeds always fetch fresh, Posts search is already ephemeral).
- **Every clear requires a `wx.MessageDialog` YES_NO confirmation first**
  — this is now a standing rule (see memory edit below), not just a
  one-off for this feature.
- **No auto-refresh after clearing** — deliberately left to F5/bgsync,
  per explicit instruction.
- **Real accessibility bug found and fixed**: a `wx.ListCtrl` that just
  lost its last row via `DeleteAllItems()` while it already had real OS
  focus never raises a fresh `EVENT_OBJECT_FOCUS` (same HWND, no genuine
  transition) — NVDA gets stuck reporting "unknown" on the now-dead
  focused row object until a real Tab/Shift+Tab bounce forces a re-fetch.
  Several fix attempts failed (delaying with `wx.CallLater`, re-calling
  `SetFocus()`, forcing `event_gainFocus()`, a synthetic two-step focus
  bounce with `cancelSpeech()` timing tricks). **Final fix, at the
  user's suggestion**: on empty-list-after-clear, move focus to the
  MainWindow toolbar's "Check for updates" button instead of fighting
  the ListCtrl's own focus state — doubles as a natural nudge to reload.
  No explicit announcement needed; the button's own accessible label
  confirms the action worked.
- **Memory edit added**: any action that deletes/clears data must always
  show a confirm dialog first — never delete silently on a single
  keypress/click. This is now a persistent rule Claude must follow in
  all future NVSky work.

### First-run / login flow overhaul
- **No account at all, open NVSky** → `LoginDialog` pops up directly,
  with **no MainWindow ever created/shown first**. Earlier attempts
  (delaying the prompt with `wx.CallLater`) failed because MainWindow
  was still fully visible+speaking "No active account" in every tab
  before the delay elapsed — the fix was to skip building MainWindow
  entirely when `db.get_active_account() is None`, not to delay showing
  the login prompt after MainWindow already exists.
- Still deferred via `wx.CallAfter` before showing `LoginDialog`
  — `ShowModal()` called synchronously from an NVDA gesture handler's
  own call stack has crashed NVDA before (documented precedent:
  `ComposeDialog`'s docstring).
- `_promptFirstLogin()` now correctly wraps its `ShowModal()` in
  `gui.mainFrame.prePopup()`/`postPopup()` (was missing initially,
  confirmed via testing to break the first-open experience).
- Login success → `_openMainWindow()` (fresh construction with the real
  account) → `_runInitialFullSync()`: a new full-sync path, separate
  from the periodic 60s tick, that ignores every per-category interval
  setting and syncs **all 8 bgsync categories** unconditionally, then
  reloads **every open tab matching each changed category** (periodic
  sync only reloads the currently-visible tab — this path is more
  thorough since freshly-built tabs may all be empty at once).
- Login cancelled → jumps to Settings > Accounts, focused on "Add
  account..." (not the account list, which is empty) — required a
  small `AccountsPanel.onTabActivated` fix (`else: self.addButton.
  SetFocus()`) since it only handled the has-accounts case before.
- **Login-via-Settings-instead-of-the-popup gap found and fixed**: if
  the user cancels the direct `LoginDialog` and instead logs in through
  Settings > Accounts > Add account, `_onSettingsAccountChanged` used to
  just call `_rebuildTabs()`, which silently no-ops if `_mainWindow is
  None` (which it always is on this path, since MainWindow was never
  created) — closing Settings afterward left nothing visible at all.
  Fixed: the callback now checks `if self._mainWindow is None:
  self._openMainWindow(); self._runInitialFullSync()` before falling
  back to `_rebuildTabs()`.
- **Remove-account flow, multiple rounds of fixes**:
  1. `AccountsPanel.onRemove` was missing the `self._onAccountChanged()`
     call entirely (present on `onAdd`/`onSetActive` but not this one)
     — removing an account while MainWindow was open left it showing
     stale cached tabs until a full NVSky restart. Fixed.
  2. Once fixed, a new problem surfaced: `_rebuildTabs()` calls
     `activateInitialTab(0)`, which grabs real OS focus onto MainWindow
     via `wx.CallAfter` — this fired **while Settings was still open**,
     yanking focus away mid-flow. Fixed by stripping all
     focus-grabbing/announcing out of `_rebuildTabs()` entirely — it now
     silently rebuilds tab data in the background, leaving Settings in
     full control of focus until the user closes it themselves.
  3. Focus after remove should land on "Add account..." within Settings
     (not the now-shorter account list) — same fix as the cancelled-login
     case above, `AccountsPanel.onRemove` now calls
     `self.addButton.SetFocus()` instead of `self.accountList.SetFocus()`.
- **Explicitly decided NOT to add**: auto-reopening `LoginDialog`
  automatically the moment the last account is removed (would have
  mirrored the fresh-install flow) — user preferred requiring an
  explicit "Add account..." click instead, now that focus correctly
  lands there.

### `bgsync.py` Lists fix (real bug, not a symptom of the audit above)
- `sync_lists()` previously only synced the **timeline** of
  already-open `ListTabWindow` temp tabs — it never fetched the
  account's own list of lists (`client.get_lists()`) at all, so
  `ListsWindow`'s tree stayed permanently empty on a fresh account until
  a manual F5. Fixed: now also upserts the `lists` table, same as
  `ListsWindow._syncListsFromServer`.
- A second pass fixed a related gap: even after the list-of-lists synced,
  the **timeline content** of a curation list only ever synced if it
  happened to be open as its own `ListTabWindow` — the `ListsWindow`
  tree itself (which shows every list's content on selection, not just
  one at a time) was never covered. Fixed: `sync_lists()` now syncs the
  timeline of **every curation list the account owns**, not just
  whichever `ListTabWindow`s happen to be open — this also incidentally
  solves the first-run case (no `ListTabWindow` needed to exist yet for
  content to populate).
- `SYNC_FUNCTIONS["lists"]` changed from `needs_my_did=False` to `True`
  to support the new `client.get_lists(atprotoClient, my_did)` call this
  required.

### Post action menu — full redesign
Previously 10 flat top-level items (11 for own posts). Reorganized into
submenus to keep the top-level list short even as new features were
added, with every item given a unique, meaningful mnemonic:

```
[own post only]
  &Manage post... ▸  &Pin/unpin to profile... / &Edit who can reply... / &Delete post...
  ───────────
&Reply...
Re&post / Undo re&post
&Quote post...
───────────
Mar&k as... ▸  &Read / &Unread
&Like / Un&like
&Save / Un&save
───────────
&Copy... ▸  Copy &post text / Copy &link to post
&View... ▸  View &thread... / View &likes... / View &reposts... / View &quotes...
&Embed...  (if present)
───────────
M&ore... ▸  &Share to chat... / &Mute thread (Un&mute thread) / &Hide post for me / &Report post...
```

- "View thread..." moved out of top-level into the new `&View...`
  submenu (now joined by likes/reposts/quotes).
- Own-post-only actions (Edit who can reply, Delete post) consolidated
  under `&Manage post...` instead of sitting bare at the top with a
  separator.
- **Plain `Delete` key** (no modifier) now works directly on `postList`
  when it has focus — deletes the post if it's the user's own (still
  goes through the existing confirm dialog inside `_deletePost`, no
  shortcut around the confirm rule), or **undoes a repost** if the
  focused row is the user's own repost of someone else's post (no
  confirm needed for this, matching the existing no-confirm convention
  for Like/Repost toggles — reversible, not destructive).
- `EVT_CONTEXT_MENU` (Applications key / Shift+F10) audit: found and
  fixed **5 missing bindings** across post-list `ListCtrl`s that only
  had Alt+A as their trigger — `FeedListMixin._bindStandardFeedEvents`
  (covers FeedWindow/SavedWindow/ListTabWindow/FeedPreviewTabWindow/
  UserTimelineTabWindow in one fix), `NotificationsWindow` (also found
  missing `EVT_LIST_ITEM_ACTIVATED` — Enter/double-click did nothing
  despite Settings > General's "Enter key action" existing),
  `ListsWindow`, `ExploreWindow`, `ThreadTabWindow`. A 6th related gap
  (`UserListTabWindow`'s `userList`, not itself a post list but the same
  class of gap) was found and fixed too, per explicit "make the whole
  app's UX consistent" instruction.

### Post action menu — feature audit (A through G), all shipped
Methodically compared existing menu items against everything
`client.py` already wrapped-but-never-wired-up, plus real Bluesky/AT
Protocol features with no wrapper at all yet.

**A — Mute/unmute thread now toggles correctly** (was label-only,
always said "Mute thread" even when already muted). Required adding
`posts.viewer_thread_muted` (new column, no migration — DB cleared
instead per project convention), populated from `viewer.thread_muted`
on every post-storing path in `client.py` (`_store_feed_item`,
`_store_resolved_post`, `_post_view_to_dict`), plus `compose.py`'s
optimistic-insert dict. **Made optimistic** after initial round-trip
version felt slow — announces immediately via `_onActionDone` (the
`EmbedViewMixin` helper that handles the focus/speech race correctly),
rolls back on server error.

**B — Report user** added to `UserActionMixin`'s menu (paired with the
existing user relation toggles). New `client.create_actor_report` —
**had to be rewritten as a raw-JSON bypass** after the first typed-call
attempt hit a real pydantic error (`Aliases for discriminator 'py_type'
must be the same (got $type, pyType)`) on the `repoRef` subject shape,
same class of SDK bug documented elsewhere in this file for other
discriminated-union bodies.

**Follow/Mute/Block menu — upgraded from ambiguous to real-state,
optimistic toggles**. Previously always showed generic "Follow /
Unfollow" (etc.) labels requiring a network round-trip + confirm dialog
before the user found out which direction they'd actually be toggling.
Now: `authors` table gained `viewer_following`/`viewer_muted`/
`viewer_blocking` columns (same DPAPI-free-migration convention, DB
cleared), populated from `author.viewer` on every author-storing path
(`_store_feed_item`, `_store_resolved_post`, `_store_notification`).
Menu reads the cache and shows the real label ("Follow" vs "Unfollow"
etc.) immediately, with **no confirm dialog needed anymore** (the label
already states the truth) — optimistic toggle + rollback on error, same
pattern as A. **Explicit fallback preserved**: if an author was never
cached from a post/notification (rare in practice — most users are
encountered via a post first), falls back to the old ambiguous
label + network-check + confirm flow unchanged.

**D — Pin/unpin post to profile**. New `client.get_pinned_post_uri`/
`pin_post_to_profile`/`unpin_post_from_profile`, using the existing
`_get_profile_record`/`_put_profile_record` raw-record pattern (same as
avatar/banner edits). **Real bug found via testing**: `_get_profile_
record`'s typed-model fallback branch (when the server returns a
non-dict-like record) built its own plain dict but forgot to include
`pinnedPost` at all — every check silently saw "not pinned" regardless
of actual state, so the confirm dialog always asked "Pin?" even when
already pinned. Fixed. Menu flow: checks real server state first (no
local cache — deliberately decided against caching this, since pinning
from another Bluesky client while NVSky is open would go undetected
until next login either way, and pinning is rare enough not to be worth
the staleness risk), confirms, then **applies optimistically** once
confirmed (was initially a network-round-trip-then-announce, fixed to
match the A/Follow pattern per explicit feedback).

**C — View likes / View reposts**. New `client.get_post_likes`/
`get_post_reposted_by` (`app.bsky.feed.getLikes`/`getRepostedBy`,
paginated like `get_followers`). Reused `UserListTabWindow`/
`UserListMixin` entirely — extended `_kind` to accept `"likes"`/
`"reposts"` with `_target` being a post URI instead of a did/query. No
bio/description available from these endpoints (unlike getFollowers) —
explicitly left blank per instruction, not fetched per-user (would be
slow for long lists). **Tab title initially just showed the post's
rkey** (meaningless fragment) — fixed to show `"<post text preview>" by
@handle` format, matching the pattern already used for View thread's
tab name.

**E — View quotes** (+ **G — Detach my post from someone's quote**,
folded in as a context action within the quotes list rather than its
own menu entry, since it needs a specific quoting post selected
first). New `client.get_post_quotes` (`app.bsky.feed.getQuotes`). New
`QuotesTabWindow` class — a simpler sibling of `ThreadTabWindow` (flat
list, no depth/indent, F5 always re-fetches fully, no incremental
sync). Detach action only offered when the *quoted* (original) post
belongs to the current account.
- **Real bug found and fixed while implementing G**: the existing
  `set_postgate_disable_quotes` (from plan-13/earlier) always wrote the
  `embeddingRules` field alone on every save, silently wiping any
  existing `detachedEmbeddingUris` on the same postgate record (and vice
  versa would have happened once detach shipped) — the two fields
  weren't being read-merged before writing. Refactored into shared
  `_get_postgate_record`/`_put_postgate_record` helpers that always
  read the full existing record and write both fields together;
  `set_postgate_disable_quotes` and the new `detach_quote`/
  `get_postgate_detached_uris` all route through these now.
- **Tab title bug, same class as C's**: `QuotesTabWindow._makeTabName`
  initially just showed the raw rkey fragment — fixed to show `Quotes:
  "<preview>" by @handle`, reading the quoted post from local cache via
  `db.get_post()`.
- New `__init__.py` restore-session branch for `type == "quotes"` temp
  tabs, symmetric with the C fix for likes/reposts persistence (both
  were initially missing the session-restore wiring, both got fixed —
  C's `"user_list"` entries needed a `post_uri` field added since the
  original schema only supported `did`-keyed followers/following/
  search entries).

**F — Share post to chat**. `client.send_message` gained an optional
`embed_ref` param, building an `app.bsky.embed.record` embed alongside
(or instead of) plain text — this mirrors how quote posts work but for
DMs, confirmed via lexicon knowledge that `text` and `embed` are
independent fields on `messageInput` (not mutually exclusive). New
`ShareToChatDialog` (`chatWindow.py`) — picks an existing accepted
conversation (no inline "start a new chat" option, `NewChatDialog`
already covers that) and now includes an optional multiline message
box, added after the user pointed out the initial version only sent an
empty-text embed with no way to add a comment.
- **Layout bug found**: the message box was placed before the
  conversation list in the sizer, but focus was set on the conversation
  list first — mismatched visual/tab order. Fixed by reordering the
  sizer to match the intended focus flow (conversation list first,
  message box second).
- **Receiving-side gap, found via testing (this was the one flagged as
  "should be complex" but the backend actually worked first try — only
  the display side needed fixing)**: messages received with a
  `record`-embed had **no display support at all** — `_sync_convo_
  messages` never parsed the `embed` field out of the raw message JSON,
  so a shared post arrived as an empty-looking message bubble. Fixed:
  new `messages.embed_json` column (DB cleared again), new `client.
  _extract_message_embed_info` (LOW CONFIDENCE — never confirmed
  against a real response shape, guessed by analogy with post embeds'
  own `record.value.text`/`record.author.handle`), new `chatWindow.
  _describe_message_embed` renders `"Shared post from @handle: <text>"`.
  **Ordering bug, twice**: first version put the shared-post
  description *before* the sender's own typed message (backwards —
  their own words should come first, shared content is supporting
  context, same reading-order convention as quote posts in the feed).
  Fixed in both `ChatWindow._messageDisplayText` and `ConvoTabWindow.
  _messageDisplayText`.

### Small unrelated bugs found and fixed along the way
- `FeedPreviewTabWindow._dbGetUnreadCount` and `ExploreWindow.
  _dbGetUnreadCount` both hardcoded `return 0` — status bar always
  showed "0 unread" even while Space (jump-to-next-unread, which reads
  each post's real `is_read` flag directly, unaffected) kept finding
  real unread posts to jump to. Both fixed to call `db.get_unread_
  count()` properly (`ExploreWindow`'s version guards on `self.
  _sourceQuery` being set, since its `_feedKey` is a placeholder before
  any search has run).
- `ExploreWindow.onUserAction` called `self._getRelevantUsers(post)`,
  a method that only ever existed on `FeedWindow` — real `AttributeError`
  crash on Alt+U in Explore's Posts results, confirmed via a pasted
  traceback. Fixed by moving `_getRelevantUsers` onto `ItemActionMixin`
  (shared by every host, same fix pattern already documented in this
  file's own code comments for `_markSelectedRead`'s prior near-identical
  history).
- `mainWindow.py`'s Settings toolbar button was missing its "(Ctrl+P)"
  keyboard-shortcut hint in the label — every other toolbar button has
  one, this one didn't. Fixed.
- `UserTimelineTabWindow` (and later `FeedWindow`, once the user
  clarified they meant the Home feed specifically) gained an
  `_onRepostChanged` hook: undoing a repost of a post that only appears
  in that feed *because* of the repost now removes the row immediately
  instead of leaving a stale repost row until next refresh — same
  pattern as `SavedWindow._onBookmarkChanged` for unsave.
- `_toggleRepost` and `_deletePost`'s completion handlers were made
  properly optimistic (repost) and had an unnecessary forced
  `onCheckForUpdates(None)` refresh removed (delete) — both were
  flagged by the user as feeling slow/uncertain during testing despite
  functioning correctly.

---

## 2. Debugging methodology notes (carried forward, reinforced this session)

- **The ListCtrl empty-focus accessibility bug** (Ctrl+Delete section
  above) is worth remembering as a documented pattern for any future
  "clear this list to zero items while it has focus" scenario in this
  codebase — the fix (bounce focus to a real, always-present control
  like a toolbar button) generalizes better than trying to force a
  fake focus event on the dead ListCtrl itself.
- **Multiple genuinely-parallel-looking bugs turned out to share one
  root cause** this session (the Ctrl+Delete-empty-focus issue across 4
  different tab classes; the missing `EVT_CONTEXT_MENU` binding across
  5-6 classes; the hardcoded `_dbGetUnreadCount() -> 0` in two separate
  classes) — worth checking for sibling occurrences across the
  `FeedListMixin`/`UserListMixin` family whenever one is found, since
  the duplication pattern in this codebase (documented extensively in
  the code's own comments) means a bug fixed in one host class often
  has an identical unfixed twin elsewhere.
- Every optimistic-UI feature added this session followed the
  project's own established `_onActionDone` pattern (from
  `EmbedViewMixin`) rather than calling `nvdaUi.message()` directly —
  the difference matters because `_onActionDone` handles the
  focus/speech race correctly (confirmed as a real, audible bug when
  skipped, see the mute-thread A section).

---

## 3. Database schema changes this session (no migrations — DB was cleared each time, per project convention)

- `posts.viewer_thread_muted` (INTEGER DEFAULT 0)
- `authors.viewer_following` (TEXT), `authors.viewer_muted` (INTEGER
  DEFAULT 0), `authors.viewer_blocking` (TEXT)
- `messages.embed_json` (TEXT)

If starting a fresh session against an existing `nvsky.db` from before
this session, it must be deleted (not migrated) before NVSky will open
correctly — this project has no real users yet, so migrations are
deliberately never written; this note exists purely as a reminder for
whoever (Claude or the user) picks this back up.

---

## 4. Open items for next session

### Explicitly next in the user's own stated plan (from plan-14 §3, revisited this session)
1. **User action menu audit** — was never done as its own dedicated pass
   the way Post action menu was; several of its items got touched
   incidentally this session (Follow/Mute/Block became cached+optimistic,
   Report user added) but a full systematic sweep (comparing against
   `client.py`'s unwired wrappers and real AT Protocol features) hasn't
   happened yet.
2. **Full method/feature audit** for the remaining untouched files —
   `chatWindow.py` was explicitly believed complete already by the user
   going into this session and wasn't touched by this session's audit
   work except for the Share-to-chat receiving-side fix; `db.py`/
   `client.py`/`mainWindow.py`/`settings.py` were only touched
   incidentally as needed by specific features, not swept end-to-end.

### Backlog carried over unchanged from plan-13/plan-14 (still not started)
1. Sound system — still pure placeholder.
2. Full i18n — only `__init__.py` wraps strings in `_()`.
3. DB portability — DPAPI key is machine+Windows-user locked, known
   limitation, not revisited.
4. Self-labeling posts (adult/graphic content at compose time) — still
   not started, still a real user-facing gap.
5. "Manage members..." admin gating — still no confirmed
   `InsufficientRole` error to justify adding it; leave alone until one
   shows up.

---

## Suggested order for next session

1. User action menu audit (§4, item 1) — natural continuation of this
   session's Post action menu work, same methodology (compare against
   `client.py`'s existing-but-unwired wrappers + real AT Protocol
   features with no wrapper yet).
2. Decide whether the remaining backlog items (sound system, i18n,
   self-labeling posts) are worth picking up now that the two biggest
   interaction surfaces (Post action, and soon User action) are in good
   shape, or whether another full-app audit pass makes more sense first.
