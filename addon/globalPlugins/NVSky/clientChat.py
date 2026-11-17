"""
Chat (DM and group) calls and the local chat cache sync for NVSky. Re-exported by client.py, so
callers keep using client.send_message, client.sync_convos and the rest.
"""
import json

from logHandler import log

from . import db
from . import soundpack
from .clientCore import models, expand_link_facets, build_facets


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


def _chat_invoke(client, nsid: str, body: dict = None, params: dict = None) -> dict:
    """
    Raw-JSON call through the chat proxy: a procedure (body) or, when params is given, a query.
    Returns the response dict ({} if the response isn't one). Raw on purpose: tolerates union
    members newer than the SDK's models, and DotDict avoids typed-model serialization.
    """
    from atproto_client.models.dot_dict import DotDict

    rawClient = get_chat_client(client).chat.bsky.group._client
    if params is not None:
        response = rawClient.invoke_query(nsid, params=DotDict(params), output_encoding="application/json")
    else:
        response = rawClient.invoke_procedure(
            nsid, data=DotDict(body or {}),
            input_encoding="application/json", output_encoding="application/json",
        )
    return response.content if isinstance(response.content, dict) else {}


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


def create_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """
    Creates or updates a group's invite link (chat.bsky.group.createJoinLink); an existing link is
    updated rather than rejected. joinRule is "anyone" or "followedByOwner". Raw JSON.
    """
    return _chat_invoke(
        client, "chat.bsky.group.createJoinLink",
        {"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval},
    ).get("joinLink")


def edit_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """Changes an existing invite link's join rule (chat.bsky.group.editJoinLink)."""
    return _chat_invoke(
        client, "chat.bsky.group.editJoinLink",
        {"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval},
    ).get("joinLink")


def enable_join_link(client, convo_id: str):
    """Re-enables a disabled invite link."""
    return _chat_invoke(client, "chat.bsky.group.enableJoinLink", {"convoId": convo_id}).get("joinLink")


def disable_join_link(client, convo_id: str):
    """Disables a group's invite link."""
    _chat_invoke(client, "chat.bsky.group.disableJoinLink", {"convoId": convo_id})


def edit_group(client, convo_id: str, name: str):
    """Renames a group (chat.bsky.group.editGroup). Raw JSON; returns the updated convo."""
    return _chat_invoke(client, "chat.bsky.group.editGroup", {"convoId": convo_id, "name": name}).get("convo")


def create_group(client, member_dids: list, name: str = None):
    """
    Creates a group conversation (chat.bsky.group.createGroup). Raw JSON with a DotDict body,
    because plain dicts have no model_dump_json. Returns the new convo dict.
    """
    body = {"members": member_dids}
    if name:
        body["name"] = name
    return _chat_invoke(client, "chat.bsky.group.createGroup", body).get("convo")


def add_group_members(client, convo_id: str, member_dids: list):
    """Adds members to a group (chat.bsky.group.addMembers)."""
    _chat_invoke(client, "chat.bsky.group.addMembers", {"convoId": convo_id, "members": member_dids})


def remove_group_members(client, convo_id: str, member_dids: list):
    """Removes members from a group (chat.bsky.group.removeMembers)."""
    _chat_invoke(client, "chat.bsky.group.removeMembers", {"convoId": convo_id, "members": member_dids})


def get_join_link_previews(client, codes: list):
    """
    Previews for invite codes (chat.bsky.group.getJoinLinkPreviews). Items are a union (a full
    preview, or only the code for a disabled or invalid link), so this is raw JSON.
    """
    return _chat_invoke(client, "chat.bsky.group.getJoinLinkPreviews", params={"codes": codes}).get("joinLinkPreviews", [])


def request_join_group(client, code: str):
    """
    Asks to join a group by invite code (chat.bsky.group.requestJoin). Returns the response dict
    (status "joined" or pending).
    """
    return _chat_invoke(client, "chat.bsky.group.requestJoin", {"code": code})


def list_join_requests(client, convo_id: str):
    """Pending join requests of a group (first page only; chat.bsky.group.listJoinRequests)."""
    return _chat_invoke(client, "chat.bsky.group.listJoinRequests", params={"convoId": convo_id}).get("requests", [])


def approve_join_request(client, convo_id: str, member_did: str):
    """
    Approves a join request (chat.bsky.group.approveJoinRequest). Raw JSON: the output carries a
    convo.
    """
    return _chat_invoke(
        client, "chat.bsky.group.approveJoinRequest", {"convoId": convo_id, "member": member_did},
    ).get("convo")


def reject_join_request(client, convo_id: str, member_did: str):
    """Rejects a join request (chat.bsky.group.rejectJoinRequest)."""
    _chat_invoke(client, "chat.bsky.group.rejectJoinRequest", {"convoId": convo_id, "member": member_did})


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
