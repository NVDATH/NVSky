# plan-11.md — NVSky handoff (Explore/Feeds session)

Session goal: build Explore tab (search: Posts/People/Starter packs/Feeds) and
lay groundwork for Feeds-in-Settings. Explore is structurally complete but has
one confirmed-broken bug going into next session; Settings/Feeds work never
started.

## Confirmed working (tested by user this session)

- Explore tab exists as permanent tab, position 3 (Home, Notifications,
  **Explore**, Saved, Chat, Lists), reorderable via Ctrl+Shift+PageUp/PageDown
  with order persisted (`db.get_ui_state("permanent_tab_order")`) — confirmed
  no double-announcement after the RemovePage/InsertPage suppress-flag fix.
- Search box + Result type radio (Posts/People/Starter packs/Feeds), debounced
  auto-search (~800ms idle), initial focus lands on search box correctly (both
  on first MainWindow open and on switching back to the tab), and no longer
  gets yanked back to the search box on F5/refresh (fixed via a one-time
  `_didInitialFocus` guard in `_restoreFocusPosition`, plus never stealing
  focus while the user is actively typing).
- Advanced search (From handle/Since/Until/Language) — collapsible via a
  `wx.CheckBox` (NOT `wx.ToggleButton`, which isn't used anywhere else in this
  codebase and apparently didn't announce state correctly), hidden panel with
  labelled fields, auto-hidden when result type isn't Posts. LOW CONFIDENCE:
  since/until/author/lang param names on `search_posts` recalled from lexicon
  knowledge, never independently verified against a real response — user
  confirmed filtering "works correctly" but this wasn't independently
  double-checked field-by-field.
- Posts results: real DB-cached, paginated feed (via `_dbGetPage`/`_syncPage`
  → `client.sync_search_page`, feed_key = `_search_feed_key(query, filters)`),
  NOT a one-shot fetch — matches Home tab's architecture. Fetch-older
  (scroll-to-bottom) confirmed working. `SUPPORTS_FOCUS_NEXT_UNREAD = True` is
  now set (was missing, so jump-to-unread never worked despite the status bar
  showing an unread count) — **not yet re-tested since being added**.
- People/Starter packs/Feeds results: separate dedicated ListCtrls swapped via
  Show/Hide. People reuses `UserActionMixin` (`_populateUserActionMenu`) both
  via right-click and a dedicated "User action" button. Starter packs: "View
  pack details..." (member count shown, full member/feed list, Follow/Open
  buttons built directly into the details dialog — not just the outer context
  menu) + "Follow everyone in this pack" (via `graph.follow` per profile,
  skips already-followed) + "Open on bsky.app". Feeds: "View feed..." (opens a
  real paginated tab, see below), "Open on bsky.app". Action buttons correctly
  hidden when the current result type has zero results.
