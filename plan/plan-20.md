# NVSky — plan-20.md

Continuation from plan-19.md. Scope: NVSky was developed and tested against
`atproto` SDK **0.0.69**, but PyPI's latest is **0.0.72** as of the first
build. `requirements.txt` was left unpinned, so the first CI build silently
pulled 0.0.72 — confirmed by two new top-level folders appearing in the
built `lib/` that were never vendored during development: `atproto_jetstream`
and `atproto_subscription`.

**Immediate stopgap already done (not part of this session's work):**
`requirements.txt` now pins `atproto==0.0.69` so the build stays on the
version the current code actually targets, until this migration is done
deliberately.

This file exists so a fresh chat can pick up the actual code-level audit/
migration work without re-deriving the changelog analysis from scratch.

---

## 1. What changed between 0.0.69 and 0.0.72 (from the SDK's own release notes)

### v0.0.70
- Cryptography dependency ceiling raised to allow `cryptography` up to v50.
- New `atproto_jetstream` package (Jetstream v2 live-tail support, dict-zstd
  compression, `snapshot()`/`replay()`). NVSky doesn't use Jetstream or
  Firehose anywhere in the current codebase — purely additive, no impact
  expected, but explains the new top-level folder.
- Firehose reliability fixes (fatal network errors, stalled backoff, hanging
  `stop()`). Not used by NVSky.
- TID validation fix (TIDs starting with a-j). Not directly touched by
  NVSky's own code, but atproto internals may rely on this.

### v0.0.71
- Jetstream/Firehose performance work. Not used by NVSky.
- **`AtUri.http` deprecated in favor of `AtUri.href`.** NVSky uses `AtUri`
  in a few places in `client.py` (`set_threadgate`, `get_threadgate_settings`,
  postgate helpers) but only ever calls `.from_str(...).rkey` — a grep
  turned up no use of `.http` anywhere, so this is likely a non-issue, but
  confirm with a fresh grep before bumping the pin.
- **Fixed JSON serialization of blobs decoded from DAG-CBOR.** NVSky's own
  blob handling (`_upload_blob_dict` in `client.py`) builds blob dicts
  manually from `upload.blob.ref.link`/`mime_type`/`size`, not from a
  DAG-CBOR decode path directly — likely unaffected, but image/video
  attachment upload and "View embed" should get a regression pass after
  bumping, since this is exactly the kind of thing that could silently
  change blob shape.
- Fixed ignored constraints of array items in codegen (codegen-only, not
  used by NVSky since it doesn't generate custom lexicons).
- Fixed unions failing the whole response on an unknown `$type`. Worth
  noting: NVSky's client.py has several LOW CONFIDENCE raw-JSON-bypass
  code paths written specifically to work around pydantic discriminated-
  union parsing bugs on 0.0.69 (`_sync_convo_messages`, `_fetch_thread_json`,
  `get_convo_log`, `create_group`, etc. — search client.py for "discriminator"
  and "union" in comments). **This fix might mean some of those bypasses
  are no longer necessary** — worth testing the typed SDK path again on
  the newer version rather than assuming the bypass is still required
  forever.

### v0.0.72
- **"Surface the server error message in request exceptions instead of the
  raw response"** — this is the change most likely to affect NVSky
  directly. `client.py`'s `_extract_error_message(e)` reads
  `e.content`/`e.response.content.message` to build user-facing error
  text across dozens of call sites. If the exception's error-message shape
  changed in 0.0.72, this helper (and everywhere that calls it) needs a
  fresh look — **highest priority item for the migration**.
- **New `RateLimitExceededError`** exception, carrying `limit`,
  `remaining`, `reset_at`, and `retry_after`. NVSky currently has no
  specific handling for rate limiting anywhere (every network call just
  catches generic `Exception`). Optional enhancement: catch this
  specifically somewhere central (e.g. in the worker-thread error paths)
  to show a friendlier "rate limited, try again in Xs" message using
  `retry_after`, instead of whatever generic error text falls out of
  `_extract_error_message`.
- Fixed `login()` failing on a PDS that does not serve `app.bsky`. Pure
  bugfix, no NVSky code change needed, just confirm login still works
  post-bump.
- `atp gen custom` custom lexicon codegen, docs site rework, CLI-only
  fixes (`atproto_cli`), Ruff/mypy/docs tooling changes — none of this
  touches runtime behavior NVSky depends on.
- New `atproto_subscription` package, extracted out of `atproto_firehose`'s
  runtime. **Needs a concrete check**: with `requirements.txt` using
  `--no-deps`, confirm whether `atproto_jetstream`/`atproto_subscription`
  ship bundled inside the single `atproto` PyPI distribution (as
  `atproto_client`/`atproto_core`/etc. already do) or whether they're now
  separate installable distributions that `pip install atproto --no-deps`
  won't pull in on their own. If separate, decide whether NVSky needs them
  at all (currently: no) before deciding whether to add them to
  `requirements.txt`.

---

## 2. Suggested migration checklist for the next session

1. Bump the `atproto==0.0.69` pin in `requirements.txt` to `0.0.72` in an
   isolated test, rebuild, and confirm which top-level folders actually
   land in `lib/` (checks the `atproto_jetstream`/`atproto_subscription`
   bundling question above).
2. Grep `client.py` for `.http` on any `AtUri` usage — switch to `.href`
   if found (expected: none).
3. Re-read `_extract_error_message()` and test it against a few real error
   conditions (bad login, a failed post, a rate-limited request) to see if
   the message shape actually changed under 0.0.72 — this is the one item
   most likely to need a real code change.
4. Decide whether to add specific `RateLimitExceededError` handling
   (optional polish, not required for correctness).
5. Regression-test image/video attachment upload and "View embed" given
   the DAG-CBOR blob JSON serialization fix in 0.0.71.
6. Re-test login on at least one account to confirm no regression from the
   0.0.72 login fix.
7. Spot-check the existing LOW CONFIDENCE raw-JSON-bypass paths in
   `client.py` (chat sync, thread fetch, getLog, group creation, join
   links, activity subscriptions) against the typed SDK response on
   0.0.72 — some may no longer need the bypass, though this is a "nice to
   simplify," not a correctness requirement; don't remove a bypass without
   confirming the typed path actually works now.
8. Once the above is confirmed clean, bump the `requirements.txt` pin for
   real and update this plan file (or close it out) accordingly.
