"""
Bluesky (AT Protocol) client wrapper for NVSky.

IMPORTANT: `atproto` is imported lazily inside functions, not at module
load time, to keep NVDA startup fast (pydantic's import cost is slow).

Most write actions use low-level repo.create_record/delete_record with
plain dict records instead of the SDK's typed Record models -- send_post()'s
sibling image-embed helpers hit a known, still-open atproto SDK bug
(MarshalX/atproto#354) with discriminated-union tags; plain dicts sidestep
that model layer. Several are EXPERIMENTAL -- paste back the traceback if
one errors.
"""

import json
import os
import random
import re
import string
import tempfile
import threading
import time
import unicodedata
import types
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from atproto import models

from logHandler import log

from . import crypto
from . import db


class LoginError(Exception):
    pass


def _normalize_handle(handle: str) -> str:
    handle = handle.strip().lstrip("@")
    if "." not in handle:
        return f"{handle}.bsky.social"
    return handle


def _extract_error_message(e: Exception) -> str:
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
        raise LoginError(
            f'Could not log in as "{normalized_handle}". '
            f"Check your handle and App Password are correct. ({message})"
        ) from e

    encrypted = crypto.encrypt(app_password)
    account_id = db.upsert_account(handle=profile.handle, did=profile.did, encrypted_password=encrypted)
    db.set_active_account(account_id)

    chatSupported = check_chat_supported(client)
    db.set_chat_supported(account_id, chatSupported)

    log.info(f"NVSky: logged in as {profile.handle} (chat supported: {chatSupported})")
    return {"id": account_id, "handle": profile.handle, "did": profile.did, "chat_supported": chatSupported}


def check_chat_supported(client) -> bool:
    """
    LOW CONFIDENCE -- no dedicated "does this account/PDS support
    chat" endpoint was found during the group-chat lexicon research
    this session (chat.bsky.actor.getStatus checks OTHER accounts'
    invite-ability, not the caller's own general chat access). Probes
    the cheapest real chat call instead (listing convos, capped to 1)
    and treats ANY failure as "not supported" -- almost certainly too
    broad (a transient network error would also read as unsupported),
    but safe in the sense that it only hides a tab rather than breaking
    anything, and this runs again on every login so a wrong result
    isn't permanent. Paste back the actual error if a real
    chat-capable account ever gets flagged unsupported here.
    """
    try:
        get_chat_client(client).chat.bsky.convo.list_convos(params={"limit": 1})
        return True
    except Exception as e:
        log.info(f"NVSky: chat capability check failed (treating as unsupported): {e}")
        return False


def get_client_for_active_account():
    from atproto import Client as ATProtoClient

    account = db.get_active_account()
    if account is None:
        raise LoginError("No active account is stored.")

    app_password = crypto.decrypt(account["encrypted_password"])

    # LOW CONFIDENCE: reported WinError 10038 ("not a socket") here
    # looked like a transient OS/network-layer glitch, not a real auth
    # failure -- a fresh ATProtoClient() + one retry with a short pause
    # resolved it in testing. Paste back the traceback if this keeps
    # happening after the retry too; that would mean it's a real
    # recurring problem, not a one-off blip.
    lastError = None
    for attempt in range(2):
        client = ATProtoClient()
        try:
            client.login(account["handle"], app_password)
            return client
        except Exception as e:
            lastError = e
            if attempt == 0:
                time.sleep(0.5)

    message = _extract_error_message(lastError)
    log.error(f"NVSky: re-login failed for {account['handle']}: {message}")
    raise LoginError(message) from lastError


# ---------------- timeline sync ----------------

def debug_dump(obj, label: str = "debug"):
    """
    Ad-hoc dev tool: writes whatever the SDK actually returned to a
    JSON file instead of guessing field names one log.info at a time
    (confirmed repeatedly this session to waste rounds -- kind's real
    shape, the group-name field, the never-found admin/role field, all
    took multiple back-and-forth rounds each). Handles pydantic models,
    plain dicts/lists/lists-of-models, and falls back to repr() for
    anything it can't otherwise serialize. Files land in
    globalPlugins/NVSky/debug_dumps/ -- open one and paste back
    whatever's relevant instead of another log.info round.
    """
    import json
    import os
    import datetime

    folder = os.path.join(os.path.dirname(__file__), "debug_dumps")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{label}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")

    def _serialize(o, depth=0):
        if depth > 8:
            return repr(o)
        if o is None or isinstance(o, (str, int, float, bool)):
            return o
        if hasattr(o, "model_dump"):
            try:
                return o.model_dump(mode="json")
            except Exception:
                pass
        if isinstance(o, dict):
            return {str(k): _serialize(v, depth + 1) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_serialize(i, depth + 1) for i in o]
        if hasattr(o, "__dict__"):
            try:
                return {k: _serialize(v, depth + 1) for k, v in vars(o).items()}
            except Exception:
                pass
        return repr(o)

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_serialize(obj), f, indent=2, ensure_ascii=False, default=repr)
        log.info(f"NVSky: debug dump written to {path}")
    except Exception as e:
        log.error(f"NVSky: debug dump failed: {e}")


def _store_convo(convo, account_id: int, my_did: str, status: str):
    otherMembers = [
        {"did": m.did, "handle": m.handle, "display_name": getattr(m, "display_name", None)}
        for m in convo.members if m.did != my_did
    ]
    # LOW CONFIDENCE: no confirmed field on ConvoView that explicitly
    # flags a group vs a 1:1 DM -- using "more than one other member"
    # as a heuristic until a real listConvos/getConvo response for a
    # group is seen (this project will generate one via the new
    # create_group() below, so it'll get confirmed on first real test).
    # Paste back the raw convo (repr) if a 1:1 ever misclassifies as a
    # group or vice versa.
    # CONFIRMED via testing: convo.kind is itself a nested
    # discriminated-union object (chat.bsky.convo.defs#groupConvo or
    # #directConvo), not a plain string -- a first attempt logging
    # str(convo.kind) just dumped this whole nested object as text
    # instead of the simple value expected. It carries is_group (via
    # its own $type/py_type discriminator), the group's real name
    # (kind.name), and lock state (kind.lock_status, 'locked' or
    # 'unlocked') directly -- none of this was ever on the top-level
    # convo object, which is why earlier attempts guessing convo.name
    # and a plain convo.kind string both came back empty.
    # kind.py_type/$type turned out to be a dead end -- getattr(kind,
    # "py_type", ...) returns pydantic's FieldInfo schema metadata
    # object, not the actual runtime discriminator string (confirmed:
    # the logged value was literally "FieldInfo(annotation=NoneType,
    # ..., default='chat.bsky.convo.defs#groupConvo', ...)", not a
    # plain string), so comparing it with .endswith() never matched
    # anything. Detecting by field presence instead: a real groupConvo
    # instance carries name/lock_status/member_count/created_at (all
    # confirmed present with real values in earlier testing); a real
    # directConvo instance carries none of those extra fields at all.
    kind = convo.kind
    if kind is not None and hasattr(kind, "name") and hasattr(kind, "lock_status"):
        isGroup = True
        groupName = getattr(kind, "name", None)
        locked = getattr(kind, "lock_status", None) == "locked"
    elif kind is not None:
        isGroup = False
        groupName = None
        locked = False
    else:
        log.info(f"NVSky: convo.kind was None for convo {getattr(convo, 'id', '?')}")
        isGroup = len(otherMembers) > 1
        groupName = None
        locked = False

    # Gave up on GUESSING admin/owner via log.info -- see debug_dump()
    # above. This dumps the full convo + kind + members ONE TIME (only
    # when a group's own member list is still empty in the local DB,
    # i.e. first time this exact convo is ever synced) so the real
    # shape can be read directly out of the JSON file instead of
    # another round of field-name guessing. Remove this call once
    # admin/owner detection (if it turns out to exist at all) is
    # confirmed and hardcoded.
    # CONFIRMED via debug_dump: role lives on member.kind.role (kind
    # here is chat.bsky.actor.defs#groupConvoMember, e.g.
    # {"role": "owner", "added_by": null, ...}) -- a member-level
    # nested object, distinct from the convo-level "kind" above
    # (chat.bsky.convo.defs#groupConvo/#directConvo). Both direct
    # member.role (first guess) and log.info-only field dumps (second
    # attempt) missed this because it's one level deeper than either
    # checked.
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


