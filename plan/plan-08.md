# NVSky — plan-08.md

Handoff doc for a new chat. Same process rules as plan-06.md/plan-07.md,
still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code — but per the standing rule adopted
  a few sessions back: once a round of patches is confirmed applied and
  tested, treat that as reflected in the user's real source and keep
  working from that state — don't ask for a full re-upload every round.
  Only ask for a fresh full-file paste, or just the specific
  method/section, if a patch is reported as failing or landing in the
  wrong place.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) — applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines before/after
  the change, must match exact whitespace, stays as one block for one
  contiguous change (don't split a single change into multiple tiny
  blocks). Always state the target filename clearly right before each
  block.
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated — when in doubt about which of two near-identical blocks
  matched, or a patch is reported as landing wrong, ask for that one
  method pasted back rather than guessing again. Same-name methods in
  the same file need extra disambiguating context in old_str (a
  preceding unique comment/line), not just the def line — this has come
  up before (e.g. `_onCheckForUpdatesDone` existing in both `ChatWindow`
  and `ConvoTabWindow` with identical signatures).
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them — has caused real
  SyntaxError/NameError crashes before, including NVDA force-restarts.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle: the optimistic-UI-then-background-
  network pattern (update UI/local state immediately, do the real
  network call on a background thread, reconcile silently after) should
  be applied to every activity across the whole add-on.
- When using an SDK method never exercised against a real server before
  in this project, flag it plainly as LOW CONFIDENCE and ask the user to
  paste back a traceback or raw response if it doesn't work, rather than
  presenting a guess as certain. Same standard applies to any wx/NVDA
  API mechanism being used for the first time in this project (e.g. the
  `wx.LC_VIRTUAL` work below) — flag confidence level honestly, verify
  with real testing rather than trusting theory alone (this project has
  been bitten by screen-reader-specific surprises that looked solid on
  paper before — see the Alt+number bug in "Completed" below).
