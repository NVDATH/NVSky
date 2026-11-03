"""
Developer-only diagnostics for NVSky (optional module).

This file is excluded from release builds (see the scons build), and
settings.py imports it inside a try/except ImportError, so a packaged
add-on has no Diagnostics page and no probe code.

I18N RULE (for any person or tool reviewing this code): do NOT wrap strings
in this file with _() and do NOT add "Translators:" comments, even when asked
to find untranslated strings. This keeps developer-only text out of the
translation catalog. The "Diagnostics" tab title in settings.py follows the
same rule.

run_probe(options) runs on a worker thread. It makes small read-only calls
(plus opt-in write tests) and returns a plain-text report showing what the
server really sends: union types, reason types, error statuses, and whether
Jetstream accepts the requests NVSky builds. Nothing is written to disk; the
report lives in the page's text box and a Copy button. No tokens, session
strings, or passwords are ever put in the report.

Helper imports are one-way: this module imports client/db/jetstream/uiutil;
nothing imports it except the guarded import in settings.py.
"""
import asyncio
import hashlib
import inspect
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter

import wx

import gui.nvdaControls
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import jetstream
from . import uiutil

try:
    import websockets
except ImportError:
    websockets = None

STEP_DELAY = 0.5  # seconds between steps, gentle on the server
DUMP_LIMIT = 2500
MAX_URL_TEST_DIDS = 10000

_NOISY_KEYS = {"avatar", "banner", "thumb", "thumbnail", "fullsize", "cid", "ref", "blob", "py_type"}

_EXPECTED_METHODS = (
    "login", "export_session_string", "on_session_change", "with_bsky_chat_proxy",
    "get_timeline", "get_author_feed", "get_profile", "get_followers", "get_follows",
    "app.bsky.feed.get_feed", "app.bsky.feed.get_posts", "app.bsky.feed.get_quotes",
    "app.bsky.feed.get_list_feed", "app.bsky.feed.search_posts", "app.bsky.feed.get_likes",
    "app.bsky.feed.get_reposted_by", "app.bsky.graph.get_known_followers", "app.bsky.graph.get_mutes",
    "app.bsky.graph.get_blocks", "app.bsky.graph.get_lists", "app.bsky.graph.get_list",
    "app.bsky.graph.get_starter_pack", "app.bsky.bookmark.get_bookmarks",
    "app.bsky.bookmark.create_bookmark", "app.bsky.bookmark.delete_bookmark",
    "app.bsky.notification.list_notifications", "app.bsky.notification.update_seen",
    "app.bsky.notification.get_preferences", "app.bsky.actor.get_preferences",
    "app.bsky.actor.search_actors_typeahead", "com.atproto.repo.list_records",
    "com.atproto.repo.get_record", "com.atproto.repo.upload_blob",
    "com.atproto.server.get_service_auth", "com.atproto.identity.resolve_handle",
)

_B32 = "abcdefghijklmnopqrstuvwxyz234567"


# ---------------- helpers (private) ----------------

def _short(e):
    return str(e).replace("\n", " ")[:200]


def _status(e):
    response = getattr(e, "response", None)
    return getattr(response, "status_code", None) or getattr(e, "status_code", None)


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in str(text).splitlines())


def _plain(obj, depth=0):
    """Converts SDK models, DotDicts and namespaces into plain JSON-able data."""
    if depth > 6:
        return repr(obj)[:60]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_plain(i, depth + 1) for i in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return _plain(dump(mode="json", by_alias=True, exclude_none=True), depth + 1)
        except Exception:
            pass
    keys = getattr(obj, "keys", None)
    if callable(keys):  # DotDict: not a dict subclass, but dict-like
        try:
            return {str(k): _plain(obj[k], depth + 1) for k in keys()}
        except Exception:
            pass
    data = getattr(obj, "data", None)
    if isinstance(data, dict):
        return _plain(data, depth + 1)
    if hasattr(obj, "__dict__"):
        return {k: _plain(v, depth + 1) for k, v in vars(obj).items() if not k.startswith("_")}
    return repr(obj)[:60]


def _trim(obj, depth=0):
    """Drops noisy keys, cuts strings to 100 chars, lists to 3 items, depth to 4."""
    if isinstance(obj, dict):
        if depth >= 4:
            return "{...}"
        return {k: _trim(v, depth + 1) for k, v in obj.items() if k not in _NOISY_KEYS}
    if isinstance(obj, list):
        if depth >= 4:
            return "[...]"
        items = [_trim(i, depth + 1) for i in obj[:3]]
        if len(obj) > 3:
            items.append(f"... +{len(obj) - 3} more")
        return items
    if isinstance(obj, str) and len(obj) > 100:
        return obj[:100] + "..."
    return obj


def _dump(obj, limit=DUMP_LIMIT):
    try:
        text = json.dumps(_trim(_plain(obj)), indent=1, ensure_ascii=False, default=repr)
    except Exception as e:
        text = f"<unserializable: {e}>"
    return text if len(text) <= limit else text[:limit] + "\n...[cut]"


def _type_name(obj):
    return "none" if obj is None else type(obj).__name__


def _lib_version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        pass
    try:
        module = __import__(name)
        return str(getattr(module, "__version__", None) or _recorded_version(name) or "unknown")
    except Exception:
        return "not importable"


def _fake_dids(count):
    return ["did:plc:" + "".join(random.choices(_B32, k=24)) for _i in range(count)]


class _Detail(str):
    """A step result: the str value is the short summary, .detail the full text shown only in verbose mode."""

    def __new__(cls, summary, detail=""):
        obj = super().__new__(cls, summary)
        obj.detail = detail
        return obj


class _Report:
    def __init__(self, options):
        self.options = options
        self.verbose = bool(options.get("verbose"))
        self.quiet = 0
        self.lines = []
        self.ctx = {}
        self._started = False

    def add(self, text=""):
        self.lines.append(text)

    def step(self, title, func, requires=None, confirmed=False):
        if requires is not None and requires not in self.ctx:
            if not (confirmed and not self.verbose):
                self.lines.append(f"[SKIP] {title} (needs {requires})")
            return
        if self._started:
            time.sleep(STEP_DELAY)
        self._started = True
        began = time.monotonic()
        try:
            info = func(self)
            status = "[ OK ]"
        except Exception as e:
            code = _status(e)
            info = f"{type(e).__name__}: {_short(e)}" + (f" (HTTP {code})" if code else "")
            status = "[FAIL]"
        if status == "[ OK ]" and confirmed and not self.verbose:
            self.quiet += 1
            return
        line = f"{status} {title} ({time.monotonic() - began:.1f}s)"
        if info:
            if self.verbose and getattr(info, "detail", ""):
                info = info + "\n" + info.detail
            info = str(info)
            line += (":\n" + _indent(info)) if "\n" in info else (": " + info)
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


# ---------------- websocket helpers ----------------

async def _ws_session(url, subprotocols, send, maxFrames, seconds):
    result = {"handshake": False, "frames": [], "error": None, "status": None, "subprotocol": None}
    try:
        kwargs = {"open_timeout": 10, "close_timeout": 2, "max_size": 4 * 1024 * 1024}
        if subprotocols:
            kwargs["subprotocols"] = subprotocols
        async with websockets.connect(url, **kwargs) as ws:
            result["handshake"] = True
            result["subprotocol"] = getattr(ws, "subprotocol", None)
            if send is not None:
                await ws.send(send)
            deadline = time.monotonic() + seconds
            while len(result["frames"]) < maxFrames:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                result["frames"].append(raw)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {_short(e)}"
        result["status"] = _status(e)
    return result


def _run_ws(url, subprotocols=None, send=None, maxFrames=1, seconds=3.0):
    if websockets is None:
        return {"handshake": False, "frames": [], "error": "websockets library unavailable",
                "status": None, "subprotocol": None}
    return asyncio.run(_ws_session(url, subprotocols, send, maxFrames, seconds))