def _sync_convo_messages(dm, convo_id: str, account_id: int, unread_count: int = 0, limit: int = 100):
    # Same SDK bug _fetch_thread_json above works around for View
    # Thread -- pydantic can't resolve chat.bsky.convo.defs#messageView's
    # discriminated union on some real responses ("Unable to extract tag
    # using discriminator 'py_type' | 'pyType'"), even though the SDK's
    # own model classes handle a $type-keyed dict fine in isolation --
    # something in the strict response-parsing pipeline loses it.
    # Bypass the typed Response model entirely and parse the raw JSON
    # ourselves, same principle as _fetch_thread_json, just via an
    # authenticated call since DMs aren't public data.
    params = models.ChatBskyConvoGetMessages.Params(convo_id=convo_id, limit=limit)
    response = dm._client.invoke_query(
        "chat.bsky.convo.getMessages", params=params, output_encoding="application/json"
    )
    rawMessages = response.content.get("messages", []) if isinstance(response.content, dict) else []

    for message in rawMessages:
        if "text" not in message:
            # A #deletedMessageView / #systemMessageView placeholder,
            # not a real message -- nothing to cache.
            continue
        sender = message.get("sender") or {}
        # Unlike the REQUEST side (MessageInput.replyTo, just {messageId}),
        # the RESPONSE embeds the entire original message under replyTo --
        # id/text directly, not a bare reference. Store both so the reply
        # preview always has text to show (even if the original isn't in
        # the currently loaded page) AND the id, for Left/Right jump.
        replyTo = message.get("replyTo") or {}
        # LOW CONFIDENCE: field name/shape ("reactions": [{"value":
        # "<emoji>", "sender": {"did": "..."}, "createdAt": "..."}, ...])
        # inferred from chat.bsky.convo.defs#messageView/#reactionView
        # in the lexicon docs, never confirmed against a real server
        # response in this project -- paste back the raw dict (or a
        # traceback) if reactions don't show up right.
        db.upsert_message({
            "account_id": account_id,
            "convo_id": convo_id,
            "message_id": message.get("id"),
            "sender_did": sender.get("did"),
            "text": message.get("text", ""),
            "sent_at": message.get("sentAt"),
            "reply_to_message_id": replyTo.get("id"),
            "reply_to_text": replyTo.get("text"),
            "reactions_json": json.dumps(message.get("reactions") or []),
        })

    # Re-derive is_read for every cached message in this conversation
    # from the server's own unread_count, instead of guessing per
    # message at insert time (a prior attempt guessed by sender_did,
    # which defaulted every already-read historical message back to
    # unread -- see db.reconcile_message_read_state's docstring).
    db.reconcile_message_read_state(account_id, convo_id, unread_count)
def sync_convos(client, account_id: int, my_did: str, limit: int = 50):
    """
    EXPERIMENTAL -- first use of chat.bsky.convo.listConvos/getMessages
    in NVSky, paste back the traceback if this errors (may also mean
    the account has never used DMs in the official app -- see
    get_chat_client()'s docstring above). Per the confirmed design,
    this pulls BOTH the conversation list AND every conversation's full
    message history in one pass -- expanding/selecting a conversation
    in the UI must be instant from the local cache, never a per-select
    network round-trip. Fetches "request" (message requests not yet
    accepted) and "accepted" (normal open conversations) separately --
    listConvos filters by exactly one status per call, there's no
    "give me both" option.
    """
    dm = get_chat_client(client).chat.bsky.convo

    for status in ("request", "accepted"):
        resp = dm.list_convos(params={"limit": limit, "status": status})
        for convo in resp.convos:
            try:
                _store_convo(convo, account_id, my_did, status)
                _sync_convo_messages(dm, convo.id, account_id, convo.unread_count or 0)
            except Exception as e:
                # No raw object dump here anymore -- a transient server
                # error (502 etc) doesn't need one, and dumping the full
                # ConvoView repr (deeply nested, 30KB+) straight into
                # log.info was blocking NVDA's shared log lock long
                # enough to freeze speech/the whole session, confirmed
                # via a real capture. This path fires far more often
                # now that background sync calls sync_convos every
                # couple of minutes instead of only on manual F5.
                log.error(f"NVSky: failed to sync a conversation: {e}")
def sync_convo_messages(client, account_id: int, convo_id: str, limit: int = 100):
    """Refreshes just ONE conversation's messages -- used after sending
    a message, and by the pop-out per-conversation tab's own Check for
    updates, without re-listing every conversation.

    LOW CONFIDENCE / KNOWN LIMITATION: this path doesn't re-fetch the
    convo's own unread_count from the server (no confirmed single-convo
    getConvo call in this project yet) -- it reuses whatever was last
    synced into the local convos table (via sync_convos, or the direct
    optimistic decrement in mark_message_read's caller). Fine for the
    common cases this is called from (right after my own send, where my
    own unread_count doesn't change; or a manual F5), but a genuinely
    fresh unread_count for this one conversation would need a real
    chat.bsky.convo.getConvo call, not added here yet.
    """
    dm = get_chat_client(client).chat.bsky.convo
    convo = db.get_convo(account_id, convo_id)
    unreadCount = (convo.get("unread_count") or 0) if convo else 0
    _sync_convo_messages(dm, convo_id, account_id, unreadCount, limit=limit)
def get_chat_client(client):
    """
    EXPERIMENTAL -- first use of the chat.bsky.* namespace in NVSky.
    DMs are served by a completely separate service from the rest of
    Bluesky (did:web:api.bsky.chat) -- every chat.bsky.* call MUST go
    through a client wrapped with with_bsky_chat_proxy(), which sets
    the required service-proxying header for us. Calling chat.bsky.*
    methods on the normal client (without this wrapper) will fail.
    Also requires the account to have chat enabled (a
    chat.bsky.actor.declaration record in their own repo, normally
    created automatically the first time they open Chat in the
    official app) -- if they've never used DMs before, calls through
    this may error until they do that once.
    """
    return client.with_bsky_chat_proxy()


