"""
Background sync scheduler logic for NVSky.

Pure functions on client/db only -- no wx panel access at all, so these
work whether or not MainWindow is currently open (see __init__.py's
GlobalPlugin timer, which calls into this module directly).

Each sync_* function does the network sync for one category, then
diffs before/after to report whether anything actually changed --
callers use this to decide whether to announce/reload a visible panel.
Returns (changed: bool, summary: str or None).
"""
from logHandler import log

from . import client
from . import db

PAGE_SIZE = 50


def _home_feed_display_name(account_id: int, feed_key: str) -> str:
    if feed_key == "home":
        return "Home"
    if feed_key == "discover":
        return "Discover"
    for feed in db.get_saved_feeds_cache(account_id):
        if feed.get("uri") == feed_key:
            return feed.get("display_name") or "Home"
    return "Home"


def _thread_display_name(posts: list) -> str:
    if not posts:
        return "Thread"
    first = posts[0]
    handle = first.get("handle") or first.get("author_did") or "?"
    preview = (first.get("text") or "")[:30]
    return f"Thread: @{handle}: {preview}" if preview else f"Thread: @{handle}"


def sync_home(atprotoClient, account_id: int):
    feedKey = db.get_home_active_filter(account_id)
    before = db.get_feed_page(account_id, feedKey, limit=1)
    oldTopUri = before[0]["uri"] if before else None

    if feedKey in ("home", "discover"):
        client.sync_timeline(atprotoClient, account_id, limit=PAGE_SIZE, feed_key=feedKey)
    else:
        # A custom saved feed -- feedKey IS the feed generator's uri.
        client.sync_feed_generator_page(atprotoClient, account_id, feedKey, limit=PAGE_SIZE)

    after = db.get_feed_page(account_id, feedKey, limit=1)
    newTopUri = after[0]["uri"] if after else None
    if newTopUri and newTopUri != oldTopUri:
        return True, [_home_feed_display_name(account_id, feedKey)]
    return False, []


def sync_notifications(atprotoClient, account_id: int):
    beforeCount = db.get_unread_notification_count(account_id)
    client.sync_notifications(atprotoClient, account_id, limit=PAGE_SIZE)
    afterCount = db.get_unread_notification_count(account_id)
    if afterCount > beforeCount:
        return True, ["Notifications"]
    return False, []


def sync_saved(atprotoClient, account_id: int):
    before = db.get_feed_page(account_id, "saved", limit=1)
    oldTopUri = before[0]["uri"] if before else None
    client.sync_saved(atprotoClient, account_id, limit=PAGE_SIZE)
    after = db.get_feed_page(account_id, "saved", limit=1)
    newTopUri = after[0]["uri"] if after else None
    if newTopUri and newTopUri != oldTopUri:
        return True, ["Saved"]
    return False, []


def sync_chat(atprotoClient, account_id: int, my_did: str):
    beforeConvos = db.get_convos(account_id)
    beforeSnapshot = {
        c["convo_id"]: (c.get("unread_count") or 0, c.get("last_message_sent_at")) for c in beforeConvos
    }
    client.sync_convos(atprotoClient, account_id, my_did)
    afterConvos = db.get_convos(account_id)
    afterSnapshot = {
        c["convo_id"]: (c.get("unread_count") or 0, c.get("last_message_sent_at")) for c in afterConvos
    }
    changedIds = {cid for cid, val in afterSnapshot.items() if beforeSnapshot.get(cid) != val}
    if not changedIds:
        return False, []
    names = []
    for convo in afterConvos:
        if convo["convo_id"] in changedIds:
            members = db.get_convo_members(account_id, convo["convo_id"])
            names.append(db.describe_convo_from_members(convo, members))
    return True, names


def sync_lists(atprotoClient, account_id: int):
    names = []
    for entry in db.get_open_temp_tabs(account_id):
        if entry.get("type") != "list":
            continue
        listUri = entry.get("list_uri")
        if not listUri:
            continue
        before = db.get_feed_page(account_id, listUri, limit=1)
        oldTopUri = before[0]["uri"] if before else None
        try:
            client.sync_list_feed(atprotoClient, account_id, listUri, limit=PAGE_SIZE)
        except Exception as e:
            log.error(f"NVSky: background list sync failed for {listUri}: {e}")
            continue
        after = db.get_feed_page(account_id, listUri, limit=1)
        newTopUri = after[0]["uri"] if after else None
        if newTopUri and newTopUri != oldTopUri:
            names.append(entry.get("custom_name") or entry.get("list_name", "List"))
    return bool(names), names