def _verdict(res):
    if res["error"]:
        where = "after connecting" if res["handshake"] else "at handshake"
        code = f" (HTTP {res['status']})" if res["status"] else ""
        return f"FAILED {where}: {res['error']}{code}"
    return f"accepted ({len(res['frames'])} frame(s))"


# ---------------- probe steps ----------------

def _probe_environment(rep):
    lines = [f"python {sys.version.split()[0]} on {platform.platform()}"]
    try:
        import buildVersion
        lines.append(f"NVDA {buildVersion.version}")
    except Exception as e:
        lines.append(f"NVDA version unknown: {_short(e)}")
    try:
        import addonHandler
        lines.append(f"NVSky {addonHandler.getCodeAddon().manifest['version']}")
    except Exception:
        pass
    for name in ("atproto", "pydantic", "httpx", "websockets", "libipld", "cryptography"):
        lines.append(f"{name} {_lib_version(name)}")
    return "\n".join(lines)


def _probe_methods(rep):
    from atproto import Client
    probe = Client()
    missing = []
    for path in _EXPECTED_METHODS:
        target = probe
        for part in path.split("."):
            target = getattr(target, part, None)
            if target is None:
                break
        if not callable(target):
            missing.append(path)
    return "none missing" if not missing else "MISSING: " + ", ".join(missing)


def _probe_login(rep, account):
    atp = client.get_client_for_active_account()
    rep.ctx["atp"] = atp
    rep.ctx["did"] = atp.me.did
    return f"handle={account['handle']} did={atp.me.did}"


def _probe_timeline(rep):
    atp = rep.ctx["atp"]
    resp = atp.get_timeline(limit=5)
    reasons = Counter(_type_name(item.reason) for item in resp.feed)
    embeds = Counter()
    labelled = 0
    for item in resp.feed:
        try:
            info = client._extract_embed_info(item.post)
            embeds[(info or {}).get("$type") or "none"] += 1
        except Exception as e:
            embeds[f"extract failed: {_short(e)}"] += 1
        if getattr(item.post, "labels", None):
            labelled += 1
    if resp.feed:
        post = resp.feed[0].post
        viewer = getattr(post, "viewer", None)
        rep.ctx["sample_uri"] = post.uri
        rep.ctx["sample_cid"] = post.cid
        rep.ctx["sample_liked"] = bool(getattr(viewer, "like", None))
        rep.ctx["sample_saved"] = bool(getattr(viewer, "bookmarked", False))
    lines = [
        f"{len(resp.feed)} items; reasons={dict(reasons)}; embeds={dict(embeds)}; "
        f"labelled posts={labelled}; cursor={'yes' if resp.cursor else 'no'}",
    ]
    summary = lines[0]
    detail = ""
    if resp.feed:
        detail = "first item:\n" + _dump(resp.feed[0])
    return _Detail(summary, detail)


def _probe_embeds(rep):
    atp = rep.ctx["atp"]
    samples = {}
    cursor = None
    scanned = 0
    for _page in range(6):
        resp = atp.get_timeline(limit=50, cursor=cursor)
        for item in resp.feed:
            scanned += 1
            embed = getattr(item.post, "embed", None)
            if embed is not None:
                kind = client._embed_type(embed) or type(embed).__name__
                if kind not in samples:
                    samples[kind] = item.post
        cursor = resp.cursor
        if not cursor or len(samples) >= 6:
            break
        time.sleep(STEP_DELAY)
    problems = []
    detail = []
    for name, post in samples.items():
        embed = post.embed
        try:
            info = client._extract_embed_info(post) or {}
        except Exception as e:
            problems.append(f"{name}: _extract_embed_info raised {type(e).__name__}: {_short(e)}")
            continue
        got = info.get("$type")
        # A media embed must yield a URL (or quote text); an empty info besides $type is a miss.
        useful = [k for k in info if k != "$type" and info.get(k)]
        if got != name or not useful:
            problems.append(f"{name}: extracted $type={got!r}, fields={useful or 'none'}")
        detail.append(f"--- {name}: {json.dumps(_trim(info), ensure_ascii=False, default=repr)}")
        detail.append(_indent(_dump(embed, 700), "    "))
    summary = f"scanned {scanned} posts; {len(samples)} embed type(s): {', '.join(sorted(samples)) or 'none'}"
    if problems:
        summary += "\nPROBLEMS:\n" + "\n".join("  " + p for p in problems)
    else:
        summary += "; every type extracted fields"
    return _Detail(summary, "\n".join(detail))


def _probe_author_feed(rep):
    atp = rep.ctx["atp"]
    resp = atp.get_author_feed(actor=rep.ctx["did"], limit=10)
    reasons = Counter(_type_name(item.reason) for item in resp.feed)
    shapes = Counter()
    for item in resp.feed:
        if item.reason is not None:
            plain = _plain(item.reason)
            shapes[",".join(sorted(plain)) if isinstance(plain, dict) else repr(plain)[:40]] += 1
    lines = [f"{len(resp.feed)} items; reasons={dict(reasons)}; reason key sets={dict(shapes)}"]
    extra = []
    withReason = next((item for item in resp.feed if item.reason is not None), None)
    if withReason is not None:
        extra.append("first item with a reason (repost or pin):")
        extra.append(_dump(withReason.reason))
    # Pins only come back when includePins is set (default false); NVSky does not ask for them.
    withPins = atp.app.bsky.feed.get_author_feed(params={"actor": rep.ctx["did"], "limit": 10, "includePins": True})
    pinTypes = Counter()
    pinned = None
    for item in withPins.feed:
        plain = _plain(item.reason) if item.reason is not None else None
        reasonType = plain.get("$type", "?") if isinstance(plain, dict) else "none"
        pinTypes[reasonType] += 1
        if pinned is None and "reasonPin" in reasonType:
            pinned = item
    lines.append(f"with includePins=true: {len(withPins.feed)} items; reason types={dict(pinTypes)}")
    if pinned is not None:
        extra.append("pinned item reason:")
        extra.append(_dump(pinned.reason))
    return _Detail("\n".join(lines), "\n".join(extra))


def _probe_notifications(rep):
    atp = rep.ctx["atp"]
    resp = atp.app.bsky.notification.list_notifications(params={"limit": 10})
    reasons = Counter(n.reason for n in resp.notifications)
    summary = f"{len(resp.notifications)} notifications; reasons={dict(reasons)}; cursor={'yes' if resp.cursor else 'no'}"
    extra = []
    if resp.notifications:
        extra.append(_dump(resp.notifications[0], 1200))
    viaRepost = next((n for n in resp.notifications if str(n.reason).endswith("via-repost")), None)
    if viaRepost is not None:
        extra.append(f"first {viaRepost.reason} notification (reason_subject={viaRepost.reason_subject}):")
        extra.append(_dump(viaRepost.record, 1200))
    return _Detail(summary, "\n".join(extra))


def _probe_bookmarks(rep):
    atp = rep.ctx["atp"]
    resp = atp.app.bsky.bookmark.get_bookmarks(params={"limit": 3})
    lines = [f"{len(resp.bookmarks)} bookmark(s); cursor={'yes' if resp.cursor else 'no'}"]
    if resp.bookmarks:
        first = resp.bookmarks[0]
        lines.append(f"item type={_type_name(getattr(first, 'item', None))}, subject type={_type_name(getattr(first, 'subject', None))}")
    return "\n".join(lines)


def _probe_likes(rep):
    atp = rep.ctx["atp"]
    resp = atp.com.atproto.repo.list_records(
        params={"repo": rep.ctx["did"], "collection": "app.bsky.feed.like", "limit": 3}
    )
    lines = [f"{len(resp.records)} record(s); cursor={'yes' if resp.cursor else 'no'}"]
    if resp.records:
        lines.append(f"value type={_type_name(resp.records[0].value)}")
        lines.append(_dump(resp.records[0].value, 800))
    return "\n".join(lines)


