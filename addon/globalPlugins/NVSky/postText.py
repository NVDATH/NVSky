"""
Post/embed text helpers for NVSky: pure functions and small shared state
helpers used by every post-list tab and by the userActions/itemActions/
embedView mixins. Leaf module: nothing here imports feedWindow.
"""
import core
import datetime
import json
import urllib.parse
import speech
import ui as nvdaUi

from . import db
from . import client
from . import timeutils
from . import uiutil
from .compose import CONTENT_LABEL_DISPLAY_NAMES

COLUMN_DISPLAY_NAME = "display_name"

REPORT_REASONS = [
    # Translators: Report reason choice.
    (_("Spam"), "com.atproto.moderation.defs#reasonSpam"),
    # Translators: Report reason choice.
    (_("Violates community guidelines"), "com.atproto.moderation.defs#reasonViolation"),
    # Translators: Report reason choice.
    (_("Misleading"), "com.atproto.moderation.defs#reasonMisleading"),
    # Translators: Report reason choice.
    (_("Sexual content"), "com.atproto.moderation.defs#reasonSexual"),
    # Translators: Report reason choice.
    (_("Rude or harassing"), "com.atproto.moderation.defs#reasonRude"),
    # Translators: Report reason choice.
    (_("Other"), "com.atproto.moderation.defs#reasonOther"),
]


def _announce_now(message: str):
    """
    Speaks `message`, cutting off current speech. Delayed briefly because
    the ListCtrl's own focus announcement is often queued after a menu
    closes and would mask it; cancelSpeech() clears that first.
    """
    def _speak():
        speech.cancelSpeech()
        nvdaUi.message(message, speechPriority=speech.priorities.Spri.NOW)

    core.callLater(100, _speak)


def _format_post_time(iso_timestamp: str) -> str:
    mode, pattern = timeutils.current_mode_and_pattern(db)
    return timeutils.format_timestamp(iso_timestamp, mode=mode, custom_pattern=pattern)


def _is_gif_url(url) -> bool:
    # The GIF picker posts a Tenor media address as an external embed.
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower() == "media.tenor.com" or parsed.path.lower().endswith(".gif")


def _embed_sound_event(embed_json: str):
    """Maps an embed's $type to a soundpack event key, or None. A quote
    with attached media plays the media's sound (image/video/link)."""
    if not embed_json:
        return None
    try:
        embed = json.loads(embed_json)
    except (ValueError, TypeError):
        return None
    embed_type = embed.get("$type", "")
    if "recordWithMedia" in embed_type:
        if embed.get("images"):
            return "embed_image"
        if embed.get("video_url") or embed.get("video_alt") is not None:
            return "embed_video"
        if embed.get("link_url"):
            return "embed_gif" if _is_gif_url(embed["link_url"]) else "embed_link"
        return "embed_quote"
    if "images" in embed_type or "gallery" in embed_type:
        return "embed_image"
    if "video" in embed_type:
        return "embed_video"
    if "record" in embed_type:
        return "embed_quote"
    if "external" in embed_type:
        return "embed_gif" if _is_gif_url(embed.get("link_url")) else "embed_link"
    return None


def _describe_embed(embed_json: str) -> str:
    if not embed_json:
        return ""
    try:
        embed = json.loads(embed_json)
    except (ValueError, TypeError):
        return ""

    embed_type = embed.get("$type", "")

    if "images" in embed_type or "gallery" in embed_type:
        images = embed.get("images", [])
        alts = [img.get("alt") for img in images if img.get("alt")]
        # Translators: Embed summary for a post with more than one image. {} is the count.
        # Translators: Embed summary for a post with exactly one image.
        label = _("Image ({})").format(len(images)) if len(images) > 1 else _("Image")
        if alts:
            # Translators: Appended to an image embed summary to list each image's alt text. {} is a semicolon-separated list of alt texts.
            label += _(": {}").format("; ".join(alts))
        return label
    if "video" in embed_type:
        alt = embed.get("video_alt")
        # Translators: Embed summary for a post with a video, showing its alt text. {} is the alt text.
        # Translators: Embed summary for a post with a video and no alt text.
        return _("Video: {}").format(alt) if alt else _("Video")
    if "recordWithMedia" in embed_type:
        if embed.get("images"):
            # Translators: Embed summary for a quote post that also carries attached image(s).
            return _("Quote + image")
        if embed.get("video_url") or embed.get("video_alt") is not None:
            # Translators: Embed summary for a quote post that also carries an attached video.
            return _("Quote + video")
        if embed.get("link_url"):
            if _is_gif_url(embed["link_url"]):
                # Translators: Embed summary for a quote post that also carries an animated GIF.
                return _("Quote + animated GIF")
            # Translators: Embed summary for a quote post that also carries a link-preview card.
            return _("Quote + link")
        # Translators: Fallback embed summary for a quote post with media whose type couldn't be determined.
        return _("Quote + media")
    if "record" in embed_type:
        # Translators: Embed summary for a plain quote post.
        return _("Quote post")
    if "external" in embed_type:
        if _is_gif_url(embed.get("link_url")):
            gifTitle = embed.get("link_title")
            # Translators: Embed summary for a post with an animated GIF, showing its description. {} is the description.
            # Translators: Embed summary for a post with an animated GIF and no description.
            return _("Animated GIF: {}").format(gifTitle) if gifTitle else _("Animated GIF")
        # Translators: Embed summary for a post with a link-preview card.
        return _("Link")
    return ""