def send_message(client, convo_id: str, text: str, reply_to_message_id: str = None):
    # Two SDK-internal issues stacked here, confirmed from a real full
    # traceback (not just the top-level message):
    # 1) sendMessage's response is a MessageView -- the same
    #    discriminated-union type that fails to parse in getMessages --
    #    so we bypass the typed response parsing (call invoke_procedure
    #    directly, ignore the echoed-back message; the caller already
    #    re-syncs this conversation right after a successful send).
    # 2) The REQUEST body itself fails to serialize with
    #    PydanticSerializationError ("Unable to serialize unknown type:
    #    FieldInfo") inside model_dump_json() -- reproduces in
    #    production every time but NOT against a freshly pip-installed
    #    copy of the same SDK, so this looks like a real bug specific to
    #    whatever pydantic/atproto_client version NVSky bundles under
    #    its own lib/ folder. Route around it entirely: build a DotDict
    #    instead of a typed Data model -- DotDict serializes through
    #    get_model_as_dict()+to_json() instead, a completely different,
    #    non-pydantic_core code path.
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
    """LOW CONFIDENCE: chat.bsky.convo.updateAllRead has never been
    exercised against a real server in this project before now --
    paste back any traceback."""
    get_chat_client(client).chat.bsky.convo.update_all_read(data={})


def get_convo_availability(client, member_dids: list):
    """LOW CONFIDENCE -- chat.bsky.convo.getConvoAvailability never
    exercised. Guessed input {members: [...]} matching
    get_convo_for_members' own shape. Used defensively -- if this
    fails or the shape is wrong, callers just skip the pre-check and
    proceed to the normal send/create flow anyway."""
    response = get_chat_client(client).chat.bsky.convo.get_convo_availability(
        params={"members": member_dids}
    )
    debug_dump(response, "convo_availability")
    return response


def get_or_create_convo_for_member(client, member_did: str):
    """LOW CONFIDENCE: chat.bsky.convo.getConvoForMembers has never
    been exercised against a real server in this project before now --
    paste back the raw response (or a traceback) if this doesn't work.
    Per the lexicon this resolves an existing 1:1 conversation with
    this member, or creates one if none exists yet, in a single call.
    Uses the same raw-JSON-bypass approach as _sync_convo_messages/
    _fetch_thread_json (see their comments) instead of trusting the
    typed SDK response -- other chat.bsky.convo endpoints in this
    project have hit pydantic union-discriminator parsing bugs before,
    and a first attempt at this one silently produced no result with
    no exception raised, consistent with that same class of bug."""
    dm = get_chat_client(client).chat.bsky.convo
    params = models.ChatBskyConvoGetConvoForMembers.Params(members=[member_did])
    response = dm._client.invoke_query(
        "chat.bsky.convo.getConvoForMembers", params=params, output_encoding="application/json"
    )
    return response.content.get("convo") if isinstance(response.content, dict) else None


def _group_ns(client):
    return get_chat_client(client).chat.bsky.group


def create_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """LOW CONFIDENCE -- field names/enum guessed (joinRule:
    "anyone"/"followedByOwner", requireApproval: bool). Real UI wording
    ("Create or modify an invite link") suggests this upserts rather
    than erroring if a link already exists. Raw-JSON bypass."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.createJoinLink",
        data=DotDict({"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval}),
        input_encoding="application/json", output_encoding="application/json",
    )
    debug_dump(response.content, "create_join_link_result")
    return response.content.get("joinLink") if isinstance(response.content, dict) else None


def edit_join_link(client, convo_id: str, join_rule: str, require_approval: bool):
    """LOW CONFIDENCE -- see create_join_link's docstring."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.editJoinLink",
        data=DotDict({"convoId": convo_id, "joinRule": join_rule, "requireApproval": require_approval}),
        input_encoding="application/json", output_encoding="application/json",
    )
    debug_dump(response.content, "edit_join_link_result")
    return response.content.get("joinLink") if isinstance(response.content, dict) else None


def enable_join_link(client, convo_id: str):
    """LOW CONFIDENCE -- guessed input {convoId} only."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.enableJoinLink", data=DotDict({"convoId": convo_id}),
        input_encoding="application/json", output_encoding="application/json",
    )
    debug_dump(response.content, "enable_join_link_result")
    return response.content.get("joinLink") if isinstance(response.content, dict) else None


def disable_join_link(client, convo_id: str):
    """LOW CONFIDENCE -- guessed input {convoId} only."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.disableJoinLink", data=DotDict({"convoId": convo_id}),
        input_encoding="application/json", output_encoding="application/json",
    )


def edit_group(client, convo_id: str, name: str):
    """Confirmed endpoint exists (edit_group). Field names (convoId/name)
    guessed by analogy with every other group action -- unconfirmed.
    Raw-JSON bypass since this almost certainly returns an updated
    convo (same union-parsing risk as create_group)."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.editGroup", data=DotDict({"convoId": convo_id, "name": name}),
        input_encoding="application/json", output_encoding="application/json",
    )
    debug_dump(response.content, "edit_group_result")
    return response.content.get("convo") if isinstance(response.content, dict) else None


def create_group(client, member_dids: list, name: str = None):
    """
    EXPERIMENTAL -- first use of chat.bsky.group.* in NVSky, never
    exercised against a real server. Same raw-JSON-bypass approach as
    get_or_create_convo_for_member/send_message (see their comments)
    since every chat.bsky.convo.* endpoint touched so far has hit a
    pydantic discriminated-union parsing bug on its typed response --
    group responses are expected to hit the same issue. Body is built
    as a DotDict (not a plain dict) for the same reason send_message
    does -- invoke_procedure calls .model_dump_json() on whatever it's
    given, which a plain dict doesn't have; confirmed by testing (a
    first attempt using a plain dict here failed with exactly
    "'dict' object has no attribute 'model_dump_json'").

    LOW CONFIDENCE: request body field names ("members" for the
    invitee DID list, "name" for the group name) are guessed from the
    lexicon docs' prose description, not a confirmed real schema.
    Also unconfirmed whether the server accepts a single-member group
    (name set but only one recipient) -- paste back the raw traceback
    if either errors.
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
    """LOW CONFIDENCE -- see create_group's docstring."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.addMembers",
        data=DotDict({"convoId": convo_id, "members": member_dids}),
        input_encoding="application/json", output_encoding="application/json",
    )


def remove_group_members(client, convo_id: str, member_dids: list):
    """LOW CONFIDENCE -- see create_group's docstring."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.removeMembers",
        data=DotDict({"convoId": convo_id, "members": member_dids}),
        input_encoding="application/json", output_encoding="application/json",
    )


def get_join_link_previews(client, codes: list):
    """
    LOW CONFIDENCE -- output items are a union (joinLinkPreviewView /
    disabledJoinLinkPreviewView / invalidJoinLinkPreviewView), same
    parsing risk as create_group, so raw-JSON bypass. Full preview has
    name/owner/memberCount etc; disabled/invalid only has "code".
    """
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_query(
        "chat.bsky.group.getJoinLinkPreviews", params=DotDict({"codes": codes}),
        output_encoding="application/json",
    )
    debug_dump(response.content, "join_link_previews_raw")
    previews = response.content.get("joinLinkPreviews", []) if isinstance(response.content, dict) else []
    return previews


