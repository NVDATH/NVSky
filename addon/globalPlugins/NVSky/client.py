"""
Bluesky (AT Protocol) client wrapper for NVSky.

The atproto Client class is imported lazily inside functions to keep
NVDA startup fast (pydantic's import cost is slow).

Record writes and several chat/preferences calls use raw
invoke_procedure/invoke_query (DotDict or plain JSON) on purpose: they
tolerate union members newer than the SDK's models and send data back
exactly as read. See _create_record and send_message.
"""

import json
import os
import random
import re
import string
import struct
import tempfile
import threading
import time
import unicodedata
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from logHandler import log

from . import crypto
from . import db
from . import soundpack


class _LazyModels:
    """atproto.models takes ~0.7 s to import: load it on first use, not at NVDA start."""

    def __getattr__(self, name):
        from atproto import models as realModels
        return getattr(realModels, name)


models = _LazyModels()


class LoginError(Exception):
    pass


def _normalize_handle(handle: str) -> str:
    handle = handle.strip().lstrip("@")
    if "." not in handle:
        return f"{handle}.bsky.social"
    return handle


def _friendly_rate_limit_message(e: Exception):
    """
    Friendly message for a rate-limit error, or None if `e` isn't one. Detects by exception class
    name or a plain HTTP 429, and uses the RateLimit-Reset header for the wait time when present.
    """
    isNamedRateLimitError = type(e).__name__ == "RateLimitExceededError"
    response = getattr(e, "response", None)
    status = getattr(response, "status_code", None)
    if not isNamedRateLimitError and status != 429:
        return None

    headers = getattr(response, "headers", None) or {}
    resetHeader = headers.get("RateLimit-Reset") or headers.get("ratelimit-reset")
    if resetHeader:
        try:
            resetTime = datetime.fromtimestamp(int(resetHeader), tz=timezone.utc)
            waitSeconds = max(0, (resetTime - datetime.now(timezone.utc)).total_seconds())
            waitMinutes = int(waitSeconds // 60) + (1 if waitSeconds % 60 else 0)
            if waitMinutes <= 1:
                # Translators: Shown when the server rate-limits the account.
                return _("You're sending requests too quickly. Try again in about a minute.")
            # Translators: Shown when the server rate-limits the account. {} is the number of minutes.
            return _("You're sending requests too quickly. Try again in about {} minutes.").format(waitMinutes)
        except (ValueError, TypeError, OSError):
            pass
    # Translators: Shown when the server rate-limits the account and no wait time is known.
    return _("You're sending requests too quickly. Please wait a bit before trying again.")


def _extract_error_message(e: Exception) -> str:
    rateLimitMessage = _friendly_rate_limit_message(e)
    if rateLimitMessage:
        return rateLimitMessage
    content = getattr(e, "content", None)
    if content is None:
        response = getattr(e, "response", None)
        content = getattr(response, "content", None)
    message = getattr(content, "message", None)
    return message or str(e) or type(e).__name__


def _parse_at_uri(uri: str) -> dict:
    parts = uri.replace("at://", "").split("/")
    return {"repo": parts[0], "collection": parts[1], "rkey": parts[2]}


def _dict_get(record):
    """
    Returns a working .get-style accessor for `record`, or None if it
    genuinely isn't dict-like. Some SDK wrapper objects have a `.get`
    ATTRIBUTE that exists (hasattr is True) but resolves to None instead of
    a callable method -- checking callable() here, not just hasattr(),
    is what actually matters. This was the real cause of a recurring
    'NoneType' object is not callable crash during sync.
    """
    getter = getattr(record, "get", None)
    return getter if callable(getter) else None


def login(handle: str, app_password: str) -> dict:
    from atproto import Client as ATProtoClient

    normalized_handle = _normalize_handle(handle)

    client = ATProtoClient()
    try:
        profile = client.login(normalized_handle, app_password)
    except Exception as e:
        message = _extract_error_message(e)
        log.error(f"NVSky: login failed for {normalized_handle}: {message}")
        # Translators: Shown when login fails. First {} is the handle, second {} is the server's error message.
        raise LoginError(
            _('Could not log in as "{}". Check your handle and App Password are correct. ({})').format(
                normalized_handle, message
            )
        ) from e

    encrypted = crypto.encrypt(app_password)
    account_id = db.upsert_account(handle=profile.handle, did=profile.did, encrypted_password=encrypted)
    db.set_active_account(account_id)

    chatSupported = check_chat_supported(client)
    db.set_chat_supported(account_id, chatSupported)

    return {"id": account_id, "handle": profile.handle, "did": profile.did, "chat_supported": chatSupported}


def check_chat_supported(client) -> bool:
    """
    Whether the account can use chat: probes the cheapest chat call (listing one conversation). A
    network error, a 5xx or a 429 counts as supported, so a bad moment never hides the Chat tab;
    other failures count as unsupported. Runs again on every login.
    """
    try:
        get_chat_client(client).chat.bsky.convo.list_convos(params={"limit": 1})
        return True
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is None or status >= 500 or status == 429:
            log.info(f"NVSky: chat capability check inconclusive, assuming supported: {e}")
            return True
        log.info(f"NVSky: chat capability check failed (treating as unsupported): {e}")
        return False


_session_lock = threading.Lock()
_session_cache = {}  # did -> session string, in memory only

_ssl_lock = threading.Lock()
_ssl_context = None


def _new_atproto_client():
    """An atproto Client sharing one SSL context: building one per client cost ~0.15 s."""
    global _ssl_context
    from atproto import Client as ATProtoClient
    try:
        if _ssl_context is None:
            with _ssl_lock:
                if _ssl_context is None:
                    import httpx
                    _ssl_context = httpx.create_ssl_context()
        from atproto_client.request import Request
        return ATProtoClient(request=Request(verify=_ssl_context))
    except Exception as e:
        log.error(f"NVSky: shared SSL context unavailable, using the default client: {e}")
        return ATProtoClient()


def _remember_session(client, account):
    def _on_change(event, session):
        try:
            with _session_lock:
                _session_cache[account["did"]] = session.encode()
        except Exception as e:
            log.error(f"NVSky: caching refreshed session failed: {e}")

    try:
        client.on_session_change(_on_change)
        with _session_lock:
            _session_cache[account["did"]] = client.export_session_string()
    except Exception as e:
        log.error(f"NVSky: could not cache session: {e}")


def _client_from_cached_session(account):
    from atproto import Client as ATProtoClient

    with _session_lock:
        sessionString = _session_cache.get(account["did"])
    if not sessionString:
        return None
    try:
        client = _new_atproto_client()
        client.login(session_string=sessionString, fetch_bsky_profile=False)
        session = getattr(client, "_session", None)
        # Only me.did is used; the profile lookup cost ~0.9 s per client.
        client.me = types.SimpleNamespace(
            did=getattr(session, "did", None), handle=getattr(session, "handle", None)
        )
        if getattr(client.me, "did", None) != account["did"]:
            raise RuntimeError("cached session belongs to another account")
        _remember_session(client, account)
        return client
    except Exception:
        with _session_lock:
            _session_cache.pop(account["did"], None)
        return None


def get_client_for_active_account():
    from atproto import Client as ATProtoClient

    account = db.get_active_account()
    if account is None:
        # Translators: Shown when an action needs an account but none is stored.
        raise LoginError(_("No active account is stored."))

    cachedClient = _client_from_cached_session(account)
    if cachedClient is not None:
        return cachedClient

    app_password = crypto.decrypt(account["encrypted_password"])

    # Retry once with a fresh client: a transient WinError 10038
    # ("not a socket") was seen here and cleared on retry.
    lastError = None
    for attempt in range(2):
        client = _new_atproto_client()
        try:
            client.login(account["handle"], app_password)
            _remember_session(client, account)
            return client
        except Exception as e:
            lastError = e
            if attempt == 0:
                time.sleep(0.5)

    message = _extract_error_message(lastError)
    log.error(f"NVSky: re-login failed for {account['handle']}: {message}")
    raise LoginError(message) from lastError


# ---------------- timeline sync ----------------

def _store_convo(convo, account_id: int, my_did: str, status: str):
    otherMembers = [
        {"did": m.did, "handle": m.handle, "display_name": getattr(m, "display_name", None)}
        for m in convo.members if m.did != my_did
    ]
    # convo.kind is a nested union (groupConvo/directConvo) whose $type is unreliable and whose
    # subtypes' fields both exist as attributes. Detect a group by the VALUE of lock_status (always
    # "locked"/"unlocked" for a group, else None).
    kind = convo.kind
    lockStatus = getattr(kind, "lock_status", None) if kind is not None else None
    if kind is not None and lockStatus is not None:
        isGroup = True
        groupName = getattr(kind, "name", None)
        locked = lockStatus == "locked"
    elif kind is not None:
        isGroup = False
        groupName = None
        locked = False
    else:
        isGroup = len(otherMembers) > 1
        groupName = None
        locked = False

    # Role lives on member.kind.role (chat.bsky.actor.defs#groupConvoMember),
    # one level deeper than the convo-level kind above.
    isAdmin = False
    if isGroup:
        myMember = next((m for m in convo.members if m.did == my_did), None)
        memberKind = getattr(myMember, "kind", None) if myMember is not None else None
        role = str(getattr(memberKind, "role", "") or "").lower()
        isAdmin = role in ("owner", "admin")

    lastMessage = convo.last_message
    lastText = getattr(lastMessage, "text", "") if lastMessage is not None else ""
    lastSentAt = getattr(lastMessage, "sent_at", None) if lastMessage is not None else None

    db.upsert_convo({
        "account_id": account_id,
        "convo_id": convo.id,
        "is_group": int(isGroup),
        "group_name": groupName,
        "locked": int(locked),
        "is_admin": int(isAdmin),
        "last_message_text": lastText or "",
        "last_message_sent_at": lastSentAt,
        "unread_count": convo.unread_count or 0,
        "muted": bool(convo.muted),
        "status": status,
        "unread_join_request_count": getattr(kind, "unread_join_request_count", 0) or 0,
    })
    db.replace_convo_members(account_id, convo.id, otherMembers)


def _extract_message_embed_info(message: dict):
    """
    Quoted-post info from a chat message's record embed, or None if there is none. The shape follows
    post embeds (record.value.text, record.author.handle).
    """
    embed = message.get("embed")
    if not isinstance(embed, dict):
        return None
    record = embed.get("record") or {}
    value = record.get("value") or {}
    author = record.get("author") or {}
    quotedText = value.get("text")
    quotedHandle = author.get("handle")
    if quotedText is None and quotedHandle is None:
        return None
    return {
        "quoted_text": quotedText,
        "quoted_author_handle": quotedHandle,
        "quoted_uri": record.get("uri"),
    }


def _upsert_message_from_raw(account_id: int, convo_id: str, message: dict) -> bool:
    """
    Shared per-message cache upsert for the full getMessages sync and the getLog delta path (the
    same raw MessageView dict either way). Returns False (does nothing) for a deleted or system
    message placeholder with no "text" field.
    """
    if "text" not in message:
        return False
    sender = message.get("sender") or {}
    # Unlike the REQUEST side (MessageInput.replyTo, just {messageId}),
    # the RESPONSE embeds the entire original message under replyTo --
    # id/text directly, not a bare reference.
    replyTo = message.get("replyTo") or {}
    embedInfo = _extract_message_embed_info(message)
    db.upsert_message({
        "account_id": account_id,
        "convo_id": convo_id,
        "message_id": message.get("id"),
        "sender_did": sender.get("did"),
        "text": expand_link_facets(message.get("text", ""), message.get("facets")),
        "sent_at": message.get("sentAt"),
        "reply_to_message_id": replyTo.get("id"),
        "reply_to_text": replyTo.get("text"),
        "reactions_json": json.dumps(message.get("reactions") or []),
        "embed_json": json.dumps(embedInfo) if embedInfo else None,
    })
    return True


def _sync_convo_messages(dm, convo_id: str, account_id: int, unread_count: int = 0, limit: int = 100):
    # Raw JSON on purpose: tolerates message types newer than the SDK's models.
    params = models.ChatBskyConvoGetMessages.Params(convo_id=convo_id, limit=limit)
    response = dm._client.invoke_query(
        "chat.bsky.convo.getMessages", params=params, output_encoding="application/json"
    )
    rawMessages = response.content.get("messages", []) if isinstance(response.content, dict) else []

    # "New incoming" = not cached before this sync and not sent by us.
    existingIds = {
        m["message_id"] for m in db.get_messages_for_convo(account_id, convo_id)
    }
    # First-ever sync of a convo (or right after a cache clear): every
    # message looks new, so skip the sound instead of playing a burst.
    isFirstSyncForConvo = not existingIds
    myDid = db.get_active_account()
    myDid = myDid["did"] if myDid else None
    hasNewIncoming = False

    for message in rawMessages:
        sender = message.get("sender") or {}
        if (
            not isFirstSyncForConvo
            and "text" in message
            and message.get("id") not in existingIds
            and sender.get("did") != myDid
        ):
            hasNewIncoming = True
        _upsert_message_from_raw(account_id, convo_id, message)

    if hasNewIncoming:
        soundpack.play("new_message")

    # is_read is re-derived from the server's unread_count, never guessed per message.
    db.reconcile_message_read_state(account_id, convo_id, unread_count)


def sync_convos(client, account_id: int, my_did: str, limit: int = 50):
    """
    Full sync: the conversation list AND every conversation's messages,
    so selecting a conversation is instant from the local cache.
    "request" and "accepted" are fetched separately (listConvos filters
    by one status per call). Only the first `limit` per status -- no
    pagination yet (listed under Known issues in the readme).
    """
    dm = get_chat_client(client).chat.bsky.convo

    for status in ("request", "accepted"):
        resp = dm.list_convos(params={"limit": limit, "status": status})
        for convo in resp.convos:
            try:
                _store_convo(convo, account_id, my_did, status)
                _sync_convo_messages(dm, convo.id, account_id, convo.unread_count or 0)
            except Exception as e:
                # Never dump the ConvoView repr here: 30KB+ log lines froze NVDA's speech.
                log.error(f"NVSky: failed to sync a conversation: {e}")


def get_convo(client, convo_id: str):
    """
    One conversation's current ConvoView (fresh unread_count, lock/name/
    muted state), or None if it doesn't exist or the call fails.
    """
    dm = get_chat_client(client).chat.bsky.convo
    try:
        resp = dm.get_convo(params={"convo_id": convo_id})
    except Exception as e:
        log.info(f"NVSky: get_convo failed for {convo_id}: {e}")
        return None
    return getattr(resp, "convo", None)


def sync_convo_messages(client, account_id: int, convo_id: str, limit: int = 100):
    """
    Refreshes just ONE conversation's messages -- used after sending a
    message, and by the pop-out per-conversation tab's own Check for
    updates, without re-listing every conversation.

    Also re-fetches this one conversation's own record via get_convo()
    first, so its unread_count -- and locked/group-name/muted state --
    comes from the server fresh instead of reusing whatever sync_convos
    last wrote. Falls back to the last-known local unread_count if
    get_convo fails or the active account doesn't match account_id.
    """
    dm = get_chat_client(client).chat.bsky.convo
    localConvo = db.get_convo(account_id, convo_id)
    unreadCount = (localConvo.get("unread_count") or 0) if localConvo else 0

    account = db.get_active_account()
    if account is not None and account["id"] == account_id:
        freshConvo = get_convo(client, convo_id)
        if freshConvo is not None:
            status = (localConvo.get("status") if localConvo else None) or "accepted"
            try:
                _store_convo(freshConvo, account_id, account["did"], status)
                unreadCount = freshConvo.unread_count or 0
            except Exception as e:
                log.error(f"NVSky: failed to store fresh convo {convo_id}: {e}")

    _sync_convo_messages(dm, convo_id, account_id, unreadCount, limit=limit)


def get_convo_log(client, cursor: str = None, limit: int = 100):
    """
    Raw JSON for chat.bsky.convo.getLog: log entries come in many types, some newer
    than the SDK's models. Returns (logs: list of raw dicts, new_cursor: str or None).
    cursor=None returns the server's current position with an EMPTY logs list (not
    historical backlog). Callers must persist and resend the returned cursor to see
    what changed since the previous call.
    """
    dm = get_chat_client(client).chat.bsky.convo
    # invoke_query needs a typed Params model, not a plain dict.
    params = models.ChatBskyConvoGetLog.Params(cursor=cursor, limit=limit)
    response = dm._client.invoke_query(
        "chat.bsky.convo.getLog", params=params, output_encoding="application/json"
    )
    content = response.content if isinstance(response.content, dict) else {}
    return content.get("logs", []), content.get("cursor")


# Log entry types that change ConvoView-level metadata (membership, lock state, group name, join
# links/requests) rather than one message or reaction. Any of them in a getLog batch triggers one
# full sync_convos() instead of hand-parsing every variant.
_STRUCTURAL_LOG_TYPES = {
    "chat.bsky.convo.defs#logBeginConvo",
    "chat.bsky.convo.defs#logAcceptConvo",
    "chat.bsky.convo.defs#logLeaveConvo",
    "chat.bsky.convo.defs#logMuteConvo",
    "chat.bsky.convo.defs#logUnmuteConvo",
    "chat.bsky.convo.defs#logAddMember",
    "chat.bsky.convo.defs#logRemoveMember",
    "chat.bsky.convo.defs#logMemberJoin",
    "chat.bsky.convo.defs#logMemberLeave",
    "chat.bsky.convo.defs#logLockConvo",
    "chat.bsky.convo.defs#logUnlockConvo",
    "chat.bsky.convo.defs#logLockConvoPermanently",
    "chat.bsky.convo.defs#logEditGroup",
    "chat.bsky.convo.defs#logCreateJoinLink",
    "chat.bsky.convo.defs#logEditJoinLink",
    "chat.bsky.convo.defs#logEnableJoinLink",
    "chat.bsky.convo.defs#logDisableJoinLink",
    "chat.bsky.convo.defs#logIncomingJoinRequest",
    "chat.bsky.convo.defs#logApproveJoinRequest",
    "chat.bsky.convo.defs#logRejectJoinRequest",
    "chat.bsky.convo.defs#logOutgoingJoinRequest",
}


def sync_chat_delta(client, account_id: int, my_did: str) -> set:
    """
    Background-sync-only delta path (Phase 1) -- replaces bgsync's
    previous full sync_convos() call on every tick. Manual F5 and
    ConvoTabWindow's own Check for updates are UNCHANGED, still call
    the full sync_convos()/sync_convo_messages() path -- scoped to
    periodic background sync only, per the plan to prove this out on
    the lowest-risk caller first.

    First call ever for this account (no stored cursor): bootstraps
    via a full sync_convos(), then makes one getLog call just to
    obtain and store a starting cursor (its logs are discarded -- the
    full sync above already has everything). Returns an empty set.

    Subsequent calls: fetches only what changed since the stored
    cursor via getLog, processing logCreateMessage/logAddReaction/
    logRemoveReaction/logDeleteMessage directly into the local cache
    (same per-message upsert path the full getMessages sync uses).
    Any OTHER log type (see _STRUCTURAL_LOG_TYPES) triggers one full
    sync_convos() fallback, since those affect convo-level state this
    function doesn't attempt to hand-reconstruct.

    Read-state (logReadConvo/logReadMessage) is deliberately NOT
    reconciled here -- background sync's job is "is there new content
    to announce", not exact read/unread bookkeeping; F5 still does
    that properly via reconcile_message_read_state.

    Returns the set of convo_ids that received a new, non-own message
    or reaction since the last check.
    """
    storedCursor = db.get_chat_log_cursor(account_id)

    if storedCursor is None:
        sync_convos(client, account_id, my_did)
        _logs, newCursor = get_convo_log(client, cursor=None)
        if newCursor:
            db.set_chat_log_cursor(account_id, newCursor)
        return set()

    try:
        logs, newCursor = get_convo_log(client, cursor=storedCursor)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 400:
            log.info(f"NVSky: chat log cursor rejected, will re-bootstrap: {e}")
            db.delete_ui_state(f"chat_log_cursor:{account_id}")
        raise
    if not logs:
        if newCursor:
            db.set_chat_log_cursor(account_id, newCursor)
        return set()

    touchedConvoIds = set()
    needsFullResync = False

    for entry in logs:
        logType = entry.get("$type", "")
        convoId = entry.get("convoId")

        if logType in (
            "chat.bsky.convo.defs#logCreateMessage",
            "chat.bsky.convo.defs#logAddReaction",
            "chat.bsky.convo.defs#logRemoveReaction",
        ):
            message = entry.get("message") or {}
            senderDid = (message.get("sender") or {}).get("did")
            if convoId and _upsert_message_from_raw(account_id, convoId, message):
                if logType == "chat.bsky.convo.defs#logAddReaction":
                    actorDid = ((entry.get("reaction") or {}).get("sender") or {}).get("did") or senderDid
                elif logType == "chat.bsky.convo.defs#logRemoveReaction":
                    actorDid = my_did
                else:
                    actorDid = senderDid
                if actorDid != my_did:
                    touchedConvoIds.add(convoId)
        elif logType == "chat.bsky.convo.defs#logDeleteMessage":
            message = entry.get("message") or {}
            messageId = message.get("id")
            if convoId and messageId:
                db.delete_message(account_id, convoId, messageId)
        elif logType in _STRUCTURAL_LOG_TYPES:
            needsFullResync = True
        # Unrecognized/future log $type: ignored here -- the next
        # full F5/manual sync still picks up whatever it was.

    if needsFullResync:
        try:
            sync_convos(client, account_id, my_did)
        except Exception as e:
            log.error(f"NVSky: structural-log fallback full sync failed: {e}")

    if touchedConvoIds:
        soundpack.play("new_message")
        # Refresh unread_count/last-message for every touched convo
        # via the cheap single-convo call, rather than a full
        # list_convos -- keeps ChatWindow's tree labels accurate.
        for convoId in touchedConvoIds:
            try:
                freshConvo = get_convo(client, convoId)
                if freshConvo is not None:
                    localConvo = db.get_convo(account_id, convoId)
                    status = (localConvo.get("status") if localConvo else None) or "accepted"
                    _store_convo(freshConvo, account_id, my_did, status)
            except Exception as e:
                log.error(f"NVSky: per-convo refresh after delta sync failed for {convoId}: {e}")

    if newCursor:
        db.set_chat_log_cursor(account_id, newCursor)

    return touchedConvoIds


def get_chat_client(client):
    """
    Wraps the client for chat.bsky.* calls. DMs are served by a separate service
    (did:web:api.bsky.chat), so every chat call must go through with_bsky_chat_proxy(). The account
    also needs chat enabled (a chat.bsky.actor.declaration record, normally created the first time
    Chat is opened in the official app).
    """
    return client.with_bsky_chat_proxy()


def send_message(client, convo_id: str, text: str, reply_to_message_id: str = None, embed_ref: dict = None):
    # Raw JSON: the response is a message union and is ignored (the caller resyncs the
    # conversation), and the request is a DotDict so no typed model is built.
    from atproto_client.models.dot_dict import DotDict

    dm = get_chat_client(client).chat.bsky.convo
    facets = build_facets(client, text)
    messageBody = {
        "text": text,
        "$type": "chat.bsky.convo.defs#messageInput",
    }
    if facets:
        messageBody["facets"] = facets
    if reply_to_message_id:
        messageBody["replyTo"] = {"messageId": reply_to_message_id}
    if embed_ref:
        messageBody["embed"] = {
            "$type": "app.bsky.embed.record",
            "record": {"uri": embed_ref["uri"], "cid": embed_ref["cid"]},
        }
    body = DotDict({
        "convoId": convo_id,
        "message": messageBody,
    })
    dm._client.invoke_procedure(
        "chat.bsky.convo.sendMessage", data=body,
        input_encoding="application/json", output_encoding="application/json",
    )


def mark_convo_read(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.update_read(data={"convo_id": convo_id})


def mark_all_convos_read(client):
    """Marks every conversation read (chat.bsky.convo.updateAllRead)."""
    get_chat_client(client).chat.bsky.convo.update_all_read(data={})


def get_convo_availability(client, member_dids: list):
    """
    Whether the given members can be messaged (chat.bsky.convo.getConvoAvailability). Used
    defensively: callers skip this pre-check if it fails.
    """
    response = get_chat_client(client).chat.bsky.convo.get_convo_availability(
        params={"members": member_dids}
    )
    return response


def get_or_create_convo_for_member(client, member_did: str):
    """
    The existing 1:1 conversation with this member, or a new one
    (chat.bsky.convo.getConvoForMembers). Raw JSON like the other conversation reads.
    """
    dm = get_chat_client(client).chat.bsky.convo
    params = models.ChatBskyConvoGetConvoForMembers.Params(members=[member_did])
    response = dm._client.invoke_query(
        "chat.bsky.convo.getConvoForMembers", params=params, output_encoding="application/json"
    )
    return response.content.get("convo") if isinstance(response.content, dict) else None


def _group_ns(client):
    return get_chat_client(client).chat.bsky.group


def create_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """
    Creates or updates a group's invite link (chat.bsky.group.createJoinLink); an existing link is
    updated rather than rejected. joinRule is "anyone" or "followedByOwner". Raw JSON.
    """
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.createJoinLink",
        data=DotDict({"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval}),
        input_encoding="application/json", output_encoding="application/json",
    )
    joinLink = response.content.get("joinLink") if isinstance(response.content, dict) else None
    return joinLink


def edit_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """Changes an existing invite link's join rule (chat.bsky.group.editJoinLink)."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.editJoinLink",
        data=DotDict({"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval}),
        input_encoding="application/json", output_encoding="application/json",
    )
    joinLink = response.content.get("joinLink") if isinstance(response.content, dict) else None
    return joinLink


def enable_join_link(client, convo_id: str):
    """Re-enables a disabled invite link."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.enableJoinLink", data=DotDict({"convoId": convo_id}),
        input_encoding="application/json", output_encoding="application/json",
    )
    joinLink = response.content.get("joinLink") if isinstance(response.content, dict) else None
    return joinLink


def disable_join_link(client, convo_id: str):
    """Disables a group's invite link."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.disableJoinLink", data=DotDict({"convoId": convo_id}),
        input_encoding="application/json", output_encoding="application/json",
    )


def edit_group(client, convo_id: str, name: str):
    """Renames a group (chat.bsky.group.editGroup). Raw JSON; returns the updated convo."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.editGroup", data=DotDict({"convoId": convo_id, "name": name}),
        input_encoding="application/json", output_encoding="application/json",
    )
    convo = response.content.get("convo") if isinstance(response.content, dict) else None
    return convo


def create_group(client, member_dids: list, name: str = None):
    """
    Creates a group conversation (chat.bsky.group.createGroup). Raw JSON with a DotDict body,
    because plain dicts have no model_dump_json. Returns the new convo dict.
    """
    from atproto_client.models.dot_dict import DotDict

    body = {"members": member_dids}
    if name:
        body["name"] = name
    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.createGroup", data=DotDict(body),
        input_encoding="application/json", output_encoding="application/json",
    )
    return response.content.get("convo") if isinstance(response.content, dict) else None