def _post_web_url(uri, handle_or_did) -> str:
    if not uri or not handle_or_did:
        return ""
    return f"https://bsky.app/profile/{handle_or_did}/post/{uri.rsplit('/', 1)[-1]}"


def _compose_copy_text(parts, url) -> str:
    text = ", ".join(p for p in parts if p)
    if url:
        return f"{text} ({url})" if text else url
    return text


def _message_text(post: dict, single: bool = True) -> str:
    text = post.get("text", "")

    if post.get("quoted_text"):
        quotedAuthor = post.get("quoted_author_handle")
        # Translators: Fallback quoted-author label when no handle is cached.
        who = f"@{quotedAuthor}" if quotedAuthor else _("original post")
        # No em dash -- screen readers spell it out as two syllables.
        # Translators: Appended to a post's text to show the quoted post. First {} is the quoted post's text, second {} is who it's from (already localized).
        if single:
            text = _("{} Quote from {}: {}").format(text, who, post["quoted_text"])
        else:
            # Translators: Heading above a quoted post's text in the full-post dialog. {} is who it's from (already localized).
            text = "\n\n".join(part for part in (text, _("Quote from {}:").format(who), post["quoted_text"]) if part)

    if post.get("reply_parent_uri"):
        replyToHandle = post.get("reply_to_handle")
        if replyToHandle:
            # Translators: Prefix on a reply's post text. First {} is the handle being replied to, second {} is the reply's own text.
            text = _("Reply to @{}: {}").format(replyToHandle, text)

    # The Author column already shows who reposted it (see _authorLabel) --
    # this just needs to name the ORIGINAL author, not repeat the reposter.
    if post.get("is_repost"):
        originalHandle = post.get("handle") or post.get("author_did")
        if originalHandle:
            # Translators: Prefix on a repost's post text, naming the original author. First {} is their handle, second {} is the post text.
            text = _("Reposted @{}: {}").format(originalHandle, text)
        else:
            # Translators: Prefix on a repost's post text when the original author isn't cached. {} is the post text.
            text = _("Reposted: {}").format(text)

    return uiutil.single_line(text) if single else text


# Translators: Short placeholder shown in the Embed column for a content-labeled ("warn") post -- kept generic/brief, the actual category names go in the Message column instead.
CONTENT_WARNING_EMBED_TEXT = _("Warning")
# Translators: Placeholder shown in the Message column for a content-labeled ("warn") post, in place of the real text. {} is a comma-separated list of the matched category names.
CONTENT_WARNING_MESSAGE_TEXT = _("(Content warning: {})")


def _label_visibility(post: dict) -> str:
    """
    Returns "show"/"warn"/"hide" for `post`, based on its cached
    labels_json and the account's cached content-label preferences
    (db.get_content_label_prefs_cache -- see Settings > Content
    labels). Only the standard adult-content labels
    (client.CONTENT_LABEL_KEYS) are considered; any other label a
    labeler might attach is ignored. If a post carries more than one
    matching label, the most restrictive visibility wins.
    """
    labelsJson = post.get("labels_json")
    if not labelsJson:
        return "show"
    try:
        labels = json.loads(labelsJson)
    except (ValueError, TypeError):
        return "show"
    if not labels:
        return "show"

    account = db.get_active_account()
    prefsCache = db.get_content_label_prefs_cache(account["id"]) if account else {}

    order = {"show": 0, "warn": 1, "hide": 2}
    worst = "show"
    for label in labels:
        if label not in client.CONTENT_LABEL_KEYS:
            continue
        visibility = client.effective_label_visibility(prefsCache, label)
        if order.get(visibility, 0) > order.get(worst, 0):
            worst = visibility
    return worst


def _matched_label_names(post: dict) -> str:
    labelsJson = post.get("labels_json")
    if not labelsJson:
        return ""
    try:
        labels = json.loads(labelsJson)
    except (ValueError, TypeError):
        return ""
    return ", ".join(CONTENT_LABEL_DISPLAY_NAMES.get(l, l) for l in labels if l in client.CONTENT_LABEL_KEYS)


