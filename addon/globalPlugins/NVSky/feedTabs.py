"""
Miscellaneous tab/dialog classes for NVSky's feed views.

Split out of feedWindow.py (see plan-17.md). ProfileDialog, UserListTabWindow,
ThreadTabWindow, QuotesTabWindow, UserTimelineTabWindow, FeedPreviewTabWindow,
and SavedWindow don't share enough to warrant their own dedicated files
(unlike Lists/Explore/Notifications), so they're grouped here together.
"""
import threading
import wx

import gui
import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import soundpack
from .feedWindow import (
    RemovableTabMixin,
    UserActionMixin,
    UserListMixin,
    FeedListMixin,
    ItemActionMixin,
    EmbedViewMixin,
    _describe_embed,
    _message_text,
    _format_post_time,
    _search_feed_key,
    _announce_now,
    _embed_sound_event,
    _visible_embed_text,
    _visible_message_text,
    _post_web_url,
    _compose_copy_text,
)


class ProfileDialog(UserActionMixin, wx.Dialog):
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
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.userList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onUserAction)
        self.userList.Bind(wx.EVT_CONTEXT_MENU, self.onUserAction)
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
                if self._kind == "followers":
                    users = client.get_followers(atprotoClient, self._target)
                elif self._kind == "following":
                    users = client.get_follows(atprotoClient, self._target)
                elif self._kind == "known_followers":
                    users = client.get_known_followers(atprotoClient, self._target)
                elif self._kind == "likes":
                    users = client.get_post_likes(atprotoClient, self._target)
                elif self._kind == "reposts":
                    users = client.get_post_reposted_by(atprotoClient, self._target)
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
        # Guards onItemFocused's embed-sound hook against firing for
        # programmatic focus (initial render's auto-focus onto the
        # target post, or a re-render after F5) -- same purpose as
        # FeedListMixin._suppressFocusEvents, just a local copy since
        # this class isn't a FeedListMixin host.
        self._suppressFocusEvents = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Column order matches FeedListMixin._buildFeedListColumns
        # (Embed/Author/Message/Posted) for UX consistency across every
        # post list in the add-on -- this was previously Author/
        # Message/Posted/Embed here, the odd one out.
        # Translators: Column header for a post's embed summary (image/video/link/quote).
        self.postList.InsertColumn(0, _("Embed"), width=140)
        # Translators: Column header for a post's author.
        self.postList.InsertColumn(1, _("Author"), width=140)
        # Translators: Column header for a post's text.
        self.postList.InsertColumn(2, _("Message"), width=380)
        # Translators: Column header for when a post was posted.
        self.postList.InsertColumn(3, _("Posted"), width=140)
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. Shows the Alt+A shortcut.
        self.postActionButton = wx.Button(self, label=_("&Post action... (Alt+A)"))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onPostAction)
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._renderThread(target_index=target_index)

    def _makeTabName(self, posts):
        if not posts:
            # Translators: Fallback tab name for an empty thread.
            return _("Thread")
        firstPost = posts[0]
        preview = firstPost.get("text", "")
        if len(preview) > 40:
            preview = preview[:40] + "..."
        label = self._displayLabel(firstPost.get("handle"), firstPost.get("display_name"))
        # Translators: Thread tab name. First {} is the first post's author label, second {} is a preview of its text.
        return _("Thread: {}: {}").format(label, preview)

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.postList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "thread", self._rootUri)
            db.delete_user_list_cache(self._account["id"], f"thread:{self._rootUri}")
        self._jumpBackToOrigin()

    def onClearCache(self, evt=None):
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to clear a thread's cached copy. {} is the tab name.
            _("Clear the cached copy of {}? This can't be undone.").format(self.TAB_NAME),
            # Translators: Title of the clear-cache confirmation dialog.
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        if self._account is not None:
            db.delete_user_list_cache(self._account["id"], f"thread:{self._rootUri}")
        self._posts = []
        self._renderThread()
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()

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
        if evt.ControlDown() and keyCode == wx.WXK_DELETE:
            self.onClearCache()
            return
        if keyCode == ord("C") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.postList:
            self._copyFocusedPostRow()
            return
        if keyCode == ord("J") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.postList:
            uiutil.jump_to_row(self, self.postList)
            return
        evt.Skip()

    def _copyFocusedPostRow(self):
        post = self._getFocusedPost()
        if post is None:
            return
        parts = [
            self._displayLabel(post.get("handle"), post.get("display_name")),
            _message_text(post),
            _format_post_time(post.get("indexed_at")),
        ]
        url = _post_web_url(post.get("uri"), post.get("handle") or post.get("author_did"))
        uiutil.copy_text_to_clipboard(_compose_copy_text(parts, url))

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
                self.postList.InsertItem(i, _describe_embed(post.get("embed_json")))
                self.postList.SetItem(i, 1, self._displayLabel(post.get("handle"), post.get("display_name")))
                self.postList.SetItem(i, 2, prefix + _message_text(post))
                self.postList.SetItem(i, 3, _format_post_time(post.get("indexed_at")))

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
                self._suppressFocusEvents = True
                try:
                    self.postList.Focus(focusIndex)
                    self.postList.Select(focusIndex)
                    self.postList.EnsureVisible(focusIndex)
                finally:
                    self._suppressFocusEvents = False
        finally:
            self.postList.Thaw()

        # Translators: Thread tab status bar text. First {} is the tab name, second {} is the post count.
        self.statusBar.SetStatusText(_("{} -- {} posts").format(self.TAB_NAME, len(self._posts)))

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        # Translators: Announced while (re)loading a whole thread.
        nvdaUi.message(_("Loading thread, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                posts, targetIndex = client.get_thread(atprotoClient, self._targetUri)
                error = None
            except Exception as e:
                posts, targetIndex = None, 0
                error = str(e)
            wx.CallAfter(self._onRefreshDone, posts, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRefreshDone(self, posts, error):
        if error:
            # Translators: Announced when refreshing a thread fails. {} is the error message.
            nvdaUi.message(_("Could not refresh thread: {}").format(error))
            return
        self._posts = posts or []
        self.TAB_NAME = self._makeTabName(self._posts)
        self._updateTitle()
        self._renderThread()
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], f"thread:{self._rootUri}", self._posts)
        # Translators: Announced after a thread finishes loading. {} is the post count.
        nvdaUi.message(_("{} posts in thread.").format(len(self._posts)))

    def onItemFocused(self, evt):
        # BUG FIX: ThreadTabWindow isn't a FeedListMixin host (see this
        # class's own docstring), so it never got the shared embed-type
        # sound hook FeedListMixin.onItemFocused provides -- confirmed
        # NOT a column-order issue, _embed_sound_event reads straight
        # from the post dict's embed_json field regardless of what
        # column order is displayed. Added directly here instead.
        # BUG FIX #2: also skips while _suppressFocusEvents is set --
        # without this, the auto-focus onto the target post right after
        # opening/refreshing the tab fired this exactly like a real
        # user-driven arrow-key move, playing an embed sound the user
        # never asked for (confirmed: heard on the tab's initial post).
        if self._suppressFocusEvents:
            evt.Skip()
            return
        index = evt.GetIndex()
        if 0 <= index < len(self._posts):
            embedEvent = _embed_sound_event(self._posts[index].get("embed_json"))
            if embedEvent:
                soundpack.play_debounced(embedEvent)
        evt.Skip()

    def _getFocusedPost(self):
        index = self.postList.GetFocusedItem()
        if 0 <= index < len(self._posts):
            return self._posts[index]
        return None

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post.get("author_did"), post.get("handle"), post.get("display_name"))

    def onPostAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return

        menu = wx.Menu()
        isLiked = bool(post.get("viewer_like_uri"))
        # Translators: Post action menu item (already liked).
        # Translators: Post action menu item (not yet liked).
        self._addMenuItem(menu, _("Unlike") if isLiked else _("Like"), lambda: self._togglePostLike(post))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        # Translators: Submenu item under "Copy...", copies the post's text.
        self._addMenuItem(copyMenu, _("Copy post text"), lambda: self._copyPostText(post))
        # Translators: Submenu item under "Copy...", copies a link to the post.
        self._addMenuItem(copyMenu, _("Copy link to post"), lambda: self._copyPostLink(post))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(copyMenu, _("Copy..."))

        embedMenu = self._buildViewEmbedMenu(post)
        if embedMenu is not None:
            # Translators: Post action submenu label for viewing an embedded image/video/link.
            menu.AppendSubMenu(embedMenu, _("Embed..."))

        self.PopupMenu(menu)
        menu.Destroy()

    def _togglePostLike(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_like_uri"):
                    client.unlike_post(atprotoClient, post["viewer_like_uri"])
                    post["viewer_like_uri"] = None
                    # Translators: Announced after unliking a post.
                    message = _("Unliked.")
                else:
                    like_uri = client.like_post(atprotoClient, post["uri"], post["cid"])
                    post["viewer_like_uri"] = like_uri
                    # Translators: Announced after liking a post.
                    message = _("Liked.")
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onLikeSynced, post, message, error)

        threading.Thread(target=worker, daemon=True).start()

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


