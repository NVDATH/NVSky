"""
Full post dialog for NVSky (Ctrl+Space in every post list): the whole text of a post plus a list of
its details -- links, address, creation time and interaction counts.

The counts shown first are the cached ones. The quote and bookmark counts are not cached, so the
dialog fetches fresh numbers as soon as it opens, and again on the check-for-updates key.
"""
import webbrowser

import wx

import gui
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import keymap
from . import uiutil

COUNT_KEYS = ("replies", "likes", "reposts", "quotes", "bookmarks")


def _count_line(key, value):
    shown = "?" if value is None else value
    if key == "replies":
        # Translators: Detail line of the full-post dialog. {} is how many replies the post has.
        return _("replies: {}").format(shown)
    if key == "likes":
        # Translators: Detail line of the full-post dialog. {} is how many likes the post has.
        return _("likes: {}").format(shown)
    if key == "reposts":
        # Translators: Detail line of the full-post dialog. {} is how many reposts the post has.
        return _("reposts: {}").format(shown)
    if key == "quotes":
        # Translators: Detail line of the full-post dialog. {} is how many quote posts the post has.
        return _("quotes: {}").format(shown)
    # Translators: Detail line of the full-post dialog. {} is how many times the post was saved.
    return _("bookmarks: {}").format(shown)


class PostDetailsDialog(wx.Dialog):
    """
    text: the full text. rows: [(line, link or None)] listed before the counts. post: a post dict
    (uri and cached counts) whose details are listed, or None for a text-only dialog.
    """

    def __init__(self, parent, title, text, rows=None, post=None):
        super().__init__(parent, title=title, size=(560, 520), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._rows = list(rows or [])
        self._post = post
        self._postUri = post["uri"] if post else None
        self._counts = {}
        if post:
            self._counts = {
                "replies": post.get("reply_count"), "likes": post.get("like_count"),
                "reposts": post.get("repost_count"), "quotes": post.get("quote_count"),
                "bookmarks": post.get("bookmark_count"),
            }
        self._refreshing = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label above the full text in the full-post dialog.
        textLabel = wx.StaticText(self, label=_("&Full text:"))
        sizer.Add(textLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.textCtrl = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        if self._postUri:
            # Translators: Label above the details list in the full-post dialog.
            detailsLabel = wx.StaticText(self, label=_("&Details (Enter opens a link):"))
            sizer.Add(detailsLabel, flag=wx.LEFT | wx.RIGHT, border=10)
            self.detailsList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER)
            self.detailsList.InsertColumn(0, "", width=500)
            sizer.Add(self.detailsList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

            # Translators: Button to copy the focused detail line. The current shortcut is appended automatically.
            self.copyButton = wx.Button(self, label=_("&Copy line") + keymap.hint("copy_row"))
            # Translators: Button to reload the interaction counts. The current shortcut is appended automatically.
            self.refreshButton = wx.Button(self, label=_("&Refresh details") + keymap.hint("check_updates"))
            buttonRow.Add(self.copyButton, flag=wx.RIGHT, border=5)
            buttonRow.Add(self.refreshButton, flag=wx.RIGHT, border=5)
            self.copyButton.Bind(wx.EVT_BUTTON, lambda e: self._copyLine())
            self.refreshButton.Bind(wx.EVT_BUTTON, lambda e: self._startRefresh(announce=True))
            self.detailsList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onActivate)
            self._renderRows()

        # Translators: Button to close the full-post dialog. Mnemonic on "l" since Copy line already claims "C".
        closeBtn = wx.Button(self, label=_("C&lose"))
        buttonRow.Add(closeBtn)
        sizer.Add(buttonRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.textCtrl.SetFocus()
        if self._postUri:
            self._startRefresh(announce=False)

    # ---------------- details list ----------------

    def _renderRows(self):
        self._lineUrls = [url for _line, url in self._rows] + [None] * len(COUNT_KEYS)
        self.detailsList.Freeze()
        try:
            self.detailsList.DeleteAllItems()
            index = 0
            for line, _url in self._rows:
                self.detailsList.InsertItem(index, line)
                index += 1
            for key in COUNT_KEYS:
                self.detailsList.InsertItem(index, _count_line(key, self._counts.get(key)))
                index += 1
            self.detailsList.Focus(0)
            self.detailsList.Select(0)
        finally:
            self.detailsList.Thaw()

    def onActivate(self, evt):
        index = evt.GetIndex()
        url = self._lineUrls[index] if 0 <= index < len(self._lineUrls) else None
        if not url:
            # Translators: Announced when Enter is pressed on a detail line that is not a link.
            nvdaUi.message(_("This line has no link."))
            return
        # Web links only -- never hand an arbitrary URI scheme to the shell.
        if not url.lower().startswith(("http://", "https://")):
            # Translators: Announced when a post's link uses a scheme other than http/https and is refused.
            nvdaUi.message(_("Only http and https links can be opened."))
            return
        webbrowser.open(url)

    def _copyLine(self):
        uiutil.copy_focused_row(self.detailsList)

    # ---------------- counts ----------------

    def _startRefresh(self, announce):
        if self._refreshing or not self._postUri:
            return
        self._refreshing = True
        if announce:
            # Translators: Announced while reloading the details of a post.
            nvdaUi.message(_("Refreshing details, please wait..."))
        uri = self._postUri

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                counts = client.get_post_counts(atprotoClient, uri)
                error = None
            except Exception as e:
                counts = None
                error = str(e)
            wx.CallAfter(self._onRefreshDone, counts, error, announce)

        uiutil.start_worker(worker, progress=announce)

    @uiutil.safe_ui_callback
    def _onRefreshDone(self, counts, error, announce):
        self._refreshing = False
        if error:
            # Translators: Announced when reloading a post's details fails. {} is the error message.
            nvdaUi.message(_("Could not refresh details: {}").format(error))
            return
        self._counts.update(counts)
        base = len(self._rows)
        for offset, key in enumerate(COUNT_KEYS):
            self.detailsList.SetItem(base + offset, 0, _count_line(key, self._counts.get(key)))
        if announce:
            # Translators: Announced after a post's details were reloaded.
            nvdaUi.message(_("Details refreshed."))
        try:
            db.update_post_counts(self._postUri, self._counts)
            for key, column in (
                ("replies", "reply_count"), ("likes", "like_count"), ("reposts", "repost_count"),
                ("quotes", "quote_count"), ("bookmarks", "bookmark_count"),
            ):
                if self._counts.get(key) is not None:
                    self._post[column] = self._counts[key]
        except Exception as e:
            log.error(f"NVSky: saving post counts failed: {e}")

    # ---------------- keyboard ----------------

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        if self._postUri:
            if keymap.matches(evt, "check_updates"):
                self._startRefresh(announce=True)
                return
            # Elsewhere Ctrl+C keeps copying the selected text.
            if keymap.matches(evt, "copy_row") and self.FindFocus() is self.detailsList:
                self._copyLine()
                return
        evt.Skip()


def show_post_details(parent, title, text, rows=None, post=None):
    gui.mainFrame.prePopup()
    dlg = PostDetailsDialog(parent, title, text, rows, post)
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()
        gui.mainFrame.postPopup()
