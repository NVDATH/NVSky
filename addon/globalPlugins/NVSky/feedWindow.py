"""
Main feed window for NVSky. v0.9

Home timeline view: report-mode ListCtrl (Author, Message, Posted, Embed),
shows everything cached on open, lazy-loads from the network once the
cache runs out. Other tabs are not implemented yet -- Home only.
"""
import core
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
from .compose import ComposeDialog

LAZY_LOAD_THRESHOLD = 3
PAGE_SIZE = 50
LOADING_BEEP_INTERVAL_MS = 1000
MAX_NETWORK_PAGE_WALK = 5

COLUMN_DISPLAY_NAME = "display_name"
COLUMN_HANDLE = "handle"

TAB_NAME = "Home"

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
    ("Spam", "com.atproto.moderation.defs#reasonSpam"),
    ("Violates community guidelines", "com.atproto.moderation.defs#reasonViolation"),
    ("Misleading", "com.atproto.moderation.defs#reasonMisleading"),
    ("Sexual content", "com.atproto.moderation.defs#reasonSexual"),
    ("Rude or harassing", "com.atproto.moderation.defs#reasonRude"),
    ("Other", "com.atproto.moderation.defs#reasonOther"),
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


def _describe_embed(embed_json: str) -> str:
    if not embed_json:
        return ""
    try:
        embed = json.loads(embed_json)
    except (ValueError, TypeError):
        return ""

    embed_type = embed.get("$type", "")

    if "images" in embed_type:
        images = embed.get("images", [])
        alts = [img.get("alt") for img in images if img.get("alt")]
        label = f"Image ({len(images)})" if len(images) > 1 else "Image"
        if alts:
            label += f": {'; '.join(alts)}"
        return label
    if "video" in embed_type:
        return "Video"
    if "recordWithMedia" in embed_type:
        return "Quote + media"
    if "record" in embed_type:
        return "Quote post"
    if "external" in embed_type:
        return "Link"
    return ""