def request_join_group(client, code: str):
    """LOW CONFIDENCE -- chat.bsky.group.requestJoin never exercised;
    "code" field name guessed from every other join-link endpoint's
    naming. Paste back traceback/debug_dump("request_join_result")."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.requestJoin", data=DotDict({"code": code}),
        input_encoding="application/json", output_encoding="application/json",
    )
    debug_dump(response.content, "request_join_result")
    return response.content if isinstance(response.content, dict) else {}


def list_join_requests(client, convo_id: str):
    """LOW CONFIDENCE -- chat.bsky.group.listJoinRequests never
    exercised; "convoId" param and "requests" output key guessed.
    Pagination not implemented. Paste back debug_dump("join_requests")."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_query(
        "chat.bsky.group.listJoinRequests", params=DotDict({"convoId": convo_id}),
        output_encoding="application/json",
    )
    debug_dump(response.content, "join_requests_raw")
    requests_ = response.content.get("requests", []) if isinstance(response.content, dict) else []
    return requests_


def approve_join_request(client, convo_id: str, member_did: str):
    """Confirmed input schema (convoId, member). Output has "convo" --
    raw-JSON bypass like create_group, same union-parsing risk."""
    from atproto_client.models.dot_dict import DotDict

    response = _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.approveJoinRequest",
        data=DotDict({"convoId": convo_id, "member": member_did}),
        input_encoding="application/json", output_encoding="application/json",
    )
    return response.content.get("convo") if isinstance(response.content, dict) else None


def reject_join_request(client, convo_id: str, member_did: str):
    """LOW CONFIDENCE -- rejectJoinRequest itself unconfirmed, mirrors
    approve_join_request's input shape. Paste back traceback."""
    from atproto_client.models.dot_dict import DotDict

    _group_ns(client)._client.invoke_procedure(
        "chat.bsky.group.rejectJoinRequest",
        data=DotDict({"convoId": convo_id, "member": member_did}),
        input_encoding="application/json", output_encoding="application/json",
    )


def withdraw_join_request(client, convo_id: str):
    """Confirmed schema (input: convoId only, empty output)."""
    get_chat_client(client).chat.bsky.group.withdraw_join_request(data={"convo_id": convo_id})


def mark_join_requests_read(client, convo_id: str):
    """Confirmed schema (input: convoId only, empty output)."""
    get_chat_client(client).chat.bsky.group.update_join_requests_read(data={"convo_id": convo_id})


def mark_message_read(client, convo_id: str, message_id: str):
    """
    LOW CONFIDENCE: chat.bsky.convo.updateRead's optional messageId
    param (marks everything up to and including that message as read,
    per the lexicon) has never been exercised against a real server in
    this project before now -- paste back any traceback. Called from a
    background thread (see chatWindow.py's onMessageFocused/
    _announceNthNewestMessage) purely to keep the server's own
    unread_count in sync with what the user has actually read locally;
    never surfaced to the UI either way.
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
    LOW CONFIDENCE -- chat.bsky.convo.lockConvo has never been
    exercised against a real server in this project. Confirmed via a
    real 400 error that locking is a real prerequisite for a group's
    owner to leave it ('OwnerCannotLeave: Owner must lock the group
    before leaving'). Paste back a traceback if this doesn't work.
    """
    get_chat_client(client).chat.bsky.convo.lock_convo(data={"convo_id": convo_id})


def unlock_convo(client, convo_id: str):
    """LOW CONFIDENCE -- see lock_convo's docstring."""
    get_chat_client(client).chat.bsky.convo.unlock_convo(data={"convo_id": convo_id})


def accept_convo(client, convo_id: str):
    get_chat_client(client).chat.bsky.convo.accept_convo(data={"convo_id": convo_id})


def add_reaction(client, convo_id: str, message_id: str, value: str):
    """LOW CONFIDENCE: chat.bsky.convo.addReaction has never been
    exercised against a real server in this project before now --
    paste back any traceback."""
    get_chat_client(client).chat.bsky.convo.add_reaction(
        data={"convo_id": convo_id, "message_id": message_id, "value": value}
    )


def remove_reaction(client, convo_id: str, message_id: str, value: str):
    """LOW CONFIDENCE -- see add_reaction's docstring."""
    get_chat_client(client).chat.bsky.convo.remove_reaction(
        data={"convo_id": convo_id, "message_id": message_id, "value": value}
    )


def delete_message_for_self(client, convo_id: str, message_id: str):
    get_chat_client(client).chat.bsky.convo.delete_message_for_self(
        data={"convo_id": convo_id, "message_id": message_id}
    )


def sync_saved(client, account_id: int, cursor: str = None, limit: int = 50) -> str:
    """
    EXPERIMENTAL -- first use of app.bsky.bookmark.getBookmarks in
    NVSky, paste back the traceback (or a print of the raw response
    object) if this errors or nothing shows up. The exact response
    shape wasn't confirmed against real data before writing this --
    Bluesky's docs page for this endpoint doesn't render its schema in
    a fetchable way, so this is inferred from the bookmark record's own
    lexicon (subject + createdAt) and the general "*View" wrapper
    pattern used everywhere else in this API. Tries both `.subject` and
    `.item` as the field name holding the bookmarked post, since which
    one the SDK actually uses wasn't confirmed either.
    """
    resp = client.app.bsky.bookmark.get_bookmarks(params={"cursor": cursor, "limit": limit})

    for bookmark in resp.bookmarks:
        # BookmarkView.item is the full PostView (author, text, counts,
        # viewer state) -- BookmarkView.subject is just a bare strongRef
        # (uri/cid only, no author), confirmed from a real error log.
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
            log.info(f"NVSky: raw bookmark that failed = {bookmark!r}")

    return resp.cursor


def sync_timeline(client, account_id: int, cursor: str = None, limit: int = 50, feed_key: str = "home") -> str:
    resp = client.get_timeline(cursor=cursor, limit=limit)

    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, feed_key)
        except Exception as e:
            log.error(f"NVSky: failed to store a feed item: {e}")
            log.info(f"NVSky: raw item that failed = {item!r}")

    return resp.cursor


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

    # "$type" is reliably a real resolved value on these View objects --
    # unlike py_type, which stays an unresolved FieldInfo default in this
    # SDK/pydantic combo for many nested models.
    embedType = getattr(embedView, "$type", "") or ""
    info = {"$type": embedType}

    try:
        if embedType.startswith("app.bsky.embed.record"):
            recordView = getattr(embedView, "record", None)
            mediaView = getattr(embedView, "media", None)  # only set for recordWithMedia
            if mediaView is not None:
                recordView = getattr(recordView, "record", recordView)
                mediaType = getattr(mediaView, "$type", "") or ""
                _fill_media_info(info, mediaView, mediaType)
            value = getattr(recordView, "value", None)
            author = getattr(recordView, "author", None)
            info["quoted_text"] = getattr(value, "text", None)
            info["quoted_author_handle"] = getattr(author, "handle", None)
        else:
            _fill_media_info(info, embedView, embedType)
    except Exception as e:
        log.info(f"NVSky: partial embed extraction failure for {post.uri}: {e}")

    return info


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
    elif mediaType.startswith("app.bsky.embed.external"):
        external = getattr(mediaView, "external", None)
        info["link_url"] = getattr(external, "uri", None)
        info["link_title"] = getattr(external, "title", None) or ""

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

    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
    )

    is_repost = item.reason is not None
    reposted_by_handle = item.reason.by.handle if is_repost else None
    reposted_by_display_name = getattr(item.reason.by, "display_name", None) if is_repost else None
    reposted_by_did = item.reason.by.did if is_repost else None
    # A repost's position in the feed is when it was RE-posted, not when
    # the underlying post was originally created -- using post.indexed_at
    # for feed ordering made old posts that just got reposted sort (and
    # paginate) as if they were old, breaking lazy-load pagination math
    # whenever a repost was involved.
    feedIndexedAt = item.reason.indexed_at if is_repost else post.indexed_at

    reply = item.reply
    reply_parent_uri = reply.parent.uri if reply else None
    reply_to_author = getattr(getattr(reply, "parent", None), "author", None) if reply else None
    reply_to_did = getattr(reply_to_author, "did", None)
    reply_to_handle = getattr(reply_to_author, "handle", None)

    viewer = getattr(post, "viewer", None)
    viewer_like_uri = getattr(viewer, "like", None) if viewer else None
    viewer_repost_uri = getattr(viewer, "repost", None) if viewer else None
    viewer_bookmarked = bool(getattr(viewer, "bookmarked", False)) if viewer else False

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

    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
    )

    viewer = getattr(post, "viewer", None)
    viewer_like_uri = getattr(viewer, "like", None) if viewer else None
    viewer_repost_uri = getattr(viewer, "repost", None) if viewer else None
    viewer_bookmarked = bool(getattr(viewer, "bookmarked", False)) if viewer else False

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
        "reply_parent_uri": None,
        "reply_to_did": None,
        "reply_to_handle": None,
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
    })


