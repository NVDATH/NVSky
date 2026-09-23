# NVSky Development Plan (plan-05)

Handoff doc from a very long multi-tab + Chat build session. Paste this
into the new chat along with fresh copies of `feedWindow.py`,
`mainWindow.py`, `chatWindow.py`, `client.py`, `db.py`, and `__init__.py`
before starting structural work — several rounds of hand-applied patches
mean any prior chat's last-known copies are stale. Read the real files
first.

## Response format & workflow conventions (unchanged, still apply)

- Code changes go as `old_str:`/`new_str:` blocks (not git diff), applied
  via a Notepad++ Python Script plugin the user built (Python 2.7.18).
  `old_str` needs 2-3 unique context lines before AND after the changed
  code — never a bare short/generic line as the whole anchor. Must match
  the real file's exact whitespace/indentation. A single contiguous
  change stays ONE block. State the target filename clearly right before
  each block. Multiple edit locations in the same file in one message →
  number them as sub-headings (1.1, 1.2, ...) under that file's heading.
- **The user does NOT run PowerShell directly.** PowerShell-styled
  outputs seen in past sessions came from a Python script inside
  Notepad++, not a real PowerShell session — never ask them to run
  PowerShell/shell commands. Ask for a Notepad++ search-and-paste
  instead, or ask them to attach/upload the file directly.
- Their patch script does **not** check for duplicate `old_str` matches
  — if the same code pattern appears more than once in a file (e.g. the
  same key-binding tail across multiple `onCharHook` methods, or across
  Dialog classes that share a mixin), the script silently patches the
  FIRST match in the file, which is often the wrong one. Always make
  `old_str` anchors long enough / specific enough (include a unique
  nearby comment) to guarantee a single match, especially anywhere
  `UserActionMixin`/`ItemActionMixin` methods are shared across many
  classes.
- **No DB migrations during this phase.** The add-on is solo-dev beta —
  user is the only tester and can always delete the local db.sqlite and
  rebuild fresh. `db.py` must NOT use `ALTER TABLE` migration blocks for
  schema changes right now — add new columns directly into the
  `CREATE TABLE IF NOT EXISTS` statements instead. (A past ALTER-TABLE
  migration was placed in the wrong order — after the table's own
  `CREATE TABLE` — and silently no-opped on fresh installs, costing a
  long multi-turn debugging detour before being caught. Don't reintroduce
  migrations until much closer to a real public release with other
  users' data worth preserving.)
- Always read the actual current source file before proposing code for
  it. Don't rely on this plan doc as source of truth.
- Chat replies in Thai. Code — including comments and UI strings — in
  English only.
- For multi-part fixes in one reply, use `###` headers to separate each
  fix point.
- `log.info()` used sparingly — only for output genuinely needed for
  debugging.
- Flag EXPERIMENTAL for anything touching a lexicon/endpoint NVSky
  hasn't exercised before, with a note to paste back the traceback (or
  ideally the FULL traceback via `log.error(traceback.format_exc())`,
  not just `str(e)` — a bare exception message cost a very long detour
  this session before a full traceback finally pinpointed the real
  failing line) if it errors.
- For anything touching overall window/UI **structure**, restate the
  intended shape back to the user in plain terms before writing code.
  This project has burned real time twice on structural mismatches
  (NotificationsWindow built as a separate Dialog when the goal was a
  tab; Chat first built as tab-per-conversation when the goal was one
  tree+list tab) — always confirm shape explicitly, don't infer it.
- When genuinely stuck on a bug that doesn't reproduce locally (e.g. an
  SDK/pydantic quirk), the installed `atproto` package can be
  pip-inspected directly (`pip install atproto --break-system-packages`,
  then `python3 -c "from atproto_client... import inspect..."`) to check
  real model field names, aliases, and method signatures rather than
  guessing from docs pages that don't render their schemas. This found
  several real bugs this session (bookmark field names, chat.bsky.convo.*
  field names, a couple of genuine SDK serialization bugs — see below).

## Confirmed multi-tab architecture (built and working)

