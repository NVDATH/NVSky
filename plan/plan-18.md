# NVSky — plan-18.md

Continuation from plan-17.md. This session completed the full SDK/
feature coverage audit plan-17.md scoped out (all 4 sections), plus
built every feature the audit flagged as worth building, plus found
and fixed a real, previously-unnoticed background-sync bug class
(hidden buttons still firing their own mnemonics) across the whole
codebase, plus shipped a real chat delta-sync architecture.

This was a long, dense session. Read this file's own sections in order
if picking specific threads back up -- don't assume anything not
listed here as DONE actually got done.

---

## 1. Confirmed DONE this session

### Full SDK/feature audit (plan-17.md §3, all 4 sections)
Went through every section plan-17.md scoped: **B (Graph/Social), A
(Posts/Feed), D (Chat), F (Moderation)**. Cross-referenced
`client.py` against the SDK's actual method surface (via
`dumpMethod.md`'s NVDA+Ctrl+Shift+D dump, re-confirmed working and
used again this session) and, for chat/notification internals with no
existing wrapper, live debug_dumps of real server responses (see §2
below for the debugging methodology that made this fast).

Section-by-section outcome:
- **B (Graph/Social)**: found 2 real gaps -- `get_mutes`/`get_blocks`
  had no UI at all (no way to see who you'd muted/blocked without
  visiting each profile). **Built.**
- **A (Posts/Feed)**: found 1 real gap -- self-labeling posts (adult-
  content categories) had no compose-time UI, and the read side
  (showing/hiding labeled posts) didn't exist either. **Built, both
  directions.** Also found `app.bsky.notification.get/putPreferencesV2`
  completely unwired (no way to control which activity notifies you at
  all). **Built.**
- **D (Chat)**: found the LOW CONFIDENCE gap `client.py` already
  flagged in its own comments -- `sync_convo_messages` never fetched a
  convo's own fresh `unread_count` (reused stale DB value). **Built**
  (`client.get_convo`). Also identified `chat.bsky.convo.getLog` as
  unwired and worth a real delta-sync architecture given chat's N+1
  background-sync cost. **Built** (see its own section below).
- **F (Moderation)**: audited `com.atproto.moderation.*`,
  `com.atproto.admin.*`, `tools.ozone.*` -- confirmed report post/actor
  already fully wired; everything else in `admin`/`ozone` is
  PDS/labeler-team tooling, correctly out of scope for an end-user
  client. `com.atproto.label.queryLabels` was flagged as the one real
  gap, but turned out **unnecessary** once content-label work started:
  `post.labels` already arrives hydrated on every PostView from
  `get_timeline`/`get_post_thread`/etc, no separate query needed.

### Muted / Blocked users (Settings)
One combined panel (`MutedBlockedActorsPanel` in settings.py), radio
switches between the two categories, `CustomCheckListBox` for bulk
unmute/unblock. Always fetches fresh on tab activation, no local
cache -- explicitly decided not worth caching for something checked
this rarely. `client.get_muted_actors`/`get_blocked_actors` wrap
`app.bsky.graph.getMutes`/`getBlocks`.

Went through 3 rounds of polish after initial build, driven directly
by user testing:
1. Fixed a real crash (`wxAssertionError: bad wxCheckListBox index`)
   -- a `CustomCheckListBox` with zero items still renders one
   phantom checkable row on Windows. Fixed by hiding the whole
   checklist (not just leaving it empty) whenever there's nothing to
   show, falling back focus to the Refresh button -- same fix pattern
   already used elsewhere in the codebase for this exact control.
2. Hid the Undo/bulk-action button too when the list is empty (missed
   this the first time -- only the checklist itself was hidden).
3. Added `soundpack.start_progress()`/`stop_progress()` around the
   network fetch, plus a spoken result summary after load
   (`nvdaUi.message`) -- initially the panel loaded silently with no
   audio feedback that anything had happened.

