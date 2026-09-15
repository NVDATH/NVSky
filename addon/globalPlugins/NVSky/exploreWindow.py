"""
Explore tab and starter-pack details dialog for NVSky.

Split out of feedWindow.py (see plan-17.md). Posts result type reuses
FeedListMixin fully; People/Starter packs/Feeds are simpler dedicated
lists swapped in via Show/Hide.
"""
import threading
import webbrowser
import wx

import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from .feedWindow import (
    FeedListMixin,
    ItemActionMixin,
    UserActionMixin,
    UserListMixin,
    EmbedViewMixin,
    _search_feed_key,
)
from .feedTabs import FeedPreviewTabWindow, UserListTabWindow


class StarterPackDetailsDialog(wx.Dialog):
    """Read-only starter pack info, plus the same actions available
    from the results-list context menu -- so the user doesn't have to
    close this and reopen the menu separately."""

    def __init__(self, parent, pack, full):
        self._pack = pack
        record = getattr(full, "record", None)
        # Translators: Fallback starter-pack name when none is set.
        title = getattr(record, "name", None) or _("Starter pack")
        # Translators: Title of the starter-pack details dialog. {} is the pack's name.
        super().__init__(parent, title=_("{} - Starter pack").format(title), size=(500, 420))

        profiles = getattr(full, "list_items_sample", None) or []
        lines = [
            # Translators: Starter-pack field label.
            _("Name: {}").format(title),
            # Translators: Starter-pack field label.
            _("Creator: @{}").format(full.creator.handle),
            # Translators: Starter-pack field label.
            _("Description: {}").format(getattr(record, 'description', '') or _("(none)")),
            # Translators: Starter-pack field label.
            _("Members: {}").format(len(profiles)),
        ]
        if profiles:
            lines.append("")
            # Translators: Header above the list of people included in a starter pack.
            lines.append(_("People included:"))
            lines.extend(f"  @{item.subject.handle}" for item in profiles)
        feeds = getattr(full, "feeds", None) or []
        if feeds:
            lines.append("")
            # Translators: Header above the list of feeds included in a starter pack.
            lines.append(_("Feeds included:"))
            lines.extend(f"  {f.display_name}" for f in feeds)

        sizer = wx.BoxSizer(wx.VERTICAL)
        textCtrl = wx.TextCtrl(self, value="\n".join(lines), style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to follow every account included in a starter pack.
        followBtn = wx.Button(self, label=_("&Follow everyone in this pack"))
        # Translators: Button to open a starter pack's page in a web browser.
        openBtn = wx.Button(self, label=_("&Open on bsky.app"))
        # Translators: Button to close the starter-pack details dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
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
    # Internal comparison keys -- never translated, RESULT_TYPES (the
    # translated display labels shown in typeRadio) must stay in this
    # same order. _currentType() returns from this list, not the
    # display one, so every == "posts" comparison below keeps working
    # regardless of UI language.
    RESULT_TYPE_KEYS = ["posts", "people", "starter_packs", "feeds"]
    RESULT_TYPES = [
        # Translators: Explore result-type radio choice.
        _("Posts"),
        # Translators: Explore result-type radio choice.
        _("People"),
        # Translators: Explore result-type radio choice.
        _("Starter packs"),
        # Translators: Explore result-type radio choice.
        _("Feeds"),
    ]
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
        # Translators: Label for Explore's search field.
        searchLabel = wx.StaticText(self, label=_("&Search:"))
        self.searchText = wx.TextCtrl(self)
        searchRow.Add(searchLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        searchRow.Add(self.searchText, proportion=1)
        sizer.Add(searchRow, flag=wx.EXPAND | wx.ALL, border=10)

        self.typeRadio = wx.RadioBox(
            # Translators: Label for the Explore result-type radio group.
            self, label=_("Result type"), choices=self.RESULT_TYPES, majorDimension=1, style=wx.RA_SPECIFY_ROWS
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
        # Translators: Label for the advanced-search "from handle" field.
        self.advFromText = self._addAdvField(self.advPanel, advSizer, _("&From handle:"))
        # Translators: Label for the advanced-search "since date" field.
        self.advSinceText = self._addAdvField(self.advPanel, advSizer, _("S&ince (YYYY-MM-DD):"))
        # Translators: Label for the advanced-search "until date" field.
        self.advUntilText = self._addAdvField(self.advPanel, advSizer, _("&Until (YYYY-MM-DD):"))
        # Translators: Label for the advanced-search "language" field.
        self.advLangText = self._addAdvField(self.advPanel, advSizer, _("&Language:"))
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
        # Translators: Column header for a starter pack's name.
        self.starterPacksList.InsertColumn(0, _("Name"), width=200)
        # Translators: Column header for a starter pack's creator.
        self.starterPacksList.InsertColumn(1, _("Creator"), width=180)
        # Translators: Column header for a starter pack's description.
        self.starterPacksList.InsertColumn(2, _("Description"), width=300)
        sizer.Add(self.starterPacksList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        self.starterPacksList.Hide()

        self.feedsResultList = wx.ListCtrl(self, style=wx.LC_REPORT)
        # Translators: Column header for a feed's name.
        self.feedsResultList.InsertColumn(0, _("Name"), width=200)
        # Translators: Column header for a feed's creator.
        self.feedsResultList.InsertColumn(1, _("Creator"), width=180)
        # Translators: Column header for a feed's description.
        self.feedsResultList.InsertColumn(2, _("Description"), width=250)
        # Translators: Column header for a feed's like count.
        self.feedsResultList.InsertColumn(3, _("Likes"), width=80)
        sizer.Add(self.feedsResultList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
        self.feedsResultList.Hide()

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. Shows the Alt+A shortcut.
        self.postActionButton = wx.Button(self, label=_("&Post action... (Alt+A)"))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        self.resultActionButton = wx.Button(self, label=_("&Action..."))
        # Translators: Button to open a search/user-list result in its own removable tab.
        self.openInTabButton = wx.Button(self, label=_("Open in new &tab"))
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
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
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
        # Translators: Advanced-search toggle button label, showing its current state. {} is "expanded" or "collapsed".
        # Translators: State word used in "Advanced search ({})".
        # Translators: State word used in "Advanced search ({})".
        status = _("expanded") if expanded else _("collapsed")
        self.advBtn.SetLabel(_("Ad&vanced search ({})").format(status))
        if layout:
            self.Layout()

    def onAdvBtnClick(self, evt):
        self._setAdvExpanded(not self._advExpanded)
        if self._advExpanded:
            self.advFromText.SetFocus()

    def onToggleAdvancedAccel(self, evt):
        if self._currentType() != "posts":
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
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.searchText.SetFocus()

    def onCheckForUpdates(self, evt):
        self._runSearch()

    def onClearCache(self, evt=None):
        # Nothing persistent to clear here -- People/Starter packs/
        # Feeds always fetch fresh, and Posts search results are
        # already ephemeral per-query.
        # Translators: Announced when clearing cache in Explore, which has nothing persistent to clear.
        nvdaUi.message(_("Nothing to clear here."))

    # ---------------- FeedListMixin contract -- unused, search bypasses the cache path ----------------

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        if not self._feedKey:
            return []
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        if not self._sourceQuery:
            return 0
        return db.get_unread_count(self._account["id"], self._feedKey)

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
        if self._currentType() == "posts":
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
        return self.RESULT_TYPE_KEYS[self.typeRadio.GetSelection()]

    def onCharHook(self, evt):
        # Alt+A/Alt+U for non-Posts result types needs to route to
        # onResultAction/onPeopleContextMenu instead of the generic
        # ItemActionMixin.onCharHook's onPostAction()/onUserAction() --
        # those act on self.postList's focused post regardless of
        # which result list actually has focus, so Alt+A on a stale
        # Posts search's leftover focused post was firing instead of
        # the Feeds/Starter packs/People action shown on the "Action..."
        # button's own label. Posts stays on the normal mixin path.
        if self._currentType() != "posts":
            keyCode = evt.GetKeyCode()
            if evt.AltDown() and keyCode == ord("A"):
                self.onResultAction()
                return
            if evt.AltDown() and keyCode == ord("U") and self._currentType() == "people":
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
            "people": (self.peopleList, self._users),
            "starter_packs": (self.starterPacksList, self._starterPacks),
            "feeds": (self.feedsResultList, self._feeds),
        }[self._currentType()]
        if not (1 <= n <= len(data)):
            # Translators: Announced when Alt+number is pressed for a result index that doesn't exist. {} is the number.
            nvdaUi.message(_("No item {}.").format(n))
            return
        index = n - 1
        parts = [listCtrl.GetItemText(index, col) for col in range(listCtrl.GetColumnCount())]
        _announce_now(", ".join(p for p in parts if p))

    def _showResultListForType(self):
        resultType = self._currentType()
        self.postList.Show(resultType == "posts")
        self.peopleList.Show(resultType == "people")
        self.starterPacksList.Show(resultType == "starter_packs")
        self.feedsResultList.Show(resultType == "feeds")
        self.advBtn.Show(resultType == "posts")
        if resultType != "posts":
            self._setAdvExpanded(False, layout=False)
        self._updateActionButtons()

    def _userListLabel(self):
        # Translators: Status text after a people search. {} is the count.
        return _("{} people found.").format(len(self._users))

    def _updateActionButtons(self):
        resultType = self._currentType()
        counts = {"posts": len(self._posts), "people": len(self._users),
                  "starter_packs": len(self._starterPacks), "feeds": len(self._feeds)}
        hasResults = counts.get(resultType, 0) > 0
        self.postActionButton.Show(resultType == "posts" and hasResults)
        self.userActionButton.Show(resultType == "posts" and hasResults)
        self.resultActionButton.Show(resultType != "posts" and hasResults)
        self.openInTabButton.Show(resultType in ("posts", "people") and hasResults)
        labels = {
            # Translators: Button label when result type is People. Shows the Alt+U shortcut.
            "people": _("User action... (Alt+U)"),
            # Translators: Button label when result type is Starter packs. Shows the Alt+A shortcut.
            "starter_packs": _("Pack action... (Alt+A)"),
            # Translators: Button label when result type is Feeds. Shows the Alt+A shortcut.
            "feeds": _("Feed action... (Alt+A)"),
        }
        if resultType in labels:
            self.resultActionButton.SetLabel(labels[resultType])
        self.Layout()

    def onResultAction(self, evt=None):
        resultType = self._currentType()
        if resultType == "people":
            self.onPeopleContextMenu(None)
        elif resultType == "starter_packs":
            self.onStarterPackContextMenu(None)
        elif resultType == "feeds":
            self.onFeedResultContextMenu(None)

    def _runSearch(self):
        query = self.searchText.GetValue().strip()
        if not query or self._account is None:
            return
        resultType = self._currentType()

        if resultType == "posts":
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

        # Translators: Announced while running a People/Starter packs/Feeds search.
        nvdaUi.message(_("Searching..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if resultType == "people":
                    results = client.search_actors(atprotoClient, query)
                elif resultType == "starter_packs":
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
            # Translators: Announced when a People/Starter packs/Feeds search fails. {} is the error message.
            nvdaUi.message(_("Search failed: {}").format(error))
            return
        if resultType == "people":
            self._users = [
                {
                    "did": a.did, "handle": a.handle,
                    "display_name": getattr(a, "display_name", None),
                    "description": getattr(a, "description", None),
                }
                for a in results.actors
            ]
            self._renderUsers()
            # Translators: Announced after a people search finishes. {} is the count.
            nvdaUi.message(_("{} people found.").format(len(self._users)))
        elif resultType == "starter_packs":
            self._starterPacks = list(results.starter_packs)
            self._renderStarterPacks()
            # Translators: Announced after a starter-pack search finishes. {} is the count.
            nvdaUi.message(_("{} starter packs found.").format(len(self._starterPacks)))
        else:
            self._feeds = list(getattr(results, "feeds", []))
            self._renderFeeds()
            # Translators: Announced after a feed search finishes. {} is the count.
            nvdaUi.message(_("{} feeds found.").format(len(self._feeds)))
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
            # Translators: Fallback starter-pack name when none is set.
            self.starterPacksList.InsertItem(i, getattr(record, "name", None) or _("Starter pack"))
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
        # Translators: Context menu item to view a starter pack's details.
        self._addMenuItem(menu, _("&View pack details..."), lambda: self._viewStarterPackDetails(pack))
        # Translators: Context menu item to follow every account in a starter pack.
        self._addMenuItem(menu, _("&Follow everyone in this pack"), lambda: self._followStarterPack(pack))
        # Translators: Context menu item to open a starter pack's page in a browser.
        self._addMenuItem(menu, _("&Open on bsky.app"), lambda: self._openStarterPackInBrowser(pack))
        self.PopupMenu(menu)
        menu.Destroy()

    def _followStarterPack(self, pack):
        # Translators: Announced while following every account in a starter pack.
        nvdaUi.message(_("Following everyone in the pack..."))

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
            # Translators: Announced when following a starter pack's members fails. {} is the error message.
            nvdaUi.message(_("Could not follow pack members: {}").format(error))
            return
        if count == 1:
            # Translators: Announced after following exactly one new person from a starter pack.
            nvdaUi.message(_("Followed 1 new person."))
        else:
            # Translators: Announced after following several new people from a starter pack. {} is the count.
            nvdaUi.message(_("Followed {} new people.").format(count))

    def _viewStarterPackDetails(self, pack):
        # Translators: Announced while loading a starter pack's details.
        nvdaUi.message(_("Loading pack details..."))

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
            # Translators: Fallback error when the server doesn't explain why loading pack details failed.
            errorText = error or _("no data returned")
            # Translators: Announced when loading a starter pack's details fails. {} is the error message.
            nvdaUi.message(_("Could not load pack details: {}").format(errorText))
            return
        StarterPackDetailsDialog(self, pack, full).Show()

    def _openStarterPackInBrowser(self, pack):
        rkey = pack.uri.rsplit("/", 1)[-1]
        creator = getattr(pack, "creator", None)
        webbrowser.open(f"https://bsky.app/starter-pack/{creator.handle if creator else ''}/{rkey}")

    def _renderFeeds(self):
        self.feedsResultList.DeleteAllItems()
        for i, feedGen in enumerate(self._feeds):
            # Translators: Fallback feed name when none is set.
            self.feedsResultList.InsertItem(i, feedGen.display_name or _("Feed"))
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
        # Translators: Context menu item to preview a feed's posts in a tab.
        self._addMenuItem(menu, _("&View feed..."), lambda: self._viewFeed(feedGen))
        # Translators: Context menu item to add a feed to the account's saved feeds.
        self._addMenuItem(menu, _("&Add to my feeds"), lambda: self._addFeed(feedGen))
        self._addMenuItem(
            # Translators: Context menu item to open a feed's page in a browser.
            menu, _("&Open on bsky.app"),
            lambda: webbrowser.open(f"https://bsky.app/profile/{feedGen.creator.handle}/feed/{feedGen.uri.rsplit('/', 1)[-1]}"),
        )
        self.PopupMenu(menu)
        menu.Destroy()

    def _viewFeed(self, feedGen):
        mainWindow = self.GetTopLevelParent()
        # Translators: Fallback feed name when none is set.
        name = feedGen.display_name or _("Feed")
        panel = FeedPreviewTabWindow(mainWindow.notebook, name, "feed", feedGen.uri, origin_key="explore")
        mainWindow.addTab(panel, name, select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "search_preview", "key": panel._feedKey, "kind": "feed", "source_key": feedGen.uri,
            "name": name, "origin_key": "explore",
        })

    def onPostAction(self, evt=None):
        super().onPostAction(evt)

    def _openSearchInNewTab(self):
        if not self._feedKey or self._currentType() != "posts":
            # Translators: Announced when trying to open Explore's search results in a tab before running a Posts search.
            nvdaUi.message(_("Search for posts first."))
            return
        mainWindow = self.GetTopLevelParent()
        # Translators: Tab title for a Posts search opened in its own tab. {} is the search query.
        name = _("Search: {}").format(self._sourceQuery)
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
        if self._currentType() == "people":
            self._openUserSearchInNewTab()
        else:
            self._openSearchInNewTab()

    def _openUserSearchInNewTab(self):
        query = self.searchText.GetValue().strip()
        if not query:
            # Translators: Announced when trying to open Explore's people-search results in a tab before searching.
            nvdaUi.message(_("Search for people first."))
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
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

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

    def _addFeed(self, feedGen):
        # Translators: Announced while adding a feed to the account's saved feeds.
        nvdaUi.message(_("Adding feed..."))

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
            # Translators: Announced when adding a feed fails. {} is the error message.
            nvdaUi.message(_("Could not add feed: {}").format(error))
            return
        # Translators: Announced after adding a feed to saved feeds.
        # Translators: Announced when a feed was already in saved feeds.
        nvdaUi.message(_("Feed added.") if added else _("Already in your feeds."))