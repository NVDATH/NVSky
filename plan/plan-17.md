# NVSky — plan-17.md

Continuation from plan-16.md. This session did two things: (1) split
`feedWindow.py` into multiple files (the split flagged-but-deferred in
plan-16.md), and (2) designed and implemented the full SoundPack
system (also carried over from plan-16.md's backlog). Neither had been
started before this session. A third item -- a full SDK/feature
coverage audit -- was scoped at the end of this session but not
started; it's the primary work for the next session.

---

## 1. Confirmed DONE this session

### File split (plan-16.md's deferred decision, now executed)
`feedWindow.py` (was 6,892 lines / 23 classes) split into:
- `notificationsWindow.py` -- `NotificationsWindow`, `_describe_notification`.
- `listsWindow.py` -- `ListsWindow`, `ListTabWindow`, `AddListDialog`,
  `SubscribeListDialog`, `ManageMembersDialog`, `AddToListDialog`.
- `exploreWindow.py` -- `ExploreWindow`, `StarterPackDetailsDialog`.
- `feedTabs.py` -- `ThreadTabWindow`, `QuotesTabWindow`, `ProfileDialog`,
  `UserListTabWindow`, `UserTimelineTabWindow`, `FeedPreviewTabWindow`,
  `SavedWindow`.
- `feedWindow.py` -- kept the 6 mixins (`RemovableTabMixin`,
  `UserActionMixin`, `UserListMixin`, `EmbedViewMixin`,
  `FeedListMixin`, `ItemActionMixin`) + `FeedWindow` (Home) +
  module-level helpers (`_message_text`, `_describe_embed`,
  `_embed_sound_event`, `_search_feed_key`, `REPORT_REASONS`, etc).
  Did NOT get split further into a separate `feedMixins.py` -- decided
  the size reduction already achieved was sufficient, further
  splitting wasn't worth the added import-graph risk.

Bugs found and fixed as a byproduct of the split:
- `UserActionMixin.showUserLists`/`addToList` and
  `ItemActionMixin._onThreadFetchedForOpen`/`_onQuotesFetchedForOpen`/
  `_openUserListForPost`/`_onProfileFetched` referenced classes that
  had just moved to `listsWindow.py`/`feedTabs.py` without an updated
  import -- fixed via lazy `from . import listsWindow` / `from . import
  feedTabs` inside each method (8 call sites total).
- Unsaving a post from any tab OTHER than Saved itself never removed
  the row from Saved's own `feed_items` cache -- `_onBookmarkChanged`
  was a `SavedWindow`-only hook, so an unsave from Home/Notifications/
  etc left the DB row (and an already-open Saved tab) stale
  indefinitely. Fixed in `ItemActionMixin._onBookmarkToggleDone`: now
  unconditionally deletes the "saved" feed_items row on unsave
  regardless of which tab the toggle happened from, and live-refreshes
  an already-open Saved tab in another panel via a new
  `_refreshOtherSavedTabs` helper.

Full retest of the split was **not done exhaustively** (person's own
words: "ไม่เจอบั๊ก = ผ่าน" -- tested casually, no dedicated pass through
every menu path). Treat as stable but not proven; if a `NameError` or
missing-import surfaces later in any moved class, check the 8 lazy-
import call sites above first.

### SoundPack system (plan-16.md's other backlog item, now built end to end)
Architecture: `globalPlugins/NVSky/SoundPack/<PackName>/*.wav`.
`soundpack.py` (new file) is the engine -- `SoundEngine` class, one
process-wide singleton via `get_engine()`. Settings > Sound (in
`settings.py`, `SoundPanel`) lets the user pick a pack (dropdown, with
"Silent / No sound" pinned first) and a per-event checklist
(`CustomCheckListBox`, same control style as user-search-results
checklists elsewhere in the app).

**Storage schema uses "disabled events" not "enabled events"**
(`db.get_soundpack_disabled_events`/`set_soundpack_disabled_events`) --
deliberate, so any event key added to `soundpack.EVENT_KEYS` in a
future session is enabled by default automatically for existing users,
without needing a migration step. Confirmed this bug pattern happened
once already this session (embed_image/embed_video/embed_link/
embed_quote came up unchecked for anyone who'd already saved Settings
before those 4 keys existed) -- fixed by flipping the storage direction,
not by migrating old data.

**25 discrete (one-shot) event keys, final list:**
```
like, unlike, repost, unrepost, save, unsave, send_post, delete,
follow, unfollow, block_mute,
send_message, new_message,
notification,
open_tab, close_tab, boundary, error, ready,
embed_image, embed_video, embed_link, embed_quote,
max_length
```
Missing file for any of these = silent, no beep fallback (deliberate --
silence is a valid pack-author choice, not treated as broken).

**One looped progress-indicator key** (`PROGRESS_KEY = "progress"`),
same pattern as YoutubePlus's own `_progress_indicator_worker`
(referenced directly from that add-on's source during design) --
`start_progress()`/`stop_progress()`, runs on its own thread, 1-second
loop interval. Unlike discrete events, **this one DOES fall back to
`tones.beep(500, 50)`** if the selected pack has no `progress.wav` --
silence during a genuine multi-second wait reads as a freeze, so this
is the one place a beep fallback was kept deliberately.

Also added `SoundEngine.play_debounced(event_key, delay_ms=150)` --
used only for the 4 `embed_*` events (fired from `onItemFocused` while
arrow-key-navigating a post list) so fast scrolling through a feed full
of images doesn't fire an overlapping sound per row; only the row the
user actually settles on plays anything, via a cancel-and-restart
`threading.Timer`.

**Wired call sites** (all confirmed working via live testing):
- `compose.py`: `send_post`/`error` (post success/fail),
  `max_length` (post too long).
- `feedWindow.py` `ItemActionMixin`: `like`/`unlike`, `save`/`unsave`,
  `repost`/`unrepost` (optimistic, fires immediately not after network),
  `delete`/`error` (post delete).
- `feedWindow.py` `UserActionMixin`: `follow`/`unfollow`, `block_mute`
  (mute/block/report, one sound shared across all three -- deliberate,
  these are infrequent enough not to need per-action sounds).
- `chatWindow.py`: `send_message`, `new_message` (incoming, guarded --
  see bug note below), `delete` (leave conversation, delete message),
  `error` on every sync/send failure path, progress indicator wired
  into every F5/Shift+F5/Ctrl+F5 sync path in both `ChatWindow` and
  `ConvoTabWindow` (this was a real gap found mid-session -- chat had
  ZERO progress feedback before this pass, not just the Shift+F5 case
  the person originally flagged).
- `listsWindow.py`: `delete` (remove list), `error`.
- `mainWindow.py`: `open_tab`/`close_tab` on `addTab`/`removeCurrentTab`
  -- **`addTab` gained a `play_sound=True` param**, explicitly passed
  `False` from `GlobalPlugin._buildTabs()`'s startup-restore loop in
  `__init__.py`, because temp-tab restore on every NVSky launch was
  firing `open_tab` once per restored tab. This exact "programmatic
  action fires the same sound as a real user action" bug pattern
  recurred twice more later in the session (see below) -- worth
  remembering as a recurring risk class specific to this sound system,
  the same way plan-16.md flagged duplicate-method-definitions as a
  recurring risk class for this codebase.
- `feedWindow.py`/`feedTabs.py`: `embed_image`/`embed_video`/
  `embed_link`/`embed_quote` via `_embed_sound_event()` (new helper) +
  `onItemFocused` hooks. `FeedListMixin.onItemFocused` already guards on
  `_suppressFocusEvents` (pre-existing flag, originally for the
  mark-as-read logic) so this was safe there immediately. `ThreadTabWindow`/
  `QuotesTabWindow` are NOT `FeedListMixin` hosts and had no such flag --
  had to add a local `_suppressFocusEvents` copy to both plus wrap
  every programmatic `Focus()/Select()` call in their render methods,
  after confirming live that opening a thread/quotes tab played an
  embed sound for whatever post happened to be auto-focused on open.
- `feedWindow.py` `_embed_sound_event`/`_describe_embed`: **recordWithMedia
  (quote + attached media) now distinguishes image vs video** instead of
  always saying/sounding like a generic "Quote + media" -- confirmed via
  `client._extract_embed_info`/`_fill_media_info` that this distinction
  was already resolved server-side and cached, just never read by either
  the display text or the sound-event mapper. Fixed both in the same
  session (this was flagged directly by the person, not found
  independently).
- Boundary sound (`boundary`) wired via **deterministic index/sibling
  comparison**, NOT `wx.CallAfter`-based before/after comparison -- the
  first attempt at this (comparing focused index before vs after via
  `wx.CallAfter`) fired on every single arrow press regardless of
  position, because `EVT_CHAR_HOOK`'s callback ran before the native
  ListCtrl had actually processed the key, so "after" always read
  identical to "before." Fixed everywhere by computing directly from
  current index/list length or `GetPrevSibling`/`GetNextSibling` (for
  TreeCtrl), no waiting on native behavior at all. Covered:
  `FeedListMixin.postList` (Home/Saved/Lists-timeline/Notifications/
  UserTimeline/FeedPreview -- shared via the mixin, one fix covers all),
  `ChatWindow`/`ConvoTabWindow`'s `messageList`, `ChatWindow`'s
  `convoTree`, `ListsWindow`'s `listTree` and `memberList`. Deliberately
  NOT extended to dialog-internal checklists (search results, member
  pickers in `ManageMembersDialog` etc) -- person's call, those are used
  too rarely to be worth it.

**Known accepted limitation, not fixed:** `SoundEngine` progress
indicator is a single global instance -- if two tabs both start a
progress sound and one finishes first, `stop_progress()` will cut off
the still-loading second tab's indicator too. Discussed explicitly;
person judged the realistic collision case (spamming refresh across
several tabs at once) unlikely enough not to warrant reference
counting. Revisit only if it turns out to actually bite someone.

**Deliberately deferred, not a bug:** `new_message`'s guard against
firing once-per-historical-message on a conversation's first-ever sync
(`client._sync_convo_messages`'s `isFirstSyncForConvo` check) was added
defensively without live confirmation that the bug it prevents was ever
actually observed -- chat is "very hard to test" per the person (can't
easily simulate an incoming message from another account on demand).
Treat the guard as correct-by-design, not correct-by-observation.

---

## 2. Available tooling discovered this session

**`dumpMethod.md`** (uploaded by the person, not part of NVSky itself)
-- a separate standalone `GlobalPlugin` script, NVDA+Ctrl+Shift+D,
recursively walks every attribute the *live, logged-in* `atproto`
`Client` object exposes (via `nvskyClient.get_client_for_active_account()`)
and dumps the full method/namespace tree to
`globalPlugins/NVSky/debug_dumps/all_bluesky_methods_<timestamp>.json`.

This is the single most reliable source for "what does the SDK
actually expose" for the audit work below -- more reliable than
web-searching lexicon docs (which can be stale, ambiguous about SDK
method names vs raw lexicon NSIDs, or simply not indexed well) and more
reliable than trusting Claude's own training data about SDK internals
(confirmed unreliable multiple times already in this project --
see plan-11 through plan-16's many "LOW CONFIDENCE" markers throughout
`client.py` itself). **Next session should ask the person to run this
dump FIRST**, before doing anything else, and attach the resulting JSON
-- turns most of the audit from "guess from docs" into "read a file."

Caveats to keep in mind reading that dump:
- It reflects whatever SDK version is vendored in this project's own
  `lib/` folder, not necessarily the latest PyPI release.
- It's a live object graph, not a lexicon schema -- method existence on
  the object doesn't guarantee the underlying endpoint actually works
  against a real server (same class of issue as this project's many
  existing "LOW CONFIDENCE -- never exercised against a real server"
  comments). Still, it's a strict upper bound: if a method doesn't
  appear in the dump, it's not in the SDK's Python surface at all, and
  is not worth chasing further.
- The script's own namespace/method split heuristic (`inspect_is_namespace`)
  is a guess (checks `hasattr(item, "__dir__")` and excludes primitives)
  -- expect some noise/misclassification in the dump, not a perfectly
  clean tree.

---

## 3. Next session's primary work: full SDK/feature coverage audit

Expanded scope from plan-16.md's narrower "user action menu" audit --
now covers the whole add-on, organized by AT Protocol lexicon
namespace so it can be tackled section by section instead of as one
undifferentiated pass.

### Sections, in the agreed working order
1. **B. Graph/Social** (`app.bsky.graph.*`) -- follow, block, mute,
   lists, starter packs, known followers. Start here -- most directly
   maps to plan-16's original ask (user action menu).
2. **A. Posts/Feed** (`app.bsky.feed.*`) -- post, like, repost, quote,
   thread, threadgate, postgate, bookmark, search. Largest section,
   most likely to turn up unwired functionality given `client.py`'s
   size.
3. **D. Chat** (`chat.bsky.*`) -- convo, group, message, reaction, join
   link. Newest/most EXPERIMENTAL-flagged section of the whole
   codebase already (see chatWindow.py/client.py's own comments) --
   expect the most real gaps here.
4. **F. Moderation** (`com.atproto.moderation.*`, `com.atproto.admin.*`)
   -- report post/actor, full label system. Handle carefully --
   anything touching moderation/labels needs more caution than a
   normal feature gap, not a rush job.
5. **C. Actor/Profile, E. Notifications, G. Identity/Repo, H. Video/Blob**
   -- lower priority, order TBD based on what sections 1-4 turn up.

### Per-section method
1. Get the `dumpMethod.md` JSON dump (see §2) for ground truth on what
   the SDK exposes.
2. Cross-reference against `client.py`: split into (a) wrapped AND
   called from some UI menu/button already -- done, skip; (b) wrapped
   in `client.py` but no UI call site anywhere -- built halfway; (c) not
   wrapped at all, but present in the SDK dump.
3. For every item in (b) and (c), decide: **build it / skip it
   (with reason) / defer it (with the condition that would unblock it)**.
   Bias toward NOT building low-value or high-risk items just because
   the SDK happens to expose them -- this is a coverage AUDIT, not a
   mandate to wire up every SDK method that exists.
4. Output a table per section: endpoint/method name | wrapped? | called
   from UI? | recommendation + one-line reason.

### Constraints carried over from the rest of this project's conventions
(see plan-11 through plan-16, still binding for this work)
- English-only in code/comments/strings; conversation in Thai.
- One patch fence per fix; full-method replacement instead of tiny
  diffs when more than 1-2 lines change; file name always stated before
  a patch block; never say "patch as above" without actually pasting it.
- New wx controls need a mnemonic (`&`) from the start.
- `# Translators:` comment above every new user-facing string.
- Full file delivery preferred over patches when creating a new file or
  when a change touches a large fraction of an existing file (this
  session's file-split work leaned on this heavily and it worked well).
- Anything genuinely uncertain about server behavior gets a
  "LOW CONFIDENCE" comment and an explicit ask to paste back a
  traceback/debug_dump rather than silently guessing -- this project's
  established norm, not new for this session.

---

## 4. Carried over, unchanged, still not started

From plan-16.md's original backlog, not touched this session:
1. ~~Sound system~~ -- **DONE this session.**
2. DB portability (DPAPI key machine+user-locked) -- known limitation,
   not revisited, not planned to be.
3. **Self-labeling posts** (adult/graphic content at compose time) --
   still not started, still a real user-facing gap, still worth
   prioritizing once the audit above is done or in parallel if the
   person wants two threads of work going.
4. "Manage members..." admin gating -- still no confirmed
   `InsufficientRole` error to justify adding it; leave alone until one
   shows up.
5. ~~User action menu full audit~~ -- superseded/absorbed into the
   expanded full-system audit above.

---

## 5. Suggested order for next session

1. Ask the person to run `dumpMethod.md`'s NVDA+Ctrl+Shift+D against a
   real logged-in account and attach the resulting JSON, plus a fresh
   copy of `client.py` (this file changes fastest and is the audit's
   real subject) and whichever UI files are needed as each section is
   reached (`feedWindow.py`, `feedTabs.py`, `listsWindow.py`,
   `chatWindow.py` first, matching the section order above).
2. Work section B (Graph/Social) end to end: dump cross-reference,
   table, decisions, and -- if the person wants to act immediately
   rather than review the whole audit first -- implement whatever gets
   marked "build it, high priority" in that section before moving to
   section A.
3. Continue down the section list from §3 above.
4. Self-labeling posts (§4 item 3) whenever there's a natural pause
   between audit sections, or after the audit concludes -- person's call
   on timing.
