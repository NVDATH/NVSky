"""
ItemActionMixin for NVSky: the shared Post action menu (Alt+A) and its
handlers, used by every post-list tab.

feedWindow is imported as a module (not by name) because it imports this
file; its helpers are only touched inside method bodies.
"""
import datetime
import json
import threading

import wx

import gui
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import soundpack
from . import uiutil
from . import feedWindow
from .compose import ComposeDialog


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
        feedWindow._announce_now(message)

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
        feedWindow.propagate_post_state(post)
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
        feedWindow.propagate_post_state(post)
        if not post.get("viewer_bookmarked"):
            # Always drop the "saved" cache row, whichever tab unsaved it.
            db.delete_feed_item(self._account["id"], "saved", post["uri"])
            self._refreshOtherSavedTabs(post)
        # Only tabs listing by saved status (SavedWindow) define this hook.
        onBookmarkChanged = getattr(self, "_onBookmarkChanged", None)
        if callable(onBookmarkChanged):
            onBookmarkChanged(post)

    def _refreshOtherSavedTabs(self, post, tabKey="saved"):
        # Live-updates an open Saved/Likes tab when the change came from another tab.
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
        feedWindow._announce_now(_("Post text copied to clipboard."))

    def _copyPostLink(self, post):
        handle = post.get("handle") or post.get("author_did")
        rkey = post["uri"].rsplit("/", 1)[-1]
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        self._copyToClipboard(url)
        # Translators: Announced after copying a post's URL to the clipboard.
        feedWindow._announce_now(_("Post URL copied to clipboard."))

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
        feedWindow.propagate_post_state(post)
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
        feedWindow._announce_now(_("Post hidden."))

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

        # Optimistic: remove from the list right away instead of
        # waiting on the network round trip -- matches Like/Repost/
        # Bookmark/Mute thread's convention. Rolled back on failure.
        previousIndex = next((i for i, p in enumerate(self._posts) if p["uri"] == post["uri"]), None)
        if previousIndex is not None:
            del self._posts[previousIndex]
            self._render()
            if self._posts:
                newIndex = min(previousIndex, len(self._posts) - 1)
                self.postList.Focus(newIndex)
                self.postList.Select(newIndex)
        soundpack.play("delete")
        # Translators: Announced after deleting a post.
        nvdaUi.message(_("Post deleted."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.delete_post(atprotoClient, post["uri"])
                db.delete_post(post["uri"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onDeletePostDone, post, previousIndex, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onDeletePostDone(self, post, previousIndex, error):
        if not error:
            return
        log.error(f"NVSky: delete post failed: {error}")
        soundpack.play("error")
        # Roll back the optimistic removal -- the server never actually
        # deleted it, so leaving it gone from the list would be a lie.
        insertAt = previousIndex if previousIndex is not None and previousIndex <= len(self._posts) else len(self._posts)
        self._posts.insert(insertAt, post)
        self._render()
        # Translators: Announced when deleting a post fails. {} is the error message.
        nvdaUi.message(_("Delete failed: {}").format(error))

    def _togglePinToProfile(self, post):
        # Pinned state isn't cached locally; check the server first.
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
        feedWindow._announce_now(_("Loading quotes, please wait..."))
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
        feedWindow._announce_now(_("Loading current reply settings, please wait..."))

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
        # Tab dedup is by thread ROOT uri, known only after the fetch
        # (see _onThreadFetchedForOpen).
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        # Translators: Announced while loading a whole thread.
        feedWindow._announce_now(_("Loading thread, please wait..."))
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
            feedWindow._announce_now(_("Please wait..."))
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
        feedWindow.propagate_post_state(post)
        onRepostChanged = getattr(self, "_onRepostChanged", None)
        if callable(onRepostChanged):
            onRepostChanged(post)

    def _reportPost(self, post):
        # NOTE: was "for label, _ in REPORT_REASONS" -- bare `_` shadowed
        # gettext within this function's scope. Renamed to avoid that trap.
        labels = [label for label, _reasonCode in feedWindow.REPORT_REASONS]
        # Translators: Prompt in the report-post reason picker.
        # Translators: Title of the report-post reason picker.
        dlg = wx.SingleChoiceDialog(self, _("Reason for reporting this post:"), _("Report post"), labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        reasonType = feedWindow.REPORT_REASONS[dlg.GetSelection()][1]
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
            feedWindow._announce_now(_("Marked {} posts as read.").format(len(posts)))
        else:
            # Translators: Announced after marking several posts unread. {} is the count.
            feedWindow._announce_now(_("Marked {} posts as unread.").format(len(posts)))

    def _getRelevantUsers(self, post):
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