def _visible_embed_text(post: dict) -> str:
    if _label_visibility(post) == "warn":
        return CONTENT_WARNING_EMBED_TEXT
    return _describe_embed(post.get("embed_json"))


def _visible_message_text(post: dict) -> str:
    if _label_visibility(post) == "warn":
        return CONTENT_WARNING_MESSAGE_TEXT.format(_matched_label_names(post))
    return _message_text(post)


def _post_detail_rows(post: dict) -> list:
    """[(line, link or None)] for the full-post dialog: links in the post, its address, its creation time."""
    urls = []
    try:
        facets = json.loads(post.get("facets_json") or "[]")
    except (ValueError, TypeError):
        facets = []
    for facet in facets if isinstance(facets, list) else []:
        features = facet.get("features") if isinstance(facet, dict) else None
        for feature in features or []:
            uri = feature.get("uri") if isinstance(feature, dict) else None
            if uri and uri not in urls:
                urls.append(uri)
    if not post.get("facets_json"):
        # Thread and quote tabs keep no facets; the text holds the full links.
        for url in client.find_all_urls(post.get("text") or ""):
            if url not in urls:
                urls.append(url)
    try:
        embed = json.loads(post.get("embed_json") or "{}")
    except (ValueError, TypeError):
        embed = {}
    linkUrl = embed.get("link_url") if isinstance(embed, dict) else None
    if linkUrl and linkUrl not in urls:
        urls.append(linkUrl)

    rows = []
    for url in urls:
        # Translators: Detail line of the full-post dialog. {} is a link in the post.
        rows.append((_("link: {}").format(url), url))
    webUrl = _post_web_url(post.get("uri"), post.get("handle") or post.get("author_did"))
    if webUrl:
        # Translators: Detail line of the full-post dialog. {} is the address of the post on bsky.app.
        rows.append((_("post url: {}").format(webUrl), webUrl))
    created = post.get("created_at") or post.get("indexed_at")
    if created:
        # Translators: Detail line of the full-post dialog. {} is when the post was created.
        rows.append((_("created at: {}").format(timeutils.format_timestamp(created, mode="absolute")), None))
    return rows


def _full_post_text(post: dict) -> str:
    """Text of the full-post dialog: a content warning above the embed summary and the unflattened message."""
    lines = []
    if _label_visibility(post) == "warn":
        lines.append(CONTENT_WARNING_MESSAGE_TEXT.format(_matched_label_names(post)))
    lines.append(_describe_embed(post.get("embed_json")))
    lines.append(_message_text(post, single=False))
    return "\n".join(line for line in lines if line)


def _show_full_post(window, post):
    """Ctrl+Space in the thread and quotes tabs (their windows have no FeedListMixin)."""
    if post is None:
        # Translators: Announced when the post-action menu is invoked with no post focused.
        nvdaUi.message(_("No post selected."))
        return
    from . import postDetails
    # Translators: Title of the full-post dialog. {} is the post's author.
    title = _("Post from {}").format(window._displayLabel(post.get("handle"), post.get("display_name")))
    postDetails.show_post_details(window, title, _full_post_text(post), _post_detail_rows(post), post)


def propagate_post_state(post: dict):
    """Copies viewer state onto every other open tab's in-memory copy of the same post."""
    from . import get_main_window
    mainWindow = get_main_window()
    if mainWindow is None:
        return
    fields = ("viewer_like_uri", "viewer_repost_uri", "viewer_bookmarked", "viewer_thread_muted")
    for panel in mainWindow.getOpenTabs():
        for other in getattr(panel, "_posts", None) or []:
            if other is not post and other.get("uri") == post.get("uri"):
                for field in fields:
                    if field in post:
                        other[field] = post[field]


def sync_like_state(post: dict, account_id: int):
    """Persists a like/unlike made outside the feed tabs and mirrors it to every open tab."""
    db.set_post_like_uri(post["uri"], post.get("viewer_like_uri"))
    propagate_post_state(post)
    if post.get("viewer_like_uri"):
        likedAt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        db.upsert_feed_item(account_id, "likes", post["uri"], likedAt)
    else:
        db.delete_feed_item(account_id, "likes", post["uri"])
    from . import get_main_window
    mainWindow = get_main_window()
    if mainWindow is None:
        return
    for panel in mainWindow.getOpenTabs():
        if getattr(panel, "TAB_KEY", None) == "likes":
            panel._loadFromCache(reset=True)


def _search_feed_key(query, filters):
    filters = filters or {}
    parts = [query, filters.get("author") or "", filters.get("since") or "", filters.get("until") or "", filters.get("lang") or ""]
    return "search:" + "|".join(parts)