**Activity subscription (per-account "new post" bell) was explored,
built, debugged extensively, then FULLY REVERTED.** Root cause found
via a live debug_dump round-trip: `putActivitySubscription` writes
succeed server-side (confirmed via echoed response) but
`listActivitySubscriptions` always reads back empty regardless.
Checked bsky.app's own "More options" menu on a real profile -- **the
feature literally doesn't exist in the official app's UI at all**,
meaning the AT Proto endpoints exist server-side but the feature
hasn't shipped to users yet. All UI (User action menu entry, Settings
radio option) was removed; `client.set_activity_subscription`/
`get_activity_subscriptions` were LEFT IN `client.py`, unused, for
when/if Bluesky ships this for real.

### Content label system (self-labeling + read-side visibility)
Full round trip, spanning `db.py`, `client.py`, `compose.py`,
`feedWindow.py`, `feedTabs.py`, `listsWindow.py`, `soundpack.py`,
`settings.py`.

**Data model**: `posts.labels_json` column (new, no migration needed
per user's explicit "DB doesn't need to survive between sessions
during development" stance -- schema changes just go straight into
`CREATE TABLE`, old DBs get wiped by the user, not migrated).
`client._extract_post_labels(post)` reads `post.labels` (a list of
label-value strings) and gets stored alongside every post everywhere
posts are cached (`_store_feed_item`, `_store_resolved_post`,
`compose.py`'s optimistic insert).

**4 standard categories** (`client.CONTENT_LABEL_KEYS`): `porn`,
`sexual`, `nudity`, `graphic-media`. Deliberately NOT a dynamic list
pulled from subscribed labelers -- user's own call, since testing
labeler-applied labels isn't practically reproducible for a
single-person dev/test setup (no access to third-party labeler
output on demand).

**Preference storage**: `app.bsky.actor.defs#contentLabelPref` via the
existing raw-JSON preferences round trip (`_get_cleaned_preferences`/
`_put_preferences`, same pattern `muted_words` already used). Local
cache (`db.get/set_content_label_prefs_cache`, a ui_state blob) so
every list render can check visibility without a network call.

**Read-side behavior** (`_label_visibility(post)` in feedWindow.py,
used everywhere via `_visible_embed_text`/`_visible_message_text`):
- `hide`: post filtered out of the list entirely at `_loadFromCache`.
- `warn`: Embed column shows a bare `"Warning"`; Message column shows
  `"(Content warning: <matched category>)"` -- NOT the real content in
  either column. **Ctrl+Space** on the focused row reveals BOTH real
  values in place (rewrites the row's displayed cells directly via
  `SetItem`, never touches `self._posts` or any persisted state) --
  purely transient: arrow off and back, F5, or a tab switch reverts to
  the warning again on the next render. This went through several
  design iterations based on user feedback (see the actual chat
  history for exact back-and-forth) -- the final shape is: no
  "remember I revealed this" state anywhere, ever.
- `show`: normal.
- Coverage: `FeedListMixin`-based hosts only (Home, Saved, Lists
  timeline, ListTabWindow, UserTimelineTabWindow, FeedPreviewTabWindow,
  Explore's Posts results) -- **deliberately excludes** ThreadTabWindow/
  QuotesTabWindow (not FeedListMixin hosts, scoped out to control size).

**Sound**: new `content_warning` soundpack event key, played
(debounced, like the embed_* events) instead of the normal embed sound
whenever arrow-navigating onto a `warn`-visibility post.

**Compose-time self-labeling**: `ComposeDialog` gained a plain
`CustomCheckListBox` (NOT collapsible -- an earlier iteration had an
expand/collapse toggle button, explicitly removed per user request:
"just show it always, don't hide it behind a button") positioned right
after the Attach-media button, before Post/Cancel in both visual
layout and tab order (this positioning needed a real fix mid-session --
widget creation order determines wx tab order, and the label checklist
was initially added to the sizer AFTER the button row, landing dead
last in tab order instead of where intended). `client.create_post`
gained a `self_labels` param that builds the
`com.atproto.label.defs#selfLabels` record field. Confirmed self-labels
round-trip into `post.labels` on sync exactly like third-party labeler
labels, so the whole warn/hide pipeline works on your own posts with
zero extra code -- this is what let the user test the entire feature
end-to-end without needing a second account or an external labeler.

### Notification preferences (Settings > Display)
Added to the EXISTING Display tab, not a new tab -- explicit user
call ("tabs are getting too crowded already"). A plain `wx.ListCtrl`
(not a checklist -- these are 3-state cycles per row, not multi-select)
listing all 12 controllable categories (`follow`, `like`,
`like_via_repost`, `mention`, `quote`, `reply`, `repost`,
`repost_via_repost`, `starterpack_joined`, `subscribed_post`,
`unverified`, `verified`). **Space** on a focused row cycles
Off → Everyone → Following → Off (filterable categories) or
Off → On → Off (the 4 simple ones), fully optimistic (UI updates
immediately, `putPreferencesV2` call happens silently in the
background, rolls back on failure) -- no dialog, no confirmation.

`chat` (13th category from the API) is intentionally NOT exposed --
its shape (`include`/`push` only, no `list` field) doesn't fit the
combined off/everyone/following model used for the other 12, and
NVSky doesn't need to control it.

**Two real bugs found and fixed via live debug_dump round trips**
(both confirmed against `bsky.app`'s own Settings page after the
fix, not just "no error thrown"):
1. `putPreferencesV2` is a PUT (full replace), not a PATCH -- a first
   attempt sending only the one changed category had literally no
   effect, no error. Fixed by always sending every category (including
   `chat`, unexposed in the UI but still round-tripped so its existing
   setting on the account never gets silently reset).
2. The SDK auto-converts camelCase JSON keys to snake_case Python
   attributes ONLY on read -- a raw dict handed to `invoke_procedure`
   for a write is sent byte-for-byte, so keys like `like_via_repost`
   must be written back out as `likeViaRepost` etc, or the server
   silently ignores that field. `client.NOTIFICATION_CATEGORY_WIRE_KEYS`
   is the explicit snake_case→camelCase map now used on every write.

Also needed each category's own `$type` discriminator
(`app.bsky.notification.defs#filterablePreference`/`#preference`/
`#chatPreference`) explicitly set on write -- same class of pydantic
discriminated-union bug documented everywhere else in this file.

Category display-name wording got one clarification pass mid-session
-- `subscribed_post`/`unverified`/`verified` initially had vague
labels; rewrote them to actually explain what each does (bell-icon
per-account subscriptions -- a DIFFERENT feature from the reverted
Activity Subscription work above, this one just governs whether
subscribing via the OFFICIAL app notifies you, even though NVSky has
no UI to create such a subscription itself -- and Bluesky's identity-
verification checkmark being granted/revoked).

### Hidden-button-mnemonic bug: found, root-caused, and swept project-wide
**The single most valuable bug found this session**, discovered via
user-reported strange behavior (Alt+A in Chat silently triggering a
full resync with a ~4s delay before the progress sound, preceded by an
unexplained "Accepted." announcement).

**Root cause**: `wx.Button.Hide()` alone does NOT disable the
button's own mnemonic (`&`-prefixed accelerator) -- a hidden-but-still-
enabled button still fires its `EVT_BUTTON` handler when its Alt+letter
combo is pressed, REGARDLESS of visibility. `ChatWindow`'s Accept
button (`&Accept`) was hidden via `Show(False)` whenever the current
conversation wasn't a pending request, but never `Disable()`d --
so Alt+A anywhere in Chat, on ANY conversation, silently ran
`accept_convo()` on whatever `_currentConvo()` happened to return,
then a full `onCheckForUpdates()` resync (source of the ~4s delay and
the mismatched progress-sound timing vs plain F5).

**Fixed at the root** (`_updateActionArea` now pairs every
`.Show(cond)` with `.Enable(cond)` for `acceptButton`/`declineButton`)
plus a second defensive layer (`onAcceptButton` now also checks
`convo.get("status") == "request"` before calling `_acceptConvo`).

**Then swept the entire codebase for the same pattern** (any
`.Show(condition)` on a button with an `&` mnemonic, with no paired
`.Enable(condition)`), per explicit user request ("we should check
every spot for this same problem"). Found and fixed:
- `feedWindow.py`'s `FeedListMixin._updateActionButtons` (generic
  Post-action/User-action buttons shared by every feed-shaped tab) --
  the highest-risk instance, since Alt+A/Alt+U are used as GLOBAL
  shortcuts throughout the app; a hidden-but-enabled instance on any
  empty list was a real risk of misfiring against stale focus state.
- `exploreWindow.py`'s `ExploreWindow._updateActionButtons` (its OWN
  override, not the generic FeedListMixin one, so the earlier fix
  didn't cover it) -- postActionButton/userActionButton/
  resultActionButton/openInTabButton all needed the same treatment.
- `listsWindow.py`'s `ListsWindow` -- both the initial-construction
  hide (before any list is selected) and `_showSelectedList`'s
  moderation-list branch.
- `compose.py`'s `chooseLinkButton` (shown only when multiple URLs are
  detected in the post text).
- `settings.py`'s `MutedBlockedActorsPanel.removeButton`.

**Confirmed safe, left alone** (has its own internal guard already):
`ChatWindow`/`ConvoTabWindow`'s emoji/send buttons (`onSend`/
`onInsertEmoji` check `composeText.IsShown()` first), `MainWindow`'s
`removeTabButton`/`findListsButton` (callable/TAB_REMOVABLE checks
already present).

