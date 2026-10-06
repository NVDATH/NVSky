"""
People tab for NVSky: one list of the accounts you have a relationship
with (following, followers, muted, blocked, activity subscriptions).
Cache-first like UserListTabWindow; F5 refetches. Alt+U acts on the focused user.
"""
import wx

import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from .feedWindow import UserActionMixin, UserListMixin


class PeopleWindow(UserActionMixin, UserListMixin, wx.Panel):
    TAB_KEY = "people"
    KIND_KEYS = ["following", "followers", "muted", "blocked", "subscriptions"]
    KIND_LABELS = [
        # Translators: People tab list choice.
        _("Following"),
        # Translators: People tab list choice.
        _("Followers"),
        # Translators: People tab list choice.
        _("Muted users"),
        # Translators: People tab list choice.
        _("Blocked users"),
        # Translators: People tab list choice.
        _("Activity subscriptions"),
    ]
    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        # Translators: Optional permanent tab label for the People tab.
        self.TAB_NAME = _("People")
        self._users = []
        self._kind = "following"
        self._fetching = set()
        self._loadedKinds = set()

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.kindRadio = wx.RadioBox(
            # Translators: Label for the People tab's list-type radio group.
            self, label=_("&List:"), choices=self.KIND_LABELS, majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        sizer.Add(self.kindRadio, flag=wx.ALL, border=10)

        self.userList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._buildUserListColumns()
        sizer.Add(self.userList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        # Translators: Button to open the current People list in its own removable tab.
        self.openTabButton = wx.Button(self, label=_("Open in new &tab"))
        actionRow.Add(self.userActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.openTabButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.kindRadio.Bind(wx.EVT_RADIOBOX, self.onKindChanged)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.openTabButton.Bind(wx.EVT_BUTTON, lambda e: self._openInNewTab())
        self.userList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onUserAction)
        self.userList.Bind(wx.EVT_CONTEXT_MENU, self.onUserAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onUserListCharHook)

        self._updateTitle()
        if self._account is None:
            # Translators: Announced when opening a tab with no active account.
            nvdaUi.message(_("No active account. Log in from Settings first."))
            self._updateButtons()
            return
        self._showCached()

    # ---------------- MainWindow hooks ----------------

    def _updateTitle(self):
        # Translators: Fallback account label in the window title when no account is active.
        accountLabel = self._account["handle"] if self._account else _("no account")
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self._restoreFocusPosition()
        self._loadKind()

    def _restoreFocusPosition(self, moveFocus=True):
        if moveFocus:
            (self.userList if self._users else self.kindRadio).SetFocus()

    # ---------------- state ----------------

    def _kindLabel(self):
        return self.KIND_LABELS[self.KIND_KEYS.index(self._kind)]

    def _cacheKey(self, kind=None):
        return f"people:{kind or self._kind}"

    def _userListLabel(self):
        # Translators: Status bar text for a user-list tab. First {} is the tab name, second {} is the total count.
        return _("{} -- {} total").format(self._kindLabel(), len(self._users))

    def _updateButtons(self):
        hasUsers = bool(self._users)
        self.userActionButton.Show(hasUsers)
        self.userActionButton.Enable(hasUsers)
        self.Layout()

    def _showCached(self):
        cached = db.get_user_list_cache(self._account["id"], self._cacheKey()) if self._account else None
        self._users = cached or []
        self._renderUsers()
        self._updateButtons()
        return cached is not None

    def onKindChanged(self, evt):
        self._kind = self.KIND_KEYS[self.kindRadio.GetSelection()]
        self._showCached()
        self._loadKind()

    def _loadKind(self):
        # Once per list per session: show the cache, then refresh from the server.
        if self._account is None or self._kind in self._loadedKinds:
            return
        self._loadedKinds.add(self._kind)
        hasCache = db.get_user_list_cache(self._account["id"], self._cacheKey()) is not None
        if not hasCache:
            # Translators: Announced while a People list loads for the first time. {} is the list name.
            nvdaUi.message(_("Loading {}, please wait...").format(self._kindLabel()))
        self._fetch(silent=hasCache)

    def _openInNewTab(self):
        if self._account is not None:
            self._openUserListTab(self._kind, self._account["did"], self._account["handle"])

    # ---------------- fetching ----------------

    def _fetch(self, silent=False):
        if self._account is None or self._kind in self._fetching:
            return
        kind = self._kind
        self._fetching.add(kind)
        myDid = self._account["did"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if kind == "following":
                    users = client.get_follows(atprotoClient, myDid)
                elif kind == "followers":
                    users = client.get_followers(atprotoClient, myDid)
                elif kind == "muted":
                    users = client.get_muted_actors(atprotoClient)
                elif kind == "blocked":
                    users = client.get_blocked_actors(atprotoClient)
                else:
                    users = client.get_activity_subscriptions(atprotoClient)
                db.cache_relationship_authors(users, kind)
                error = None
            except Exception as e:
                users = None
                error = str(e)
            finally:
                db.close_all_connections()
            wx.CallAfter(self._onFetchDone, kind, users, error, silent)

        uiutil.start_worker(worker, progress=not silent)

    @uiutil.safe_ui_callback
    def _onFetchDone(self, kind, users, error, silent=False):
        self._fetching.discard(kind)
        label = self.KIND_LABELS[self.KIND_KEYS.index(kind)]
        if error:
            if not silent:
                # Translators: Announced when loading a People list fails. First {} is the list name, second {} is the error message.
                nvdaUi.message(_("Could not load {}: {}").format(label, error))
            return
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], self._cacheKey(kind), users)
        if kind != self._kind:
            return
        if silent and users == self._users:
            return
        self._users = users
        self._renderUsers()
        self._updateButtons()
        # Translators: Announced after a People list loads. First {} is the list name, second {} is the count.
        nvdaUi.message(_("{}: {} loaded.").format(label, len(users)))

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        # Translators: Announced while loading a user list. {} is already-translated (e.g. "followers").
        nvdaUi.message(_("Loading {}, please wait...").format(self._kindLabel()))
        self._fetch()

    def onClearCache(self, evt=None):
        if self._account is None:
            return
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to clear a People list's cache. {} is the list name.
            _("Clear the cached {} list? This can't be undone.").format(self._kindLabel()),
            # Translators: Title of the clear-cache confirmation dialog.
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        db.delete_user_list_cache(self._account["id"], self._cacheKey())
        self._loadedKinds.discard(self._kind)
        self._users = []
        self._renderUsers()
        self._updateButtons()
        self.kindRadio.SetFocus()

    # ---------------- acting on users ----------------

    def _listIsMine(self):
        return True

    @uiutil.safe_ui_callback
    def _focusWhenEmpty(self):
        self.kindRadio.SetFocus()

    def _usersChanged(self):
        # Called by UserListMixin after a row leaves the list.
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], self._cacheKey(), self._users)
        self._updateButtons()