def add_group_members(client, convo_id: str, member_dids: list):
    """Adds members to a group (chat.bsky.group.addMembers)."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.addMembers",
        data=DotDict({"convoId": convo_id, "members": member_dids}),
        input_encoding="application/json", output_encoding="application/json",
    )


def remove_group_members(client, convo_id: str, member_dids: list):
    """Removes members from a group (chat.bsky.group.removeMembers)."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.removeMembers",
        data=DotDict({"convoId": convo_id, "members": member_dids}),
        input_encoding="application/json", output_encoding="application/json",
    )


def get_join_link_previews(client, codes: list):
    """
    Previews for invite codes (chat.bsky.group.getJoinLinkPreviews). Items are a union (a full
    preview, or only the code for a disabled or invalid link), so this is raw JSON.
    """
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_query(
        "chat.bsky.group.getJoinLinkPreviews", params=DotDict({"codes": codes}),
        output_encoding="application/json",
    )
    previews = response.content.get("joinLinkPreviews", []) if isinstance(response.content, dict) else []
    return previews


def request_join_group(client, code: str):
    """
    Asks to join a group by invite code (chat.bsky.group.requestJoin). Returns the response dict
    (status "joined" or pending).
    """
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.requestJoin", data=DotDict({"code": code}),
        input_encoding="application/json", output_encoding="application/json",
    )
    return response.content if isinstance(response.content, dict) else {}


def list_join_requests(client, convo_id: str):
    """Pending join requests of a group (first page only; chat.bsky.group.listJoinRequests)."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_query(
        "chat.bsky.group.listJoinRequests", params=DotDict({"convoId": convo_id}),
        output_encoding="application/json",
    )
    requests_ = response.content.get("requests", []) if isinstance(response.content, dict) else []
    return requests_


def approve_join_request(client, convo_id: str, member_did: str):
    """
    Approves a join request (chat.bsky.group.approveJoinRequest). Raw JSON: the output carries a
    convo.
    """
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.approveJoinRequest",
        data=DotDict({"convoId": convo_id, "member": member_did}),
        input_encoding="application/json", output_encoding="application/json",
    )
    return response.content.get("convo") if isinstance(response.content, dict) else None


def reject_join_request(client, convo_id: str, member_did: str):
    """Rejects a join request (chat.bsky.group.rejectJoinRequest)."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.rejectJoinRequest",
        data=DotDict({"convoId": convo_id, "member": member_did}),
        input_encoding="application/json", output_encoding="application/json",
    )


def withdraw_join_request(client, convo_id: str):
    """Withdraws the account's own pending request to join a group."""
    get_chat_client(client).chat.bsky.group.withdraw_join_request(data={"convo_id": convo_id})


def mark_join_requests_read(client, convo_id: str):
    """Marks a group's join requests as read."""
    get_chat_client(client).chat.bsky.group.update_join_requests_read(data={"convo_id": convo_id})


def mark_message_read(client, convo_id: str, message_id: str):
    """
    Marks everything up to this message read (chat.bsky.convo.updateRead with messageId). Called
    from a background thread to keep the server's unread_count in step with local reads; never shown
    in the UI.
    """
    get_chat_client(client).chat.bsky.convo.update_read(
        data={"convo_id": convo_id, "message_id": message_id}
    )