### Chat delta-sync via `chat.bsky.convo.getLog` (background sync only)
The biggest single architectural change this session. Full design
discussion happened first (see §2 for how the schema was actually
nailed down, since this is chat.bsky.* -- the most EXPERIMENTAL-
flagged surface in the whole project).

**Scope, exactly as planned in plan-17.md**: background sync
(`bgsync.sync_chat`) ONLY. Manual F5 (`ChatWindow.onRefreshSelectedConvo`)
and `ConvoTabWindow`'s own refresh are UNCHANGED, still full
`sync_convo_messages`/`sync_convos`. This was a deliberate, explicit
scoping decision -- prove the pattern on the lowest-risk caller first.

**Why chat specifically needed this** (and why notifications/feed
did NOT, see the explicit comparison below): the OLD `bgsync.sync_chat`
called `list_convos()` (1 call) then looped `sync_convo_messages()`
once PER conversation (N calls) -- a genuine N+1 problem. A user with
20 open conversations meant 21 API calls every single background-sync
tick (every 2 minutes by default). `getLog` collapses this to exactly
1 call regardless of conversation count.

**Design** (`client.sync_chat_delta`, `client.get_convo_log`):
- Cursor persisted per account (`db.get/set_chat_log_cursor`, a
  ui_state key).
- First-ever call (no stored cursor): bootstraps via the OLD full
  `sync_convos()`, then makes one throwaway `getLog(cursor=None)` call
  purely to seed a starting cursor for next time (its own empty logs
  are discarded -- the full sync above already has everything).
  CONFIRMED via testing: `getLog` with no cursor returns an EMPTY logs
  array plus a cursor representing "now" -- it is NOT a history dump,
  it's a "what's changed since X" endpoint with no backlog mode.