- If a temporary debug patch (timing log, diagnostic log.info, etc.) is
  applied on the user's side, track that it's still present — a later
  whole-method patch to that same method needs to account for it or the
  old_str won't match. Ask "is the temporary log still in there?" before
  patching a method known to have one.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`) holds Home, Notifications,
Saved, Chat, and Lists as permanent tabs, plus dynamically-opened
removable tabs (per-list timelines, per-conversation chat pop-outs).

## Completed since plan-07.md

**Structural bug hardening pass (plan-07.md's whole focus) — fully
closed out and confirmed working:**

- **Crash** (`RuntimeError: wrapped C/C++ object of type TreeCtrl has
  been deleted`, force-restarted NVDA): root cause was `ListsWindow`
  unconditionally auto-syncing from the server on every `__init__`
  (unintentional — never meant to behave that way), racing against
  `MainWindow.onClose`'s immediate `self.Destroy()`. Fixed with a new
  shared `uiutil.py` module (`uiutil.safe_ui_callback` decorator —
  catches `RuntimeError` containing "has been deleted" from a destroyed
  wx object, turns it into a silent no-op, logs via a still-present
  temporary `log.info` for now) applied to all 43 `wx.CallAfter`-target
  completion-handler methods across `chatWindow.py`, `compose.py`,
  `feedWindow.py`, `mainWindow.py`, `settings.py`.
- **~700-800ms freeze on every MainWindow open** (confirmed via NVDA's
  own "Recovered from freeze" watchdog log): root cause was `db.py`'s
  `_connect()` opening/closing a brand-new SQLCipher connection on
  *every single query* (214 connects in one MainWindow open, ~0.82s
  total, confirmed via timing logs), compounded by an N+1 pattern where
  `_format_post_time()`/`_format_time()` independently re-queried the
  `time_format_mode` UI setting once per rendered row. Fixed with (a)
  `db.py` — one persistent connection reused per thread via
  `threading.local()`, `_connect()`'s call shape unchanged so nothing
  else in `db.py` needed touching; (b) `timeutils.py` — module-level
  in-memory cache for `(mode, pattern)`, invalidated only when
  `settings.py`'s DisplayPanel writes a new value. Freeze fully gone,
  confirmed by the user; Stage 4 (lazy tab construction) from the
  original plan is no longer needed and was dropped.

**New bug found and fixed after the above closed out:** Alt+number
(read Nth-newest post/message) started double-reading the row — root
cause was that moving the ListCtrl's real focused-item position
(`Focus()`/`Select()`) can trigger NVDA's own automatic announcement of
that row IF the control already had real OS focus, racing against the
add-on's own explicit `nvdaUi.message()` call. A first attempt using
delay + `speech.cancelSpeech()` (mirroring an existing `_announce_now`
pattern already used ~20 places in `feedWindow.py`) was rejected by the
user as fragile ms-tuning that still let a stray syllable through. The
user's own alternative was adopted instead and is confirmed fully
working: check `list.HasFocus()` and compare `GetFocusedItem()` before
vs after moving focus — only skip the add-on's own announcement when
the list already had real focus AND the index is actually changing (a
same-index repeat, e.g. pressing Alt+N again on the same row, is a
no-op state change that fires no accessible event, so NVDA would
otherwise stay silent). Refactored per a Gemini suggestion into a
single shared `uiutil.move_focus_and_check_announce(list_ctrl, index)`
helper (returns bool, lets callers lazily skip building the announced
string when not needed) used by both `ChatWindow._announceNthNewestMessage`
and the shared `FeedListMixin._announceNthNewestPost` (covers
Home/Saved/Lists/ListTab/Notifications in one place).

**Re-confirmed:** the Ctrl+Tab/Ctrl+number double-announcement fix
(`EVT_NOTEBOOK_PAGE_CHANGING` in `mainWindow.py`, from plan-07.md) is
holding up — user explicitly re-tested and confirmed. No longer an open
item.

## Current task — message/post text truncated at ~511 characters

**This is what the new chat should pick up first.**

- User noticed the Message column in Chat doesn't show the full text of
  long messages (Bluesky DMs allow up to 1000 chars). Confirmed via
  testing: copying the message via the existing `_copyMessageText()`
  action (which reads straight from the message dict, bypassing the
  ListCtrl entirely) comes out **complete** — the full text is intact
  both in the DB and in memory. The **ListCtrl column display itself**
  cuts off at **exactly 511 characters** every time, regardless of
  content.
- Ruled out: not a data-layer slice anywhere in the pipeline (checked
  `db.py`'s `messages` table schema and `upsert_message` — plain `TEXT`
  column, no truncation; checked `chatWindow.py`/`feedWindow.py`'s
  formatting code — no `[:n]` slicing on the main message/post text
  anywhere). Not a newline-handling issue either — added a shared
  `uiutil.single_line()` helper that replaces `\n`/`\r\n` with a plain
  space before `SetItem()`, applied to both `chatWindow.py`'s message
  text and `feedWindow.py`'s `_message_text()` (which had the identical
  latent bug for posts, just never surfaced since Bluesky posts cap at
  300 chars, under the 511 cutoff) — made **zero difference** to the
  511 cutoff, confirmed by the user. The number matches the classic
  signature of a fixed-size buffer (`WCHAR buffer[512]` → 511 usable
  characters + null terminator) somewhere in the Win32
  ListView/accessibility stack — exact layer unconfirmed, but the
  practical implication is clear: **the control cannot be relied on to
  store/return more than ~511 characters in a single cell.**
- User explicitly rejected a "truncate + press Enter for full text"
  workaround (compared unfavorably to apps like Unigram that display
  long pasted text completely) — wants a real fix that displays the
  full text, understands it's more invasive, and wants to pilot it on
  the **Chat tab only** first before deciding whether to extend to
  `feedWindow.py`'s `postList`.

### Agreed plan: convert `messageList` to `wx.LC_VIRTUAL`

In virtual-list mode, the control never stores per-cell text internally
— it calls back into Python's `OnGetItemText(item, column)` on demand
whenever it needs to know a cell's text (painting, accessibility
queries, etc.), so the apparent fixed-buffer limit should never come
into play at all.

**CRITICAL implementation gotcha (from a Gemini review, not yet
independently verified firsthand):** `OnGetItemText` **must be a method
on an actual `wx.ListCtrl` subclass** — if it's defined on the mixin
(`_ChatMessagePanelMixin`) or on `ChatWindow`/`ConvoTabWindow` directly
instead, wx will silently never call it (blank/empty-looking list, no
error raised). Correct approach: a small subclass —

```python
class VirtualMessageList(wx.ListCtrl):
    def OnGetItemText(self, item, column):
        return self.GetParent().get_virtual_item_text(item, column)
