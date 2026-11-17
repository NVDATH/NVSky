"""
Shared feed-list machinery and the Home tab for NVSky.

Holds RemovableTabMixin, FeedListMixin (the cache-first paginated post
list behind every post-list tab) and FeedWindow (Home). Pure post/embed
text helpers live in postText.py.
"""
import json
import wx
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import timeutils
from . import uiutil
from . import soundpack
from . import keymap
from .compose import ComposeDialog
from .userActions import UserActionMixin
from .itemActions import ItemActionMixin
from .embedView import EmbedViewMixin
from .postText import (
    COLUMN_DISPLAY_NAME,
    _compose_copy_text,
    _embed_sound_event,
    _format_post_time,
    _full_post_text,
    _label_visibility,
    _message_text,
    _post_detail_rows,
    _post_web_url,
    _visible_embed_text,
    _visible_message_text,
)

PAGE_SIZE = 50
MAX_NETWORK_PAGE_WALK = 5

# Translators: Permanent tab label for the Home feed.
TAB_NAME = _("Home")

# Home's built-in feed keys are "home" and "discover"; a custom feed's key is its uri.


class _VirtualPostList(wx.ListCtrl):
    """A report list that holds no rows itself: the host supplies a cell's text when it is drawn or read."""

    def __init__(self, parent, cellText):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self._cellText = cellText

    def OnGetItemText(self, item, col):
        return self._cellText(item, col)

    def DeleteAllItems(self):
        self.SetItemCount(0)
        return True


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
        self._cellCache = {}  # virtual list only: row index -> cell texts, built when first asked for
        self._cellMode = COLUMN_DISPLAY_NAME
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
    # True: the post list is a _VirtualPostList (needs _rowCells for the host's row layout).
    VIRTUAL_LIST = False

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
        if self.VIRTUAL_LIST:
            for index, cells in self._cellCache.items():
                if index < len(self._posts) and len(cells) > self.TIME_COLUMN_INDEX:
                    cells[self.TIME_COLUMN_INDEX] = _format_post_time(self._posts[index].get("indexed_at"))
            count = min(len(self._posts), self.postList.GetItemCount())
            if count:
                self.postList.RefreshItems(0, count - 1)
            return
        for i, post in enumerate(self._posts):
            self.postList.SetItem(i, self.TIME_COLUMN_INDEX, _format_post_time(post.get("indexed_at")))

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
        Standard feed-tab layout: optional extra_top row above postList, postList, Post action/User
        action buttons (+ optional extra_action_widgets after them), statusBar. Calls
        self.SetSizer(). extra_top: a wx.Sizer above postList (e.g. FeedWindow's filter-choice row).
        extra_action_widgets: wx.Window objects added to the action row after userActionButton (e.g.
        "Add to my feeds").
        """
        sizer = wx.BoxSizer(wx.VERTICAL)

        if extra_top is not None:
            sizer.Add(extra_top, flag=wx.ALL, border=10)

        if self.VIRTUAL_LIST:
            self.postList = _VirtualPostList(self, self._cellAt)
        else:
            self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildFeedListColumns()
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. The current shortcut is appended automatically.
        self.postActionButton = wx.Button(self, label=_("&Post action...") + keymap.hint("post_action"))
        # Translators: Button to open the User action menu. The current shortcut is appended automatically.
        self.userActionButton = wx.Button(self, label=_("&User action...") + keymap.hint("user_action"))
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

    def _rowCells(self, post: dict, mode: str):
        # Default row layout (Embed/Author/Message/Posted). A host with its own _insertRow needs its own _rowCells to go virtual.
        return [
            _visible_embed_text(post),
            self._authorLabel(post, mode),
            _visible_message_text(post),
            _format_post_time(post.get("indexed_at")),
        ]

    def _insertRow(self, index: int, post: dict, mode: str):
        # NotificationsWindow overrides it.
        cells = self._rowCells(post, mode)
        self.postList.InsertItem(index, cells[0])
        for col in range(1, len(cells)):
            self.postList.SetItem(index, col, cells[col])

    def _cellsAt(self, index):
        cells = self._cellCache.get(index)
        if cells is None:
            cells = self._cellCache[index] = self._rowCells(self._posts[index], self._cellMode)
        return cells

    def _cellAt(self, index, col):
        # The virtual list calls this for every cell it draws or is asked to read.
        if not 0 <= index < len(self._posts):
            return ""
        cells = self._cellsAt(index)
        return cells[col] if col < len(cells) else ""

    def _rowParts(self, index):
        if self.VIRTUAL_LIST:
            return list(self._cellsAt(index))
        return [self.postList.GetItemText(index, col) for col in range(self.postList.GetColumnCount())]

    def _clearSelection(self, keep=None):
        selected = []
        i = self.postList.GetFirstSelected()
        while i != -1:
            selected.append(i)
            i = self.postList.GetNextSelected(i)
        for i in selected:
            if i != keep:
                self.postList.SetItemState(i, 0, wx.LIST_STATE_SELECTED)

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
            if self.VIRTUAL_LIST:
                self._cellMode = mode
                self._cellCache = {}
                self._clearSelection()
                self.postList.SetItemCount(len(self._posts))
                if self._posts:
                    self.postList.RefreshItems(0, len(self._posts) - 1)
            else:
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
        # Disable() too, not just Hide(): a hidden but enabled button still fires its mnemonic
        # (Alt+A/Alt+U), a silent wrong-target trigger on an empty list.
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
            nvdaUi.message(", ".join(p for p in self._rowParts(index) if p))
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

    @uiutil.safe_ui_callback
    def _applyFocusPosition(self, targetIndex):
        self._suppressFocusEvents = True
        try:
            if self._posts:
                # Select() only adds; clear other selections so Alt+A/Alt+U
                # don't see a multi-selection.
                self._clearSelection(keep=targetIndex)
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
                    nvdaUi.message(", ".join(p for p in self._rowParts(i) if p))
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

    def _removePostFromList(self, post):
        """Drops `post` from the visible list and focuses the row that took its place. Returns its old index, or None."""
        for i, p in enumerate(self._posts):
            if p["uri"] == post["uri"]:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                return i
        return None


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

        uiutil.start_worker(worker, progress=False)

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
            nvdaUi.message(_("No older {} posts found nearby -- load older posts again to keep looking back.").format(self.TAB_NAME))
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

        uiutil.start_worker(worker, progress=False)

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

        uiutil.start_worker(worker, progress=False)

    @uiutil.safe_ui_callback
    def _onCheckForUpdatesDone(self, error):
        self._checkingUpdates = False
        self._stopLoadingBeep()
        if error:
            log.error(f"NVSky: check for updates failed: {error}")
            # Translators: Announced when checking a feed for updates fails. {} is the error message.
            nvdaUi.message(_("Check for updates failed: {}").format(error))
            return

        # A pinned post (user timelines) always sits first and is never "new".
        oldTopUri = next((p["uri"] for p in self._posts if not p.get("_pinned")), None)
        self._loadFromCache(reset=True)
        self._restoreFocusPosition()

        regular = [p for p in self._posts if not p.get("_pinned")]
        if oldTopUri is None:
            newCount = len(regular)
        else:
            newCount = 0
            for post in regular:
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
        inList = self.FindFocus() is self.postList

        if keyCode in (wx.WXK_UP, wx.WXK_DOWN) and not evt.HasAnyModifiers() and inList:
            self._checkListBoundaryBeforeKey(keyCode)
        if keymap.matches(evt, "clear_cache"):
            self.onClearCache()
            return
        if keymap.matches(evt, "delete_post") and inList:
            self.onDeletePostShortcut()
            return
        if keymap.matches(evt, "check_all"):
            evt.Skip()  # bubble up to MainWindow's checkAllOpenTabs
            return
        if keymap.matches(evt, "load_older"):
            self.onFetchPreviousPosts(None)
            return
        if keymap.matches(evt, "check_updates"):
            self.onCheckForUpdates(None)
            return
        if keymap.matches(evt, "new_item") and getattr(self, "SUPPORTS_NEW_POST", False):
            self.onNewPost(None)
            return
        if keymap.matches(evt, "post_action"):
            self.onPostAction()
            return
        if evt.AltDown() and ord("1") <= keyCode <= ord("9"):
            self._onAltNumber(keyCode - ord("0"))
            return
        if keymap.matches(evt, "user_action"):
            self._onAltU()
            return
        if keymap.matches(evt, "copy_row"):
            focused = self.FindFocus()
            if focused is self.postList:
                self._copyFocusedPostRow()
                return
            if isinstance(focused, (wx.ListCtrl, wx.TreeCtrl)) and uiutil.copy_focused_row(focused):
                return
        if keymap.matches(evt, "jump_row"):
            focused = self.FindFocus()
            if isinstance(focused, wx.ListCtrl):
                rowText = None
                if self.VIRTUAL_LIST and focused is self.postList:
                    rowText = lambda index: ", ".join(p for p in self._rowParts(index) if p)
                uiutil.jump_to_row(self, focused, row_text=rowText)
                return
        if keymap.matches(evt, "reveal_warning") and inList:
            self._showFullPost()
            return
        if keymap.matches(evt, "next_unread") and getattr(self, "SUPPORTS_FOCUS_NEXT_UNREAD", False) and inList:
            self._focusNextUnread()
            return
        if keymap.matches(evt, "select_all") and getattr(self, "SUPPORTS_SELECT_ALL", False) and inList:
            self._selectAllPosts()
            return
        if keymap.matches(evt, "jump_user_previous") and getattr(self, "SUPPORTS_JUMP_TO_USER", False) and inList:
            self._jumpToUserPost(-1)
            return
        if keymap.matches(evt, "jump_user_next") and getattr(self, "SUPPORTS_JUMP_TO_USER", False) and inList:
            self._jumpToUserPost(1)
            return
        if inList and callable(getattr(self, "runCustomShortcut", None)) and self.runCustomShortcut(evt, self._getFocusedPost()):
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

    def _fullPostContent(self, post):
        """(title, text, post whose details are listed) for the full-post dialog."""
        # Translators: Title of the full-post dialog. {} is the post's author.
        title = _("Post from {}").format(self._authorLabel(post, self._currentAuthorMode()))
        return title, _full_post_text(post), post

    def _showFullPost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        from . import postDetails
        title, text, detailsPost = self._fullPostContent(post)
        rows = _post_detail_rows(detailsPost) if detailsPost else None
        postDetails.show_post_details(self, title, text, rows, detailsPost)

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
    # Shared so any host can opt in via SUPPORTS_JUMP_TO_USER (only self._posts/self.postList are
    # used).

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
                    self._clearSelection(keep=i)
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
    VIRTUAL_LIST = True
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
                # feed_key is the raw feed uri, matching how sync_feed_generator_page keys its DB
                # writes (a prefix would read a different cache entry).
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
        # Show the cache at once; only sync when this feed has no cache yet.
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
        self._removePostFromList(post)

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