- Subsequent calls: `getLog(cursor=<stored>)`, dispatches each log
  entry by its `$type`:
  - `logCreateMessage`/`logAddReaction`/`logRemoveReaction`: upserted
    directly into the local message cache via a newly-factored-out
    shared helper `_upsert_message_from_raw` (this same helper is now
    also used by the OLD full-sync path `_sync_convo_messages`, so
    there's exactly one place that knows how to turn a raw
    MessageView dict into a DB row, not two drifting copies).
  - `logDeleteMessage`: new `db.delete_message` removes the row.
  - Every OTHER log type (20 of them -- `logBeginConvo`,
    `logAcceptConvo`, `logLeaveConvo`, `logMuteConvo`/`logUnmuteConvo`,
    `logAddMember`/`logRemoveMember`/`logMemberJoin`/`logMemberLeave`,
    `logLockConvo`/`logUnlockConvo`/`logLockConvoPermanently`,
    `logEditGroup`, all 4 join-link log types, all 4 join-request log
    types) triggers ONE full `sync_convos()` fallback for correctness
    -- deliberately not hand-parsing 20 different `SystemMessageView`
    payload shapes for events that are rare compared to plain
    messaging. This is `client._STRUCTURAL_LOG_TYPES`.
  - `logReadConvo`/`logReadMessage` (deprecated alias) are
    DELIBERATELY IGNORED in the delta path -- background sync's job is
    "is there new content to tell the user about", not exact
    per-message read/unread bookkeeping; F5 still does that properly
    via the untouched `reconcile_message_read_state` path.