def search_posts_hydrated(
    client, account_id: int, query: str, sort: str = "latest", cursor: str = None, limit: int = 25,
    author: str = None, since: str = None, until: str = None, lang: str = None,
):
    """Search + hydrate into the local posts cache via
    _store_resolved_post (same helper resolve_posts uses for
    notifications) -- Post action (Alt+A)/React/etc. then work on
    search results exactly like any other post, no separate converter
    needed. Returns (uris, cursor) -- caller reads db.get_post(uri) per
    result to build display dicts. LOW CONFIDENCE: author/since/until/
    lang param names recalled from lexicon knowledge, never tested."""
    params = {"q": query, "sort": sort, "cursor": cursor, "limit": limit}
    if author:
        params["author"] = author
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if lang:
        params["lang"] = [lang]
    response = client.app.bsky.feed.search_posts(params=params)
    uris = []
    for post in response.posts:
        try:
            _store_resolved_post(post, account_id)
            uris.append(post.uri)
        except Exception as e:
            log.error(f"NVSky: failed to store a search result post: {e}")
    return uris, response.cursor


def follow_starter_pack_members(client, full) -> int:
    """Follows every profile in list_items_sample. LOW CONFIDENCE on
    field names (same as get_starter_pack_full). Returns count
    actually followed; skips ones already followed (viewer.following set)."""
    count = 0
    for item in getattr(full, "list_items_sample", None) or []:
        subject = item.subject
        viewer = getattr(subject, "viewer", None)
        if viewer and getattr(viewer, "following", None):
            continue
        client.app.bsky.graph.follow(data={"subject": subject.did})
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
    """LOW CONFIDENCE -- graph.get_starter_pack never exercised. Guessed
    input {starterPack: uri} matching the usual single-record-by-uri
    param shape elsewhere."""
    response = client.app.bsky.graph.get_starter_pack(params={"starter_pack": uri})
    return response.starter_pack


def get_feed_preview(client, account_id: int, feed_uri: str, limit: int = 30) -> list:
    """One-shot fetch (no cursor loop, no local feed-membership row --
    see FeedPreviewTabWindow's docstring) via app.bsky.feed.getFeed,
    hydrated through the same _store_resolved_post path search results use."""
    response = client.app.bsky.feed.get_feed(params={"feed": feed_uri, "limit": limit})
    uris = []
    for item in response.feed:
        try:
            _store_resolved_post(item.post, account_id)
            uris.append(item.post.uri)
        except Exception as e:
            log.error(f"NVSky: failed to store a feed preview post: {e}")
    return uris


def search_actors(client, query: str, cursor: str = None, limit: int = 25):
    return client.app.bsky.actor.search_actors(params={"q": query, "cursor": cursor, "limit": limit})


def search_starter_packs(client, query: str, cursor: str = None, limit: int = 25):
    return client.app.bsky.graph.search_starter_packs(params={"q": query, "cursor": cursor, "limit": limit})


def search_feeds(client, query: str, limit: int = 25):
    """LOW CONFIDENCE -- no dedicated 'search feed generators' endpoint
    in the SDK dump; get_popular_feed_generators is the closest match
    and its "query" param is unconfirmed to actually filter by keyword
    vs just being ignored (falling back to popular-only). Paste back
    the result if search terms don't seem to affect what comes back."""
    params = {"limit": limit}
    if query:
        params["query"] = query
    return client.app.bsky.unspecced.get_popular_feed_generators(params=params)


def _fix_field_info(obj):
    """Shared by every savedFeedsPrefV2 read/write helper below --
    atproto v0.0.69's ContentLabelPref.py_type holds a raw unresolved
    pydantic FieldInfo instead of its string value, which breaks
    model_dump() unless patched first."""
    if obj is None:
        return
    try:
        from pydantic.fields import FieldInfo
        raw_type = getattr(obj, "py_type", None)
        if isinstance(raw_type, FieldInfo):
            default_type = raw_type.default
            if default_type is not None:
                object.__setattr__(obj, "py_type", default_type)
    except Exception:
        pass


def _clean_dumped(value):
    """Recursively fixes the pyType/py_type -> $type fallout left over
    after model_dump() on a preferences object with the FieldInfo bug."""
    if isinstance(value, dict):
        pt1 = value.pop("pyType", None)
        pt2 = value.pop("py_type", None)
        real_type = value.get("$type") or pt1 or pt2
        if real_type:
            value["$type"] = real_type
        for key in list(value.keys()):
            value[key] = _clean_dumped(value[key])
        return value
    if isinstance(value, list):
        for i in range(len(value)):
            value[i] = _clean_dumped(value[i])
        return value
    return value


