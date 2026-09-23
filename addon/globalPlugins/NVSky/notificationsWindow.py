"""
Notifications tab for NVSky.

Split out of feedWindow.py (see plan-17.md) -- structurally identical
FeedListMixin host to FeedWindow/SavedWindow/ListsWindow, just backed
by the `notifications` table instead of posts/feed_items, since a
notification isn't shaped like a post.
"""
import wx

import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from .feedWindow import (
    FeedListMixin,
    ItemActionMixin,
    UserActionMixin,
    EmbedViewMixin,
    _format_post_time,
    _announce_now,
    _post_web_url,
    _compose_copy_text,
)


def _describe_notification(notif: dict) -> str:
    reason = notif.get("reason")
    subjectText = (notif.get("subject_text") or "").replace("\n", " ").strip()
    label = {
        # Translators: Notification-type label.
        "like": _("Liked"),
        # Translators: Notification-type label.
        "repost": _("Reposted"),
        # Translators: Notification-type label.
        "follow": _("Followed you"),
        # Translators: Notification-type label.
        "reply": _("Replied"),
        # Translators: Notification-type label.
        "mention": _("Mentioned you"),
        # Translators: Notification-type label.
        "quote": _("Quoted"),
    # Translators: Fallback notification-type label when the reason is unrecognized.
    }.get(reason, reason or _("Notification"))

    if reason == "follow":
        return label
    if subjectText:
        # Translators: Notification row combining its type and the related post's text. First {} is the type label, second {} is the post text.
        return _("{}: {}").format(label, subjectText)
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
        # Translators: Permanent tab label for Notifications.
        self.TAB_NAME = _("Notifications")
        self._feedKey = "notifications"
        self._initFeedListState()

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        # Translators: Column header for a notification's author.
        self.postList.InsertColumn(0, _("Author"), width=180)
        # Translators: Column header for a notification's content.
        self.postList.InsertColumn(1, _("Notification"), width=400)
        # Translators: Column header for when a notification was received.
        self.postList.InsertColumn(2, _("Received"), width=140)
        sizer.Add(self.postList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. Shows the Alt+A shortcut.
        self.postActionButton = wx.Button(self, label=_("&Post action... (Alt+A)"))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
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
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onItemActivated)
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._loadFromCache(reset=True)

        if self._account is None:
            # Translators: Announced when opening a tab with no active account.
            nvdaUi.message(_("No active account. Log in from Settings first."))
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
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
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
            # Translators: Announced when the user-action menu is invoked with no notification focused.
            nvdaUi.message(_("No notification selected."))
            return
        self.showUserActionMenu(notif["author_did"], notif["handle"], notif.get("display_name"))

    def _copyFocusedPostRow(self):
        notif = self._getFocusedPost()
        if notif is None:
            return
        parts = [
            self._authorLabel(notif, self._currentAuthorMode()),
            _describe_notification(notif),
            _format_post_time(notif.get("indexed_at")),
        ]
        url = _post_web_url(notif.get("subject_uri"), notif.get("subject_author_handle"))
        uiutil.copy_text_to_clipboard(_compose_copy_text(parts, url))

    def onClearCache(self, evt=None):
        if self._account is None:
            # Translators: Announced when clearing cache with no active account.
            nvdaUi.message(_("No active account."))
            return
        confirm = wx.MessageDialog(
            # Translators: Confirmation to clear all cached notifications.
            # Translators: Title of the clear-cache confirmation dialog.
            self, _("Clear all cached notifications? This can't be undone."),
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        db.clear_notifications_cache(self._account["id"])
        self._loadFromCache(reset=True)
        if self._posts:
            self.postList.SetFocus()
            return
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()

    # ---------------- post action menu (Alt+A) ----------------

    def _getActionablePost(self):
        notif = self._getFocusedPost()
        if notif is None:
            # Translators: Announced when the post-action menu is invoked with no notification focused.
            nvdaUi.message(_("No notification selected."))
            return None

        subjectUri = notif.get("subject_uri")
        if not subjectUri:
            if notif.get("reason") == "follow":
                # Translators: Announced when Post action is invoked on a follow notification, which has no post.
                nvdaUi.message(_("Follow notifications have no post -- try User action (Alt+U) instead."))
            else:
                # Translators: Announced when a notification's post isn't available at all.
                nvdaUi.message(_("This notification's post isn't available -- try Check for updates (F5) to resync."))
            return None

        post = db.get_post(subjectUri)
        if post is None:
            # Translators: Announced when a notification's post exists but isn't cached locally yet.
            nvdaUi.message(_("This notification's post isn't cached yet -- try Check for updates (F5) to resync."))
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
        # Translators: Bulk mark-read menu item. {} is the selected count.
        self._addMenuItem(markMenu, _("Read ({} selected)").format(selectedCount),
                           lambda: self._markSelectedNotificationsRead(True))
        # Translators: Bulk mark-unread menu item. {} is the selected count.
        self._addMenuItem(markMenu, _("Unread ({} selected)").format(selectedCount),
                           lambda: self._markSelectedNotificationsRead(False))
        # Translators: Post action submenu label.
        menu.AppendSubMenu(markMenu, _("Mar&k as..."))
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
        # Translators: Announced after marking notifications read.
        # Translators: Announced after marking notifications unread.
        _announce_now(_("Marked as read.") if read else _("Marked as unread."))

    # onCharHook is inherited from FeedListMixin (see SUPPORTS_* flags
    # above). This also fixes a real bug: this class's old onCharHook
    # never had the "Ctrl+F5 bubbles up to MainWindow" guard the other
    # 4 tabs have, so Ctrl+F5 here was silently doing a single-tab F5
    # sync instead of MainWindow.checkAllOpenTabs' full sweep.