class QuotesTabWindow(RemovableTabMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    Posts that quote a given post, popped into its own removable tab.
    Structurally a simpler sibling of ThreadTabWindow (flat list, no
    depth/indent, F5 always re-fetches the whole thing) -- not a
    FeedListMixin host, same reasoning as ThreadTabWindow (getQuotes
    has no incremental sync). Adds one action ThreadTabWindow doesn't
    have: "Detach my post from this quote" -- only offered when the
    ORIGINAL quoted post (self._targetUri) is mine, since detaching is
    something only the quoted post's own author can do.
    """

    def __init__(self, parent, target_uri, quotes, origin_key=None):
        super().__init__(parent)
        self._account = db.get_active_account()
        self._targetUri = target_uri
        self.TAB_TEMP_TYPE = "quotes"
        self.TAB_TEMP_KEY = target_uri
        self._originTabKey = origin_key
        self._posts = quotes
        self.TAB_NAME = self._makeTabName()
        # See ThreadTabWindow's identical attribute for why this exists.
        self._suppressFocusEvents = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Column order matches FeedListMixin._buildFeedListColumns
        # (Embed/Author/Message/Posted) for UX consistency across every
        # post list in the add-on -- this was previously Author/
        # Message/Posted/Embed here, the odd one out.
        # Translators: Column header for a post's embed summary (image/video/link/quote).
        self.postList.InsertColumn(0, _("Embed"), width=140)
        # Translators: Column header for a post's author.
        self.postList.InsertColumn(1, _("Author"), width=140)
        # Translators: Column header for a post's text.
        self.postList.InsertColumn(2, _("Message"), width=380)
        # Translators: Column header for when a post was posted.
        self.postList.InsertColumn(3, _("Posted"), width=140)
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu.
        self.postActionButton = wx.Button(self, label=_("&Post action..."))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)
        self.SetSizer(sizer)

        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onPostAction)
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._render()

    def _makeTabName(self):
        target = db.get_post(self._targetUri)
        if target is None:
            # Translators: Fallback quotes-tab name when the quoted post isn't cached. {} is the post's rkey fragment.
            return _("Quotes: {}").format(self._targetUri.rsplit('/', 1)[-1])
        handle = target.get("handle") or target.get("author_did")
        preview = (target.get("text") or "")[:40]
        if len(target.get("text") or "") > 40:
            preview += "..."
        if handle and preview:
            # Translators: Quotes-tab name with a text preview. First {} is a preview of the quoted post, second {} is the handle.
            return _('Quotes: "{}" by @{}').format(preview, handle)
        if handle:
            # Translators: Quotes-tab name with no text preview available. {} is the handle.
            return _("Quotes on @{}'s post").format(handle)
        # Translators: Fallback quotes-tab name when the quoted post isn't cached. {} is the post's rkey fragment.
        return _("Quotes: {}").format(self._targetUri.rsplit('/', 1)[-1])

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.postList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "quotes", self._targetUri)
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
        if keyCode == ord("C") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.postList:
            self._copyFocusedPostRow()
            return
        if keyCode == ord("J") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.postList:
            uiutil.jump_to_row(self, self.postList)
            return
        evt.Skip()

    def _copyFocusedPostRow(self):
        post = self._getFocusedPost()
        if post is None:
            return
        parts = [
            self._displayLabel(post.get("handle"), post.get("display_name")),
            _message_text(post),
            _format_post_time(post.get("indexed_at")),
        ]
        url = _post_web_url(post.get("uri"), post.get("handle") or post.get("author_did"))
        uiutil.copy_text_to_clipboard(_compose_copy_text(parts, url))

    def _render(self):
        previousUri = None
        focused = self._getFocusedPost()
        if focused is not None:
            previousUri = focused["uri"]

        self.postList.Freeze()
        try:
            self.postList.DeleteAllItems()
            for i, post in enumerate(self._posts):
                self.postList.InsertItem(i, _describe_embed(post.get("embed_json")))
                self.postList.SetItem(i, 1, self._displayLabel(post.get("handle"), post.get("display_name")))
                self.postList.SetItem(i, 2, _message_text(post))
                self.postList.SetItem(i, 3, _format_post_time(post.get("indexed_at")))

            if self._posts:
                focusIndex = 0
                if previousUri:
                    focusIndex = next((i for i, p in enumerate(self._posts) if p["uri"] == previousUri), 0)
                self._suppressFocusEvents = True
                try:
                    self.postList.Focus(focusIndex)
                    self.postList.Select(focusIndex)
                    self.postList.EnsureVisible(focusIndex)
                finally:
                    self._suppressFocusEvents = False
        finally:
            self.postList.Thaw()

        # Translators: Quotes tab status bar text. First {} is the tab name, second {} is the quote count.
        self.statusBar.SetStatusText(_("{} -- {} quotes").format(self.TAB_NAME, len(self._posts)))

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        # Translators: Announced while loading a post's quotes.
        nvdaUi.message(_("Loading quotes, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                quotes = client.get_post_quotes(atprotoClient, self._targetUri)
                error = None
            except Exception as e:
                quotes = []
                error = str(e)
            wx.CallAfter(self._onRefreshDone, quotes, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRefreshDone(self, quotes, error):
        if error:
            # Translators: Announced when refreshing quotes fails. {} is the error message.
            nvdaUi.message(_("Could not refresh quotes: {}").format(error))
            return
        self._posts = quotes or []
        self._render()
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], f"quotes:{self._targetUri}", self._posts)
        # Translators: Announced after quotes finish loading. {} is the count.
        nvdaUi.message(_("{} quotes.").format(len(self._posts)))

    def onItemFocused(self, evt):
        # Same bug/fix as ThreadTabWindow.onItemFocused just above --
        # QuotesTabWindow isn't a FeedListMixin host either, so it
        # never had the embed-type sound hook wired at all. Also guards
        # against programmatic auto-focus the same way (see
        # ThreadTabWindow.onItemFocused's own comment on this).
        if self._suppressFocusEvents:
            evt.Skip()
            return
        index = evt.GetIndex()
        if 0 <= index < len(self._posts):
            embedEvent = _embed_sound_event(self._posts[index].get("embed_json"))
            if embedEvent:
                soundpack.play_debounced(embedEvent)
        evt.Skip()

    def _getFocusedPost(self):
        index = self.postList.GetFocusedItem()
        if 0 <= index < len(self._posts):
            return self._posts[index]
        return None

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post.get("author_did"), post.get("handle"), post.get("display_name"))

    def onPostAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return

        menu = wx.Menu()
        isLiked = bool(post.get("viewer_like_uri"))
        # Translators: Post action menu item (already liked).
        # Translators: Post action menu item (not yet liked).
        self._addMenuItem(menu, _("Un&like") if isLiked else _("&Like"), lambda: self._togglePostLike(post))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        # Translators: Submenu item under "Copy...", copies the post's text.
        self._addMenuItem(copyMenu, _("Copy &post text"), lambda: self._copyPostText(post))
        # Translators: Submenu item under "Copy...", copies a link to the post.
        self._addMenuItem(copyMenu, _("Copy &link to post"), lambda: self._copyPostLink(post))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(copyMenu, _("&Copy..."))

        embedMenu = self._buildViewEmbedMenu(post)
        if embedMenu is not None:
            # Translators: Post action submenu label for viewing an embedded image/video/link.
            menu.AppendSubMenu(embedMenu, _("&Embed..."))

        isMyOriginal = self._account and self._account.get("did") == self._getTargetAuthorDid()
        if isMyOriginal:
            menu.AppendSeparator()
            # Translators: Post action menu item, only offered on your own quoted post.
            self._addMenuItem(menu, _("&Detach my post from this quote..."), lambda: self._detachQuote(post))

        self.PopupMenu(menu)
        menu.Destroy()

    def _getTargetAuthorDid(self):
        # The quoted post itself isn't in self._posts (only the
        # quoting posts are) -- read it from the local cache instead.
        target = db.get_post(self._targetUri)
        return target.get("author_did") if target else None

    def _togglePostLike(self, post):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if post.get("viewer_like_uri"):
                    client.unlike_post(atprotoClient, post["viewer_like_uri"])
                    post["viewer_like_uri"] = None
                    # Translators: Announced after unliking a post.
                    message = _("Unliked.")
                else:
                    like_uri = client.like_post(atprotoClient, post["uri"], post["cid"])
                    post["viewer_like_uri"] = like_uri
                    # Translators: Announced after liking a post.
                    message = _("Liked.")
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onLikeSynced, post, message, error)

        threading.Thread(target=worker, daemon=True).start()

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

    def _detachQuote(self, quotingPost):
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to detach your own post from someone else's quote of it.
            _("Remove your post from this person's quote? This only affects your post's own quote list."),
            # Translators: Title of the confirm-detach dialog.
            _("Confirm detach"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.detach_quote(atprotoClient, self._targetUri, quotingPost["uri"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onDetachDone, quotingPost, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onDetachDone(self, quotingPost, error):
        if error:
            self._onActionDone(None, error)
            return
        for i, p in enumerate(self._posts):
            if p["uri"] == quotingPost["uri"]:
                del self._posts[i]
                self._render()
                break
        # Translators: Announced after detaching your post from someone's quote.
        self._onActionDone(_("Detached."), None)


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
        self._render()
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
        self._jumpBackToOrigin()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _visible_embed_text(post))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _visible_message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

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
        for i, p in enumerate(self._posts):
            if p["uri"] == post["uri"]:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break

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
        self._render()
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
        # Translators: Permanent tab label for saved posts.
        self.TAB_NAME = _("Saved")
        self._feedKey = "saved"
        self._tracksUnread = False
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit()

    def onTabActivated(self):
        self._render()
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
        self.postList.InsertItem(index, _visible_embed_text(post))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _visible_message_text(post))
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
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    # onCharHook is inherited from FeedListMixin -- no SUPPORTS_* flags
    # needed here, this class never had Space/Ctrl+A/Left-Right/Ctrl+N.


class LikesWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """Posts the active account liked, newest like first. Optional permanent tab."""

    TAB_KEY = "likes"

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
        self._render()
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
        for i, p in enumerate(self._posts):
            if p["uri"] == post["uri"]:
                del self._posts[i]
                self._render()
                if self._posts:
                    newIndex = min(i, len(self._posts) - 1)
                    self.postList.Focus(newIndex)
                    self.postList.Select(newIndex)
                break

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