def _message_text(post: dict) -> str:
    text = post.get("text", "")

    if post.get("quoted_text"):
        quotedAuthor = post.get("quoted_author_handle")
        who = f"@{quotedAuthor}" if quotedAuthor else "original post"
        # No em dash -- screen readers spell it out as two syllables.
        text = f"{text} Quote from {who}: {post['quoted_text']}"

    if post.get("reply_parent_uri"):
        replyToHandle = post.get("reply_to_handle")
        if replyToHandle:
            text = f"Reply to @{replyToHandle}: {text}"

    # The Author column already shows who reposted it (see _authorLabel) --
    # this just needs to name the ORIGINAL author, not repeat the reposter.
    if post.get("is_repost"):
        originalHandle = post.get("handle") or post.get("author_did")
        text = f"Reposted @{originalHandle}: {text}" if originalHandle else f"Reposted: {text}"

    return uiutil.single_line(text)


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
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky")

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
        self._addMenuItem(menu, "View user info", lambda: self.viewProfile(did))
        self._addMenuItem(menu, "Copy profile URL", lambda: self.copyProfileUrl(did, handle))
        self._addMenuItem(menu, "View timeline", lambda: self.showTimeline(did, handle))
        self._addMenuItem(menu, "Show followers", lambda: self.showFollowers(did, handle, display_name))
        self._addMenuItem(menu, "Show following", lambda: self.showFollowing(did, handle, display_name))
        self._addMenuItem(menu, "Open profile on bsky.app",
                           lambda: webbrowser.open(f"https://bsky.app/profile/{handle or did}"))
        menu.AppendSeparator()
        self._addMenuItem(menu, "Follow / Unfollow", lambda: self._toggleRelation(did, handle, "follow"))
        self._addMenuItem(menu, "Mute / Unmute", lambda: self._toggleRelation(did, handle, "mute"))
        self._addMenuItem(menu, "Block / Unblock", lambda: self._toggleRelation(did, handle, "block"))

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
        mainWindow = get_main_window()
        if mainWindow is None:
            nvdaUi.message("Open NVSky's main window first.")
            return

        identity = {"kind": "user_timeline", "key": did}
        if mainWindow.focusTabByIdentity(identity):
            return

        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        ownerLabel = self._displayLabel(handle, display_name)
        tab = UserTimelineTabWindow(mainWindow.notebook, did, ownerLabel, origin_key=originKey)
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

    def _openUserListTab(self, kind, did, handle=None, display_name=None):
        # Opens (or focuses an already-open) UserListTabWindow --
        # replaces the old UserListDialog popup. Needs the real
        # MainWindow, not whatever dialog this mixin happens to be
        # mixed into (e.g. ManageGroupMembersDialog) -- falls back to
        # a message if MainWindow isn't open at all.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            nvdaUi.message("Open NVSky's main window first.")
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
        tab = UserListTabWindow(mainWindow.notebook, kind, did, ownerLabel, origin_key=originKey)
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
        return f"@{handle}" if handle else "unknown user"

    def viewProfile(self, did):
        _announce_now("Loading profile, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onProfileFetched, profile, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onProfileFetched(self, profile, error):
        if error:
            nvdaUi.message(f"Could not load profile: {error}")
            return
        gui.mainFrame.prePopup()
        dlg = ProfileDialog(self, profile)
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
        nvdaUi.message("Checking status, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onRelationStatusChecked, did, handle, kind, profile, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRelationStatusChecked(self, did, handle, kind, profile, error):
        if error:
            nvdaUi.message(f"Could not check status: {error}")
            return

        viewer = (profile or {}).get("viewer") or {}
        label = f"@{handle}" if handle else did

        if kind == "follow":
            currentValue = viewer.get("following")
            question = f"Unfollow {label}?" if currentValue else f"Follow {label}?"
        elif kind == "mute":
            currentValue = viewer.get("muted")
            question = f"Unmute {label}?" if currentValue else f"Mute {label}?"
        else:
            currentValue = viewer.get("blocking")
            question = f"Unblock {label}?" if currentValue else f"Block {label}?"

        confirm = wx.MessageDialog(self, question, "Confirm", wx.YES_NO | wx.NO_DEFAULT)
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
            wx.CallAfter(self._onUserActionDone, "Done." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserActionDone(self, message, error):
        if error:
            log.error(f"NVSky: user action failed: {error}")
            nvdaUi.message(f"Action failed: {error}")
            return
        if message:
            nvdaUi.message(message)


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
        self.userList.InsertColumn(0, "Handle", width=180)
        self.userList.InsertColumn(1, "Display name", width=180)
        self.userList.InsertColumn(2, "Bio", width=300)

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
            nvdaUi.message("No user selected.")
            return
        self.showUserActionMenu(user["did"], user["handle"], user.get("display_name"))

    def onUserListCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if evt.AltDown() and keyCode == ord("U"):
            self.onUserAction()
            return
        if keyCode == wx.WXK_F5 and not evt.ShiftDown() and not evt.ControlDown():
            self.onCheckForUpdates(None)
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
            self._addMenuItem(menu, "Open image", lambda: self._openEmbedImage(url))
            self._addMenuItem(menu, "Send image to Be My Eyes", lambda: self._sendEmbedImageToBeMyEyes(url))
            self._addMenuItem(menu, "Copy image to clipboard", lambda: self._copyEmbedImageToClipboard(url))
            added = True
        elif len(images) > 1:
            for i, img in enumerate(images):
                url = img["fullsize_url"]
                # "&" forces the digit itself as the access key -- the
                # default would use the first letter ("I") for every
                # item, since they'd all start with "Image".
                label = f"Image &{i + 1}"
                imageMenu = wx.Menu()
                self._addMenuItem(imageMenu, "Open", lambda url=url: self._openEmbedImage(url))
                self._addMenuItem(imageMenu, "Send to Be My Eyes", lambda url=url: self._sendEmbedImageToBeMyEyes(url))
                self._addMenuItem(imageMenu, "Copy to clipboard", lambda url=url: self._copyEmbedImageToClipboard(url))
                menu.AppendSubMenu(imageMenu, label)
                added = True

        videoUrl = embed.get("video_url")
        if videoUrl:
            self._addMenuItem(menu, "Open video", lambda: self._openEmbedVideo(videoUrl))
            self._addMenuItem(menu, "Copy video URL", lambda: self._copyEmbedUrl(videoUrl, "Video URL"))
            added = True

        linkUrl = embed.get("link_url")
        if linkUrl:
            title = embed.get("link_title") or linkUrl
            self._addMenuItem(menu, f"Open link: {title}", lambda: webbrowser.open(linkUrl))
            self._addMenuItem(menu, "Copy link URL", lambda: self._copyEmbedUrl(linkUrl, "Link URL"))
            added = True

        if not added:
            menu.Destroy()
            return None
        return menu

    def _copyEmbedUrl(self, url, label):
        self._copyToClipboard(url)
        _announce_now(f"{label} copied to clipboard.")

    def _openEmbedImage(self, url):
        _announce_now("Downloading image, please wait...")

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onActionDone, "Image opened." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    def _sendEmbedImageToBeMyEyes(self, url):
        _announce_now("Downloading image, please wait...")

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                ok = attachments.send_to_bemyeyes(path)
                error = None if ok else "Could not launch Be My Eyes. Is it installed?"
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onActionDone, "Sent to Be My Eyes." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    def _copyEmbedImageToClipboard(self, url):
        def announce_start():
            speech.cancelSpeech()
            nvdaUi.message("Downloading image, please wait...")

        core.callLater(150, announce_start)

        def copy_and_notify(path):
            try:
                ok = attachments.copy_image_to_clipboard(path)
                error = None if ok else "Could not read the downloaded image."
                self._onActionDone("Image copied to clipboard." if not error else None, error)
            except Exception as e:
                self._onActionDone(None, str(e))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                wx.CallAfter(copy_and_notify, path)
            except Exception as e:
                wx.CallAfter(self._onActionDone, None, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _openEmbedVideo(self, url):
        _announce_now("Downloading video, please wait...")

        def worker():
            try:
                path = attachments.download_video_playlist_to_temp(url)
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onActionDone, "Video opened." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onActionDone(self, message, error):
        def announce_immediately(text):
            speech.cancelSpeech()  # เธ•เธฑเธ”เธเธ—/เธซเธขเธธเธ”เน€เธชเธตเธขเธเธ—เธตเนเธเธณเธฅเธฑเธเธญเนเธฒเธเธเนเธญเธเธงเธฒเธกเนเธ ListCtrl เธ—เธฑเธเธ—เธต
            nvdaUi.message(text)   # เธเธนเธ”เธเนเธญเธเธงเธฒเธกเธเธญเธเน€เธฃเธฒเนเธ—เธเธ—เธฑเธเธ—เธต

        if error:
            log.error(f"NVSky: action failed: {error}")
            core.callLater(200, announce_immediately, f"Action failed: {error}")
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
        mode, _ = timeutils.current_mode_and_pattern(db)
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
        accountLabel = self._account["handle"] if self._account else "no account"

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
            self.statusBar.SetStatusText(f"{self.TAB_NAME} {unread} unread {len(self._posts)} total")
        else:
            self.statusBar.SetStatusText(f"{self.TAB_NAME} {len(self._posts)} total")

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
            self._posts = self._applySortOrder(self._dbGetPage())
        self._render()

    def _buildFeedListColumns(self):
        self.postList.InsertColumn(0, "Embed", width=140)
        self.postList.InsertColumn(1, "Author", width=180)
        self.postList.InsertColumn(2, "Message", width=330)
        self.postList.InsertColumn(3, "Posted", width=140)

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
        self.postActionButton = wx.Button(self, label="Post action... (Alt+A)")
        self.userActionButton = wx.Button(self, label="User action... (Alt+U)")
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
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def _finishStandardFeedInit(self, sync_if_empty=False):
        """Standard __init__ tail: title, cache load, no-account message,
        focus-position restore, optional initial sync-if-empty. Call last,
        after self._feedKey (and anything _syncPage/_dbGetPage need) is
        already set."""
        self._updateTitle()
        self._loadFromCache(reset=True)

        if self._account is None:
            nvdaUi.message("No active account. Log in from Settings first.")
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
        self.postList.InsertItem(index, _describe_embed(post.get("embed_json")))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _message_text(post))
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
        hasItems = bool(self._posts)
        for buttonName in ("postActionButton", "userActionButton"):
            button = getattr(self, buttonName, None)
            if button is not None:
                button.Show(hasItems)
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
            nvdaUi.message(f"No item {n}.")
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

            if not post.get("is_read"):
                self._markItemRead(post)
                post["is_read"] = 1
                self._updateStatusBar()

        evt.Skip()

    def onFetchPreviousPosts(self, evt):
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        if self._loadingMore:
            nvdaUi.message(f"Already fetching older {self.TAB_NAME} posts, please wait...")
            return
        if not self._posts:
            nvdaUi.message(f"Nothing loaded yet in {self.TAB_NAME}.")
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
        nvdaUi.message("No unread posts.")

    def _selectAllPosts(self):
        self._suppressFocusEvents = True
        try:
            for i in range(len(self._posts)):
                self.postList.SetItemState(i, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
        finally:
            self._suppressFocusEvents = False
        nvdaUi.message(f"{len(self._posts)} posts selected.")

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

        nvdaUi.message(f"Loading older {self.TAB_NAME} posts, please wait...")
        self._startLoadingBeep()

        def worker():
            cursor = self._olderCursor
            foundOlder = False
            error = None
            try:
                atprotoClient = client.get_client_for_active_account()
                for _ in range(MAX_NETWORK_PAGE_WALK):
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
            nvdaUi.message(f"Could not load more {self.TAB_NAME} posts: {error}")
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
                nvdaUi.message(f"{self.TAB_NAME} load complete.")
            elif addedCount == 1:
                nvdaUi.message(f"1 older post loaded in {self.TAB_NAME}.")
            else:
                nvdaUi.message(f"{addedCount} older posts loaded in {self.TAB_NAME}.")
        elif cursor:
            # Walked MAX_NETWORK_PAGE_WALK pages without turning up a new
            # cached post (e.g. a stretch of muted/hidden posts got
            # filtered out) -- the timeline itself isn't actually
            # exhausted since the cursor is still valid, so press
            # Shift+F5 again to keep looking further back.
            nvdaUi.message(f"No older {self.TAB_NAME} posts found nearby -- press Shift+F5 again to keep looking back.")
        else:
            nvdaUi.message(f"No more {self.TAB_NAME} posts to load.")

    def _startLoadingBeep(self):
        self._loadingTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._onLoadingBeepTick, self._loadingTimer)
        self._loadingTimer.Start(LOADING_BEEP_INTERVAL_MS)

    def _onLoadingBeepTick(self, evt):
        tones.beep(500, 50)

    def _stopLoadingBeep(self):
        if self._loadingTimer is not None:
            self._loadingTimer.Stop()
            self._loadingTimer = None

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
            nvdaUi.message("No active account.")
            return
        if self._checkingUpdates:
            return

        self._checkingUpdates = True
        nvdaUi.message(f"Checking {self.TAB_NAME} feed for updates, please wait...")
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
            nvdaUi.message(f"Check for updates failed: {error}")
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
            nvdaUi.message(f"No new posts in {self.TAB_NAME} feed.")
        elif newCount == 1:
            nvdaUi.message(f"1 new post in {self.TAB_NAME} feed.")
        else:
            nvdaUi.message(f"{newCount} new posts in {self.TAB_NAME} feed.")

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
        if keyCode == wx.WXK_SPACE and getattr(self, "SUPPORTS_FOCUS_NEXT_UNREAD", False) and self.FindFocus() is self.postList:
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
        for _ in range(n):
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

        nvdaUi.message(f"No more posts involving @{targetHandle} in that direction.")

    # ---------------- new post (SUPPORTS_NEW_POST) ----------------

    def onNewPost(self, evt=None):
        dlg = ComposeDialog(self, onClosed=None)
        dlg.Show()


class ProfileDialog(UserActionMixin, wx.Dialog):
    """Read-only profile info view. Press Alt+U for the user action menu (view/follow/mute/block/etc.)."""

    def __init__(self, parent, profile: dict):
        self._profile = profile
        title = profile.get("display_name") or f"@{profile['handle']}"
        super().__init__(parent, title=f"{title} - Profile", size=(500, 400))

        sizer = wx.BoxSizer(wx.VERTICAL)

        info = (
            f"Display name: {profile.get('display_name') or '(none)'}\n"
            f"Handle: @{profile['handle']}\n"
            f"Followers: {profile['followers_count']}\n"
            f"Following: {profile['follows_count']}\n"
            f"Posts: {profile['posts_count']}\n\n"
            f"{profile.get('description') or '(no bio)'}"
        )
        textCtrl = wx.TextCtrl(self, value=info, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        closeBtn = wx.Button(self, label="&Close")
        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        btnSizer.Add(closeBtn, flag=wx.ALIGN_CENTER)
        sizer.Add(btnSizer, flag=wx.ALIGN_CENTER | wx.ALL, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        if evt.AltDown() and evt.GetKeyCode() == ord("U"):
            self.showUserActionMenu(self._profile["did"], self._profile["handle"], self._profile.get("display_name"))
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

class UserListTabWindow(RemovableTabMixin, UserActionMixin, UserListMixin, wx.Panel):
    """
    Browsable user-list tab -- replaces the old UserListDialog and
    covers three kinds: "followers"/"following" (fixed to one account's
    did) and "search" (a people-search query, opened via Explore's
    "Open in new tab"). F5 re-fetches from the network -- no local
    cache/pagination, same fetch-once-per-refresh behavior the old
    dialog had. Not persisted differently by kind -- all three
    persist/restore the same way as any other temp tab.
    """

    _KIND_LABELS = {"followers": "followers", "following": "following", "search": "search results"}

    def __init__(self, parent, kind, target, owner_label=None, origin_key=None):
        super().__init__(parent)
        self._account = db.get_active_account()
        self._kind = kind  # "followers" / "following" / "search"
        self._target = target  # did for followers/following, query for search
        if kind in ("followers", "following"):
            self.TAB_NAME = f"{kind.capitalize()} of {owner_label}"
        else:
            self.TAB_NAME = f"People search: {target}"
        self.TAB_TEMP_TYPE = "user_list"
        self.TAB_TEMP_KEY = f"{kind}:{target}"
        self._originTabKey = origin_key
        self._users = []

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.userList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._buildUserListColumns()
        sizer.Add(self.userList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.userActionButton = wx.Button(self, label="&User action... (Alt+U)")
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.userList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onUserAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onUserListCharHook)

        self._pendingIsInitial = True
        cached = db.get_user_list_cache(self._account["id"], self.TAB_TEMP_KEY) if self._account else None
        self._hadInitialCache = cached is not None
        if cached is not None:
            self._users = cached
            self._renderUsers()
        else:
            self.statusBar.SetStatusText("Loading, please wait...")
        self._fetch()

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        self.userList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "user_list", self.TAB_TEMP_KEY)
            db.delete_user_list_cache(self._account["id"], self.TAB_TEMP_KEY)
        self._jumpBackToOrigin()

    def _userListLabel(self):
        return f"{self.TAB_NAME} -- {len(self._users)} total"

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        nvdaUi.message(f"Loading {self._KIND_LABELS[self._kind]}, please wait...")
        self._pendingIsInitial = False
        self._fetch()

    def _fetch(self):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if self._kind == "followers":
                    users = client.get_followers(atprotoClient, self._target)
                elif self._kind == "following":
                    users = client.get_follows(atprotoClient, self._target)
                else:
                    response = client.search_actors(atprotoClient, self._target)
                    users = [
                        {
                            "did": a.did, "handle": a.handle,
                            "display_name": getattr(a, "display_name", None),
                            "description": getattr(a, "description", None),
                        }
                        for a in response.actors
                    ]
                error = None
            except Exception as e:
                users = None
                error = str(e)
            wx.CallAfter(self._onFetchDone, users, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onFetchDone(self, users, error):
        label = self._KIND_LABELS[self._kind]
        isInitial = self._pendingIsInitial
        self._pendingIsInitial = False
        if error:
            if not self._users:
                nvdaUi.message(f"Could not load {label}: {error}")
                self.statusBar.SetStatusText(f"Could not load {label}.")
            else:
                nvdaUi.message(f"Could not refresh {label}: {error}")
            return

        users = users or []
        changed = users != self._users
        self._users = users
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], self.TAB_TEMP_KEY, users)

        # Cache-first pattern (same convention as FeedManagerPanel in
        # settings.py): a silent background refresh that found no
        # changes doesn't re-render or re-announce -- the cached view
        # already shown at open is still accurate. First-ever load, a
        # manual F5, or a background refresh that DID find changes
        # renders and announces normally.
        if not (isInitial and self._hadInitialCache and not changed):
            self._renderUsers()
            nvdaUi.message(f"{len(self._users)} {label} loaded.")

class ThreadTabWindow(RemovableTabMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    Full thread view, popped into its own removable tab -- replaces
    ThreadDialog. NOT a FeedListMixin host: a thread is a tree
    (ancestors + depth-first replies), not chronological pagination,
    so there's no "load older" -- F5 always re-fetches the WHOLE thread
    fresh (a genuinely new reply from someone else can appear this
    way). Cache-first on open via db.get_user_list_cache/
    set_user_list_cache under key f"thread:{root_uri}" -- same
    convention UserListTabWindow uses, just reused here rather than a
    separate helper since the shape (a plain list of dicts) is
    identical.

    Same reduced Post-action set as before conversion (Like, Copy,
    Embed) -- Reply/Repost/Quote parity still deferred.
    """

    def __init__(self, parent, root_uri, posts, target_index, origin_key=None):
        super().__init__(parent)
        self._account = db.get_active_account()
        self._rootUri = root_uri
        self.TAB_TEMP_TYPE = "thread"
        self.TAB_TEMP_KEY = root_uri
        self._originTabKey = origin_key
        self._posts = posts
        self._targetUri = posts[target_index]["uri"] if posts and target_index < len(posts) else root_uri
        self.TAB_NAME = self._makeTabName(posts)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.postList.InsertColumn(0, "Author", width=140)
        self.postList.InsertColumn(1, "Message", width=380)
        self.postList.InsertColumn(2, "Posted", width=140)
        self.postList.InsertColumn(3, "Embed", width=140)
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.postActionButton = wx.Button(self, label="Post action... (Alt+A)")
        self.userActionButton = wx.Button(self, label="User action... (Alt+U)")
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onPostAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._renderThread(target_index=target_index)

    def _makeTabName(self, posts):
        if not posts:
            return "Thread"
        firstPost = posts[0]
        preview = firstPost.get("text", "")
        if len(preview) > 40:
            preview = preview[:40] + "..."
        label = self._displayLabel(firstPost.get("handle"), firstPost.get("display_name"))
        return f"Thread: {label}: {preview}"

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        self.postList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "thread", self._rootUri)
            db.delete_user_list_cache(self._account["id"], f"thread:{self._rootUri}")
        self._jumpBackToOrigin()

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == wx.WXK_F5 and not evt.ShiftDown() and not evt.ControlDown():
            self.onCheckForUpdates(None)
            return
        if evt.AltDown() and keyCode == ord("A"):
            self.onPostAction()
            return
        if evt.AltDown() and keyCode == ord("U"):
            self.onUserAction()
            return
        evt.Skip()

    def _renderThread(self, target_index=None):
        # Keeps whichever post is currently focused (by uri) unless
        # target_index is given explicitly -- mirrors the "stay on the
        # same message" principle used elsewhere (e.g. ChatWindow.
        # _showMessages), since a re-fetch can insert a genuinely new
        # reply anywhere in the list.
        previousUri = None
        if target_index is None:
            focused = self._getFocusedPost()
            if focused is not None:
                previousUri = focused["uri"]

        self.postList.Freeze()
        try:
            self.postList.DeleteAllItems()
            for i, post in enumerate(self._posts):
                depth = post.get("_thread_depth", 0)
                prefix = "> " * depth
                self.postList.InsertItem(i, self._displayLabel(post.get("handle"), post.get("display_name")))
                self.postList.SetItem(i, 1, prefix + _message_text(post))
                self.postList.SetItem(i, 2, _format_post_time(post.get("indexed_at")))
                self.postList.SetItem(i, 3, _describe_embed(post.get("embed_json")))

            if self._posts:
                if target_index is not None:
                    focusIndex = min(target_index, len(self._posts) - 1)
                else:
                    lookupUri = previousUri or self._targetUri
                    focusIndex = next(
                        (i for i, p in enumerate(self._posts) if p["uri"] == lookupUri), None
                    )
                    if focusIndex is None:
                        focusIndex = min(
                            next((i for i, p in enumerate(self._posts) if p["uri"] == self._targetUri), 0),
                            len(self._posts) - 1,
                        )
                for i in range(self.postList.GetItemCount()):
                    if i != focusIndex and self.postList.GetItemState(i, wx.LIST_STATE_SELECTED):
                        self.postList.SetItemState(i, 0, wx.LIST_STATE_SELECTED)
                self.postList.Focus(focusIndex)
                self.postList.Select(focusIndex)
                self.postList.EnsureVisible(focusIndex)
        finally:
            self.postList.Thaw()

        self.statusBar.SetStatusText(f"{self.TAB_NAME} -- {len(self._posts)} posts")

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        nvdaUi.message("Loading thread, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                posts, targetIndex = client.get_thread(atprotoClient, self._targetUri)
                error = None
            except Exception as e:
                posts, targetIndex = None, 0
                error = str(e)
            wx.CallAfter(self._onRefreshDone, posts, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRefreshDone(self, posts, error):
        if error:
            nvdaUi.message(f"Could not refresh thread: {error}")
            return
        self._posts = posts or []
        self.TAB_NAME = self._makeTabName(self._posts)
        self._updateTitle()
        self._renderThread()
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], f"thread:{self._rootUri}", self._posts)
        nvdaUi.message(f"{len(self._posts)} posts in thread.")

    def _getFocusedPost(self):
        index = self.postList.GetFocusedItem()
        if 0 <= index < len(self._posts):
            return self._posts[index]
        return None

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post.get("author_did"), post.get("handle"), post.get("display_name"))

    def onPostAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return

        menu = wx.Menu()
        isLiked = bool(post.get("viewer_like_uri"))
        self._addMenuItem(menu, "Unlike" if isLiked else "Like", lambda: self._togglePostLike(post))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        self._addMenuItem(copyMenu, "Copy post text", lambda: self._copyPostText(post))
        self._addMenuItem(copyMenu, "Copy link to post", lambda: self._copyPostLink(post))
        menu.AppendSubMenu(copyMenu, "Copy...")

        embedMenu = self._buildViewEmbedMenu(post)
        if embedMenu is not None:
            menu.AppendSubMenu(embedMenu, "Embed...")

        self.PopupMenu(menu)
        menu.Destroy()

    def _togglePostLike(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_like_uri"):
                    client.unlike_post(atprotoClient, post["viewer_like_uri"])
                    post["viewer_like_uri"] = None
                    message = "Unliked."
                else:
                    like_uri = client.like_post(atprotoClient, post["uri"], post["cid"])
                    post["viewer_like_uri"] = like_uri
                    message = "Liked."
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onActionDone, message, error)

        threading.Thread(target=worker, daemon=True).start()

    def _copyPostText(self, post):
        self._copyToClipboard(post.get("text", ""))
        _announce_now("Post text copied to clipboard.")

    def _copyPostLink(self, post):
        handle = post.get("handle") or post.get("author_did")
        rkey = post["uri"].rsplit("/", 1)[-1]
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        self._copyToClipboard(url)
        _announce_now("Post URL copied to clipboard.")


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

    def _showPostActionMenu(self, post):
        isOwnPost = self._account and post.get("author_did") == self._account.get("did")

        menu = wx.Menu()

        if isOwnPost:
            self._addMenuItem(menu, "Edit who can reply...", lambda: self._editReplyPermissions(post))
            self._addMenuItem(menu, "Delete post...", lambda: self._deletePost(post))
            menu.AppendSeparator()

        self._addMenuItem(menu, "Reply...", lambda: self._openReply(post))
        self._addMenuItem(menu, "Undo repost" if post.get("viewer_repost_uri") else "Repost",
                           lambda: self._toggleRepost(post))
        self._addMenuItem(menu, "Quote post...", lambda: self._openQuote(post))
        self._addMenuItem(menu, "View thread...", lambda: self._openThread(post))
        menu.AppendSeparator()

        markMenu = wx.Menu()
        self._addMenuItem(markMenu, "Read", lambda: self._setMarkRead(post, True))
        self._addMenuItem(markMenu, "Unread", lambda: self._setMarkRead(post, False))
        menu.AppendSubMenu(markMenu, "Mar&k as...")

        isLiked = bool(post.get("viewer_like_uri"))
        self._addMenuItem(menu, "Unlike" if isLiked else "Like", lambda: self._togglePostLike(post))
        self._addMenuItem(menu, "Unsave" if post.get("viewer_bookmarked") else "Save",
                           lambda: self._toggleBookmark(post))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        self._addMenuItem(copyMenu, "Copy post text", lambda: self._copyPostText(post))
        self._addMenuItem(copyMenu, "Copy link to post", lambda: self._copyPostLink(post))
        menu.AppendSubMenu(copyMenu, "Copy...")

        embedMenu = self._buildViewEmbedMenu(post)
        if embedMenu is not None:
            menu.AppendSubMenu(embedMenu, "Embed...")
        menu.AppendSeparator()

        moreMenu = wx.Menu()
        self._addMenuItem(moreMenu, "Mute thread", lambda: self._muteThread(post))
        self._addMenuItem(moreMenu, "Hide post for me", lambda: self._hidePost(post))
        self._addMenuItem(moreMenu, "Report post...", lambda: self._reportPost(post))
        menu.AppendSubMenu(moreMenu, "More...")

        self.PopupMenu(menu)
        menu.Destroy()

    def _showBulkPostActionMenu(self, selectedCount):
        menu = wx.Menu()
        markMenu = wx.Menu()
        self._addMenuItem(markMenu, f"Read ({selectedCount} selected)", lambda: self._markSelectedRead(True))
        self._addMenuItem(markMenu, f"Unread ({selectedCount} selected)", lambda: self._markSelectedRead(False))
        menu.AppendSubMenu(markMenu, "Mar&k as...")
        self.PopupMenu(menu)
        menu.Destroy()

    def _setMarkRead(self, post, read: bool):
        if read:
            db.mark_post_read(post["uri"])
            post["is_read"] = 1
            message = "Marked as read."
        else:
            db.mark_post_unread(post["uri"])
            post["is_read"] = 0
            message = "Marked as unread."
        self._updateStatusBar()
        _announce_now(message)

    def _togglePostLike(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_like_uri"):
                    client.unlike_post(atprotoClient, post["viewer_like_uri"])
                    db.set_post_like_uri(post["uri"], None)
                    post["viewer_like_uri"] = None
                    message = "Unliked."
                else:
                    like_uri = client.like_post(atprotoClient, post["uri"], post["cid"])
                    db.set_post_like_uri(post["uri"], like_uri)
                    post["viewer_like_uri"] = like_uri
                    message = "Liked."
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onActionDone, message, error)

        threading.Thread(target=worker, daemon=True).start()

    def _toggleBookmark(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_bookmarked"):
                    client.unbookmark_post(atprotoClient, post["uri"])
                    db.set_post_bookmarked(post["uri"], False)
                    post["viewer_bookmarked"] = False
                    message = "Removed from saved."
                else:
                    client.bookmark_post(atprotoClient, post["uri"], post["cid"])
                    db.set_post_bookmarked(post["uri"], True)
                    post["viewer_bookmarked"] = True
                    message = "Saved post success."
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onBookmarkToggleDone, post, message, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onBookmarkToggleDone(self, post, message, error):
        self._onActionDone(message, error)
        if error:
            return
        # Default no-op -- only a tab that actually LISTS posts by their
        # saved status (SavedWindow) needs to react when one gets
        # unsaved. Home/Notifications don't list by bookmark status, so
        # toggling Save there should never remove the row.
        onBookmarkChanged = getattr(self, "_onBookmarkChanged", None)
        if callable(onBookmarkChanged):
            onBookmarkChanged(post)

    def _copyPostText(self, post):
        self._copyToClipboard(post.get("text", ""))
        _announce_now("Post text copied to clipboard.")

    def _copyPostLink(self, post):
        handle = post.get("handle") or post.get("author_did")
        rkey = post["uri"].rsplit("/", 1)[-1]
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        self._copyToClipboard(url)
        _announce_now("Post URL copied to clipboard.")

    def _muteThread(self, post):
        root_uri = post.get("reply_parent_uri") or post["uri"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.mute_thread(atprotoClient, root_uri)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onActionDone, "Thread muted." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

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
        _announce_now("Post hidden.")

    def _deletePost(self, post):
        confirm = wx.MessageDialog(
            self, "Delete this post? This can't be undone.", "Delete post",
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
            nvdaUi.message(f"Delete failed: {error}")
            return
        for i, p in enumerate(self._posts):
            if p["uri"] == uri:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break
        nvdaUi.message("Post deleted.")
        self.onCheckForUpdates(None)

    def _editReplyPermissions(self, post):
        _announce_now("Loading current reply settings, please wait...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onReplyPermissionsLoaded(self, post, threadgate, disablesQuotes, error):
        if error:
            nvdaUi.message(f"Could not load current reply settings: {error}")
            # Fall through anyway -- still let them set it, just without
            # the current values pre-selected.

        state = threadgate.get("state", "everyone")
        rules = threadgate.get("rules", set())
        hasListRules = threadgate.get("has_list_rules", False)

        dlg = wx.Dialog(self, title="Edit who can reply", style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        stateChoices = ["Allow anyone to reply", "Disable replies entirely", "Custom"]
        stateIndex = {"everyone": 0, "nobody": 1, "custom": 2}.get(state, 0)
        stateBox = wx.RadioBox(dlg, label="Who can reply", choices=stateChoices, style=wx.RA_SPECIFY_ROWS)
        stateBox.SetSelection(stateIndex)
        sizer.Add(stateBox, flag=wx.ALL | wx.EXPAND, border=8)

        followersCheck = wx.CheckBox(dlg, label="Allow your followers to reply")
        followingCheck = wx.CheckBox(dlg, label="Allow people you follow to reply")
        mentionedCheck = wx.CheckBox(dlg, label="Allow people you mention to reply")
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
                label=(
                    "Note: this post also has list-based reply rules set "
                    "(e.g. from the Bluesky app). NVSky can't edit those "
                    "yet -- saving here will remove them."
                ),
            )
            listWarning.Wrap(350)
            sizer.Add(listWarning, flag=wx.ALL | wx.EXPAND, border=8)

        quoteCheck = wx.CheckBox(dlg, label="Disable quote posts of this post")
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
            wx.CallAfter(self._onActionDone, "Reply permissions updated." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

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
            nvdaUi.message("Open NVSky's main window first.")
            return

        _announce_now("Loading thread, please wait...")

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
        if error:
            nvdaUi.message(f"Could not load thread: {error}")
            return
        posts = posts or []
        rootUri = posts[0]["uri"] if posts else None
        if rootUri is None:
            nvdaUi.message("Could not load thread: no root post found.")
            return

        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return

        identity = {"kind": "thread", "key": rootUri}
        if mainWindow.focusTabByIdentity(identity):
            return

        tab = ThreadTabWindow(mainWindow.notebook, rootUri, posts, targetIndex, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.set_user_list_cache(account["id"], f"thread:{rootUri}", posts)
            db.add_open_temp_tab(account["id"], {
                "type": "thread", "key": rootUri, "root_uri": rootUri, "origin_key": originKey,
            })

    def _toggleRepost(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_repost_uri"):
                    client.unrepost_post(atprotoClient, post["viewer_repost_uri"])
                    db.set_post_repost_uri(post["uri"], None)
                    post["viewer_repost_uri"] = None
                    message = "Repost undone."
                else:
                    repost_uri = client.repost_post(atprotoClient, post["uri"], post["cid"])
                    db.set_post_repost_uri(post["uri"], repost_uri)
                    post["viewer_repost_uri"] = repost_uri
                    message = "Reposted."
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onActionDone, message, error)

        threading.Thread(target=worker, daemon=True).start()

    def _reportPost(self, post):
        labels = [label for label, _ in REPORT_REASONS]
        dlg = wx.SingleChoiceDialog(self, "Reason for reporting this post:", "Report post", labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        reasonType = REPORT_REASONS[dlg.GetSelection()][1]
        dlg.Destroy()

        confirm = wx.MessageDialog(
            self, "Send this report to Bluesky moderation?", "Confirm report", wx.YES_NO | wx.NO_DEFAULT
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
            wx.CallAfter(self._onActionDone, "Report sent." if not error else None, error)

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
        _announce_now(f"Marked {len(posts)} posts as {'read' if read else 'unread'}.")


class UserTimelineTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    A user's own post timeline, popped into its own removable tab --
    replaces UserTimelineDialog. Full FeedListMixin cache-first/
    lazy-load machinery via client.sync_author_feed_page's feed_key
    (f"user_timeline:{did}"). Not user-renameable.
    """

    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True

    def __init__(self, parent, did: str, owner_label: str, origin_key: str = None):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = f"Timeline of {owner_label}"
        self._did = did
        self.TAB_TEMP_TYPE = "user_timeline"
        self.TAB_TEMP_KEY = did
        self._feedKey = f"user_timeline:{did}"
        self._originTabKey = origin_key
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit(sync_if_empty=True)

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            nvdaUi.message(f"{self.TAB_NAME} tab")
            self._restoreFocusPosition()

    def onTabRemoved(self):
        for value in vars(self).values():
            if isinstance(value, wx.Timer):
                value.Stop()
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "user_timeline", self._did)
        self._jumpBackToOrigin()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _describe_embed(post.get("embed_json")))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_author_feed_page(atprotoClient, self._account["id"], self._did, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))


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
        filterLabel = wx.StaticText(self, label="Feed &filter:")
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
            nvdaUi.message(f"{self.TAB_NAME} tab")
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
            self.filterChoice.Append("Following")
            self._filterChoiceMap[0] = ("home", None)
            self.filterChoice.Append("Discover")
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
            message = "Marked as unread."
        else:
            db.mark_post_read(post["uri"])
            post["is_read"] = 1
            message = "Marked as read."
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
            nvdaUi.message("No post selected.")
        return post

# ---------------- user action menu (Alt+U) ----------------

    def onUserAction(self, evt=None):
        if self.postList.GetSelectedItemCount() > 1:
            nvdaUi.message("User action needs a single post selected.")
            return

        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return

        users = self._getRelevantUsers(post)

        if len(users) == 1:
            _, did, handle = users[0]
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
        users = [(f"{authorHandle} (post author)", post["author_did"], authorHandle)]
        seen = {post["author_did"]}

        if post.get("is_repost") and post.get("reposted_by_did"):
            reposterDid = post["reposted_by_did"]
            if reposterDid not in seen:
                seen.add(reposterDid)
                reposterHandle = post.get("reposted_by_handle") or reposterDid
                users.append((f"{reposterHandle} (reposted by)", reposterDid, reposterHandle))

        replyToDid = post.get("reply_to_did")
        replyToHandle = post.get("reply_to_handle")
        if replyToDid and replyToDid not in seen:
            seen.add(replyToDid)
            users.append((f"{replyToHandle or replyToDid} (replying to)", replyToDid, replyToHandle))

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

class StarterPackDetailsDialog(wx.Dialog):
    """Read-only starter pack info, plus the same actions available
    from the results-list context menu -- so the user doesn't have to
    close this and reopen the menu separately."""

    def __init__(self, parent, pack, full):
        self._pack = pack
        record = getattr(full, "record", None)
        title = getattr(record, "name", None) or "Starter pack"
        super().__init__(parent, title=f"{title} - Starter pack", size=(500, 420))

        profiles = getattr(full, "list_items_sample", None) or []
        lines = [
            f"Name: {title}",
            f"Creator: @{full.creator.handle}",
            f"Description: {getattr(record, 'description', '') or '(none)'}",
            f"Members: {len(profiles)}",
        ]
        if profiles:
            lines.append("")
            lines.append("People included:")
            lines.extend(f"  @{item.subject.handle}" for item in profiles)
        feeds = getattr(full, "feeds", None) or []
        if feeds:
            lines.append("")
            lines.append("Feeds included:")
            lines.extend(f"  {f.display_name}" for f in feeds)

        sizer = wx.BoxSizer(wx.VERTICAL)
        textCtrl = wx.TextCtrl(self, value="\n".join(lines), style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        followBtn = wx.Button(self, label="&Follow everyone in this pack")
        openBtn = wx.Button(self, label="&Open on bsky.app")
        closeBtn = wx.Button(self, label="&Close")
        btnSizer.Add(followBtn, flag=wx.RIGHT, border=5)
        btnSizer.Add(openBtn, flag=wx.RIGHT, border=5)
        btnSizer.Add(closeBtn)
        sizer.Add(btnSizer, flag=wx.ALIGN_CENTER | wx.ALL, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        followBtn.Bind(wx.EVT_BUTTON, lambda e: self._doFollow())
        openBtn.Bind(wx.EVT_BUTTON, lambda e: self._doOpen())
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def _doFollow(self):
        parent = self.GetParent()
        pack = self._pack
        self.Close()
        parent._followStarterPack(pack)

    def _doOpen(self):
        parent = self.GetParent()
        pack = self._pack
        self.Close()
        parent._openStarterPackInBrowser(pack)

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()


class ExploreWindow(FeedListMixin, ItemActionMixin, UserActionMixin, UserListMixin, EmbedViewMixin, wx.Panel):
    """Posts result type reuses FeedListMixin fully (search results
    hydrated into the same posts cache via client.search_posts_hydrated,
    same as notifications' resolve_posts -- Post action/React/Reply/
    View thread all just work). People/Starter packs/Feeds are simpler
    dedicated lists swapped in via Show/Hide, People reusing
    UserActionMixin's menu the same way ManageGroupMembersDialog does.
    _dbGetPage/_syncPage are unused stubs -- this never goes through
    FeedListMixin's cache/pagination path, only _runSearch below."""

    TAB_KEY = "explore"
    SUPPORTS_SELECT_ALL = True
    SUPPORTS_FOCUS_NEXT_UNREAD = True
    RESULT_TYPES = ["Posts", "People", "Starter packs", "Feeds"]
    SEARCH_DEBOUNCE_MS = 800

    def _addAdvField(self, panel, sizer, label):
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(panel, label=label), flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        ctrl = wx.TextCtrl(panel)
        row.Add(ctrl, proportion=1)
        sizer.Add(row, flag=wx.EXPAND | wx.BOTTOM, border=5)
        return ctrl

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = "Explore"
        self._feedKey = "explore"
        self._tracksUnread = False
        self._initFeedListState()
        self._users = []
        self._starterPacks = []
        self._feeds = []
        self._advExpanded = False
        self._sourceQuery = None
        self._filters = {}

        sizer = wx.BoxSizer(wx.VERTICAL)

        searchRow = wx.BoxSizer(wx.HORIZONTAL)
        searchLabel = wx.StaticText(self, label="&Search:")
        self.searchText = wx.TextCtrl(self)
        searchRow.Add(searchLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        searchRow.Add(self.searchText, proportion=1)
        sizer.Add(searchRow, flag=wx.EXPAND | wx.ALL, border=10)

        self.typeRadio = wx.RadioBox(
            self, label="Result type", choices=self.RESULT_TYPES, majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        sizer.Add(self.typeRadio, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # LOW CONFIDENCE -- since/until/author/lang params never tested
        # against a real server, recalled from general lexicon
        # knowledge not a debug_dump. Only used for Posts.
        # wx.CollapsiblePane instead of a checkbox -- it doesn't
        # support native mnemonic dispatch, so the & in the label is
        # cosmetic only; real Alt+V activation is wired up via a
        # manual AcceleratorTable below (see onToggleAdvancedAccel).
        # State text ("expanded"/"collapsed") is written into the
        # label by hand rather than relying on any automatic
        # accessible-state announcement.
        # Plain wx.Button + wx.Panel instead of wx.CollapsiblePane --
        # CollapsiblePane's internal child structure fires TWO
        # accessibility events on Windows (its own UIA wrapper plus the
        # underlying native disclosure triangle), which reads the
        # label twice the first time focus lands on it -- confirmed
        # unfixable from the wx/app side (see plan-13.md follow-up). A
        # plain Button is a single atomic control and never has this.
        self.advBtn = wx.Button(self, label="")
        sizer.Add(self.advBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.advPanel = wx.Panel(self)
        advSizer = wx.BoxSizer(wx.VERTICAL)
        self.advFromText = self._addAdvField(self.advPanel, advSizer, "&From handle:")
        self.advSinceText = self._addAdvField(self.advPanel, advSizer, "S&ince (YYYY-MM-DD):")
        self.advUntilText = self._addAdvField(self.advPanel, advSizer, "&Until (YYYY-MM-DD):")
        self.advLangText = self._addAdvField(self.advPanel, advSizer, "&Language:")
        self.advPanel.SetSizer(advSizer)
        sizer.Add(self.advPanel, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self._setAdvExpanded(False, layout=False)
        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        
        self._buildFeedListColumns()
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        self.peopleList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.userList = self.peopleList  # UserListMixin operates on self.userList
        self._buildUserListColumns()
        sizer.Add(self.peopleList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        self.peopleList.Hide()

        self.starterPacksList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.starterPacksList.InsertColumn(0, "Name", width=200)
        self.starterPacksList.InsertColumn(1, "Creator", width=180)
        self.starterPacksList.InsertColumn(2, "Description", width=300)
        sizer.Add(self.starterPacksList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        self.starterPacksList.Hide()

        self.feedsResultList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.feedsResultList.InsertColumn(0, "Name", width=200)
        self.feedsResultList.InsertColumn(1, "Creator", width=180)
        self.feedsResultList.InsertColumn(2, "Description", width=250)
        self.feedsResultList.InsertColumn(3, "Likes", width=80)
        sizer.Add(self.feedsResultList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        self.feedsResultList.Hide()

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.postActionButton = wx.Button(self, label="Post action... (Alt+A)")
        self.userActionButton = wx.Button(self, label="User action... (Alt+U)")
        self.resultActionButton = wx.Button(self, label="Action...")
        self.openInTabButton = wx.Button(self, label="Open in new &tab")
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.resultActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.openInTabButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self._debounceTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.onDebounceTimer, self._debounceTimer)
        self.searchText.Bind(wx.EVT_TEXT, self.onSearchTextChanged)
        self.typeRadio.Bind(wx.EVT_RADIOBOX, self.onTypeChanged)
        self.advBtn.Bind(wx.EVT_BUTTON, self.onAdvBtnClick)
        self._advToggleId = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.onToggleAdvancedAccel, id=self._advToggleId)
        # Bound to this panel, not a true top-level window -- per
        # testing, Alt+V won't fire while focus is already deep inside
        # the pane's own child fields (advFromText etc). Accepted
        # limitation rather than plumbing this up to MainWindow.
        self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_ALT, ord("V"), self._advToggleId)]))
        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.resultActionButton.Bind(wx.EVT_BUTTON, self.onResultAction)
        self.openInTabButton.Bind(wx.EVT_BUTTON, lambda e: self._openInNewTab())
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onItemActivated)
        self.peopleList.Bind(wx.EVT_CONTEXT_MENU, self.onPeopleContextMenu)
        self.starterPacksList.Bind(wx.EVT_CONTEXT_MENU, self.onStarterPackContextMenu)
        self.feedsResultList.Bind(wx.EVT_CONTEXT_MENU, self.onFeedResultContextMenu)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._showResultListForType()
        self._updateStatusBar()

    def _setAdvExpanded(self, expanded, layout=True):
        self._advExpanded = expanded
        self.advPanel.Show(expanded)
        status = "expanded" if expanded else "collapsed"
        self.advBtn.SetLabel(f"Ad&vanced search ({status})")
        if layout:
            self.Layout()

    def onAdvBtnClick(self, evt):
        self._setAdvExpanded(not self._advExpanded)
        if self._advExpanded:
            self.advFromText.SetFocus()

    def onToggleAdvancedAccel(self, evt):
        if self._currentType() != "Posts":
            return
        self._setAdvExpanded(not self._advExpanded)
        self.advBtn.SetFocus()
        
    def _restoreFocusPosition(self, moveFocus=True):
        if self.FindFocus() is self.searchText:
            return  # never steal focus while the user is actively typing
        if not getattr(self, "_didInitialFocus", False):
            self._didInitialFocus = True
            if moveFocus:
                self.searchText.SetFocus()
            return
        # Later calls (F5/refresh, debounced re-search) fall through to
        # the normal FeedListMixin behavior (focus the result list) --
        # only the very first ever call goes to the search box.
        super()._restoreFocusPosition(moveFocus=moveFocus)

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        self.searchText.SetFocus()

    def onCheckForUpdates(self, evt):
        self._runSearch()

    # ---------------- FeedListMixin contract -- unused, search bypasses the cache path ----------------

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        if not self._feedKey:
            return []
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return 0

    def _syncPage(self, atprotoClient, cursor, limit):
        # NOTE: guard on _sourceQuery, not _feedKey -- _feedKey is set
        # to a fixed "explore" placeholder from __init__ (so
        # _dbGetPage works before any search), so it's always truthy
        # and never actually guards anything here. _sourceQuery is the
        # real "has a Posts search actually run yet" signal; without
        # this guard, Ctrl+F5's checkAllOpenTabs hit this on every
        # untouched Explore tab and called search_posts(q=None).
        if not self._sourceQuery:
            return None
        return client.sync_search_page(
            atprotoClient, self._account["id"], self._feedKey, self._sourceQuery, cursor=cursor, limit=limit,
            author=self._filters.get("author"), since=self._filters.get("since"),
            until=self._filters.get("until"), lang=self._filters.get("lang"),
        )

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def _getSelectedPosts(self):
        indices = []
        i = self.postList.GetFirstSelected()
        while i != -1:
            indices.append(i)
            i = self.postList.GetNextSelected(i)
        return [self._posts[i] for i in indices if 0 <= i < len(self._posts)]

    def _render(self):
        super()._render()
        if self._currentType() == "Posts":
            self._updateActionButtons()

    # ---------------- search ----------------

    def onSearchTextChanged(self, evt):
        self._debounceTimer.Stop()
        if self.searchText.GetValue().strip():
            self._debounceTimer.StartOnce(self.SEARCH_DEBOUNCE_MS)

    def onDebounceTimer(self, evt):
        self._runSearch()

    def onTypeChanged(self, evt):
        self._showResultListForType()
        self._runSearch()

    def _currentType(self):
        return self.RESULT_TYPES[self.typeRadio.GetSelection()]

    def onCharHook(self, evt):
        # Alt+A/Alt+U for non-Posts result types needs to route to
        # onResultAction/onPeopleContextMenu instead of the generic
        # ItemActionMixin.onCharHook's onPostAction()/onUserAction() --
        # those act on self.postList's focused post regardless of
        # which result list actually has focus, so Alt+A on a stale
        # Posts search's leftover focused post was firing instead of
        # the Feeds/Starter packs/People action shown on the "Action..."
        # button's own label. Posts stays on the normal mixin path.
        if self._currentType() != "Posts":
            keyCode = evt.GetKeyCode()
            if evt.AltDown() and keyCode == ord("A"):
                self.onResultAction()
                return
            if evt.AltDown() and keyCode == ord("U") and self._currentType() == "People":
                self.onResultAction()
                return
            # Alt+1-9 (announce Nth newest post) and Shift+F5 (fetch
            # older posts) both read/act on self._posts unconditionally
            # in the generic ItemActionMixin handler below, same class
            # of bug Alt+A had -- self._posts is Posts-search-only here,
            # so these would announce a stale/empty post instead of
            # doing anything meaningful for People/Starter packs/Feeds
            # results. Space and Ctrl+A don't need the same treatment,
            # they already guard on self.FindFocus() is self.postList.
            if evt.AltDown() and ord("1") <= keyCode <= ord("9"):
                self._announceNthResult(keyCode - ord("0"))
                return
            if keyCode == wx.WXK_F5 and evt.ShiftDown():
                return
        super().onCharHook(evt)

    def _announceNthResult(self, n):
        listCtrl, data = {
            "People": (self.peopleList, self._users),
            "Starter packs": (self.starterPacksList, self._starterPacks),
            "Feeds": (self.feedsResultList, self._feeds),
        }[self._currentType()]
        if not (1 <= n <= len(data)):
            nvdaUi.message(f"No item {n}.")
            return
        index = n - 1
        parts = [listCtrl.GetItemText(index, col) for col in range(listCtrl.GetColumnCount())]
        _announce_now(", ".join(p for p in parts if p))

    def _showResultListForType(self):
        resultType = self._currentType()
        self.postList.Show(resultType == "Posts")
        self.peopleList.Show(resultType == "People")
        self.starterPacksList.Show(resultType == "Starter packs")
        self.feedsResultList.Show(resultType == "Feeds")
        self.advBtn.Show(resultType == "Posts")
        if resultType != "Posts":
            self._setAdvExpanded(False, layout=False)
        self._updateActionButtons()

    def _userListLabel(self):
        return f"{len(self._users)} people found."

    def _updateActionButtons(self):
        resultType = self._currentType()
        counts = {"Posts": len(self._posts), "People": len(self._users),
                  "Starter packs": len(self._starterPacks), "Feeds": len(self._feeds)}
        hasResults = counts.get(resultType, 0) > 0
        self.postActionButton.Show(resultType == "Posts" and hasResults)
        self.userActionButton.Show(resultType == "Posts" and hasResults)
        self.resultActionButton.Show(resultType != "Posts" and hasResults)
        self.openInTabButton.Show(resultType in ("Posts", "People") and hasResults)
        labels = {"People": "User action... (Alt+U)", "Starter packs": "Pack action... (Alt+A)", "Feeds": "Feed action... (Alt+A)"}
        if resultType in labels:
            self.resultActionButton.SetLabel(labels[resultType])
        self.Layout()

    def onResultAction(self, evt=None):
        resultType = self._currentType()
        if resultType == "People":
            self.onPeopleContextMenu(None)
        elif resultType == "Starter packs":
            self.onStarterPackContextMenu(None)
        elif resultType == "Feeds":
            self.onFeedResultContextMenu(None)

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def onUserAction(self, evt=None):
        if self.postList.GetSelectedItemCount() > 1:
            nvdaUi.message("User action needs a single post selected.")
            return
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        users = self._getRelevantUsers(post)
        if len(users) == 1:
            _, did, handle = users[0]
            self.showUserActionMenu(did, handle)
            return
        menu = wx.Menu()
        for label, did, handle in users:
            submenu = wx.Menu()
            self._populateUserActionMenu(submenu, did, handle)
            menu.AppendSubMenu(submenu, label)
        self.PopupMenu(menu)
        menu.Destroy()

    def _runSearch(self):
        query = self.searchText.GetValue().strip()
        if not query or self._account is None:
            return
        resultType = self._currentType()

        if resultType == "Posts":
            self._sourceQuery = query
            self._filters = {
                "author": self.advFromText.GetValue().strip() or None,
                "since": self.advSinceText.GetValue().strip() or None,
                "until": self.advUntilText.GetValue().strip() or None,
                "lang": self.advLangText.GetValue().strip() or None,
            }
            self._feedKey = _search_feed_key(query, self._filters)
            # onCheckForUpdates (not _loadFromCache directly) is what
            # actually calls _syncPage -- _loadFromCache alone only
            # reads whatever's already cached under _feedKey, which is
            # why a fresh query showed 0 and a repeated one silently
            # showed stale results.
            super().onCheckForUpdates(None)
            return

        nvdaUi.message("Searching...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if resultType == "People":
                    results = client.search_actors(atprotoClient, query)
                elif resultType == "Starter packs":
                    results = client.search_starter_packs(atprotoClient, query)
                else:
                    results = client.search_feeds(atprotoClient, query)
                error = None
            except Exception as e:
                results = None
                error = str(e)
            wx.CallAfter(self._onSearchDone, resultType, query, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSearchDone(self, resultType, query, results, error):
        if query != self.searchText.GetValue().strip() or resultType != self._currentType():
            return
        if error is not None:
            nvdaUi.message(f"Search failed: {error}")
            return
        if resultType == "People":
            self._users = [
                {
                    "did": a.did, "handle": a.handle,
                    "display_name": getattr(a, "display_name", None),
                    "description": getattr(a, "description", None),
                }
                for a in results.actors
            ]
            self._renderUsers()
            nvdaUi.message(f"{len(self._users)} people found.")
        elif resultType == "Starter packs":
            self._starterPacks = list(results.starter_packs)
            self._renderStarterPacks()
            nvdaUi.message(f"{len(self._starterPacks)} starter packs found.")
        else:
            self._feeds = list(getattr(results, "feeds", []))
            self._renderFeeds()
            nvdaUi.message(f"{len(self._feeds)} feeds found.")
        self._updateActionButtons()

    def onPeopleContextMenu(self, evt):
        user = self._getFocusedUser()
        if user is None:
            return
        self.showUserActionMenu(user["did"], user["handle"], user.get("display_name"))

    def _renderStarterPacks(self):
        self.starterPacksList.DeleteAllItems()
        for i, pack in enumerate(self._starterPacks):
            record = pack.record
            self.starterPacksList.InsertItem(i, getattr(record, "name", "Starter pack"))
            creator = getattr(pack, "creator", None)
            self.starterPacksList.SetItem(i, 1, f"@{creator.handle}" if creator else "")
            self.starterPacksList.SetItem(i, 2, (getattr(record, "description", None) or "").replace("\n", " "))
        if self._starterPacks:
            self.starterPacksList.Focus(0)
            self.starterPacksList.Select(0)

    def onStarterPackContextMenu(self, evt):
        index = self.starterPacksList.GetFocusedItem()
        if index == -1 or index >= len(self._starterPacks):
            return
        pack = self._starterPacks[index]
        menu = wx.Menu()
        self._addMenuItem(menu, "View pack details...", lambda: self._viewStarterPackDetails(pack))
        self._addMenuItem(menu, "Follow everyone in this pack", lambda: self._followStarterPack(pack))
        self._addMenuItem(menu, "Open on bsky.app", lambda: self._openStarterPackInBrowser(pack))
        self.PopupMenu(menu)
        menu.Destroy()

    def _followStarterPack(self, pack):
        nvdaUi.message("Following everyone in the pack...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                full = client.get_starter_pack_full(atprotoClient, pack.uri)
                count = client.follow_starter_pack_members(atprotoClient, full)
                error = None
            except Exception as e:
                count = 0
                error = str(e)
            wx.CallAfter(self._onFollowStarterPackDone, count, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onFollowStarterPackDone(self, count, error):
        if error:
            nvdaUi.message(f"Could not follow pack members: {error}")
            return
        nvdaUi.message(f"Followed {count} new {'person' if count == 1 else 'people'}.")

    def _viewStarterPackDetails(self, pack):
        nvdaUi.message("Loading pack details...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                full = client.get_starter_pack_full(atprotoClient, pack.uri)
                error = None
            except Exception as e:
                full = None
                error = str(e)
            wx.CallAfter(self._onStarterPackDetailsDone, pack, full, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onStarterPackDetailsDone(self, pack, full, error):
        if error or full is None:
            nvdaUi.message(f"Could not load pack details: {error or 'no data returned'}")
            return
        StarterPackDetailsDialog(self, pack, full).Show()

    def _openStarterPackInBrowser(self, pack):
        rkey = pack.uri.rsplit("/", 1)[-1]
        creator = getattr(pack, "creator", None)
        webbrowser.open(f"https://bsky.app/starter-pack/{creator.handle if creator else ''}/{rkey}")

    def _renderFeeds(self):
        self.feedsResultList.DeleteAllItems()
        for i, feedGen in enumerate(self._feeds):
            self.feedsResultList.InsertItem(i, feedGen.display_name or "Feed")
            creator = getattr(feedGen, "creator", None)
            self.feedsResultList.SetItem(i, 1, f"@{creator.handle}" if creator else "")
            self.feedsResultList.SetItem(i, 2, (feedGen.description or "").replace("\n", " "))
            self.feedsResultList.SetItem(i, 3, str(getattr(feedGen, "like_count", 0) or 0))
        if self._feeds:
            self.feedsResultList.Focus(0)
            self.feedsResultList.Select(0)

    def onFeedResultContextMenu(self, evt):
        index = self.feedsResultList.GetFocusedItem()
        if index == -1 or index >= len(self._feeds):
            return
        feedGen = self._feeds[index]
        menu = wx.Menu()
        self._addMenuItem(menu, "View feed...", lambda: self._viewFeed(feedGen))
        self._addMenuItem(menu, "Add to my feeds", lambda: self._addFeed(feedGen))
        self._addMenuItem(
            menu, "Open on bsky.app",
            lambda: webbrowser.open(f"https://bsky.app/profile/{feedGen.creator.handle}/feed/{feedGen.uri.rsplit('/', 1)[-1]}"),
        )
        self.PopupMenu(menu)
        menu.Destroy()

    def _viewFeed(self, feedGen):
        mainWindow = self.GetTopLevelParent()
        name = feedGen.display_name or "Feed"
        panel = FeedPreviewTabWindow(mainWindow.notebook, name, "feed", feedGen.uri, origin_key="explore")
        mainWindow.addTab(panel, name, select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "search_preview", "key": panel._feedKey, "kind": "feed", "source_key": feedGen.uri,
            "name": name, "origin_key": "explore",
        })

    def onPostAction(self, evt=None):
        super().onPostAction(evt)

    def _openSearchInNewTab(self):
        if not self._feedKey or self._currentType() != "Posts":
            nvdaUi.message("Search for posts first.")
            return
        mainWindow = self.GetTopLevelParent()
        name = f'Search: {self._sourceQuery}'
        panel = FeedPreviewTabWindow(
            mainWindow.notebook, name, "search", self._sourceQuery,
            filters=self._filters, feed_key=self._feedKey, origin_key="explore",
        )
        mainWindow.addTab(panel, name, select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "search_preview", "key": self._feedKey, "kind": "search", "source_key": self._sourceQuery,
            "name": name, "filters": self._filters, "origin_key": "explore",
        })

    def _openInNewTab(self):
        if self._currentType() == "People":
            self._openUserSearchInNewTab()
        else:
            self._openSearchInNewTab()

    def _openUserSearchInNewTab(self):
        query = self.searchText.GetValue().strip()
        if not query:
            nvdaUi.message("Search for people first.")
            return
        mainWindow = self.GetTopLevelParent()
        identity = {"kind": "user_list", "key": f"search:{query}"}
        if mainWindow.focusTabByIdentity(identity):
            return
        tab = UserListTabWindow(mainWindow.notebook, "search", query, origin_key="explore")
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        if self._account is not None:
            db.add_open_temp_tab(self._account["id"], {
                "type": "user_list", "key": f"search:{query}", "list_kind": "search",
                "query": query, "origin_key": "explore",
            })

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def onUserAction(self, evt=None):
        if self.postList.GetSelectedItemCount() > 1:
            nvdaUi.message("User action needs a single post selected.")
            return
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        users = self._getRelevantUsers(post)
        if len(users) == 1:
            _, did, handle = users[0]
            self.showUserActionMenu(did, handle)
            return
        menu = wx.Menu()
        for label, did, handle in users:
            submenu = wx.Menu()
            self._populateUserActionMenu(submenu, did, handle)
            menu.AppendSubMenu(submenu, label)
        self.PopupMenu(menu)
        menu.Destroy()

    def _addFeed(self, feedGen):
        nvdaUi.message("Adding feed...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                added = client.add_feed_to_saved(atprotoClient, feedGen.uri)
                error = None
            except Exception as e:
                added = False
                error = str(e)
            wx.CallAfter(self._onAddFeedDone, added, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onAddFeedDone(self, added, error):
        if error:
            nvdaUi.message(f"Could not add feed: {error}")
            return
        nvdaUi.message("Feed added." if added else "Already in your feeds.")


def _search_feed_key(query, filters):
    filters = filters or {}
    parts = [query, filters.get("author") or "", filters.get("since") or "", filters.get("until") or "", filters.get("lang") or ""]
    return "search:" + "|".join(parts)


class FeedPreviewTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """Popped-out feed generator or pinned search -- structurally a
    clone of ListTabWindow (real cached/paginated feed via
    _dbGetPage/_syncPage), not a one-shot fetch. kind is "feed" or
    "search"; source_key is the feed_uri or search query."""

    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True

    def __init__(self, parent, tab_name: str, kind: str, source_key: str, filters: dict = None, feed_key: str = None, origin_key: str = None):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = tab_name
        self._kind = kind
        self._sourceKey = source_key
        self._filters = filters or {}
        self._feedKey = feed_key or (source_key if kind == "feed" else _search_feed_key(source_key, self._filters))
        self._originTabKey = origin_key
        self.TAB_TEMP_TYPE = "search_preview"
        self.TAB_TEMP_KEY = self._feedKey
        self._tracksUnread = True
        self._initFeedListState()

        extraWidgets = None
        if kind == "feed":
            self.addFeedButton = wx.Button(self, label="&Add to my feeds")
            extraWidgets = [self.addFeedButton]

        self._buildStandardFeedSizer(extra_action_widgets=extraWidgets)
        self._bindStandardFeedEvents()
        if kind == "feed":
            self.addFeedButton.Bind(wx.EVT_BUTTON, lambda e: self._addFeedFromPreview())

        self._finishStandardFeedInit(sync_if_empty=True)

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            nvdaUi.message(f"{self.TAB_NAME} tab")
            self._restoreFocusPosition()

    def onTabRemoved(self):
        for value in vars(self).values():
            if isinstance(value, wx.Timer):
                value.Stop()
        if self._account:
            db.remove_open_temp_tab(self._account["id"], self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)
        self._jumpBackToOrigin()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return 0

    def _syncPage(self, atprotoClient, cursor, limit):
        if not self._sourceKey:
            return None
        if self._kind == "feed":
            return client.sync_feed_generator_page(atprotoClient, self._account["id"], self._sourceKey, cursor=cursor, limit=limit)
        return client.sync_search_page(
            atprotoClient, self._account["id"], self._feedKey, self._sourceKey, cursor=cursor, limit=limit,
            author=self._filters.get("author"), since=self._filters.get("since"),
            until=self._filters.get("until"), lang=self._filters.get("lang"),
        )

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])
        

    def _addFeedFromPreview(self):
        self.addFeedButton.Disable()
        nvdaUi.message("Adding feed...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                added = client.add_feed_to_saved(atprotoClient, self._sourceKey)
                error = None
            except Exception as e:
                added = False
                error = str(e)
            wx.CallAfter(self._onAddFeedFromPreviewDone, added, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onAddFeedFromPreviewDone(self, added, error):
        self.addFeedButton.Enable()
        if error:
            nvdaUi.message(f"Could not add feed: {error}")
            return
        nvdaUi.message("Feed added." if added else "Already in your feeds.")


class SavedWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    TAB_KEY = "saved"

    """
    Saved posts (bookmarks) -- structurally identical to FeedWindow's
    Home feed (real posts, same ItemActionMixin Post action menu
    applies unchanged), just backed by feed_key "saved" / sync_saved()
    instead of the Following timeline. No filter -- there's only one
    "Saved" list, unlike Home's Following/Discover choice.

    NOTE: unsaving a post from its own Post action menu (Alt+A -> Save/
    Unsave, already shared via ItemActionMixin) toggles the bookmark on
    the server but does NOT remove the row from this list immediately
    -- same as everywhere else, that only happens on next Check for
    updates (F5). Matches existing behavior elsewhere (e.g. Hide post),
    not a new gap introduced here.
    """

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = "Saved"
        self._feedKey = "saved"
        self._tracksUnread = False
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit()

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            nvdaUi.message(f"{self.TAB_NAME} tab")
            self._restoreFocusPosition()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def _onBookmarkChanged(self, post):
        if post.get("viewer_bookmarked"):
            return  # re-saved (or still saved) -- nothing to remove
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

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _describe_embed(post.get("embed_json")))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_saved(atprotoClient, self._account["id"], cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    # onCharHook is inherited from FeedListMixin -- no SUPPORTS_* flags
    # needed here, this class never had Space/Ctrl+A/Left-Right/Ctrl+N.

class AddListDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Add list", size=(420, 320))

        sizer = wx.BoxSizer(wx.VERTICAL)

        nameLabel = wx.StaticText(self, label="Name:")
        self.nameText = wx.TextCtrl(self)
        sizer.Add(nameLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.nameText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        descLabel = wx.StaticText(self, label="Description (optional):")
        self.descText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 80))
        sizer.Add(descLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        sizer.Add(self.descText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.purposeRadio = wx.RadioBox(
            self, label="List type",
            choices=[
                "For browsing (see everyone's posts together as one timeline)",
                "For moderation (mute or block this whole group of accounts)",
            ],
        )
        sizer.Add(self.purposeRadio, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        btnSizer = wx.StdDialogButtonSizer()
        okBtn = wx.Button(self, wx.ID_OK, label="Create")
        cancelBtn = wx.Button(self, wx.ID_CANCEL)
        btnSizer.AddButton(okBtn)
        btnSizer.AddButton(cancelBtn)
        btnSizer.Realize()
        sizer.Add(btnSizer, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()
        self.nameText.SetFocus()

    def getValues(self):
        purpose = client.LIST_PURPOSE_CURATE if self.purposeRadio.GetSelection() == 0 else client.LIST_PURPOSE_MOD
        return self.nameText.GetValue().strip(), self.descText.GetValue().strip(), purpose


class SubscribeListDialog(wx.Dialog):
    """
    Find someone else's lists by searching for their handle/name (same
    typeahead search as Manage members below), then either open one of
    their curation lists straight as a tab -- get_list_feed works off
    any public list uri, no subscription needed, this is the "browse
    their content" path -- or subscribe (mute/block) to one of their
    moderation lists, which DOES need a real subscription since that's
    what makes it apply to your own timeline.
    """

    def __init__(self, parent, on_subscribed=None):
        self._userSuggestions = []
        self._selectedUser = None
        self._userLists = []
        self._onSubscribed = on_subscribed

        super().__init__(parent, title="Find lists by user", size=(480, 480))

        sizer = wx.BoxSizer(wx.VERTICAL)

        userLabel = wx.StaticText(self, label="Search for a user by handle or name:")
        self.userSearchText = wx.TextCtrl(self)
        sizer.Add(userLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.userSearchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.userChoice = wx.Choice(self, choices=[])
        sizer.Add(self.userChoice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.loadListsButton = wx.Button(self, label="Show their lists")
        sizer.Add(self.loadListsButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.loadListsButton.Hide()  # nothing to load until a user is picked

        self.listsLabel = wx.StaticText(self, label="Their lists:")
        self.listsCheckBox = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.listsLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.listsCheckBox, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.openTabButton = wx.Button(self, label="Open checked as tabs")
        self.subscribeAsMuteButton = wx.Button(self, label="Subscribe checked (mute)")
        self.subscribeAsBlockButton = wx.Button(self, label="Subscribe checked (block)")
        actionRow.Add(self.openTabButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.subscribeAsMuteButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.subscribeAsBlockButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing to open/subscribe until their lists are actually loaded.
        self.listsLabel.Hide()
        self.listsCheckBox.Hide()
        self.openTabButton.Hide()
        self.subscribeAsMuteButton.Hide()
        self.subscribeAsBlockButton.Hide()

        closeBtn = wx.Button(self, label="&Close")
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.userSearchText.Bind(wx.EVT_TEXT, self.onUserSearchChanged)
        self.loadListsButton.Bind(wx.EVT_BUTTON, self.onLoadLists)
        self.openTabButton.Bind(wx.EVT_BUTTON, self.onOpenAsTabs)
        self.subscribeAsMuteButton.Bind(wx.EVT_BUTTON, lambda e: self.onSubscribe("mute"))
        self.subscribeAsBlockButton.Bind(wx.EVT_BUTTON, lambda e: self.onSubscribe("block"))
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.userSearchText.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        parent = self.GetParent()
        self.Destroy()
        if parent is not None:
            parent.subscribeButton.SetFocus()

    def onUserSearchChanged(self, evt):
        wx.CallLater(400, self._runUserSearch, self.userSearchText.GetValue())

    def _runUserSearch(self, query):
        if query != self.userSearchText.GetValue():
            return  # a newer keystroke already superseded this debounce
        if not query.strip():
            self.userChoice.Set([])
            self._userSuggestions = []
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                results = client.search_actors_typeahead(atprotoClient, query)
                error = None
            except Exception as e:
                results = []
                error = str(e)
            wx.CallAfter(self._onUserSearchDone, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserSearchDone(self, results, error):
        if error:
            return
        self._userSuggestions = results
        self.userChoice.Set([f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in results])
        self.loadListsButton.Show(bool(results))
        self.Layout()
        if results:
            self.userChoice.SetSelection(0)

    def onLoadLists(self, evt):
        index = self.userChoice.GetSelection()
        if not (0 <= index < len(self._userSuggestions)):
            nvdaUi.message("Search for a user and pick one from the list first.")
            return
        self._selectedUser = self._userSuggestions[index]
        nvdaUi.message(f'Loading lists for @{self._selectedUser["handle"]}, please wait...')

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                entries = client.get_lists(atprotoClient, self._selectedUser["did"])
                error = None
            except Exception as e:
                entries = []
                error = str(e)
            wx.CallAfter(self._onUserListsLoaded, entries, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserListsLoaded(self, entries, error):
        if error:
            nvdaUi.message(f"Could not load their lists: {error}")
            return
        # getLists(actor=them) can also include lists THEY subscribed
        # to (not just ones they created) -- only show ones they
        # actually authored, showing back a list they merely subscribe
        # to isn't useful here.
        self._userLists = [e for e in entries if e["creator_did"] == self._selectedUser["did"]]
        hasLists = bool(self._userLists)
        self.listsLabel.Show(hasLists)
        self.listsCheckBox.Show(hasLists)
        self.openTabButton.Show(hasLists)
        self.subscribeAsMuteButton.Show(hasLists)
        self.subscribeAsBlockButton.Show(hasLists)
        self.Layout()
        self.listsCheckBox.Set([self._listChoiceLabel(l) for l in self._userLists])
        self.listsCheckBox.CheckedItems = []
        if hasLists:
            self.listsCheckBox.SetSelection(0)
        if not self._userLists:
            nvdaUi.message(f'@{self._selectedUser["handle"]} has no public lists.')
        else:
            nvdaUi.message(f"{len(self._userLists)} lists loaded.")

    def _listChoiceLabel(self, lst):
        kind = "moderation list" if lst["purpose"] == client.LIST_PURPOSE_MOD else "curation list"
        return f'{lst["name"]} ({kind})'

    def onOpenAsTabs(self, evt):
        checked = [self._userLists[i] for i in self.listsCheckBox.CheckedItems if 0 <= i < len(self._userLists)]
        curateLists = [l for l in checked if l["purpose"] == client.LIST_PURPOSE_CURATE]
        if not curateLists:
            nvdaUi.message("Check at least one curation list first -- moderation lists don't have a timeline to open.")
            return
        mainWindow = self.GetParent().GetTopLevelParent()
        account = db.get_active_account()
        for i, lst in enumerate(curateLists):
            tab = ListTabWindow(mainWindow.notebook, lst["uri"], lst["name"], origin_key="lists")
            mainWindow.addTab(tab, lst["name"], select=(i == len(curateLists) - 1), removable=True)
            if account is not None:
                db.add_open_temp_tab(account["id"], {
                    "type": "list", "key": lst["uri"], "list_uri": lst["uri"], "list_name": lst["name"],
                    "origin_key": "lists",
                })
        nvdaUi.message(f"Opened {len(curateLists)} list{'s' if len(curateLists) != 1 else ''} as tabs.")

    def onSubscribe(self, action):
        checked = [self._userLists[i] for i in self.listsCheckBox.CheckedItems if 0 <= i < len(self._userLists)]
        modLists = [l for l in checked if l["purpose"] == client.LIST_PURPOSE_MOD]
        if not modLists:
            nvdaUi.message("Check at least one moderation list first -- curation lists can't be muted/blocked, use Open checked as tabs instead.")
            return

        def worker():
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for lst in modLists:
                try:
                    if action == "mute":
                        client.mute_actor_list(atprotoClient, lst["uri"])
                    else:
                        client.block_actor_list(atprotoClient, lst["uri"])
                except Exception as e:
                    errors.append(f'{lst["name"]}: {e}')
            wx.CallAfter(self._onSubscribeDone, len(modLists) - len(errors), errors)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSubscribeDone(self, successCount, errors):
        if successCount:
            nvdaUi.message(f"Subscribed to {successCount} list{'s' if successCount != 1 else ''}.")
            if self._onSubscribed:
                self._onSubscribed()
        if errors:
            nvdaUi.message("Some subscriptions failed: " + "; ".join(errors))


class ManageMembersDialog(wx.Dialog):
    """
    Add/remove members for a list. Read-only (no Add/Remove controls)
    if you're not the list's creator -- membership can only be edited
    by the creator per the AT Protocol's own permission model.
    """

    def __init__(self, parent, list_info: dict):
        self._listInfo = list_info
        self._members = list(list_info.get("members", []))
        self._isOwner = list_info["creator_did"] == db.get_active_account()["did"]
        self._suggestions = []

        super().__init__(parent, title=f"Manage members - {list_info['name']}", size=(500, 500))

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.memberList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.memberList.InsertColumn(0, "Handle", width=220)
        self.memberList.InsertColumn(1, "Display name", width=220)
        self._renderMembers()
        sizer.Add(self.memberList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        if self._isOwner:
            removeRow = wx.BoxSizer(wx.HORIZONTAL)
            self.removeButton = wx.Button(self, label="Remove selected")
            removeRow.Add(self.removeButton)
            sizer.Add(removeRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            self.removeButton.Show(bool(self._members))

            addLabel = wx.StaticText(self, label="Add member (type a handle or name to search):")
            sizer.Add(addLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
            self.searchText = wx.TextCtrl(self)
            sizer.Add(self.searchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

            self.suggestLabel = wx.StaticText(self, label="Search results:")
            self.suggestionList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
            sizer.Add(self.suggestLabel, flag=wx.LEFT | wx.TOP, border=10)
            sizer.Add(self.suggestionList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            self.addButton = wx.Button(self, label="Add selected users")
            sizer.Add(self.addButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            # Nothing to show/add until an actual search has results.
            self.suggestLabel.Hide()
            self.suggestionList.Hide()
            self.addButton.Hide()
        else:
            note = wx.StaticText(self, label="You're not the creator of this list -- membership is read-only.")
            sizer.Add(note, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        closeBtn = wx.Button(self, label="&Close")
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        if self._isOwner:
            self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
            self.searchText.Bind(wx.EVT_TEXT, self.onSearchTextChanged)
            self.addButton.Bind(wx.EVT_BUTTON, self.onAddSuggestion)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        if self._members:
            self.memberList.Focus(0)
            self.memberList.Select(0)
        self.memberList.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        parent = self.GetParent()
        self.Destroy()
        if parent is not None:
            parent.manageMembersButton.SetFocus()

    def _renderMembers(self):
        self.memberList.Freeze()
        try:
            self.memberList.DeleteAllItems()
            for i, m in enumerate(self._members):
                self.memberList.InsertItem(i, f'@{m["handle"]}')
                self.memberList.SetItem(i, 1, m.get("display_name") or "")
        finally:
            self.memberList.Thaw()

    def onRemove(self, evt):
        index = self.memberList.GetFocusedItem()
        if not (0 <= index < len(self._members)):
            nvdaUi.message("No member selected.")
            return
        member = self._members[index]

        confirm = wx.MessageDialog(
            self, f'Remove @{member["handle"]} from this list?', "Confirm remove", wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_list_member(atprotoClient, member["listitem_uri"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, index, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, index, error):
        if error:
            nvdaUi.message(f"Could not remove member: {error}")
            return
        del self._members[index]
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self.Layout()
        if self._members:
            newIndex = min(index, len(self._members) - 1)
            self.memberList.Focus(newIndex)
            self.memberList.Select(newIndex)
            self.memberList.EnsureVisible(newIndex)
        nvdaUi.message("Member removed.")

    def onSearchTextChanged(self, evt):
        wx.CallLater(400, self._runSearch, self.searchText.GetValue())

    def _runSearch(self, query):
        if query != self.searchText.GetValue():
            return  # a newer keystroke already superseded this debounce
        if not query.strip():
            self.suggestionList.Set([])
            self._suggestions = []
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                results = client.search_actors_typeahead(atprotoClient, query)
                error = None
            except Exception as e:
                results = []
                error = str(e)
            wx.CallAfter(self._onSearchDone, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSearchDone(self, results, error):
        if error:
            return
        self._suggestions = results
        hasResults = bool(results)
        self.suggestLabel.Show(hasResults)
        self.suggestionList.Show(hasResults)
        self.addButton.Show(hasResults)
        self.Layout()
        self.suggestionList.Set([f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in results])
        self.suggestionList.CheckedItems = []
        if hasResults:
            self.suggestionList.SetSelection(0)

    def onAddSuggestion(self, evt):
        indices = list(self.suggestionList.CheckedItems)
        if not indices:
            nvdaUi.message("No suggestions checked.")
            return
        actors = [self._suggestions[i] for i in indices if 0 <= i < len(self._suggestions)]
        newActors = [a for a in actors if not any(m["did"] == a["did"] for m in self._members)]
        if not newActors:
            nvdaUi.message("Already in this list.")
            return

        def worker():
            added = []
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for actor in newActors:
                try:
                    listitem_uri = client.add_list_member(atprotoClient, self._listInfo["uri"], actor["did"])
                    added.append((actor, listitem_uri))
                except Exception as e:
                    errors.append(f'@{actor["handle"]}: {e}')
            wx.CallAfter(self._onAddDone, added, errors)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onAddDone(self, added, errors):
        for actor, listitem_uri in added:
            self._members.append({
                "listitem_uri": listitem_uri,
                "did": actor["did"],
                "handle": actor["handle"],
                "display_name": actor.get("display_name"),
            })
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self.Layout()
        if self._members:
            lastIndex = len(self._members) - 1
            self.memberList.Focus(lastIndex)
            self.memberList.Select(lastIndex)
            self.memberList.EnsureVisible(lastIndex)
        if added:
            names = ", ".join(f'@{a["handle"]}' for a, _ in added)
            nvdaUi.message(f"Added {names}.")
        if errors:
            nvdaUi.message("Some members could not be added: " + "; ".join(errors))


class ListsWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    TAB_KEY = "lists"
    # Same content shape as Home, so it gets the full shortcut set too
    # (see plan-09.md) -- Alt+number/Alt+U still branch on list purpose
    # via the _onAltNumber/_onAltU overrides below, independent of
    # these flags.
    SUPPORTS_NEW_POST = True
    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    SUPPORTS_JUMP_TO_USER = True

    """
    "My lists" -- a flat tree of every list this account created or
    subscribed to (mute/block), same set bsky.app's "My lists" page
    shows. Selecting a curation list shows its timeline on the right
    (a normal post list -- Post action/User action apply unchanged,
    feed_key is just the list's own at:// uri, so FeedListMixin/
    get_feed_page/check-for-updates all work exactly like Home/Saved).
    Selecting a moderation list has no timeline (modlists aren't a
    feed), so the right side swaps to a member roster instead.
    List-level management (create/delete, open in its own tab, edit
    membership, subscribe to someone else's moderation list) lives in
    this tab's own local toolbar, not the shared MainWindow one.
    """

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = "Lists"
        self._lists = []
        self._selectedList = None  # the dict for whichever tree row is selected
        self._feedKey = None
        self._initFeedListState()

        sizer = wx.BoxSizer(wx.VERTICAL)

        splitRow = wx.BoxSizer(wx.HORIZONTAL)

        self.listTree = wx.TreeCtrl(
            self, style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_SINGLE | wx.TR_LINES_AT_ROOT
        )
        self._listRoot = self.listTree.AddRoot("Lists")
        splitRow.Add(self.listTree, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=5)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildFeedListColumns()
        splitRow.Add(self.postList, proportion=2, flag=wx.EXPAND)

        self.memberList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.memberList.InsertColumn(0, "Handle", width=220)
        self.memberList.InsertColumn(1, "Display name", width=220)
        splitRow.Add(self.memberList, proportion=2, flag=wx.EXPAND)
        self.memberList.Hide()

        sizer.Add(splitRow, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.postActionButton = wx.Button(self, label="Post action... (Alt+A)")
        self.userActionButton = wx.Button(self, label="User action... (Alt+U)")
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing is focused/selectable at construction time yet --
        # _showSelectedList()/_updateActionButtons() re-show these once
        # a curation list with posts actually gets focused.
        self.postActionButton.Hide()
        self.userActionButton.Hide()

        toolbarRow = wx.BoxSizer(wx.HORIZONTAL)
        self.addListButton = wx.Button(self, label="Add list...")
        self.removeListButton = wx.Button(self, label="Remove list")
        self.showInNewTabButton = wx.Button(self, label="Show in new tab")
        self.manageMembersButton = wx.Button(self, label="Manage members...")
        self.subscribeButton = wx.Button(self, label="Find lists by user...")
        for button in (self.addListButton, self.removeListButton, self.showInNewTabButton, self.manageMembersButton, self.subscribeButton):
            button.Hide()
        #self.addListButton.Hide()
        # Remove/Show in new tab/Manage members moved into the list
        # tree's context menu (see onListContextMenu) -- mirrors Chat's
        # conversation-tree menu. "Add list..." moved to the shared
        # toolbar's New-post button (becomes "New list..." on this tab,
        # see mainWindow.py's onNewPost) -- kept as a real widget
        # (just hidden) rather than deleted so onAddList()'s existing
        # focus-restore calls (parent.addListButton.SetFocus()-style,
        # if any) don't need touching.
        toolbarRow.Add(self.subscribeButton, flag=wx.RIGHT, border=5)
        sizer.Add(toolbarRow, flag=wx.ALL, border=10)

# note: removeListButton/showInNewTabButton/manageMembersButton are intentionally left OUT of toolbarRow.Add() above -- only subscribeButton gets added now, the other three just aren't placed in a sizer (still constructed further down where .Bind() references them, but never shown).

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self.addListButton.Bind(wx.EVT_BUTTON, self.onAddList)
        self.removeListButton.Bind(wx.EVT_BUTTON, self.onRemoveList)
        self.showInNewTabButton.Bind(wx.EVT_BUTTON, self.onShowInNewTab)
        self.manageMembersButton.Bind(wx.EVT_BUTTON, self.onManageMembers)
        self.subscribeButton.Bind(wx.EVT_BUTTON, self.onSubscribeViaLink)
        self.listTree.Bind(wx.EVT_TREE_SEL_CHANGED, self.onListSelected)
        self.listTree.Bind(wx.EVT_TREE_ITEM_MENU, self.onListContextMenu)
        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onItemActivated)
        self.memberList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onMemberAction)
        self.memberList.Bind(wx.EVT_CONTEXT_MENU, self.onMemberContextMenu)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._loadListsFromCache()

        if self._account is None:
            nvdaUi.message("No active account. Log in from Settings first.")

    # ---------------- MainWindow integration hooks ----------------

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        self.listTree.SetFocus()

    def _restoreFocusPosition(self, moveFocus=True):
        # Lists behaves like Chat: the tree is this tab's "home"
        # control, not whichever list happens to be showing on the
        # right -- MainWindow's addTab()/_focusPanel() call this
        # expecting to land real focus somewhere sane on tab open, and
        # for this tab that's always the tree. The per-list post-
        # scroll-position restore (used when switching which list is
        # selected, see _showSelectedList below) is a separate concern
        # and calls FeedListMixin._restoreFocusPosition directly
        # instead of going through this override.
        if moveFocus:
            self.listTree.SetFocus()

    # ---------------- lists tree ----------------

    def _loadListsFromCache(self):
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot("Lists")
            self._lists = db.get_lists(self._account["id"]) if self._account else []

            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])

            firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
            if firstItem.IsOk():
                self.listTree.SelectItem(firstItem)
            else:
                self._selectedList = None
                self.postList.DeleteAllItems()
                self.memberList.DeleteAllItems()
                self.postActionButton.Hide()
                self.userActionButton.Hide()
                self.Layout()
        finally:
            self.listTree.Thaw()

        self._updateListsStatusBar()
        self._updateToolbarVisibility()
        # Deliberately NOT auto-syncing from the server here anymore.
        # This used to fire unconditionally on every __init__ (i.e.
        # every MainWindow open), unlike the other 4 permanent tabs
        # which only ever load from local cache at construction time
        # -- this was Stage 2 of the original crash-hardening plan
        # (plan-07.md), agreed on but never actually done; Stage 0's
        # safe_ui_callback fix made the resulting race SAFE at the
        # Python level (caught RuntimeError instead of a hard crash)
        # but never removed the race itself. Confirmed by testing
        # (see plan-09.md): every "quick close after opening
        # MainWindow" crash report so far shows this exact background
        # sync's completion handler as the immediately-preceding
        # event, every time, regardless of which tab the user actually
        # interacted with -- removing the automatic call here matches
        # ListsWindow's behavior to the other 4 tabs (cache-only on
        # open, sync only ever on explicit user action -- F5/Shift+F5/
        # after add-list/remove-list/subscribe, which still call
        # _syncListsFromServer() directly and are untouched by this).

    def _updateToolbarVisibility(self):
        # No-op now -- Remove/Show in new tab/Manage members live in
        # the list tree's context menu (see onListContextMenu), which
        # naturally only appears on an actual item, so there's nothing
        # left to show/hide here. Kept as a callable stub since it's
        # still called from a couple of places below.
        pass

    def onListContextMenu(self, evt):
        item = evt.GetItem()
        if not item.IsOk() or item == self._listRoot:
            return
        self.listTree.SelectItem(item)

        menu = wx.Menu()
        removeItem = menu.Append(wx.ID_ANY, "Remove list")
        openTabItem = menu.Append(wx.ID_ANY, "Show in new tab")
        membersItem = menu.Append(wx.ID_ANY, "Manage members...")
        self.Bind(wx.EVT_MENU, lambda e: self.onRemoveList(), removeItem)
        self.Bind(wx.EVT_MENU, lambda e: self.onShowInNewTab(), openTabItem)
        self.Bind(wx.EVT_MENU, lambda e: self.onManageMembers(), membersItem)

        self.PopupMenu(menu)
        menu.Destroy()

    def _listLabel(self, lst):
        kind = "moderation list" if lst["purpose"] == client.LIST_PURPOSE_MOD else "curation list"
        suffix = ", muted" if lst["purpose"] == client.LIST_PURPOSE_MOD and lst.get("muted") else ""
        return f'{lst["name"]} ({kind}{suffix})'

    def _updateListsStatusBar(self):
        self.statusBar.SetStatusText(f"Lists {len(self._lists)} total")

    def _syncListsFromServer(self):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                entries = client.get_lists(atprotoClient, self._account["did"])
                for entry in entries:
                    db.upsert_list({
                        "account_id": self._account["id"],
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
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onSyncListsDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSyncListsDone(self, error):
        if error:
            log.error(f"NVSky: list sync failed: {error}")
            nvdaUi.message(f"Could not refresh lists: {error}")
            return
        selectedUri = self._selectedList["list_uri"] if self._selectedList else None
        self._lists = db.get_lists(self._account["id"])
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot("Lists")
            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])

            item, cookie = self.listTree.GetFirstChild(self._listRoot)
            selected = False
            while item.IsOk():
                if self.listTree.GetItemData(item) == selectedUri:
                    self.listTree.SelectItem(item)
                    selected = True
                    break
                item, cookie = self.listTree.GetNextChild(self._listRoot, cookie)
            if not selected:
                firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
                if firstItem.IsOk():
                    self.listTree.SelectItem(firstItem)
                else:
                    self._selectedList = None
                    self.postList.DeleteAllItems()
                    self.memberList.DeleteAllItems()
                    self.postActionButton.Hide()
                    self.userActionButton.Hide()
                    self.Layout()
        finally:
            self.listTree.Thaw()
        self._updateListsStatusBar()
        self._updateToolbarVisibility()

    def onListSelected(self, evt):
        item = evt.GetItem()
        if item.IsOk() and item != self._listRoot:
            listUri = self.listTree.GetItemData(item)
            self._selectedList = next((l for l in self._lists if l["list_uri"] == listUri), None)
            self._showSelectedList()
        evt.Skip()

    def _showSelectedList(self):
        if self._selectedList is None:
            return
        if self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self.postList.Hide()
            self.memberList.Show()
            self.postActionButton.Hide()
            self.userActionButton.Hide()
            self.Layout()
            self._loadMembersLive()
        else:
            self.memberList.Hide()
            self.postList.Show()
            self.Layout()
            self._feedKey = self._selectedList["list_uri"]
            self._loadFromCache(reset=True)
            FeedListMixin._restoreFocusPosition(self, moveFocus=False)

    # ---------------- curation list timeline (FeedListMixin hooks) ----------------

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _describe_embed(post.get("embed_json")))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_list_feed(atprotoClient, self._account["id"], self._feedKey, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    # ---------------- moderation list members ----------------

    def _loadMembersLive(self):
        self.memberList.DeleteAllItems()
        nvdaUi.message("Loading members, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                info = client.get_list(atprotoClient, self._selectedList["list_uri"])
                error = None
            except Exception as e:
                info = None
                error = str(e)
            wx.CallAfter(self._onMembersLoaded, info, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onMembersLoaded(self, info, error):
        if error:
            nvdaUi.message(f"Could not load members: {error}")
            return
        self._currentMembers = info["members"]
        self.memberList.Freeze()
        try:
            self.memberList.DeleteAllItems()
            for i, m in enumerate(self._currentMembers):
                self.memberList.InsertItem(i, f'@{m["handle"]}')
                self.memberList.SetItem(i, 1, m.get("display_name") or "")
        finally:
            self.memberList.Thaw()
        self.statusBar.SetStatusText(f"{self._selectedList['name']} {len(self._currentMembers)} members")
        if self._currentMembers:
            self.memberList.Focus(0)
            self.memberList.Select(0)

    def _getFocusedMember(self):
        index = self.memberList.GetFocusedItem()
        if 0 <= index < len(self._currentMembers):
            return self._currentMembers[index]
        return None

    def onMemberAction(self, evt=None):
        member = self._getFocusedMember()
        if member is None:
            nvdaUi.message("No member selected.")
            return
        self.showUserActionMenu(member["did"], member["handle"], member.get("display_name"))

    def onMemberContextMenu(self, evt):
        self.onMemberAction()

    # ---------------- toolbar actions ----------------

    def onAddList(self, evt=None):
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        gui.mainFrame.prePopup()
        dlg = AddListDialog(self)
        result = dlg.ShowModal()
        name, description, purpose = dlg.getValues()
        dlg.Destroy()
        gui.mainFrame.postPopup()
        self.addListButton.SetFocus()
        if result != wx.ID_OK:
            return
        if not name:
            nvdaUi.message("A list needs a name.")
            return
        self._createList(name, description, purpose)

    def _createList(self, name, description, purpose):
        nvdaUi.message("Creating list, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.create_list(atprotoClient, name, description, purpose)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onCreateListDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCreateListDone(self, error):
        if error:
            nvdaUi.message(f"Could not create list: {error}")
            return
        nvdaUi.message("List created.")
        self._syncListsFromServer()

    def onRemoveList(self, evt=None):
        if self._selectedList is None:
            nvdaUi.message("No list selected.")
            return
        if self._selectedList["creator_did"] != self._account["did"]:
            nvdaUi.message("You can only remove a list you created -- use Find lists by user's Mute/Block to leave someone else's list.")
            return

        confirm = wx.MessageDialog(
            self, f'Remove the list "{self._selectedList["name"]}"? This can\'t be undone.',
            "Confirm remove", wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        listUri = self._selectedList["list_uri"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.delete_list(atprotoClient, listUri)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveListDone, listUri, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveListDone(self, listUri, error):
        if error:
            nvdaUi.message(f"Could not remove list: {error}")
            return
        db.delete_list(self._account["id"], listUri)
        nvdaUi.message("List removed.")
        self._selectedList = None
        self._loadListsFromCache()

    def onShowInNewTab(self, evt=None):
        if self._selectedList is None:
            nvdaUi.message("No list selected.")
            return
        if self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            nvdaUi.message("Moderation lists don't have a timeline to open in a tab.")
            return
        mainWindow = self.GetTopLevelParent()
        tab = ListTabWindow(
            mainWindow.notebook, self._selectedList["list_uri"], self._selectedList["name"], origin_key="lists"
        )
        mainWindow.addTab(tab, self._selectedList["name"], select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "list",
            "key": self._selectedList["list_uri"],
            "list_uri": self._selectedList["list_uri"],
            "list_name": self._selectedList["name"],
            "origin_key": "lists",
        })

    def onManageMembers(self, evt=None):
        if self._selectedList is None:
            nvdaUi.message("No list selected.")
            return
        nvdaUi.message("Loading members, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                info = client.get_list(atprotoClient, self._selectedList["list_uri"])
                error = None
            except Exception as e:
                info = None
                error = str(e)
            wx.CallAfter(self._onManageMembersInfoReady, info, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onManageMembersInfoReady(self, info, error):
        if error:
            nvdaUi.message(f"Could not load list: {error}")
            return
        gui.mainFrame.prePopup()
        dlg = ManageMembersDialog(self, info)
        dlg.Show()

    def onSubscribeViaLink(self, evt=None):
        gui.mainFrame.prePopup()
        dlg = SubscribeListDialog(self, on_subscribed=self._syncListsFromServer)
        dlg.Show()

    # ---------------- keyboard ----------------

    # onCharHook is inherited from FeedListMixin -- Alt+number/Alt+U
    # stay class-specific here since they branch on list purpose.

    def _onAltNumber(self, n):
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            self._announceNthNewestPost(n)

    def _onAltU(self):
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self.onMemberAction()
        else:
            self.onUserAction()
        
    def _syncForBulkCheck(self, atprotoClient):
        # Was calling self._syncListsFromServer(), which spawns its OWN
        # separate background thread and its OWN separate
        # client.get_client_for_active_account() login -- called from
        # INSIDE the bulk-check flow's already-shared background thread,
        # this raced a second concurrent login against the one
        # checkAllOpenTabs already holds, exactly the class of bug the
        # shared-thread design was meant to avoid (see MainWindow.
        # checkAllOpenTabs' own comment). Also fire-and-forget, so this
        # method's return value never reflected whether anything
        # actually changed -- confirmed as the cause of "Lists doesn't
        # announce AND doesn't update" during Ctrl+F5. Do the list-sync
        # work directly here instead, synchronously, on the SAME
        # atprotoClient/thread the caller already established.
        beforeSnapshot = {l["list_uri"]: l.get("muted") for l in self._lists}
        entries = client.get_lists(atprotoClient, self._account["did"])
        for entry in entries:
            db.upsert_list({
                "account_id": self._account["id"],
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
        afterLists = db.get_lists(self._account["id"])
        afterSnapshot = {l["list_uri"]: l.get("muted") for l in afterLists}
        listsChanged = afterSnapshot != beforeSnapshot

        curateChanged = False
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            curateChanged = super()._syncForBulkCheck(atprotoClient)

        return listsChanged or curateChanged

    def _reloadAfterBulkCheck(self, moveFocus=True):
        # _syncForBulkCheck above already wrote fresh list metadata to
        # the DB synchronously -- rebuild the tree from it here instead
        # of leaving it stale until the next explicit F5 (which still
        # goes through the async _syncListsFromServer/_onSyncListsDone
        # path unchanged).
        selectedUri = self._selectedList["list_uri"] if self._selectedList else None
        self._lists = db.get_lists(self._account["id"])
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot("Lists")
            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])
            item, cookie = self.listTree.GetFirstChild(self._listRoot)
            selected = False
            while item.IsOk():
                if self.listTree.GetItemData(item) == selectedUri:
                    self.listTree.SelectItem(item)
                    selected = True
                    break
                item, cookie = self.listTree.GetNextChild(self._listRoot, cookie)
            if not selected:
                firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
                if firstItem.IsOk():
                    self.listTree.SelectItem(firstItem)
        finally:
            self.listTree.Thaw()
        self._updateListsStatusBar()

        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            super()._reloadAfterBulkCheck(moveFocus=moveFocus)
        elif self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self._loadMembersLive()

    def onCheckForUpdates(self, evt):
        self._syncListsFromServer()
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            super().onCheckForUpdates(evt)
        elif self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self._loadMembersLive()


class ListTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    A single curation list's timeline, popped out into its own
    removable tab via Lists' "Show in new tab" -- structurally a clone
    of SavedWindow fixed to one list_uri instead of a filter choice,
    so it can stay open and be checked for updates independently of
    the main Lists tab.
    """

    def __init__(self, parent, list_uri: str, list_name: str, origin_key: str = None):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = list_name
        # Generic identity for MainWindow's remember-last-tab feature.
        self.TAB_TEMP_TYPE = "list"
        self.TAB_TEMP_KEY = list_uri
        self._feedKey = list_uri
        self._originTabKey = origin_key
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit(sync_if_empty=True)

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            nvdaUi.message(f"{self.TAB_NAME} tab")
            self._restoreFocusPosition()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _describe_embed(post.get("embed_json")))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_list_feed(atprotoClient, self._account["id"], self._feedKey, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            nvdaUi.message("No post selected.")
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    def onTabRemoved(self):
        # MainWindow.removeCurrentTab() calls this (if present) right
        # before DeletePage() -- see the mainWindow.py edit -- so a
        # temp tab the user closes with Ctrl+W doesn't come back next
        # time NVSky opens. Scans for every wx.Timer instance rather
        # than calling _stopTimeRefreshTimer() by name (see plan-09.md
        # -- MainWindow.onClose had the identical gap: only knew about
        # _timeRefreshTimer and missed _loadingTimer, the F5-in-progress
        # beep, which this tab can also start via onCheckForUpdates).
        for value in vars(self).values():
            if isinstance(value, wx.Timer):
                value.Stop()
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "list", self._feedKey)
        self._jumpBackToOrigin()

    def onTabRenamed(self, newName):
        # MainWindow.renameCurrentTab() calls this (if present) right
        # after updating panel.TAB_NAME in memory -- persists the new
        # name into this tab's existing db.get_open_temp_tabs() entry
        # so it survives past this session (previously session-only).
        # Overrides RemovableTabMixin's no-op stub -- ListTabWindow IS
        # user-renameable, unlike the other RemovableTabMixin hosts.
        if self._account is not None:
            db.set_temp_tab_custom_name(self._account["id"], "list", self._feedKey, newName)
    
    # onCharHook is inherited from FeedListMixin -- no SUPPORTS_* flags
    # needed here, this class never had Space/Ctrl+A/Left-Right/Ctrl+N.

def _describe_notification(notif: dict) -> str:
    reason = notif.get("reason")
    subjectText = (notif.get("subject_text") or "").replace("\n", " ").strip()
    label = {
        "like": "Liked",
        "repost": "Reposted",
        "follow": "Followed you",
        "reply": "Replied",
        "mention": "Mentioned you",
        "quote": "Quoted",
    }.get(reason, reason or "Notification")

    if reason == "follow":
        return label
    # Always show the original message when we have one -- the user
    # shouldn't have to open the website to see what a like/repost/
    # reply/mention/quote was actually about.
    if subjectText:
        return f"{label}: {subjectText}"
    return label


class NotificationsWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    TAB_KEY = "notifications"
    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    TIME_COLUMN_INDEX = 2  # Author(0)/Notification(1)/Received(2) -- only 3 columns, not the usual 4

    """
    Notifications list -- likes, reposts, follows, replies, mentions,
    quotes. Reuses FeedListMixin the same way FeedWindow does, but
    backed by its own `notifications` table instead of posts/
    feed_items, since a notification isn't shaped like a post.

    Permanent tab embedded in MainWindow (same category as Home) --
    no per-panel Close button/Escape/Ctrl+W handling, same as
    FeedWindow's conversion.
    """

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = "Notifications"
        self._feedKey = "notifications"
        self._initFeedListState()

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.postList.InsertColumn(0, "Author", width=180)
        self.postList.InsertColumn(1, "Notification", width=400)
        self.postList.InsertColumn(2, "Received", width=140)
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.postActionButton = wx.Button(self, label="Post action... (Alt+A)")
        self.userActionButton = wx.Button(self, label="User action... (Alt+U)")
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Check for updates used to have its own button here -- now a
        # toolbar-level button shared across every tab in MainWindow
        # instead (see mainWindow.py). F5 still works as a keyboard
        # shortcut while this tab has focus, via onCharHook below.
        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._loadFromCache(reset=True)

        if self._account is None:
            nvdaUi.message("No active account. Log in from Settings first.")
        else:
            # moveFocus=False -- see the matching comment in
            # FeedWindow.__init__ for why (this panel gets constructed
            # BEFORE MainWindow.addTab() adds it, so grabbing real
            # focus here would steal it from whichever tab is actually
            # meant to be visible).
            self._restoreFocusPosition(moveFocus=False)

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            nvdaUi.message(f"{self.TAB_NAME} tab")
            self._restoreFocusPosition()

    def _insertRow(self, index: int, notif: dict, mode: str):
        self.postList.InsertItem(index, self._authorLabel(notif, mode))
        self.postList.SetItem(index, 1, _describe_notification(notif))
        self.postList.SetItem(index, 2, _format_post_time(notif.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_notification_page(self._account["id"], before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_notification_count(self._account["id"])

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_notifications(atprotoClient, self._account["id"], cursor=cursor, limit=limit)

    def _markItemRead(self, notif):
        db.mark_notification_read(notif["uri"])

    def onUserAction(self, evt=None):
        notif = self._getFocusedPost()
        if notif is None:
            nvdaUi.message("No notification selected.")
            return
        self.showUserActionMenu(notif["author_did"], notif["handle"], notif.get("display_name"))

    # ---------------- post action menu (Alt+A) ----------------

    def _getActionablePost(self):
        notif = self._getFocusedPost()
        if notif is None:
            nvdaUi.message("No notification selected.")
            return None

        subjectUri = notif.get("subject_uri")
        if not subjectUri:
            if notif.get("reason") == "follow":
                nvdaUi.message("Follow notifications have no post -- try User action (Alt+U) instead.")
            else:
                nvdaUi.message("This notification's post isn't available -- try Check for updates (F5) to resync.")
            return None

        post = db.get_post(subjectUri)
        if post is None:
            nvdaUi.message("This notification's post isn't cached yet -- try Check for updates (F5) to resync.")
            return None
        return post

    def _showBulkPostActionMenu(self, selectedCount):
        # Bulk reply/like/repost/etc. on several notifications' underlying
        # posts still isn't coherent (that part stays single-item-only,
        # via _getActionablePost above) -- but bulk mark read/unread IS
        # meaningful: it marks the NOTIFICATIONS themselves, the same
        # thing single-item read-tracking already does elsewhere in this
        # tab, not their underlying posts.
        menu = wx.Menu()
        markMenu = wx.Menu()
        self._addMenuItem(markMenu, f"Read ({selectedCount} selected)",
                           lambda: self._markSelectedNotificationsRead(True))
        self._addMenuItem(markMenu, f"Unread ({selectedCount} selected)",
                           lambda: self._markSelectedNotificationsRead(False))
        menu.AppendSubMenu(markMenu, "Mar&k as...")
        self.PopupMenu(menu)
        menu.Destroy()

    def _markSelectedNotificationsRead(self, read: bool):
        notifs = self._getSelectedPosts()
        for notif in notifs:
            if read:
                db.mark_notification_read(notif["uri"])
            else:
                db.mark_notification_unread(notif["uri"])
        # Reload from DB rather than patch in-memory state -- avoids
        # guessing the notification dict's exact read-status key name,
        # and guarantees the status bar's unread count matches reality.
        self._loadFromCache(reset=True)
        _announce_now("Marked as read." if read else "Marked as unread.")

    # onCharHook is inherited from FeedListMixin (see SUPPORTS_* flags
    # above). This also fixes a real bug: this class's old onCharHook
    # never had the "Ctrl+F5 bubbles up to MainWindow" guard the other
    # 4 tabs have, so Ctrl+F5 here was silently doing a single-tab F5
    # sync instead of MainWindow.checkAllOpenTabs' full sweep.