def sync_search(atprotoClient, account_id: int):
    names = []
    for entry in db.get_open_temp_tabs(account_id):
        if entry.get("type") != "search_preview":
            continue
        feedKey = entry.get("key")
        kind = entry.get("kind", "feed")
        sourceKey = entry.get("source_key")
        if not feedKey or not sourceKey:
            continue
        before = db.get_feed_page(account_id, feedKey, limit=1)
        oldTopUri = before[0]["uri"] if before else None
        try:
            if kind == "feed":
                client.sync_feed_generator_page(atprotoClient, account_id, sourceKey, limit=PAGE_SIZE)
            else:
                filters = entry.get("filters") or {}
                client.sync_search_page(
                    atprotoClient, account_id, feedKey, sourceKey, limit=PAGE_SIZE,
                    author=filters.get("author"), since=filters.get("since"),
                    until=filters.get("until"), lang=filters.get("lang"),
                )
        except Exception as e:
            log.error(f"NVSky: background search sync failed for {feedKey}: {e}")
            continue
        after = db.get_feed_page(account_id, feedKey, limit=1)
        newTopUri = after[0]["uri"] if after else None
        if newTopUri and newTopUri != oldTopUri:
            names.append(entry.get("custom_name") or entry.get("name", "Search"))
    return bool(names), names


def sync_profile(atprotoClient, account_id: int):
    names = []
    for entry in db.get_open_temp_tabs(account_id):
        entryType = entry.get("type")
        if entryType == "user_timeline":
            did = entry.get("did")
            if not did:
                continue
            feedKey = f"user_timeline:{did}"
            before = db.get_feed_page(account_id, feedKey, limit=1)
            oldTopUri = before[0]["uri"] if before else None
            try:
                client.sync_author_feed_page(atprotoClient, account_id, did, limit=PAGE_SIZE)
            except Exception as e:
                log.error(f"NVSky: background user timeline sync failed for {did}: {e}")
                continue
            after = db.get_feed_page(account_id, feedKey, limit=1)
            newTopUri = after[0]["uri"] if after else None
            if newTopUri and newTopUri != oldTopUri:
                owner = entry.get("owner_label", "user")
                names.append(entry.get("custom_name") or f"Timeline of {owner}")
        elif entryType == "user_list":
            kind = entry.get("list_kind")
            cacheKey = entry.get("key")
            if not kind or not cacheKey:
                continue
            before = db.get_user_list_cache(account_id, cacheKey)
            try:
                if kind in ("followers", "following"):
                    did = entry.get("did")
                    if not did:
                        continue
                    users = (
                        client.get_followers(atprotoClient, did) if kind == "followers"
                        else client.get_follows(atprotoClient, did)
                    )
                else:
                    query = entry.get("query")
                    if not query:
                        continue
                    response = client.search_actors(atprotoClient, query)
                    users = [
                        {
                            "did": a.did, "handle": a.handle,
                            "display_name": getattr(a, "display_name", None),
                            "description": getattr(a, "description", None),
                        }
                        for a in response.actors
                    ]
            except Exception as e:
                log.error(f"NVSky: background user_list sync failed for {cacheKey}: {e}")
                continue
            db.set_user_list_cache(account_id, cacheKey, users)
            if users != (before or []):
                if kind in ("followers", "following"):
                    label = f"{kind.capitalize()} of {entry.get('owner_label', 'user')}"
                else:
                    label = f"People search: {entry.get('query', '')}"
                names.append(entry.get("custom_name") or label)
    return bool(names), names


def sync_thread(atprotoClient, account_id: int):
    names = []
    for entry in db.get_open_temp_tabs(account_id):
        if entry.get("type") != "thread":
            continue
        rootUri = entry.get("root_uri")
        if not rootUri:
            continue
        cacheKey = f"thread:{rootUri}"
        before = db.get_user_list_cache(account_id, cacheKey) or []
        try:
            posts, _targetIndex = client.get_thread(atprotoClient, rootUri)
        except Exception as e:
            log.error(f"NVSky: background thread sync failed for {rootUri}: {e}")
            continue
        db.set_user_list_cache(account_id, cacheKey, posts)
        if len(posts) != len(before):
            names.append(entry.get("custom_name") or _thread_display_name(posts))
    return bool(names), names


# category key -> (sync function, needs_my_did)
SYNC_FUNCTIONS = {
    "home": (sync_home, False),
    "notifications": (sync_notifications, False),
    "saved": (sync_saved, False),
    "chat": (sync_chat, True),
    "lists": (sync_lists, False),
    "search": (sync_search, False),
    "profile": (sync_profile, False),
    "thread": (sync_thread, False),
}