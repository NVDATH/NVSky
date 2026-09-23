# NVSky — plan-07.md

Handoff doc for a new chat. Same process rules as plan-06.md, still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code — but per the standing rule
  adopted mid-way through the last session: once a round of patches is
  confirmed applied and tested, treat that as reflected in the user's
  real source and keep working from that state — don't ask for a full
  re-upload every round. Only ask for a fresh full-file paste, or just
  the specific method/section, if a patch is reported as failing or
  landing in the wrong place.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) — applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines before/after
  the change, must match exact whitespace, stays as one block for one
  contiguous change (don't split a single change into multiple tiny
  blocks).
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff — a full-file rewrite was already done once
  for chatWindow.py's refactor and worked well.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated (this bit us twice already — a misplaced `_reactToMessage`
  and having to hand-fix method placement once). When in doubt about
  which of two near-identical blocks matched, or a patch is reported as
  landing wrong, ask for that one method pasted back rather than
  guessing again.
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them — this has caused
  multiple real SyntaxError/NameError crashes (including two NVDA
  force-restarts) this session. Check line-join points carefully before
  sending.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle (explicitly generalized by the user):
  the optimistic-UI-then-background-network pattern (update UI/local
  state immediately, do the real network call on a background thread,
  reconcile silently after) should be applied to every activity across
  the whole add-on, not just chat send.