- After processing, every convo that received a new non-own message
  gets ONE cheap `client.get_convo()` call (the Tier-1 single-convo
  fetch built earlier this session) to refresh its `unread_count`/
  last-message state -- avoids a full `list_convos()` just to keep
  `ChatWindow`'s tree labels accurate.
- `bgsync.sync_chat` itself is now a thin wrapper: calls
  `sync_chat_delta`, returns which convo names changed (for the
  existing "New post in: X" announcement plumbing) -- the old
  before/after full-snapshot diff logic is gone entirely.

**How the schema was actually nailed down** (see §2's methodology
notes for the reusable process): repeated live debug_dump round trips
against a real account hit THREE real bugs in sequence before this
worked, each one a genuine "wrong API usage" bug in the code being
written, not an SDK/server limitation:
1. First attempt hung indefinitely calling the TYPED `dm.get_log()`
   method with no timeout -- traced to needing a raw-JSON bypass
   (same class of pydantic discriminated-union parsing failure
   documented everywhere else in chat.bsky.* handling in this file:
   "Unable to extract tag using discriminator 'py_type' | 'pyType'").
   Fixed by calling `invoke_query("chat.bsky.convo.getLog", ...)`
   directly instead of the typed wrapper, with a queue+15s-timeout
   bound so a hang reports clearly instead of blocking a background
   thread forever.
2. A debug instrumentation call to explore the getLog response shape
   was WIRED INTO THE WRONG METHOD in `chatWindow.py` initially
   (`onCheckForUpdates`, which only Ctrl+F5/the toolbar button reaches
   -- NOT plain F5, which actually calls `onRefreshSelectedConvo`).
   This meant the debug call silently never ran when the user tested
   with plain F5, wasting a full debugging round before being caught.
3. Once wired to the right method: `debug_get_convo_log`'s exploratory
   version passed a PLAIN DICT as `params` to `invoke_query` --
   crashed real background sync in production with `'dict' object has
   no attribute 'model_dump'` (invoke_query calls `.model_dump()` on
   whatever `params` object it's given; every OTHER `invoke_query`
   call in this file already correctly passes a typed
   `models.ChatBskyConvoGet*.Params(...)` object, this one didn't
   follow that established pattern). This bug reached a real user's
   running background sync before being caught and fixed -- **the
   final, correct `get_convo_log` uses
   `models.ChatBskyConvoGetLog.Params(cursor=cursor, limit=limit)`**.
   Once fixed, the user confirmed: "bgsync ไม่พัง" (background sync no
   longer breaks), and delta-sync is now live and stable.

All debug/exploratory instrumentation code (the timeout-guarded
`debug_get_convo_log`, the module-level cursor variable it used) was
FULLY REMOVED once the real implementation shipped -- nothing
exploratory is left in the codebase.

