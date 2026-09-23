"""
Main feed window for NVSky. v0.9

Home timeline view: report-mode ListCtrl (Author, Message, Posted, Embed),
shows everything cached on open, lazy-loads from the network once the
cache runs out. Other tabs are not implemented yet -- Home only.
"""
import core
import datetime
import json
import threading
import webbrowser
import wx
import tones
import speech

import gui
import gui.nvdaControls
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import attachments
from . import timeutils
from . import uiutil
from . import soundpack
from .compose import ComposeDialog

LAZY_LOAD_THRESHOLD = 3
PAGE_SIZE = 50
LOADING_BEEP_INTERVAL_MS = 1000
MAX_NETWORK_PAGE_WALK = 5

COLUMN_DISPLAY_NAME = "display_name"
COLUMN_HANDLE = "handle"

# Translators: Permanent tab label for the Home feed.
TAB_NAME = _("Home")

# Maps FeedWindow's filter RadioBox selection index to the feed_key used
# for both local caching (feed_items.feed_key) and which server feed
# sync_timeline() actually fetches. Both are confirmed real Bluesky
# system feeds -- Following (chronological, followed accounts only) and
# Discover (Bluesky's own official algorithmic feed, formerly "What's
# Hot"). A third "For You" option was deliberately NOT added here --
# it isn't a Bluesky system feed at all, just a common NAME third-party
# custom feed creators give their own feeds (different AT-URI per
# creator) -- that belongs in the future Saved tab (pin any custom feed
# by URI), not hardcoded into this filter.
FILTER_INDEX_TO_FEED_KEY = {0: "home", 1: "discover"}
FEED_KEY_TO_FILTER_INDEX = {"home": 0, "discover": 1}

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
    Speaks `message`, cutting off whatever NVDA is currently reading --
    speechPriority=NOW alone isn't enough right after a popup menu
    closes or focus shifts, since the ListCtrl's own focus announcement
    is often queued AFTER this fires, not before, and ends up masking
    it anyway. Delaying ~200ms lets that settle first, then
    cancelSpeech() clears it before speaking -- same technique already
    confirmed working for post-action result announcements.
    """
    def _speak():
        speech.cancelSpeech()
        nvdaUi.message(message, speechPriority=speech.priorities.Spri.NOW)

    core.callLater(100, _speak)


def _format_post_time(iso_timestamp: str) -> str:
    mode, pattern = timeutils.current_mode_and_pattern(db)
    return timeutils.format_timestamp(iso_timestamp, mode=mode, custom_pattern=pattern)


def _embed_sound_event(embed_json: str):
    """Maps an embed's $type to a soundpack event key, or None if the
    post has no embed (or an embed type this doesn't cover). A quote
    post with attached media (recordWithMedia) plays whichever media
    sound matches what's actually attached (image/video), same fix as
    _describe_embed's own recordWithMedia branch just above -- was
    previously always "embed_quote" regardless of the attached media
    type, which meant the sound (and the on-screen "Quote + media"
    text) never actually told the image/video apart even though
    _extract_embed_info (client.py) already resolves that distinction."""
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
            return "embed_link"
        return "embed_quote"
    if "images" in embed_type or "gallery" in embed_type:
        return "embed_image"
    if "video" in embed_type:
        return "embed_video"
    if "record" in embed_type:
        return "embed_quote"
    if "external" in embed_type:
        return "embed_link"
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
            # Translators: Embed summary for a quote post that also carries a link-preview card.
            return _("Quote + link")
        # Translators: Fallback embed summary for a quote post with media whose type couldn't be determined.
        return _("Quote + media")
    if "record" in embed_type:
        # Translators: Embed summary for a plain quote post.
        return _("Quote post")
    if "external" in embed_type:
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


def _message_text(post: dict) -> str:
    text = post.get("text", "")

    if post.get("quoted_text"):
        quotedAuthor = post.get("quoted_author_handle")
        # Translators: Fallback quoted-author label when no handle is cached.
        who = f"@{quotedAuthor}" if quotedAuthor else _("original post")
        # No em dash -- screen readers spell it out as two syllables.
        # Translators: Appended to a post's text to show the quoted post. First {} is the quoted post's text, second {} is who it's from (already localized).
        text = _("{} Quote from {}: {}").format(text, who, post["quoted_text"])

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

    return uiutil.single_line(text)


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
    return ", ".join(l for l in labels if l in client.CONTENT_LABEL_KEYS)


def _visible_embed_text(post: dict) -> str:
    if _label_visibility(post) == "warn":
        return CONTENT_WARNING_EMBED_TEXT
    return _describe_embed(post.get("embed_json"))


def _visible_message_text(post: dict) -> str:
    if _label_visibility(post) == "warn":
        return CONTENT_WARNING_MESSAGE_TEXT.format(_matched_label_names(post))
    return _message_text(post)


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


class RemovableTabMixin:
    """
    Shared behavior for removable temp tabs that (a) show a simple
    "<TAB_NAME> - NVSky" window title, and (b) jump back to whichever
    permanent tab they were opened FROM when closed via Ctrl+W. Was
    byte-for-byte duplicated across ConvoTabWindow (chatWindow.py),
    ListTabWindow, FeedPreviewTabWindow, UserListTabWindow,
    UserTimelineTabWindow, and ThreadTabWindow before this.

    A host class must set self._originTabKey (str or None) before use,
    and call self._jumpBackToOrigin() from its own onTabRemoved().
    """

    def _updateTitle(self):
        # self._account may not exist on every RemovableTabMixin host's
        # exact attribute name in theory, but every current host sets
        # it in __init__ before this is ever called -- matches the
        # pattern FeedListMixin._updateTitle/ChatWindow._updateTitle
        # already use, which this was missing (title bar showed no
        # account label at all, inconsistent with every other tab).
        # Translators: Fallback account label in the window title when no account is active.
        accountLabel = self._account["handle"] if getattr(self, "_account", None) else _("no account")
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")

    def _jumpBackToOrigin(self):
        if not self._originTabKey:
            return
        mainWindow = self.GetTopLevelParent()
        for panel in mainWindow.getOpenTabs():
            identity = mainWindow._getTabIdentity(panel)
            if identity and identity.get("key") == self._originTabKey:
                mainWindow.notebook.SetSelection(mainWindow.notebook.FindPage(panel))
                break

    def onTabRenamed(self, newName):
        # Not user-renameable in the UI currently -- no-op stub so
        # MainWindow.renameCurrentTab's getattr(...) check stays
        # consistent, in case that changes later.
        pass


class UserActionMixin:
    """
    Shared "act on a user" menu + implementations, mixed into any dialog
    that needs to offer user actions -- FeedWindow's user action menu,
    UserListDialog, and ProfileDialog (via Alt+U) all share this single
    implementation instead of drifting copies.
    """

    def _addMenuItem(self, menu, label, callback):
        item = menu.Append(wx.ID_ANY, label)
        self.Bind(wx.EVT_MENU, lambda evt: callback(), item)
        return item

    def _populateUserActionMenu(self, menu, did, handle, display_name=None):
        browseMenu = wx.Menu()
        # Translators: Submenu item under "View...", opens the user's profile.
        self._addMenuItem(browseMenu, _("&Profile..."), lambda: self.viewProfile(did))
        # Translators: Submenu item under "View...", opens the user's timeline.
        self._addMenuItem(browseMenu, _("&Timeline..."), lambda: self.showTimeline(did, handle, display_name))
        # Translators: Submenu item under "View...", opens the user's followers list.
        self._addMenuItem(browseMenu, _("&Followers..."), lambda: self.showFollowers(did, handle, display_name))
        # Translators: Submenu item under "View...", opens who the user follows.
        self._addMenuItem(browseMenu, _("Follo&wing..."), lambda: self.showFollowing(did, handle, display_name))
        # Translators: Submenu item under "View...", opens followers you both share.
        self._addMenuItem(browseMenu, _("Kn&own followers..."), lambda: self.showKnownFollowers(did, handle, display_name))
        # Translators: Submenu item under "View...", opens lists the user is on.
        self._addMenuItem(browseMenu, _("&Lists..."), lambda: self.showUserLists(did, handle, display_name))
        # Translators: User action submenu label.
        menu.AppendSubMenu(browseMenu, _("&View..."))
        # Translators: User action menu item.
        self._addMenuItem(menu, _("&Start chat..."), lambda: self.startChat(did, handle))
        menu.AppendSeparator()

        # Real state from cache when available -- no confirm dialog
        # needed anymore since the label already tells the truth
        # (matches the optimistic Like/Repost/Mute-thread pattern).
        # Falls back to the old ambiguous toggle (network check +
        # confirm) if this author was never cached from a post/
        # notification yet.
        cached = db.get_author(did)
        if cached is not None:
            followingUri = cached.get("viewer_following")
            isMuted = bool(cached.get("viewer_muted"))
            blockingUri = cached.get("viewer_blocking")
            # Translators: User action menu item (already following).
            # Translators: User action menu item (not yet following).
            self._addMenuItem(menu, _("Un&follow") if followingUri else _("&Follow"),
                               lambda: self._toggleRelationCached(did, handle, "follow", followingUri))
            # Translators: User action menu item (already muted).
            # Translators: User action menu item (not yet muted).
            self._addMenuItem(menu, _("Un&mute") if isMuted else _("Mu&te"),
                               lambda: self._toggleRelationCached(did, handle, "mute", isMuted))
            # Translators: User action menu item (already blocked).
            # Translators: User action menu item (not yet blocked).
            self._addMenuItem(menu, _("Un&block") if blockingUri else _("&Block"),
                               lambda: self._toggleRelationCached(did, handle, "block", blockingUri))
        else:
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("&Follow / Unfollow"), lambda: self._toggleRelation(did, handle, "follow"))
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("Mu&te / Unmute"), lambda: self._toggleRelation(did, handle, "mute"))
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("&Block / Unblock"), lambda: self._toggleRelation(did, handle, "block"))
        menu.AppendSeparator()

        # Translators: User action menu item.
        self._addMenuItem(menu, _("&Add to list..."), lambda: self.addToList(did, handle, display_name))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        # Translators: Submenu item under "Copy...", copies the user's bsky.app profile URL.
        self._addMenuItem(copyMenu, _("Copy &profile URL"), lambda: self.copyProfileUrl(did, handle))
        # Translators: Submenu item under "Copy...", opens the user's profile in a web browser.
        self._addMenuItem(copyMenu, _("&Open on bsky.app"),
                           lambda: webbrowser.open(f"https://bsky.app/profile/{handle or did}"))
        # Translators: User action submenu label.
        menu.AppendSubMenu(copyMenu, _("&Copy..."))
        menu.AppendSeparator()

        # Translators: User action menu item.
        self._addMenuItem(menu, _("&Report user..."), lambda: self._reportActor(did, handle))

    def showUserActionMenu(self, did, handle, display_name=None):
        menu = wx.Menu()
        self._populateUserActionMenu(menu, did, handle, display_name)
        self.PopupMenu(menu)
        menu.Destroy()

    def showTimeline(self, did, handle, display_name=None):
        # Opens (or focuses an already-open) UserTimelineTabWindow --
        # replaces the old UserTimelineDialog popup. Needs the real
        # MainWindow, same reasoning as _openUserListTab below.
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "user_timeline", "key": did}
        if mainWindow.focusTabByIdentity(identity):
            return

        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        ownerLabel = self._displayLabel(handle, display_name)
        tab = feedTabs.UserTimelineTabWindow(mainWindow.notebook, did, ownerLabel, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.add_open_temp_tab(account["id"], {
                "type": "user_timeline", "key": did, "did": did,
                "owner_label": ownerLabel, "origin_key": originKey,
            })

    def showFollowers(self, did, handle=None, display_name=None):
        self._openUserListTab("followers", did, handle, display_name)

    def showFollowing(self, did, handle=None, display_name=None):
        self._openUserListTab("following", did, handle, display_name)

    def showKnownFollowers(self, did, handle=None, display_name=None):
        # _openUserListTab is already generic on kind -- no changes
        # needed there, just a new kind string flowing through.
        self._openUserListTab("known_followers", did, handle, display_name)

    def _openUserListTab(self, kind, did, handle=None, display_name=None):
        # Opens (or focuses an already-open) UserListTabWindow --
        # replaces the old UserListDialog popup. Needs the real
        # MainWindow, not whatever dialog this mixin happens to be
        # mixed into (e.g. ManageGroupMembersDialog) -- falls back to
        # a message if MainWindow isn't open at all.
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "user_list", "key": f"{kind}:{did}"}
        if mainWindow.focusTabByIdentity(identity):
            return

        # origin_key only set when opened from a PERMANENT tab -- a
        # removable-tab origin just lets wx.Notebook auto-select
        # whatever's next when this tab closes, no special fallback.
        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        ownerLabel = self._displayLabel(handle, display_name)
        tab = feedTabs.UserListTabWindow(mainWindow.notebook, kind, did, ownerLabel, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.add_open_temp_tab(account["id"], {
                "type": "user_list",
                "key": f"{kind}:{did}",
                "list_kind": kind,
                "did": did,
                "owner_label": ownerLabel,
                "origin_key": originKey,
            })

    def _displayLabel(self, handle, display_name=None):
        mode = db.get_ui_state("column1_display") or COLUMN_DISPLAY_NAME
        if mode == COLUMN_DISPLAY_NAME and display_name:
            return display_name
        # Translators: Fallback user label when neither display name nor handle is known.
        return f"@{handle}" if handle else _("unknown user")

    def viewProfile(self, did):
        # Translators: Announced while loading a user's profile.
        _announce_now(_("Loading profile, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onProfileFetched, profile, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onProfileFetched(self, profile, error):
        if error:
            # Translators: Announced when loading a profile fails. {} is the error message.
            nvdaUi.message(_("Could not load profile: {}").format(error))
            return
        from . import feedTabs
        gui.mainFrame.prePopup()
        dlg = feedTabs.ProfileDialog(self, profile)
        dlg.Show()

    def _copyToClipboard(self, text: str):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def copyProfileUrl(self, did, handle):
        url = f"https://bsky.app/profile/{handle or did}"
        self._copyToClipboard(url)
        label = f"@{handle}" if handle else did
        _announce_now(f"Profile URL for {label} copied to clipboard.")

    def startChat(self, did, handle):
        # Opens (or focuses an already-open) ConvoTabWindow for this
        # user -- a genuinely NEW tab, not a switch into the shared
        # Chat tab (that would jump the user's Chat tab to a different
        # conversation than whatever they had selected there before).
        # Same pattern as ChatWindow._openInNewTab.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return
        account = db.get_active_account()
        if account is None:
            # Translators: Announced when trying to start a chat with no active account.
            nvdaUi.message(_("No active account."))
            return
        if not account.get("chat_supported", 1):
            # Translators: Announced when the active account doesn't support DMs.
            nvdaUi.message(_("This account doesn't support direct messages."))
            return
        if did == account.get("did"):
            # BUG FIX: previously this went straight to the network and
            # surfaced the server's raw "Convos may only contain two
            # members" XrpcError to the user verbatim. Caught here
            # instead, before any request is made.
            # Translators: Announced when trying to start a chat with your own account.
            nvdaUi.message(_("You can't start a chat with yourself."))
            return
        label = f"@{handle}" if handle else did
        # Translators: Announced while starting a new chat. {} is the recipient's label.
        _announce_now(_("Starting chat with {}, please wait...").format(label))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                try:
                    availability = client.get_convo_availability(atprotoClient, [did])
                    canChat = getattr(availability, "can_chat", getattr(availability, "canChat", True))
                except Exception:
                    canChat = True
                if not canChat:
                    # Translators: Reported when the recipient doesn't accept messages from this account. {} is their label.
                    wx.CallAfter(self._onStartChatDone, None, _("{} isn't accepting messages from you.").format(label))
                    return
                convo = client.get_or_create_convo_for_member(atprotoClient, did)
                convoId = convo.get("id") if convo else None
                if convoId:
                    client.sync_convo_messages(atprotoClient, account["id"], convoId)
                    if db.get_convo(account["id"], convoId) is None:
                        client.sync_convos(atprotoClient, account["id"], account["did"])
                error = None
            except Exception as e:
                convo = None
                error = str(e)
            wx.CallAfter(self._onStartChatDone, convo, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onStartChatDone(self, convo, error):
        soundpack.stop_progress()
        if error or not convo or not convo.get("id"):
            # Translators: Fallback error when the server doesn't explain why opening a chat failed.
            errorText = error or _("no conversation was returned")
            # Translators: Announced when opening a chat fails. {} is the error message.
            nvdaUi.message(_("Could not open chat: {}").format(errorText))
            return
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        convoId = convo["id"]
        identity = {"kind": "conversation", "key": convoId}
        if mainWindow.focusTabByIdentity(identity):
            return
        account = db.get_active_account()
        if account is None:
            return
        convoRow = db.get_convo(account["id"], convoId)
        if convoRow is None:
            # sync_convos above should have cached it already -- fall
            # back to the shared Chat tab if it somehow isn't there yet.
            mainWindow._openChatConvo(convoId)
            return
        from . import chatWindow
        members = db.get_convo_members(account["id"], convoId)
        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None
        panel = chatWindow.ConvoTabWindow(mainWindow.notebook, convoRow, account, members, origin_key=originKey)
        tabLabel = db.describe_convo_from_members(convoRow, members)
        # Translators: Title of a popped-out conversation tab. {} is the conversation's display name.
        mainWindow.addTab(panel, _("Chat: {}").format(tabLabel), select=True, removable=True)
        db.add_open_temp_tab(account["id"], {
            "type": "conversation",
            "key": convoId,
            "convo_id": convoId,
            "origin_key": originKey,
        })

    def addToList(self, did, handle, display_name=None):
        account = db.get_active_account()
        if account is None:
            # Translators: Announced when trying to add a user to a list with no active account.
            nvdaUi.message(_("No active account."))
            return
        from . import listsWindow
        gui.mainFrame.prePopup()
        dlg = listsWindow.AddToListDialog(self, account, did, handle, display_name)
        dlg.Show()

    def showUserLists(self, did, handle, display_name=None):
        # Reuses the existing "Find lists by user..." dialog but skips
        # the search step -- did/handle are already known here.
        from . import listsWindow
        gui.mainFrame.prePopup()
        dlg = listsWindow.SubscribeListDialog(self, preselected_user={"did": did, "handle": handle, "display_name": display_name})
        dlg.Show()

    def _toggleRelation(self, did, handle, kind):
        """
        Shared confirm-then-act flow for Follow/Mute/Block (kind is
        "follow"/"mute"/"block"). Fetches the real current state first
        (with a "please wait" -- there's no cached follow/mute/block
        status anywhere, so this is unavoidable network latency), THEN
        asks a Yes/No confirmation worded for whichever direction is
        actually correct ("Follow @x?" vs "Unfollow @x?"), instead of
        silently toggling. The menu itself still opens instantly either
        way -- only clicking one of these three items waits.
        """
        # Translators: Announced while checking a user's follow/mute/block status.
        nvdaUi.message(_("Checking status, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onRelationStatusChecked, did, handle, kind, profile, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRelationStatusChecked(self, did, handle, kind, profile, error):
        if error:
            # Translators: Announced when checking a user's status fails. {} is the error message.
            nvdaUi.message(_("Could not check status: {}").format(error))
            return

        viewer = (profile or {}).get("viewer") or {}
        label = f"@{handle}" if handle else did

        if kind == "follow":
            currentValue = viewer.get("following")
            # Translators: Confirmation to unfollow a user. {} is their label.
            # Translators: Confirmation to follow a user. {} is their label.
            question = _("Unfollow {}?").format(label) if currentValue else _("Follow {}?").format(label)
        elif kind == "mute":
            currentValue = viewer.get("muted")
            # Translators: Confirmation to unmute a user. {} is their label.
            # Translators: Confirmation to mute a user. {} is their label.
            question = _("Unmute {}?").format(label) if currentValue else _("Mute {}?").format(label)
        else:
            currentValue = viewer.get("blocking")
            # Translators: Confirmation to unblock a user. {} is their label.
            # Translators: Confirmation to block a user. {} is their label.
            question = _("Unblock {}?").format(label) if currentValue else _("Block {}?").format(label)

        # Translators: Title of a Yes/No confirmation dialog.
        confirm = wx.MessageDialog(self, question, _("Confirm"), wx.YES_NO | wx.NO_DEFAULT)
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        if kind == "follow":
            self._runRelationAction(client.unfollow_actor, currentValue) if currentValue else \
                self._runRelationAction(client.follow_actor, did)
        elif kind == "mute":
            self._runRelationAction(client.unmute_actor, did) if currentValue else \
                self._runRelationAction(client.mute_actor, did)
        else:
            self._runRelationAction(client.unblock_actor, currentValue) if currentValue else \
                self._runRelationAction(client.block_actor, did)

    def _runRelationAction(self, action_fn, arg):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                action_fn(atprotoClient, arg)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after a follow/mute/block action succeeds.
            wx.CallAfter(self._onUserActionDone, _("Done.") if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserActionDone(self, message, error):
        if error:
            log.error(f"NVSky: user action failed: {error}")
            # Translators: Announced when a user action fails. {} is the error message.
            nvdaUi.message(_("Action failed: {}").format(error))
            return
        if message:
            nvdaUi.message(message)

    def _toggleRelationCached(self, did, handle, kind, currentValue):
        if currentValue == "pending":
            # Translators: Announced when an action is repeated while the previous one is still in progress.
            _announce_now(_("Please wait..."))
            return
        label = f"@{handle}" if handle else did
        if kind == "follow":
            db.set_author_following(did, None if currentValue else "pending")
            soundpack.play("unfollow" if currentValue else "follow")
            # Translators: Announced after unfollowing a user. {} is their label.
            # Translators: Announced after following a user. {} is their label.
            nvdaUi.message(_("Unfollowed {}.").format(label) if currentValue else _("Followed {}.").format(label))
        elif kind == "mute":
            db.set_author_muted(did, not currentValue)
            soundpack.play("block_mute")
            # Translators: Announced after unmuting a user. {} is their label.
            # Translators: Announced after muting a user. {} is their label.
            nvdaUi.message(_("Unmuted {}.").format(label) if currentValue else _("Muted {}.").format(label))
        else:
            db.set_author_blocking(did, None if currentValue else "pending")
            soundpack.play("block_mute")
            # Translators: Announced after unblocking a user. {} is their label.
            # Translators: Announced after blocking a user. {} is their label.
            nvdaUi.message(_("Unblocked {}.").format(label) if currentValue else _("Blocked {}.").format(label))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if kind == "follow":
                    resultValue = None if currentValue else client.follow_actor(atprotoClient, did)
                    if currentValue:
                        client.unfollow_actor(atprotoClient, currentValue)
                elif kind == "mute":
                    if currentValue:
                        client.unmute_actor(atprotoClient, did)
                    else:
                        client.mute_actor(atprotoClient, did)
                    resultValue = not currentValue
                else:
                    resultValue = None if currentValue else client.block_actor(atprotoClient, did)
                    if currentValue:
                        client.unblock_actor(atprotoClient, currentValue)
                error = None
            except Exception as e:
                resultValue = currentValue
                error = str(e)
            wx.CallAfter(self._onRelationCachedDone, did, kind, currentValue, resultValue, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRelationCachedDone(self, did, kind, previousValue, resultValue, error):
        finalValue = previousValue if error else resultValue
        if kind == "follow":
            db.set_author_following(did, finalValue)
        elif kind == "mute":
            db.set_author_muted(did, finalValue)
        else:
            db.set_author_blocking(did, finalValue)
        if error:
            # Translators: Announced when a follow/mute/block/subscribe action fails after the optimistic UI update. {} is the error message.
            nvdaUi.message(_("Action failed: {}").format(error))

    def _reportActor(self, did, handle):
        # NOTE: was "for label, _ in REPORT_REASONS" -- bare `_` shadowed
        # gettext within this function's scope. Renamed to avoid that trap.
        labels = [label for label, _reasonCode in REPORT_REASONS]
        # Translators: Prompt in the report-user reason picker.
        # Translators: Title of the report-user reason picker.
        dlg = wx.SingleChoiceDialog(self, _("Reason for reporting this user:"), _("Report user"), labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        reasonType = REPORT_REASONS[dlg.GetSelection()][1]
        dlg.Destroy()

        label = f"@{handle}" if handle else did
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation before sending a user report to Bluesky moderation. {} is the user's label.
            _("Send this report for {} to Bluesky moderation?").format(label),
            # Translators: Title of the confirm-report dialog.
            _("Confirm report"), wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.create_actor_report(atprotoClient, did, reasonType)
                error = None
            except Exception as e:
                error = str(e)
            if not error:
                soundpack.play("block_mute")
            # Translators: Announced after successfully sending a user report.
            wx.CallAfter(self._onUserActionDone, _("Report sent.") if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()


class UserListMixin:
    """
    Shared "browsable list of users" render/focus/action behavior --
    used by UserListTabWindow (followers/following) and, later,
    Explore's People results. No local DB cache/pagination like
    FeedListMixin -- always a single fresh network fetch, matching what
    the old UserListDialog already did.

    A host class must set self._users (list of dicts with did/handle,
    optionally display_name/description), self.userList, self.statusBar
    before render, and implement self._userListLabel() -> str.
    """

    def _buildUserListColumns(self):
        # Translators: Column header for a user's handle in a user list.
        self.userList.InsertColumn(0, _("Handle"), width=180)
        # Translators: Column header for a user's display name in a user list.
        self.userList.InsertColumn(1, _("Display name"), width=180)
        # Translators: Column header for a user's bio in a user list.
        self.userList.InsertColumn(2, _("Bio"), width=300)

    def _insertUserRow(self, index, user):
        self.userList.InsertItem(index, f'@{user["handle"]}')
        self.userList.SetItem(index, 1, user.get("display_name") or "")
        self.userList.SetItem(index, 2, (user.get("description") or "").replace("\n", " "))

    def _renderUsers(self, target_index=None):
        previouslyFocused = self.userList.GetFocusedItem()
        self.userList.Freeze()
        try:
            self.userList.DeleteAllItems()
            for i, user in enumerate(self._users):
                self._insertUserRow(i, user)
            if not self._users:
                index = None
            elif target_index is not None:
                index = max(0, min(target_index, len(self._users) - 1))
            elif previouslyFocused != -1:
                index = min(previouslyFocused, len(self._users) - 1)
            else:
                index = 0
            if index is not None:
                self.userList.Focus(index)
                self.userList.Select(index)
                self.userList.EnsureVisible(index)
        finally:
            self.userList.Thaw()
        if hasattr(self, "statusBar"):
            self.statusBar.SetStatusText(self._userListLabel())

    def _getFocusedUser(self):
        index = self.userList.GetFocusedItem()
        if 0 <= index < len(self._users):
            return self._users[index]
        return None

    def onUserAction(self, evt=None):
        user = self._getFocusedUser()
        if user is None:
            # Translators: Announced when the user-action menu (Alt+U) is invoked with no user focused.
            nvdaUi.message(_("No user selected."))
            return
        self.showUserActionMenu(user["did"], user["handle"], user.get("display_name"))

    def onUserListCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == ord("C") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.userList:
            if uiutil.copy_focused_row(self.userList):
                return
        if keyCode == ord("J") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.userList:
            uiutil.jump_to_row(self, self.userList)
            return
        if evt.AltDown() and keyCode == ord("U"):
            self.onUserAction()
            return
        if keyCode == wx.WXK_F5 and not evt.ShiftDown() and not evt.ControlDown():
            self.onCheckForUpdates(None)
            return
        if evt.ControlDown() and keyCode == wx.WXK_DELETE:
            self.onClearCache()
            return
        evt.Skip()


class EmbedViewMixin:
    """
    Shared "View embed" submenu + open/send/copy handlers for images,
    video, and external links -- mixed into FeedWindow and
    UserTimelineDialog so this stays one implementation, not two
    drifting copies.
    """

    def _buildViewEmbedMenu(self, post):
        embed_json = post.get("embed_json")
        if not embed_json:
            return None
        try:
            embed = json.loads(embed_json)
        except (ValueError, TypeError):
            return None

        menu = wx.Menu()
        added = False

        images = [img for img in (embed.get("images") or []) if img.get("fullsize_url")]
        if len(images) == 1:
            url = images[0]["fullsize_url"]
            # Translators: Context menu item to open an attached image in the default viewer.
            self._addMenuItem(menu, _("&Open image"), lambda: self._openEmbedImage(url))
            # Translators: Context menu item to send an attached image to the Be My Eyes app.
            self._addMenuItem(menu, _("&Send image to Be My Eyes"), lambda: self._sendEmbedImageToBeMyEyes(url))
            # Translators: Context menu item to copy an attached image to the clipboard.
            self._addMenuItem(menu, _("&Copy image to clipboard"), lambda: self._copyEmbedImageToClipboard(url))
            added = True
        elif len(images) > 1:
            for i, img in enumerate(images):
                url = img["fullsize_url"]
                # "&" forces the digit itself as the access key -- the
                # default would use the first letter ("I") for every
                # item, since they'd all start with "Image".
                # Translators: Submenu label for one of several attached images. {} is the image's 1-based index.
                label = _("Image &{}").format(i + 1)
                imageMenu = wx.Menu()
                # Translators: Context menu item to open an attached image in the default viewer.
                self._addMenuItem(imageMenu, _("&Open"), lambda url=url: self._openEmbedImage(url))
                # Translators: Context menu item to send an attached image to the Be My Eyes app.
                self._addMenuItem(imageMenu, _("&Send to Be My Eyes"), lambda url=url: self._sendEmbedImageToBeMyEyes(url))
                # Translators: Context menu item to copy an attached image to the clipboard.
                self._addMenuItem(imageMenu, _("&Copy to clipboard"), lambda url=url: self._copyEmbedImageToClipboard(url))
                menu.AppendSubMenu(imageMenu, label)
                added = True

        videoUrl = embed.get("video_url")
        if videoUrl:
            # Translators: Context menu item to open an attached video in the default player.
            self._addMenuItem(menu, _("Open &video"), lambda: self._openEmbedVideo(videoUrl))
            # Translators: Context menu item to copy an attached video's URL.
            self._addMenuItem(menu, _("Copy video &URL"), lambda: self._copyEmbedUrl(videoUrl, _("Video URL")))
            added = True
        elif embed.get("$type") == "app.bsky.embed.video":
            # Optimistic insert right after posting a video (see
            # compose.py's _insertOptimisticPost) only knows the embed
            # TYPE, not its playlist URL yet -- that's only known once
            # the server's own processed copy is synced back down.
            # Previously this just silently produced no menu at all
            # (added stayed False), which looked like a dead/broken
            # menu item rather than "not ready yet".
            self._addMenuItem(
                menu,
                # Translators: Disabled-looking menu item shown when a just-posted video hasn't finished processing on the server yet.
                _("Video not ready yet (Check for updates first)"),
                # Translators: Announced when trying to view a video embed that hasn't finished processing yet.
                lambda: nvdaUi.message(_("This video was just posted -- press F5 to refresh, then try again.")),
            )
            added = True

        linkUrl = embed.get("link_url")
        if linkUrl:
            title = embed.get("link_title") or linkUrl
            # Translators: Context menu item to open a post's link preview in a browser. {} is the link's title or URL.
            self._addMenuItem(menu, _("Open &link: {}").format(title), lambda: self._openWebLink(linkUrl))
            # Translators: Context menu item to copy a post's link-preview URL.
            self._addMenuItem(menu, _("Copy link &URL"), lambda: self._copyEmbedUrl(linkUrl, _("Link URL")))
            added = True

        if not added:
            menu.Destroy()
            return None
        return menu

    def _openWebLink(self, url):
        # Web links only -- never hand an arbitrary URI scheme to the shell.
        if not str(url).lower().startswith(("http://", "https://")):
            # Translators: Announced when a post's link uses a scheme other than http/https and is refused.
            _announce_now(_("Only http and https links can be opened."))
            return
        webbrowser.open(url)

    def _copyEmbedUrl(self, url, label):
        self._copyToClipboard(url)
        # Translators: Announced after copying an embed's URL to the clipboard. {} is already-translated (e.g. "Video URL").
        _announce_now(_("{} copied to clipboard.").format(label))

    def _openEmbedImage(self, url):
        # Translators: Announced while downloading an image to open it.
        _announce_now(_("Downloading image, please wait..."))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after an image finishes opening.
            wx.CallAfter(self._onActionDone, _("Image opened.") if not error else None, error)

        uiutil.start_worker(worker)

    def _sendEmbedImageToBeMyEyes(self, url):
        # Translators: Announced while downloading an image to send it to Be My Eyes.
        _announce_now(_("Downloading image, please wait..."))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                ok = attachments.send_to_bemyeyes(path)
                # Translators: Error shown when Be My Eyes can't be launched.
                error = None if ok else _("Could not launch Be My Eyes. Is it installed?")
            except Exception as e:
                error = str(e)
            # Translators: Announced after an image is sent to Be My Eyes.
            wx.CallAfter(self._onActionDone, _("Sent to Be My Eyes.") if not error else None, error)

        uiutil.start_worker(worker)

    def _copyEmbedImageToClipboard(self, url):
        def announce_start():
            speech.cancelSpeech()
            # Translators: Announced while downloading an image to copy it to the clipboard.
            nvdaUi.message(_("Downloading image, please wait..."))

        core.callLater(150, announce_start)

        def copy_and_notify(path):
            try:
                ok = attachments.copy_image_to_clipboard(path)
                # Translators: Error shown when the downloaded image file can't be read for clipboard copy.
                error = None if ok else _("Could not read the downloaded image.")
                # Translators: Announced after an image is copied to the clipboard.
                self._onActionDone(_("Image copied to clipboard.") if not error else None, error)
            except Exception as e:
                self._onActionDone(None, str(e))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                wx.CallAfter(copy_and_notify, path)
            except Exception as e:
                wx.CallAfter(self._onActionDone, None, str(e))

        uiutil.start_worker(worker)

    def _openEmbedVideo(self, url):
        # Translators: Announced while downloading a video to open it.
        _announce_now(_("Downloading video, please wait..."))

        def worker():
            try:
                path = attachments.download_video_playlist_to_temp(url)
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after a video finishes opening.
            wx.CallAfter(self._onActionDone, _("Video opened.") if not error else None, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onLikeSynced(self, post, message, error):
        self._onActionDone(message, error)
        if error:
            return
        soundpack.play("like" if post.get("viewer_like_uri") else "unlike")
        account = db.get_active_account()
        if account is not None:
            sync_like_state(post, account["id"])

    @uiutil.safe_ui_callback
    def _onActionDone(self, message, error):
        def announce_immediately(text):
            speech.cancelSpeech()  # cuts off the ListCtrl's own focus announcement
            nvdaUi.message(text)   # speak ours instead

        if error:
            log.error(f"NVSky: action failed: {error}")
            # Translators: Announced when an embed action (open/copy/send) fails. {} is the error message.
            core.callLater(200, announce_immediately, _("Action failed: {}").format(error))
            return

        if message:
            core.callLater(200, announce_immediately, message)


class FeedListMixin:
    """
    Shared "paginated post list" behavior -- lazy-load, status bar,
    loading beep, check-for-updates, focus save/restore, unread
    tracking -- so FeedWindow and future post-based tabs (Explore,
    Feeds, Lists, Saved) don't each reimplement the same fetch/cache/
    render cycle. Notifications is NOT expected to fit this mixin as-is
    since its data isn't shaped like a post -- it would supply its own
    _dbGetPage/_dbGetUnreadCount/_syncPage against a different table.

    A host class must, before calling self._initFeedListState():
      - set self.TAB_NAME, self._feedKey, self._account, self.postList,
        self.statusBar
    And must implement:
      - self._insertRow(index, item, mode) -- render one row
      - self._dbGetPage(before_indexed_at=None, limit=None) -- cache read
      - self._dbGetUnreadCount() -- cache read
      - self._syncPage(atprotoClient, cursor, limit) -> new cursor -- network sync + cache write
    And must bind postList's EVT_LIST_ITEM_FOCUSED to self.onItemFocused.
    """

    def _initFeedListState(self):
        self._posts = []
        self._olderCursor = None
        self._loadingMore = False
        self._loadingTimer = None
        self._checkingUpdates = False
        self._suppressFocusEvents = False
        # Only meaningful for hosts with SUPPORTS_JUMP_TO_USER = True,
        # but harmless to always set -- keeps _jumpToUserPost/
        # onItemFocused from needing a getattr(..., False) fallback.
        self._jumpTargetDid = None
        self._jumpTargetHandle = None
        self._jumpingToUser = False
        self._startTimeRefreshTimer()

    def _startTimeRefreshTimer(self):
        # Relative time labels ("5 minutes ago") go stale the longer a
        # tab is left open without a full re-render -- this recomputes
        # just the Posted/Received column text for whatever's already
        # in memory, no DB read or network call, so it's cheap enough
        # to run on every FeedListMixin tab for its whole lifetime.
        # Bound to `self` (the panel), not self.postList -- some hosts
        # (FeedWindow) call _initFeedListState() before postList exists
        # yet, and `self` is guaranteed to exist at that point.
        self._timeRefreshTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._onTimeRefreshTick, self._timeRefreshTimer)
        self._timeRefreshTimer.Start(60000)

    # Column index of the "Posted"/"Received" time column -- 3 for the
    # standard Embed/Author/Message/Posted layout (FeedWindow, Saved,
    # Lists, ListTab), overridden by hosts with a different column
    # layout (NotificationsWindow). Confirmed by testing (see
    # plan-09.md): this was hardcoded to 3 unconditionally before,
    # which crashed NVDA outright (unhandled wxAssertionError, not
    # caught by safe_ui_callback since it's not a "has been deleted"
    # RuntimeError) whenever this timer ticked while Notifications
    # (only 3 columns, time column at index 2) was the visible tab
    # under a relative-time Display setting.
    TIME_COLUMN_INDEX = 3

    def _onTimeRefreshTick(self, evt):
        # Absolute/custom modes print the same string every time --
        # skip the (harmless but pointless) work there. wx.Notebook
        # hides non-active pages, so a background tab's IsShownOnScreen()
        # is False and this also skips tabs nobody's currently looking at.
        mode, _pattern = timeutils.current_mode_and_pattern(db)
        if mode not in ("relative_24h", "relative_always"):
            return
        if not self.postList.IsShownOnScreen():
            return
        for i, post in enumerate(self._posts):
            self.postList.SetItem(i, self.TIME_COLUMN_INDEX, _format_post_time(post.get("indexed_at")))

    def _stopTimeRefreshTimer(self):
        timer = getattr(self, "_timeRefreshTimer", None)
        if timer is not None:
            timer.Stop()

    def _updateTitle(self):
        # Translators: Fallback account label in the window title when no account is active.
        accountLabel = self._account["handle"] if self._account else _("no account")

        # Panel-based tabs (FeedWindow, NotificationsWindow, and any
        # future Panel-based tab) keep the notebook tab label short
        # (just the tab's own name, e.g. "Home"/"Notifications") and
        # instead push the fuller "<tab name> - NVSky - <handle>" text
        # onto the shared MainWindow title bar -- but only when this
        # panel is the currently active tab, so switching tabs updates
        # the title to match. isinstance(self, wx.Panel) still guards
        # this in case a future host stays a wx.Dialog (single-item info
        # dialogs like ProfileDialog, per plan-04.md's "stays a Dialog"
        # rule) and needs the old direct window-title behavior instead.
        if isinstance(self, wx.Panel):
            notebook = self.GetParent()
            index = notebook.FindPage(self)
            if index != wx.NOT_FOUND:
                notebook.SetPageText(index, self.TAB_NAME)
                if index == notebook.GetSelection():
                    topWindow = self.GetTopLevelParent()
                    topWindow.SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")
        else:
            self.SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")

    def onAccountChanged(self):
        # Called by GlobalPlugin._rebuildTabs (see __init__.py) when
        # the active account changes while MainWindow is already open --
        # self._account was otherwise only ever set once, at __init__,
        # confirmed via testing to be the reason a freshly-added account
        # (after removing the only existing one) never showed up in an
        # already-open MainWindow.
        self._account = db.get_active_account()
        self._updateTitle()
        self._loadFromCache(reset=True)

    def _updateStatusBar(self):
        # _tracksUnread=False (set by e.g. SavedWindow) skips the unread
        # count entirely -- a saved-posts list is a personal reference
        # list the user deliberately built, not a stream to catch up on,
        # so "X unread" doesn't mean anything useful there.
        if getattr(self, "_tracksUnread", True):
            unread = self._dbGetUnreadCount() if self._account else 0
            # Translators: Feed-tab status bar text. First {} is tab name, second {} is unread count, third {} is total count.
            self.statusBar.SetStatusText(_("{} {} unread {} total").format(self.TAB_NAME, unread, len(self._posts)))
        else:
            # Translators: Feed-tab status bar text (no unread tracking). First {} is tab name, second {} is total count.
            self.statusBar.SetStatusText(_("{} {} total").format(self.TAB_NAME, len(self._posts)))

    def _currentAuthorMode(self) -> str:
        return db.get_ui_state("column1_display") or COLUMN_DISPLAY_NAME

    def _authorLabel(self, post: dict, mode: str) -> str:
        # For reposts, show WHO reposted it -- that's who you follow and
        # why it's in your feed -- not the original author (that's named
        # in the Message column instead, via _message_text). Still
        # respects the display-name-vs-handle setting like any other post.
        if post.get("is_repost") and post.get("reposted_by_handle"):
            if mode == COLUMN_DISPLAY_NAME:
                return post.get("reposted_by_display_name") or post["reposted_by_handle"]
            return post["reposted_by_handle"]
        if mode == COLUMN_DISPLAY_NAME:
            return post.get("display_name") or post.get("handle") or post.get("author_did")
        return post.get("handle") or post.get("author_did")

    def _getFocusedPost(self):
        index = self.postList.GetFocusedItem()
        if 0 <= index < len(self._posts):
            return self._posts[index]
        return None

    def _applySortOrder(self, posts):
        # Always sorts fresh rather than reversing an already-ordered
        # list -- correct no matter what order `posts` arrived in (a
        # fresh DB page, an appended lazy-load batch, or the existing
        # self._posts after the setting changed while the window was
        # open). Sorts by feed_indexed_at (position IN THIS FEED) rather
        # than the post's own indexed_at -- for a repost those two can
        # differ a lot, and sorting by the post's own time put reposts
        # of old posts in the wrong place entirely.
        newestFirst = db.get_ui_state("sort_order") != "oldest_first"
        return sorted(posts, key=lambda p: p.get("feed_indexed_at") or p["indexed_at"], reverse=newestFirst)

    def _loadFromCache(self, reset: bool):
        if self._account is None:
            self.postList.DeleteAllItems()
            return
        if reset:
            posts = self._applySortOrder(self._dbGetPage())
            self._posts = [p for p in posts if _label_visibility(p) != "hide"]
        self._render()

    def _buildFeedListColumns(self):
        # Translators: Column header for a post's embed summary (image/video/link/quote).
        self.postList.InsertColumn(0, _("Embed"), width=140)
        # Translators: Column header for a post's author.
        self.postList.InsertColumn(1, _("Author"), width=180)
        # Translators: Column header for a post's text.
        self.postList.InsertColumn(2, _("Message"), width=330)
        # Translators: Column header for when a post was posted/received.
        self.postList.InsertColumn(3, _("Posted"), width=140)

    def _buildStandardFeedSizer(self, extra_top=None, extra_action_widgets=None):
        """
        Standard feed-tab layout: optional extra_top row above postList,
        postList itself, Post action/User action buttons (+ optional
        extra_action_widgets after them), statusBar. Calls self.SetSizer().
        Was byte-for-byte duplicated across SavedWindow/ListTabWindow/
        FeedPreviewTabWindow/FeedWindow before this (see plan-13.md).
        extra_top: a wx.Sizer to place above postList (e.g. FeedWindow's
        filter-choice row).
        extra_action_widgets: list of wx.Window added to the action row
        after userActionButton (e.g. "Add to my feeds").
        """
        sizer = wx.BoxSizer(wx.VERTICAL)

        if extra_top is not None:
            sizer.Add(extra_top, flag=wx.ALL, border=10)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildFeedListColumns()
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. Shows the Alt+A shortcut.
        self.postActionButton = wx.Button(self, label=_("&Post action... (Alt+A)"))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        if extra_action_widgets:
            for widget in extra_action_widgets:
                actionRow.Add(widget, flag=wx.LEFT, border=5)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

    def _bindStandardFeedEvents(self):
        """Binds Post action/User action buttons and postList's focus/
        activate/char-hook to the standard handler names every host
        already implements identically. Call after any host-specific
        binds (e.g. FeedWindow's filterChoice) so those aren't affected."""
        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onItemActivated)
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def _finishStandardFeedInit(self, sync_if_empty=False):
        """Standard __init__ tail: title, cache load, no-account message,
        focus-position restore, optional initial sync-if-empty. Call last,
        after self._feedKey (and anything _syncPage/_dbGetPage need) is
        already set."""
        self._updateTitle()
        self._loadFromCache(reset=True)

        if self._account is None:
            # Translators: Announced when opening a feed tab with no active account.
            nvdaUi.message(_("No active account. Log in from Settings first."))
            return

        self._restoreFocusPosition(moveFocus=False)
        if sync_if_empty:
            self._syncIfCacheEmpty()
        if getattr(self, "TAB_KEY", None) == "home":
            db.set_home_active_filter(self._account["id"], self._feedKey)

    def _insertRow(self, index: int, post: dict, mode: str):
        # Shared by every FeedListMixin host except NotificationsWindow
        # (which overrides this -- its columns are Author/Notification/
        # Received, no Embed at all). Used to be copy-pasted identically
        # into FeedWindow/SavedWindow/ListsWindow/ListTabWindow -- same
        # class of duplication chatWindow.py's ChatWindow/ConvoTabWindow
        # had, fixed the same way.
        self.postList.InsertItem(index, _visible_embed_text(post))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _visible_message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _render(self):
        # Sorted here (not just at fetch time) so switching the setting
        # while this window is already open takes effect via onActivate's
        # _render() call too, without needing a fresh fetch.
        self._posts = self._applySortOrder(self._posts)
        mode = self._currentAuthorMode()

        focusedUri = None
        focusedIndex = self.postList.GetFocusedItem()
        if 0 <= focusedIndex < len(self._posts):
            focusedUri = self._posts[focusedIndex]["uri"]

        self.postList.Freeze()
        self._suppressFocusEvents = True
        try:
            self.postList.DeleteAllItems()
            for i, post in enumerate(self._posts):
                self._insertRow(i, post, mode)

            restored = False
            if focusedUri:
                for i, post in enumerate(self._posts):
                    if post["uri"] == focusedUri:
                        self.postList.Focus(i)
                        self.postList.Select(i)
                        restored = True
                        break

            # The previously-focused post can vanish from the list (it
            # got deleted, hidden, or fell out of the cache window during
            # a re-sync) -- fall back to the top of the list rather than
            # leaving nothing focused. Index 0 already means "top of the
            # currently-sorted list" per _applySortOrder above, so this
            # fallback tracks the sort order setting the same way the
            # initial "no saved position" case in _restoreFocusPosition
            # does.
            if not restored and self._posts:
                self.postList.Focus(0)
                self.postList.Select(0)
        finally:
            self._suppressFocusEvents = False
            self.postList.Thaw()

        self._updateStatusBar()
        self._updateActionButtons()

    def _updateActionButtons(self):
        # Post action/User action only make sense when there's something
        # focused to act on -- hide them entirely while the list is
        # empty instead of leaving a button visible that just replies
        # "No post selected." every time. Same Show()/Layout() pattern
        # MainWindow already uses to hide Remove current tab on
        # permanent tabs.
        #
        # Also Disable(), not just Hide() -- CONFIRMED bug elsewhere
        # (ChatWindow's Accept button): a Hide()-only wx.Button still
        # fires its own mnemonic (Alt+A/Alt+U here) even while
        # invisible, since wx dispatches mnemonics to any control that
        # is merely non-shown but still enabled. Alt+A/Alt+U are
        # global shortcuts used everywhere in this app, so a stray
        # hidden-but-enabled instance is a real risk of a silent
        # duplicate/wrong-target trigger on any empty list.
        hasItems = bool(self._posts)
        for buttonName in ("postActionButton", "userActionButton"):
            button = getattr(self, buttonName, None)
            if button is not None:
                button.Show(hasItems)
                button.Enable(hasItems)
        self.Layout()

    def _announceNthNewestPost(self, n: int):
        # self._posts is sorted by _applySortOrder/_render to match
        # Settings > Display > sort order, so which end is "newest"
        # flips depending on that setting -- must check it here instead
        # of assuming self._posts is always newest-first. Doesn't move
        # focus, just speaks it, so the user can stay wherever they
        # were (e.g. typing in Chat's compose box). Reads every column
        # straight off the already-rendered row instead of
        # reconstructing text from the post dict -- that reconstruction
        # assumed a uniform "Author/Message" shape that doesn't hold
        # for every host (Notifications' columns are "Author/
        # Notification/Received", no Message/Embed at all). Reading the
        # row itself is correct automatically everywhere.
        if not (1 <= n <= len(self._posts)):
            # Translators: Announced when Alt+number is pressed for a post index that doesn't exist. {} is the number.
            nvdaUi.message(_("No item {}.").format(n))
            return
        newestFirst = db.get_ui_state("sort_order") != "oldest_first"
        index = (n - 1) if newestFirst else (len(self._posts) - n)
        # Alt+number doesn't move focus, so onItemFocused (the normal
        # mark-on-scroll path, below) never fires for it -- without
        # this, reading an item aloud this way never marked it read.
        # Fixed once here in the shared mixin so it covers every host
        # automatically (Home, Saved, Lists, Notifications, and any
        # future FeedListMixin tab) instead of needing the same fix
        # repeated per tab and occasionally forgotten -- the exact
        # class of bug already hit twice over in Chat.
        if uiutil.move_focus_and_check_announce(self.postList, index):
            columnCount = self.postList.GetColumnCount()
            parts = [self.postList.GetItemText(index, col) for col in range(columnCount)]
            nvdaUi.message(", ".join(p for p in parts if p))
        post = self._posts[index]
        if not post.get("is_read"):
            self._markItemRead(post)
            post["is_read"] = 1
            self._updateStatusBar()

    def _restoreFocusPosition(self, moveFocus=True):
        """
        Restores which row was last focused (Focus/Select/EnsureVisible
        on the ListCtrl -- always safe, doesn't touch real OS/screen-
        reader focus) and, only when moveFocus=True, also grabs real
        keyboard focus onto postList. moveFocus MUST be False when
        called from a tab panel's own __init__ -- at that point the
        panel may not even be added to MainWindow's notebook yet (or
        may be a tab that isn't the one meant to be visible), and
        grabbing real focus unconditionally there is what caused two
        tabs' worth of content to get announced back-to-back on open.
        Real focus for the initially-selected tab is granted once,
        later, by MainWindow.addTab()'s wx.CallAfter; for tab switches,
        by onTabActivated() below (moveFocus defaults to True there).

        SetFocus() BEFORE Focus()/Select() when moveFocus=True -- same
        fix as chatWindow.py's ConvoTabWindow._loadMessages. Confirmed
        by testing: calling Select() while the ListCtrl doesn't have
        real OS focus yet doesn't fully register the selection with
        the native control (GetSelectedItemCount() stayed 0 until the
        user pressed an arrow key, breaking Alt+U/Alt+A right after a
        tab first opens/activates). Granting real focus first, then
        selecting, avoids that race.
        """
        savedUri = db.get_ui_state(self._focusStateKey())

        targetIndex = 0
        if savedUri:
            for i, post in enumerate(self._posts):
                if post["uri"] == savedUri:
                    targetIndex = i
                    break

        if moveFocus:
            self.postList.SetFocus()
            # SetFocus() doesn't take effect synchronously -- the
            # native control doesn't fully "have" real OS focus until
            # the event loop processes it. Selecting a non-zero index
            # immediately after, in the same call, raced that and left
            # GetSelectedItemCount() reporting 0 until an arrow key
            # forced a real focus-changed event. Index 0 happened to
            # look unaffected only because it's already the ListCtrl's
            # natural default focused row from insertion, not because
            # this race didn't apply to it. wx.CallAfter defers the
            # actual selection until after focus has genuinely landed.
            wx.CallAfter(self._applyFocusPosition, targetIndex)
        else:
            self._applyFocusPosition(targetIndex)

    def _applyFocusPosition(self, targetIndex):
        self._suppressFocusEvents = True
        try:
            if self._posts:
                # Select() only ADDS to the selection, it never clears
                # anything else already selected -- confirmed root
                # cause of Alt+U/Alt+A wrongly reporting multiple posts
                # selected right after a tab opens/activates: _render()
                # falls back to Select(0) when it can't find a saved
                # focus target yet, then this method selects the real
                # target on top of that without ever clearing index 0
                # first. Same fix already used by
                # uiutil.move_focus_and_check_announce and
                # _jumpToUserPost for the identical class of bug.
                for i in range(self.postList.GetItemCount()):
                    if i != targetIndex and self.postList.GetItemState(i, wx.LIST_STATE_SELECTED):
                        self.postList.SetItemState(i, 0, wx.LIST_STATE_SELECTED)
                self.postList.Focus(targetIndex)
                self.postList.Select(targetIndex)
                self.postList.EnsureVisible(targetIndex)
        finally:
            self._suppressFocusEvents = False

    def _focusStateKey(self) -> str:
        return f"lastFocus:{self._feedKey}:{self._account['id']}"

    def onItemFocused(self, evt):
        if self._suppressFocusEvents:
            evt.Skip()
            return

        if not getattr(self, "_jumpingToUser", False):
            self._jumpTargetDid = None
            self._jumpTargetHandle = None

        index = evt.GetIndex()

        if self._account and 0 <= index < len(self._posts):
            post = self._posts[index]
            db.set_ui_state(self._focusStateKey(), post["uri"])

            if _label_visibility(post) == "warn":
                soundpack.play_debounced("content_warning")
            else:
                embedEvent = _embed_sound_event(post.get("embed_json"))
                if embedEvent:
                    soundpack.play_debounced(embedEvent)

            if not post.get("is_read"):
                self._markItemRead(post)
                post["is_read"] = 1
                self._updateStatusBar()

        evt.Skip()

    def onFetchPreviousPosts(self, evt):
        if self._account is None:
            # Translators: Announced when trying to fetch older posts with no active account.
            nvdaUi.message(_("No active account."))
            return
        if self._loadingMore:
            # Translators: Announced when Shift+F5 is pressed while an older-posts fetch is already in progress. {} is the tab name.
            nvdaUi.message(_("Already fetching older {} posts, please wait...").format(self.TAB_NAME))
            return
        if not self._posts:
            # Translators: Announced when trying to fetch older posts in an empty feed. {} is the tab name.
            nvdaUi.message(_("Nothing loaded yet in {}.").format(self.TAB_NAME))
            return
        self._loadMore()

    def _focusNextUnread(self):
        # Same root cause and fix as chatWindow.py's
        # _focusNextUnreadMessage: moving focus alone doesn't reliably
        # mark the row read, since EVT_LIST_ITEM_FOCUSED only fires when
        # the focused index actually changes -- landing on an already-
        # focused unread row (e.g. right after opening the tab) was a
        # no-op state change, so Space appeared to do nothing. Shared
        # here in FeedListMixin fixes it for every host at once (Home,
        # Saved, Lists, Notifications, ListTabWindow).
        #
        # Must also always progress OLDEST-unread-first chronologically
        # regardless of Settings > Display sort order -- self._posts'
        # array order follows the on-screen display order (see
        # _applySortOrder), so a plain forward scan picked the NEWEST
        # unread item first whenever newest-first was set (jumping to
        # the top, then working backward), backwards from the intended
        # "catch up from where you left off" behavior. Same fix as
        # chatWindow.py's _focusNextUnreadMessage.
        newestFirst = db.get_ui_state("sort_order") != "oldest_first"
        indices = range(len(self._posts) - 1, -1, -1) if newestFirst else range(len(self._posts))
        for i in indices:
            if not self._posts[i].get("is_read"):
                post = self._posts[i]
                if uiutil.move_focus_and_check_announce(self.postList, i):
                    columnCount = self.postList.GetColumnCount()
                    parts = [self.postList.GetItemText(i, col) for col in range(columnCount)]
                    nvdaUi.message(", ".join(p for p in parts if p))
                self._markItemRead(post)
                post["is_read"] = 1
                self._updateStatusBar()
                return
        soundpack.play("boundary")
        # Translators: Announced when there's no unread post to jump to.
        nvdaUi.message(_("No unread posts."))

    def _selectAllPosts(self):
        self._suppressFocusEvents = True
        try:
            for i in range(len(self._posts)):
                self.postList.SetItemState(i, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
        finally:
            self._suppressFocusEvents = False
        # Translators: Announced after Ctrl+A selects every post. {} is the count.
        nvdaUi.message(_("{} posts selected.").format(len(self._posts)))

    def _getSelectedPosts(self):
        # Was FeedWindow-only since the very first version of this file
        # -- moved here so every FeedListMixin host (Notifications,
        # Saved, ...) can use bulk selection, not just Home.
        indices = []
        i = self.postList.GetFirstSelected()
        while i != -1:
            indices.append(i)
            i = self.postList.GetNextSelected(i)
        return [self._posts[i] for i in indices if 0 <= i < len(self._posts)]

    def _loadMore(self):
        if self._account is None or not self._posts:
            return

        self._loadingMore = True
        oldestIndexedAt = min(p.get("feed_indexed_at") or p["indexed_at"] for p in self._posts)

        # Translators: Announced while fetching older posts. {} is the tab name.
        nvdaUi.message(_("Loading older {} posts, please wait...").format(self.TAB_NAME))
        self._startLoadingBeep()

        def worker():
            cursor = self._olderCursor
            foundOlder = False
            error = None
            try:
                atprotoClient = client.get_client_for_active_account()
                for _walk in range(MAX_NETWORK_PAGE_WALK):
                    cursor = self._syncPage(atprotoClient, cursor, PAGE_SIZE)
                    foundOlder = bool(self._dbGetPage(before_indexed_at=oldestIndexedAt))
                    if foundOlder or not cursor:
                        break
            except Exception as e:
                log.error(f"NVSky: lazy load failed: {e}")
                error = str(e)

            wx.CallAfter(self._onLoadMoreDone, foundOlder, error, cursor)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onLoadMoreDone(self, foundOlder, error, cursor):
        self._loadingMore = False
        self._stopLoadingBeep()
        if error:
            # Translators: Announced when fetching older posts fails. First {} is the tab name, second {} is the error message.
            nvdaUi.message(_("Could not load more {} posts: {}").format(self.TAB_NAME, error))
            return
        self._olderCursor = cursor
        if foundOlder:
            # Full reload from the DB rather than appending just the
            # delta -- guarantees the displayed total always matches
            # exactly what's cached (the same source the unread count
            # reads from), with no separate limit/append bookkeeping
            # left to drift out of sync.
            oldCount = len(self._posts)
            self._loadFromCache(reset=True)
            self._restoreFocusPosition()
            addedCount = len(self._posts) - oldCount
            if addedCount <= 0:
                # Translators: Announced when no genuinely new older posts were added. {} is the tab name.
                nvdaUi.message(_("{} load complete.").format(self.TAB_NAME))
            elif addedCount == 1:
                # Translators: Announced after loading exactly one older post. {} is the tab name.
                nvdaUi.message(_("1 older post loaded in {}.").format(self.TAB_NAME))
            else:
                # Translators: Announced after loading several older posts. First {} is the count, second {} is the tab name.
                nvdaUi.message(_("{} older posts loaded in {}.").format(addedCount, self.TAB_NAME))
        elif cursor:
            # Walked MAX_NETWORK_PAGE_WALK pages without turning up a new
            # cached post (e.g. a stretch of muted/hidden posts got
            # filtered out) -- the timeline itself isn't actually
            # exhausted since the cursor is still valid, so press
            # Shift+F5 again to keep looking further back.
            # Translators: Announced when no new older posts were found in the pages walked so far. {} is the tab name.
            nvdaUi.message(_("No older {} posts found nearby -- press Shift+F5 again to keep looking back.").format(self.TAB_NAME))
        else:
            soundpack.play("boundary")
            # Translators: Announced when there are no more older posts to load at all. {} is the tab name.
            nvdaUi.message(_("No more {} posts to load.").format(self.TAB_NAME))

    def _startLoadingBeep(self):
        # Routed through soundpack's own looped progress indicator now
        # (see soundpack.py's SoundEngine.start_progress/stop_progress)
        # instead of a local wx.Timer -- that module already handles
        # the beep-fallback-when-no-progress-sound-file case, so this
        # method (kept under its old name -- every FeedListMixin call
        # site still calls it unchanged) just delegates to it.
        soundpack.start_progress()

    def _stopLoadingBeep(self):
        soundpack.stop_progress()

    def _syncForBulkCheck(self, atprotoClient):
        # Pure network+DB work, safe to call from Ctrl+F5's single
        # shared background thread (mainWindow.checkAllOpenTabs) --
        # must NOT touch self.postList or anything else wx here.
        before = self._dbGetPage(limit=1)
        oldTopUri = before[0]["uri"] if before else None
        self._syncPage(atprotoClient, None, PAGE_SIZE)
        after = self._dbGetPage(limit=1)
        newTopUri = after[0]["uri"] if after else None
        return newTopUri != oldTopUri

    def _syncIfCacheEmpty(self):
        """Call after _loadFromCache() wherever a feed_key can be
        genuinely brand new with zero cached posts (a fresh
        FeedPreviewTabWindow/ListTabWindow, or a Home filter switched
        to a feed that's never been viewed before) -- there's nothing
        at all to show otherwise, so this does ONE silent background
        sync instead of leaving the list empty until the user manually
        presses refresh. Deliberately NOT called from every tab's
        __init__/onFilterChanged unconditionally -- Home's "Following"/
        "Discover", Notifications, Saved, and Explore's own search are
        expected to already have history and stay cache-first/manual-
        refresh like the rest of the app; auto-syncing them too would
        fight that design (see FeedWindow.onFilterChanged's own note
        about not re-fetching on every filter switch)."""
        if self._account is None or self._posts:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                self._syncPage(atprotoClient, None, PAGE_SIZE)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onInitialSyncDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onInitialSyncDone(self, error):
        if error:
            log.error(f"NVSky: initial sync for a new/empty feed failed: {error}")
            return
        self._loadFromCache(reset=True)

    def _reloadAfterBulkCheck(self, moveFocus=True):
        # Main-thread only -- called back by checkAllOpenTabs for every
        # tab that had new data, not just the visible one; moveFocus is
        # False for any tab that isn't actually on screen right now, so
        # a background refresh can't steal real keyboard focus.
        self._loadFromCache(reset=True)
        self._restoreFocusPosition(moveFocus=moveFocus)

    def onCheckForUpdates(self, evt):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        if self._checkingUpdates:
            return

        self._checkingUpdates = True
        # Translators: Announced while checking a feed for updates. {} is the tab name.
        nvdaUi.message(_("Checking {} feed for updates, please wait...").format(self.TAB_NAME))
        self._startLoadingBeep()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                self._syncPage(atprotoClient, None, PAGE_SIZE)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onCheckForUpdatesDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCheckForUpdatesDone(self, error):
        self._checkingUpdates = False
        self._stopLoadingBeep()
        if error:
            log.error(f"NVSky: check for updates failed: {error}")
            # Translators: Announced when checking a feed for updates fails. {} is the error message.
            nvdaUi.message(_("Check for updates failed: {}").format(error))
            return

        oldTopUri = self._posts[0]["uri"] if self._posts else None
        self._loadFromCache(reset=True)
        self._restoreFocusPosition()

        if oldTopUri is None:
            newCount = len(self._posts)
        else:
            newCount = 0
            for post in self._posts:
                if post["uri"] == oldTopUri:
                    break
                newCount += 1

        if newCount == 0:
            # Translators: Announced when checking a feed for updates finds nothing new. {} is the tab name.
            nvdaUi.message(_("No new posts in {} feed.").format(self.TAB_NAME))
        elif newCount == 1:
            # Translators: Announced when checking a feed for updates finds exactly one new post. {} is the tab name.
            nvdaUi.message(_("1 new post in {} feed.").format(self.TAB_NAME))
        else:
            # Translators: Announced when checking a feed for updates finds several new posts. First {} is the count, second {} is the tab name.
            nvdaUi.message(_("{} new posts in {} feed.").format(newCount, self.TAB_NAME))

    def onClearCache(self, evt=None):
        # Clears this tab's cached posts only -- no auto-refresh,
        # leaves reload timing to F5/bgsync.
        if self._account is None:
            # Translators: Announced when clearing cache with no active account.
            nvdaUi.message(_("No active account."))
            return
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to clear a feed's cached posts. {} is the tab name.
            _("Clear cached posts for {}? This can't be undone.").format(self.TAB_NAME),
            # Translators: Title of the clear-cache confirmation dialog.
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        self._doClearCache()

    def _doClearCache(self):
        db.clear_feed_key_cache(self._account["id"], self._feedKey)
        self._loadFromCache(reset=True)

        if self._posts:
            # Still has items -- normal focus works fine.
            self.postList.SetFocus()
            return

        # Empty list: a ListCtrl that just lost its last row via
        # DeleteAllItems() while it already had real OS focus doesn't
        # raise a fresh focus event (same HWND, no real transition),
        # so NVDA gets stuck reporting "unknown" on the dead old row
        # object. Rather than fighting that with fake focus bounces,
        # send focus somewhere genuinely useful instead: the shared
        # "Check for updates" toolbar button (present on every tab,
        # never hidden) -- doubles as a natural nudge toward refreshing
        # the now-empty list.
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()

    # ---------------- window-level keyboard shortcuts (shared) ----------------
    # Consolidated from 5 near-identical copies (Home/Saved/Lists/ListTab/
    # Notifications) -- see plan-09.md. Per-class differences are gated by
    # class attributes (default False, set True where the original class
    # had that behavior) so this preserves every class's exact prior
    # behavior rather than guessing which differences were intentional.
    # ListsWindow's Alt+number/Alt+U logic is genuinely class-specific
    # (branches on list purpose), so it overrides _onAltNumber/_onAltU
    # below instead of using a flag.

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()

        if keyCode in (wx.WXK_UP, wx.WXK_DOWN) and not evt.HasAnyModifiers() and self.FindFocus() is self.postList:
            self._checkListBoundaryBeforeKey(keyCode)
        if evt.ControlDown() and keyCode == wx.WXK_DELETE:
            self.onClearCache()
            return
        if keyCode == wx.WXK_DELETE and not evt.HasAnyModifiers() and self.FindFocus() is self.postList:
            self.onDeletePostShortcut()
            return
        if keyCode == wx.WXK_F5 and evt.ControlDown():
            evt.Skip()  # bubble up to MainWindow's checkAllOpenTabs
            return
        if keyCode == wx.WXK_F5 and evt.ShiftDown():
            self.onFetchPreviousPosts(None)
            return
        if keyCode == wx.WXK_F5:
            self.onCheckForUpdates(None)
            return
        if evt.ControlDown() and keyCode == ord("N") and getattr(self, "SUPPORTS_NEW_POST", False):
            self.onNewPost(None)
            return
        if evt.AltDown() and keyCode == ord("A"):
            self.onPostAction()
            return
        if evt.AltDown() and ord("1") <= keyCode <= ord("9"):
            self._onAltNumber(keyCode - ord("0"))
            return
        if evt.AltDown() and keyCode == ord("U"):
            self._onAltU()
            return
        if keyCode == ord("C") and evt.ControlDown() and not evt.ShiftDown() and not evt.AltDown():
            focused = self.FindFocus()
            if focused is self.postList:
                self._copyFocusedPostRow()
                return
            if isinstance(focused, (wx.ListCtrl, wx.TreeCtrl)) and uiutil.copy_focused_row(focused):
                return
        if keyCode == ord("J") and evt.ControlDown() and not evt.ShiftDown() and not evt.AltDown():
            focused = self.FindFocus()
            if isinstance(focused, wx.ListCtrl):
                uiutil.jump_to_row(self, focused)
                return
        if keyCode == wx.WXK_SPACE and evt.ControlDown() and self.FindFocus() is self.postList:
            self._revealContentWarning()
            return
        if keyCode == wx.WXK_SPACE and not evt.ControlDown() and getattr(self, "SUPPORTS_FOCUS_NEXT_UNREAD", False) and self.FindFocus() is self.postList:
            self._focusNextUnread()
            return
        if evt.ControlDown() and keyCode == ord("A") and getattr(self, "SUPPORTS_SELECT_ALL", False) and self.FindFocus() is self.postList:
            self._selectAllPosts()
            return
        if keyCode == wx.WXK_LEFT and not evt.HasAnyModifiers() and getattr(self, "SUPPORTS_JUMP_TO_USER", False) and self.FindFocus() is self.postList:
            self._jumpToUserPost(-1)
            return
        if keyCode == wx.WXK_RIGHT and not evt.HasAnyModifiers() and getattr(self, "SUPPORTS_JUMP_TO_USER", False) and self.FindFocus() is self.postList:
            self._jumpToUserPost(1)
            return

        evt.Skip()

    def _copyFocusedPostRow(self):
        post = self._getFocusedPost()
        if post is None:
            return
        parts = [
            self._authorLabel(post, self._currentAuthorMode()),
            _message_text(post),
            _format_post_time(post.get("indexed_at")),
        ]
        url = _post_web_url(post.get("uri"), post.get("handle") or post.get("author_did"))
        uiutil.copy_text_to_clipboard(_compose_copy_text(parts, url))

    def _revealContentWarning(self):
        # Rewrites the FOCUSED ROW's own displayed embed AND message
        # columns in place (SetItem only, never touches self._posts or
        # any cached state) so the real content actually replaces both
        # placeholders on screen, not just spoken once. Deliberately
        # transient -- any future _render() call (arrow off and back,
        # F5, tab switch) recomputes from _visible_embed_text/
        # _visible_message_text again and goes right back to showing
        # the warning; nothing here is remembered.
        index = self.postList.GetFocusedItem()
        post = self._getFocusedPost()
        if post is None:
            return
        if _label_visibility(post) != "warn":
            # Translators: Announced when Ctrl+Space is pressed on a post with no content warning to reveal.
            nvdaUi.message(_("This post has no content warning."))
            return
        realEmbedText = _describe_embed(post.get("embed_json"))
        realMessageText = _message_text(post)
        matchedNames = _matched_label_names(post)
        revealedEmbedText = f"{CONTENT_WARNING_EMBED_TEXT}, {realEmbedText}" if realEmbedText else CONTENT_WARNING_EMBED_TEXT
        revealedMessageText = f"{CONTENT_WARNING_MESSAGE_TEXT.format(matchedNames)} {realMessageText}".strip()
        self.postList.SetItem(index, 0, revealedEmbedText)
        self.postList.SetItem(index, 2, revealedMessageText)
        nvdaUi.message(", ".join(p for p in [revealedEmbedText, revealedMessageText] if p))

    def _checkListBoundaryBeforeKey(self, keyCode):
        # Deterministic instead of comparing focus before/after via
        # wx.CallAfter -- that approach fired on EVERY arrow press, not
        # just at the real top/bottom, because EVT_CHAR_HOOK's own
        # CallAfter callback ran before Windows had actually dispatched
        # the key down to the native ListCtrl's own handler, so "after"
        # always read back identical to "before" regardless of whether
        # a real boundary was hit. self._posts is already the single
        # source of truth for row order (matches _render's own sort),
        # so the boundary can be computed directly from the CURRENT
        # focused index without waiting on native behavior at all: Up
        # from row 0, or Down from the last row, has nowhere left to go.
        if not self._posts:
            return
        index = self.postList.GetFocusedItem()
        if index == -1:
            return
        if keyCode == wx.WXK_UP and index == 0:
            soundpack.play("boundary")
        elif keyCode == wx.WXK_DOWN and index == len(self._posts) - 1:
            soundpack.play("boundary")

    def _onAltNumber(self, n):
        self._announceNthNewestPost(n)

    def _onAltU(self):
        self.onUserAction()

    # ---------------- Left/Right jump-to-user (SUPPORTS_JUMP_TO_USER) ----------------
    # Moved here from FeedWindow (see plan-09.md) so ListsWindow can
    # opt in via the SUPPORTS_JUMP_TO_USER flag too -- logic itself was
    # already fully generic (self._posts/self.postList only), nothing
    # FeedWindow-specific.

    def _postInvolvesUser(self, post, did):
        if (
            post.get("author_did") == did
            or post.get("reposted_by_did") == did
            or post.get("reply_to_did") == did
        ):
            return True
        facets_json = post.get("facets_json")
        if facets_json:
            try:
                facets = json.loads(facets_json)
            except (ValueError, TypeError):
                facets = []
            for facet in facets:
                for feature in facet.get("features", []):
                    if feature.get("$type") == "app.bsky.richtext.facet#mention" and feature.get("did") == did:
                        return True
        return False

    def _jumpToUserPost(self, direction: int):
        """
        Left/Right jump to the next/previous post (going up/down the
        list) that's either BY the reference user or MENTIONS them --
        like OpenTween's jump-to-this-user's-tweets. The reference user
        locks to whoever the focused post's author was on the FIRST
        Left/Right press, and stays locked across repeated presses so
        jumping onto a mention-post (authored by someone else) doesn't
        silently switch who you're tracking -- it only resets once you
        navigate some other way (arrow keys, click, etc).
        """
        current = self._getFocusedPost()
        if current is None:
            return

        if self._jumpTargetDid is None:
            targetDid = current["author_did"]
            targetHandle = current.get("handle") or targetDid
        else:
            targetDid = self._jumpTargetDid
            targetHandle = self._jumpTargetHandle

        n = len(self._posts)
        i = self.postList.GetFocusedItem()
        for _step in range(n):
            i += direction
            if i < 0 or i >= n:
                break
            if self._postInvolvesUser(self._posts[i], targetDid):
                self._jumpTargetDid = targetDid
                self._jumpTargetHandle = targetHandle
                self._jumpingToUser = True
                try:
                    # Left/Right is a single-item jump, not an additive
                    # multi-select action like Ctrl+A -- Select() only
                    # ADDS a row to the selection, it doesn't clear the
                    # old one the way arrow-key navigation does natively.
                    # Without this, repeated jumps left multiple rows
                    # selected at once, so Post action (Alt+A) wrongly
                    # treated it as a bulk selection.
                    for j in range(self.postList.GetItemCount()):
                        if j != i and self.postList.GetItemState(j, wx.LIST_STATE_SELECTED):
                            self.postList.SetItemState(j, 0, wx.LIST_STATE_SELECTED)
                    self.postList.Focus(i)
                    self.postList.Select(i)
                    self.postList.EnsureVisible(i)
                finally:
                    self._jumpingToUser = False
                return

        soundpack.play("boundary")
        # Translators: Announced when Left/Right jump-to-same-user runs out of matching posts. {} is the handle.
        nvdaUi.message(_("No more posts involving @{} in that direction.").format(targetHandle))

    # ---------------- new post (SUPPORTS_NEW_POST) ----------------

    def onNewPost(self, evt=None):
        dlg = ComposeDialog(self, onClosed=None)
        dlg.Show()


class ItemActionMixin:
    """
    Shared "Post action" menu (Alt+A) and its handlers. Written
    originally as FeedWindow's own onPostAction where every item in
    the list already WAS a post; now generalized so any host class can
    reuse it as long as it supplies _getActionablePost() -- the
    resolved post dict to act on (or None, having already announced
    why via nvdaUi.message()).
    """

    def onPostAction(self, evt=None):
        selectedCount = self.postList.GetSelectedItemCount()
        if selectedCount > 1:
            self._showBulkPostActionMenu(selectedCount)
            return

        post = self._getActionablePost()
        if post is None:
            return

        self._showPostActionMenu(post)

    def onDeletePostShortcut(self):
        # Plain Delete key -- confirm dialog inside _deletePost still
        # gates the actual removal, this just skips opening the menu.
        if self.postList.GetSelectedItemCount() > 1:
            # Translators: Announced when Delete is pressed with multiple posts selected.
            nvdaUi.message(_("Delete needs a single post selected."))
            return
        post = self._getActionablePost()
        if post is None:
            return
        isOwnPost = self._account and post.get("author_did") == self._account.get("did")
        if isOwnPost:
            self._deletePost(post)
            return
        # Not our own post -- but if it's OUR repost of someone else's
        # post, Delete undoes the repost instead (same action as the
        # "Undo repost" menu item, no confirm needed -- reversible,
        # matches Like/Unlike's existing no-confirm behavior).
        isOwnRepost = (
            self._account and post.get("is_repost") and post.get("reposted_by_did") == self._account.get("did")
        )
        if isOwnRepost:
            self._toggleRepost(post)
            return
        # Translators: Announced when Delete is pressed on a post that isn't the account's own.
        nvdaUi.message(_("You can only delete your own posts."))

    def _showPostActionMenu(self, post):
        isOwnPost = self._account and post.get("author_did") == self._account.get("did")

        menu = wx.Menu()

        if isOwnPost:
            manageMenu = wx.Menu()
            # Translators: Submenu item under "Manage post...".
            self._addMenuItem(manageMenu, _("&Pin/unpin to profile..."), lambda: self._togglePinToProfile(post))
            # Translators: Submenu item under "Manage post...".
            self._addMenuItem(manageMenu, _("&Edit who can reply..."), lambda: self._editReplyPermissions(post))
            # Translators: Submenu item under "Manage post...".
            self._addMenuItem(manageMenu, _("&Delete post..."), lambda: self._deletePost(post))
            # Translators: Post action submenu label, only shown on your own posts.
            menu.AppendSubMenu(manageMenu, _("&Manage post..."))
            menu.AppendSeparator()

        # Translators: Post action menu item.
        self._addMenuItem(menu, _("&Reply..."), lambda: self._openReply(post))
        # Translators: Post action menu item (already reposted).
        # Translators: Post action menu item (not yet reposted).
        self._addMenuItem(menu, _("Undo re&post") if post.get("viewer_repost_uri") else _("Re&post"),
                           lambda: self._toggleRepost(post))
        # Translators: Post action menu item.
        self._addMenuItem(menu, _("&Quote post..."), lambda: self._openQuote(post))
        menu.AppendSeparator()

        markMenu = wx.Menu()
        # Translators: Submenu item under "Mark as...".
        self._addMenuItem(markMenu, _("&Read"), lambda: self._setMarkRead(post, True))
        # Translators: Submenu item under "Mark as...".
        self._addMenuItem(markMenu, _("&Unread"), lambda: self._setMarkRead(post, False))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(markMenu, _("Mar&k as..."))

        isLiked = bool(post.get("viewer_like_uri"))
        # Translators: Post action menu item (already liked).
        # Translators: Post action menu item (not yet liked).
        self._addMenuItem(menu, _("Un&like") if isLiked else _("&Like"), lambda: self._togglePostLike(post))
        # Translators: Post action menu item (already saved).
        # Translators: Post action menu item (not yet saved).
        self._addMenuItem(menu, _("Un&save") if post.get("viewer_bookmarked") else _("&Save"),
                           lambda: self._toggleBookmark(post))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        # Translators: Submenu item under "Copy...", copies the post's text.
        self._addMenuItem(copyMenu, _("Copy &post text"), lambda: self._copyPostText(post))
        # Translators: Submenu item under "Copy...", copies a link to the post.
        self._addMenuItem(copyMenu, _("Copy &link to post"), lambda: self._copyPostLink(post))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(copyMenu, _("&Copy..."))

        viewMenu = wx.Menu()
        # Translators: Submenu item under "View...", opens the full thread.
        self._addMenuItem(viewMenu, _("View &thread..."), lambda: self._openThread(post))
        # Translators: Submenu item under "View...", opens who liked the post.
        self._addMenuItem(viewMenu, _("View &likes..."), lambda: self._openUserListForPost("likes", post))
        # Translators: Submenu item under "View...", opens who reposted the post.
        self._addMenuItem(viewMenu, _("View &reposts..."), lambda: self._openUserListForPost("reposts", post))
        # Translators: Submenu item under "View...", opens posts quoting this one.
        self._addMenuItem(viewMenu, _("View &quotes..."), lambda: self._openQuotes(post))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(viewMenu, _("&View..."))

        embedMenu = self._buildViewEmbedMenu(post)
        if embedMenu is not None:
            # Translators: Post action submenu label for viewing an embedded image/video/link.
            menu.AppendSubMenu(embedMenu, _("&Embed..."))
        menu.AppendSeparator()

        moreMenu = wx.Menu()
        # Translators: Submenu item under "More...".
        self._addMenuItem(moreMenu, _("&Share to chat..."), lambda: self._shareToChat(post))
        isThreadMuted = bool(post.get("viewer_thread_muted"))
        # Translators: Submenu item under "More..." (thread already muted).
        # Translators: Submenu item under "More..." (thread not yet muted).
        self._addMenuItem(moreMenu, _("Un&mute thread") if isThreadMuted else _("&Mute thread"), lambda: self._muteThread(post))
        # Translators: Submenu item under "More...".
        self._addMenuItem(moreMenu, _("&Hide post for me"), lambda: self._hidePost(post))
        # Translators: Submenu item under "More...".
        self._addMenuItem(moreMenu, _("&Report post..."), lambda: self._reportPost(post))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(moreMenu, _("M&ore..."))

        self.PopupMenu(menu)
        menu.Destroy()

    def _showBulkPostActionMenu(self, selectedCount):
        menu = wx.Menu()
        markMenu = wx.Menu()
        # Translators: Bulk mark-read menu item. {} is the selected count.
        self._addMenuItem(markMenu, _("Read ({} selected)").format(selectedCount), lambda: self._markSelectedRead(True))
        # Translators: Bulk mark-unread menu item. {} is the selected count.
        self._addMenuItem(markMenu, _("Unread ({} selected)").format(selectedCount), lambda: self._markSelectedRead(False))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(markMenu, _("Mar&k as..."))
        self.PopupMenu(menu)
        menu.Destroy()

    def _setMarkRead(self, post, read: bool):
        if read:
            db.mark_post_read(post["uri"])
            post["is_read"] = 1
            # Translators: Announced after marking a post read.
            message = _("Marked as read.")
        else:
            db.mark_post_unread(post["uri"])
            post["is_read"] = 0
            # Translators: Announced after marking a post unread.
            message = _("Marked as unread.")
        self._updateStatusBar()
        _announce_now(message)

    def _togglePostLike(self, post):
        wasLiked = bool(post.get("viewer_like_uri"))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_like_uri"):
                    client.unlike_post(atprotoClient, post["viewer_like_uri"])
                    db.set_post_like_uri(post["uri"], None)
                    post["viewer_like_uri"] = None
                    # Translators: Announced after unliking a post.
                    message = _("Unliked.")
                else:
                    like_uri = client.like_post(atprotoClient, post["uri"], post["cid"])
                    db.set_post_like_uri(post["uri"], like_uri)
                    post["viewer_like_uri"] = like_uri
                    # Translators: Announced after liking a post.
                    message = _("Liked.")
                error = None
            except Exception as e:
                error = str(e)
                message = None
            if not error:
                soundpack.play("unlike" if wasLiked else "like")
            wx.CallAfter(self._onLikeToggleDone, post, message, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _reloadOtherLikesTabs(self):
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        for panel in mainWindow.getOpenTabs():
            if panel is not self and getattr(panel, "TAB_KEY", None) == "likes":
                panel._loadFromCache(reset=True)

    @uiutil.safe_ui_callback
    def _onLikeToggleDone(self, post, message, error):
        self._onActionDone(message, error)
        if error:
            return
        propagate_post_state(post)
        if post.get("viewer_like_uri"):
            likedAt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            db.upsert_feed_item(self._account["id"], "likes", post["uri"], likedAt)
            self._reloadOtherLikesTabs()
            return
        db.delete_feed_item(self._account["id"], "likes", post["uri"])
        self._refreshOtherSavedTabs(post, "likes")
        onLikeChanged = getattr(self, "_onLikeChanged", None)
        if callable(onLikeChanged):
            onLikeChanged(post)

    def _toggleBookmark(self, post):
        wasBookmarked = bool(post.get("viewer_bookmarked"))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_bookmarked"):
                    client.unbookmark_post(atprotoClient, post["uri"])
                    db.set_post_bookmarked(post["uri"], False)
                    post["viewer_bookmarked"] = False
                    # Translators: Announced after removing a post from Saved.
                    message = _("Removed from saved.")
                else:
                    client.bookmark_post(atprotoClient, post["uri"], post["cid"])
                    db.set_post_bookmarked(post["uri"], True)
                    post["viewer_bookmarked"] = True
                    # Translators: Announced after saving a post.
                    message = _("Saved post success.")
                error = None
            except Exception as e:
                error = str(e)
                message = None
            if not error:
                soundpack.play("unsave" if wasBookmarked else "save")
            wx.CallAfter(self._onBookmarkToggleDone, post, message, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onBookmarkToggleDone(self, post, message, error):
        self._onActionDone(message, error)
        if error:
            return
        propagate_post_state(post)
        if not post.get("viewer_bookmarked"):
            # BUG FIX: unsaving always drops the "saved" feed cache
            # entry now, regardless of which tab the toggle happened
            # from -- previously this only happened via SavedWindow's
            # own _onBookmarkChanged below, so unsaving from Home/
            # Notifications/etc left a stale row in the "saved" cache
            # forever (confirmed: Saved tab kept showing it even after
            # F5, since the DB row itself was never removed).
            db.delete_feed_item(self._account["id"], "saved", post["uri"])
            self._refreshOtherSavedTabs(post)
        # Default no-op -- only a tab that actually LISTS posts by their
        # saved status (SavedWindow) needs to react when one gets
        # unsaved. Home/Notifications don't list by bookmark status, so
        # toggling Save there should never remove the row.
        onBookmarkChanged = getattr(self, "_onBookmarkChanged", None)
        if callable(onBookmarkChanged):
            onBookmarkChanged(post)

    def _refreshOtherSavedTabs(self, post, tabKey="saved"):
        # Live-updates an already-open Saved tab if the unsave happened
        # from somewhere else (e.g. Home) -- without this, Saved would
        # only reflect the removal on its next full reload/F5.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        for panel in mainWindow.getOpenTabs():
            if panel is self or getattr(panel, "TAB_KEY", None) != tabKey:
                continue
            for i, p in enumerate(getattr(panel, "_posts", [])):
                if p["uri"] == post["uri"]:
                    del panel._posts[i]
                    panel._render()
                    break

    def _copyPostText(self, post):
        self._copyToClipboard(post.get("text", ""))
        # Translators: Announced after copying a post's text to the clipboard.
        _announce_now(_("Post text copied to clipboard."))

    def _copyPostLink(self, post):
        handle = post.get("handle") or post.get("author_did")
        rkey = post["uri"].rsplit("/", 1)[-1]
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        self._copyToClipboard(url)
        # Translators: Announced after copying a post's URL to the clipboard.
        _announce_now(_("Post URL copied to clipboard."))

    def _shareToChat(self, post):
        if self._account is None:
            # Translators: Announced when trying to share a post to chat with no active account.
            nvdaUi.message(_("No active account."))
            return
        from . import chatWindow
        gui.mainFrame.prePopup()
        dlg = chatWindow.ShareToChatDialog(self, self._account, post)
        dlg.Show()

    def _muteThread(self, post):
        root_uri = post.get("reply_parent_uri") or post["uri"]
        wasMuted = bool(post.get("viewer_thread_muted"))
        post["viewer_thread_muted"] = not wasMuted
        db.set_post_thread_muted(post["uri"], not wasMuted)
        propagate_post_state(post)
        # Translators: Announced after unmuting a thread.
        # Translators: Announced after muting a thread.
        self._onActionDone(_("Thread unmuted.") if wasMuted else _("Thread muted."), None)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if wasMuted:
                    client.unmute_thread(atprotoClient, root_uri)
                else:
                    client.mute_thread(atprotoClient, root_uri)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onMuteThreadDone, post, wasMuted, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onMuteThreadDone(self, post, wasMuted, error):
        if not error:
            return
        post["viewer_thread_muted"] = wasMuted
        db.set_post_thread_muted(post["uri"], wasMuted)
        self._onActionDone(None, error)

    def onItemActivated(self, evt):
        post = self._getFocusedPost()
        if post is None:
            return
        action = db.get_ui_state("enter_action") or "view_thread"

        if action == "mark_read":
            if self.postList.GetSelectedItemCount() > 1:
                self._markSelectedRead(not post.get("is_read"))
            else:
                self._setMarkRead(post, not post.get("is_read"))
            return

        if action == "reply":
            self._openReply(post)
        elif action == "quote":
            self._openQuote(post)
        elif action == "repost":
            self._toggleRepost(post)
        elif action == "like":
            self._togglePostLike(post)
        elif action == "mark_read":
            self._toggleMarkRead(post)
        else:
            self._openThread(post)

    def _hidePost(self, post):
        db.hide_post(post["uri"])
        for i, p in enumerate(self._posts):
            if p["uri"] == post["uri"]:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break
        # Translators: Announced after hiding a post for the current account only.
        _announce_now(_("Post hidden."))

    def _deletePost(self, post):
        confirm = wx.MessageDialog(
            # Translators: Confirmation body for deleting your own post.
            # Translators: Title of the delete-post confirmation dialog.
            self, _("Delete this post? This can't be undone."), _("Delete post"),
            wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.delete_post(atprotoClient, post["uri"])
                db.delete_post(post["uri"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onDeletePostDone, post["uri"], error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onDeletePostDone(self, uri, error):
        if error:
            log.error(f"NVSky: delete post failed: {error}")
            soundpack.play("error")
            # Translators: Announced when deleting a post fails. {} is the error message.
            nvdaUi.message(_("Delete failed: {}").format(error))
            return
        soundpack.play("delete")
        for i, p in enumerate(self._posts):
            if p["uri"] == uri:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break
        # Translators: Announced after deleting a post.
        nvdaUi.message(_("Post deleted."))

    def _togglePinToProfile(self, post):
        # No local cache of pinned-post state (unlike thread-mute/
        # follow, Bluesky doesn't send this per-post) -- check the
        # real server state first, same pattern _toggleRelation used
        # before its cached upgrade.
        # Translators: Announced while checking whether a post is pinned.
        nvdaUi.message(_("Checking pin status, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                pinnedUri = client.get_pinned_post_uri(atprotoClient)
                error = None
            except Exception as e:
                pinnedUri = None
                error = str(e)
            wx.CallAfter(self._onPinStatusChecked, post, pinnedUri, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onPinStatusChecked(self, post, pinnedUri, error):
        if error:
            # Translators: Announced when checking pin status fails. {} is the error message.
            nvdaUi.message(_("Could not check pin status: {}").format(error))
            return
        isPinned = pinnedUri == post["uri"]
        # Translators: Confirmation to unpin a post from your profile.
        # Translators: Confirmation to pin a post to your profile.
        question = _("Unpin this post from your profile?") if isPinned else _("Pin this post to your profile?")
        # Translators: Title of a Yes/No confirmation dialog.
        confirm = wx.MessageDialog(self, question, _("Confirm"), wx.YES_NO | wx.NO_DEFAULT)
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        # Translators: Announced after unpinning a post.
        # Translators: Announced after pinning a post.
        self._onActionDone(_("Post unpinned.") if isPinned else _("Post pinned."), None)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if isPinned:
                    client.unpin_post_from_profile(atprotoClient)
                else:
                    client.pin_post_to_profile(atprotoClient, post["uri"], post["cid"])
                workerError = None
            except Exception as e:
                workerError = str(e)
            wx.CallAfter(self._onPinActionDone, workerError)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onPinActionDone(self, error):
        if error:
            self._onActionDone(None, error)

    def _openQuotes(self, post):
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "quotes", "key": post["uri"]}
        if mainWindow.focusTabByIdentity(identity):
            return

        # Translators: Announced while loading a post's quotes.
        _announce_now(_("Loading quotes, please wait..."))
        soundpack.start_progress()
        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                quotes = client.get_post_quotes(atprotoClient, post["uri"])
                error = None
            except Exception as e:
                quotes = []
                error = str(e)
            wx.CallAfter(self._onQuotesFetchedForOpen, post["uri"], quotes, error, originKey)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onQuotesFetchedForOpen(self, targetUri, quotes, error, originKey):
        soundpack.stop_progress()
        if error:
            soundpack.play("error")
            # Translators: Announced when loading a post's quotes fails. {} is the error message.
            nvdaUi.message(_("Could not load quotes: {}").format(error))
            return
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        tab = feedTabs.QuotesTabWindow(mainWindow.notebook, targetUri, quotes or [], origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.set_user_list_cache(account["id"], f"quotes:{targetUri}", quotes or [])
            db.add_open_temp_tab(account["id"], {
                "type": "quotes", "key": targetUri, "target_uri": targetUri, "origin_key": originKey,
            })

    def _openUserListForPost(self, kind, post):
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "user_list", "key": f"{kind}:{post['uri']}"}
        if mainWindow.focusTabByIdentity(identity):
            return

        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        authorHandle = post.get("handle") or post.get("author_did")
        preview = (post.get("text") or "")[:40]
        if len(post.get("text") or "") > 40:
            preview += "..."
        if authorHandle and preview:
            # Translators: Owner label combining a post preview and its author, used in tab names like "Likes on {this}". First {} is the preview, second {} is the handle.
            ownerLabel = _('"{}" by @{}').format(preview, authorHandle)
        elif authorHandle:
            ownerLabel = f"@{authorHandle}"
        else:
            ownerLabel = None
        tab = feedTabs.UserListTabWindow(mainWindow.notebook, kind, post["uri"], ownerLabel, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.add_open_temp_tab(account["id"], {
                "type": "user_list", "key": f"{kind}:{post['uri']}",
                "list_kind": kind, "post_uri": post["uri"], "owner_label": ownerLabel, "origin_key": originKey,
            })

    def _editReplyPermissions(self, post):
        # Translators: Announced while loading a post's current reply-permission settings.
        _announce_now(_("Loading current reply settings, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                threadgate = client.get_threadgate_settings(atprotoClient, post["uri"])
                disablesQuotes = client.get_postgate_disables_quotes(atprotoClient, post["uri"])
                error = None
            except Exception as e:
                threadgate = {"state": "everyone", "rules": set(), "has_list_rules": False}
                disablesQuotes = False
                error = str(e)
            wx.CallAfter(self._onReplyPermissionsLoaded, post, threadgate, disablesQuotes, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onReplyPermissionsLoaded(self, post, threadgate, disablesQuotes, error):
        if error:
            # Translators: Announced when loading a post's reply settings fails. {} is the error message.
            nvdaUi.message(_("Could not load current reply settings: {}").format(error))
            # Fall through anyway -- still let them set it, just without
            # the current values pre-selected.

        state = threadgate.get("state", "everyone")
        rules = threadgate.get("rules", set())
        hasListRules = threadgate.get("has_list_rules", False)

        # Translators: Title of the edit-who-can-reply dialog.
        dlg = wx.Dialog(self, title=_("Edit who can reply"), style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        stateChoices = [
            # Translators: Reply-permission radio choice.
            _("Allow anyone to reply"),
            # Translators: Reply-permission radio choice.
            _("Disable replies entirely"),
            # Translators: Reply-permission radio choice.
            _("Custom"),
        ]
        stateIndex = {"everyone": 0, "nobody": 1, "custom": 2}.get(state, 0)
        # Translators: Label for the who-can-reply radio group.
        stateBox = wx.RadioBox(dlg, label=_("Who can reply"), choices=stateChoices, style=wx.RA_SPECIFY_ROWS)
        stateBox.SetSelection(stateIndex)
        sizer.Add(stateBox, flag=wx.ALL | wx.EXPAND, border=8)

        # Translators: Checkbox in the edit-who-can-reply dialog.
        followersCheck = wx.CheckBox(dlg, label=_("Allow your &followers to reply"))
        # Translators: Checkbox in the edit-who-can-reply dialog.
        followingCheck = wx.CheckBox(dlg, label=_("Allow people you follo&w to reply"))
        # Translators: Checkbox in the edit-who-can-reply dialog.
        mentionedCheck = wx.CheckBox(dlg, label=_("Allow people you &mention to reply"))
        followersCheck.SetValue("followers" in rules)
        followingCheck.SetValue("following" in rules)
        mentionedCheck.SetValue("mentioned" in rules)
        for chk in (followersCheck, followingCheck, mentionedCheck):
            sizer.Add(chk, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        def onStateChanged(evt):
            isCustom = stateBox.GetSelection() == 2
            for chk in (followersCheck, followingCheck, mentionedCheck):
                chk.Enable(isCustom)
        stateBox.Bind(wx.EVT_RADIOBOX, onStateChanged)
        onStateChanged(None)

        if hasListRules:
            listWarning = wx.StaticText(
                dlg,
                # Translators: Warning shown when a post has list-based reply rules NVSky can't edit yet.
                label=_(
                    "Note: this post also has list-based reply rules set "
                    "(e.g. from the Bluesky app). NVSky can't edit those "
                    "yet -- saving here will remove them."
                ),
            )
            listWarning.Wrap(350)
            sizer.Add(listWarning, flag=wx.ALL | wx.EXPAND, border=8)

        # Translators: Checkbox in the edit-who-can-reply dialog.
        quoteCheck = wx.CheckBox(dlg, label=_("Disable &quote posts of this post"))
        quoteCheck.SetValue(disablesQuotes)
        sizer.Add(quoteCheck, flag=wx.ALL, border=8)

        buttonSizer = dlg.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttonSizer, flag=wx.ALL | wx.ALIGN_CENTER, border=8)

        dlg.SetSizerAndFit(sizer)
        stateBox.SetFocus()
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        newState = ["everyone", "nobody", "custom"][stateBox.GetSelection()]
        newRules = set()
        if followersCheck.GetValue():
            newRules.add("followers")
        if followingCheck.GetValue():
            newRules.add("following")
        if mentionedCheck.GetValue():
            newRules.add("mentioned")
        newDisablesQuotes = quoteCheck.GetValue()
        dlg.Destroy()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.set_threadgate(atprotoClient, post["uri"], newState, newRules)
                if newDisablesQuotes != disablesQuotes:
                    client.set_postgate_disable_quotes(atprotoClient, post["uri"], newDisablesQuotes)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after successfully updating reply permissions.
            wx.CallAfter(self._onActionDone, _("Reply permissions updated.") if not error else None, error)

        uiutil.start_worker(worker)

    def _openComposeWithContext(self, reply_to=None, quote_of=None):
        gui.mainFrame.prePopup()
        dlg = ComposeDialog(self, onClosed=None, reply_to=reply_to, quote_of=quote_of)
        dlg.Bind(wx.EVT_CLOSE, self._onComposeClosed)
        dlg.Show()

    def _onComposeClosed(self, evt):
        gui.mainFrame.postPopup()
        evt.Skip()

    def _openReply(self, post):
        self._openComposeWithContext(reply_to={
            "uri": post["uri"],
            "cid": post["cid"],
            "handle": post.get("handle") or post.get("author_did"),
            "text": post.get("text", ""),
            "is_reply": bool(post.get("reply_parent_uri")),
        })

    def _openQuote(self, post):
        self._openComposeWithContext(quote_of={
            "uri": post["uri"],
            "cid": post["cid"],
            "handle": post.get("handle") or post.get("author_did"),
            "text": post.get("text", ""),
        })

    def _openThread(self, post):
        # Opens (or focuses an already-open) ThreadTabWindow -- replaces
        # the old ThreadDialog popup. The dedup identity is the THREAD
        # ROOT's uri, not post["uri"] itself -- opening from any reply
        # within the same thread should land on the same tab. The root
        # uri isn't known yet without fetching the thread first, so the
        # dedup check happens after fetch, in _onThreadFetchedForOpen.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        # Translators: Announced while loading a whole thread.
        _announce_now(_("Loading thread, please wait..."))
        soundpack.start_progress()

        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                posts, targetIndex = client.get_thread(atprotoClient, post["uri"])
                error = None
            except Exception as e:
                posts, targetIndex = None, 0
                error = str(e)
            wx.CallAfter(self._onThreadFetchedForOpen, posts, targetIndex, error, originKey)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onThreadFetchedForOpen(self, posts, targetIndex, error, originKey):
        soundpack.stop_progress()
        if error:
            soundpack.play("error")
            # Translators: Announced when loading a thread fails. {} is the error message.
            nvdaUi.message(_("Could not load thread: {}").format(error))
            return
        posts = posts or []
        rootUri = posts[0]["uri"] if posts else None
        if rootUri is None:
            soundpack.play("error")
            # Translators: Announced when a thread has no discoverable root post.
            nvdaUi.message(_("Could not load thread: no root post found."))
            return

        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            return

        identity = {"kind": "thread", "key": rootUri}
        if mainWindow.focusTabByIdentity(identity):
            return

        tab = feedTabs.ThreadTabWindow(mainWindow.notebook, rootUri, posts, targetIndex, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.set_user_list_cache(account["id"], f"thread:{rootUri}", posts)
            db.add_open_temp_tab(account["id"], {
                "type": "thread", "key": rootUri, "root_uri": rootUri, "origin_key": originKey,
            })

    def _toggleRepost(self, post):
        if post.get("viewer_repost_uri") == "pending":
            # Translators: Announced when an action is repeated while the previous one is still in progress.
            _announce_now(_("Please wait..."))
            return
        wasReposted = bool(post.get("viewer_repost_uri"))
        previousUri = post.get("viewer_repost_uri")
        post["viewer_repost_uri"] = None if wasReposted else "pending"
        db.set_post_repost_uri(post["uri"], post["viewer_repost_uri"])
        soundpack.play("unrepost" if wasReposted else "repost")
        # Translators: Announced after undoing a repost.
        # Translators: Announced after reposting.
        self._onActionDone(_("Repost undone.") if wasReposted else _("Reposted."), None)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if wasReposted:
                    client.unrepost_post(atprotoClient, previousUri)
                    newUri = None
                else:
                    newUri = client.repost_post(atprotoClient, post["uri"], post["cid"])
                error = None
            except Exception as e:
                newUri = None
                error = str(e)
            wx.CallAfter(self._onRepostDone, post, wasReposted, previousUri, newUri, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRepostDone(self, post, wasReposted, previousUri, newUri, error):
        if error:
            post["viewer_repost_uri"] = previousUri
            db.set_post_repost_uri(post["uri"], previousUri)
            self._onActionDone(None, error)
            return
        if not wasReposted:
            post["viewer_repost_uri"] = newUri
            db.set_post_repost_uri(post["uri"], newUri)
        propagate_post_state(post)
        onRepostChanged = getattr(self, "_onRepostChanged", None)
        if callable(onRepostChanged):
            onRepostChanged(post)

    def _reportPost(self, post):
        # NOTE: was "for label, _ in REPORT_REASONS" -- bare `_` shadowed
        # gettext within this function's scope. Renamed to avoid that trap.
        labels = [label for label, _reasonCode in REPORT_REASONS]
        # Translators: Prompt in the report-post reason picker.
        # Translators: Title of the report-post reason picker.
        dlg = wx.SingleChoiceDialog(self, _("Reason for reporting this post:"), _("Report post"), labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        reasonType = REPORT_REASONS[dlg.GetSelection()][1]
        dlg.Destroy()

        confirm = wx.MessageDialog(
            # Translators: Confirmation before sending a post report to Bluesky moderation.
            # Translators: Title of the confirm-report dialog.
            self, _("Send this report to Bluesky moderation?"), _("Confirm report"), wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.create_report(atprotoClient, post["uri"], post["cid"], reasonType)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after successfully sending a post report.
            wx.CallAfter(self._onActionDone, _("Report sent.") if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    def _markSelectedRead(self, read: bool):
        # Lives here (not on FeedWindow) because it's called from
        # _showBulkPostActionMenu and onItemActivated, both defined in
        # this same mixin -- every host class (FeedWindow, Notifications,
        # Saved, Lists, ListTabWindow, ExploreWindow, FeedPreviewTabWindow)
        # needs it, not just FeedWindow. This has been fixed at least
        # once before this session and apparently reverted/never
        # actually applied -- if this crashes again with the same
        # AttributeError, check whether something is re-adding a
        # duplicate _markSelectedRead onto a specific host class further
        # down the file, since Python uses whichever def executes last.
        posts = self._getSelectedPosts()
        for post in posts:
            if read:
                db.mark_post_read(post["uri"])
                post["is_read"] = 1
            else:
                db.mark_post_unread(post["uri"])
                post["is_read"] = 0
        self._updateStatusBar()
        if read:
            # Translators: Announced after marking several posts read. {} is the count.
            _announce_now(_("Marked {} posts as read.").format(len(posts)))
        else:
            # Translators: Announced after marking several posts unread. {} is the count.
            _announce_now(_("Marked {} posts as unread.").format(len(posts)))

    def _getRelevantUsers(self, post):
        # Same reasoning as _markSelectedRead above -- lives on the
        # mixin, not just FeedWindow. Confirmed via a real
        # AttributeError that ExploreWindow called this without it
        # existing anywhere on that class.
        authorHandle = post.get("handle") or post.get("author_did")
        # Translators: Label for the post's author in the "which user?" picker. {} is their handle.
        users = [(_("{} (post author)").format(authorHandle), post["author_did"], authorHandle)]
        seen = {post["author_did"]}

        if post.get("is_repost") and post.get("reposted_by_did"):
            reposterDid = post["reposted_by_did"]
            if reposterDid not in seen:
                seen.add(reposterDid)
                reposterHandle = post.get("reposted_by_handle") or reposterDid
                # Translators: Label for who reposted this in the "which user?" picker. {} is their handle.
                users.append((_("{} (reposted by)").format(reposterHandle), reposterDid, reposterHandle))

        replyToDid = post.get("reply_to_did")
        replyToHandle = post.get("reply_to_handle")
        if replyToDid and replyToDid not in seen:
            seen.add(replyToDid)
            # Translators: Label for who this post is replying to in the "which user?" picker. {} is their handle.
            users.append((_("{} (replying to)").format(replyToHandle or replyToDid), replyToDid, replyToHandle))

        facets_json = post.get("facets_json")
        if facets_json:
            try:
                facets = json.loads(facets_json)
            except (ValueError, TypeError):
                facets = []
            for facet in facets:
                for feature in facet.get("features", []):
                    if feature.get("$type") == "app.bsky.richtext.facet#mention":
                        did = feature.get("did")
                        if did and did not in seen:
                            seen.add(did)
                            author = db.get_author(did)
                            handle = author["handle"] if author else did
                            users.append((handle, did, handle))

        return users


class FeedWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    TAB_KEY = "home"  # for MainWindow's remember-last-tab feature
    SUPPORTS_NEW_POST = True
    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    SUPPORTS_JUMP_TO_USER = True

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = TAB_NAME
        self._feedKey = "home"  # switched by onFilterChanged() below
        # index -> (feed_key, feed_uri_or_None); feed_uri is None for
        # the two built-in system feeds, set for a custom saved feed.
        self._filterChoiceMap = {}
        self._initFeedListState()

        toolbarRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Label for the Home feed's filter dropdown.
        filterLabel = wx.StaticText(self, label=_("Feed &filter:"))
        # wx.Choice, not RadioBox -- a saved feed can be added/removed/
        # reordered from Settings > Feed manager while Home is open, and
        # RadioBox can't add/remove choices after construction.
        self.filterChoice = wx.Choice(self)
        toolbarRow.Add(filterLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        toolbarRow.Add(self.filterChoice)

        self._buildStandardFeedSizer(extra_top=toolbarRow)

        self._buildFilterChoices()
        self.filterChoice.Bind(wx.EVT_CHOICE, self.onFilterChanged)
        self._bindStandardFeedEvents()

        # No sync_if_empty here -- onFilterChanged() already calls
        # _syncIfCacheEmpty() itself when switching filters; the
        # initial Following/Discover load stays cache-first/manual F5.
        self._finishStandardFeedInit()

    # ---------------- tab activation (replaces wx.Dialog's EVT_ACTIVATE) ----------------

    def onTabActivated(self):
        # Called by MainWindow whenever this tab becomes the selected
        # notebook page -- same purpose the old EVT_ACTIVATE handler
        # served on wx.Dialog: pick up settings that changed (sort
        # order, display name/handle) while this window stayed open,
        # AND (now that this can be a background tab regaining focus)
        # re-grab real keyboard focus onto this tab's own list.
        self._render()
        if self._account is not None:
            # Announce the tab name explicitly BEFORE moving real focus
            # into the list -- wx.Notebook's own "<tab> tab selected"
            # speech loses the race against the focus change below and
            # never gets heard otherwise (same class of problem as the
            # Home/Notifications focus-stealing bug from earlier).
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
            self._restoreFocusPosition()

    # ---------------- filter ----------------

    def _buildFilterChoices(self):
        """Following/Discover, then every saved feed (Settings > Feed
        manager), in the order the user set there. Preserves the
        current selection by feed_key across a rebuild (e.g. after
        refreshFeedFilterChoices()) when it still exists, falling back
        to "Following" if the currently-selected custom feed was just
        removed."""
        previousFeedKey = self._feedKey
        account = db.get_active_account()
        savedFeeds = db.get_saved_feeds_cache(account["id"]) if account else []

        self.filterChoice.Freeze()
        try:
            self.filterChoice.Clear()
            self._filterChoiceMap = {}
            # Translators: Home feed filter choice for the standard chronological/algorithmic following feed.
            self.filterChoice.Append(_("Following"))
            self._filterChoiceMap[0] = ("home", None)
            # Translators: Home feed filter choice for Bluesky's Discover feed.
            self.filterChoice.Append(_("Discover"))
            self._filterChoiceMap[1] = ("discover", None)
            for i, feed in enumerate(savedFeeds, start=2):
                self.filterChoice.Append(feed["display_name"])
                # feed_key == the raw feed uri, matching how
                # client.sync_feed_generator_page's _store_feed_item
                # actually keys its DB writes (confirmed by reading
                # client.py) -- an earlier "feed:{uri}" prefix here
                # meant _dbGetPage was reading a completely different
                # cache entry than _syncPage ever wrote to, so a
                # custom feed's cache always looked empty and forced a
                # fresh server sync on every single filter switch.
                self._filterChoiceMap[i] = (feed["uri"], feed["uri"])

            selectIndex = 0
            for index, (feedKey, _uri) in self._filterChoiceMap.items():
                if feedKey == previousFeedKey:
                    selectIndex = index
                    break
            self.filterChoice.SetSelection(selectIndex)
            self._feedKey = self._filterChoiceMap[selectIndex][0]
        finally:
            self.filterChoice.Thaw()

    def refreshFeedFilterChoices(self):
        """Called from Settings > Feed manager (via get_main_window())
        whenever the saved-feed list changes, so a newly added/removed/
        reordered feed shows up in this dropdown immediately instead of
        needing MainWindow reopened. Only rebuilds the dropdown itself
        -- does NOT touch postList/re-sync, since the currently-viewed
        feed (if not the one that changed) shouldn't be disturbed."""
        self._buildFilterChoices()

    def onFilterChanged(self, evt):
        index = self.filterChoice.GetSelection()
        if index not in self._filterChoiceMap:
            return
        feedKey, _uri = self._filterChoiceMap[index]
        if feedKey == self._feedKey:
            return

        self._feedKey = feedKey
        self._loadFromCache(reset=True)
        # Show cache immediately; only actually hit the server if this
        # feed has never been synced before (empty cache) -- switching
        # back and forth between filters used to force a fresh
        # re-fetch every single time, which is both slow and pointless
        # once a feed already has a cache to show.
        self._syncIfCacheEmpty()
        if self._account is not None:
            db.set_home_active_filter(self._account["id"], self._feedKey)

    # ---------------- loading (fetch/cache hooks for FeedListMixin) ----------------

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        _feedKey, feedUri = self._filterChoiceMap.get(self.filterChoice.GetSelection(), (self._feedKey, None))
        if feedUri:
            return client.sync_feed_generator_page(atprotoClient, self._account["id"], feedUri, cursor=cursor, limit=limit)
        return client.sync_timeline(atprotoClient, self._account["id"], cursor=cursor, limit=limit, feed_key=self._feedKey)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def _onRepostChanged(self, post):
        # A repost row only exists in Home because I reposted it (you
        # follow yourself implicitly) -- undoing it should drop the row,
        # same pattern as SavedWindow._onBookmarkChanged for unsave.
        if post.get("viewer_repost_uri") or not post.get("is_repost"):
            return
        db.delete_feed_item(self._account["id"], self._feedKey, post["uri"])
        for i, p in enumerate(self._posts):
            if p["uri"] == post["uri"]:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break

    def _getSelectedPosts(self):
        indices = []
        i = self.postList.GetFirstSelected()
        while i != -1:
            indices.append(i)
            i = self.postList.GetNextSelected(i)
        return [self._posts[i] for i in indices if 0 <= i < len(self._posts)]

    def _toggleMarkRead(self, post):
        if post.get("is_read"):
            db.mark_post_unread(post["uri"])
            post["is_read"] = 0
            # Translators: Announced after marking a post unread.
            message = _("Marked as unread.")
        else:
            db.mark_post_read(post["uri"])
            post["is_read"] = 1
            # Translators: Announced after marking a post read.
            message = _("Marked as read.")
        self._updateStatusBar()
        _announce_now(message)

    # _markSelectedRead lives on ItemActionMixin now (used by every
    # host class, not just FeedWindow) -- FeedWindow inherits it.
    # _postInvolvesUser/_jumpToUserPost/onNewPost moved to FeedListMixin
    # (see plan-09.md) so ListsWindow can share them via SUPPORTS_*
    # flags -- nothing left to define here, FeedWindow inherits them.

    # ---------------- post action menu (Alt+A) ----------------

    def _getActionablePost(self):
        # The focused list item already IS the post here -- nothing to
        # resolve, unlike NotificationsWindow's version of this method.
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

# ---------------- user action menu (Alt+U) ----------------

    def onUserAction(self, evt=None):
        if self.postList.GetSelectedItemCount() > 1:
            # Translators: Announced when the user-action menu is invoked with multiple posts selected.
            nvdaUi.message(_("User action needs a single post selected."))
            return

        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return

        users = self._getRelevantUsers(post)

        if len(users) == 1:
            _label, did, handle = users[0]
            self.showUserActionMenu(did, handle)
            return

        menu = wx.Menu()
        for label, did, handle in users:
            submenu = wx.Menu()
            self._populateUserActionMenu(submenu, did, handle)
            menu.AppendSubMenu(submenu, label)
        self.PopupMenu(menu)
        menu.Destroy()

    def _getRelevantUsers(self, post):
        # NOTE: Follow/Mute/Block are one-directional (no
        # unfollow/unmute/unblock) -- toggling needs a getProfile-style
        # relationship lookup that isn't wired up yet.
        authorHandle = post.get("handle") or post.get("author_did")
        # Translators: Label for the post's author in the "which user?" picker. {} is their handle.
        users = [(_("{} (post author)").format(authorHandle), post["author_did"], authorHandle)]
        seen = {post["author_did"]}

        if post.get("is_repost") and post.get("reposted_by_did"):
            reposterDid = post["reposted_by_did"]
            if reposterDid not in seen:
                seen.add(reposterDid)
                reposterHandle = post.get("reposted_by_handle") or reposterDid
                # Translators: Label for who reposted this in the "which user?" picker. {} is their handle.
                users.append((_("{} (reposted by)").format(reposterHandle), reposterDid, reposterHandle))

        replyToDid = post.get("reply_to_did")
        replyToHandle = post.get("reply_to_handle")
        if replyToDid and replyToDid not in seen:
            seen.add(replyToDid)
            # Translators: Label for who this post is replying to in the "which user?" picker. {} is their handle.
            users.append((_("{} (replying to)").format(replyToHandle or replyToDid), replyToDid, replyToHandle))

        facets_json = post.get("facets_json")
        if facets_json:
            try:
                facets = json.loads(facets_json)
            except (ValueError, TypeError):
                facets = []
            for facet in facets:
                for feature in facet.get("features", []):
                    if feature.get("$type") == "app.bsky.richtext.facet#mention":
                        did = feature.get("did")
                        if did and did not in seen:
                            seen.add(did)
                            author = db.get_author(did)
                            handle = author["handle"] if author else did
                            users.append((handle, did, handle))

        return users

    # ---------------- window-level keyboard shortcuts ----------------
    # onCharHook is inherited from FeedListMixin (see SUPPORTS_* flags
    # above) -- Escape/Ctrl+W are still deliberately not handled here:
    # Home is a permanent tab now, so both bubble up to MainWindow.


def _search_feed_key(query, filters):
    filters = filters or {}
    parts = [query, filters.get("author") or "", filters.get("since") or "", filters.get("until") or "", filters.get("lang") or ""]
    return "search:" + "|".join(parts)