```

— construct `messageList` as an instance of this subclass instead of a
bare `wx.ListCtrl(self, style=wx.LC_REPORT)`, with the actual
per-column text logic living in a `get_virtual_item_text` method on the
parent panel (`ChatWindow`/`ConvoTabWindow`, via the mixin) so both
still share one implementation.

**On NVDA/screen-reader compatibility:** a Gemini review asserted that
NVDA and JAWS fully support Win32 virtual ListViews (`LVS_OWNERDATA`)
already — Explorer and Task Manager use the same underlying pattern,
and screen readers read via `LVN_GETDISPINFO` → `OnGetItemText`
dynamically, so no accessibility regression is expected. **Treat this
as reassuring but not a substitute for real testing** — this project
has hit genuine screen-reader-specific surprises before that looked
fine in theory (see the Alt+number bug above). Test thoroughly with
real NVDA once implemented, not just "does the list render."

### Concrete call sites in chatWindow.py needing conversion

(All confirmed via grep against the current source — scoped to
`_ChatMessagePanelMixin`/`ChatWindow`/`ConvoTabWindow`'s `messageList`
only; `feedWindow.py`'s `postList` deliberately not touched yet.)

1. `ChatWindow._showMessages` and `ConvoTabWindow._loadMessages` —
   currently `DeleteAllItems()` + a loop of `InsertItem`/`SetItem` per
   row. Needs to become: store `self._currentMessages = messages`, then
   `self.messageList.SetItemCount(len(messages))` +
   `self.messageList.RefreshItems(...)`. The per-column logic currently
   inline in these loops (Reactions/From/Message/Sent columns) should
   move into the new `get_virtual_item_text(item, column)` method as
   the single source of truth.
2. `_onTimeRefreshTick` — currently `SetItem(i, 3, ...)` per row on a
   60s timer. Becomes `self.messageList.RefreshItem(i)` (re-invokes
   `OnGetItemText` for that row on demand instead of pushing text in
   directly).
3. The optimistic "Sending..." temp-row insert in `onSend` (the
   `tempIndex = ...; InsertItem/SetItem` block) — needs to insert a
   synthetic dict into `self._currentMessages` first, then
   `SetItemCount`/`RefreshItems`, instead of inserting directly into
   the control.
4. `_announceNthNewestMessage`'s `GetItemText` call — should need **no
   code change**; wx's `GetItemText` on a virtual list already proxies
   to `OnGetItemText` per docs, and should incidentally start returning
   the full (untruncated) text once the conversion lands.
5. **Not yet fully verified** — whether the reaction-update or
   mark-as-read paths ever patch a single `messageList` cell directly
   (vs. always re-rendering the whole list via `_showMessages`/
   `_loadMessages`). Grep so far found no single-cell `SetItem` calls
   for reactions specifically, but this needs a final confirmation pass
   (check `_onReactionDone`, `_onDeleteMessageDone`, and the
   mark-read/unread paths in chatWindow.py) before writing the full
   patch — this was the one open item mid-check when this chat's
   context ran out. **Recommended first step in the new chat.**
6. `Focus()`/`Select()`/`EnsureVisible()`/`HasFocus()`/
   `GetFocusedItem()` (used throughout, including the just-fixed
   Alt+number code in `uiutil.move_focus_and_check_announce`) — should
   keep working unchanged in virtual mode since they're index-based
   with no dependency on per-cell storage; expected to need no code
   change, just confirm in testing.

## Other outstanding items (lower priority, carried from plan-07.md)

1. `onConvoSelected`'s TreeCtrl crash — a defensive try/except guard is
   in place and stops the crash, but the actual root cause (why the
   tree gets destroyed) was never confirmed. Distinct mechanism from
   the Stage-0 `wx.CallAfter` fix above (this is a direct event-handler
   race, not a background-thread completion callback). Watch for
   "selected a conversation, nothing happened" (silent, no crash) as a
   sign it's still live.
2. `feedWindow.py`'s `onCharHook` still duplicated 5x (Home/Saved/
   Lists/ListTab/Notifications) — worthwhile refactor, not urgent, same
   class of risk that caused chatWindow.py's duplication bugs before.
3. Whether to remove the temporary `log.info` logging in
   `uiutil.safe_ui_callback` (added during the original crash
   investigation) — extensive testing across many sessions since then
   has caught nothing else. User hasn't been asked to decide yet.
4. `chat.bsky.convo.getLog` for incremental sync (pure performance,
   nice-to-have, not user-visible); a dedicated Chat options settings
   page (no concrete need identified yet); group chat
   (`chat.bsky.group.*`) is a wholly separate, unstarted namespace —
   skip unless actually wanted. (Carried forward unchanged from
   plan-07.md, never revisited this session.)
