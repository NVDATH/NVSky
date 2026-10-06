"""
Shared feed-list machinery and the Home tab for NVSky.

Holds RemovableTabMixin, FeedListMixin (the cache-first paginated post
list behind every post-list tab), FeedWindow (Home), and shared helpers.
UserActionMixin/UserListMixin, ItemActionMixin, and EmbedViewMixin live in
userActions.py/itemActions.py/embedView.py and are re-exported here, so
"from .feedWindow import UserActionMixin" etc. keep working.
"""
import core
import datetime
import json
import threading
import wx
import speech

import gui
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import timeutils
from . import uiutil
from . import soundpack
from .compose import ComposeDialog, CONTENT_LABEL_DISPLAY_NAMES
from .userActions import UserActionMixin, UserListMixin
from .itemActions import ItemActionMixin
from .embedView import EmbedViewMixin

PAGE_SIZE = 50
MAX_NETWORK_PAGE_WALK = 5

COLUMN_DISPLAY_NAME = "display_name"
COLUMN_HANDLE = "handle"

# Translators: Permanent tab label for the Home feed.
TAB_NAME = _("Home")

# Home's built-in feed keys are "home" and "discover"; a custom feed's key is its uri.

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
    return ", ".join(CONTENT_LABEL_DISPLAY_NAMES.get(l, l) for l in labels if l in client.CONTENT_LABEL_KEYS)


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
    Shared behavior for removable temp tabs: the "<TAB_NAME> - NVSky -
    <handle>" window title, and jumping back to the permanent tab they
    were opened from on Ctrl+W.

    A host must set self._originTabKey (str or None) and call
    self._jumpBackToOrigin() from its onTabRemoved().
    """

    def _updateTitle(self):
        # Translators: Fallback account label in the window title when no account is active.
        accountLabel = self._account["handle"] if getattr(self, "_account", None) else _("no account")
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")
        # Status bar text embeds TAB_NAME, so refresh it after a rename/restore.
        refreshStatus = getattr(self, "_updateStatusBar", None)
        if callable(refreshStatus):
            refreshStatus()

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
        # Persists the rename for every temp tab (hosts may override).
        account = getattr(self, "_account", None)
        if account is not None:
            db.set_temp_tab_custom_name(account["id"], self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY, newName)



class FeedListMixin:
    """
    Shared cache-first paginated list behavior -- lazy-load, status bar,
    loading sound, check-for-updates, focus save/restore, unread
    tracking, keyboard shortcuts -- for every post-list tab.
    Notifications uses it too, supplying its own _dbGetPage/
    _dbGetUnreadCount/_syncPage against the notifications table.

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
        # Refreshes relative time labels ("5 minutes ago") from memory only.
        # Bound to the panel: some hosts call this before postList exists.
        self._timeRefreshTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._onTimeRefreshTick, self._timeRefreshTimer)
        self._timeRefreshTimer.Start(60000)

    # Index of the time column: 3 for the standard layout; hosts with other
    # layouts override it (SetItem on a missing column crashes NVDA).
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

        # Panel tabs keep a short notebook label and put the full title on
        # MainWindow (active tab only); a Dialog host sets its own title.
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
        if getattr(self, "TAB_KEY", None) in ("home", "notifications"):
            from . import refresh_tray
            refresh_tray()

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
        # Always sorts fresh, by position in THIS feed (feed_indexed_at) and
        # not the post's own time: a repost of an old post would sort wrong.
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
        # Default row layout (Embed/Author/Message/Posted); NotificationsWindow overrides it.
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
        hadRows = self.postList.GetItemCount() > 0

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
        if hadRows and not self._posts and self.postList.HasFocus():
            wx.CallAfter(self._focusWhenEmpty)

    @uiutil.safe_ui_callback
    def _focusWhenEmpty(self):
        # An emptied ListCtrl that keeps focus makes NVDA report "unknown".
        uiutil.focus_check_updates_button(self)

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
        Restores the last-focused row (Focus/Select/EnsureVisible) and, only
        when moveFocus=True, grabs real keyboard focus onto postList.
        moveFocus MUST be False from a panel's __init__ (the panel may not
        be in the notebook yet; grabbing focus there announced two tabs
        back to back). MainWindow.addTab()/onTabActivated grant it later.
        SetFocus() comes before Select(): selecting first left
        GetSelectedItemCount() at 0 until a key press.
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
            # SetFocus() isn't synchronous; defer the selection until focus lands.
            wx.CallAfter(self._applyFocusPosition, targetIndex)
        else:
            self._applyFocusPosition(targetIndex)

    def _applyFocusPosition(self, targetIndex):
        self._suppressFocusEvents = True
        try:
            if self._posts:
                # Select() only adds; clear other selections so Alt+A/Alt+U
                # don't see a multi-selection.
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
        # Marks read explicitly: EVT_LIST_ITEM_FOCUSED doesn't fire when the
        # focused index doesn't change. Always scans OLDEST-unread-first,
        # whatever the display sort order.
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
        """Call after _loadFromCache() for a feed_key that can be brand new
        (a fresh preview/list tab, or a Home filter never viewed): does ONE
        silent sync when the cache is empty. Other tabs stay cache-first
        with manual refresh on purpose."""
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
    # Per-class differences are gated by SUPPORTS_* class attributes;
    # ListsWindow overrides _onAltNumber/_onAltU (it branches on list purpose).

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
        # Called when this tab becomes the selected page. Re-reads from the
        # DB (not just re-renders): background sync only live-refreshes the
        # visible tab, and display settings may have changed.
        self._loadFromCache(reset=True)
        if self._account is not None:
            # Announce the tab name BEFORE moving real focus; the notebook's
            # own announcement loses the race against the focus change.
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
            from . import refresh_tray
            refresh_tray()

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

    # ---------------- window-level keyboard shortcuts ----------------
    # onCharHook is inherited from FeedListMixin (see SUPPORTS_* flags
    # above) -- Escape/Ctrl+W are still deliberately not handled here:
    # Home is a permanent tab now, so both bubble up to MainWindow.


def _search_feed_key(query, filters):
    filters = filters or {}
    parts = [query, filters.get("author") or "", filters.get("since") or "", filters.get("until") or "", filters.get("lang") or ""]
    return "search:" + "|".join(parts)