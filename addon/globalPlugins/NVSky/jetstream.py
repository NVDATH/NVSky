"""
Jetstream (real-time firehose subscription) client for NVSky.

Handles the WebSocket side only: connect/reconnect with backoff, cursor
tracking, JSON decoding. What to do with events lives in __init__.py
(GlobalPlugin's _onJetstreamEvent and worker loop).

Jetstream is a public, unauthenticated read-only firehose -- no atproto
login needed. Runs its own asyncio loop in a dedicated background thread
so the rest of NVSky stays synchronous.

Two wire formats are supported:
 - v2 (preferred): /xrpc/network.bsky.jetstream.subscribeEvents, filters
   named collections/dids/kinds, frames wrapped as
   {"$type": "message", "payload": {...}} (xrpc.v1.json subprotocol).
 - v1 (legacy): /subscribe, filters named wantedCollections/wantedDids.
   Its options_update message was tested and ignored the DID filter, so a
   list too long for a URL is not watched at all (see can_watch).
After FAILS_BEFORE_MODE_SWITCH unhealthy connections in a row the client
switches to the other format. Every frame is normalized into the v1 event
shape, so callers (parse_commit_event, parse_repost_subject) never see
the difference.

Cursors are unix-microsecond timestamps on both versions (v2 treats values
>= 1e15 as timestamps, which also makes a cursor valid across instances).
zstd compression is not used; plain JSON only.
"""
import asyncio
import json
import random
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from logHandler import log

try:
    import websockets
except ImportError:
    websockets = None

MODE_V2 = "v2"
MODE_V1 = "v1"

# Public instances listed in Bluesky's Jetstream docs; tried round-robin.
HOSTS = {
    MODE_V2: ["jetstream.us-east.bsky.network", "jetstream.us-west.bsky.network"],
    MODE_V1: ["jetstream2.us-east.bsky.network", "jetstream1.us-west.bsky.network"],
}
PATHS = {
    MODE_V2: "/xrpc/network.bsky.jetstream.subscribeEvents",
    MODE_V1: "/subscribe",
}
V2_SUBPROTOCOL = "xrpc.v1.json"

# Measured limits: v2 accepts an 11.5 KB URL and refuses a 38 KB one (HTTP 400).
V2_URL_BUDGET = 10000
# v1 was only tried up to about 1.4 KB; stay well below the v2 limit.
V1_URL_BUDGET = 4000

RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
FAILS_BEFORE_MODE_SWITCH = 3
HEALTHY_SECONDS = 30.0
# Resume slightly before the last event: cursors are timestamps and instances differ by ms.
REPLAY_BUFFER_US = 2_000_000

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def build_url(mode, host, collections=None, dids=None, cursor=None, require_hello=False):
    params = []
    if mode == MODE_V2:
        for c in collections or ():
            params.append(("collections", c))
        params.append(("kinds", "commit"))
        for d in dids or ():
            params.append(("dids", d))
    else:
        for c in collections or ():
            params.append(("wantedCollections", c))
        for d in dids or ():
            params.append(("wantedDids", d))
        if require_hello:
            params.append(("requireHello", "true"))
    if cursor:
        params.append(("cursor", str(cursor)))
    query = urllib.parse.urlencode(params, safe=":")
    return f"wss://{host}{PATHS[mode]}?{query}"


def connection_params(mode, host, collections, dids, cursor, force_hello=False):
    """Returns (url, hello_text_or_None). The hello message is used by diagnostics only."""
    # v1 ignores the DID filter sent this way, so the client never uses it.
    if mode == MODE_V1 and dids and force_hello:
        hello = json.dumps({
            "type": "options_update",
            "payload": {"wantedCollections": list(collections or []), "wantedDids": list(dids)},
        })
        return build_url(mode, host, collections, None, cursor, require_hello=True), hello
    return build_url(mode, host, collections, dids, cursor), None