- **`MainWindow(wx.Frame)`** holds a plain **`wx.Notebook`** (NOT
  `wx.aui.AuiNotebook` — the AUI notebook has broken per-tab
  accessibility, NVDA read tab names concatenated together; plain
  `wx.Notebook` reads cleanly, "Home tab selected" etc.). Everything
  (toolbar + notebook) is wrapped in one `wx.Panel` parented to the
  Frame — `wx.Frame` does NOT apply dialog-style Tab-key traversal
  across its direct children the way `wx.Panel`/`wx.Dialog` do, so
  without this wrapper Tab could never reach the toolbar.
- **Persistent toolbar** (in `MainWindow`, shared across every tab):
  Check for updates (F5 fallback), New post (Ctrl+N fallback), Settings
  (opens NVDA Settings to NVSky's category via
  `gui.mainFrame.popupSettingsDialog`), Remove current tab (Ctrl+W —
  hides itself via `_updateRemoveTabButton()` when the current tab isn't
  removable), Close (Escape/Alt+F4 — closes the whole MainWindow).
- **Close vs Remove terminology, final and consistent everywhere:**
  "Close" = closing the whole `MainWindow` (Escape/Alt+F4/toolbar Close
  button). "Remove" = Ctrl+W taking the current TAB out of the notebook
  — no-op on permanent tabs, never called "close" anywhere in code/UI to
  avoid the confusion that cost a full round of back-and-forth earlier.
- **Ctrl+1-9** jumps to tab N by position (`MainWindow.onCharHook`).
- Opened via NVDA+Alt+B (`script_openFeed` in `__init__.py`), which
  creates `MainWindow` and adds each permanent tab in order via
  `addTab(panel, label, select=..., removable=False)`.
- **Permanent tabs** (Ctrl+W no-ops): Home, Notifications, Saved, Chat.
  Future primary sections (Explore, Feeds, **Lists — next up**) are also
  permanent tabs, not closable.
- **Removable tabs** (opened from an item's action menu, Ctrl+W takes
  them out): View Thread, User Timeline, Followers/Following list — NOT
  YET converted from their old `wx.Dialog` form to tab-panel form (this
  conversion never actually got done during the Home/Notifications push
  — see backlog below). Chat's "Open in new tab..." pop-out
  (`ConvoTabWindow`) is the one removable tab that IS done.