def mute_convo(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.mute_convo(data={"convo_id": convo_id})


def unmute_convo(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.unmute_convo(data={"convo_id": convo_id})


def leave_convo(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.leave_convo(data={"convo_id": convo_id})


def lock_convo(client, convo_id: str):
    """
    Locks a group. An owner must lock a group before leaving it (otherwise the server answers
    OwnerCannotLeave).
    """
    get_chat_client(client).chat.bsky.convo.lock_convo(data={"convo_id": convo_id})


def unlock_convo(client, convo_id: str):
    """Unlocks a group."""
    get_chat_client(client).chat.bsky.convo.unlock_convo(data={"convo_id": convo_id})


def accept_convo(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.accept_convo(data={"convo_id": convo_id})


def add_reaction(client, convo_id: str, message_id: str, value: str):
    """Adds an emoji reaction to a message (chat.bsky.convo.addReaction)."""
    get_chat_client(client).chat.bsky.convo.add_reaction(
        data={"convo_id": convo_id, "message_id": message_id, "value": value}
    )


def remove_reaction(client, convo_id: str, message_id: str, value: str):
    """Removes the account's emoji reaction from a message."""
    get_chat_client(client).chat.bsky.convo.remove_reaction(
        data={"convo_id": convo_id, "message_id": message_id, "value": value}
    )


def delete_message_for_self(client, convo_id: str, message_id: str):
    get_chat_client(client).chat.bsky.convo.delete_message_for_self(
        data={"convo_id": convo_id, "message_id": message_id}
    )


def sync_saved(client, account_id: int, cursor: str = None, limit: int = 50) -> str:
    """Syncs one page of bookmarks (getBookmarks) into the "saved" feed."""
    resp = client.app.bsky.bookmark.get_bookmarks(params={"cursor": cursor, "limit": limit})

    for bookmark in resp.bookmarks:
        # BookmarkView.item is the full PostView (author, text, counts, viewer state);
        # BookmarkView.subject is only a strongRef (uri/cid, no author).
        post = getattr(bookmark, "item", None) or getattr(bookmark, "subject", None)
        if post is None or not hasattr(post, "author"):
            # Likely a #notFoundPost / #blockedPost placeholder (the
            # bookmarked post was deleted, or the author blocked us) --
            # nothing useful to cache, skip it.
            continue
        try:
            _store_resolved_post(post, account_id)
            createdAt = (
                getattr(bookmark, "created_at", None)
                or getattr(bookmark, "createdAt", None)
                or post.indexed_at
            )
            db.upsert_feed_item(account_id, "saved", post.uri, createdAt)
        except Exception as e:
            log.error(f"NVSky: failed to store a bookmark: {e}")

    return resp.cursor


def sync_likes(client, account_id: int, cursor: str = None, limit: int = 50) -> str:
    """Own likes via listRecords (getActorLikes has no like time, so no usable ordering)."""
    resp = client.com.atproto.repo.list_records(
        params={"repo": client.me.did, "collection": "app.bsky.feed.like", "limit": limit, "cursor": cursor}
    )
    entries = []
    for record in resp.records:
        value = record.value
        subjectUri = _view_field(_view_field(value, "subject"), "uri")
        createdAt = _view_field(value, "createdAt") or _view_field(value, "created_at")
        if subjectUri and createdAt:
            entries.append((subjectUri, createdAt))
    resolved = resolve_posts(client, account_id, [uri for uri, _time in entries]) if entries else {}
    for uri, createdAt in entries:
        if uri in resolved:
            db.upsert_feed_item(account_id, "likes", uri, createdAt)
    return resp.cursor


def sync_timeline(client, account_id: int, cursor: str = None, limit: int = 50, feed_key: str = "home") -> str:
    resp = client.get_timeline(cursor=cursor, limit=limit)

    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, feed_key)
        except Exception as e:
            log.error(f"NVSky: failed to store a feed item: {e}")

    return resp.cursor


CONTENT_LABEL_KEYS = ["porn", "sexual", "nudity", "graphic-media"]
# Official app defaults for a label the account never set (graphic-media not verified).
CONTENT_LABEL_DEFAULT_VISIBILITIES = {
    "porn": "hide", "sexual": "warn", "nudity": "show", "graphic-media": "warn",
}
# Only these follow the master adult-content switch; nudity does not.
CONTENT_LABEL_ADULT_ONLY = {"porn", "sexual", "graphic-media"}


def label_setting(prefs: dict, label: str) -> str:
    return prefs.get(label) or CONTENT_LABEL_DEFAULT_VISIBILITIES.get(label, "warn")


def effective_label_visibility(prefs: dict, label: str) -> str:
    if label in CONTENT_LABEL_ADULT_ONLY and not prefs.get(ADULT_CONTENT_PREF_KEY, True):
        return "hide"
    return label_setting(prefs, label)
CONTENT_LABEL_PREF_TYPE = "app.bsky.actor.defs#contentLabelPref"
ADULT_CONTENT_PREF_TYPE = "app.bsky.actor.defs#adultContentPref"
# Stored inside the same label-prefs dict/cache; missing in an old cache = treat as enabled.
ADULT_CONTENT_PREF_KEY = "__adult_content_enabled"


def _extract_post_labels(post) -> list:
    """
    Label value strings (e.g. "porn", "graphic-media") a labeler or the author attached to `post`,
    or [].
    """
    labels = getattr(post, "labels", None) or []
    return [getattr(l, "val", None) for l in labels if getattr(l, "val", None)]


def _extract_embed_info(post):
    """
    Plain dict describing the embed for display AND for opening it
    (image/video/link URLs, not just alt text). Built from post.embed
    (the hydrated View server always sends) via plain attribute access
    only -- never .model_dump(), which crashes on some nested types in
    this SDK/pydantic combo. Returns None if there's no embed.

    recordWithMedia (quote post + attached media together) carries BOTH:
    the quoted record nested one level deeper under .record.record, AND
    the actual attached media (images/video/external) under .media with
    its own nested $type -- both get merged into the same info dict, so
    "View embed" still shows the attached media on a quote-with-media
    post, not just the quote text.
    """
    embedView = getattr(post, "embed", None)
    if embedView is None:
        return None

    embedType = _embed_type(embedView)
    info = {"$type": embedType}

    try:
        if embedType.startswith("app.bsky.embed.record"):
            recordView = getattr(embedView, "record", None)
            mediaView = getattr(embedView, "media", None)  # only set for recordWithMedia
            if mediaView is not None:
                recordView = getattr(recordView, "record", recordView)
                mediaType = _embed_type(mediaView)
                _fill_media_info(info, mediaView, mediaType)
            value = _view_field(recordView, "value")
            author = _view_field(recordView, "author")
            info["quoted_text"] = _view_field(value, "text")
            info["quoted_author_handle"] = _view_field(author, "handle")
        else:
            _fill_media_info(info, embedView, embedType)
    except Exception as e:
        # embedType tells which embed variant failed.
        log.info(f"NVSky: partial embed extraction failure for {post.uri} (embed type: {embedType}): {e}")

    return info


def _embed_type(view) -> str:
    # atproto >= 0.0.73 keeps the type string in py_type; older versions (and the raw-JSON
    # namespaces built by _dict_to_ns) keep it under "$type". An unresolved FieldInfo is not a str.
    for name in ("py_type", "$type"):
        value = getattr(view, name, None)
        if isinstance(value, str) and value:
            return value
    value = _view_field(view, "$type")
    return value if isinstance(value, str) else ""


def _fill_media_info(info: dict, mediaView, mediaType: str):
    """Adds image/video/external URL fields to `info` -- shared between
    a plain media embed and the .media portion of a recordWithMedia."""
    if "images" in mediaType:
        images = getattr(mediaView, "images", []) or []
        info["images"] = [
            {
                "alt": getattr(img, "alt", "") or "",
                "thumb_url": getattr(img, "thumb", None),
                "fullsize_url": getattr(img, "fullsize", None),
            }
            for img in images
        ]
    elif mediaType.startswith("app.bsky.embed.video"):
        info["video_url"] = getattr(mediaView, "playlist", None)
        info["video_thumb_url"] = getattr(mediaView, "thumbnail", None)
        info["video_alt"] = getattr(mediaView, "alt", None)
    elif mediaType.startswith("app.bsky.embed.external"):
        external = getattr(mediaView, "external", None)
        info["link_url"] = getattr(external, "uri", None)
        info["link_title"] = getattr(external, "title", None) or ""
    elif mediaType.startswith("app.bsky.embed.gallery"):
        info["images"] = [
            {
                "alt": _view_field(item, "alt") or "",
                "thumb_url": _view_field(item, "thumbnail"),
                "fullsize_url": _view_field(item, "fullsize"),
            }
            for item in (_view_field(mediaView, "items") or [])
        ]


def _view_field(obj, name):
    """
    getattr(obj, name, None) isn't safe when `obj` is a plain dict instead of a resolved model (a
    union member the SDK couldn't resolve): a name like "items" then gives dict's own .items()
    method instead of None. A callable result is treated as missing and the dict .get() path is
    used.
    """
    value = getattr(obj, name, None)
    if value is None or callable(value):
        getter = _dict_get(obj)
        value = getter(name) if getter is not None else None
    return value


def expand_link_facets(text: str, facets) -> str:
    """Replaces each link facet's visible text with its full URL. Official
    clients store long URLs shortened ("example.com/pa...") in the text and
    keep the real one only in the facet, which this add-on doesn't render.
    Handles dict, namespace, and typed-model facets."""
    if not text or not facets:
        return text
    try:
        data = text.encode("utf-8")
        edits = []
        for facet in facets:
            index = _view_field(facet, "index")
            start = _view_field(index, "byteStart")
            if start is None:
                start = _view_field(index, "byte_start")
            end = _view_field(index, "byteEnd")
            if end is None:
                end = _view_field(index, "byte_end")
            uri = None
            for feature in _view_field(facet, "features") or []:
                uri = _view_field(feature, "uri")
                if uri:
                    break
            if uri and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(data):
                edits.append((start, end, uri))
        previousStart = len(data)
        for start, end, uri in sorted(edits, reverse=True):
            if end > previousStart or data[start:end].decode("utf-8", errors="replace") == uri:
                continue
            data = data[:start] + uri.encode("utf-8") + data[end:]
            previousStart = start
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"NVSky: expanding link facets failed: {e}")
        return text


def _store_feed_item(item, account_id: int, feed_key: str = "home"):
    post = item.post
    author = post.author

    record = post.record
    recordGet = _dict_get(record)
    if recordGet is not None:
        text = recordGet("text", "") or ""
        created_at = recordGet("createdAt")
        facets_data = recordGet("facets")
    else:
        text = getattr(record, "text", "") or ""
        created_at = getattr(record, "created_at", None) or getattr(record, "createdAt", None)
        facets_data = None
    text = expand_link_facets(text, _view_field(record, "facets"))

    authorViewer = getattr(author, "viewer", None)
    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
        viewer_following=getattr(authorViewer, "following", None) if authorViewer else None,
        viewer_muted=bool(getattr(authorViewer, "muted", False)) if authorViewer else False,
        viewer_blocking=getattr(authorViewer, "blocking", None) if authorViewer else None,
        viewer_activity_subscription=getattr(authorViewer, "activity_subscription", None) is not None if authorViewer else False,
    )

    # ReasonPin (a pinned post in an author feed) has no "by"; only a repost does.
    repostBy = getattr(item.reason, "by", None) if item.reason is not None else None
    is_repost = repostBy is not None
    reposted_by_handle = repostBy.handle if is_repost else None
    reposted_by_display_name = getattr(repostBy, "display_name", None) if is_repost else None
    reposted_by_did = repostBy.did if is_repost else None
    # A repost's position in the feed is when it was RE-posted, not when
    # the underlying post was originally created -- using post.indexed_at
    # for feed ordering made old posts that just got reposted sort (and
    # paginate) as if they were old, breaking lazy-load pagination math
    # whenever a repost was involved.
    feedIndexedAt = item.reason.indexed_at if is_repost else post.indexed_at

    # posts/feed_items hold one row per uri, so another followed
    # account's repost of the same post (listed AFTER ours in the
    # timeline response) would overwrite our own repost framing.
    # viewer.repost confirms server-side that we still repost it.
    selfAccount = db.get_active_account()
    if is_repost and selfAccount is not None and selfAccount["id"] == account_id and reposted_by_did != selfAccount["did"]:
        postViewer = getattr(post, "viewer", None)
        if postViewer is not None and getattr(postViewer, "repost", None):
            existingSelf = db.get_post(post.uri)
            if (
                existingSelf is not None and existingSelf.get("is_repost")
                and existingSelf.get("reposted_by_did") == selfAccount["did"]
            ):
                reposted_by_did = existingSelf["reposted_by_did"]
                reposted_by_handle = existingSelf.get("reposted_by_handle")
                reposted_by_display_name = existingSelf.get("reposted_by_display_name")
                keptIndexedAt = db.get_feed_item_indexed_at(account_id, feed_key, post.uri)
                if keptIndexedAt:
                    feedIndexedAt = keptIndexedAt

    if not is_repost:
        # A sync response for a post the active account just reposted (recorded via Jetstream) can
        # come back with no `reason`, which would drop the repost framing and sink the entry to the
        # original post's older indexed_at. Un-reposting is handled only in itemActions
        # (_toggleRepost and _onRepostDone), so existing self-repost state is preserved here.
        existingPost = db.get_post(post.uri)
        account = db.get_active_account()
        if (
            existingPost is not None and existingPost.get("is_repost")
            and account is not None and account["id"] == account_id
            and existingPost.get("reposted_by_did") == account["did"]
        ):
            is_repost = True
            reposted_by_did = existingPost["reposted_by_did"]
            reposted_by_handle = existingPost.get("reposted_by_handle")
            reposted_by_display_name = existingPost.get("reposted_by_display_name")
            existingIndexedAt = db.get_feed_item_indexed_at(account_id, feed_key, post.uri)
            if existingIndexedAt:
                feedIndexedAt = existingIndexedAt
    reply = item.reply
    reply_parent_uri = reply.parent.uri if reply else None
    reply_to_author = getattr(getattr(reply, "parent", None), "author", None) if reply else None
    reply_to_did = getattr(reply_to_author, "did", None)
    reply_to_handle = getattr(reply_to_author, "handle", None)

    viewer = getattr(post, "viewer", None)
    viewer_like_uri = getattr(viewer, "like", None) if viewer else None
    viewer_repost_uri = getattr(viewer, "repost", None) if viewer else None
    viewer_bookmarked = bool(getattr(viewer, "bookmarked", False)) if viewer else False
    viewer_thread_muted = bool(getattr(viewer, "thread_muted", False)) if viewer else False

    embed_data = _extract_embed_info(post)
    quoted_text = embed_data.get("quoted_text") if embed_data else None
    quoted_author_handle = embed_data.get("quoted_author_handle") if embed_data else None

    db.upsert_post({
        "uri": post.uri,
        "cid": post.cid,
        "account_id": account_id,
        "author_did": author.did,
        "text": text,
        "created_at": created_at,
        "indexed_at": post.indexed_at,
        "like_count": post.like_count or 0,
        "repost_count": post.repost_count or 0,
        "reply_count": post.reply_count or 0,
        "reply_parent_uri": reply_parent_uri,
        "reply_to_did": reply_to_did,
        "reply_to_handle": reply_to_handle,
        "is_repost": int(is_repost),
        "reposted_by_handle": reposted_by_handle,
        "reposted_by_display_name": reposted_by_display_name,
        "reposted_by_did": reposted_by_did,
        "embed_json": json.dumps(embed_data) if embed_data else None,
        "facets_json": json.dumps(facets_data) if facets_data else None,
        "quoted_text": quoted_text,
        "quoted_author_handle": quoted_author_handle,
        "viewer_like_uri": viewer_like_uri,
        "viewer_repost_uri": viewer_repost_uri,
        "viewer_bookmarked": viewer_bookmarked,
        "viewer_thread_muted": viewer_thread_muted,
        "labels_json": json.dumps(_extract_post_labels(post)) or None,
    })
    db.upsert_feed_item(account_id, feed_key, post.uri, feedIndexedAt)


def _store_resolved_post(post, account_id: int):
    """Hydrates a standalone PostView (from getPosts -- no FeedViewPost
    wrapper, so no reason/reply context) into the `posts` cache, same
    fields _store_feed_item writes minus the feed-membership row.
    Lets Post action (Alt+A) work on a notification's underlying post
    exactly like it works on a Home feed post -- same cached shape,
    same viewer like/repost/bookmark state."""
    author = post.author

    record = post.record
    recordGet = _dict_get(record)
    if recordGet is not None:
        text = recordGet("text", "") or ""
        created_at = recordGet("createdAt")
        facets_data = recordGet("facets")
    else:
        text = getattr(record, "text", "") or ""
        created_at = getattr(record, "created_at", None) or getattr(record, "createdAt", None)
        facets_data = None
    text = expand_link_facets(text, _view_field(record, "facets"))

    replyParentUri = None
    replyToDid = None
    replyToHandle = None
    replyRef = _view_field(record, "reply")
    if replyRef is not None:
        parentRef = _view_field(replyRef, "parent")
        replyParentUri = _view_field(parentRef, "uri") if parentRef is not None else None
        if replyParentUri:
            replyToDid = replyParentUri.replace("at://", "").split("/")[0]
            cachedParentAuthor = db.get_author(replyToDid)
            replyToHandle = cachedParentAuthor["handle"] if cachedParentAuthor else None

    authorViewer = getattr(author, "viewer", None)
    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
        viewer_following=getattr(authorViewer, "following", None) if authorViewer else None,
        viewer_muted=bool(getattr(authorViewer, "muted", False)) if authorViewer else False,
        viewer_blocking=getattr(authorViewer, "blocking", None) if authorViewer else None,
        viewer_activity_subscription=getattr(authorViewer, "activity_subscription", None) is not None if authorViewer else False,
    )

    viewer = getattr(post, "viewer", None)
    viewer_like_uri = getattr(viewer, "like", None) if viewer else None
    viewer_repost_uri = getattr(viewer, "repost", None) if viewer else None
    viewer_bookmarked = bool(getattr(viewer, "bookmarked", False)) if viewer else False
    viewer_thread_muted = bool(getattr(viewer, "thread_muted", False)) if viewer else False

    embed_data = _extract_embed_info(post)
    quoted_text = embed_data.get("quoted_text") if embed_data else None
    quoted_author_handle = embed_data.get("quoted_author_handle") if embed_data else None

    db.upsert_post({
        "uri": post.uri,
        "cid": post.cid,
        "account_id": account_id,
        "author_did": author.did,
        "text": text,
        "created_at": created_at,
        "indexed_at": post.indexed_at,
        "like_count": post.like_count or 0,
        "repost_count": post.repost_count or 0,
        "reply_count": post.reply_count or 0,
        "reply_parent_uri": replyParentUri,
        "reply_to_did": replyToDid,
        "reply_to_handle": replyToHandle,
        "is_repost": 0,
        "reposted_by_did": None,
        "reposted_by_handle": None,
        "reposted_by_display_name": None,
        "embed_json": json.dumps(embed_data) if embed_data else None,
        "facets_json": json.dumps(facets_data) if facets_data else None,
        "quoted_text": quoted_text,
        "quoted_author_handle": quoted_author_handle,
        "viewer_like_uri": viewer_like_uri,
        "viewer_repost_uri": viewer_repost_uri,
        "viewer_bookmarked": viewer_bookmarked,
        "viewer_thread_muted": viewer_thread_muted,
        "labels_json": json.dumps(_extract_post_labels(post)) or None,
    })