def can_watch(collections, dids):
    """True if the DID list fits in a v2 URL, the only way we trust to filter a long list."""
    url = build_url(MODE_V2, HOSTS[MODE_V2][0], collections, dids, None)
    return len(url) <= V2_URL_BUDGET


def _time_us(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(microseconds=1)


def normalize_frame(frame):
    """
    Returns a v1-shaped event dict, or None for frames NVSky ignores (info,
    non-commit kinds, anything unknown). Raises ConnectionError for a v2
    error frame so the caller reconnects.
    """
    if not isinstance(frame, dict):
        return None
    if "kind" in frame:  # v1 wire format: already the shape used downstream
        return frame
    frameType = frame.get("$type")
    if frameType == "error":
        raise ConnectionError(f"{frame.get('error')}: {frame.get('message')}")
    if frameType != "message":
        return None
    payload = frame.get("payload") or {}
    if not str(payload.get("$type", "")).endswith("#commit"):
        return None
    return {
        "kind": "commit",
        "did": payload.get("did"),
        "time_us": _time_us(payload.get("time")),
        "seq": payload.get("seq"),
        "commit": {key: payload.get(key) for key in ("operation", "collection", "rkey", "rev", "cid", "record")},
    }


class JetstreamClient:
    """
    Runs a Jetstream subscription on a dedicated background thread.
    Call start() once; call stop() to shut it down (blocks briefly
    until the thread exits). on_event(event: dict) is called for every
    decoded commit, from the background thread -- NOT the wx main
    thread; callers touching wx must wx.CallAfter() themselves.
    on_cursor(time_us: int) is called after every event is handed to
    on_event, so the caller can persist it (see db.py's
    get/set_jetstream_cursor) -- called on the same background thread.
    """

    def __init__(self, on_event, on_cursor=None, wanted_collections=None, wanted_dids=None, cursor=None):
        self._onEvent = on_event
        self._onCursor = on_cursor
        self._wantedCollections = list(wanted_collections) if wanted_collections else None
        self._wantedDids = list(wanted_dids) if wanted_dids else None
        self._cursor = cursor
        # A resumed cursor replays events already handled; these two let
        # _consume skip on_event for that replay but still persist the cursor.
        self._initialCursor = cursor
        self._suppressReplay = cursor is not None
        self._mode = MODE_V2
        self._hostIndex = 0
        self._failStreak = 0
        self._framesSeen = 0
        self._stopEvent = threading.Event()
        self._thread = None

    def start(self):
        if websockets is None:
            log.error("NVSky: jetstream -- websockets library unavailable, cannot start")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopEvent.clear()
        self._thread = threading.Thread(target=self._threadMain, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopEvent.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _fits(self, mode):
        budget = V2_URL_BUDGET if mode == MODE_V2 else V1_URL_BUDGET
        url = build_url(mode, HOSTS[mode][0], self._wantedCollections, self._wantedDids, self._cursor)
        return len(url) <= budget

    def _switchMode(self):
        target = MODE_V1 if self._mode == MODE_V2 else MODE_V2
        if not self._fits(target):
            self._failStreak = 0  # the other protocol cannot take this list; keep retrying this one
            return
        self._mode = target
        self._failStreak = 0
        self._hostIndex = 0
        log.info(f"NVSky: jetstream -- switching to the {self._mode} protocol after repeated connection failures")

    def _threadMain(self):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._runLoop())
        finally:
            loop.close()

    async def _runLoop(self):
        attempt = 0
        while not self._stopEvent.is_set():
            mode = self._mode
            hosts = HOSTS[mode]
            host = hosts[self._hostIndex % len(hosts)]
            resume = max(0, self._cursor - REPLAY_BUFFER_US) if self._cursor else None
            url, hello = connection_params(mode, host, self._wantedCollections, self._wantedDids, resume)
            kwargs = {"open_timeout": 15, "close_timeout": 5, "max_size": 4 * 1024 * 1024}
            if mode == MODE_V2:
                kwargs["subprotocols"] = [V2_SUBPROTOCOL]
            startedAt = time.monotonic()
            framesBefore = self._framesSeen
            try:
                async with websockets.connect(url, **kwargs) as ws:
                    if hello is not None:
                        await ws.send(hello)
                    await self._consume(ws)
            except Exception as e:
                if self._stopEvent.is_set():
                    break
                log.info(f"NVSky: jetstream {mode} connection to {host} failed/dropped: {e}")

            if self._stopEvent.is_set():
                break
            # Healthy = it delivered something, or stayed up long enough to prove the filter was accepted.
            healthy = self._framesSeen > framesBefore or time.monotonic() - startedAt >= HEALTHY_SECONDS
            self._hostIndex += 1
            if healthy:
                self._failStreak = 0
                attempt = 0
            else:
                self._failStreak += 1
                if self._failStreak >= FAILS_BEFORE_MODE_SWITCH:
                    self._switchMode()
            attempt += 1
            delay = min(RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY * (2 ** (attempt - 1)))
            delay = delay * (0.8 + 0.4 * random.random())  # jitter, avoid thundering herd
            await self._sleepInterruptible(delay)

    async def _sleepInterruptible(self, seconds):
        elapsed = 0.0
        step = 0.5
        while elapsed < seconds and not self._stopEvent.is_set():
            await asyncio.sleep(step)
            elapsed += step

    async def _consume(self, ws):
        while not self._stopEvent.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._framesSeen += 1
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError) as e:
                log.info(f"NVSky: jetstream -- failed to decode message: {e}")
                continue
            event = normalize_frame(frame)
            if event is None:
                continue

            timeUs = event.get("time_us")
            if timeUs is not None:
                self._cursor = timeUs

            isReplay = (
                self._suppressReplay and timeUs is not None and self._initialCursor is not None
                and timeUs <= self._initialCursor
            )
            if timeUs is not None and not isReplay:
                self._suppressReplay = False

            if not isReplay:
                try:
                    self._onEvent(event)
                except Exception as e:
                    log.error(f"NVSky: jetstream on_event callback failed: {e}")

            if timeUs is not None and self._onCursor is not None:
                try:
                    self._onCursor(timeUs)
                except Exception as e:
                    log.error(f"NVSky: jetstream on_cursor callback failed: {e}")


