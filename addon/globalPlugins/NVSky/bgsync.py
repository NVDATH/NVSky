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
        # Translators: Display name for the Home timeline in sync-change announcements.
        return _("Home")
    if feed_key == "discover":
        # Translators: Display name for the Discover feed in sync-change announcements.
        return _("Discover")
    for feed in db.get_saved_feeds_cache(account_id):
        if feed.get("uri") == feed_key:
            # Translators: Fallback display name for a saved feed with no cached name.
            return feed.get("display_name") or _("Home")
    # Translators: Display name for the Home timeline in sync-change announcements.
    return _("Home")


def _thread_display_name(posts: list) -> str:
    if not posts:
        # Translators: Fallback display name for a thread with no cached posts.
        return _("Thread")
    first = posts[0]
    handle = first.get("handle") or first.get("author_did") or "?"
    preview = (first.get("text") or "")[:30]
    if preview:
        # Translators: Thread display name with a text preview. First {} is the handle, second {} is a preview of the first post.
        return _("Thread: @{}: {}").format(handle, preview)
    # Translators: Thread display name with no text preview available. {} is the handle.
    return _("Thread: @{}").format(handle)


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
        # Translators: Display name for the Notifications tab in sync-change announcements.
        return True, [_("Notifications")]
    return False, []


def sync_saved(atprotoClient, account_id: int):
    before = db.get_feed_page(account_id, "saved", limit=1)
    oldTopUri = before[0]["uri"] if before else None
    client.sync_saved(atprotoClient, account_id, limit=PAGE_SIZE)
    after = db.get_feed_page(account_id, "saved", limit=1)
    newTopUri = after[0]["uri"] if after else None
    if newTopUri and newTopUri != oldTopUri:
        # Translators: Display name for the Saved tab in sync-change announcements.
        return True, [_("Saved")]
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


def sync_lists(atprotoClient, account_id: int, my_did: str):
    # Unlike Home (one active filter at a time), ListsWindow shows a
    # TREE of every list at once -- so background sync covers every
    # curation list I own, not just whichever one happens to be
    # selected right now (no way for this pure function to know that
    # anyway). Also covers any list open as its own ListTabWindow temp
    # tab, which may belong to someone else entirely (e.g. opened via
    # Find lists by user).
    changed = False
    names = []

    beforeLists = db.get_lists(account_id)
    beforeSnapshot = {l["list_uri"]: (l.get("name"), l.get("muted")) for l in beforeLists}
    try:
        entries = client.get_lists(atprotoClient, my_did)
        for entry in entries:
            db.upsert_list({
                "account_id": account_id,
                "list_uri": entry["uri"],
                "cid": entry["cid"],
                "name": entry["name"],
                "description": entry["description"],
                "purpose": entry["purpose"],
                "creator_did": entry["creator_did"],
                "creator_handle": entry["creator_handle"],
                "muted": int(entry["muted"]),
                "blocked_uri": entry["blocked_uri"],
            })
    except Exception as e:
        log.error(f"NVSky: background list-of-lists sync failed: {e}")
    afterLists = db.get_lists(account_id)
    afterSnapshot = {l["list_uri"]: (l.get("name"), l.get("muted")) for l in afterLists}
    if afterSnapshot != beforeSnapshot:
        changed = True
        # Translators: Display name for the Lists tab in sync-change announcements.
        names.append(_("Lists"))

    targets = {}
    for lst in afterLists:
        if lst["purpose"] == client.LIST_PURPOSE_CURATE:
            targets[lst["list_uri"]] = lst["name"]
    for entry in db.get_open_temp_tabs(account_id):
        if entry.get("type") != "list":
            continue
        listUri = entry.get("list_uri")
        if listUri and listUri not in targets:
            # Translators: Fallback display name for a list with no cached name.
            targets[listUri] = entry.get("custom_name") or entry.get("list_name", _("List"))

    for listUri, displayName in targets.items():
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
            changed = True
            names.append(displayName)
    return changed, names


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
            # Translators: Fallback display name for a search-preview tab with no cached name.
            names.append(entry.get("custom_name") or entry.get("name", _("Search")))
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
                # Translators: Fallback owner label when a user-timeline tab has no name cached.
                owner = entry.get("owner_label", _("user"))
                # Translators: Display name for a user-timeline tab in sync-change announcements. {} is the account owner.
                names.append(entry.get("custom_name") or _("Timeline of {}").format(owner))
        elif entryType == "user_list":
            kind = entry.get("list_kind")
            cacheKey = entry.get("key")
            if not kind or not cacheKey:
                continue
            before = db.get_user_list_cache(account_id, cacheKey)
            try:
                if kind in ("followers", "following", "known_followers"):
                    did = entry.get("did")
                    if not did:
                        continue
                    if kind == "followers":
                        users = client.get_followers(atprotoClient, did)
                    elif kind == "following":
                        users = client.get_follows(atprotoClient, did)
                    else:
                        users = client.get_known_followers(atprotoClient, did)
                elif kind in ("likes", "reposts"):
                    # BUG FIX: these used to fall into the "search"
                    # else-branch below, which requires entry["query"]
                    # -- likes/reposts entries store "post_uri" instead
                    # (see _openUserListForPost), so query was always
                    # None and background sync silently skipped these
                    # tabs every single tick since C shipped.
                    postUri = entry.get("post_uri")
                    if not postUri:
                        continue
                    users = (
                        client.get_post_likes(atprotoClient, postUri) if kind == "likes"
                        else client.get_post_reposted_by(atprotoClient, postUri)
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
                if kind in ("followers", "following", "known_followers"):
                    kindLabels = {
                        # Translators: Kind label in a "{kind} of {owner}" sync-change summary.
                        "followers": _("Followers"),
                        # Translators: Kind label in a "{kind} of {owner}" sync-change summary.
                        "following": _("Following"),
                        # Translators: Kind label in a "{kind} of {owner}" sync-change summary.
                        "known_followers": _("Known followers"),
                    }
                    # Translators: Composite display name for a followers/following list change. First {} is the kind, second {} is the account owner.
                    label = _("{} of {}").format(kindLabels[kind], entry.get("owner_label", _("user")))
                elif kind in ("likes", "reposts"):
                    kindLabels = {
                        # Translators: Kind label in a "{kind} on {post}" sync-change summary.
                        "likes": _("Likes"),
                        # Translators: Kind label in a "{kind} on {post}" sync-change summary.
                        "reposts": _("Reposts"),
                    }
                    # Translators: Composite display name for a likes/reposts list change. First {} is the kind, second {} is the post owner.
                    label = _("{} on {}").format(kindLabels[kind], entry.get("owner_label", _("post")))
                else:
                    # Translators: Display name for a people-search tab in sync-change announcements. {} is the search query.
                    label = _("People search: {}").format(entry.get("query", ""))
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
    "lists": (sync_lists, True),
    "search": (sync_search, False),
    "profile": (sync_profile, False),
    "thread": (sync_thread, False),
}