def follow_starter_pack_members(client, full) -> int:
    """Follows every profile in list_items_sample (a sample, possibly not
    the whole pack) that the account doesn't follow yet. Compares against
    the real follows list, since the sample's viewer state can't be trusted.
    Returns the count followed."""
    alreadyFollowing = {f["did"] for f in get_follows(client, client.me.did)}
    count = 0
    for item in getattr(full, "list_items_sample", None) or []:
        did = item.subject.did
        if did in alreadyFollowing:
            continue
        follow_actor(client, did)
        alreadyFollowing.add(did)
        count += 1
    return count


def sync_feed_generator_page(client, account_id: int, feed_uri: str, cursor: str = None, limit: int = 50) -> str:
    """Same caching shape as sync_list_feed -- feed_key is the feed
    generator's own at:// uri. Used by FeedPreviewTabWindow's real
    "View feed" pagination (not a one-shot fetch anymore)."""
    resp = client.app.bsky.feed.get_feed(params={"feed": feed_uri, "limit": limit, "cursor": cursor})
    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, feed_uri)
        except Exception as e:
            log.error(f"NVSky: failed to store a feed generator item: {e}")
    return resp.cursor


def sync_search_page(
    client, account_id: int, feed_key: str, query: str, cursor: str = None, limit: int = 25,
    author: str = None, since: str = None, until: str = None, lang: str = None,
) -> str:
    """Search results cached like any other feed under a synthetic
    feed_key (see feedWindow._search_feed_key) -- gives real
    "fetch older" pagination for a pinned search tab instead of a
    one-shot 25 that got replaced on every refresh."""
    params = {"q": query, "sort": "latest", "cursor": cursor, "limit": limit}
    if author:
        params["author"] = author
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if lang:
        params["lang"] = [lang]
    resp = client.app.bsky.feed.search_posts(params=params)
    for post in resp.posts:
        try:
            _store_resolved_post(post, account_id)
            db.upsert_feed_item(account_id, feed_key, post.uri, post.indexed_at)
        except Exception as e:
            log.error(f"NVSky: failed to store a search result item: {e}")
    return resp.cursor


def get_starter_pack_full(client, uri: str):
    """Full starter pack view (creator, feeds, list_items_sample)."""
    response = client.app.bsky.graph.get_starter_pack(params={"starter_pack": uri})
    return response.starter_pack


def search_actors(client, query: str, cursor: str = None, limit: int = 25):
    return client.app.bsky.actor.search_actors(params={"q": query, "cursor": cursor, "limit": limit})


def search_starter_packs(client, query: str, cursor: str = None, limit: int = 25):
    return client.app.bsky.graph.search_starter_packs(params={"q": query, "cursor": cursor, "limit": limit})


def search_feeds(client, query: str, limit: int = 25):
    """Keyword feed search via get_popular_feed_generators (its "query" param filters results)."""
    params = {"limit": limit}
    if query:
        params["query"] = query
    return client.app.bsky.unspecced.get_popular_feed_generators(params=params)


class _RawJsonModel:
    """Prevents atproto's put_preferences from rehydrating an
    already-clean payload back into a Pydantic model (which re-triggers
    the same FieldInfo bug a second time) by handing invoke_procedure()
    something that already looks serialized."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump_json(self, exclude_none=True, by_alias=True, **kwargs):
        return json.dumps(self._payload, ensure_ascii=False, separators=(",", ":"))


def _pref_to_dict(pref):
    """
    Converts one entry from getPreferences() into a plain dict: normally a typed model dumped with
    model_dump. A preference the SDK couldn't resolve to a typed model (e.g. a labeler-specific
    label value) comes back as a DotDict, whose .model_dump resolves to None; it is converted with
    dict()/vars().
    """
    dumpMethod = getattr(pref, "model_dump", None)
    if callable(dumpMethod):
        return dumpMethod(mode="json", by_alias=True, exclude_none=True)
    # DotDict fallback: not a dict subclass despite its repr; dict()/vars() cover it.
    try:
        return dict(pref)
    except (TypeError, ValueError):
        pass
    try:
        return dict(vars(pref))
    except TypeError:
        pass
    raise TypeError(f"Don't know how to convert preference of type {type(pref).__name__} to a dict")


def _get_cleaned_preferences(client) -> list:
    """
    Fetches app.bsky.actor.getPreferences as a list of plain dicts. Every preferences read/write
    helper starts from this.
    """
    try:
        prefs_response = client.app.bsky.actor.get_preferences()
    except Exception as e:
        log.error("NVSky: getPreferences failed: %s", e, exc_info=True)
        raise

    prefs = []
    try:
        for pref in prefs_response.preferences:
            prefs.append(_pref_to_dict(pref))
    except Exception as e:
        log.error("NVSky: failed to serialize preferences: %s", e, exc_info=True)
        raise
    return prefs


def _put_preferences(client, prefs: list):
    """Sends `prefs` (a list of plain dicts, as returned by
    _get_cleaned_preferences) back via the low-level putPreferences
    procedure -- NOT client.app.bsky.actor.put_preferences(), which
    internally calls get_or_create() and rehydrates the payload into a
    fresh Pydantic model before serializing, hitting the FieldInfo bug
    again regardless of how clean the input dict was."""
    payload = {"preferences": prefs}
    try:
        raw_json_model = _RawJsonModel(payload)
        raw_client = client.app.bsky.actor._client
        raw_client.invoke_procedure(
            "app.bsky.actor.putPreferences",
            data=raw_json_model,
            input_encoding="application/json",
            output_encoding="application/json",
        )
    except Exception as e:
        log.error("NVSky: putPreferences failed: %s", e, exc_info=True)
        raise


def _find_saved_feeds_pref_v2(prefs: list, create_if_missing: bool = False) -> dict:
    saved_feeds_pref = next(
        (p for p in prefs if p.get("$type") == "app.bsky.actor.defs#savedFeedsPrefV2"),
        None,
    )
    if saved_feeds_pref is None and create_if_missing:
        saved_feeds_pref = {"$type": "app.bsky.actor.defs#savedFeedsPrefV2", "items": []}
        prefs.append(saved_feeds_pref)
    if saved_feeds_pref is not None and not isinstance(saved_feeds_pref.get("items"), list):
        saved_feeds_pref["items"] = []
    return saved_feeds_pref


def add_feed_to_saved(client, feed_uri: str) -> bool:
    if not feed_uri:
        return False

    prefs = _get_cleaned_preferences(client)
    saved_feeds_pref = _find_saved_feeds_pref_v2(prefs, create_if_missing=True)
    items = saved_feeds_pref["items"]

    for item in items:
        if isinstance(item, dict) and item.get("type") == "feed" and item.get("value") == feed_uri:
            return False

    fake_tid = "".join(random.choices(string.ascii_lowercase + "234567", k=13))
    items.append({
        "$type": "app.bsky.actor.defs#savedFeed",
        "id": fake_tid,
        "type": "feed",
        "value": feed_uri,
        "pinned": False,
    })

    # Keep the legacy V1 pref in sync too, same as before.
    saved_feeds_pref_v1 = next(
        (p for p in prefs if p.get("$type") == "app.bsky.actor.defs#savedFeedsPref"), None,
    )
    if saved_feeds_pref_v1 is not None:
        saved_list = saved_feeds_pref_v1.setdefault("saved", [])
        if feed_uri not in saved_list:
            saved_list.append(feed_uri)

    _put_preferences(client, prefs)
    return True


def get_saved_feeds_pref(client) -> list:
    """Returns the raw savedFeedsPrefV2 `items` list (each a dict with
    id/type/value/pinned) -- empty list if the pref doesn't exist yet.
    `type` is "feed" for a custom feed generator, "timeline" for the
    default Following feed, or "list" for a list-as-feed; the Feed
    manager settings panel only lets the user manage "feed" entries."""
    prefs = _get_cleaned_preferences(client)
    saved_feeds_pref = _find_saved_feeds_pref_v2(prefs, create_if_missing=False)
    if saved_feeds_pref is None:
        return []
    return list(saved_feeds_pref["items"])


def remove_feed_from_saved(client, feed_uri: str) -> bool:
    prefs = _get_cleaned_preferences(client)
    saved_feeds_pref = _find_saved_feeds_pref_v2(prefs, create_if_missing=False)
    if saved_feeds_pref is None:
        return False
    items = saved_feeds_pref["items"]
    newItems = [i for i in items if not (isinstance(i, dict) and i.get("type") == "feed" and i.get("value") == feed_uri)]
    if len(newItems) == len(items):
        return False
    saved_feeds_pref["items"] = newItems

    saved_feeds_pref_v1 = next(
        (p for p in prefs if p.get("$type") == "app.bsky.actor.defs#savedFeedsPref"), None,
    )
    if saved_feeds_pref_v1 is not None:
        saved_list = saved_feeds_pref_v1.get("saved")
        if isinstance(saved_list, list) and feed_uri in saved_list:
            saved_list.remove(feed_uri)

    _put_preferences(client, prefs)
    return True


def set_feed_pinned(client, feed_uri: str, pinned: bool) -> bool:
    prefs = _get_cleaned_preferences(client)
    saved_feeds_pref = _find_saved_feeds_pref_v2(prefs, create_if_missing=False)
    if saved_feeds_pref is None:
        return False
    found = False
    for item in saved_feeds_pref["items"]:
        if isinstance(item, dict) and item.get("type") == "feed" and item.get("value") == feed_uri:
            item["pinned"] = pinned
            found = True
    if not found:
        return False
    _put_preferences(client, prefs)
    return True


def reorder_saved_feeds(client, ordered_feed_uris: list) -> bool:
    """`ordered_feed_uris` is the desired new relative order for the
    "feed"-type items only -- "timeline"/"list" items keep their
    existing absolute slot in the items array, only the feed-type
    slots get refilled in the new order, so the overall list length
    and non-feed positions never change."""
    prefs = _get_cleaned_preferences(client)
    saved_feeds_pref = _find_saved_feeds_pref_v2(prefs, create_if_missing=False)
    if saved_feeds_pref is None:
        return False
    items = saved_feeds_pref["items"]

    byUri = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") == "feed":
            byUri[item.get("value")] = item

    newOrderedFeeds = [byUri[u] for u in ordered_feed_uris if u in byUri]
    if len(newOrderedFeeds) != len(byUri):
        log.error("NVSky: reorder_saved_feeds -- uri list didn't match existing feed items, aborting")
        return False

    it = iter(newOrderedFeeds)
    newItems = [next(it) if isinstance(item, dict) and item.get("type") == "feed" else item for item in items]
    saved_feeds_pref["items"] = newItems

    _put_preferences(client, prefs)
    return True


def get_feed_generators_info(client, uris: list) -> dict:
    """
    Batch-resolves feed generator URIs to display info (display_name/creator_handle/description) for
    the Feed manager. Chunks of 25 (getPosts' documented maximum; no separate limit is documented
    for this endpoint).
    """
    info = {}
    uris = [u for u in uris if u]
    for i in range(0, len(uris), 25):
        chunk = uris[i:i + 25]
        try:
            resp = client.app.bsky.feed.get_feed_generators(params={"feeds": chunk})
        except Exception as e:
            log.error("NVSky: getFeedGenerators failed: %s", e, exc_info=True)
            continue
        for feedGen in resp.feeds:
            creator = getattr(feedGen, "creator", None)
            info[feedGen.uri] = {
                "display_name": feedGen.display_name or "Feed",
                "creator_handle": creator.handle if creator else "",
                "description": feedGen.description or "",
            }
    return info

    
def resolve_posts(client, account_id: int, uris: list) -> dict:
    """Batch-resolves post URIs to text + author handle AND hydrates each
    into the local `posts` cache via _store_resolved_post(), so Post
    action has real data to work with. Chunks of 25 (the API's max)."""
    resolved = {}
    uris = list({u for u in uris if u})
    for i in range(0, len(uris), 25):
        chunk = uris[i:i + 25]
        try:
            resp = client.app.bsky.feed.get_posts(params={"uris": chunk})
        except Exception as e:
            log.info(f"NVSky: resolve_posts failed for a batch: {e}")
            continue
        for post in resp.posts:
            record = post.record
            recordGet = _dict_get(record)
            text = recordGet("text", "") if recordGet is not None else getattr(record, "text", "")
            resolved[post.uri] = {
                "text": text or "",
                "author_handle": post.author.handle,
            }
            try:
                _store_resolved_post(post, account_id)
            except Exception as e:
                log.info(f"NVSky: failed to cache resolved post {post.uri}: {e}")
    return resolved


def store_jetstream_repost(client, account_id: int, subject_uri: str, subject_cid: str, reposter_did: str, created_at: str = None):
    """
    Hydrates and threads a repost discovered via Jetstream (see
    jetstream.py) into the Home feed cache. Resolves the reposted post
    itself via resolve_posts (same path notifications/search already
    use), fetches/caches the reposting account's author info if not
    already cached, marks the post row as a repost, and inserts the
    feed_items row at the repost's OWN timestamp (not the original
    post's) -- matches _store_feed_item's own reasoning for feed
    ordering.
    """
    resolved = resolve_posts(client, account_id, [subject_uri])
    if subject_uri not in resolved:
        return
    reposter = db.get_author(reposter_did)
    if reposter is None:
        try:
            profile = get_profile(client, reposter_did)
            db.upsert_author(
                did=profile["did"], handle=profile["handle"],
                display_name=profile.get("display_name"), avatar_url=None,
                viewer_known=False,
            )
            reposter = db.get_author(reposter_did)
        except Exception as e:
            log.info(f"NVSky: jetstream -- could not resolve reposter profile {reposter_did}: {e}")
    reposterHandle = reposter["handle"] if reposter else None
    reposterDisplayName = reposter.get("display_name") if reposter else None
    db.mark_post_reposted(subject_uri, reposter_did, reposterHandle, reposterDisplayName)
    feedIndexedAt = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    db.upsert_feed_item(account_id, "home", subject_uri, feedIndexedAt)


NOTIFICATION_SYNC_MAX_PAGES = 20  # safety cap -- see sync_notifications' docstring


def sync_notifications(client, account_id: int, cursor: str = None, limit: int = 50) -> str:
    """
    Walks pages of listNotifications from the newest until it reaches the notification recorded by
    the previous resume sync (the uri of the newest notification then), or
    NOTIFICATION_SYNC_MAX_PAGES pages (a safety cap: an account with thousands of unread syncs what
    it can per call and picks up the rest on the next tick). Without a resume point, an account
    getting more than `limit` notifications between two syncs would silently lose the rest.

    Passing `cursor` walks exactly ONE page from there (lazy-load "fetch older") and does not touch
    the persisted resume cursor; only a fresh sync (cursor=None) consults and updates it.
    """
    isResumeSync = cursor is None
    resumeUri = db.get_notification_sync_cursor(account_id) if isResumeSync else None

    newestUriThisSync = None
    lastPageCursor = None
    pageCursor = cursor
    pagesWalked = 0
    reachedKnownNotification = False

    while True:
        resp = client.app.bsky.notification.list_notifications(params={"cursor": pageCursor, "limit": limit})
        lastPageCursor = resp.cursor
        if newestUriThisSync is None and resp.notifications:
            newestUriThisSync = resp.notifications[0].uri

        urisToResolve = []
        for notif in resp.notifications:
            if notif.reason in ("reply", "mention", "quote", "subscribed-post"):
                urisToResolve.append(notif.uri)
            elif notif.reason in ("like", "repost") and notif.reason_subject:
                urisToResolve.append(notif.reason_subject)
            elif notif.reason in ("like-via-repost", "repost-via-repost"):
                viaSubject = _notification_record_subject(notif) or notif.reason_subject
                if viaSubject:
                    urisToResolve.append(viaSubject)

        resolvedSubjects = resolve_posts(client, account_id, urisToResolve) if urisToResolve else {}

        for notif in resp.notifications:
            if isResumeSync and resumeUri and notif.uri == resumeUri:
                reachedKnownNotification = True
                break
            try:
                _store_notification(notif, account_id, resolvedSubjects)
            except Exception as e:
                log.error(f"NVSky: failed to store a notification: {e}")

        pagesWalked += 1
        if not isResumeSync:
            # Caller-driven lazy-load pagination -- one page only,
            # exactly like before this change.
            break
        if reachedKnownNotification or not resp.cursor or pagesWalked >= NOTIFICATION_SYNC_MAX_PAGES:
            break
        pageCursor = resp.cursor

    if isResumeSync and newestUriThisSync:
        db.set_notification_sync_cursor(account_id, newestUriThisSync)

    # Bluesky's server only supports one "seen up to this point in time"
    # watermark (no per-notification read state) -- this is what clears
    # the unread badge in the official app and any other client reading
    # the same account, separate from NVSky's own local is_read state.
    try:
        seenAt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        client.app.bsky.notification.update_seen({"seen_at": seenAt})
    except Exception as e:
        log.info(f"NVSky: updateSeen failed (non-fatal): {e}")

    return lastPageCursor


def _notification_record_subject(notif):
    # A like/repost record always carries the liked/reposted post in record.subject.uri.
    return _view_field(_view_field(notif.record, "subject"), "uri")


def _store_notification(notif, account_id: int, resolvedSubjects: dict):
    author = notif.author
    authorViewer = getattr(author, "viewer", None)
    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
        viewer_following=getattr(authorViewer, "following", None) if authorViewer else None,
        viewer_muted=bool(getattr(authorViewer, "muted", False)) if authorViewer else False,
        viewer_blocking=getattr(authorViewer, "blocking", None) if authorViewer else None,
        viewer_activity_subscription=getattr(authorViewer, "activity_subscription", None) is not None if authorViewer else False,
    )

    reason = notif.reason
    subjectText = ""
    subjectAuthorHandle = None

    if reason in ("reply", "mention", "quote", "subscribed-post"):
        # For these, the notification's own record/uri IS the new post
        # the author just wrote -- that's also the actionable post for
        # Post action (Alt+A). subscribed-post is the activity-
        # subscription bell-icon feature -- same shape as reply/mention/
        # quote (a real new post), just no reply/mention/quote context.
        subjectUri = notif.uri
        record = notif.record
        recordGet = _dict_get(record)
        subjectText = (recordGet("text", "") if recordGet is not None else getattr(record, "text", "")) or ""
        subjectAuthorHandle = author.handle
    elif reason in ("like", "repost"):
        # For these, the notification's own record is just the Like/
        # Repost record (no text, not something you can reply to) --
        # reasonSubject is the actual post that got liked/reposted, and
        # that's the actionable one for Post action.
        subjectUri = notif.reason_subject
    elif reason in ("like-via-repost", "repost-via-repost"):
        subjectUri = _notification_record_subject(notif) or notif.reason_subject
    else:
        # "follow" has no post at all -- User action (Alt+U) only.
        subjectUri = None

    # Prefer the freshly-resolved copy (sync_notifications batch-
    # resolved subjectUri above, which also cached the full post for
    # Post action) over the notif.record-derived text -- same text
    # either way, but this is the only source for like/repost (which
    # has no text of its own).
    resolved = resolvedSubjects.get(subjectUri) if subjectUri else None
    if resolved:
        subjectText = resolved["text"]
        subjectAuthorHandle = resolved["author_handle"]

    db.upsert_notification({
        "account_id": account_id,
        "uri": notif.uri,
        "cid": notif.cid,
        "reason": reason,
        "reason_subject": notif.reason_subject,
        "subject_uri": subjectUri,
        "author_did": author.did,
        "indexed_at": notif.indexed_at,
        "subject_text": subjectText,
        "subject_author_handle": subjectAuthorHandle,
    })