def parse_commit_event(event: dict):
    """
    Extracts (uri, cid, operation, collection) from a (normalized) commit
    event, or None if `event` isn't a commit or is missing a field this
    needs. uri is reconstructed from did/collection/rkey -- Jetstream
    itself never sends the uri directly.
    """
    if event.get("kind") != "commit":
        return None
    commit = event.get("commit") or {}
    collection = commit.get("collection")
    rkey = commit.get("rkey")
    did = event.get("did")
    operation = commit.get("operation")
    if not (collection and rkey and did and operation):
        return None
    uri = f"at://{did}/{collection}/{rkey}"
    cid = commit.get("cid")
    return uri, cid, operation, collection


def parse_repost_subject(event: dict):
    """
    Extracts (subject_uri, subject_cid, reposter_did, created_at) from
    a (normalized) commit event for an app.bsky.feed.repost record, or
    None if the shape doesn't match. reposter_did is the account that
    performed the repost (event's own did), not the original post's
    author.
    """
    if event.get("kind") != "commit":
        return None
    commit = event.get("commit") or {}
    if commit.get("collection") != "app.bsky.feed.repost":
        return None
    record = commit.get("record") or {}
    subject = record.get("subject") or {}
    subjectUri = subject.get("uri")
    if not subjectUri:
        return None
    subjectCid = subject.get("cid")
    reposterDid = event.get("did")
    createdAt = record.get("createdAt")
    return subjectUri, subjectCid, reposterDid, createdAt
