"""
Post-browser tabs for NVSky: a whole thread (ThreadTabWindow) and the posts that quote a post
(QuotesTabWindow), both built on PostBrowserTabWindow.
"""
import wx

import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import soundpack
from . import keymap
from .feedWindow import RemovableTabMixin
from .userActions import UserActionMixin
from .embedView import EmbedViewMixin
from .postText import (
    _describe_embed,
    _message_text,
    _format_post_time,
    _announce_now,
    _embed_sound_event,
    _post_web_url,
    _compose_copy_text,
    _show_full_post,
)


class PostBrowserTabWindow(RemovableTabMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    Flat post list in a removable tab, shared by the thread and quotes tabs. Not a FeedListMixin
    host: neither has incremental sync, so F5 re-fetches everything. Cache-first on open via
    db.get_user_list_cache under _cacheKey(). Reduced Post action set (Like, Copy, Embed).

    A subclass sets TAB_TEMP_TYPE, TAB_TEMP_KEY and TAB_NAME, and provides _cacheKey, _statusText,
    _fetchPosts, _announceLoading, _announceRefreshFailed and _announceLoaded.
    """

    SUPPORTS_CLEAR_CACHE = False

    def __init__(self, parent, posts, origin_key=None):
        super().__init__(parent)
        self._account = db.get_active_account()
        self._originTabKey = origin_key
        self._posts = posts
        # Skips the embed sound for programmatic focus (initial render / refresh).
        self._suppressFocusEvents = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
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
        # Translators: Button to open the Post action menu. The current shortcut is appended automatically.
        self.postActionButton = wx.Button(self, label=_("&Post action...") + keymap.hint("post_action"))
        # Translators: Button to open the User action menu. The current shortcut is appended automatically.
        self.userActionButton = wx.Button(self, label=_("&User action...") + keymap.hint("user_action"))
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

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.postList.SetFocus()

    def onTabRemoved(self):
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)
            db.delete_user_list_cache(self._account["id"], self._cacheKey())
        self._jumpBackToOrigin()

    def onCharHook(self, evt):
        inList = self.FindFocus() is self.postList
        if keymap.matches(evt, "check_updates"):
            self.onCheckForUpdates(None)
            return
        if keymap.matches(evt, "post_action"):
            self.onPostAction()
            return
        if keymap.matches(evt, "user_action"):
            self.onUserAction()
            return
        if keymap.matches(evt, "clear_cache") and self.SUPPORTS_CLEAR_CACHE:
            self.onClearCache()
            return
        if keymap.matches(evt, "reveal_warning") and inList:
            _show_full_post(self, self._getFocusedPost())
            return
        if keymap.matches(evt, "copy_row") and inList:
            self._copyFocusedPostRow()
            return
        if keymap.matches(evt, "jump_row") and inList:
            uiutil.jump_to_row(self, self.postList)
            return
        if inList and self.runCustomShortcut(evt, self._getFocusedPost()):
            return
        evt.Skip()

    def _copyFocusedPostRow(self):
        post = self._getFocusedPost()
        if post is None:
            return
        parts = [
            self._displayLabel(post.get("handle"), post.get("display_name")),
            _message_text(post),
        ]
        url = _post_web_url(post.get("uri"), post.get("handle") or post.get("author_did"))
        uiutil.copy_text_to_clipboard(_compose_copy_text(parts, url))

    def _render(self, target_index=None):
        # Keep the focused post (by uri) unless target_index is given; a
        # re-fetch can insert posts anywhere.
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

        self._updateStatusBar()
        self._updateActionButtons()

    def _updateActionButtons(self):
        hasItems = bool(self._posts)
        for button in (self.postActionButton, self.userActionButton):
            button.Show(hasItems)
            button.Enable(hasItems)
        self.Layout()

    def _updateStatusBar(self):
        self.statusBar.SetStatusText(self._statusText())

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            # Translators: Announced when checking for updates with no active account.
            nvdaUi.message(_("No active account."))
            return
        self._announceLoading()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                posts = self._fetchPosts(atprotoClient)
                error = None
            except Exception as e:
                posts = None
                error = str(e)
            wx.CallAfter(self._onRefreshDone, posts, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRefreshDone(self, posts, error):
        if error:
            self._announceRefreshFailed(error)
            return
        self._posts = posts or []
        self._afterRefresh()
        if self._account is not None:
            db.set_user_list_cache(self._account["id"], self._cacheKey(), self._posts)
        self._announceLoaded()

    def _afterRefresh(self):
        self._render()

    def onItemFocused(self, evt):
        # Embed sound on focus; skipped for programmatic focus.
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

        self._extendPostMenu(menu, post)

        self.PopupMenu(menu)
        menu.Destroy()

    def _extendPostMenu(self, menu, post):
        pass

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

        uiutil.start_worker(worker, progress=False)

    def _copyPostText(self, post):
        self._copyToClipboard(post.get("text", ""))
        # Translators: Announced after copying a post's text to the clipboard.
        _announce_now(_("Post text copied to clipboard."))

    def _copyPostLink(self, post):
        url = _post_web_url(post["uri"], post.get("handle") or post.get("author_did"))
        self._copyToClipboard(url)
        # Translators: Announced after copying a post's URL to the clipboard.
        _announce_now(_("Post URL copied to clipboard."))


class ThreadTabWindow(PostBrowserTabWindow):
    """
    Full thread: ancestors up to the root, the post, then its replies depth-first (each post
    tagged "_thread_depth" for indentation). Cache key f"thread:{root_uri}".
    """

    SUPPORTS_CLEAR_CACHE = True

    def __init__(self, parent, root_uri, posts, target_index, origin_key=None):
        super().__init__(parent, posts, origin_key)
        self._rootUri = root_uri
        self.TAB_TEMP_TYPE = "thread"
        self.TAB_TEMP_KEY = root_uri
        self._targetUri = posts[target_index]["uri"] if posts and target_index < len(posts) else root_uri
        self.TAB_NAME = self._makeTabName(posts)
        # Set by a user rename (or restore); stops refresh from resetting the name.
        self._customName = None
        self._render(target_index=target_index)

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

    def onTabRenamed(self, newName):
        self._customName = newName
        super().onTabRenamed(newName)

    def _cacheKey(self):
        return f"thread:{self._rootUri}"

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
            db.delete_user_list_cache(self._account["id"], self._cacheKey())
        self._posts = []
        self._render()
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()

    def _statusText(self):
        # Translators: Thread tab status bar text. First {} is the tab name, second {} is the post count.
        return _("{} -- {} posts").format(self.TAB_NAME, len(self._posts))

    def _announceLoading(self):
        # Translators: Announced while (re)loading a whole thread.
        nvdaUi.message(_("Loading thread, please wait..."))

    def _fetchPosts(self, atprotoClient):
        posts, _targetIndex = client.get_thread(atprotoClient, self._targetUri)
        return posts

    def _afterRefresh(self):
        if not self._customName:
            self.TAB_NAME = self._makeTabName(self._posts)
        self._updateTitle()
        self._render()

    def _announceRefreshFailed(self, error):
        # Translators: Announced when refreshing a thread fails. {} is the error message.
        nvdaUi.message(_("Could not refresh thread: {}").format(error))

    def _announceLoaded(self):
        # Translators: Announced after a thread finishes loading. {} is the post count.
        nvdaUi.message(_("{} posts in thread.").format(len(self._posts)))


class QuotesTabWindow(PostBrowserTabWindow):
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
        super().__init__(parent, quotes, origin_key)
        self._targetUri = target_uri
        self.TAB_TEMP_TYPE = "quotes"
        self.TAB_TEMP_KEY = target_uri
        self.TAB_NAME = self._makeTabName()
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

    def _cacheKey(self):
        return f"quotes:{self._targetUri}"

    def _statusText(self):
        # Translators: Quotes tab status bar text. First {} is the tab name, second {} is the quote count.
        return _("{} -- {} quotes").format(self.TAB_NAME, len(self._posts))

    def _announceLoading(self):
        # Translators: Announced while loading a post's quotes.
        nvdaUi.message(_("Loading quotes, please wait..."))

    def _fetchPosts(self, atprotoClient):
        return client.get_post_quotes(atprotoClient, self._targetUri)

    def _announceRefreshFailed(self, error):
        # Translators: Announced when refreshing quotes fails. {} is the error message.
        nvdaUi.message(_("Could not refresh quotes: {}").format(error))

    def _announceLoaded(self):
        # Translators: Announced after quotes finish loading. {} is the count.
        nvdaUi.message(_("{} quotes.").format(len(self._posts)))

    def _extendPostMenu(self, menu, post):
        isMyOriginal = self._account and self._account.get("did") == self._getTargetAuthorDid()
        if isMyOriginal:
            menu.AppendSeparator()
            # Translators: Post action menu item, only offered on your own quoted post.
            self._addMenuItem(menu, _("&Detach my post from this quote..."), lambda: self._detachQuote(post))

    def _getTargetAuthorDid(self):
        # The quoted post itself isn't in self._posts (only the
        # quoting posts are) -- read it from the local cache instead.
        target = db.get_post(self._targetUri)
        return target.get("author_did") if target else None

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

        uiutil.start_worker(worker, progress=False)

    @uiutil.safe_ui_callback
    def _onDetachDone(self, quotingPost, error):
        if error:
            self._onActionDone(None, error)
            return
        for i, p in enumerate(self._posts):
            if p["uri"] == quotingPost["uri"]:
                del self._posts[i]
                self._render()
                if not self._posts:
                    uiutil.focus_check_updates_button(self)
                break
        # Translators: Announced after detaching your post from someone's quote.
        self._onActionDone(_("Detached."), None)
