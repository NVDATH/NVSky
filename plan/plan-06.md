# NVSky — plan-06.md

Handoff doc from a chat that ran too long (context got too big to track
file state reliably — several patches failed because Claude guessed at
file contents instead of reading them fresh). Starting a new chat.

## Process reminder for the new chat (agreed with the user)

- **Read actual current source files fresh at the start of the new
  chat** (paste in or upload) before proposing any code. Never guess
  file contents from memory/earlier context, even within the same
  conversation, once it's gotten long — re-read before editing.
- Diff format: `old_str:` / `new_str:` blocks, applied via the user's
  Notepad++ script — this is the fast/preferred path for the user when
  it's precise.
- For anything sizable (heavily-edited method, brand new method), send
  the **whole method** instead of a diff — less risky than a long
  old_str that has to match exactly.
- Whichever of the two costs Claude fewer tokens to produce accurately
  is fine — full-method paste is often actually cheaper/safer than a
  fragile long diff, so default to that when a change touches more
  than a few contiguous lines.
- When multiple edit locations exist, number sub-headings clearly
  (`##### 1.1`, `##### 1.2`, ...) under a file heading — the user
  navigates by these to track what's applied.
- If unsure whether a specific method/file state matches what's being
  patched, **ask for just that method pasted back** (not the whole
  file) rather than guessing — cheaper than a failed patch loop, and
  much cheaper than a whole-file re-upload.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. Structure mirrors YoutubePlus/MessengerAccess (same
developer's other add-ons). Multi-tab `MainWindow` (`wx.Notebook`)
holding Home, Notifications, Saved, Chat, and (new this session) Lists
as permanent tabs, plus dynamically-opened removable tabs (per-list
timelines, per-conversation chat pop-outs).

## Completed this session

- **Lists tab** (`globalPlugins/NVSky/feedWindow.py`): `ListsWindow`
  (tree of the account's lists + curation-list timeline / moderation-
  list member view depending on which list is selected),
  `ListTabWindow` (a single curation list popped into its own
  removable tab, persisted across restarts via `db.get_open_temp_tabs`/
  `add_open_temp_tab`/`remove_open_temp_tab`), `AddListDialog`,
  `ManageMembersDialog` (add/remove members, typeahead user search via
  a `CustomCheckListBox`), `SubscribeListDialog` ("Find lists by
  user" — search a user, browse their public lists, open curation
  lists as tabs directly or mute/block moderation lists). New
  `client.py` functions: `get_lists`, `get_list`, `sync_list_feed`,
  `create_list`, `delete_list`, `add_list_member`, `remove_list_member`,
  `mute_actor_list`/`unmute_actor_list`, `block_actor_list`/
  `unblock_actor_list`, `resolve_list_uri`, `search_actors_typeahead`.
  New `db.py` table `lists` + `upsert_list`/`get_lists`/`delete_list`,
  plus `get_open_temp_tabs`/`add_open_temp_tab`/`remove_open_temp_tab`
  (JSON blob per account in `ui_state`, key `open_temp_tabs:<account_id>`).
  `mainWindow.py`'s `removeCurrentTab` now calls `panel.onTabRemoved()`
  if present before `DeletePage()`.
- **Timezone + time-format bug** (`timeutils.py`, new shared module):
  fixed absolute/custom time display showing UTC instead of local time
  across every tab; centralized `format_timestamp`/
  `current_mode_and_pattern` so Chat and the feed tabs use the same
  logic instead of Chat having its own broken one-off formatter.
- **Grapheme counting bug** (`client.py`: `count_graphemes`): Python's
  `len()` overcounts Thai text substantially (confirmed against
  bsky.app side-by-side: 1704 real graphemes vs `len()`'s 1887 on the
  same message) because Thai combining vowels/tone marks are separate
  codepoints that count as ONE grapheme with their base consonant.
  Fixed in both post compose (`compose.py`) and Chat's char-limit
  display/validation.
- **Alt+1 through Alt+9 shortcut** — reads the Nth-newest item's full
  row (all columns, via `GetItemText`) aloud without moving focus.
  Works from anywhere including while typing in Chat's compose box.
  Wired into every `FeedListMixin` host (`FeedWindow`,
  `NotificationsWindow`, `SavedWindow`, `ListsWindow`'s curation-list
  view, `ListTabWindow`) via a shared `_announceNthNewestPost`, and
  separately into `ChatWindow`/`ConvoTabWindow` via
  `_announceNthNewestMessage`. Respects the `sort_order` setting
  (newest/oldest) automatically for the feed tabs (they already sort
  `self._posts` before this reads it); Chat needed its own explicit
  fix, see below.
- **Periodic time-column refresh** — a `wx.Timer` (30s interval, no
  network/DB call, just re-formats the already-in-memory timestamp)
  keeps relative time labels ("5 minutes ago") from going stale while
  a tab is left open. Wired into `FeedListMixin._initFeedListState`
  (covers every feed tab automatically) and separately into
  `ChatWindow`/`ConvoTabWindow`. **Bug fixed along the way**: the timer
  must bind to `self` (the panel), not `self.postList` — `FeedWindow`
  calls `_initFeedListState()` before `postList` exists yet, so binding
  to the widget crashed with `AttributeError` on open.
- **Chat optimistic send** — `onSend` in both `ChatWindow` and
  `ConvoTabWindow` now inserts the message into the visible list AND
  `self._currentMessages` (the cache Alt+number reads) synchronously,
  *then* speaks "Message sent" after a short `wx.CallLater(250, ...)`
  delay (was speaking instantly, before the row even rendered, and
  before this fix Alt+number right after sending read stale data
  because only the widget was updated, not the cache). The real send
  still happens on a background thread; `send_message`'s own echoed-
  back response can't be trusted (SDK bug, see the note already in
  `client.py` on `send_message` itself), so a background resync
  (`sync_convo_messages`) silently swaps the placeholder row for the
  real one once it lands. Confirmed working by the user.
