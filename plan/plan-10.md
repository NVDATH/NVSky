# plan-10.md — NVSky handoff

Session goal: complete Chat tab group-chat support before moving to any other tab.
Core group chat (create/display/manage/lock/leave) is DONE and tested working.
This doc covers what's still open going into the next session.

## Confirmed working (tested by user this session)

- Group creation via New chat dialog (multi-recipient check-list, auto-detects
  group vs 1:1 from recipient count or an explicit group name)
- Group display: name, member list, `(group)`/`(locked)`/`(request)` tags in the
  conversation tree
- Manage members dialog: check-list add/remove, optimistic UI (removes/adds
  render immediately, roll back on failure)
- Lock / Unlock (`chat.bsky.convo.lockConvo`/`unlockConvo`) — confirmed working,
  reads `locked` correctly from DB (`convo.kind.lock_status`)
- Leave conversation — confirmed working for both 1:1 and group, including as
  the group's own owner (works once locked; `OwnerCannotLeave` from the server
  is caught and turned into a clear message telling the user to lock first)
- Context menu on the conversation tree (Leave/Lock/Manage members) — confirmed
  correct as designed: shown unconditionally for every group rather than gated
  on admin/owner status, since owner-vs-member makes no practical difference to
  which menu items are USABLE (leave still works either way, just requires lock
  first for the owner)
- `is_admin` — role now readable via `member.kind.role` (confirmed via a real
  debug_dump: `"owner"`) but not currently used for any UI decision. Available
  in `convos.is_admin` if a future feature needs it.
- Account switching / fresh re-login now correctly refreshes an already-open
  MainWindow (`GlobalPlugin.rebuild_main_window_tabs()`, wired from
  `AccountsPanel.onAdd`/`onSetActive`) — was previously silently stuck because
  `_onAccountChanged()` was never called after `onAdd`, and even when called,
  nothing downstream ever reached MainWindow's tabs
- `chat_supported` capability probe added at login (`client.check_chat_supported`
  — LOW CONFIDENCE, no documented capability endpoint exists, this just tries
  `listConvos` and treats any failure as unsupported). Chat tab is skipped
  entirely when false. **Not yet tested against a real unsupported account.**
- `remove_account` now also cleans up `convos`/`messages`/`convo_members` (were
  never covered by a FOREIGN KEY CASCADE). **Not yet tested** (blocked on the
  login bug being fixed first, per user).
- DB connection cleanup on `GlobalPlugin.terminate()` (WAL checkpoint + close
  current-thread connection) — partial fix for the "can't copy config to
  portable NVDA" issue Gemini flagged; does NOT close background-thread
  connections (can't, safely — see `db.close_all_connections`'s docstring).
  Disabling the add-on first remains the fallback for that specific case.

## Fixed this session, not yet re-tested by user

- `_onCheckAllConvosDone` (Shift+F5 in Chat tab) now also calls
  `_reloadConvoListIfAny()` → `ChatWindow._loadFromCache()` — previously synced
  data to DB correctly but never refreshed the conversation tree itself
- `ChatWindow._loadFromCache()` now restores the PREVIOUSLY selected
  conversation instead of always jumping back to the first one on every reload
  — suspected (not confirmed) contributor to both the garbled chat status-bar
  text and Ctrl+F5's "only says the first/last tab name" report
- New chat dialog now announces "Chat started." on success (was silent before)
- `onSend` returns focus to the compose box after sending (guess at the
  "status bar becomes 'Send'" report — turned out to be the wrong theory, see
  below; this focus-return is still a reasonable UX fix regardless)

## Known bugs, blocked on source not yet provided

Explicitly not guessing further at these — asked for source, waiting on it:

- **Chat tab status bar shows garbled, STUCK text** (not a speech race — the
  displayed value itself is wrong, same garbled text regardless of which
  conversation is selected). Reported text: "Message: locked -- no new
  messages [un]read 5 conversations can be sent." The leading "Message:" is
  suspicious — that's `composeLabel`'s original default text, which shouldn't
  have anything to do with the status bar at all unless something is
  conflating the two widgets. Needs: `ChatWindow.__init__`'s statusBar/
  composeLabel construction, and the current live `_updateActionArea`.
- **Mark-as-read on focus + Space key not working in Chat tab.** Reading into
  the newest (already-focused-by-default) message on open doesn't count as
  read; Space does nothing. Needs: Chat tab's `WXK_SPACE` handler and whatever
  "mark read on focus" hook exists (`_afterMessageRead` or similar).
- **Space doesn't move unread count in Lists tab either** (separate from the
  Chat tab report above, same symptom). Needs: `ListsWindow`/`FeedListMixin`'s
  Space handler.

## Deliberately deferred (backlog, not blocking)

- **Join links** (paste a bsky.app group invite URL, validate, show group
  name, join) — Stage 5, explicitly parked all session. Real
  `chat.bsky.group.*` behavior has repeatedly differed from what the lexicon
  docs implied (nested `kind` objects, no docs for member role, etc.) — start
  this in its own session with debug_dump available from the start rather than
  guessing through several rounds like this session did for group name/kind/role.
- **Join-request approval UI** (admin side: someone requests to join a group)
  — same reason, tied to join links, never tested since no join-link flow
  exists yet to generate a request.
- **Background update system** — Lock/Unlock currently does a foreground
  refresh right after the action instead of updating quietly in the
  background. Flagged to fix once a real background-sync system exists,
  rather than one-off per action.
- **React optimistic UI** — message reactions still wait on the server
  round-trip; every other group/member action was made optimistic this
  session, reactions weren't gotten to.
- **Cross-tab live sync** — sending a message from ChatWindow doesn't
  immediately show up in an already-open ConvoTabWindow for the same
  conversation (and vice versa) — each only refreshes on its own trigger
  (F5/Ctrl+F5/reopen). No pub-sub between panels showing the same convoId
  exists yet. Real architecture item, not a quick patch.
- **What happens when a group is down to one member / the last member leaves**
  — unknown, never tested, nothing in the lexicon docs found so far. User may
  test this directly (reversible — worst case the conversation just
  disappears) and report back with real behavior if curious.
- **Edit group name** — no confirmed endpoint exists in the lexicon research
  done this session (createGroup/addMembers/removeMembers/join-link endpoints
  were found; no updateGroup/rename equivalent). Not implemented.
- **Delete group entirely** — no such endpoint found either; lock + leave
  appears to be the closest functional equivalent by design.

## Debug tooling added this session

`client.debug_dump(obj, label)` — dumps any SDK object (pydantic models,
nested objects, dicts, lists) to a JSON file under
`globalPlugins/NVSky/debug_dumps/`, instead of guessing field names one
`log.info` round at a time. This directly resolved the group-name/`kind`/role
confusion this session after several wrong guesses — **use this first** for
any new unverified SDK surface (join links, background sync, etc.) rather
than repeating the log.info-guessing pattern.

## Suggested order for next session

1. Fix the 3 blocked-on-source bugs above once source is provided (status bar,
   Space/mark-as-read in Chat, Space/mark-as-read in Lists)
2. User-test: `chat_supported` false case, `remove_account` cleanup, last-member-leaves-group behavior
3. Then: either finish remaining Chat backlog (react optimistic, background
   lock refresh, cross-tab live sync) or move straight to join links as its
   own focused session — user's call
4. Other tabs (Explore/Feeds, or whatever's next) only after the above is
   actually closed out — explicit priority this session