def _probe_preferences(rep):
    atp = rep.ctx["atp"]
    prefs = client._get_cleaned_preferences(atp)
    types = [p.get("$type", "?") for p in prefs]
    notif = client.get_notification_prefs(atp)
    return f"{len(prefs)} preference entries: {', '.join(types)}\nnotification categories: {', '.join(sorted(notif))}"


def _probe_lists(rep):
    atp = rep.ctx["atp"]
    lists = client.get_lists(atp, rep.ctx["did"])
    purposes = Counter(l["purpose"].rsplit("#", 1)[-1] for l in lists)
    return f"{len(lists)} list(s); purposes={dict(purposes)}"


def _probe_follows(rep):
    atp = rep.ctx["atp"]
    follows = client.get_follows(atp, rep.ctx["did"])
    rep.ctx["follow_dids"] = [f["did"] for f in follows]
    return f"{len(follows)} followed account(s)"


def _probe_thread(rep):
    atp = rep.ctx["atp"]
    raw = client._fetch_thread_json(rep.ctx["sample_uri"], depth=2, parent_height=2, client=atp)
    thread = raw.get("thread", raw)
    lines = [f"top-level $type={thread.get('$type')}, keys={sorted(thread)}"]
    # A post that does not exist: shows what get_thread must cope with.
    badUri = f"at://{rep.ctx['did']}/app.bsky.feed.post/3zzzzzzzzzzzz"
    try:
        posts, _index = client.get_thread(atp, badUri)
        lines.append(f"missing post: get_thread returned {len(posts)} post(s)")
    except Exception as e:
        lines.append(f"missing post: get_thread raised {type(e).__name__}: {_short(e)}")
    return "\n".join(lines)


def _probe_chat(rep):
    atp = rep.ctx["atp"]
    try:
        resp = client.get_chat_client(atp).chat.bsky.convo.list_convos(params={"limit": 1})
        text = f"list_convos ok, {len(resp.convos)} convo(s)"
    except Exception as e:
        text = f"list_convos failed: {type(e).__name__} status={_status(e)}: {_short(e)}"
    return text + f"\ncheck_chat_supported -> {client.check_chat_supported(atp)}"


def _probe_chat_log(rep):
    atp = rep.ctx["atp"]
    logs, cursor = client.get_convo_log(atp, cursor=None)
    return f"{len(logs)} log entries; cursor={'present, length ' + str(len(cursor)) if cursor else 'missing'}"


def _probe_video_limits(rep):
    return _dump(client.get_video_upload_limits(rep.ctx["atp"]), 600)


def _probe_write(rep):
    atp = rep.ctx["atp"]
    uri, cid = rep.ctx["sample_uri"], rep.ctx["sample_cid"]
    lines = []
    if rep.ctx["sample_liked"]:
        lines.append("like test skipped: the sample post is already liked")
    else:
        likeUri = None
        try:
            likeUri = client.like_post(atp, uri, cid)
            lines.append("like ok")
        finally:
            if likeUri:
                try:
                    client.unlike_post(atp, likeUri)
                    lines.append("unlike ok")
                except Exception as e:
                    lines.append(f"UNLIKE FAILED, remove the like by hand on {uri}: {_short(e)}")
    if rep.ctx["sample_saved"]:
        lines.append("save test skipped: the sample post is already saved")
    else:
        saved = False
        try:
            client.bookmark_post(atp, uri, cid)
            saved = True
            lines.append("save ok")
        finally:
            if saved:
                try:
                    client.unbookmark_post(atp, uri)
                    lines.append("unsave ok")
                except Exception as e:
                    lines.append(f"UNSAVE FAILED, remove the saved post by hand: {uri}: {_short(e)}")
    return "\n".join(lines)


def _probe_typed_calls(rep):
    from pydantic.fields import FieldInfo
    from atproto import models
    atp = rep.ctx["atp"]
    did = rep.ctx["did"]
    ok, bad = [], []

    def attempt(label, func):
        try:
            note = func()
            ok.append(label + (f" ({note})" if note else ""))
        except Exception as e:
            bad.append(f"{label}: {type(e).__name__}: {_short(e)}")
        time.sleep(STEP_DELAY)

    def typedPrefs():
        resp = atp.app.bsky.actor.get_preferences()
        broken = sum(1 for p in resp.preferences if isinstance(getattr(p, "py_type", None), FieldInfo))
        if broken:
            raise RuntimeError(f"{broken} of {len(resp.preferences)} preferences still have a FieldInfo py_type")
        return f"{len(resp.preferences)} preferences"

    dm = client.get_chat_client(atp).chat.bsky.convo
    convos = dm.list_convos(params={"limit": 1}).convos

    def typedMessages():
        if not convos:
            raise RuntimeError("skipped, no conversation")
        return f"{len(dm.get_messages(params={'convo_id': convos[0].id, 'limit': 5}).messages)} messages"

    def typedThread():
        resp = atp.app.bsky.feed.get_post_thread(params={"uri": rep.ctx["sample_uri"], "depth": 2, "parent_height": 2})
        return type(resp.thread).__name__

    def typedSubscriptions():
        typed = len(atp.app.bsky.notification.list_activity_subscriptions(params={"limit": 50}).subscriptions)
        raw = len(client.get_activity_subscriptions(atp))
        if typed != raw:
            raise RuntimeError(f"typed returned {typed}, raw returned {raw}")
        return f"{typed} entries"

    def dumps(factory):
        def run():
            factory().model_dump_json(exclude_none=True, by_alias=True)
        return run

    attempt("read: get_preferences", typedPrefs)
    attempt("read: chat get_messages", typedMessages)
    attempt("read: chat get_log", lambda: f"{len(dm.get_log(params={'limit': 5}).logs)} entries")
    attempt("read: get_post_thread", typedThread)
    attempt("read: list_activity_subscriptions", typedSubscriptions)
    attempt("write model: createRecord", dumps(lambda: models.ComAtprotoRepoCreateRecord.Data(
        repo=did, collection="app.bsky.feed.like",
        record={"$type": "app.bsky.feed.like", "subject": {"uri": rep.ctx["sample_uri"], "cid": rep.ctx["sample_cid"]},
                "createdAt": "2026-01-01T00:00:00.000Z"},
    )))
    attempt("write model: chat sendMessage", dumps(lambda: models.ChatBskyConvoSendMessage.Data(
        convo_id="x", message=models.ChatBskyConvoDefs.MessageInput(text="t"),
    )))
    attempt("write model: putActivitySubscription", dumps(lambda: models.AppBskyNotificationPutActivitySubscription.Data(
        subject=did, activity_subscription=models.AppBskyNotificationDefs.ActivitySubscription(post=True, reply=True),
    )))
    summary = f"{len(ok)} typed call(s) work, {len(bad)} fail"
    if bad:
        summary += "\nFAILED (bypass still needed):\n" + "\n".join("  " + b for b in bad)
    return _Detail(summary, "worked:\n" + "\n".join("  " + o for o in ok))


def _probe_soundpack(rep):
    from . import soundpack
    packs = soundpack.list_packs()
    if not packs:
        return "no sound pack folders"
    lines = []
    for pack in packs:
        keys = list(soundpack.EVENT_KEYS) + [soundpack.PROGRESS_KEY]
        missing = [k for k in keys if not os.path.isfile(os.path.join(soundpack.SOUND_PACK_DIR, pack, k + ".wav"))]
        lines.append(f"{pack}: " + (f"missing {', '.join(missing)}" if missing else "complete"))
    return "\n".join(lines)