- **Per-tab focus/title mechanics** (`feedWindow.py`'s `FeedListMixin`):
  - `_restoreFocusPosition(moveFocus=True)` — restores saved
    list-position/selection always; only grabs REAL OS/screen-reader
    focus when `moveFocus=True`. Tab panels call it with
    `moveFocus=False` in their own `__init__` (a panel may be
    constructed before it's even the tab meant to be visible — grabbing
    real focus there caused two tabs' content to get announced
    back-to-back on open). `MainWindow.addTab()`'s `wx.CallAfter` grants
    real focus once, correctly, after notebook layout settles; a tab's
    own `onTabActivated()` (called by `MainWindow.onPageChanged`) also
    calls it with default `moveFocus=True` when regaining focus via
    tab-switch, AND explicitly speaks `"{TAB_NAME} tab"` first —
    `wx.Notebook`'s own tab-selected speech was losing the race against
    the focus change.
  - `_updateTitle()` — sets the notebook tab label to just `TAB_NAME`
    (short), and pushes the fuller `"{TAB_NAME} - NVSky - {handle}"`
    onto the **shared MainWindow title bar** only while that tab is the
    currently active one. Uses `notebook.FindPage(self)` (wx.Notebook;
    NOT `GetPageIndex`, that's AuiNotebook-only).
  - `_updateActionButtons()` — hides Post/User action buttons entirely
    when the tab's list is empty (`self._posts` is empty), rather than
    leaving a visible button that just replies "No post selected."
  - `_getSelectedPosts()` lives in `FeedListMixin` (shared) — was
    originally FeedWindow-only, moved during this session after
    NotificationsWindow's bulk actions hit an AttributeError.
- **`ItemActionMixin`** (Post action menu, Alt+A, shared by FeedWindow /
  NotificationsWindow / SavedWindow): host class supplies
  `_getActionablePost()` — FeedWindow/SavedWindow return the focused
  item directly; NotificationsWindow resolves via a `subject_uri` column
  (see Notifications below). `_showBulkPostActionMenu` differs per host
  where bulk semantics differ (Notifications' bulk mark read/unread
  marks the NOTIFICATIONS themselves via a dedicated
  `_markSelectedNotificationsRead`, not the underlying posts).

## Tab-by-tab status

### Home — DONE
Following (`get_timeline`) and Discover (Bluesky's real "whats-hot" feed
generator, `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`
via `app.bsky.feed.getFeed`) both wired and working. "For You" was
**deliberately dropped** — confirmed (both via API research and the
user's own live test) that it's not a Bluesky system feed; it was a
specific pinned third-party custom feed that Bluesky has since sunset
("This feed is no longer online"). `filterRadio` now has only 2 options.

### Notifications — DONE
Full parity with Home: Post action (Alt+A) works on notification items
via `subject_uri` (added to the `notifications` table — for
reply/mention/quote it's the notification's own post; for like/repost
it's `reasonSubject`; follow has none). `resolve_posts`/`sync_notifications`
fully hydrate every notification's actionable post into the local
`posts` cache (not just display text) so Post action has real data
(cid, viewer like/repost/bookmark state). `app.bsky.notification.updateSeen`
is called after every sync so the unread badge in the official app/other
clients clears too, not just NVSky's local `is_read`. Bulk select-all →
mark read/unread works via the dedicated
`_markSelectedNotificationsRead` (marks the notifications, not their
underlying posts — bulk-liking/replying to N notifications' posts at
once was never coherent, so Post action's bulk menu stays single-item
only, same as before).

### Saved — DONE
Backed by `app.bsky.bookmark.getBookmarks` (response field is `.item`
for the full hydrated post — NOT `.subject`, which is just a bare
strongRef with no author; confirmed from a real error log). No filter,
no unread tracking (`_tracksUnread=False` — a saved-posts list is a
personal reference list, not a stream to catch up on).
Unsaving a post (from ANY tab's Post action menu, via the shared
`_toggleBookmark`) removes the row from Saved's list immediately if
currently viewing Saved (`_onBookmarkChanged` hook, default no-op for
Home/Notifications) AND deletes the local `feed_items` row
(`db.delete_feed_item`) so it doesn't come back on next sync — the
original bug was `feed_items` rows never getting cleaned up locally even
though the server-side unbookmark worked correctly.

### Chat (DM) — DONE (v1), several rounds of real bugs found and fixed
**Structure** (confirmed after two revisions — do not redesign again
without explicit re-confirmation): ONE permanent "Chat" tab —
`wx.TreeCtrl` (flat list of conversations, requests always sorted to the
top with a "(request) " label prefix) + `wx.ListCtrl` (messages of
whichever conversation is selected in the tree) side by side, like
YoutubePlus's category-list pattern. F5 syncs the conversation list AND
every conversation's full message history in one pass — expanding/
selecting a conversation must be instant from local cache, never a
per-select network round-trip. Compose box + Send button at the bottom
(Enter or Ctrl+Enter) send to whichever conversation is selected.
"Open in new tab..." on a conversation's context menu pops it out into
its own removable `ConvoTabWindow` tab (title = the other person's
name) — opt-in only, not default. Message requests (status="request")
get dedicated **visible** Accept/Decline buttons (not menu items — the
user wants primary actions like this to always have a visible button,
context-menu/Application-key access alone isn't enough); accepted
conversations get a visible "Message options..." button alongside the
right-click/Menu-key menu (same principle — this is likely to apply to
other tabs' primary action too, not yet decided which).

**Reply** works: Post action-style "Reply" on a message sets a
"Replying to: ..." indicator + Cancel button, includes
`reply_to_message_id` in `send_message`. Response-side `MessageView.reply_to`
embeds the **entire original message** (id + text directly), NOT just a
bare ID like the request side (`MessageInput.reply_to` = `{messageId}`
only) — different shape each direction, both fields (`reply_to_message_id`,
`reply_to_text`) are stored locally so the reply preview always has text
without needing a lookup. Left/Right on a message list jump to/from the
replied-to message; Right also does a **forward search** (find any
message whose `reply_to_message_id` equals the currently focused
message) when there's no pending "jump back" target, so starting from an
original message can navigate forward to whatever replied to it, not
just backtrack a prior Left jump.

**Real SDK bugs found and worked around this session (important for any
future chat.bsky.* work):**
1. `dm.get_messages()`'s typed response parsing throws
   `PydanticUserError: union_tag_not_found` on some real responses (a
   discriminated-union resolution bug on `chat.bsky.convo.defs#messageView`).
   Fixed by calling `dm._client.invoke_query(...)` directly and parsing
   `response.content` as a raw dict instead of the typed Response model
   — same principle `_fetch_thread_json` already used elsewhere in
   `client.py` for a near-identical bug on `getPostThread`, just adapted
   for an authenticated (not public) endpoint.
2. `dm.send_message()`'s RESPONSE parsing hits the same MessageView bug
   (its return type is also MessageView) — fixed the same way, call
   `invoke_procedure` directly and ignore the echoed-back message (the
   caller already re-syncs after a successful send anyway).
3. Separately, the REQUEST body for `send_message` threw
   `PydanticSerializationError: Unable to serialize unknown type: FieldInfo`
   inside `model_dump_json()` — did NOT reproduce against a freshly
   pip-installed copy of the exact same `atproto`==0.0.69 / `pydantic`==2.13.4
   versions, so this looked like an environment-specific issue. **Root
   cause confirmed by the user**: a "dependency ghost" — another add-on
   (Gemini-related) bundles its OWN older `pydantic` under its own
   `lib/`, loads AFTER NVSky at NVDA startup, and was shadowing NVSky's
   bundled pydantic via `sys.path` order, creating a mismatched
   pydantic/pydantic_core pair at runtime despite `pydantic.VERSION`
   itself reading identically. User fixed it on their end (removed the
   other add-on's own pydantic, forcing everything onto NVSky's copy).
   **The workaround is kept anyway** — send the request body as a
   `DotDict` (from `atproto_client.models.dot_dict`) instead of a typed
   `Data` model; `DotDict` serializes through `get_model_as_dict()` +
   `to_json()`, a completely different code path that never touches
   `pydantic_core`'s `model_dump_json()` at all. This protects any OTHER
   user who might hit the same cross-add-on dependency conflict, costs
   nothing, and should not be reverted.
4. **Packaging implication for later**: `requirements.txt` should pin
   `atproto` to an EXACT version (`atproto==0.0.69`, or whatever's
   current when this is set up) — not a range — specifically because of
   #3 above; reproducible builds matter a lot for an accessibility tool
   where an unexpected dependency-version drift could silently break
   things for blind users. No need to enumerate sub-dependencies
   individually — pip resolves those from `atproto`'s own declared
   requirements.

**Still open / explicitly deferred, in the order the user wants them
picked up:**
1. **Chat settings → fold into the main Settings dialog**, NOT a
   button/dialog inside the Chat tab itself. New idea from this
   session's end: add a "Chat" category/tab to the existing NVDA
   Settings panel (`settings.py`) alongside Accounts/General/Display/
   Sound/Profile. Confirmed-available via SDK (**must fetch current
   real values every time the dialog opens, exactly like the existing
   Edit-who-can-reply dialog does — re-verify that pattern in
   `_editReplyPermissions`/`_onReplyPermissionsLoaded` in `feedWindow.py`
   before building this, per the user's explicit reminder that settings
   shown locally must always reflect the server's real current state,
   not a stale local cache):
   - "Allow direct messages from" (Everyone/Following/No one) +
     "Allow group chat invites from" (same 3 choices) — these are
     fields `allowIncoming`/`allowGroupInvites` on the
     `chat.bsky.actor.declaration` **record** (read/write via
     `com.atproto.repo.getRecord`/`putRecord`, NOT a dedicated
     procedure — confirmed via `Record.model_fields` on
     `atproto_client.models.chat.bsky.actor.declaration`).
   - "Mark all chats as read" / "Mark all requests as read" —
     `chat.bsky.convo.updateAllRead(status="accepted"|"request")`
     (confirmed field: `Data.model_fields == ['status']`).
   - Notification settings for new messages / new message requests —
     `chat.bsky.notification.putPreferences(chat=ChatPreference(include=,
     push=), chat_request=ChatPreference(...))` — `ChatPreference` has
     `include: 'all'|'follows'` and `push: bool`.
   - Export chat data — `chat.bsky.actor.exportAccountData` exists;
     response shape (file? stream?) NOT YET confirmed, lowest priority.
2. **Emoji message reactions** — deliberately deferred as a nice-to-have,
   not core. Confirmed feasible if wanted later:
   `chat.bsky.convo.addReaction`/`removeReaction`, Data =
   `{convo_id, message_id, value}` where `value` is a plain string
   (1-64 chars, presumably a single emoji).
3. `ConvoTabWindow` (the pop-out tab) doesn't have the message-options
   button/reply-jump parity that `ChatWindow` got in the same session —
   it DOES have Reply/Copy/Delete via context menu and Ctrl+Enter, just
   not the newer visible "Message options" button or Left/Right jump.
   Low priority since it's an opt-in secondary view.
4. Message-level own read-tracking + focus-first-unread — floated by the
   user as an idea (chat apps' default of always focusing the newest
   message might not be ideal for a busy conversation), explicitly
   deferred, not decided.
5. Clear-cache (Ctrl+Delete equivalent) — Settings > General already has
   a "clear cache" feature but it was found to only clear the Home feed,
   not Notifications/Saved/Chat. Needs the actual `settings.py` source
   read (never inspected yet this whole session) before fixing.

### Explore, Feeds — NOT STARTED
Purpose/scope still undecided by the user (unclear what these would even
show that Home's Following/Discover doesn't already cover). Do not start
without asking first.

### Lists — NOT STARTED, but confirmed feasible, **user said the next chat should build this**
SDK research already done this session, ready to build directly:
- `app.bsky.graph.list` — the list record itself (name, description,
  `purpose`: modlist vs curatelist).
- `app.bsky.graph.listitem` — a separate record per member, references
  the list record.
- Create/delete both via plain `com.atproto.repo.createRecord`/
  `deleteRecord` (same pattern as posts/likes/follows elsewhere in this
  codebase) — no dedicated procedure needed.
- `app.bsky.graph.getLists` — read all of an actor's lists, filterable
  by `purpose`.
- `app.bsky.graph.getList` — read one list's full details + hydrated
  member list.
- Also exists: `muteActorList`/`unmuteActorList` for moderation-purpose
  lists, and (if `purpose=curatelist`) a curation list can be browsed as
  a feed via `app.bsky.feed.getListFeed` (name unconfirmed — verify via
  pip-inspection before use, wasn't double-checked this session).

**Proposed UI** (from user's own description, not yet built or
re-confirmed — restate and confirm shape before coding, per the
standing rule above): list-of-lists you can expand, same
category-tree-then-detail-list pattern as Chat (`wx.TreeCtrl` for the
list-of-lists, `wx.ListCtrl` for a selected list's members), plus
Add/Remove buttons for managing membership. Given Chat's tree+list
pattern is now proven and working well, Lists should very likely reuse
the same structural approach rather than reinvent one — but confirm
with the user before assuming.

### Saved-feeds / pinned custom feeds — NOT STARTED, NOT in the original tab inventory
Came up implicitly during the Discover/For-You research (a "For You"
custom feed some users pin in the official app) — no tab planned for
this yet, but noting it exists as a concept (`savedFeedsPref`/
`savedFeedsPrefV2` in `app.bsky.actor.getPreferences`) in case it comes
up again.

### Profile — folded into Settings already, no separate tab planned.

### General Settings — dialog already exists (`settings.py`, never
directly inspected by Claude this session — do that before touching it).
Confirmed extra things the SDK can pull that the current dialog might
not yet expose: saved/pinned feeds, thread-view sort-order default,
per-labeler content-label preferences, adult-content toggle (all via
`app.bsky.actor.getPreferences`/`putPreferences`), plus the Chat
settings listed above.

## Remaining backlog carried over from earlier phases (still not started)
- View Thread / User Timeline / Followers-Following list — still
  separate `wx.Dialog`s, never actually converted to removable tabs
  despite being planned for the multi-tab push from the very start.
- Ctrl+F5 (check every open tab), Ctrl+Delete (clear current tab's
  cache) — `MainWindow.checkAllOpenTabs()` exists and works; per-tab
  cache-clear does not exist anywhere yet.
- Sound system, background fetch scheduler, i18n pass — not started.
- Video attachment support in Compose — open question, not confirmed
  either way.
