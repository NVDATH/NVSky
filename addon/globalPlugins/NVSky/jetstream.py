"""
Jetstream (real-time firehose subscription) client for NVSky.

Handles the WebSocket side only: connect/reconnect with backoff, cursor
tracking, JSON decoding. What to do with events lives in __init__.py
(GlobalPlugin's _onJetstreamEvent and worker loop).

Jetstream is a public, unauthenticated read-only firehose
(https://github.com/bluesky-social/jetstream) -- no atproto login
needed. Runs its own asyncio loop in a dedicated background thread so
the rest of NVSky stays synchronous.

zstd compression is not used (compress param omitted); plain JSON only.
"""
import asyncio
import json
import random
import threading
import urllib.parse

from logHandler import log

try:
    import websockets
except ImportError:
    websockets = None

# Public Jetstream instances (bluesky-social/jetstream's own docs) --
# tried in round-robin order on each (re)connect attempt.
JETSTREAM_HOSTS = [
    "jetstream1.us-east.bsky.network",
    "jetstream2.us-east.bsky.network",
    "jetstream1.us-west.bsky.network",
    "jetstream2.us-west.bsky.network",
]

RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0


class JetstreamClient:
    """
    Runs a Jetstream subscription on a dedicated background thread.
    Call start() once; call stop() to shut it down (blocks briefly
    until the thread exits). on_event(event: dict) is called for every
    decoded message, from the background thread -- NOT the wx main
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
        # Jetstream replays the event AT a resumed cursor again on
        # reconnect (confirmed via testing) -- these two track that so
        # _consume can skip firing on_event for that stale replay
        # without skipping cursor persistence itself.
        self._initialCursor = cursor
        self._suppressReplay = cursor is not None
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

    def _buildUrl(self, host):
        params = []
        if self._wantedCollections:
            for c in self._wantedCollections:
                params.append(("wantedCollections", c))
        if self._wantedDids:
            for d in self._wantedDids:
                params.append(("wantedDids", d))
        if self._cursor:
            params.append(("cursor", str(self._cursor)))
        query = urllib.parse.urlencode(params)
        return f"wss://{host}/subscribe" + (f"?{query}" if query else "")

    def _threadMain(self):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._runLoop())
        finally:
            loop.close()

    async def _runLoop(self):
        attempt = 0
        hostIndex = 0
        while not self._stopEvent.is_set():
            host = JETSTREAM_HOSTS[hostIndex % len(JETSTREAM_HOSTS)]
            url = self._buildUrl(host)
            try:
                async with websockets.connect(url, open_timeout=15, close_timeout=5) as ws:
                    attempt = 0  # reset backoff on a real successful connect
                    await self._consume(ws)
            except Exception as e:
                if self._stopEvent.is_set():
                    break
                log.info(f"NVSky: jetstream connection to {host} failed/dropped: {e}")
                hostIndex += 1

            if self._stopEvent.is_set():
                break
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
            try:
                event = json.loads(raw)
            except (ValueError, TypeError) as e:
                log.info(f"NVSky: jetstream -- failed to decode message: {e}")
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
    Extracts (uri, cid, operation, collection) from a Jetstream commit
    event, or None if `event` isn't a commit (e.g. an identity/account
    event) or is missing a field this needs. uri is reconstructed from
    did/collection/rkey -- Jetstream itself never sends the uri
    directly.
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
    a Jetstream commit event for an app.bsky.feed.repost record, or
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