- **Chat message sort order (`sort_order` setting)** — `ChatWindow.
  _showMessages` and `ConvoTabWindow._loadMessages` now reverse the
  (always oldest-first-from-DB) message list when the setting isn't
  `"oldest_first"`, store the result in `self._currentMessages` /
  `self._messagesNewestFirst`, and every other method that used to
  re-query the DB and index directly (`onMessageContextMenu`,
  `_jumpToRepliedMessage`, `_jumpBackToReply` in both classes) was
  switched to read `self._currentMessages` instead, so row index always
  matches what's actually displayed regardless of sort direction.
  **Given to the user in the last message but not yet confirmed
  applied/tested** — check this first in the new chat.
- **Empty-state UI hiding + focus-restore-on-close + remove
  confirmations** for the Lists tab's dialogs (`ManageMembersDialog`,
  `SubscribeListDialog`) and `ListsWindow.onRemoveList` — confirmed
  working by the user.

## Outstanding / not yet done

1. **Chat message reactions (emoji reacts) — not displayed at all.**
   Confirmed via web search that the AT Protocol chat lexicon really
   does support this: `chat.bsky.convo.addReaction`/`removeReaction`
   procedures exist, and `chat.bsky.convo.defs` has `#reactionView`/
   `#reactionViewSender`/`#messageAndReactionView` — so a message view
   should carry a `reactions` field. Exact field names NOT yet
   confirmed against the installed SDK (pip-inspect before trusting).
   Already drafted (given to the user, not yet applied):
   - `_describe_reactions(reactions_json)` formatter function for
     `chatWindow.py` (dedupes by emoji, shows `emoji×N` for repeats).
   - A 4th "Reactions" column added to both `ChatWindow` and
     `ConvoTabWindow`'s `messageList` (`InsertColumn` — note this line
     is IDENTICAL text in both classes, a diff must apply to both
     occurrences).
   - Both `_showMessages` and `_loadMessages` (already rewritten this
     session for sort-order, see above) call
     `self.messageList.SetItem(i, 3, _describe_reactions(message.get("reactions_json")))`.
   **Still needed, blocked on seeing real source:**
   - `db.py`: add a `reactions_json TEXT` column to the `messages`
     table's `CREATE TABLE` (remember: no `ALTER TABLE` migrations
     during solo-dev beta — add directly to the `CREATE TABLE`
     statement per the project's standing rule).
   - `client.py`: find the actual message-sync function (likely named
     `sync_convo_messages`, stores each message via something like
     `db.upsert_message`) and extend it to pull reaction data off each
     message view into `reactions_json` before storing. **Never seen
     this function's real content in this chat — read it fresh before
     touching it.**
   - Decide/build the add/remove-reaction UI itself (a menu item on
     the message context menu, presumably) — not designed yet, only
     the read/display side has been drafted.

2. **MainWindow speaks the tab name immediately when a tab is
   *created*, not just when the user actually switches to it.**
   Diagnosis (not yet confirmed against real code, needs a fresh read
   of `mainWindow.py`): `addTab()` likely calls `onTabActivated()` (or
   equivalent) unconditionally right after adding the page, instead of
   only in response to `EVT_NOTEBOOK_PAGE_CHANGED` firing from an
   actual user-driven selection change. Needs a proper fix distinguishing
   "tab was just constructed" from "user switched to this tab" —
   **read `mainWindow.py`'s current `addTab`/`_focusPanel` and whatever
   binds `EVT_NOTEBOOK_PAGE_CHANGED` fresh before patching.**

3. **Chat: message read-status handling.** Reported as "still shows
   unread even after scrolling through and reading it" plus "Space to
   jump to next unread doesn't work." **Needs clarification from the
   user first** (was asked, not yet answered): does this mean —
   - (a) conversation-level unread (the indicator in the conversation
     tree not clearing after opening/reading a convo), or
   - (b) message-level read-tracking + Space-jump-to-next-unread, the
     same feature `FeedListMixin` already has for Home/Notifications/
     Saved (`db.mark_post_read`, Space-jump navigation) — which Chat
     may not have an equivalent of at all yet?
   These are very different sizes of work — resolve which one before
   estimating/building.

## Immediate next steps for the new chat

1. Paste in current `chatWindow.py` and confirm the sort-order rewrite
   (item above, "given but not yet confirmed applied") actually landed
   and works, including Alt+number now reading the correct side.
2. Get the user's answer on the unread-status question (#3 above).
3. Paste in current `mainWindow.py` to fix the tab-announce-on-create
   bug (#2 above).
4. Paste in current `client.py`'s message-sync function + `db.py`'s
   `messages` table schema to finish reactions (#1 above).
