"""
Miscellaneous tab/dialog classes for NVSky's feed views.

ProfileDialog, UserListTabWindow, UserTimelineTabWindow, FeedPreviewTabWindow,
SavedWindow, and LikesWindow: the smaller tab/dialog classes that don't warrant
their own files. The thread and quotes tabs live in postTabs.py.
"""

import wx

import gui
import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import keymap
from .feedWindow import RemovableTabMixin, FeedListMixin
from .userActions import UserActionMixin, UserListMixin
from .itemActions import ItemActionMixin
from .embedView import EmbedViewMixin
from .postText import _search_feed_key


class ProfileDialog(UserActionMixin, uiutil.EscapeCloseDialog):
    """Read-only profile info view. Press Alt+U for the user action menu (view/follow/mute/block/etc.)."""

    def __init__(self, parent, profile: dict):
        self._profile = profile
        title = profile.get("display_name") or f"@{profile['handle']}"
        # Translators: Title of the read-only profile dialog. {} is the account's display name or handle.
        super().__init__(parent, title=_("{} - Profile").format(title), size=(500, 400))

        sizer = wx.BoxSizer(wx.VERTICAL)

        info = (
            # Translators: Profile field label.
            _("Display name: {}\n").format(profile.get("display_name") or _("(none)")) +
            # Translators: Profile field label.
            _("Handle: @{}\n").format(profile["handle"]) +
            # Translators: Profile field label.
            _("Followers: {}\n").format(profile["followers_count"]) +
            # Translators: Profile field label.
            _("Following: {}\n").format(profile["follows_count"]) +
            # Translators: Profile field label.
            _("Posts: {}\n\n").format(profile["posts_count"]) +
            # Translators: Fallback shown when a profile has no bio set.
            (profile.get("description") or _("(no bio)"))
        )
        textCtrl = wx.TextCtrl(self, value=info, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        # Translators: Button to close the profile dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        btnSizer.Add(closeBtn, flag=wx.ALIGN_CENTER)
        sizer.Add(btnSizer, flag=wx.ALIGN_CENTER | wx.ALL, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        if keymap.matches(evt, "user_action"):
            self.showUserActionMenu(self._profile["did"], self._profile["handle"], self._profile.get("display_name"))
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()


class UserListTabWindow(RemovableTabMixin, UserActionMixin, UserListMixin, wx.Panel):
    """
    Browsable user-list tab. Kinds: followers, following, known_followers,
    likes, reposts (fixed to a did/post uri) and search (a people-search
    query). Cache-first on open, then a silent refetch; F5 refetches. No
    pagination.
    """

    _KIND_LABELS = {
        # Translators: Noun used in status/loading text, e.g. "Loading followers, please wait...".
        "followers": _("followers"),
        # Translators: Noun used in status/loading text, e.g. "Loading following, please wait...".
        "following": _("following"),
        # Translators: Noun used in status/loading text, e.g. "Loading search results, please wait...".
        "search": _("search results"),
        # Translators: Noun used in status/loading text, e.g. "Loading likes, please wait...".
        "likes": _("likes"),
        # Translators: Noun used in status/loading text, e.g. "Loading reposts, please wait...".
        "reposts": _("reposts"),
        # Translators: Noun used in status/loading text, e.g. "Loading known followers, please wait...".
        "known_followers": _("known followers"),
        # Translators: Noun used in status/loading text, e.g. "Loading muted users, please wait...".
        "muted": _("muted users"),
        # Translators: Noun used in status/loading text, e.g. "Loading blocked users, please wait...".
        "blocked": _("blocked users"),
        # Translators: Noun used in status/loading text, e.g. "Loading activity subscriptions, please wait...".
        "subscriptions": _("activity subscriptions"),
    }
    _OWN_LIST_NAMES = {
        # Translators: Tab name for the account's own muted-users list.
        "muted": _("Muted users"),
        # Translators: Tab name for the account's own blocked-users list.
        "blocked": _("Blocked users"),
        # Translators: Tab name for the account's own activity-subscriptions list.
        "subscriptions": _("Activity subscriptions"),
    }

    def __init__(self, parent, kind, target, owner_label=None, origin_key=None):
        super().__init__(parent)
        self._account = db.get_active_account()
        self._kind = kind  # "followers" / "following" / "search" / "likes" / "reposts"
        self._target = target  # did for followers/following, query for search, post uri for likes/reposts
        if kind in ("followers", "following"):
            kindLabels = {
                # Translators: Tab-name kind label in "{kind} of {owner}".
                "followers": _("Followers"),
                # Translators: Tab-name kind label in "{kind} of {owner}".
                "following": _("Following"),
            }
            # Translators: Tab name for a followers/following list. First {} is the kind, second {} is the account owner.
            self.TAB_NAME = _("{} of {}").format(kindLabels[kind], owner_label)
        elif kind == "known_followers":
            # Translators: Tab name for a known-followers list. {} is the account owner.
            self.TAB_NAME = _("Known followers of {}").format(owner_label)
        elif kind in self._OWN_LIST_NAMES:
            self.TAB_NAME = self._OWN_LIST_NAMES[kind]
        elif kind in ("likes", "reposts"):
            kindLabels = {
                # Translators: Tab-name kind label in "{kind} on {post}".
                "likes": _("Likes"),
                # Translators: Tab-name kind label in "{kind} on {post}".
                "reposts": _("Reposts"),
            }
            if owner_label:
                # Translators: Tab name for a likes/reposts list. First {} is the kind, second {} is the post owner.
                self.TAB_NAME = _("{} on {}").format(kindLabels[kind], owner_label)
            else:
                self.TAB_NAME = kindLabels[kind]
        else:
            # Translators: Tab name for a people-search results list. {} is the search query.
            self.TAB_NAME = _("People search: {}").format(target)
        self.TAB_TEMP_TYPE = "user_list"
        self.TAB_TEMP_KEY = f"{kind}:{target}"
        self._originTabKey = origin_key
        self._users = []

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.userList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._buildUserListColumns()
        sizer.Add(self.userList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action...") + keymap.hint("user_action"))
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.userList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onUserAction)
        self.userList.Bind(wx.EVT_CONTEXT_MENU, self.onUserAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onUserListCharHook)

        self._updateButtons()
        self._pendingIsInitial = True
        cached = db.get_user_list_cache(self._account["id"], self.TAB_TEMP_KEY) if self._account else None
        self._hadInitialCache = cached is not None
        if cached is not None:
            self._users = cached
            self._renderUsers()
        else:
            # Translators: Status bar text while a user list loads for the first time.
            self.statusBar.SetStatusText(_("Loading, please wait..."))
        self._fetch()

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.userList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "user_list", self.TAB_TEMP_KEY)
            db.delete_user_list_cache(self._account["id"], self.TAB_TEMP_KEY)
        self._jumpBackToOrigin()

    def _userListLabel(self):
        # Translators: Status bar text for a user-list tab. First {} is the tab name, second {} is the total count.
        return _("{} -- {} total").format(self.TAB_NAME, len(self._users))

    def _updateStatusBar(self):
        self.statusBar.SetStatusText(self._userListLabel())

    def _listIsMine(self):
        return self._account is not None and self._target == self._account["did"]

    def _usersChanged(self):
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], self.TAB_TEMP_KEY, self._users)

    def _updateButtons(self):
        hasUsers = bool(self._users)
        self.userActionButton.Show(hasUsers)
        self.userActionButton.Enable(hasUsers)
        self.Layout()

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        # Translators: Announced while loading a user list. {} is already-translated (e.g. "followers").
        nvdaUi.message(_("Loading {}, please wait...").format(self._KIND_LABELS[self._kind]))
        self._pendingIsInitial = False
        self._fetch(progress=True)

    def onClearCache(self, evt=None):
        # Clears the cached list only -- no auto-refetch, F5/bgsync
        # will repopulate it in their own time.
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to clear a cached user list. First {} is already-translated (e.g. "followers"), second {} is the tab name.
            _("Clear cached {} for {}? This can't be undone.").format(self._KIND_LABELS[self._kind], self.TAB_NAME),
            # Translators: Title of the clear-cache confirmation dialog.
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        if self._account is not None:
            db.delete_user_list_cache(self._account["id"], self.TAB_TEMP_KEY)
        self._users = []
        self._renderUsers()
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()

    def _fetch(self, progress=False):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                users = client.fetch_user_list(atprotoClient, self._kind, self._target)
                db.cache_relationship_authors(users, self._kind)
                error = None
            except Exception as e:
                users = None
                error = str(e)
            finally:
                db.close_all_connections()
            wx.CallAfter(self._onFetchDone, users, error)

        uiutil.start_worker(worker, progress=progress)

    @uiutil.safe_ui_callback
    def _onFetchDone(self, users, error):
        label = self._KIND_LABELS[self._kind]
        isInitial = self._pendingIsInitial
        self._pendingIsInitial = False
        if error:
            if not self._users:
                # Translators: Announced when first loading a user list fails. First {} is already-translated (e.g. "followers"), second {} is the error message.
                nvdaUi.message(_("Could not load {}: {}").format(label, error))
                # Translators: Status bar text when first loading a user list fails. {} is already-translated (e.g. "followers").
                self.statusBar.SetStatusText(_("Could not load {}.").format(label))
            else:
                # Translators: Announced when refreshing an already-loaded user list fails. First {} is already-translated (e.g. "followers"), second {} is the error message.
                nvdaUi.message(_("Could not refresh {}: {}").format(label, error))
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
            # Translators: Announced after a user list loads. First {} is the count, second {} is already-translated (e.g. "followers").
            nvdaUi.message(_("{} {} loaded.").format(len(self._users), label))


class UserTimelineTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    A user's own timeline in a removable tab: FeedListMixin cache-first/
    lazy-load via client.sync_author_feed_page (feed_key
    f"user_timeline:{did}"). Not user-renameable.
    """

    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    VIRTUAL_LIST = True

    def __init__(self, parent, did: str, owner_label: str, origin_key: str = None):
        super().__init__(parent)

        self._account = db.get_active_account()
        # Translators: Tab name for a user's own timeline popped into its own tab. {} is the account owner.
        self.TAB_NAME = _("Timeline of {}").format(owner_label)
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
        # Re-reads from the DB -- see notificationsWindow.py's
        # onTabActivated for why _render() alone isn't enough.
        self._loadFromCache(reset=True)
        if self._account is not None:
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
            self._restoreFocusPosition()

    def onTabRemoved(self):
        for value in vars(self).values():
            if isinstance(value, wx.Timer):
                value.Stop()
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "user_timeline", self._did)
            db.clear_feed_key_cache(self._account["id"], self._feedKey)
            db.clear_feed_key_cache(self._account["id"], client.pin_feed_key(self._did))
        self._jumpBackToOrigin()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        posts = db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)
        if before_indexed_at is not None or limit is not None:
            return posts  # pagination probes and top-of-feed checks ignore the pin
        pins = db.get_feed_page(self._account["id"], client.pin_feed_key(self._did), limit=1)
        if not pins:
            return posts
        pinned = dict(pins[0])
        pinned["_pinned"] = True
        # Keeps the pin out of _loadMore's "oldest loaded post" minimum.
        pinned["feed_indexed_at"] = "9999-12-31T23:59:59.999Z"
        return [pinned] + [p for p in posts if p["uri"] != pinned["uri"]]

    def _applySortOrder(self, posts):
        # The pin stays first whatever the sort order.
        pinned = [p for p in posts if p.get("_pinned")]
        return pinned + super()._applySortOrder([p for p in posts if not p.get("_pinned")])

    def _rowCells(self, post, mode):
        cells = super()._rowCells(post, mode)
        if post.get("_pinned"):
            # Translators: Prefix on a user's pinned post in their timeline tab. {} is the post text.
            cells[2] = _("Pinned: {}").format(cells[2])
        return cells

    def _doClearCache(self):
        db.clear_feed_key_cache(self._account["id"], client.pin_feed_key(self._did))
        super()._doClearCache()

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_author_feed_page(atprotoClient, self._account["id"], self._did, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def _onRepostChanged(self, post):
        # This row only exists in my own timeline BECAUSE I reposted
        # it -- once undone, remove it, same pattern as
        # SavedWindow._onBookmarkChanged for unsave.
        if post.get("viewer_repost_uri") or not post.get("is_repost"):
            return
        db.delete_feed_item(self._account["id"], self._feedKey, post["uri"])
        self._removePostFromList(post)

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))


class FeedPreviewTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """Popped-out feed generator or pinned search -- structurally a
    clone of ListTabWindow (real cached/paginated feed via
    _dbGetPage/_syncPage), not a one-shot fetch. kind is "feed" or
    "search"; source_key is the feed_uri or search query."""

    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    VIRTUAL_LIST = True

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
            # Translators: Button to add a previewed feed to the account's saved feeds.
            self.addFeedButton = wx.Button(self, label=_("&Add to my feeds"))
            extraWidgets = [self.addFeedButton]

        self._buildStandardFeedSizer(extra_action_widgets=extraWidgets)
        self._bindStandardFeedEvents()
        if kind == "feed":
            self.addFeedButton.Bind(wx.EVT_BUTTON, lambda e: self._addFeedFromPreview())

        self._finishStandardFeedInit(sync_if_empty=True)

    def onTabActivated(self):
        # Re-reads from the DB -- see notificationsWindow.py's
        # onTabActivated for why _render() alone isn't enough.
        self._loadFromCache(reset=True)
        if self._account is not None:
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
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
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

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
        # Translators: Announced while adding a feed to the account's saved feeds.
        nvdaUi.message(_("Adding feed..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                added = client.add_feed_to_saved(atprotoClient, self._sourceKey)
                error = None
            except Exception as e:
                added = False
                error = str(e)
            wx.CallAfter(self._onAddFeedFromPreviewDone, added, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onAddFeedFromPreviewDone(self, added, error):
        self.addFeedButton.Enable()
        if error:
            # Translators: Announced when adding a feed fails. {} is the error message.
            nvdaUi.message(_("Could not add feed: {}").format(error))
            return
        # Translators: Announced after adding a feed to saved feeds.
        # Translators: Announced when a feed was already in saved feeds.
        nvdaUi.message(_("Feed added.") if added else _("Already in your feeds."))


class SavedWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """Saved posts (bookmarks): feed_key "saved" via sync_saved(). Unsaving a post removes its row (_onBookmarkChanged)."""

    TAB_KEY = "saved"
    VIRTUAL_LIST = True

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        # Translators: Permanent tab label for saved posts.
        self.TAB_NAME = _("Saved")
        self._feedKey = "saved"
        self._tracksUnread = False
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit()

    def onTabActivated(self):
        # Re-reads from the DB -- see notificationsWindow.py's
        # onTabActivated for why _render() alone isn't enough.
        self._loadFromCache(reset=True)
        if self._account is not None:
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
            self._restoreFocusPosition()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _onBookmarkChanged(self, post):
        if post.get("viewer_bookmarked"):
            return  # re-saved (or still saved) -- nothing to remove
        db.delete_feed_item(self._account["id"], self._feedKey, post["uri"])
        self._removePostFromList(post)

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
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    # onCharHook is inherited from FeedListMixin -- no SUPPORTS_* flags
    # needed here, this class never had Space/Ctrl+A/Left-Right/Ctrl+N.


class LikesWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """Posts the active account liked, newest like first. Optional permanent tab."""

    TAB_KEY = "likes"
    VIRTUAL_LIST = True

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        # Translators: Optional permanent tab label for liked posts.
        self.TAB_NAME = _("Likes")
        self._feedKey = "likes"
        self._tracksUnread = False
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit()

    def onTabActivated(self):
        # Re-reads from the DB -- see notificationsWindow.py's
        # onTabActivated for why _render() alone isn't enough.
        self._loadFromCache(reset=True)
        if self._account is not None:
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
            self._restoreFocusPosition()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _onLikeChanged(self, post):
        if post.get("viewer_like_uri"):
            return
        self._removePostFromList(post)

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_likes(atprotoClient, self._account["id"], cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))