- When using an SDK method never exercised against a real server before
  in this project, flag it plainly as LOW CONFIDENCE and ask the user
  to paste back a traceback or raw response if it doesn't work, rather
  than presenting a guess as certain.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`) holds Home, Notifications,
Saved, Chat, and Lists as permanent tabs, plus dynamically-opened
removable tabs (per-list timelines, per-conversation chat pop-outs).

## Completed since plan-06.md (this past session — very large)

**Tab-identity / persistence system** (`mainWindow.py`, `db.py`,
`__init__.py`): generalized "last active tab" and "remember this tab on
restart" to work for temp tabs too (`TAB_TEMP_TYPE`/`TAB_TEMP_KEY` on
`ConvoTabWindow`/`ListTabWindow`, matching `db.get_open_temp_tabs()`'s
`type`/`key` fields), and made tab rename actually persist across
sessions (`db.set_temp_tab_custom_name`, `onTabRenamed` hook called from
`MainWindow.renameCurrentTab`) — it had only ever been an in-memory
`TAB_NAME` assignment before, no db write at all.

**Chat message-level read tracking**: first attempt (a local-only
`is_read` column guessed at insert time) was wrong — drifted from the
server's real state, defaulted every pre-existing historical message to
unread. Corrected design: `db.reconcile_message_read_state(account_id,
convo_id, unread_count)` re-derives the local `is_read` boundary from
the server's authoritative per-conversation `unread_count` on every
sync (newest N messages = unread), and `client.mark_message_read`
pushes individual reads back to the server in the background via
`chat.bsky.convo.updateRead`'s `messageId` param (LOW CONFIDENCE, never
confirmed against a real server response). Space jumps to next unread,
Alt+number also marks read (extended to ALL `FeedListMixin` tabs too,
not just chat — Home/Notifications/Saved/Lists/ListTab).

**Chat message reactions**: display (`reactions_json` column + `Reactions`
column, moved to be the FIRST column — same fix applied to the `Embed`
column across every `FeedListMixin` host), react/unreact via a message
context menu item (validates exactly-1-grapheme before calling the API,
after hitting a real `InvalidRequest` from the server), and a real emoji
picker (`_pick_emoji`/`_COMMON_EMOJI`, a `wx.SingleChoiceDialog` list of
common emoji + "Custom..." fallback — NOT a bare text box, which was
flagged as useless). `client.add_reaction`/`remove_reaction` are LOW
CONFIDENCE (first-ever use).

**New chat flow**: `NewChatDialog` (typeahead user search, same pattern
as `SubscribeListDialog`) + a required first-message field — discovered
the hard way that `getConvoForMembers` alone doesn't create anything
visible until an actual message is sent to the resolved convo id.
`client.get_or_create_convo_for_member` had to be rewritten to bypass
the typed SDK response (same class of pydantic union-discriminator bug
hit elsewhere in this project) after it silently returned nothing with
no exception at all.

**A real crash** (NVDA force-restarted twice): `RuntimeError: wrapped
C/C++ object of type TreeCtrl has been deleted` in
`ChatWindow.onConvoSelected`. Root cause never fully pinned down —
`MainWindow._openChatConvo`'s double tree-reload race was fixed (now
suppresses `onTabActivated` during its own programmatic tab switch,
using the existing `_activationSuppressed` flag) but crashes continued
after that alone, so a defensive 3-layer guard was added directly in
`onConvoSelected` too (attribute check + `try/except
RuntimeError/AttributeError` + `finally: evt.Skip()`) on a Gemini
suggestion. This turns a crash into a silent no-op — **the actual root
cause is still not confirmed**, only papered over safely. Watch for
"selected a conversation, nothing happened" (no crash, just silently
swallowed) as a sign the underlying issue is still live.

**chatWindow.py full refactor** (delivered as a whole-file rewrite, not
a diff): `_ChatMessagePanelMixin` now owns everything both `ChatWindow`
and `ConvoTabWindow` need for "the currently displayed conversation's
message list" — reactions, Alt+number, Space-jump, reply/copy/delete,
the emoji picker, sending (optimistic UI), all keyboard shortcuts.
Fixed `_deleteMessageForSelf`/`onMessageContextMenu` to a single shared
signature (root-caused the earlier `_reactToMessage` argument-count
crash). Side effect: `ConvoTabWindow` gained Left/Right reply-jump (it
never had it before) and its Alt+number now reads all 4 columns like
`ChatWindow` (previously just "From: text").

**F5 / Shift+F5 / Ctrl+F5 scheme, finalized** (chat-specific, after a
couple of wrong turns):
- F5 = refresh whatever's the narrowest current context (selected
  conversation in Chat; a feed tab's own page in feedWindow.py hosts)
- Shift+F5 = full refresh of the current tab's whole scope (all
  conversations in Chat via `onCheckAllConvos`; "fetch older posts" in
  feed tabs — inapplicable/no-op consideration was WRONG for chat,
  corrected to mean "all chats")
- Ctrl+F5 = every open tab, app-wide, via `MainWindow.checkAllOpenTabs`

`checkAllOpenTabs` was rewritten from "loop calling each panel's full
`onCheckForUpdates` concurrently" (caused real `re-login failed` errors
— concurrent calls into `client.get_client_for_active_account()` racing
on session/token refresh — and spoke each tab's own "no new X" message
back-to-back, plus jerked focus around background tabs) to: one shared
background thread calling a new `_syncForBulkCheck(atprotoClient) ->
bool` hook on each panel sequentially (pure network+DB, no wx calls),
one final spoken summary, and only the currently-VISIBLE tab actually
re-renders (`_reloadAfterBulkCheck()`) — every other tab's local cache
is fresh and picks it up naturally next time it's activated. This hook
pair now needs to exist on every checkable panel: `FeedListMixin`
(covers Home/Saved/ListTab/Notifications), `ListsWindow` (its own
override, curate vs mod list), `ChatWindow`, `ConvoTabWindow`.

Also fixed while touching this: every `feedWindow.py` host's
`onCharHook` intercepted Ctrl+F5 too (no `ControlDown()` exclusion),
silently eating it before it could bubble up to `checkAllOpenTabs` —
patched in Home/Saved/Lists/ListTab/Notifications (5 near-identical
`onCharHook` methods, sent as full-method replacements since the
duplication made precise diffs risky).

**Alt+number now moves real ListCtrl position** (`Focus`/`Select`/
`EnsureVisible`) so arrowing afterward continues from that item — but
deliberately does NOT call `SetFocus()`, so a chat compose box (or
wherever the user's real keyboard focus was) never gets stolen. Two
wrong iterations before landing here: first attempt moved real focus
too (broke "keep typing while checking a message" — the whole reason
this shortcut exists); the fix-of-the-fix over-corrected and reverted
Chat's version entirely.

**NVDA reading a ListCtrl item twice on Ctrl+Tab/Ctrl+number tab
switch** (confirmed universal — every ListCtrl-based tab, not just
chat; TreeCtrl-based tabs never doubled; switching via the tab strip
itself never doubled either). First attempted fix (reordering
`SetFocus()` vs `Focus()/Select()` inside `ConvoTabWindow._loadMessages`)
had zero effect, meaning the real cause is outside chatWindow.py
entirely. Current fix: `MainWindow` binds `EVT_NOTEBOOK_PAGE_CHANGING`
(fires before the swap, unlike `PAGE_CHANGED`) to move focus onto the
notebook itself first, theorized to prevent Windows' native
"focused-control-about-to-be-hidden" auto-reassignment from firing
alongside `onTabActivated()`'s own explicit focus call. **Not yet
re-confirmed fixed after the mainWindow.py patch landed** — last
status was "cleared" per the user but worth a specific re-check.

**Lists tab UI reorganized** to match Chat's pattern: `Remove
list`/`Show in new tab`/`Manage members...` moved into the list tree's
context menu (`onListContextMenu`, new `EVT_TREE_ITEM_MENU` binding);
`Add list...` moved to the shared toolbar's New-post button (becomes
"New list... (Ctrl+N)" while a Lists tab is active, dispatched via
`MainWindow.onPageChanged`/`onNewPost`'s existing tab-identity check,
same mechanism as Chat's "New chat..." swap) — the external
`addListButton`/`removeListButton`/`showInNewTabButton`/
`manageMembersButton` widgets are kept constructed but `.Hide()`'d
rather than removed, so their existing `.Bind()` calls don't need
touching. `Find lists by user...` moved into `MainWindow`'s own main
toolbar (`findListsButton`, shown only while Lists is active) rather
than staying in `ListsWindow`'s own row.

## Outstanding

1. **New structural bug — performance + security, not yet detailed.**
   The user has this queued for the NEW chat specifically (didn't want
   to describe it twice across two chats). **First thing to do in the
   new chat: ask for the actual details/repro before proposing
   anything.** Framed as "big, structural" by the user — treat as
   higher priority than any of the polish items below once described.
2. Re-confirm the Ctrl+Tab/Ctrl+number double-announcement fix
   (`EVT_NOTEBOOK_PAGE_CHANGING` in mainWindow.py) is actually holding
   up — last report was "cleared" but wasn't re-verified explicitly
   after the final round of fixes landed alongside other changes.
3. `feedWindow.py`'s `onCharHook` is still duplicated 5x (Home/Saved/
   Lists/ListTab/Notifications) — same class of risk that caused the
   chatWindow.py duplication bugs. Not urgent, but flagged as a
   worthwhile follow-up refactor once the structural bug (#1) is
   handled, mirroring the mixin extraction already done for
   `_insertRow`/`_buildFeedListColumns` and for chatWindow.py.
4. `onConvoSelected`'s TreeCtrl crash — the defensive guard stops NVDA
   from crashing, but the actual root cause of the tree being destroyed
   was never confirmed. Keep an eye out for "selected a conversation,
   nothing happened silently" as a sign it's still there underneath.
5. Nice-to-have, no urgency: `chat.bsky.convo.getLog` for incremental
   sync (pure performance, not user-visible); a dedicated Chat options
   settings page (no concrete need identified yet — push notifications
   were investigated and confirmed infeasible for a desktop add-on,
   `registerPush` only forwards to a self-hosted relay service); group
   chat (`chat.bsky.group.*`) is a wholly separate, unstarted namespace,
   skip unless actually wanted.