def _probe_account_cycle(rep):
    from . import _next_account_id
    three = [{"id": 3}, {"id": 5}, {"id": 9}]
    cases = [
        ("next from the middle", _next_account_id(three, 5, 1), 9),
        ("next wraps to the first", _next_account_id(three, 9, 1), 3),
        ("previous wraps to the last", _next_account_id(three, 3, -1), 9),
        ("previous from the middle", _next_account_id(three, 5, -1), 3),
        ("unknown active starts at the first", _next_account_id(three, 77, 1), 5),
        ("one account gives nothing", _next_account_id([{"id": 3}], 3, 1), None),
        ("no account gives nothing", _next_account_id([], None, 1), None),
    ]
    failed = [f"{label}: got {got!r}, expected {want!r}" for label, got, want in cases if got != want]
    return "all %d cases passed" % len(cases) if not failed else "FAILED:\n" + "\n".join(failed)


def _probe_bg_selection(rep):
    import datetime
    import math
    from . import bgsync
    utc = datetime.timezone.utc
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=utc)
    ago = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat()
    enabled = {1: True, 2: False, 3: True}
    custom = {(3, "home"): 60}
    defaults = {"home": 5, "chat": 2}
    lasts = {(1, "home"): ago(6), (1, "chat"): ago(1), (2, "home"): ago(500), (3, "home"): ago(30), (3, "chat"): None}
    due = bgsync.collect_due(
        now, [{"id": 1}, {"id": 2}, {"id": 3}], lambda a: enabled[a],
        lambda a, c: custom.get((a, c), defaults.get(c, 0)), lambda a, c: lasts.get((a, c)),
    )
    naive = (now - datetime.timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    cases = [
        ("due items come most overdue first", due == [(120, 3, "chat"), (60, 1, "home")]),
        ("an account with checks off is skipped", all(a != 2 for _o, a, _c in due)),
        ("categories not yet due are left out", all((a, c) not in ((1, "chat"), (3, "home")) for _o, a, c in due)),
        ("grouping keeps the most overdue account first",
         list(bgsync.group_by_account(due).items()) == [(3, ["chat"]), (1, ["home"])]),
        ("a timestamp without a zone counts as UTC", bgsync.seconds_until_due(now, naive, 2) == 60),
        ("an unreadable timestamp counts as never run", bgsync.seconds_until_due(now, "garbage", 5) is None),
    ]
    failed = [label for label, passed in cases if not passed]

    def when(remaining):
        return "due" if remaining is None or remaining <= 0 else f"in {math.ceil(remaining / 60)} min"

    enabledOf, intervalOf, lastOf = bgsync.db_readers()
    liveNow = bgsync.utc_now()
    soonest, table = [], []
    for account in db.get_all_accounts():
        label = f"@{account['handle']}" + (" (active)" if account["is_active"] else "")
        if not enabledOf(account["id"]):
            soonest.append(f"{label}: checks off")
            table.append(f"{label}: checks off")
            continue
        entries = []
        for category in bgsync.SYNC_FUNCTIONS:
            minutes = intervalOf(account["id"], category)
            if minutes > 0:
                remaining = bgsync.seconds_until_due(liveNow, lastOf(account["id"], category), minutes)
                entries.append((remaining if remaining is not None else 0, category, minutes))
        entries.sort()
        table.append(f"{label}: " + "; ".join(f"{c} every {m} min, {when(r)}" for r, c, m in entries))
        soonest.append(f"{label}: next {entries[0][1]} {when(entries[0][0])}" if entries else f"{label}: nothing scheduled")
    summary = ("all %d cases passed" % len(cases)) if not failed else "FAILED:\n" + "\n".join(failed)
    return _Detail(summary + "\n" + "\n".join(soonest), "\n".join(table))


def _diff_paths(raw, typed, path="$", out=None):
    out = [] if out is None else out
    if len(out) >= 12:
        return out
    if isinstance(raw, dict) and isinstance(typed, dict):
        for key in sorted(set(raw) | set(typed)):
            if key not in typed:
                out.append(f"{path}.{key}: only in raw")
            elif key not in raw:
                if key != "$type":  # the typed models always add the type tag; harmless
                    out.append(f"{path}.{key}: only in typed")
            else:
                _diff_paths(raw[key], typed[key], f"{path}.{key}", out)
    elif isinstance(raw, list) and isinstance(typed, list):
        if len(raw) != len(typed):
            out.append(f"{path}: length raw {len(raw)}, typed {len(typed)}")
        else:
            for i, (a, b) in enumerate(zip(raw, typed)):
                _diff_paths(a, b, f"{path}[{i}]", out)
    elif raw != typed:
        out.append(f"{path}: raw {str(raw)[:40]!r}, typed {str(typed)[:40]!r}")
    return out


def _probe_bypass_equivalence(rep):
    from atproto import models
    atp = rep.ctx["atp"]
    lines = []

    def compare(label, getRaw, getTyped):
        try:
            raw, typed = getRaw(), getTyped()
        except Exception as e:
            lines.append(f"{label}: could not run ({type(e).__name__}: {_short(e)})")
            return
        size = len(raw) if hasattr(raw, "__len__") else 0
        diffs = _diff_paths(raw, typed)
        if not diffs:
            lines.append(f"{label}: identical ({size} item(s)){' - nothing to compare' if not size else ''}")
        else:
            lines.append(f"{label}: DIFFERENT")
            lines.extend("    " + d for d in diffs)
        time.sleep(STEP_DELAY)

    dump = lambda m: m.model_dump(mode="json", by_alias=True, exclude_none=True)
    compare(
        "preferences (cleaned vs plain typed dump)",
        lambda: client._get_cleaned_preferences(atp),
        lambda: [client._pref_to_dict(p) for p in atp.app.bsky.actor.get_preferences().preferences],
    )
    dm = client.get_chat_client(atp).chat.bsky.convo
    convos = dm.list_convos(params={"limit": 1}).convos
    if convos:
        convoId = convos[0].id
        params = models.ChatBskyConvoGetMessages.Params(convo_id=convoId, limit=20)
        compare(
            "chat getMessages",
            lambda: dm._client.invoke_query(
                "chat.bsky.convo.getMessages", params=params, output_encoding="application/json"
            ).content.get("messages", []),
            lambda: [dump(m) for m in dm.get_messages(params={"convo_id": convoId, "limit": 20}).messages],
        )
    else:
        lines.append("chat getMessages: no conversation to compare")
    uri = rep.ctx["sample_uri"]
    compare(
        "getPostThread (logged in)",
        lambda: client._fetch_thread_json(uri, depth=2, parent_height=2, client=atp).get("thread", {}),
        lambda: dump(atp.app.bsky.feed.get_post_thread(
            params={"uri": uri, "depth": 2, "parent_height": 2}
        ).thread),
    )
    from atproto_client.models.dot_dict import DotDict
    compare(
        "activity subscriptions",
        lambda: atp.app.bsky.notification._client.invoke_query(
            "app.bsky.notification.listActivitySubscriptions", params=DotDict({"limit": 50}),
            output_encoding="application/json",
        ).content.get("subscriptions", []),
        lambda: [dump(s) for s in atp.app.bsky.notification.list_activity_subscriptions(
            params={"limit": 50}
        ).subscriptions],
    )
    return "\n".join(lines)


def _probe_error_shapes(rep):
    from atproto import Client
    atp = rep.ctx["atp"]
    cases = [
        ("unknown profile", lambda: atp.get_profile("did:plc:" + "a" * 24)),
        ("unresolvable handle", lambda: atp.com.atproto.identity.resolve_handle(params={"handle": "nvsky-probe.invalid"})),
        ("malformed post uri", lambda: atp.app.bsky.feed.get_posts(params={"uris": ["at://nvsky-probe"]})),
        ("bad login", lambda: Client().login("nvsky-probe-nobody.bsky.social", "aaaa-bbbb-cccc-dddd")),
    ]
    lines = []
    detail = []
    for label, call in cases:
        time.sleep(STEP_DELAY)
        try:
            call()
            lines.append(f"{label}: no error raised")
        except Exception as e:
            content = getattr(e, "content", None)
            response = getattr(e, "response", None)
            lines.append(
                f"{label}: status={_status(e)} -> {client._extract_error_message(e)!r}"
            )
            detail.append(
                f"{label}: {type(e).__name__}, content={_type_name(content)}, "
                f"content.message={client._view_field(content, 'message')!r}, "
                f"response.content={_type_name(getattr(response, 'content', None))}, str(e)={_short(e)!r}"
            )
    return _Detail("\n".join(lines), "\n".join(detail))


def _db_check_post(uri, accountId):
    return {
        "uri": uri, "cid": "cid", "account_id": accountId, "author_did": "did:plc:selfcheckauthor",
        "text": "text", "created_at": "2026-01-01T00:00:00Z", "indexed_at": "2026-01-01T00:00:00Z",
        "like_count": 0, "repost_count": 0, "reply_count": 0, "reply_parent_uri": None,
        "reply_to_did": None, "reply_to_handle": None, "is_repost": 0, "reposted_by_did": None,
        "reposted_by_handle": None, "reposted_by_display_name": None, "embed_json": None,
        "facets_json": None, "quoted_text": None, "quoted_author_handle": None,
        "viewer_like_uri": None, "viewer_repost_uri": None, "viewer_bookmarked": False,
        "viewer_thread_muted": False, "labels_json": None,
    }


def _run_db_checks(m, check):
    """m: a private copy of the db module pointed at a temporary folder."""
    m.init_db()
    a = m.upsert_account("a.test", "did:plc:selfchecka", b"x")
    b = m.upsert_account("b.test", "did:plc:selfcheckb", b"x")
    m.set_active_account(a)
    check("two accounts get different ids", a != b)
    check("the active account is the one that was set", m.get_active_account()["id"] == a)

    m.upsert_author("did:plc:selfcheckauthor", "author.test", "Author A", None)
    m.upsert_post(_db_check_post("at://selfcheck/1", a))
    m.upsert_feed_item(a, "home", "at://selfcheck/1", "2026-01-01T00:00:00Z")
    check("account A sees its own post", len(m.get_feed_page(a, "home")) == 1)
    check("account B does not see it", m.get_feed_page(b, "home") == [])

    with m.account_context(b):
        check("inside account_context the account is B", m.get_active_account()["id"] == b)
        m.upsert_author("did:plc:selfcheckauthor", "author.test", "Author B", None)
        m.set_ui_state("selfcheck_setting", "from b")
    m.upsert_post(_db_check_post("at://selfcheck/2", b))
    m.upsert_feed_item(b, "home", "at://selfcheck/2", "2026-01-02T00:00:00Z")
    check("a post saved with B's id lands in B's file while A is active",
          [p["uri"] for p in m.get_feed_page(b, "home")] == ["at://selfcheck/2"])
    check("A's feed is untouched by that", [p["uri"] for p in m.get_feed_page(a, "home")] == ["at://selfcheck/1"])
    check("a setting written for B is not seen by A", m.get_ui_state("selfcheck_setting") is None)
    check("the same author is cached apart for each account", m.get_author("did:plc:selfcheckauthor")["display_name"] == "Author A")

    names = sorted(n for n in os.listdir(m.DB_DIR) if n.endswith(".db"))
    check("one file per account plus the central file", names == sorted(["nvsky.db", f"nvsky_{a}.db", f"nvsky_{b}.db"]))

    m.set_ui_state("keymap", '{"selfcheck": "1"}')
    with m.account_context(b):
        check("the keymap is shared by every account", m.get_ui_state("keymap") == '{"selfcheck": "1"}')
    m.set_bg_enabled(b, False)
    check("background checks can be turned off for one account", m.get_bg_enabled(b) is False and m.get_bg_enabled(a) is True)

    outcome = {}

    def worker():
        try:
            m.set_bg_sync_last(b, "home", "stamp b")
            outcome["b"] = m.get_bg_sync_last(b, "home")
            outcome["a"] = m.get_bg_sync_last(a, "home")
        finally:
            m.close_all_connections()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    check("a thread with no context still reaches the account it names", outcome == {"b": "stamp b", "a": None})

    m.set_ui_state("selfcheck_keep", "kept")
    m.clear_all_cache(a)
    check("clearing the cache empties the account's feed", m.get_feed_page(a, "home") == [])
    check("clearing the cache keeps its settings", m.get_ui_state("selfcheck_keep") == "kept")

    m.remove_account(b)
    check("removing an account deletes its file", not os.path.exists(os.path.join(m.DB_DIR, f"nvsky_{b}.db")))
    check("the other account's file is still there", os.path.exists(os.path.join(m.DB_DIR, f"nvsky_{a}.db")))
    try:
        m.get_feed_page(b, "home")
        recreated = True
    except m.AccountRemovedError:
        recreated = False
    check("a removed account's file is not created again", not recreated and not os.path.exists(os.path.join(m.DB_DIR, f"nvsky_{b}.db")))

    stale = os.path.join(m.DB_DIR, f"nvsky_{b}.db")
    with open(stale, "wb") as f:
        f.write(b"left over")
    c = m.upsert_account("c.test", "did:plc:selfcheckc", b"x")
    check("a new account never inherits a stale file", c == b and not os.path.exists(stale))
    check("the new account starts empty", m.get_feed_page(c, "home") == [])

    orphan = os.path.join(m.DB_DIR, "nvsky_99.db")
    with open(orphan, "wb") as f:
        f.write(b"orphan")
    m.init_db()
    check("a file of no account is swept at start", not os.path.exists(orphan))

    m.set_active_account(c)
    m.remove_account(c)
    check("removing the active account activates another", m.get_active_account()["id"] == a and m.current_account_id() == a)
    m.remove_account(a)
    check("with no account left there is no active account", m.get_active_account() is None and m.current_account_id() is None)
    m.set_ui_state("selfcheck_noaccount", "1")
    check("settings still work with no account", m.get_ui_state("selfcheck_noaccount") == "1")


def _probe_db_selfcheck(rep):
    import importlib.util
    import shutil
    import sqlite3
    import tempfile

    name = f"{db.__package__}.db_selfcheck"
    spec = importlib.util.spec_from_file_location(name, db.__file__)
    copy = importlib.util.module_from_spec(spec)
    sys.modules[name] = copy
    folder = tempfile.mkdtemp(prefix="nvsky-selfcheck-")
    results = []

    def check(label, condition):
        results.append((label, bool(condition)))

    try:
        spec.loader.exec_module(copy)
        copy.DB_DIR = os.path.join(folder, "NVSky")
        copy.DB_PATH = os.path.join(copy.DB_DIR, "nvsky.db")
        copy.DB_KEY_PATH = os.path.join(copy.DB_DIR, "db.key")
        try:
            _run_db_checks(copy, check)
            accountFiles = [n for n in os.listdir(copy.DB_DIR) if n.endswith(".db")]
        except Exception as e:
            check(f"the checks ran to the end (stopped with {type(e).__name__}: {_short(e)})", False)
            accountFiles = []
        copy.close_all_connections()
        for fileName in accountFiles[:1]:
            plain = sqlite3.connect(os.path.join(copy.DB_DIR, fileName))
            try:
                plain.execute("SELECT count(*) FROM sqlite_master").fetchone()
                readable = True
            except sqlite3.DatabaseError:
                readable = False
            finally:
                plain.close()
            check("the files are encrypted (plain sqlite cannot read them)", not readable)
    finally:
        try:
            copy.close_all_connections()
        except Exception:
            pass
        sys.modules.pop(name, None)
        shutil.rmtree(folder, ignore_errors=True)
    failed = [label for label, passed in results if not passed]
    if not failed:
        return _Detail(
            f"all {len(results)} checks passed",
            "\n".join("ok: " + label for label, _passed in results),
        )
    lines = [f"{len(failed)} OF {len(results)} FAILED"]
    lines += [("ok: " if passed else "FAILED: ") + label for label, passed in results if not passed or rep.verbose]
    return "\n".join(lines)


def _probe_js_shape(rep):
    host = jetstream.HOSTS[jetstream.MODE_V2][0]
    url = jetstream.build_url(jetstream.MODE_V2, host, ["app.bsky.feed.post"], None, None)
    res = _run_ws(url, [jetstream.V2_SUBPROTOCOL], None, 3, 8.0)
    if res["error"]:
        return _verdict(res)
    frames = res["frames"]
    out = [f"{host}: subprotocol={res['subprotocol']!r}, {len(frames)} frame(s)"]
    if not frames:
        out.append("no frames within 8s")
        return "\n".join(out)
    parsed = json.loads(frames[0])
    event = jetstream.normalize_frame(parsed)
    if event is None:
        out.append("normalized: None (frame ignored)")
        return _Detail("\n".join(out), "first frame:\n" + _dump(parsed, 1800))
    commit = event["commit"]
    out.append(f"normalized ok: kind={event['kind']} operation={commit['operation']} collection={commit['collection']}")
    return _Detail("\n".join(out), "first frame:\n" + _dump(parsed, 1800))


def _probe_js_follows(rep, mode):
    dids = list(rep.ctx["follow_dids"])
    if rep.ctx["did"] not in dids:
        dids.append(rep.ctx["did"])
    dids = dids[:MAX_URL_TEST_DIDS]
    host = jetstream.HOSTS[mode][0]
    collections = ["app.bsky.feed.post", "app.bsky.feed.repost"]
    url, hello = jetstream.connection_params(mode, host, collections, dids, None)
    subprotocols = [jetstream.V2_SUBPROTOCOL] if mode == jetstream.MODE_V2 else None
    res = _run_ws(url, subprotocols, hello, 1, 4.0)
    via = ", DIDs sent in options_update" if hello else ""
    return f"{host}: {len(dids)} DIDs, URL {len(url)} chars{via}: {_verdict(res)}"


def _probe_js_url_lengths(rep):
    count = int(rep.options.get("url_test_dids") or 0)
    collections = ["app.bsky.feed.post", "app.bsky.feed.repost"]
    sizes = sorted({s for s in (100, 300, 1000, count) if 0 < s <= count})
    lines = []
    host2 = jetstream.HOSTS[jetstream.MODE_V2][0]
    for size in sizes:
        url = jetstream.build_url(jetstream.MODE_V2, host2, collections, _fake_dids(size), None)
        res = _run_ws(url, [jetstream.V2_SUBPROTOCOL], None, 1, 1.5)
        lines.append(f"v2, {size} DIDs in URL ({len(url)} chars): {_verdict(res)}")
        time.sleep(STEP_DELAY)
    host1 = jetstream.HOSTS[jetstream.MODE_V1][0]
    fakeDids = _fake_dids(count)
    url, hello = jetstream.connection_params(
        jetstream.MODE_V1, host1, collections, fakeDids, None, force_hello=True
    )
    res = _run_ws(url, None, hello, 20, 3.0)
    lines.append(f"v1, {count} DIDs via options_update (URL {len(url)} chars, message {len(hello)} chars): {_verdict(res)}")
    # Fake DIDs match nobody: a frame from any other DID means the filter was ignored.
    fakeSet = set(fakeDids)
    leaked = 0
    parsedFrames = []
    for raw in res["frames"]:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        parsedFrames.append(parsed)
        if not isinstance(parsed, dict) or parsed.get("did") not in fakeSet:
            leaked += 1
    lines.append(f"v1 filter check: {leaked} of {len(res['frames'])} frame(s) were NOT from the listed DIDs (expected 0)")
    if parsedFrames:
        lines.append("first v1 frame:")
        lines.append(_dump(parsedFrames[0] if parsedFrames[0] is not None else res["frames"][0][:300], 1200))
    return "\n".join(lines)


_PROFILE_TEST_SPEC = "nvskyProbe"
_PROFILE_SPEECH_KEYS = ("voice", "rate", "rateBoost", "pitch", "volume")


def _list_profiles():
    try:
        import config
        return [n for n in config.conf.listProfiles() if n]
    except Exception:
        return []


def _on_main(func, timeout=30.0):
    """Runs func on the main thread; returns its result or re-raises its exception."""
    done = threading.Event()
    box = {}

    def run():
        try:
            box["value"] = func()
        except Exception as e:
            box["error"] = e
        finally:
            done.set()

    wx.CallAfter(run)
    if not done.wait(timeout):
        raise TimeoutError("the main thread did not run the step")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _profile_state():
    import config
    import synthDriverHandler
    synth = synthDriverHandler.getSynth()
    state = {"synth": getattr(synth, "name", None)}
    for attr in ("voice", "rate", "pitch", "volume"):
        try:
            state[attr] = getattr(synth, attr)
        except Exception:
            state[attr] = "?"
    try:
        state["effective speech.synth"] = config.conf["speech"]["synth"]
    except Exception as e:
        state["effective speech.synth"] = f"? ({type(e).__name__})"
    try:
        state["active profiles"] = [getattr(p, "name", None) or "base" for p in config.conf.profiles]
    except Exception:
        state["active profiles"] = "?"
    return state


def _state_text(state):
    return ", ".join(f"{k}={v}" for k, v in state.items())


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def _probe_profile_api(rep):
    import config
    from speech import commands
    has = lambda obj, name: "yes" if hasattr(obj, name) else "NO"
    lines = [f"config.ProfileTrigger: {has(config, 'ProfileTrigger')}"]
    mapping = getattr(config.conf, "triggersToProfiles", None)
    lines.append(f"config.conf.triggersToProfiles: {_type_name(mapping)}")
    for name in (
        "profiles", "listProfiles", "manualActivateProfile", "getProfile", "_getProfile",
        "disableProfileTriggers", "enableProfileTriggers", "saveProfileTriggers",
    ):
        lines.append(f"config.conf.{name}: {has(config.conf, name)}")
    lines.append(f"speech.commands.ConfigProfileTriggerCommand: {has(commands, 'ConfigProfileTriggerCommand')}")
    try:
        lines.append("ConfigProfileTriggerCommand signature: " + str(inspect.signature(commands.ConfigProfileTriggerCommand)))
    except Exception as e:
        lines.append(f"ConfigProfileTriggerCommand signature unavailable: {type(e).__name__}")
    lines.append("ProfileTrigger members: " + ", ".join(n for n in dir(config.ProfileTrigger) if not n.startswith("__")))
    try:
        lines.append("trigger specs with a profile: " + ", ".join(sorted(str(k) for k in (mapping or {}))))
    except Exception:
        pass
    return "\n".join(lines)


def _probe_profiles(rep):
    import config
    names = _list_profiles()
    lines = [f"{len(names)} profile(s): {', '.join(names) if names else '(none)'}", "now: " + _state_text(_profile_state())]
    getter = getattr(config.conf, "_getProfile", None) or getattr(config.conf, "getProfile", None)
    if getter is None:
        lines.append("no way found to read a profile's settings")
        return "\n".join(lines)
    for name in names:
        try:
            profile = getter(name)
            section = profile.get("speech") if hasattr(profile, "get") else None
            if not section:
                lines.append(f"{name}: no speech settings")
                continue
            synthName = section.get("synth")
            detail = {"synth": synthName}
            synthSection = section.get(synthName) if synthName else section
            if hasattr(synthSection, "get"):
                for key in _PROFILE_SPEECH_KEYS:
                    if key in synthSection:
                        detail[key] = synthSection[key]
            lines.append(f"{name}: {_state_text(detail)}")
        except Exception as e:
            lines.append(f"{name}: could not read ({type(e).__name__}: {_short(e)})")
    return "\n".join(lines)


def _probe_profile_test(rep):
    import config
    import speech
    from speech import commands
    name = rep.options.get("profile_name") or ""
    if not name:
        return "skipped: no profile chosen"
    mapping = getattr(config.conf, "triggersToProfiles", None)
    if mapping is None:
        raise RuntimeError("config.conf.triggersToProfiles is not available")
    import globalVars
    iniPath = os.path.join(globalVars.appArgs.configPath, "profileTriggers.ini")
    hashBefore = _file_hash(iniPath)
    spec = _PROFILE_TEST_SPEC
    marks = []
    done = threading.Event()
    box = {}

    def prepare():
        before = _profile_state()
        if name in before["active profiles"]:
            raise RuntimeError(f"profile {name!r} is already active; choose another one")

        class ProbeTrigger(config.ProfileTrigger):
            pass

        ProbeTrigger.spec = spec
        box["trigger"] = ProbeTrigger()
        box["previous"] = mapping.get(spec)
        box["stateBefore"] = before
        mapping[spec] = name

    def mark(label):
        def callback():
            marks.append((label, time.monotonic(), _profile_state()))
        return commands.CallbackCommand(callback)

    def speak():
        trigger = box["trigger"]
        box["started"] = time.monotonic()
        speech.speak([
            "Before the profile.", mark("before"),
            commands.ConfigProfileTriggerCommand(trigger, True),
            mark("inside"), "Inside the profile.", mark("spoken"),
            commands.ConfigProfileTriggerCommand(trigger, False),
            mark("after"), "After the profile.", commands.CallbackCommand(done.set),
        ])

    def cleanup():
        trigger = box.get("trigger")
        if trigger is not None and name in _profile_state()["active profiles"]:
            try:
                trigger.exit()
            except Exception:
                pass
        if "previous" in box:
            if box["previous"] is None:
                mapping.pop(spec, None)
            else:
                mapping[spec] = box["previous"]
        box["stateAfter"] = _profile_state()

    _on_main(prepare)
    finished = False
    try:
        _on_main(speak)
        finished = done.wait(25)
    finally:
        _on_main(cleanup)

    started = box.get("started", 0.0)
    lines = [f"profile {name!r}; the test speaks three short sentences, do not press keys meanwhile"]
    for label, when, state in marks:
        lines.append(f"{label} +{when - started:.2f}s: {_state_text(state)}")
    lines.append(f"finished in time: {'yes' if finished else 'NO (25s timeout)'}")
    inside = next((s for label, _t, s in marks if label == "inside"), None)
    if inside is None:
        lines.append("the profile was never entered")
    else:
        changed = {k: (box["stateBefore"].get(k), v) for k, v in inside.items() if box["stateBefore"].get(k) != v}
        lines.append("changed inside the profile (before, inside): " + (str(changed) if changed else "nothing"))
    lines.append(f"state restored: {'yes' if box.get('stateAfter') == box['stateBefore'] else 'NO (restart NVDA)'}")
    lines.append(f"profileTriggers.ini unchanged: {'yes' if _file_hash(iniPath) == hashBefore else 'NO'}")
    return "\n".join(lines)


def run_probe(options):
    rep = _Report(options)
    rep.add("NVSky diagnostics report " + time.strftime("%Y-%m-%d %H:%M:%S"))
    rep.add("No tokens or passwords are included.")
    rep.add("")
    rep.step("Environment", _probe_environment, confirmed=True)
    rep.step("Expected client methods", _probe_methods, confirmed=True)
    if rep.verbose:
        rep.step("NVDA profile API", _probe_profile_api)
        rep.step("NVDA profiles and their speech settings", _probe_profiles)
    rep.step("Database self-check (temporary folder)", _probe_db_selfcheck, confirmed=True)
    rep.step("Account switching logic", _probe_account_cycle, confirmed=True)
    rep.step("Background sync selection", _probe_bg_selection, confirmed=True)
    rep.step("Sound pack coverage", _probe_soundpack)

    account = db.get_active_account()
    if account is None:
        rep.add("[SKIP] account steps: no active account")
    else:
        rep.step("Login", lambda r: _probe_login(r, account), confirmed=True)
        rep.step("Home timeline", _probe_timeline, requires="atp", confirmed=True)
        rep.step("Embed shapes (scans up to 300 timeline posts)", _probe_embeds, requires="atp", confirmed=True)
        rep.step("Own author feed (repost and pin reasons)", _probe_author_feed, requires="atp", confirmed=True)
        rep.step("Notifications", _probe_notifications, requires="atp", confirmed=True)
        rep.step("Bookmarks", _probe_bookmarks, requires="atp", confirmed=True)
        rep.step("Likes via listRecords", _probe_likes, requires="atp", confirmed=True)
        rep.step("Preferences", _probe_preferences, requires="atp", confirmed=True)
        rep.step("Lists", _probe_lists, requires="atp", confirmed=True)
        rep.step("Follows", _probe_follows, requires="atp", confirmed=True)
        rep.step("Thread JSON and a missing post", _probe_thread, requires="sample_uri", confirmed=True)
        rep.step("Chat availability", _probe_chat, requires="atp", confirmed=True)
        rep.step("Chat log cursor", _probe_chat_log, requires="atp", confirmed=True)
        rep.step("Video upload limits", _probe_video_limits, requires="atp", confirmed=True)
        rep.step("Typed SDK calls vs raw bypass", _probe_typed_calls, requires="sample_uri", confirmed=True)
        rep.step("Typed vs raw: same data?", _probe_bypass_equivalence, requires="sample_uri", confirmed=True)
        rep.step("Error message shapes", _probe_error_shapes, requires="atp", confirmed=True)
        if options.get("write_tests"):
            rep.step("Write test: like/unlike and save/unsave", _probe_write, requires="sample_uri")

    rep.step("Jetstream v2: frame shape (no DID filter)", _probe_js_shape, confirmed=True)
    rep.step("Jetstream v2: your real follow list", lambda r: _probe_js_follows(r, jetstream.MODE_V2),
             requires="follow_dids", confirmed=True)
    rep.step("Jetstream v1: your real follow list", lambda r: _probe_js_follows(r, jetstream.MODE_V1),
             requires="follow_dids", confirmed=True)
    if int(options.get("url_test_dids") or 0) > 0:
        rep.step("Jetstream URL length test (synthetic DIDs)", _probe_js_url_lengths)
    if options.get("profile_test"):
        rep.step("NVDA profile voice test (custom trigger)", _probe_profile_test)
    rep.add("")
    if rep.quiet:
        rep.add(f"{rep.quiet} confirmed step(s) passed and are not shown (tick Verbose to see them).")
    rep.add("End of report.")
    return rep.text()


# ---------------- library upgrade ----------------

_ADDON_DIR = os.path.dirname(__file__)
_LIB_DIR = os.path.join(_ADDON_DIR, "lib")
_PENDING_DIR = os.path.join(_ADDON_DIR, "lib.new")  # swapped into lib by __init__ at the next start
_WORK_DIR = os.path.join(_ADDON_DIR, "_upgrade")
_VERSIONS_FILE = os.path.join(_LIB_DIR, "versions.json")
_NO_WINDOW = 0x08000000


def _normal(name):
    return name.lower().replace("-", "_")


def _read_versions():
    try:
        with open(_VERSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_versions(path, versions):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(versions, f, indent=1, sort_keys=True)


def _recorded_version(name):
    # dist-info folders are cleaned away, so their versions are kept in lib/versions.json.
    return _read_versions().get(_normal(name))


def _dist_versions(folder):
    versions = {}
    for name in os.listdir(folder):
        if name.endswith(".dist-info") and "-" in name:
            package, version = name[:-len(".dist-info")].split("-", 1)
            versions[_normal(package)] = version
    return versions


def _clean_lib():
    """Records versions, then removes *.dist-info folders and the bin folder from lib."""
    known = _read_versions()
    known.update(_dist_versions(_LIB_DIR))
    _write_versions(_VERSIONS_FILE, known)
    removed = []
    for name in sorted(os.listdir(_LIB_DIR)):
        full = os.path.join(_LIB_DIR, name)
        if os.path.isdir(full) and (name.endswith(".dist-info") or name == "bin"):
            shutil.rmtree(full, ignore_errors=True)
            removed.append(name)
    return removed


def _pip_command():
    for prefix in (["py", "-3"], ["python"]):
        try:
            done = subprocess.run(
                prefix + ["-m", "pip", "--version"], capture_output=True, text=True,
                timeout=30, creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return prefix + ["-m", "pip"]
    return None


def _upgrade_libs():
    """Installs the newest atproto and dependencies into lib.new; lib itself is not touched."""
    pip = _pip_command()
    if pip is None:
        raise RuntimeError("no Python with pip found (tried 'py -3' and 'python')")
    shutil.rmtree(_WORK_DIR, ignore_errors=True)
    staging = os.path.join(_WORK_DIR, "new")
    os.makedirs(staging)
    try:
        major, minor = sys.version_info[:2]
        platform = "win_amd64" if sys.maxsize > 2 ** 32 else "win32"
        done = subprocess.run(
            pip + [
                "install", "--upgrade", "--target", staging, "--only-binary=:all:",
                "--python-version", f"{major}.{minor}", "--implementation", "cp",
                "--abi", f"cp{major}{minor}", "--platform", platform,
                "--disable-pip-version-check", "--no-input", "atproto",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=900, creationflags=_NO_WINDOW,
        )
        if done.returncode != 0:
            raise RuntimeError("pip failed:\n" + (done.stdout + done.stderr)[-1500:])
        new = _dist_versions(staging)
        if "atproto" not in new:
            raise RuntimeError("pip finished but atproto is missing from the result")
        previous = _read_versions()
        for name in os.listdir(staging):
            if name.endswith(".dist-info") or name == "bin":
                shutil.rmtree(os.path.join(staging, name), ignore_errors=True)
        known = dict(previous)
        known.update(new)
        _write_versions(os.path.join(staging, "versions.json"), known)
        shutil.rmtree(_PENDING_DIR, ignore_errors=True)
        shutil.move(staging, _PENDING_DIR)
    finally:
        shutil.rmtree(_WORK_DIR, ignore_errors=True)
    changed = [
        f"  {name}: {previous.get(name, 'unknown')} -> {version}"
        for name, version in sorted(new.items()) if previous.get(name) != version
    ]
    lines = [
        f"Prepared {len(new)} package(s) in lib.new. Restart NVDA to switch to them.",
        f"atproto {previous.get('atproto', 'unknown')} -> {new['atproto']}",
        f"Changed or not recorded before ({len(changed)}):",
    ]
    return "\n".join(lines + changed)


# ---------------- settings page ----------------

_OPTIONS = [
    ("verbose", "Verbose report (full dumps and every sub-check)"),
    ("write_tests", "Write tests (like/unlike and save/unsave one post)"),
]


class DiagnosticsPage(wx.Panel):
    """Developer-only Settings page: Run probe, read-only report, Copy report."""

    def __init__(self, parent):
        super().__init__(parent)
        self._running = False
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "Developer diagnostics. Reads what Bluesky really sends and what Jetstream accepts. "
                "Nothing is saved to disk; copy the report to share it."
            ),
        )
        intro.Wrap(500)
        sizer.Add(intro, flag=wx.ALL, border=10)

        optionsLabel = wx.StaticText(self, label="&Options:")
        sizer.Add(optionsLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        self.optionsList = gui.nvdaControls.CustomCheckListBox(self, choices=[label for _key, label in _OPTIONS])
        self.optionsList.SetSelection(0)
        sizer.Add(self.optionsList, flag=wx.EXPAND | wx.ALL, border=10)

        row = wx.BoxSizer(wx.HORIZONTAL)
        urlLabel = wx.StaticText(self, label="&DIDs in the Jetstream URL length test (0 = skip):")
        self.urlSpin = wx.SpinCtrl(
            self, min=0, max=MAX_URL_TEST_DIDS, initial=0, name="DIDs in the Jetstream URL length test"
        )
        row.Add(urlLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        row.Add(self.urlSpin)
        sizer.Add(row, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.runButton = wx.Button(self, label="Run &probe")
        sizer.Add(self.runButton, flag=wx.LEFT | wx.BOTTOM, border=10)

        reportLabel = wx.StaticText(self, label="&Report:")
        sizer.Add(reportLabel, flag=wx.LEFT, border=10)
        self.reportCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.reportCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        self.copyButton = wx.Button(self, label="&Copy report")
        sizer.Add(self.copyButton, flag=wx.LEFT | wx.BOTTOM, border=10)

        # Rarely used, so last in the tab order.
        self.upgradeButton = wx.Button(self, label="&Upgrade libraries...")
        self.cleanButton = wx.Button(self, label="Clean &lib folder")
        maintenanceRow = wx.BoxSizer(wx.HORIZONTAL)
        maintenanceRow.Add(self.upgradeButton, flag=wx.RIGHT, border=5)
        maintenanceRow.Add(self.cleanButton)
        sizer.Add(maintenanceRow, flag=wx.LEFT | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.runButton.Bind(wx.EVT_BUTTON, self.onRun)
        self.upgradeButton.Bind(wx.EVT_BUTTON, self.onUpgrade)
        self.cleanButton.Bind(wx.EVT_BUTTON, self.onClean)
        self.copyButton.Bind(wx.EVT_BUTTON, self.onCopy)

    def onTabActivated(self):
        self.runButton.SetFocus()

    def onRun(self, evt):
        if self._running:
            return
        self._running = True
        self.runButton.Disable()
        checked = {_OPTIONS[i][0] for i in self.optionsList.CheckedItems}
        options = {key: key in checked for key, _label in _OPTIONS}
        options["url_test_dids"] = self.urlSpin.GetValue()
        nvdaUi.message("Running probe, please wait...")

        def worker():
            try:
                report = run_probe(options)
            except Exception as e:
                log.error(f"NVSky: diagnostics probe crashed: {e}")
                report = f"Probe crashed: {type(e).__name__}: {_short(e)}"
            finally:
                db.close_all_connections()
            wx.CallAfter(self._onDone, report)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onDone(self, report):
        self._running = False
        self.runButton.Enable()
        self.reportCtrl.SetValue(report)
        self.reportCtrl.SetInsertionPoint(0)
        self.reportCtrl.SetFocus()
        nvdaUi.message("Probe finished. The report is ready.")

    def onCopy(self, evt):
        text = self.reportCtrl.GetValue()
        if not text:
            nvdaUi.message("No report yet. Run the probe first.")
            return
        uiutil.copy_text_to_clipboard(text)

    def _show(self, text):
        self.reportCtrl.SetValue(text)
        self.reportCtrl.SetInsertionPoint(0)
        self.reportCtrl.SetFocus()
        nvdaUi.message(text.splitlines()[0] if text else "")

    def onClean(self, evt):
        if self._running:
            return
        try:
            removed = _clean_lib()
        except Exception as e:
            self._show(f"Cleaning lib failed: {type(e).__name__}: {_short(e)}")
            return
        self._show(
            f"Removed {len(removed)} item(s) from lib: {', '.join(removed) or 'nothing to remove'}\n"
            "Run the probe to confirm everything still imports."
        )

    def onUpgrade(self, evt):
        if self._running:
            return
        dlg = wx.MessageDialog(
            self,
            "Install the newest atproto and every dependency at its newest compatible release from PyPI, "
            "using the Python with pip found on this computer? The files are prepared next to lib and "
            "switched in when NVDA restarts; lib is not touched until then.",
            "Upgrade libraries", wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        if not confirmed:
            return
        self._running = True
        nvdaUi.message("Upgrading libraries, this can take a few minutes...")

        def worker():
            try:
                report = _upgrade_libs()
            except Exception as e:
                report = f"Upgrade failed, lib was left as it was: {type(e).__name__}: {_short(e)}"
            wx.CallAfter(self._onUpgradeDone, report)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onUpgradeDone(self, report):
        self._running = False
        self._show(report)