class _RawJsonModel:
    """Prevents atproto's put_preferences from rehydrating an
    already-clean payload back into a Pydantic model (which re-triggers
    the same FieldInfo bug a second time) by handing invoke_procedure()
    something that already looks serialized."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump_json(self, exclude_none=True, by_alias=True, **kwargs):
        return json.dumps(self._payload, ensure_ascii=False, separators=(",", ":"))


def _get_cleaned_preferences(client) -> list:
    """Fetches app.bsky.actor.getPreferences and returns it as a list
    of plain, FieldInfo-safe dicts. Every savedFeedsPrefV2 read/write
    helper starts from this."""
    try:
        prefs_response = client.app.bsky.actor.get_preferences()
    except Exception as e:
        log.error("NVSky: getPreferences failed: %s", e, exc_info=True)
        raise

    for pref in prefs_response.preferences:
        _fix_field_info(pref)
        try:
            for attr_name in dir(pref):
                if attr_name.startswith("_"):
                    continue
                try:
                    attr_value = getattr(pref, attr_name, None)
                except Exception:
                    continue
                if isinstance(attr_value, list):
                    for item in attr_value:
                        _fix_field_info(item)
        except Exception:
            pass

    prefs = []
    try:
        for pref in prefs_response.preferences:
            dumped = pref.model_dump(mode="json", by_alias=True, exclude_none=True)
            prefs.append(_clean_dumped(dumped))
    except Exception as e:
        log.error("NVSky: failed to serialize preferences: %s", e, exc_info=True)
        debug_dump({"type": type(pref).__name__, "repr": repr(pref)}, "get_preferences_dump_failure")
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
        debug_dump(payload, "put_preferences_failed")
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
    """Batch-resolves feed generator URIs to display info (display_name/
    creator_handle/description) for the Feed manager settings panel.
    LOW CONFIDENCE -- first use of app.bsky.feed.getFeedGenerators in
    NVSky, paste back the traceback if this errors. Chunks into
    batches of 25 (getPosts' documented max, assumed to apply here too
    since no separate limit is documented for this endpoint)."""
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
    """Batch-resolves post URIs (a notification's actionable post) to
    their text + author handle for display, AND fully hydrates each
    one into the local `posts` cache via _store_resolved_post() so Post
    action (Alt+A) has real data (cid, viewer like/repost/bookmark
    state, counts) to work with -- not just display text. EXPERIMENTAL
    -- first use of app.bsky.feed.getPosts in NVSky, paste back the
    traceback if this errors. Chunks into batches of 25 (the API's max
    per call)."""
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


def sync_notifications(client, account_id: int, cursor: str = None, limit: int = 50) -> str:
    """EXPERIMENTAL -- first use of app.bsky.notification.listNotifications
    in NVSky, paste back the traceback if this errors."""
    resp = client.app.bsky.notification.list_notifications(params={"cursor": cursor, "limit": limit})

    # Bluesky's server only supports one "seen up to this point in time"
    # watermark (no per-notification read state) -- this is what clears
    # the unread badge in the official app and any other client reading
    # the same account, separate from NVSky's own local is_read state.
    # EXPERIMENTAL -- first use of app.bsky.notification.updateSeen in
    # NVSky, paste back the traceback if this errors. Best-effort: don't
    # let a failure here break the sync that already succeeded above.
    try:
        seenAt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        client.app.bsky.notification.update_seen({"seen_at": seenAt})
    except Exception as e:
        log.info(f"NVSky: updateSeen failed (non-fatal): {e}")

    # Resolve every notification's actionable post -- not just like/
    # repost's reasonSubject -- so Post action (Alt+A) has full cached
    # data for reply/mention/quote notifications too (their own uri IS
    # the actionable post there). Mirrored in _store_notification's own
    # subjectUri logic below.
    urisToResolve = []
    for notif in resp.notifications:
        if notif.reason in ("reply", "mention", "quote"):
            urisToResolve.append(notif.uri)
        elif notif.reason in ("like", "repost") and notif.reason_subject:
            urisToResolve.append(notif.reason_subject)

    resolvedSubjects = resolve_posts(client, account_id, urisToResolve) if urisToResolve else {}

    for notif in resp.notifications:
        try:
            _store_notification(notif, account_id, resolvedSubjects)
        except Exception as e:
            log.error(f"NVSky: failed to store a notification: {e}")
            log.info(f"NVSky: raw notification that failed = {notif!r}")

    return resp.cursor


def _store_notification(notif, account_id: int, resolvedSubjects: dict):
    author = notif.author
    db.upsert_author(
        did=author.did,
        handle=author.handle,
        display_name=getattr(author, "display_name", None),
        avatar_url=getattr(author, "avatar", None),
    )

    reason = notif.reason
    subjectText = ""
    subjectAuthorHandle = None

    if reason in ("reply", "mention", "quote"):
        # For these, the notification's own record/uri IS the new post
        # the author just wrote -- that's also the actionable post for
        # Post action (Alt+A).
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
    image_path = _compress_image_for_blob(image_path)
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    upload = client.com.atproto.repo.upload_blob(image_bytes)
    blob = upload.blob
    try:
        return {
            "$type": "blob",
            "ref": {"$link": blob.ref.link},
            "mimeType": blob.mime_type,
            "size": blob.size,
        }
    except AttributeError:
        log.info(f"NVSky: upload_blob() result shape = {blob!r}")
        raise


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
        return {
            "$type": "app.bsky.actor.profile",
            "displayName": getattr(record, "display_name", None),
            "description": getattr(record, "description", None),
            "avatar": getattr(record, "avatar", None),
            "banner": getattr(record, "banner", None),
        }
    except Exception:
        # Some accounts have never had a profile record written at all --
        # start from an empty one rather than failing the edit outright.
        return {"$type": "app.bsky.actor.profile"}


def _put_profile_record(client, record: dict):
    record["$type"] = "app.bsky.actor.profile"
    client.com.atproto.repo.put_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.actor.profile",
            "rkey": "self",
            "record": record,
        }
    )


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


URL_REGEX = re.compile(r'https?://[^\s<>"\']+')
MENTION_REGEX = re.compile(r'(?<!\w)@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})\b')

def find_all_urls(text: str) -> list:
    """Every URL in text, in order, trailing punctuation stripped."""
    return [m.group(0).rstrip(').,!?') for m in URL_REGEX.finditer(text)]


def find_first_url(text: str):
    """First URL in text, or None. Shared with build_facets() (actual
    facet indices) so the two never disagree about what counts as a
    URL."""
    urls = find_all_urls(text)
    return urls[0] if urls else None

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
    Scrapes the target page's OpenGraph tags for a link-card preview --
    the same job bsky.app's own compose box does client-side. Fetches
    the page directly (not through a Bluesky-internal proxy, which isn't
    a documented/stable API to depend on), with a browser-like
    User-Agent since some sites block non-browser requests.
    Regex-based tag extraction (no HTML parser vendored) --
    EXPERIMENTAL: works for well-formed pages, can miss oddly-ordered or
    unusual meta tags. A missing title/description/image just means a
    plainer card, never a hard failure.
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

    # Falls back to the domain, not the full raw URL, when og:title
    # can't be scraped (JS-rendered pages, unusual meta tag ordering,
    # etc) -- a nicer-looking card than the literal URL string, though
    # still not a real title. Once posted this can't be edited, so
    # domain is at least a saner permanent fallback.
    fallbackTitle = urllib.parse.urlparse(url).netloc or url
    return {"uri": url, "title": title or fallbackTitle, "description": description or "", "image_url": image}


# ---------------- posting ----------------

def create_post(client, text: str, attachments: list = None, reply_ref: dict = None,
                 quote_ref: dict = None, link_card: dict = None, facets: list = None):
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
    """
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": client.get_current_time_iso(),
    }

    if facets:
        record["facets"] = facets

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

    resp = client.com.atproto.repo.create_record(
        data={"repo": client.me.did, "collection": "app.bsky.feed.post", "record": record}
    )
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

    raw = _fetch_thread_json(post_uri, depth=0, parent_height=100)
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

    client.com.atproto.repo.put_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.feed.threadgate",
            "rkey": rkey,
            "record": {
                "$type": "app.bsky.feed.threadgate",
                "post": post_uri,
                "allow": allow,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )

def delete_post(client, post_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(post_uri))

def repost_post(client, post_uri: str, post_cid: str) -> str:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.feed.repost",
            "record": {
                "$type": "app.bsky.feed.repost",
                "subject": {"uri": post_uri, "cid": post_cid},
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
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
        results.extend(
            {
                "did": f.did, "handle": f.handle, "display_name": getattr(f, "display_name", None),
                "description": getattr(f, "description", None),
            }
            for f in resp.followers
        )
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
        results.extend(
            {
                "did": f.did, "handle": f.handle, "display_name": getattr(f, "display_name", None),
                "description": getattr(f, "description", None),
            }
            for f in resp.follows
        )
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
    }

