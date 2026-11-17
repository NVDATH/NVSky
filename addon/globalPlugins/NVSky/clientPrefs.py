"""
Account preference calls for NVSky: saved feeds, muted words, content-label visibility and
notification settings. Re-exported by client.py.
"""
import json
import random
import string
import time

from logHandler import log




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
