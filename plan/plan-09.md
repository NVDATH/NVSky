# NVSky — plan-09.md

Handoff doc for a new chat. Same process rules as plan-06/07/08.md,
still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code -- once a round of patches is
  confirmed applied and tested, treat that as reflected in the user's
  real source and keep working from that state -- don't ask for a
  full re-upload every round. Only ask for a fresh full-file paste, or
  just the specific method/section, if a patch is reported as failing
  or landing in the wrong place.
- **Always double-check the target FILENAME against the actual `##
  File:` marker in the uploaded source before writing a patch header.**
  This session had repeated mistakes labeling `MainWindow` patches as
  `feedWindow.py` when `MainWindow` actually lives in `mainWindow.py`
  (file boundary confirmed via the `## File:` markers in the uploaded
  .md files) -- caused real confusion for the user, who patches by
  hand per-file. Don't guess the file from memory of where a class
  "should" be; check the marker.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) -- applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines
  before/after the change, must match exact whitespace, stays as one
  block for one contiguous change. Always state the target filename
  clearly right before each block.
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated -- when in doubt about which of two near-identical blocks
  matched, ask for that one method pasted back rather than guessing.
  Same-name/near-identical methods across classes need extra
  disambiguating context in old_str (a preceding unique comment/line
  or a wider span reaching back to the class's own docstring/first
  unique line), not just the def line -- came up again this session
  (feedWindow.py's onCharHook refactor needed this for 3+ classes with
  literally identical neighboring methods).
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle: optimistic-UI-then-background-
  network pattern (update UI/local state immediately, background
  thread does the real network call, reconcile silently after) applies
  to every activity across the whole add-on.
- When using an SDK method, wx/NVDA API mechanism, or general technique
  never exercised in this project before, flag it plainly as LOW
  CONFIDENCE and ask the user to test/paste back a traceback rather
  than presenting a guess as certain -- this project has repeatedly
  been bitten by things that looked solid in theory (LC_VIRTUAL not
  fixing the 511-char cutoff; three separate wrong guesses in a row
  for the "quick close" crash this session before finding the real
  cause). **When a hypothesis-based fix comes back from testing as
  having NO effect, say so plainly and move to the next hypothesis --
  don't defend the original theory.**