### Explicit comparison, settled this session: why NOT feed/notifications too
User directly asked whether feed and notifications needed the same
delta-sync treatment, given it "saves API calls." Answer settled and
recorded here for future reference: **the N+1 problem chat had is
unique to chat.** `sync_timeline`/`sync_notifications` each make
exactly ONE API call already, with no per-item loop -- there is
nothing to collapse. Bluesky also has no timeline/notification
equivalent of `getLog` (a "what changed since X" endpoint) -- only
backward-pagination cursors ("give me older items"), which is a
different concept entirely, and both `sync_timeline` and
`sync_notifications` already use that correctly.

**Lists DOES have the same N+1 shape as chat** (`bgsync.sync_lists`
loops `sync_list_feed()` once per open curation-list tab), but was
explicitly NOT changed -- Bluesky has no `getLog` equivalent for list
feeds either, so there's no endpoint to build delta-sync FROM. Its N+1
cost is also inherently bounded by how many list tabs the user has
open at once (not by total list count), which is a much gentler scale
than chat's "one call per conversation regardless of which ones you
actually look at."

### Notification sync cursor overflow bug (found via direct user pushback)
User correctly rejected an early "not urgent, your usage is light"
framing -- the argument that mattered instead: **this is an add-on
meant for public release**, and even for THIS user, leaving NVSky
closed for a while and coming back is a realistic way to exceed the
threshold, regardless of typical interaction volume.

**Bug**: `sync_notifications` always fetched just the newest 50
notifications with NO persisted resume point. Any account receiving
more than 50 notifications between two syncs (a popular account, or
NVSky simply left closed a while) silently lost whatever fell past
position 50 -- no error, no indication anything was missed.

**Fix**: `db.get/set_notification_sync_cursor` (a ui_state key,
storing the newest notification's own `uri` as of the last resume
sync, NOT a server pagination cursor -- `listNotifications`' own
cursor only paginates backward/older, it has no forward/"since"
concept, so an actual item identity is the only viable resume marker).
`sync_notifications` now walks pages forward from the newest until it
either re-encounters that stored uri (fully caught up) or hits
`NOTIFICATION_SYNC_MAX_PAGES` (20 pages = up to 1000 notifications --
a safety cap so a genuinely enormous backlog, e.g. after months
closed, doesn't block on one unbounded fetch; it syncs what it can and
picks up the rest on the NEXT bgsync tick).