- "View feed" (from a Feeds search result) and "Open in new tab" (from Posts
  results) both open a `FeedPreviewTabWindow` — a real cached/paginated tab
  (same `_dbGetPage`/`_syncPage` contract as Home, `sync_feed_generator_page`/
  `sync_search_page` in client.py), NOT a one-shot preview. Confirmed:
  fetch-older works, persists across closing/reopening NVSky (`TAB_TEMP_TYPE
  = "search_preview"`, reconstructed from `db.get_open_temp_tab`s at startup;
  stale pre-this-session entries missing "source_key" are dropped instead of
  crashing on reconstruction). Confirmed: closing a `FeedPreviewTabWindow`
  that was opened from Explore jumps focus back to the Explore tab (via
  `origin_key` tracked through `TAB_TEMP_TYPE`/temp-tab entries) — **user
  explicitly wants this "remember where I came from on close" behavior
  audited across every OTHER pop-out tab type in the app (View Thread,
  followers/following lists, chat's "Open in new tab", etc.) next session —
  Explore is the only place it's been done so far.**
- `FeedPreviewTabWindow` now also has an "Add to my feeds" button when opened
  from a feed generator (not from a pinned search) — untested this session
  (added in the same round as the `_markSelectedRead` crash below, never
  reached testing).

## Known bugs, blocked on source not yet provided

- **`ExploreWindow` crashes on open with `AttributeError:
  'ExploreWindow' object has no attribute '_markSelectedRead'`** — this
  method lives in `ItemActionMixin`, which `ExploreWindow` explicitly
  inherits, so by MRO it should exist. Strong suspicion (same failure mode hit
  multiple times this session): a stray `class` declaration got left in the
  middle of `ExploreWindow`'s own method list at some point across the many
  edits this session (exact same root cause as the earlier
  `StarterPackDetailsDialog`-in-the-middle-of-`ExploreWindow` bug, fixed once
  already this session but apparently recurred or a different instance of it
  was never caught). **Needs: paste the full current `ExploreWindow` class
  (and ideally a plain grep of `^class ` across feedWindow.py) before touching
  it again** — guessing further without seeing the real file risks the same
  whack-a-mole seen earlier this session (fixing one missing method only to
  discover the next one two patches later). Still open going into next
  session -- blocks testing "Add to my feeds" and `SUPPORTS_FOCUS_NEXT_UNREAD`
  (jump-to-unread) in `FeedPreviewTabWindow`, neither of which has been
  reachable since this crash appeared.

## Resolved this session (verified against a real server)

- **`add_feed_to_saved` (client.py)** — RESOLVED. Three of Claude's own fix
  attempts all hit the identical `Unable to serialize unknown type:
  <class 'pydantic.fields.FieldInfo'>` error (root cause: `atproto` v0.0.69's
  generated `ContentLabelPref` model has a broken `py_type` field that holds
  the raw, unresolved `FieldInfo` declaration instead of its string value,
  and no form of `model_dump()` on that object could serialize it). The user
  brought in outside help (Gemini, then ChatGPT) and landed on a working fix:
  the real problem wasn't the dump step at all -- `client.app.bsky.actor.
  put_preferences()` itself calls `get_or_create()` internally, which
  **rehydrates the already-sanitized plain-dict payload back into a fresh
  Pydantic model** before calling `model_dump_json()`, hitting the exact same
  `FieldInfo` bug a second time regardless of how clean the input dict was.
  Passing raw JSON bytes into `invoke_procedure()` doesn't avoid this either,
  since the low-level client still calls `get_model_as_json(data)`, which
  expects `data` to expose `model_dump_json()`. The working pattern: build a
  clean raw payload and drive the **low-level `app.bsky.actor.putPreferences`
  procedure path directly**, in a way that never rehydrates the payload back
  into a Pydantic model before serialization. Confirmed working against the
  real server -- the added feed shows up on Bluesky Web. Do not touch
  `NVSky\lib`'s vendored `atproto`/`pydantic` versions to "fix" this at the
  library level; the payload-level workaround is what's in place now.
  **Follow-up, not urgent:** the resulting function grew from ~2K to ~10K+
  characters because of how involved the workaround is -- Claude should read
  the actual current source next session and see whether it can be
  simplified/shortened without breaking the fix, per the user's request.

## Deliberately deferred (backlog, not blocking)

- **Embed-type filtering in Explore search** (image/video/link, like
  Twitter's advanced search) — confirmed no such param exists in
  `searchPosts`; would need to be a client-side post-hoc filter on already-
  fetched results. User explicitly said skip it, keyword search (e.g. typing
  domain names or obvious media-related terms) is good enough.
- **Hashtag search** — no dedicated UI needed; typing `#tag` directly into
  the existing Posts search box already works via the normal search API.

## Suggested order for next session

1. Fix `ExploreWindow`'s `_markSelectedRead` crash — get the real current
   class source first, don't guess.
2. While looking at `add_feed_to_saved`'s now-working ChatGPT-provided fix,
   see if it can be shortened/simplified (grew from ~2K to 10K+ characters)
   without breaking it — user's explicit ask, not urgent.
3. Test `FeedPreviewTabWindow`'s "Add to my feeds" button and
   `SUPPORTS_FOCUS_NEXT_UNREAD` (jump-to-unread) — both added but never
   reached testing before the `ExploreWindow` crash blocked further use of
   the tab.
4. Audit "remember where I came from on close" (`origin_key` /
   `TAB_TEMP_TYPE` jump-back-on-close) across every other pop-out tab in the
   app, not just Explore's — per user's explicit request last session.
5. **Main goal this next session: Settings > Feed manager.** List/reorder/
   remove subscribed feeds (read+write via `savedFeedsPrefV2` in
   `actor.get_preferences`/`put_preferences` -- reuse whatever payload
   pattern `add_feed_to_saved` ends up using once simplified), pin/unpin
   feeds per whatever the SDK actually exposes for that, browse+add new
   feeds by reusing Explore's Feeds search. Then wire the result into a real
   Home tab filter dropdown so a saved/pinned feed can actually be viewed
   from Home, not just added to the list -- this is the part that makes the
   whole feed-manager feature actually usable end to end, not just a list
   you can edit but never see reflected anywhere.