- The user now sometimes brings a second opinion from Gemini
  (analysis/ideas only, never code applied directly -- the user
  applies only Claude's diffs). Treat this as a genuinely useful
  second-opinion channel, not noise: engage with it seriously,
  fact-check specific technical claims against the actual source
  rather than accepting or dismissing wholesale, and adopt the parts
  that hold up. This session, Gemini's general diagnosis category
  (background thread racing a destroyed window) was directionally
  right and worth taking seriously, but several of its specific
  causal claims (e.g. "the worker thread in this trace is what
  crashed it") didn't hold up against a closer read of the actual
  code and were worth pushing back on with specifics rather than
  accepting at face value.
- If a temporary debug patch (timing log, diagnostic log.info, etc.)
  is applied on the user's side, track that it's still present -- a
  later whole-method patch to that same method needs to account for
  it or the old_str won't match.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`, defined in mainWindow.py)
holds Home, Notifications, Saved, Chat, and Lists as permanent tabs,
plus dynamically-opened removable tabs (per-list timelines, per-
conversation chat pop-outs).

## Completed since plan-08.md

**Chat message 511-character truncation (plan-08.md's whole focus) --
resolved, but NOT via the originally-planned route:**

- `wx.LC_VIRTUAL`/`OnGetItemText` conversion was tried and **confirmed
  by testing to NOT fix the cutoff** -- the ~511-char limit persisted
  identically in virtual mode, including via `GetItemText()` and even
  via a Column Review add-on cell-inspection dialog independent of
  virtual/non-virtual mode. This was fully reverted (user restored
  from their own backup) -- messageList is back to a plain
  `wx.ListCtrl(style=wx.LC_REPORT)`, not virtual.
- Real fix landed in 2 parts:
  1. **Alt+number / "Show message..." full-text reads**: fixed by
     reading straight from `self._currentMessages[index]` in Python
     instead of any ListCtrl text-retrieval API (`GetItemText` etc,
     which is itself capped at ~511 regardless of virtual mode -- root
     mechanism never fully pinned down, Win32-level buffer suspected
     but not proven). New `_show_message_dialog` helper (chatWindow.py)
     -- a read-only `wx.TextCtrl(style=TE_MULTILINE|TE_READONLY)`
     dialog, same idea as Column Review -- wired to a new "Show
     message..." context-menu item (not bound to a key yet, user
     hasn't decided which key -- Enter is reserved for a planned
     quick-action feature, maybe Space, undecided).
  2. **Arrow-key/normal navigation reads** (harder problem -- NVDA's
     own automatic focus announcement reads the ListCtrl's native
     text, not app code, so Python-level fixes can't touch it):
     solved via the user's own idea, confirmed elegant and working --
     split any message over `MESSAGE_COLUMN_SPLIT_THRESHOLD` (450)
     chars across TWO columns ("Message" / "Message (more)"), since
     NVDA already reads all visible columns of a focused row back-to-
     back automatically. New `client.split_at_grapheme_boundary(text,
     max_chars)`: grapheme-cluster-safe (won't sever a Thai combining
     mark or a ZWJ emoji sequence), targets the MIDPOINT of the text
     (not a fixed max) so neither column risks exceeding the native
     cap even near Bluesky's ~1000-char message limit, and prefers to
     land the split at a whitespace/punctuation boundary within a
     100-char lookback window (covers CJK full-width punctuation too)
     to avoid mid-word breaks -- confirmed by testing to still li mid-
     word for long unbroken runs of Thai with no punctuation nearby at
     all (inherent limit, no dictionary-based segmenter in scope);
     "Show message..." remains the guaranteed-complete fallback for
     that case. `_ChatMessagePanelMixin` gained shared
     `_messageFromLabel`/`_messageDisplayText` hooks (`ChatWindow` +
     `ConvoTabWindow` both implement them) so column-population,
     Alt+number, and "Show message..." all pull from one source of
     truth instead of duplicating the reply-preview-prefix logic.
- Feature deliberately scoped to Chat's `messageList` only, same as
  plan-08.md's original pilot decision -- `feedWindow.py`'s `postList`
  (posts cap at 300 chars, under the cutoff) still untouched.

**Bonus bug found during this work and fixed:** `SavedWindow`,
`ListsWindow`, `ListTabWindow`'s `_insertRow` still populated columns
in the OLD order (Author/Message/Posted/Embed) after a past refactor
moved the shared header to Embed-first (Embed/Author/Message/Posted)
-- `FeedWindow` was fine (uses `FeedListMixin`'s shared row-insert),
the other three had their own duplicated `_insertRow` that never got
updated. Fixed in all three; confirmed working by the user.

**`onCharHook` consolidation (was on plan-08.md's lower-priority
list) -- done:** 5 near-identical copies (Home/Saved/Lists/ListTab/
Notifications) collapsed into one shared `FeedListMixin.onCharHook`,
gated per-class by `SUPPORTS_NEW_POST`/`SUPPORTS_FOCUS_NEXT_UNREAD`/
`SUPPORTS_SELECT_ALL`/`SUPPORTS_JUMP_TO_USER` class attributes (kept
each class's exact prior behavior, nothing silently added/removed).
Found and fixed a real bug in the process: `NotificationsWindow`'s old
onCharHook was missing the "Ctrl+F5 bubbles up to MainWindow" guard
the other 4 had, so Ctrl+F5 there did a single-tab sync instead of
`MainWindow.checkAllOpenTabs`'s full sweep -- fixed as a side effect
of the consolidation. `_jumpToUserPost`/`_postInvolvesUser`/
`onNewPost` (previously FeedWindow-only) moved up into `FeedListMixin`
so `ListsWindow` could opt into full Home-equivalent shortcuts per the
user's explicit design call (Home and Lists should behave identically
since they're the same kind of content; Notifications gets mark-read
only, no Left/Right cross-tab jump; Saved gets nothing extra).

**Multi-select "leftover selection" bug found and fixed:** Alt+number
(both chat and every FeedListMixin post-list tab) left the PREVIOUSLY
focused row still selected after moving to a new one, because
`uiutil.move_focus_and_check_announce`'s `Select(index)` call only
ADDS to a multi-select ListCtrl's selection, never replaces it --
`feedWindow.py`'s `_jumpToUserPost` had already solved this exact
problem locally (explicit deselect-others loop) but the fix was never
applied to the shared `move_focus_and_check_announce` helper. Fixed by
mirroring that same idiom in the one shared spot -- confirmed working
across all tabs by the user.

**A real, if minor, pre-existing bug found (unrelated to any of the
above) and fixed:** `FeedListMixin._onTimeRefreshTick` hardcoded
column index 3 for the "Posted"/time column, correct for the standard
4-column Embed/Author/Message/Posted layout but wrong for
`NotificationsWindow` (only 3 columns, Author/Notification/Received,
time column at index 2) -- would throw an unhandled `wxAssertionError`
(not caught by `safe_ui_callback`, which only catches "has been
deleted" RuntimeErrors) whenever the 60s timer ticked while
Notifications was the visible tab under a relative-time Display
setting. Fixed via a new `TIME_COLUMN_INDEX` class attribute
(default 3, `NotificationsWindow` overrides to 2). Confirmed by the
user this specific crash mode is gone, though it turned out NOT to be
the cause of the "quick close" crash reports below (different bug,
found and fixed along the way during the same testing round).

## The "quick close" crash saga -- resolved, worth reading in full for the eventual real cause

Two related-but-distinct crash reports arrived this session, both now
believed fixed (user confirmed the ORIGINAL untouched-window one is
gone; the refresh-in-progress one is also confirmed gone after the
final fix below):

1. **Original report**: open MainWindow (NVDA+Alt+B), close it again
   immediately, having touched nothing. Intermittent, sometimes took
   ~30s to manifest.
2. **Second report** (found while chasing #1): "Home tab > F5 refresh
   > close immediately" -- much more reliably/instantly reproducible
   once found.

**Root cause of #1, confirmed fixed**: `ListsWindow.__init__` (via
`_loadListsFromCache()`) was STILL unconditionally kicking off a
background `_syncListsFromServer()` network call on every single
MainWindow open, regardless of whether the user ever touched the
Lists tab -- this was the ORIGINAL Stage-0 crash from plan-07.md;
Stage 0 (the `safe_ui_callback` decorator) made the resulting
destroyed-window race SAFE at the Python level (caught RuntimeError
instead of crashing), but the previously-agreed "Stage 2" (remove the
auto-sync entirely, matching the other 4 permanent tabs which only
ever load from local cache at construction) was never actually done.
Every "quick close, untouched window" crash log showed
`ListsWindow._onSyncListsDone skipped -- already destroyed` as the
immediately-preceding event, every time. Removing the auto-sync call
(now Lists behaves exactly like Home/Saved/Notifications on open --
cache only, sync only on explicit F5/Shift+F5/user action) fully
fixed this per the user's confirmation. **User's stated intent**: some
form of background auto-update across all tabs IS wanted eventually,
just not now -- deliberately deferred to later, this was an accidental
early/partial version of it, not an intentional design.

**Root cause of #2, confirmed fixed, took 3 wrong guesses first**
(worth reading so the next session doesn't repeat them):
- Wrong guess 1: `ChatWindow._loadFromCache` reentrancy (a `SelectItem`
  synchronously firing `onConvoSelected` while still unwinding from an
  earlier call). Added a reentrancy guard -- harmless, kept, but did
  NOT fix this crash (chat wasn't even involved in the repro).
- Wrong guess 2: no timer cleanup in `MainWindow.onClose` at all
  (`wx.Timer` isn't part of the parent-child destroy cascade). Added a
  stop-loop for `_timeRefreshTimer` specifically -- directionally
  right but incomplete, see below.
- **Real cause**: `FeedListMixin._startLoadingBeep()`, called every
  time F5/`onCheckForUpdates` runs, creates a SEPARATE
  `self._loadingTimer` (1-second interval, much faster than the 60s
  `_timeRefreshTimer`) that is ONLY ever stopped from inside
  `_onCheckForUpdatesDone`. An `app_closing` module-level flag
  (`uiutil.app_closing`, checked first thing inside
  `safe_ui_callback`, set True at the very start of
  `MainWindow.onClose` and reset False at the start of
  `MainWindow.__init__`) was added for defense-in-depth on ALL
  CallAfter-dispatched completions -- but this ALONE made things
  WORSE for this specific bug, since it caused
  `_onCheckForUpdatesDone` to skip entirely when closing, meaning
  `_loadingTimer` NEVER got stopped at all, left ticking into a
  destroyed window every second. Real fix: `MainWindow.onClose` (and
  `ListTabWindow.onTabRemoved`, which had the identical gap for its
  own Ctrl+W removal path) now scan `vars(panel).values()` for every
  `wx.Timer` INSTANCE and `.Stop()` each one, rather than checking for
  specific attribute names -- avoids repeating this exact class of
  miss if another timer gets added later without this method being
  remembered/updated. The `app_closing` flag is still kept (harmless,
  useful defense-in-depth for the OTHER ~39 CallAfter-dispatched
  completions that aren't timer-related), just wasn't sufficient
  alone.

**Follow-up hardening audit done at the user's request** ("ไล่เช็กโค้ด
ให้แข็งแรงก่อนขยับไปฟีเจอร์ใหม่"), systematic not reactive:
- Cross-checked all ~39 unique `wx.CallAfter(self._onXxx, ...)` target
  methods against having `@uiutil.safe_ui_callback` -- found ONE real
  gap: `chatWindow.py`'s `_onSendComplete` (the chat-send background
  thread's completion handler) had NO decorator at all, meaning
  sending a message then immediately closing/removing that chat's tab
  before the post-send resync finished was an unguarded version of
  the exact same crash class. Fixed. Also added the decorator to
  `mainWindow.py`'s `_focusPanel` for consistency (much lower risk --
  same-thread deferred call, not a background-thread completion).
- Scanned every class's `__init__` for the same "unconditional
  background thread/sync call at construction" bug class as the
  ListsWindow one -- found nothing else, confirmed clean.
- Checked the Ctrl+W tab-removal path (`removeCurrentTab` ->
  `onTabRemoved`) for the same timer-cleanup completeness as
  `MainWindow.onClose` -- found `ListTabWindow.onTabRemoved` had the
  identical `_loadingTimer` gap, fixed with the same generic
  Timer-instance scan.

## Outstanding items carried forward (all previously lower-priority, none newly urgent)

1. Whether to remove the temporary `log.info` logging in
   `uiutil.safe_ui_callback` (added during the very first crash
   investigation, several sessions ago) -- user's explicit call this
   session: leave it in for now, this is still active dev/testing,
   clean it up in one pass at the actual end of the dev phase, not
   piecemeal.
2. `chat.bsky.convo.getLog` for incremental sync (pure perf,
   nice-to-have), a dedicated Chat options settings page (no concrete
   need identified), group chat (`chat.bsky.group.*`, wholly separate
   unstarted namespace) -- all explicitly "skip unless actually
   wanted", never revisited this session, not asked about again.
3. Whether `_stopTimeRefreshTimer()` (the old by-name method, now
   superseded by the generic `vars()`-scan pattern in both
   `MainWindow.onClose` and `ListTabWindow.onTabRemoved`) still has
   any other caller worth checking before deleting it as dead code --
   flagged but not checked this session, small cleanup item for
   whenever the log.info cleanup above happens.
4. "Show message..." context-menu item still has no keyboard shortcut
   bound -- user hasn't decided which key (Enter is reserved for a
   planned quick-action feature; Space was floated but not confirmed).
   Low priority, no urgency expressed.

## Next planned direction (per the user, start of next chat)

Hardening/audit pass is considered done for now (no further known
issues) -- next chat moves on to a NEW feature/tab rather than more
bug-hunting. Nothing has been decided yet about which one -- candidates
mentioned in earlier sessions but never scoped: Explore and Feeds tabs
(purpose/scope never decided). Start the next chat by asking the user
what they want to tackle, don't assume Explore/Feeds without asking.