MAX_BLOB_BYTES = 1_000_000  # Bluesky's hard limit on any single blob


def _compress_image_for_blob(image_path: str, max_bytes: int = MAX_BLOB_BYTES) -> str:
    """Bluesky rejects any blob over max_bytes -- hit this for an
    auto-fetched link-preview thumbnail (1024x576 PNG, 1,032,450 bytes,
    just over the 1,000,000 limit) but the same cap applies to avatar/
    banner/attached images too, so this is called from the one shared
    upload path (_upload_blob_dict) rather than patched per call site.
    Returns image_path unchanged if it's already small enough or if
    Pillow/compression isn't available; otherwise re-encodes as JPEG,
    lowering quality and then dimensions until it fits, and returns a
    NEW temp file path (the original at image_path is left untouched)."""
    try:
        if os.path.getsize(image_path) <= max_bytes:
            return image_path
    except OSError:
        return image_path

    try:
        from PIL import Image
    except ImportError:
        log.error("NVSky: Pillow unavailable, cannot shrink oversized image for upload")
        return image_path

    try:
        img = Image.open(image_path)
        img.load()
    except Exception as e:
        log.error(f"NVSky: could not open oversized image to shrink it: {e}")
        return image_path

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    fd, out_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)

    quality = 85
    scale = 1.0
    while True:
        working = img if scale >= 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS,
        )
        working.save(out_path, "JPEG", quality=quality, optimize=True)
        if os.path.getsize(out_path) <= max_bytes or scale < 0.1:
            break
        if quality > 40:
            quality -= 15
        else:
            scale *= 0.8

    return out_path


def _upload_blob_dict(client, image_path: str) -> dict:
    originalPath = image_path
    image_path = _compress_image_for_blob(image_path)
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    if image_path != originalPath:
        try:
            os.remove(image_path)
        except OSError:
            pass
    upload = client.com.atproto.repo.upload_blob(image_bytes)
    blob = upload.blob
    return {
        "$type": "blob",
        "ref": {"$link": blob.ref.link},
        "mimeType": blob.mime_type,
        "size": blob.size,
    }


VIDEO_EXTENSIONS = (".mp4", ".mpeg", ".mpg", ".mov", ".webm")
# Current Bluesky video limits (10 minutes / 300MB since 2026-08-26); they have changed before. The
# server is the final authority: this is only a cheap pre-check.
VIDEO_MAX_DURATION_SECONDS = 600
VIDEO_MAX_BYTES = 300 * 1_000_000

_VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mov": "video/quicktime", ".webm": "video/webm",
}


class VideoUploadCancelled(Exception):
    pass


def _find_isobmff_box(f, target_type: bytes):
    """Scans top-level boxes of an ISOBMFF (MP4/MOV) file for
    `target_type` (e.g. b"moov"), returns its content bytes or None.
    Reads only box headers via seeks, not the whole file -- a 300MB
    video shouldn't be fully loaded just to check its duration."""
    f.seek(0, 2)
    fileSize = f.tell()
    pos = 0
    while pos + 8 <= fileSize:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[0:4])[0]
        boxType = header[4:8]
        headerSize = 8
        if size == 1:
            extSize = f.read(8)
            if len(extSize) < 8:
                break
            size = struct.unpack(">Q", extSize)[0]
            headerSize = 16
        elif size == 0:
            size = fileSize - pos
        if size < headerSize:
            break
        if boxType == target_type:
            f.seek(pos + headerSize)
            return f.read(size - headerSize)
        pos += size
    return None


def _read_mp4_duration_seconds(video_path: str):
    """Best-effort duration for MP4/MOV via the mvhd box -- no library.
    Returns None (not an error) for anything this simple parser can't
    read (WebM, unusual atom layout); callers should skip the duration
    check rather than block the upload in that case."""
    try:
        with open(video_path, "rb") as f:
            moov = _find_isobmff_box(f, b"moov")
            if moov is None:
                return None
            pos = 0
            while pos + 8 <= len(moov):
                size = struct.unpack(">I", moov[pos:pos + 4])[0]
                boxType = moov[pos + 4:pos + 8]
                if size < 8 or pos + size > len(moov):
                    break
                content = moov[pos + 8:pos + size]
                if boxType == b"mvhd":
                    version = content[0]
                    if version == 1:
                        timescale = struct.unpack(">I", content[20:24])[0]
                        duration = struct.unpack(">Q", content[24:32])[0]
                    else:
                        timescale = struct.unpack(">I", content[12:16])[0]
                        duration = struct.unpack(">I", content[16:20])[0]
                    return duration / timescale if timescale else None
                pos += size
    except Exception:
        return None
    return None


def validate_video_file(video_path: str) -> dict:
    """Best-effort pre-upload check -- {"ok": True} or {"ok": False,
    "message": ...}. Server has the final say; this just catches the
    cheap, obvious rejects before spending time on an upload that
    would fail anyway."""
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        # Translators: Shown when a video file's format isn't supported. First {} is the extension (or "no extension"), second {} is the accepted list.
        return {
            "ok": False,
            "message": _("Unsupported video format ({}). Bluesky accepts: {}.").format(
                ext or _("no extension"), ", ".join(VIDEO_EXTENSIONS)
            ),
        }

    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        # Translators: Shown when a video file can't be read. {} is the error message.
        return {"ok": False, "message": _("Could not read file: {}").format(e)}
    if size > VIDEO_MAX_BYTES:
        # Translators: Shown when a video is too large. First {} is its size in MB, second {} is the limit in MB.
        return {
            "ok": False,
            "message": _("Video is {}MB, over Bluesky's current {}MB limit.").format(
                f"{size / 1_000_000:.0f}", f"{VIDEO_MAX_BYTES / 1_000_000:.0f}"
            ),
        }

    if ext in (".mp4", ".mov"):
        duration = _read_mp4_duration_seconds(video_path)
        if duration is not None and duration > VIDEO_MAX_DURATION_SECONDS:
            # Translators: Shown when a video is too long. First {} is its length in seconds, second {} is the limit in seconds.
            return {
                "ok": False,
                "message": _("Video is {}s long, over Bluesky's current {}s limit.").format(
                    f"{duration:.0f}", VIDEO_MAX_DURATION_SECONDS
                ),
            }

    return {"ok": True}


def _get_pds_service_did(client) -> str:
    """
    The user's own PDS service DID (did:web:<pds-domain>), used as
    getServiceAuth's `aud` for uploadVideo -- NOT video.bsky.app's
    (that gives a 401). Read from the session's pds_endpoint.
    """
    session = getattr(client, "_session", None)
    pdsEndpoint = getattr(session, "pds_endpoint", None) if session is not None else None
    if not pdsEndpoint:
        pdsEndpoint = "https://bsky.social"  # SDK's own documented default
    pdsHost = urllib.parse.urlparse(pdsEndpoint).netloc
    return f"did:web:{pdsHost}"


def _get_upload_video_auth(client) -> str:
    """
    Service auth for app.bsky.video.uploadVideo only: aud is the user's own PDS (not video.bsky.app)
    and lxm is always "com.atproto.repo.uploadBlob". getUploadLimits/getJobStatus need a different
    aud/lxm, see _get_video_query_auth.
    """
    audDid = _get_pds_service_did(client)
    resp = client.com.atproto.server.get_service_auth(
        params={"aud": audDid, "lxm": "com.atproto.repo.uploadBlob"}
    )
    return resp.token


def _get_video_query_auth(client, lxm: str) -> str:
    """
    Service auth for app.bsky.video.getUploadLimits / getJobStatus: aud is video.bsky.app itself
    (not the PDS, unlike uploadVideo) and lxm is the endpoint being called.
    """
    resp = client.com.atproto.server.get_service_auth(
        params={"aud": "did:web:video.bsky.app", "lxm": lxm}
    )
    return resp.token