def get_author_feed(client, did: str, page_limit: int = 50, max_pages: int = 2) -> list:
    """
    A user's own recent posts (most recent first). NOT persisted to the
    cache DB -- kept only for callers that just want a quick read-only
    snapshot. For a real cached/lazy-loadable view, use
    sync_author_feed_page below instead (UserTimelineTabWindow).
    """
    results = []
    cursor = None
    for _ in range(max_pages):
        resp = client.get_author_feed(actor=did, limit=page_limit, cursor=cursor)
        results.extend(_post_view_to_dict(item.post) for item in resp.feed)
        cursor = resp.cursor
        if not cursor:
            break
    return results


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
    resp = client.get_author_feed(actor=did, limit=limit, cursor=cursor)
    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, f"user_timeline:{did}")
        except Exception as e:
            log.error(f"NVSky: failed to store a user timeline item: {e}")
    return resp.cursor


def _fetch_thread_json(post_uri: str, depth: int = 25, parent_height: int = 100) -> dict:
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

def get_postgate_disables_quotes(client, post_uri: str) -> bool:
    """Reads the current app.bsky.feed.postgate record for post_uri (if
    any) and returns True if it disables quote posts (embeddingRules
    contains a disableRule). EXPERIMENTAL -- paste back the traceback
    if this errors; postgate hasn't been exercised elsewhere in NVSky
    yet."""
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey
    try:
        resp = client.com.atproto.repo.get_record(
            params={"repo": client.me.did, "collection": "app.bsky.feed.postgate", "rkey": rkey}
        )
    except Exception as e:
        if "RecordNotFound" not in str(e):
            log.info(f"NVSky: get_postgate_disables_quotes failed to read record for {post_uri}: {e}")
        return False

    record = resp.value
    recordGet = _dict_get(record)
    if recordGet is not None:
        embeddingRules = recordGet("embeddingRules", []) or []
    else:
        embeddingRules = getattr(record, "embedding_rules", None) or getattr(record, "embeddingRules", None) or []
    for rule in embeddingRules:
        ruleGet = _dict_get(rule)
        ruleType = ruleGet("$type", "") if ruleGet is not None else (getattr(rule, "py_type", "") or getattr(rule, "$type", "") or "")
        if "disableRule" in ruleType:
            return True
    return False


def set_postgate_disable_quotes(client, post_uri: str, disable: bool):
    """Sets or clears the app.bsky.feed.postgate record for post_uri to
    disable/allow quote posts of it. EXPERIMENTAL -- paste back the
    traceback if this errors."""
    from atproto import AtUri
    rkey = AtUri.from_str(post_uri).rkey

    if not disable:
        try:
            client.com.atproto.repo.delete_record(
                data={"repo": client.me.did, "collection": "app.bsky.feed.postgate", "rkey": rkey}
            )
        except Exception:
            pass  # no postgate existed -- quotes already allowed, nothing to do
        return

    client.com.atproto.repo.put_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.feed.postgate",
            "rkey": rkey,
            "record": {
                "$type": "app.bsky.feed.postgate",
                "post": post_uri,
                "embeddingRules": [{"$type": "app.bsky.feed.postgate#disableRule"}],
                "createdAt": client.get_current_time_iso(),
            },
        }
    )

def get_thread(client, post_uri: str, depth: int = 25, parent_height: int = 100):
    """
    Fetches the full thread for post_uri via the public AppView (see
    _fetch_thread_json) -- every ancestor up to the root, then the post
    itself, then its replies depth-first -- as a flat list of post
    dicts in display order (each tagged "_thread_depth" for
    indentation), plus the index of the target post within that list.
    """
    raw = _fetch_thread_json(post_uri, depth, parent_height)
    node = _dict_to_ns(raw.get("thread", raw))

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


# ---------------- post actions ----------------