The existing caller that passes an explicit `cursor` param (the
Notifications tab's own Shift+F5 "fetch older"/lazy-load path) is
completely unaffected -- that code path still walks exactly one page
from the given cursor, exactly as before; the new resume-tracking
logic only activates when `cursor=None` (a fresh background/manual
sync), and only THAT path reads/writes the persisted resume marker.

**NOT independently confirmed working** by the user beyond "doesn't
error and normal refresh still works" -- the actual overflow scenario
(receiving 50+ notifications between two syncs) is hard to
deliberately trigger with this account's current low interaction
volume. Logic follows the same pagination/cursor conventions already
proven elsewhere in this file; risk is assessed as low, but flag this
explicitly if a gap ever surfaces (e.g. notifications tab showing
fewer items than expected after a long time away from the account).

---

## 2. Methodology note: live debug_dump round trips, confirmed as the
##    right way to resolve LOW CONFIDENCE areas going forward

This came up explicitly and the user asked Claude to REMEMBER this
process for future sessions -- recorded here so it survives.

**The pattern**: instead of guessing SDK/API shapes from training
data or general lexicon knowledge (which this project's own comment
history shows has been unreliable multiple times across many prior
sessions), add a temporary call to `client.debug_dump(response,
"some_label")` at the exact point in the code the user can trigger
with a button/keystroke they already know (F5, a Settings tab open, a
User action menu item) -- NEVER ask the user to open a Python console
or type commands themselves. The dump lands in
`globalPlugins/NVSky/debug_dumps/<label>_<timestamp>.json`; the user
attaches that file back in chat. This round trip is fast (one user
action, no typing) and gives GROUND TRUTH instead of a guess.

**Explicitly rejected this session**: asking the user to run
multi-line Python snippets in NVDA's Python console. Tried this once
for the activity-subscription investigation; user pushed back hard
("ทำแล้วทำอีก อะไรไม่รู้... กลับไปใช้แนวทางที่ถูกต้องด้วย" -- roughly
"you keep making me do more and more, I don't know what for... go
back to doing it the right way") -- the "right way" being: wire the
debug_dump call into a UI action that ALREADY EXISTS and ask the user
to press it, exactly as `debug_dump` calls elsewhere in this codebase
were already designed to be used. This is now the standing default,
not a fallback -- for any future LOW CONFIDENCE endpoint, wire a
temporary `debug_dump` into an existing button/keystroke FIRST, before
writing speculative parsing code.

**Also confirmed working this session**: for genuinely undocumented-
in-code lexicon shapes (chat's full `getLog` log-type union), a plain
web search for `atproto.blue` (the exact SDK docs site matching the
vendored SDK version) or the raw lexicon JSON on GitHub
(`bluesky-social/atproto`) gave a complete, authoritative answer
faster than several rounds of trial-and-error debug_dumps would have.
**For future EXPERIMENTAL chat.bsky.*/app.bsky.* work, check
atproto.blue's model docs page for the relevant namespace FIRST**,
before reaching for debug_dump -- reserve debug_dump for confirming
things the docs don't cover (actual server behavior quirks, whether a
write really lands, wrong-casing bugs) rather than basic schema shape.

---

## 3. Carried over, unchanged, still not started

From plan-17.md's original backlog:
1. ~~Sound system~~ -- done (plan-17.md session).
2. DB portability (DPAPI key machine+user-locked) -- known limitation,
   not revisited, not planned to be.
3. ~~Self-labeling posts~~ -- **DONE this session**, both directions
   (compose-time labeling AND read-side warn/hide).
4. "Manage members..." admin gating -- still no confirmed
   `InsufficientRole` error to justify adding it; leave alone until one
   shows up.
5. ~~User action menu full audit~~ -- superseded by the full-system
   audit, now complete.
6. ~~Full SDK/feature coverage audit~~ -- **DONE this session, all 4
   sections.**

New, deliberately NOT pursued this session (decided, not just
deferred):
- `com.atproto.label.queryLabels` -- turned out unnecessary; `post.labels`
  arrives hydrated on every existing PostView fetch already.
- Activity subscription (per-account post-notification bell) --
  reverted; feature doesn't exist in the official app yet, endpoints
  are server-side-only pre-release. `client.py` wrapper functions left
  in place, unused, for whenever Bluesky ships it for real.
- Feed/notification delta-sync -- explicitly not needed, see the
  comparison recorded in §1 above.
- Lists delta-sync -- no endpoint exists to build it from; N+1 cost is
  inherently bounded by open-tab count, not total list count.

---

## 4. Suggested starting point for next session

No firm backlog item remains from either plan-17.md or this session's
own work. Genuinely open-ended going into the next session -- possible
directions, none committed to:

1. **Real-world testing pass**: several features shipped this session
   were tested only lightly by necessity (notification-preferences
   writes were confirmed against bsky.app directly -- solid; the
   notification-sync-cursor-overflow fix and the chat delta-sync
   structural-log fallback path were NOT independently exercised
   against their actual trigger conditions, since those require either
   a busy account or deliberately causing a group-chat structural
   event). If a natural opportunity arises (a friend willing to
   generate real chat/group activity, or the account naturally
   accumulating more notification volume over time), revisit and
   confirm these paths for real.
2. If the person wants a specific new feature or a different corner of
   the app looked at, that supersedes anything above -- there's no
   scoped-but-unstarted work waiting.