def get_video_upload_limits(client) -> dict:
    """Raw HTTP to video.bsky.app. Returns {"canUpload", "message",
    "remainingDailyBytes", "remainingDailyVideos"}."""
    token = _get_video_query_auth(client, "app.bsky.video.getUploadLimits")
    request = urllib.request.Request(
        "https://video.bsky.app/xrpc/app.bsky.video.getUploadLimits",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def upload_video(client, video_path: str, progress_callback=None, cancel_event=None) -> dict:
    """
    Uploads a video and polls until Bluesky's processing job finishes,
    returning the blob dict (same shape _upload_blob_dict returns for
    images -- usable directly in a post embed).

    Raw HTTP (uploadVideo + getJobStatus), not the typed SDK.

    progress_callback(state, extra), if given, is called with
    "uploading" once the file is sent, then the job's own state string
    (e.g. "JOB_STATE_ENCODING") on every poll -- no byte-level upload
    progress is available. cancel_event (threading.Event), if given,
    is checked between polls; the initial upload POST itself can't be
    interrupted mid-flight since it's one blocking call, only the
    polling phase can actually stop.
    """
    ext = os.path.splitext(video_path)[1].lower()
    mimeType = _VIDEO_MIME_TYPES.get(ext, "video/mp4")

    token = _get_upload_video_auth(client)
    with open(video_path, "rb") as f:
        videoBytes = f.read()

    if progress_callback:
        progress_callback("uploading", None)

    params = urllib.parse.urlencode({"did": client.me.did, "name": os.path.basename(video_path)})
    request = urllib.request.Request(
        f"https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?{params}",
        data=videoBytes, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": mimeType,
            "Content-Length": str(len(videoBytes)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            jobStatus = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Uploading the same video bytes again isn't a failure: the server answers 409 with a
            # JOB_STATE_COMPLETED job status (and a usable blob) in the error body. HTTPError is
            # readable as a response here.
            jobStatus = json.loads(e.read().decode("utf-8"))
        else:
            raise

    if cancel_event is not None and cancel_event.is_set():
        raise VideoUploadCancelled()

    blob = jobStatus.get("blob")
    if blob:
        return blob
    jobId = jobStatus.get("jobId")
    if not jobId:
        # Translators: Shown when the server's video upload response has no blob and no job ID.
        raise RuntimeError(_("Video upload returned neither a blob nor a job ID."))

    pollToken = _get_video_query_auth(client, "app.bsky.video.getJobStatus")
    pollParams = urllib.parse.urlencode({"jobId": jobId})
    pollUrl = f"https://video.bsky.app/xrpc/app.bsky.video.getJobStatus?{pollParams}"

    for _attempt in range(200):  # ~10 min at 3s intervals
        if cancel_event is not None and cancel_event.is_set():
            raise VideoUploadCancelled()
        time.sleep(3)
        pollRequest = urllib.request.Request(pollUrl, headers={"Authorization": f"Bearer {pollToken}"})
        with urllib.request.urlopen(pollRequest, timeout=15) as response:
            status = json.loads(response.read().decode("utf-8"))
        jobStatus = status.get("jobStatus", status)
        state = jobStatus.get("state", "")
        if progress_callback:
            progress_callback(state, jobStatus.get("progress"))
        if state == "JOB_STATE_COMPLETED":
            blob = jobStatus.get("blob")
            if blob:
                return blob
            # Translators: Shown when video processing finished but the server returned no video.
            raise RuntimeError(_("Video processing completed but returned no blob."))
        if state == "JOB_STATE_FAILED":
            # Translators: Fallback shown when video processing fails with no server message.
            raise RuntimeError(jobStatus.get("error") or jobStatus.get("message") or _("Video processing failed."))

    # Translators: Shown when video processing takes too long.
    raise RuntimeError(_("Video processing timed out."))


def _get_profile_record(client) -> dict:
    """
    Fetches the raw app.bsky.actor.profile record (rkey "self") as a
    plain dict -- needed before any edit, since putRecord replaces the
    WHOLE record and we don't want to blank out fields (like avatar)
    the person isn't touching right now.
    """
    try:
        resp = client.com.atproto.repo.get_record(
            params={"repo": client.me.did, "collection": "app.bsky.actor.profile", "rkey": "self"}
        )
        record = resp.value
        recordGet = _dict_get(record)
        if recordGet is not None:
            return dict(record)
        pinnedPostObj = getattr(record, "pinned_post", None) or getattr(record, "pinnedPost", None)
        pinnedPost = None
        if pinnedPostObj is not None:
            pinnedPost = {"uri": getattr(pinnedPostObj, "uri", None), "cid": getattr(pinnedPostObj, "cid", None)}
        return {
            "$type": "app.bsky.actor.profile",
            "displayName": getattr(record, "display_name", None),
            "description": getattr(record, "description", None),
            "avatar": getattr(record, "avatar", None),
            "banner": getattr(record, "banner", None),
            "pinnedPost": pinnedPost,
        }
    except Exception:
        # Some accounts have never had a profile record written at all --
        # start from an empty one rather than failing the edit outright.
        return {"$type": "app.bsky.actor.profile"}


def _put_profile_record(client, record: dict):
    record["$type"] = "app.bsky.actor.profile"
    _put_record(client, client.me.did, "app.bsky.actor.profile", record, "self")


def update_profile_text(client, display_name: str, description: str):
    record = _get_profile_record(client)
    record["displayName"] = display_name
    record["description"] = description
    _put_profile_record(client, record)


def update_profile_avatar(client, image_path: str):
    record = _get_profile_record(client)
    record["avatar"] = _upload_blob_dict(client, image_path)
    _put_profile_record(client, record)


def update_profile_banner(client, image_path: str):
    record = _get_profile_record(client)
    record["banner"] = _upload_blob_dict(client, image_path)
    _put_profile_record(client, record)


def get_pinned_post_uri(client):
    record = _get_profile_record(client)
    pinned = record.get("pinnedPost") if isinstance(record, dict) else None
    if isinstance(pinned, dict):
        return pinned.get("uri")
    return None


def pin_post_to_profile(client, post_uri: str, post_cid: str):
    record = _get_profile_record(client)
    record["pinnedPost"] = {"uri": post_uri, "cid": post_cid}
    _put_profile_record(client, record)


def unpin_post_from_profile(client):
    record = _get_profile_record(client)
    record.pop("pinnedPost", None)
    _put_profile_record(client, record)


URL_REGEX = re.compile(r'https?://[^\s<>"\']+')
MENTION_REGEX = re.compile(r'(?<!\w)@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})\b')

def find_all_urls(text: str) -> list:
    """Every URL in text, in order, trailing punctuation stripped."""
    return [m.group(0).rstrip(').,!?') for m in URL_REGEX.finditer(text)]


def build_facets(client, text: str) -> list:
    """
    Scans post text for @handle mentions and http(s) URLs and builds the
    RichText facets Bluesky needs to render them as clickable/highlighted
    text -- entirely client-side work, the server does none of it.
    Byte offsets (not character offsets) are required by the lexicon,
    since facets index into the UTF-8 encoding of the text, not Python
    string indices.
    """
    facets = []

    for m in URL_REGEX.finditer(text):
        url = m.group(0).rstrip(').,!?')
        startByte = len(text[:m.start()].encode("utf-8"))
        endByte = startByte + len(url.encode("utf-8"))
        facets.append({
            "index": {"byteStart": startByte, "byteEnd": endByte},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })

    for m in MENTION_REGEX.finditer(text):
        handle = m.group(1)
        try:
            did = client.com.atproto.identity.resolve_handle(params={"handle": handle}).did
        except Exception:
            continue  # not a real handle -- leave as plain text, same as bsky.app does
        startByte = len(text[:m.start()].encode("utf-8"))
        endByte = startByte + len(f"@{handle}".encode("utf-8"))
        facets.append({
            "index": {"byteStart": startByte, "byteEnd": endByte},
            "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
        })

    return facets


def fetch_link_card(url: str) -> dict:
    """
    Scrapes the target page's OpenGraph tags for a link-card preview, the same job bsky.app's
    compose box does client-side. Fetches the page directly with a browser-like User-Agent (some
    sites block non-browser requests). Tags are extracted with regexes (no HTML parser is bundled):
    this works for well-formed pages and can miss unusual tag ordering. A missing title, description
    or image just means a plainer card, never a failure.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        html = response.read(500_000).decode("utf-8", errors="replace")

    def _og(prop):
        m = re.search(
            rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']*)["\']', html, re.IGNORECASE
        )
        if not m:
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:{prop}["\']', html, re.IGNORECASE
            )
        return m.group(1) if m else None

    title = _og("title")
    description = _og("description")
    image = _og("image")
    if image:
        image = urllib.parse.urljoin(url, image)
        # A page must not be able to point the thumbnail download at file:/ftp: etc.
        if urllib.parse.urlparse(image).scheme.lower() not in ("http", "https"):
            image = None

    # Falls back to the domain, not the full raw URL, when og:title
    # can't be scraped (JS-rendered pages, unusual meta tag ordering,
    # etc) -- a nicer-looking card than the literal URL string, though
    # still not a real title. Once posted this can't be edited, so
    # domain is at least a saner permanent fallback.
    fallbackTitle = urllib.parse.urlparse(url).netloc or url
    return {"uri": url, "title": title or fallbackTitle, "description": description or "", "image_url": image}


def _strip_none_values(record: dict) -> dict:
    """
    Drops None-valued top-level fields: the raw write has no exclude_none step, and the server
    rejects a literal null for a missing blob (e.g. no avatar). Shallow on purpose; nested optional
    fields are omitted when the record is built.
    """
    return {k: v for k, v in record.items() if v is not None}


def _create_record(client, repo: str, collection: str, record: dict, rkey: str = None):
    """
    Raw JSON write for com.atproto.repo.createRecord: the record is sent exactly as
    built, with no typed model in between, so unknown fields survive and nothing is
    rewritten. Same path as send_message.
    """
    from atproto_client.models.dot_dict import DotDict

    body = {"repo": repo, "collection": collection, "record": _strip_none_values(record)}
    if rkey:
        body["rkey"] = rkey
    response = client.com.atproto.repo._client.invoke_procedure(
        "com.atproto.repo.createRecord", data=DotDict(body),
        input_encoding="application/json", output_encoding="application/json",
    )
    content = response.content if isinstance(response.content, dict) else {}
    return _dict_to_ns(content)


def _put_record(client, repo: str, collection: str, record: dict, rkey: str):
    """Same fix as _create_record, for com.atproto.repo.putRecord."""
    from atproto_client.models.dot_dict import DotDict

    response = client.com.atproto.repo._client.invoke_procedure(
        "com.atproto.repo.putRecord",
        data=DotDict({"repo": repo, "collection": collection, "rkey": rkey, "record": _strip_none_values(record)}),
        input_encoding="application/json", output_encoding="application/json",
    )
    content = response.content if isinstance(response.content, dict) else {}
    return _dict_to_ns(content)


# ---------------- posting ----------------

def create_post(client, text: str, attachments: list = None, reply_ref: dict = None,
                 quote_ref: dict = None, link_card: dict = None, facets: list = None,
                 video: dict = None, self_labels: list = None):
    """
    Creates a post. Covers every combo: plain text, images, reply,
    quote, quote-with-images, and a link-card preview (external embed).

    attachments: list of {"path", "alt"} dicts, up to 4 images.
    reply_ref: {"parent": {"uri","cid"}, "root": {"uri","cid"}} -- see
    get_reply_refs().
    quote_ref: {"uri", "cid"} of the post being quoted.
    link_card: result of fetch_link_card(), optionally with a
    "thumb_blob" key added (see _upload_blob_dict). Ignored if
    attachments or quote_ref are also set -- a post can't have images
    AND an external-link embed at the same time, and a quote-with-link-
    card isn't something the official app does either, so quote/images
    take priority the same way they already did before this existed.
    facets: pre-built via build_facets() -- passed straight through.
    self_labels: list of label value strings (subset of
    CONTENT_LABEL_KEYS) the author is voluntarily attaching to their
    OWN post via com.atproto.label.defs#selfLabels -- distinct from
    moderation labels a labeler service attaches server-side, but
    lands in the same post.labels array once synced back, so the
    existing _extract_post_labels/warn-hide rendering picks these up
    automatically with no separate handling needed.
    """
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": client.get_current_time_iso(),
    }

    if facets:
        record["facets"] = facets

    if self_labels:
        record["labels"] = {
            "$type": "com.atproto.label.defs#selfLabels",
            "values": [{"val": label} for label in self_labels],
        }

    if reply_ref:
        record["reply"] = {
            "root": {"uri": reply_ref["root"]["uri"], "cid": reply_ref["root"]["cid"]},
            "parent": {"uri": reply_ref["parent"]["uri"], "cid": reply_ref["parent"]["cid"]},
        }

    embed = None
    if attachments:
        images_field = [
            {"image": _upload_blob_dict(client, att["path"]), "alt": att.get("alt") or ""}
            for att in attachments
        ]
        embed = {"$type": "app.bsky.embed.images", "images": images_field}
    elif video:
        embed = {"$type": "app.bsky.embed.video", "video": video["blob"]}
        if video.get("alt"):
            embed["alt"] = video["alt"]

    if quote_ref:
        quote_embed = {
            "$type": "app.bsky.embed.record",
            "record": {"uri": quote_ref["uri"], "cid": quote_ref["cid"]},
        }
        if embed:
            embed = {"$type": "app.bsky.embed.recordWithMedia", "record": quote_embed, "media": embed}
        else:
            embed = quote_embed
    elif not embed and link_card:
        external = {
            "uri": link_card["uri"],
            "title": link_card.get("title") or link_card["uri"],
            "description": link_card.get("description") or "",
        }
        if link_card.get("thumb_blob"):
            external["thumb"] = link_card["thumb_blob"]
        embed = {"$type": "app.bsky.embed.external", "external": external}

    if embed:
        record["embed"] = embed

    resp = _create_record(client, client.me.did, "app.bsky.feed.post", record)
    return {"uri": resp.uri, "cid": resp.cid, "created_at": record["createdAt"], "text": text}



def get_reply_refs(client, post_uri: str, post_cid: str, is_already_reply: bool) -> dict:
    """
    Builds the {parent, root} strongRefs a NEW reply to (post_uri, cid)
    needs. parent is always the post itself. root is the same post if
    it's top-level; otherwise walks the reply chain via the public
    AppView (see _fetch_thread_json) to find the actual thread root.
    """
    parent_ref = {"uri": post_uri, "cid": post_cid}
    if not is_already_reply:
        return {"parent": parent_ref, "root": parent_ref}

    raw = _fetch_thread_json(post_uri, depth=0, parent_height=100, client=client)
    node = _dict_to_ns(raw.get("thread", raw))
    root_ref = parent_ref
    walker = node
    while getattr(walker, "parent", None) is not None:
        walker = walker.parent
        p = getattr(walker, "post", None)
        if p is not None:
            root_ref = {"uri": p.uri, "cid": p.cid}
    return {"parent": parent_ref, "root": root_ref}

def set_threadgate(client, post_uri: str, state: str, rules: set = None):
    """
    Sets who can reply to one of YOUR OWN posts via an
    app.bsky.feed.threadgate record (rkey must match the post's own
    rkey). state: "everyone" (deletes any existing threadgate -- no
    record means no gate), "nobody" (empty allow list), "custom" (allow
    list built from `rules`, a subset of {"followers", "following",
    "mentioned"}). List-based rules aren't supported yet -- no list
    management UI exists in NVSky, and state="custom" will overwrite
    any list-based rules a previous session had set on this post.
    """
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey

    if state == "everyone":
        try:
            client.com.atproto.repo.delete_record(
                data={"repo": client.me.did, "collection": "app.bsky.feed.threadgate", "rkey": rkey}
            )
        except Exception:
            pass  # no threadgate existed -- already "everyone", nothing to do
        return

    if state == "nobody":
        allow = []
    elif state == "custom":
        rules = rules or set()
        ruleTypeMap = {
            "followers": "app.bsky.feed.threadgate#followerRule",
            "following": "app.bsky.feed.threadgate#followingRule",
            "mentioned": "app.bsky.feed.threadgate#mentionRule",
        }
        allow = [{"$type": ruleTypeMap[r]} for r in rules if r in ruleTypeMap]
    else:
        raise ValueError(f"Unknown threadgate state: {state}")

    _put_record(client, client.me.did, "app.bsky.feed.threadgate", {
        "$type": "app.bsky.feed.threadgate",
        "post": post_uri,
        "allow": allow,
        "createdAt": client.get_current_time_iso(),
    }, rkey)

def delete_post(client, post_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(post_uri))

def repost_post(client, post_uri: str, post_cid: str) -> str:
    resp = _create_record(client, client.me.did, "app.bsky.feed.repost", {
        "$type": "app.bsky.feed.repost",
        "subject": {"uri": post_uri, "cid": post_cid},
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri

def unrepost_post(client, repost_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(repost_uri))

# ---------------- profile ----------------

def get_profile(client, did: str) -> dict:
    """Plain dict of profile fields, for building the profile dialog UI
    AND for deciding Follow/Unfollow, Mute/Unmute, Block/Unblock in the
    user action menu (via the "viewer" sub-dict)."""
    profile = client.get_profile(did)
    viewer = getattr(profile, "viewer", None)
    return {
        "did": profile.did,
        "handle": profile.handle,
        "display_name": getattr(profile, "display_name", None),
        "description": getattr(profile, "description", None),
        "followers_count": getattr(profile, "followers_count", 0) or 0,
        "follows_count": getattr(profile, "follows_count", 0) or 0,
        "posts_count": getattr(profile, "posts_count", 0) or 0,
        "viewer": {
            "following": getattr(viewer, "following", None),  # follow record URI, or None
            "muted": bool(getattr(viewer, "muted", False)),
            "blocking": getattr(viewer, "blocking", None),  # block record URI, or None
        },
    }

def _actor_dict(actor) -> dict:
    """Plain dict for a ProfileView, with the viewer's relationship state."""
    viewer = getattr(actor, "viewer", None)
    return {
        "did": actor.did, "handle": actor.handle,
        "display_name": getattr(actor, "display_name", None),
        "description": getattr(actor, "description", None),
        "following_uri": getattr(viewer, "following", None) if viewer else None,
        "muted": bool(getattr(viewer, "muted", False)) if viewer else False,
        "blocking_uri": getattr(viewer, "blocking", None) if viewer else None,
    }


def get_followers(client, did: str, page_limit: int = 100, max_pages: int = 100) -> list:
    """
    Fetches the COMPLETE followers list, walking every page -- Show
    followers has no lazy-load by design, so returning just the first
    page would silently under-report. max_pages is a safety cap
    (100 x 100 = 10,000 people), not an expected ceiling.
    """
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.get_followers(did, limit=page_limit, cursor=cursor)
        results.extend(_actor_dict(f) for f in resp.followers)
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_follows(client, did: str, page_limit: int = 100, max_pages: int = 100) -> list:
    """Same as get_followers, but for who `did` follows."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.get_follows(did, limit=page_limit, cursor=cursor)
        results.extend(_actor_dict(f) for f in resp.follows)
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_known_followers(client, did: str, page_limit: int = 100, max_pages: int = 100) -> list:
    """
    app.bsky.graph.getKnownFollowers -- people the ACTIVE account
    follows who also follow `did` (bsky.app's "Followed by X, Y, and 3
    others").
    """
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.graph.get_known_followers(params={"actor": did, "limit": page_limit, "cursor": cursor})
        results.extend(_actor_dict(f) for f in resp.followers)
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_post_likes(client, post_uri: str, page_limit: int = 100, max_pages: int = 100) -> list:
    """app.bsky.feed.getLikes -- who liked this post. No bio/description
    in this response shape (unlike getFollowers/getFollows), left blank."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.feed.get_likes(params={"uri": post_uri, "limit": page_limit, "cursor": cursor})
        for like in resp.likes:
            actor = like.actor
            results.append({
                "did": actor.did, "handle": actor.handle,
                "display_name": getattr(actor, "display_name", None), "description": None,
            })
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_post_reposted_by(client, post_uri: str, page_limit: int = 100, max_pages: int = 100) -> list:
    """app.bsky.feed.getRepostedBy -- who reposted this post. No bio/
    description in this response shape, left blank."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.feed.get_reposted_by(params={"uri": post_uri, "limit": page_limit, "cursor": cursor})
        for actor in resp.reposted_by:
            results.append({
                "did": actor.did, "handle": actor.handle,
                "display_name": getattr(actor, "display_name", None), "description": None,
            })
        cursor = resp.cursor
        if not cursor:
            break
    return results

def _post_view_to_dict(post) -> dict:
    """
    Converts an atproto PostView into the plain dict shape feedWindow.py
    expects (_message_text/_format_post_time/_describe_embed) -- shared
    by get_author_feed and get_thread so this mapping only lives in one
    place instead of drifting into two copies.
    """
    author = post.author
    record = post.record
    recordGet = _dict_get(record)
    if recordGet is not None:
        text = recordGet("text", "") or ""
        reply = recordGet("reply", None)
    else:
        text = getattr(record, "text", "") or ""
        reply = getattr(record, "reply", None)
    text = expand_link_facets(text, _view_field(record, "facets"))

    reply_parent_uri = None
    reply_to_did = None
    reply_to_handle = None
    if reply is not None:
        parent = getattr(reply, "parent", None)
        reply_parent_uri = getattr(parent, "uri", None) if parent else None
        # The reply-to author's handle isn't in the record itself (only
        # their DID, via the parent URI) -- getPostThread's raw JSON and
        # the SDK's PostView both only carry the reply target's URI/CID
        # here, not who they are. Good enough to know "this is a reply"
        # and link back to it; the handle for display comes from the
        # author of whichever post this really replies to, which isn't
        # available without a second fetch -- left blank rather than
        # guessing.

    viewer = getattr(post, "viewer", None)
    viewer_like_uri = getattr(viewer, "like", None) if viewer else None
    viewer_repost_uri = getattr(viewer, "repost", None) if viewer else None
    viewer_bookmarked = bool(getattr(viewer, "bookmarked", False)) if viewer else False
    viewer_thread_muted = bool(getattr(viewer, "thread_muted", False)) if viewer else False

    embed_data = _extract_embed_info(post)

    return {
        "uri": post.uri,
        "cid": post.cid,
        "author_did": author.did,
        "handle": author.handle,
        "display_name": getattr(author, "display_name", None),
        "text": text,
        "indexed_at": post.indexed_at,
        "is_repost": False,
        "reply_parent_uri": reply_parent_uri,
        "reply_to_did": reply_to_did,
        "reply_to_handle": reply_to_handle,
        "embed_json": json.dumps(embed_data) if embed_data else None,
        "quoted_text": embed_data.get("quoted_text") if embed_data else None,
        "quoted_author_handle": embed_data.get("quoted_author_handle") if embed_data else None,
        "viewer_like_uri": viewer_like_uri,
        "viewer_repost_uri": viewer_repost_uri,
        "viewer_bookmarked": viewer_bookmarked,
        "viewer_thread_muted": viewer_thread_muted,
    }

def pin_feed_key(did: str) -> str:
    return f"user_timeline_pin:{did}"


def sync_author_feed_page(client, account_id: int, did: str, cursor: str = None, limit: int = 50) -> str:
    """
    Syncs one page of a user's own post timeline into the feed_items/
    posts cache under feed_key f"user_timeline:{did}" -- same shape as
    sync_timeline/sync_list_feed, lets UserTimelineTabWindow reuse
    FeedListMixin's cache-first/lazy-load machinery unchanged. Unlike
    get_author_feed above, resp.feed items here are the same
    FeedViewPost shape _store_feed_item already expects (reason/reply/
    post), so no separate conversion is needed.
    """
    params = {"actor": did, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    else:
        params["includePins"] = True  # a pin only comes with the first page
    resp = client.app.bsky.feed.get_author_feed(params=params)
    pinKey = pin_feed_key(did)
    if not cursor:
        db.clear_feed_key_cache(account_id, pinKey)  # drops a pin the author has since removed
    for item in resp.feed:
        # ReasonPin carries no "by"; only a repost does.
        isPin = item.reason is not None and getattr(item.reason, "by", None) is None
        try:
            _store_feed_item(item, account_id, pinKey if isPin else f"user_timeline:{did}")
        except Exception as e:
            log.error(f"NVSky: failed to store a user timeline item: {e}")
    return resp.cursor


def _fetch_thread_json(post_uri: str, depth: int = 25, parent_height: int = 100, client=None) -> dict:
    """
    Fetches getPostThread as raw JSON from Bluesky's public AppView
    (no auth needed -- a public read endpoint, the same one many
    third-party tools use for public content) instead of going through
    the SDK's typed response models. Sidesteps a real SDK bug: pydantic
    fails to resolve ThreadViewPost's discriminated union on this
    specific recursive/self-referential response shape ("Unable to
    extract tag using discriminator 'py_type' | 'pyType'"), even though
    flatter responses parse fine through the SDK elsewhere in this file.
    """
    if client is not None:
        # Authenticated first (sees logged-out-hidden accounts, real viewer state).
        try:
            authParams = models.AppBskyFeedGetPostThread.Params(
                uri=post_uri, depth=depth, parent_height=parent_height
            )
            authResponse = client.app.bsky.feed._client.invoke_query(
                "app.bsky.feed.getPostThread", params=authParams, output_encoding="application/json"
            )
            if isinstance(authResponse.content, dict) and authResponse.content.get("thread"):
                return authResponse.content
        except Exception as e:
            log.info(f"NVSky: authenticated getPostThread failed, using public AppView: {e}")
    params = urllib.parse.urlencode({"uri": post_uri, "depth": depth, "parentHeight": parent_height})
    url = f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _camel_to_snake(name: str) -> str:
    if name.startswith("$"):
        return name  # "$type" is looked up by this exact string everywhere -- keep as-is
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _dict_to_ns(value):
    """
    Recursively converts a plain JSON dict/list (from a raw HTTP fetch)
    into SimpleNamespace objects, converting camelCase keys to
    snake_case along the way (indexedAt -> indexed_at, likeCount ->
    like_count, etc) -- the real atproto SDK does this same conversion
    for its typed response objects, and _post_view_to_dict/
    _extract_embed_info were written assuming that convention. Without
    this, raw JSON round-tripped through here has the WRONG attribute
    names (e.g. .indexedAt instead of .indexed_at) and every getattr()
    lookup downstream silently returns the default instead of the real
    value.
    """
    if isinstance(value, dict):
        ns = types.SimpleNamespace()
        for k, v in value.items():
            setattr(ns, _camel_to_snake(k), _dict_to_ns(v))
        return ns
    if isinstance(value, list):
        return [_dict_to_ns(v) for v in value]
    return value

def get_threadgate_settings(client, post_uri: str) -> dict:
    """Reads the current app.bsky.feed.threadgate record for post_uri
    (if any) and returns {"state": "everyone"/"nobody"/"custom",
    "rules": set of "followers"/"following"/"mentioned" present,
    "has_list_rules": True if the record also has list-based rules
    NVSky doesn't have a picker for yet -- callers should warn before
    a save would drop those}."""
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey
    try:
        resp = client.com.atproto.repo.get_record(
            params={"repo": client.me.did, "collection": "app.bsky.feed.threadgate", "rkey": rkey}
        )
    except Exception as e:
        # RecordNotFound just means no threadgate has ever been set on
        # this post -- the normal case for most posts, not worth logging.
        if "RecordNotFound" not in str(e):
            log.info(f"NVSky: get_threadgate_settings failed to read record for {post_uri}: {e}")
        return {"state": "everyone", "rules": set(), "has_list_rules": False}

    record = resp.value
    recordGet = _dict_get(record)
    allow = recordGet("allow", []) if recordGet is not None else (getattr(record, "allow", []) or [])
    if not allow:
        return {"state": "nobody", "rules": set(), "has_list_rules": False}

    rules = set()
    hasListRules = False
    for rule in allow:
        ruleGet = _dict_get(rule)
        if ruleGet is not None:
            ruleType = ruleGet("$type", "") or ""
        else:
            # Typed SDK model objects expose the discriminator as
            # `py_type`, not `$type` -- checking only `$type` here was
            # the actual cause of every custom mode reading back as
            # "everyone", since typed objects don't have that attribute.
            ruleType = getattr(rule, "py_type", "") or getattr(rule, "$type", "") or ""
        if "followerRule" in ruleType:
            rules.add("followers")
        elif "followingRule" in ruleType:
            rules.add("following")
        elif "mentionRule" in ruleType:
            rules.add("mentioned")
        elif "listRule" in ruleType:
            hasListRules = True

    return {"state": "custom", "rules": rules, "has_list_rules": hasListRules}

def _get_postgate_record(client, post_uri: str) -> dict:
    """Reads the current app.bsky.feed.postgate record for post_uri as
    a plain dict ({"embeddingRules": [...], "detachedEmbeddingUris":
    [...]}), or an empty shape if none exists yet. Shared by every
    postgate read/write helper below so they never clobber each
    other's field."""
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey
    try:
        resp = client.com.atproto.repo.get_record(
            params={"repo": client.me.did, "collection": "app.bsky.feed.postgate", "rkey": rkey}
        )
    except Exception as e:
        if "RecordNotFound" not in str(e):
            log.info(f"NVSky: _get_postgate_record failed to read record for {post_uri}: {e}")
        return {"embeddingRules": [], "detachedEmbeddingUris": []}

    record = resp.value
    recordGet = _dict_get(record)
    if recordGet is not None:
        embeddingRules = recordGet("embeddingRules", []) or []
        detachedUris = recordGet("detachedEmbeddingUris", []) or []
    else:
        embeddingRules = getattr(record, "embedding_rules", None) or getattr(record, "embeddingRules", None) or []
        detachedUris = getattr(record, "detached_embedding_uris", None) or getattr(record, "detachedEmbeddingUris", None) or []
    return {"embeddingRules": list(embeddingRules), "detachedEmbeddingUris": list(detachedUris)}


def _put_postgate_record(client, post_uri: str, embedding_rules: list, detached_uris: list):
    """
    Writes the FULL postgate record (both fields together), so changing one field can't blank the
    other.
    """
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey

    if not embedding_rules and not detached_uris:
        try:
            client.com.atproto.repo.delete_record(
                data={"repo": client.me.did, "collection": "app.bsky.feed.postgate", "rkey": rkey}
            )
        except Exception:
            pass  # no postgate existed -- nothing to do
        return

    _put_record(client, client.me.did, "app.bsky.feed.postgate", {
        "$type": "app.bsky.feed.postgate",
        "post": post_uri,
        "embeddingRules": embedding_rules,
        "detachedEmbeddingUris": detached_uris,
        "createdAt": client.get_current_time_iso(),
    }, rkey)


def get_postgate_disables_quotes(client, post_uri: str) -> bool:
    """Reads the current app.bsky.feed.postgate record for post_uri (if
    any) and returns True if it disables quote posts (embeddingRules
    contains a disableRule)."""
    record = _get_postgate_record(client, post_uri)
    for rule in record["embeddingRules"]:
        ruleGet = _dict_get(rule)
        ruleType = ruleGet("$type", "") if ruleGet is not None else (getattr(rule, "py_type", "") or getattr(rule, "$type", "") or "")
        if "disableRule" in ruleType:
            return True
    return False


def set_postgate_disable_quotes(client, post_uri: str, disable: bool):
    """Sets or clears the disableRule on post_uri's postgate record --
    preserves any existing detachedEmbeddingUris on the same record
    (see _get_postgate_record/_put_postgate_record)."""
    record = _get_postgate_record(client, post_uri)
    rules = [
        r for r in record["embeddingRules"]
        if "disableRule" not in (
            _dict_get(r)("$type", "") if _dict_get(r) is not None else (getattr(r, "py_type", "") or getattr(r, "$type", "") or "")
        )
    ]
    if disable:
        rules.append({"$type": "app.bsky.feed.postgate#disableRule"})
    _put_postgate_record(client, post_uri, rules, record["detachedEmbeddingUris"])


def detach_quote(client, post_uri: str, quoting_post_uri: str):
    """Detaches MY post_uri from someone else's quoting_post_uri that
    quoted it -- doesn't touch their post, just removes it from
    post_uri's own quote list. Preserves any existing disableRule."""
    record = _get_postgate_record(client, post_uri)
    uris = record["detachedEmbeddingUris"]
    if quoting_post_uri not in uris:
        uris = uris + [quoting_post_uri]
    _put_postgate_record(client, post_uri, record["embeddingRules"], uris)

def get_thread(client, post_uri: str, depth: int = 25, parent_height: int = 100):
    """
    Fetches the full thread for post_uri via the public AppView (see
    _fetch_thread_json) -- every ancestor up to the root, then the post
    itself, then its replies depth-first -- as a flat list of post
    dicts in display order (each tagged "_thread_depth" for
    indentation), plus the index of the target post within that list.
    """
    try:
        raw = _fetch_thread_json(post_uri, depth, parent_height, client=client)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            # Translators: Shown when a thread's post was deleted, blocked, or can't be seen.
            raise RuntimeError(_("This post was deleted or isn't visible to you.")) from e
        raise
    node = _dict_to_ns(raw.get("thread", raw))
    if getattr(node, "post", None) is None:
        # notFoundPost / blockedPost root: nothing to show.
        # Translators: Shown when a thread's post was deleted, blocked, or can't be seen.
        raise RuntimeError(_("This post was deleted or isn't visible to you."))

    ancestors = []
    walker = node
    while getattr(walker, "parent", None) is not None:
        walker = walker.parent
        p = getattr(walker, "post", None)
        if p is not None:
            ancestors.append(p)
    ancestors.reverse()

    ordered = [(0, p) for p in ancestors]
    target_index = len(ordered)
    ordered.append((0, node.post))

    def walk_replies(thread_node, level):
        for reply_node in getattr(thread_node, "replies", None) or []:
            reply_post = getattr(reply_node, "post", None)
            if reply_post is None:
                continue
            ordered.append((level, reply_post))
            walk_replies(reply_node, level + 1)

    walk_replies(node, 1)

    posts = []
    for level, p in ordered:
        d = _post_view_to_dict(p)
        d["_thread_depth"] = level
        posts.append(d)

    return posts, target_index


def get_post_quotes(client, post_uri: str, page_limit: int = 50, max_pages: int = 20) -> list:
    """app.bsky.feed.getQuotes -- posts that quote post_uri. Returns
    plain post dicts via _post_view_to_dict, same shape as get_thread's
    entries minus _thread_depth."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.feed.get_quotes(params={"uri": post_uri, "limit": page_limit, "cursor": cursor})
        results.extend(_post_view_to_dict(p) for p in resp.posts)
        cursor = resp.cursor
        if not cursor:
            break
    return results


# ---------------- post actions ----------------

def like_post(client, post_uri: str, post_cid: str) -> str:
    resp = _create_record(client, client.me.did, "app.bsky.feed.like", {
        "$type": "app.bsky.feed.like",
        "subject": {"uri": post_uri, "cid": post_cid},
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri


def unlike_post(client, like_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(like_uri))


def mute_thread(client, root_uri: str):
    client.app.bsky.graph.mute_thread(data={"root": root_uri})


def unmute_thread(client, root_uri: str):
    client.app.bsky.graph.unmute_thread(data={"root": root_uri})


def create_report(client, subject_uri: str, subject_cid: str, reason_type: str, reason: str = ""):
    """
    Reports a post (com.atproto.moderation.createReport). Raw JSON: the subject carries a nested
    $type, sent exactly as built.
    """
    from atproto_client.models.dot_dict import DotDict

    client.com.atproto.moderation._client.invoke_procedure(
        "com.atproto.moderation.createReport",
        data=DotDict({
            "reasonType": reason_type,
            "reason": reason,
            "subject": {"$type": "com.atproto.repo.strongRef", "uri": subject_uri, "cid": subject_cid},
        }),
        input_encoding="application/json", output_encoding="application/json",
    )


def create_actor_report(client, did: str, reason_type: str, reason: str = ""):
    """
    Reports an account (com.atproto.moderation.createReport with a repoRef subject). Raw JSON, same
    as create_report.
    """
    from atproto_client.models.dot_dict import DotDict

    client.com.atproto.moderation._client.invoke_procedure(
        "com.atproto.moderation.createReport",
        data=DotDict({
            "reasonType": reason_type,
            "reason": reason,
            "subject": {"$type": "com.atproto.admin.defs#repoRef", "did": did},
        }),
        input_encoding="application/json", output_encoding="application/json",
    )


# ---------------- user (actor) actions ----------------

def follow_actor(client, subject_did: str) -> str:
    resp = _create_record(client, client.me.did, "app.bsky.graph.follow", {
        "$type": "app.bsky.graph.follow",
        "subject": subject_did,
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri


def unfollow_actor(client, follow_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(follow_uri))

def bookmark_post(client, post_uri: str, post_cid: str):
    """
    Saves a post to the account's private Bookmarks. Not a public repo record, so there is no record
    URI: removing it later references the original post's URI again.
    """
    client.app.bsky.bookmark.create_bookmark(data={"uri": post_uri, "cid": post_cid})


def unbookmark_post(client, post_uri: str):
    # deleteBookmark returns a bool (success/failure) instead of raising
    # on failure -- the old code never checked it, so a False result
    # silently looked identical to success (local state got cleared,
    # but the bookmark was still there server-side, and reappeared on
    # the next sync). Surface it as a real error instead.
    success = client.app.bsky.bookmark.delete_bookmark(data={"uri": post_uri})
    if not success:
        raise RuntimeError(f"deleteBookmark returned False for {post_uri}")

def mute_actor(client, did: str):
    client.app.bsky.graph.mute_actor(data={"actor": did})


def unmute_actor(client, did: str):
    client.app.bsky.graph.unmute_actor(data={"actor": did})


def get_muted_actors(client, page_limit: int = 100, max_pages: int = 100) -> list:
    """app.bsky.graph.getMutes -- accounts the active user has muted."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.graph.get_mutes(params={"limit": page_limit, "cursor": cursor})
        results.extend(_actor_dict(a) for a in resp.mutes)
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_blocked_actors(client, page_limit: int = 100, max_pages: int = 100) -> list:
    """
    app.bsky.graph.getBlocks -- accounts the active user has blocked. Each entry's viewer.blocking
    is the block record's URI, needed to unblock.
    """
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.graph.get_blocks(params={"limit": page_limit, "cursor": cursor})
        results.extend(_actor_dict(a) for a in resp.blocks)
        cursor = resp.cursor
        if not cursor:
            break
    return results


def block_actor(client, did: str) -> str:
    resp = _create_record(client, client.me.did, "app.bsky.graph.block", {
        "$type": "app.bsky.graph.block",
        "subject": did,
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri


def unblock_actor(client, block_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(block_uri))


def set_activity_subscription(client, did: str, subscribed: bool):
    """
    Subscribes (or unsubscribes) to per-account "new post" bell notifications, separate from follow.
    The target account field is "subject". Raw JSON via DotDict.
    """
    from atproto_client.models.dot_dict import DotDict

    client.app.bsky.notification._client.invoke_procedure(
        "app.bsky.notification.putActivitySubscription",
        data=DotDict({
            "subject": did,
            "activitySubscription": {"post": subscribed, "reply": subscribed},
        }),
        input_encoding="application/json", output_encoding="application/json",
    )


def get_activity_subscriptions(client, page_limit: int = 100, max_pages: int = 100) -> list:
    """app.bsky.notification.listActivitySubscriptions -- accounts the active user has
    bell-subscribed to for new-post notifications (separate from follow/mute/block)."""
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.notification.list_activity_subscriptions(
            params={"limit": page_limit, "cursor": cursor}
        )
        results.extend(_actor_dict(a) for a in resp.subscriptions)
        cursor = resp.cursor
        if not cursor:
            break
    return results


MUTED_WORDS_PREF_TYPE = "app.bsky.actor.defs#mutedWordsPref"

def get_muted_words(client) -> list:
    """
    Returns the account's muted words/tags as plain dicts: {"id", "value", "targets": ["content",
    "tag"]}.
    """
    prefs = _get_cleaned_preferences(client)
    pref = next((p for p in prefs if p.get("$type") == MUTED_WORDS_PREF_TYPE), None)
    if pref is None:
        return []
    items = pref.get("items") or []
    return [
        {
            "id": item.get("id"),
            "value": item.get("value", ""),
            "targets": list(item.get("targets") or []),
        }
        for item in items
    ]

def _save_muted_words(client, words: list):
    """
    Writes `words` back as the account's mutedWordsPref, preserving
    every OTHER preference type untouched -- via the same raw-JSON
    round trip _put_preferences already uses for saved feeds, instead
    of the typed get_preferences()/put_preferences() calls this used
    to make. See get_muted_words' docstring for why: the typed path
    fails hard (168 pydantic validation errors) on the account's OTHER
    preference types, not just muted words, so it can never be trusted
    to round-trip safely.
    """
    prefs = _get_cleaned_preferences(client)
    prefs = [p for p in prefs if p.get("$type") != MUTED_WORDS_PREF_TYPE]
    prefs.append({
        "$type": MUTED_WORDS_PREF_TYPE,
        "items": [
            {
                "id": w["id"] or f"{int(time.time() * 1000)}{i}",
                "value": w["value"],
                "targets": w["targets"] or ["content", "tag"],
                "actorTarget": "all",
            }
            for i, w in enumerate(words)
        ],
    })
    _put_preferences(client, prefs)


def add_muted_word(client, value: str, targets: list = None):
    words = get_muted_words(client)
    words.append({"id": None, "value": value, "targets": targets or ["content", "tag"]})
    _save_muted_words(client, words)


def remove_muted_word(client, value: str):
    words = [w for w in get_muted_words(client) if w["value"] != value]
    _save_muted_words(client, words)


def get_content_label_prefs(client) -> dict:
    """
    Returns {label_val: "show"/"warn"/"hide"} for every
    app.bsky.actor.defs#contentLabelPref the account has explicitly
    set. Missing keys mean "not set yet" -- callers should fall back
    to CONTENT_LABEL_DEFAULT_VISIBILITY. Same raw-JSON preferences
    round trip as get_muted_words (see _get_cleaned_preferences).
    """
    prefs = _get_cleaned_preferences(client)
    result = {ADULT_CONTENT_PREF_KEY: False}
    for pref in prefs:
        if pref.get("$type") == ADULT_CONTENT_PREF_TYPE:
            result[ADULT_CONTENT_PREF_KEY] = bool(pref.get("enabled"))
        elif pref.get("$type") == CONTENT_LABEL_PREF_TYPE:
            label = pref.get("label")
            visibility = pref.get("visibility")
            if label and visibility:
                result[label] = "show" if visibility == "ignore" else visibility
    return result


def set_adult_content_enabled(client, enabled: bool):
    prefs = _get_cleaned_preferences(client)
    prefs = [p for p in prefs if p.get("$type") != ADULT_CONTENT_PREF_TYPE]
    prefs.append({"$type": ADULT_CONTENT_PREF_TYPE, "enabled": bool(enabled)})
    _put_preferences(client, prefs)


def set_content_label_pref(client, label: str, visibility: str):
    """Sets one label's visibility, preserving every other
    contentLabelPref and every other preference type untouched --
    same raw-JSON round trip as _save_muted_words."""
    prefs = _get_cleaned_preferences(client)
    prefs = [
        p for p in prefs
        if not (p.get("$type") == CONTENT_LABEL_PREF_TYPE and p.get("label") == label)
    ]
    prefs.append({
        "$type": CONTENT_LABEL_PREF_TYPE,
        "label": label,
        "visibility": visibility,
    })
    _put_preferences(client, prefs)


# Categories of app.bsky.notification.getPreferences. "chat" is here only so writes round-trip it
# unchanged: putPreferencesV2 is a PUT (full replace), so every category must be resent every time.
# It is not an editable row in the UI (no "list" field, only include/push).
NOTIFICATION_FILTERABLE_CATEGORIES = [
    "follow", "like", "like_via_repost", "mention", "quote", "reply", "repost", "repost_via_repost",
]
NOTIFICATION_SIMPLE_CATEGORIES = [
    "starterpack_joined", "subscribed_post", "unverified", "verified",
]
NOTIFICATION_ALL_CATEGORIES = NOTIFICATION_FILTERABLE_CATEGORIES + NOTIFICATION_SIMPLE_CATEGORIES

# $type per category. Required on write: the server needs it to parse these union fields.
NOTIFICATION_CATEGORY_TYPES = {
    "chat": "app.bsky.notification.defs#chatPreference",
    "follow": "app.bsky.notification.defs#filterablePreference",
    "like": "app.bsky.notification.defs#filterablePreference",
    "like_via_repost": "app.bsky.notification.defs#filterablePreference",
    "mention": "app.bsky.notification.defs#filterablePreference",
    "quote": "app.bsky.notification.defs#filterablePreference",
    "reply": "app.bsky.notification.defs#filterablePreference",
    "repost": "app.bsky.notification.defs#filterablePreference",
    "repost_via_repost": "app.bsky.notification.defs#filterablePreference",
    "starterpack_joined": "app.bsky.notification.defs#preference",
    "subscribed_post": "app.bsky.notification.defs#preference",
    "unverified": "app.bsky.notification.defs#preference",
    "verified": "app.bsky.notification.defs#preference",
}

# snake_case category (the SDK's attribute name) -> wire-format camelCase key. A plain dict is sent
# as given, so writes must use the camelCase name.
NOTIFICATION_CATEGORY_WIRE_KEYS = {
    "chat": "chat",
    "follow": "follow",
    "like": "like",
    "like_via_repost": "likeViaRepost",
    "mention": "mention",
    "quote": "quote",
    "reply": "reply",
    "repost": "repost",
    "repost_via_repost": "repostViaRepost",
    "starterpack_joined": "starterpackJoined",
    "subscribed_post": "subscribedPost",
    "unverified": "unverified",
    "verified": "verified",
}


def get_notification_prefs(client) -> dict:
    """
    Returns {category: {"list": bool, "include": "all"/"follows"/None, "push": bool}} for every
    category in NOTIFICATION_CATEGORY_TYPES (including "chat", read-only here but needed so writes
    round-trip it unchanged -- see set_notification_category).
    """
    resp = client.app.bsky.notification.get_preferences()
    prefs = resp.preferences
    result = {}
    for category in NOTIFICATION_CATEGORY_TYPES:
        pref = getattr(prefs, category, None)
        if pref is None:
            continue
        result[category] = {
            "list": bool(getattr(pref, "list", False)),
            "include": getattr(pref, "include", None),
            "push": bool(getattr(pref, "push", False)),
        }
    return result


def set_notification_category(client, current_prefs: dict, category: str, list_enabled: bool, push: bool, include: str = None):
    """
    Saves notification preferences (app.bsky.notification.putPreferencesV2). It is a PUT, so the
    FULL preferences object is sent: every category in current_prefs (chat included), each with its
    own $type and wire key. `current_prefs` is what get_notification_prefs last returned, with the
    one `category` already changed. `push` is sent back as last known for every category (the UI
    never changes it).
    """
    from atproto_client.models.dot_dict import DotDict

    body = {}
    for cat, catType in NOTIFICATION_CATEGORY_TYPES.items():
        pref = current_prefs.get(cat) or {}
        entry = {"$type": catType, "push": bool(pref.get("push", False))}
        if catType == "app.bsky.notification.defs#filterablePreference":
            entry["list"] = bool(pref.get("list", False))
            entry["include"] = pref.get("include") or "all"
        elif catType == "app.bsky.notification.defs#chatPreference":
            entry["include"] = pref.get("include") or "all"
        else:  # plain "preference" -- list/push only, no include
            entry["list"] = bool(pref.get("list", False))
        body[NOTIFICATION_CATEGORY_WIRE_KEYS[cat]] = entry

    client.app.bsky.notification._client.invoke_procedure(
        "app.bsky.notification.putPreferencesV2",
        data=DotDict(body),
        input_encoding="application/json", output_encoding="application/json",
    )


# ---------------- lists ----------------

LIST_PURPOSE_MOD = "app.bsky.graph.defs#modlist"
LIST_PURPOSE_CURATE = "app.bsky.graph.defs#curatelist"


def get_lists(client, did: str, page_limit: int = 100, max_pages: int = 20) -> list:
    """
    Every list `did` created OR subscribed to (mute/block) -- what
    bsky.app's "My lists" shows, since getLists(actor=...) returns both.
    """
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.graph.get_lists(params={"actor": did, "limit": page_limit, "cursor": cursor})
        for lst in resp.lists:
            viewer = getattr(lst, "viewer", None)
            results.append({
                "uri": lst.uri,
                "cid": lst.cid,
                "name": lst.name,
                "description": getattr(lst, "description", None),
                "purpose": lst.purpose,
                "creator_did": lst.creator.did,
                "creator_handle": lst.creator.handle,
                "muted": bool(getattr(viewer, "muted", False)) if viewer else False,
                "blocked_uri": getattr(viewer, "blocked", None) if viewer else None,
            })
        cursor = resp.cursor
        if not cursor:
            break
    return results


def get_list(client, list_uri: str, page_limit: int = 100, max_pages: int = 100) -> dict:
    """
    List info plus the COMPLETE member roster (walks every page) -- the
    modlist member view and Manage members both need the whole list.
    """
    info = None
    members = []
    cursor = None
    for _ in range(max_pages):
        resp = client.app.bsky.graph.get_list(params={"list": list_uri, "limit": page_limit, "cursor": cursor})
        if info is None:
            lst = resp.list
            viewer = getattr(lst, "viewer", None)
            info = {
                "uri": lst.uri,
                "cid": lst.cid,
                "name": lst.name,
                "description": getattr(lst, "description", None),
                "purpose": lst.purpose,
                "creator_did": lst.creator.did,
                "creator_handle": lst.creator.handle,
                "muted": bool(getattr(viewer, "muted", False)) if viewer else False,
                "blocked_uri": getattr(viewer, "blocked", None) if viewer else None,
            }
        for item in resp.items:
            subject = item.subject
            members.append({
                "listitem_uri": item.uri,
                "did": subject.did,
                "handle": subject.handle,
                "display_name": getattr(subject, "display_name", None),
            })
        cursor = resp.cursor
        if not cursor:
            break
    info["members"] = members
    return info


def sync_list_feed(client, account_id: int, list_uri: str, cursor: str = None, limit: int = 50) -> str:
    """
    Same shape as sync_timeline; feed_key is the list's own at:// uri.
    Curation lists only -- modlists raise a server error, so callers
    must check purpose first.
    """
    resp = client.app.bsky.feed.get_list_feed(params={"list": list_uri, "limit": limit, "cursor": cursor})
    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, list_uri)
        except Exception as e:
            log.error(f"NVSky: failed to store a list feed item: {e}")
    return resp.cursor


def create_list(client, name: str, description: str, purpose: str) -> dict:
    resp = _create_record(client, client.me.did, "app.bsky.graph.list", {
        "$type": "app.bsky.graph.list",
        "name": name,
        "description": description or "",
        "purpose": purpose,
        "createdAt": client.get_current_time_iso(),
    })
    return {"uri": resp.uri, "cid": resp.cid}


def delete_list(client, list_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(list_uri))


def add_list_member(client, list_uri: str, subject_did: str) -> str:
    resp = _create_record(client, client.me.did, "app.bsky.graph.listitem", {
        "$type": "app.bsky.graph.listitem",
        "subject": subject_did,
        "list": list_uri,
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri


def remove_list_member(client, listitem_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(listitem_uri))


def mute_actor_list(client, list_uri: str):
    client.app.bsky.graph.mute_actor_list(data={"list": list_uri})


def unmute_actor_list(client, list_uri: str):
    client.app.bsky.graph.unmute_actor_list(data={"list": list_uri})


def block_actor_list(client, list_uri: str) -> str:
    """
    Blocks a whole list: a public repo record (collection app.bsky.graph.listblock, parallel to
    block_actor), unlike the private mute procedure.
    """
    resp = _create_record(client, client.me.did, "app.bsky.graph.listblock", {
        "$type": "app.bsky.graph.listblock",
        "subject": list_uri,
        "createdAt": client.get_current_time_iso(),
    })
    return resp.uri


def unblock_actor_list(client, listblock_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(listblock_uri))


def search_actors_typeahead(client, query: str, limit: int = 8) -> list:
    """Search-as-you-type suggestions (Manage members, New chat dialogs)."""
    if not query.strip():
        return []
    resp = client.app.bsky.actor.search_actors_typeahead(params={"q": query, "limit": limit})
    return [
        {"did": a.did, "handle": a.handle, "display_name": getattr(a, "display_name", None)}
        for a in resp.actors
    ]


def count_graphemes(text: str) -> int:
    """
    Approximates the Unicode grapheme count, which is what Bluesky enforces (maxGraphemes) for posts
    and chat messages. len() overcounts Thai because combining vowels and tone marks are separate
    codepoints. Collapses basic ZWJ emoji sequences; does not handle flag emoji or skin-tone
    modifiers.
    """
    count = 0
    prevWasJoiner = False
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("Mn", "Mc", "Me"):
            # Nonspacing/spacing/enclosing combining marks attach to
            # the previous base character instead of starting a new
            # grapheme cluster -- this is the line that fixes Thai.
            prevWasJoiner = False
            continue
        if ch == "\u200d":
            prevWasJoiner = True
            continue
        if prevWasJoiner:
            prevWasJoiner = False
            continue
        count += 1
    return count


def split_at_grapheme_boundary(text: str, max_chars: int):
    """
    Splits `text` into (first, rest) for the two message columns, aiming at the midpoint of `text`
    (capped at max_chars) so both halves stay under the per-cell limit (about 511 characters) for
    messages up to roughly 2 * max_chars.

    The split never falls inside a grapheme cluster (a naive slice can sever a Thai tone mark from
    its base consonant, or split a ZWJ emoji sequence). Within WHITESPACE_LOOKBACK characters of the
    target it prefers to land after whitespace or punctuation (any Unicode "P" category, so CJK
    punctuation counts). Text with no such landmark nearby gets a grapheme-safe hard cut, which can
    land mid-word (no word segmenter is bundled).

    Not a guarantee against truncation in the worst case; "Show message..." shows the full text.
    Returns (text, "") unchanged if text already fits within max_chars.
    """
    WHITESPACE_LOOKBACK = 100

    if len(text) <= max_chars:
        return text, ""

    targetSplit = min(max_chars, (len(text) + 1) // 2)

    boundaries = [0]
    prevWasJoiner = False
    for i, ch in enumerate(text):
        if i == 0:
            continue
        category = unicodedata.category(ch)
        if category in ("Mn", "Mc", "Me"):
            prevWasJoiner = False
            continue
        if ch == "\u200d":
            prevWasJoiner = True
            continue
        if prevWasJoiner:
            prevWasJoiner = False
            continue
        boundaries.append(i)

    validBoundaries = [b for b in boundaries if b <= targetSplit]
    splitIndex = max(validBoundaries, default=0)

    # Look for the whitespace/punctuation boundary closest to (but not
    # past) the target -- validBoundaries is already in ascending
    # order, so the last match found in the window is the closest one.
    windowStart = max(0, splitIndex - WHITESPACE_LOOKBACK)
    for b in validBoundaries:
        if b < windowStart:
            continue
        prevChar = text[b - 1] if b > 0 else ""
        if prevChar.isspace() or unicodedata.category(prevChar).startswith("P"):
            splitIndex = b

    if splitIndex == 0:
        # First grapheme cluster is itself longer than targetSplit --
        # extremely unlikely, but fall back to a hard cut rather than
        # returning the whole string unsplit.
        splitIndex = targetSplit or 1

    return text[:splitIndex].rstrip(), text[splitIndex:].lstrip()