def like_post(client, post_uri: str, post_cid: str) -> str:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.feed.like",
            "record": {
                "$type": "app.bsky.feed.like",
                "subject": {"uri": post_uri, "cid": post_cid},
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return resp.uri


def unlike_post(client, like_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(like_uri))


def mute_thread(client, root_uri: str):
    client.app.bsky.graph.mute_thread(data={"root": root_uri})


def unmute_thread(client, root_uri: str):
    client.app.bsky.graph.unmute_thread(data={"root": root_uri})


def create_report(client, subject_uri: str, subject_cid: str, reason_type: str, reason: str = ""):
    client.com.atproto.moderation.create_report(
        data={
            "reasonType": reason_type,
            "reason": reason,
            "subject": {"$type": "com.atproto.repo.strongRef", "uri": subject_uri, "cid": subject_cid},
        }
    )


# ---------------- user (actor) actions ----------------

def follow_actor(client, subject_did: str) -> str:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.graph.follow",
            "record": {
                "$type": "app.bsky.graph.follow",
                "subject": subject_did,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return resp.uri


def unfollow_actor(client, follow_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(follow_uri))

def bookmark_post(client, post_uri: str, post_cid: str):
    """
    Saves a post to the account's private Bookmarks (Bluesky's official
    "Saved posts" feature, added Sept 2025). Not a public repo record --
    Bluesky stores it off-protocol for privacy, so there's no record URI
    to track; toggling later is done by referencing the ORIGINAL post's
    uri again. EXPERIMENTAL -- exact param names weren't independently
    confirmed against a live response, paste back the traceback if this
    errors.
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


def block_actor(client, did: str) -> str:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.graph.block",
            "record": {
                "$type": "app.bsky.graph.block",
                "subject": did,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return resp.uri


def unblock_actor(client, block_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(block_uri))

MUTED_WORDS_PREF_TYPE = "app.bsky.actor.defs#mutedWordsPref"

def get_muted_words(client) -> list:
    """
    Returns the account's muted words/tags as plain dicts:
    {"id", "value", "targets": ["content", "tag"]}. Uses the same
    raw-JSON-bypass helper as the saved-feeds prefs
    (_get_cleaned_preferences) instead of the typed
    client.app.bsky.actor.get_preferences() call this used to make --
    confirmed via a real 168-error pydantic validation traceback that
    the typed call chokes on the SAME FieldInfo/pyType bug across
    EVERY preference type on the account, not just muted words.
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
                "id": w["id"] or str(int(time.time() * 1000)),
                "value": w["value"],
                "targets": w["targets"] or ["content", "tag"],
                "actorTarget": "all",
            }
            for w in words
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


# ---------------- lists ----------------

LIST_PURPOSE_MOD = "app.bsky.graph.defs#modlist"
LIST_PURPOSE_CURATE = "app.bsky.graph.defs#curatelist"

LIST_URL_RE = re.compile(r"bsky\.app/profile/([^/]+)/lists/([a-zA-Z0-9]+)")


def get_lists(client, did: str, page_limit: int = 100, max_pages: int = 20) -> list:
    """
    Every list `did` created OR has subscribed to (mute/block) -- this
    matches what bsky.app's own "My lists" page shows, since
    getLists(actor=...) returns both kinds together. EXPERIMENTAL --
    first use of the app.bsky.graph.* list endpoints in NVSky, field
    names below were read off the atproto SDK's ListView model but not
    yet confirmed against a live response -- paste back the traceback
    if this errors.
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
    Fetches list info plus the COMPLETE member roster (walks every
    page, like get_followers) -- used for the modlist member view and
    Manage members, both of which need the whole list, not a lazy
    page. EXPERIMENTAL, see get_lists above.
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
    Same shape as sync_timeline -- feed_key is just the list's own
    at:// uri, so FeedListMixin/get_feed_page/upsert_feed_item work
    completely unchanged for a list's timeline. Only curatelists have
    a feed here -- modlists raise a server error if you try, callers
    must check purpose first. EXPERIMENTAL, see get_lists above.
    """
    resp = client.app.bsky.feed.get_list_feed(params={"list": list_uri, "limit": limit, "cursor": cursor})
    for item in resp.feed:
        try:
            _store_feed_item(item, account_id, list_uri)
        except Exception as e:
            log.error(f"NVSky: failed to store a list feed item: {e}")
            log.info(f"NVSky: raw item that failed = {item!r}")
    return resp.cursor


def create_list(client, name: str, description: str, purpose: str) -> dict:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.graph.list",
            "record": {
                "$type": "app.bsky.graph.list",
                "name": name,
                "description": description or "",
                "purpose": purpose,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return {"uri": resp.uri, "cid": resp.cid}


def delete_list(client, list_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(list_uri))


def add_list_member(client, list_uri: str, subject_did: str) -> str:
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.graph.listitem",
            "record": {
                "$type": "app.bsky.graph.listitem",
                "subject": subject_did,
                "list": list_uri,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return resp.uri


def remove_list_member(client, listitem_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(listitem_uri))


def mute_actor_list(client, list_uri: str):
    client.app.bsky.graph.mute_actor_list(data={"list": list_uri})


def unmute_actor_list(client, list_uri: str):
    client.app.bsky.graph.unmute_actor_list(data={"list": list_uri})


def block_actor_list(client, list_uri: str) -> str:
    """
    Bulk-blocking a whole list is a public repo record (parallel to
    block_actor's app.bsky.graph.block, since blocks are public)
    rather than a private procedure like mute_actor_list above. LOW
    CONFIDENCE -- "app.bsky.graph.listblock" as the collection name is
    from memory of the lexicon, not confirmed against the installed
    SDK's model classes yet. Paste back the traceback if this errors.
    """
    resp = client.com.atproto.repo.create_record(
        data={
            "repo": client.me.did,
            "collection": "app.bsky.graph.listblock",
            "record": {
                "$type": "app.bsky.graph.listblock",
                "subject": list_uri,
                "createdAt": client.get_current_time_iso(),
            },
        }
    )
    return resp.uri


def unblock_actor_list(client, listblock_uri: str):
    client.com.atproto.repo.delete_record(data=_parse_at_uri(listblock_uri))


def resolve_list_uri(client, value: str) -> str:
    """
    Accepts either a raw at:// list uri or a bsky.app list link
    (https://bsky.app/profile/<handle-or-did>/lists/<rkey>) -- pasting
    a link is the only way to subscribe to someone else's list right
    now, there's no list search endpoint in the API to browse them.
    """
    value = value.strip()
    if value.startswith("at://"):
        return value
    match = LIST_URL_RE.search(value)
    if not match:
        raise ValueError("Doesn't look like a Bluesky list link or at:// list URI.")
    actor, rkey = match.group(1), match.group(2)
    did = actor if actor.startswith("did:") else client.com.atproto.identity.resolve_handle(params={"handle": actor}).did
    return f"at://{did}/app.bsky.graph.list/{rkey}"


def search_actors_typeahead(client, query: str, limit: int = 8) -> list:
    """
    Quick "who did you mean" suggestions as the user types a handle/
    name -- used by Manage members' Add field, same behavior as
    bsky.app's own search-while-typing. Also the natural building
    block for a future @mention autocomplete in ComposeDialog.
    EXPERIMENTAL, see get_lists above.
    """
    if not query.strip():
        return []
    resp = client.app.bsky.actor.search_actors_typeahead(params={"q": query, "limit": limit})
    return [
        {"did": a.did, "handle": a.handle, "display_name": getattr(a, "display_name", None)}
        for a in resp.actors
    ]


def count_graphemes(text: str) -> int:
    """
    Approximates Unicode grapheme cluster count -- what Bluesky's
    server actually enforces via maxGraphemes on both posts and chat
    messages. Python's len() counts codepoints instead, which
    overcounts Thai text substantially (combining vowels/tone marks
    are separate codepoints that attach to the preceding consonant as
    ONE visual character/grapheme, not two) -- confirmed against a
    real side-by-side test against bsky.app (1704 graphemes vs len()'s
    1887 on the same Thai-heavy message). Also collapses basic ZWJ
    emoji sequences (e.g. family emoji) into one grapheme. Does NOT
    handle flag emoji (regional indicator pairs) or skin-tone
    modifiers -- rarer in practice, and Bluesky's own counter has
    quirks there too, so not worth the extra complexity right now.
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
    Splits `text` into (first, rest), targeting the MIDPOINT of `text`
    (capped at max_chars) rather than always maxing out the first
    chunk -- for a message near the overall length cap, putting as
    much as possible in the first chunk just pushes the overflow risk
    onto the second chunk instead. Aiming for the midpoint keeps both
    halves roughly balanced and safely under the per-cell limit for
    any message up to roughly 2 * max_chars total.

    The split never falls in the middle of a grapheme cluster -- a
    naive index slice can sever a Thai combining tone/vowel mark from
    its base consonant (or split a ZWJ emoji sequence), corrupting
    both halves. Uses the same category-based boundary logic as
    count_graphemes above, just recording each grapheme's start index
    instead of counting them.

    Within WHITESPACE_LOOKBACK chars of the target, prefers to land
    right after whitespace OR a punctuation mark (Unicode general
    category starting with "P" -- covers CJK full-width punctuation
    like "ใ€","ใ€","๏ผ","๏ผ" and Western ".", ",", "!", "?" alike), so
    the split lands on a natural phrase/sentence break instead of
    mid-word wherever the text has ANY such break nearby. Thai commonly
    has no punctuation or spaces at all within a short span, and CJK
    scripts don't reliably mark every word boundary either -- for text
    with no such landmark near the target, this still falls back to a
    grapheme-safe hard cut, which can land mid-word. There is no
    general-purpose dictionary-based word segmenter here (e.g.
    PyThaiNLP), which would be needed to close that gap fully, and is
    out of scope for this add-on.

    NOTE: this optimizes the common arrow-key-navigation read; it does
    NOT guarantee zero truncation in absolute worst case (e.g. a
    message at Bluesky's stated max length -- itself LOW CONFIDENCE,
    see plan-08.md -- combined with this app's own reply-preview
    prefix could theoretically still slightly exceed a 2-column
    budget). "Show message..." (chatWindow.py) is the dialog that
    reads the full, unsplit text and is the actual guaranteed-complete
    fallback for that edge case.

    Used by chatWindow.py to spread a long message across two
    ListCtrl columns (Message / Message (more)) to work around
    Windows' native per-cell text limit (confirmed by testing to sit
    around ~511 characters, cause not fully pinned down -- see
    plan-09.md). Returns (text, "") unchanged if text already fits
    within max_chars.
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
