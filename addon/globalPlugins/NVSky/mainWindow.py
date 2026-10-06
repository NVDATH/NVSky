"""
MainWindow for NVSky -- single top-level window holding every tab,
plus a persistent toolbar (Check for updates / New post / Settings /
Remove current tab / Close) that's shared across all tabs instead of
being duplicated in each panel.

Uses plain wx.Notebook, NOT wx.aui.AuiNotebook: the AUI notebook doesn't
expose per-tab accessibility properly (NVDA read all tab names
concatenated). Tabs are reordered with Ctrl+Shift+PageUp/PageDown and
removed with Ctrl+W (no per-tab close button).

Two kinds of tabs:
  - Permanent tabs (Home, Notifications, and future primary sections
    like Explore/Feeds/Lists/Saved): always present, Ctrl+W is a
    no-op on them.
  - Removable tabs (View Thread, View timeline, Show followers/
    following, and any future action-menu-spawned list view): opened
    on demand, can be left open and refreshed, and taken out of the
    notebook with Ctrl+W.

Each tab panel is expected to expose:
  - self.TAB_REMOVABLE: bool -- set automatically by addTab(), read by
    removeCurrentTab().
  - self.onCheckForUpdates(evt): refresh hook -- called by the
    toolbar's Check for updates button/F5 fallback for whichever tab
    is currently selected, and by Ctrl+F5's check-all-open-tabs loop
    for every open tab regardless of selection.
  - self.onTabActivated(): optional hook called whenever this tab
    becomes the selected page -- used to re-sync the title bar and
    restore this tab's own real keyboard focus/list position (see
    _restoreFocusPosition(moveFocus=...) in feedWindow.py).

Ctrl+Delete (clear the tab's cache) is handled by each tab itself.
"""
import json
import threading
import wx

import gui
import ui as nvdaUi
from logHandler import log

from . import db
from . import chatWindow
from . import uiutil
from . import soundpack
from .compose import ComposeDialog
from . import client

class MainWindow(wx.Frame):
    def __init__(self, parent):
        # Title starts plain; the first addTab() sets the real
        # "<tab name> - NVSky - <handle>" title and onPageChanged keeps it in sync.
        super().__init__(parent, title="NVSky", size=(900, 550))

        # Reset here so a second open in the same NVDA session isn't blocked.
        uiutil.app_closing = False

        # True until activateInitialTab(): skips onTabActivated() (which
        # speaks "<tab> tab") for the page-changed events fired while the
        # notebook is built, so "Home tab" isn't spoken on every open.
        self._activationSuppressed = True

        # Everything lives in one wx.Panel: a wx.Frame doesn't do dialog-style
        # Tab traversal, so Tab couldn't reach the toolbar.
        panel = wx.Panel(self)
        frameSizer = wx.BoxSizer(wx.VERTICAL)
        frameSizer.Add(panel, proportion=1, flag=wx.EXPAND)
        self.SetSizer(frameSizer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Persistent toolbar shared by every tab.
        toolbarSizer = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Toolbar button to sync the current tab. Shows the F5 shortcut.
        self.checkUpdatesButton = wx.Button(panel, label=_("Check for &updates (F5)"))
        # Translators: Toolbar button to compose a new post. Shows the Ctrl+N shortcut. Relabeled to "New chat.../New list..." in chat/lists context, see onPageChanged.
        self.newPostButton = wx.Button(panel, label=_("&New post... (Ctrl+N)"))
        # Translators: Toolbar button, only shown while a Lists tab is active, to find and subscribe to a list by link.
        self.findListsButton = wx.Button(panel, label=_("&Find lists by user..."))
        self.findListsButton.Hide()  # only shown while a Lists tab is active, see onPageChanged
        # Translators: Toolbar button to open NVSky's settings. Shows the Ctrl+P shortcut.
        self.settingsButton = wx.Button(panel, label=_("&Settings... (Ctrl+P)"))
        # Translators: Toolbar button to remove the current removable tab. Shows the Ctrl+W shortcut.
        self.removeTabButton = wx.Button(panel, label=_("&Remove current tab (Ctrl+W)"))
        # Translators: Toolbar button to close the NVSky window.
        self.closeButton = wx.Button(panel, label=_("&Close"))
        for button in (
            self.checkUpdatesButton, self.newPostButton, self.findListsButton, self.settingsButton,
            self.removeTabButton, self.closeButton,
        ):
            toolbarSizer.Add(button, flag=wx.RIGHT, border=5)
        sizer.Add(toolbarSizer, flag=wx.ALL, border=8)

        self.notebook = wx.Notebook(panel)
        sizer.Add(self.notebook, proportion=1, flag=wx.EXPAND)
        panel.SetSizer(sizer)

        self.checkUpdatesButton.Bind(wx.EVT_BUTTON, self.onCheckForUpdates)
        self.newPostButton.Bind(wx.EVT_BUTTON, self.onNewPost)
        self.findListsButton.Bind(wx.EVT_BUTTON, self.onFindLists)
        self.settingsButton.Bind(wx.EVT_BUTTON, self.onSettings)
        self.removeTabButton.Bind(wx.EVT_BUTTON, lambda evt: self.removeCurrentTab())
        self.closeButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())

        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.onPageChanging)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onPageChanged)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
        self.Bind(wx.EVT_CLOSE, self.onClose)

        self.CentreOnScreen()

    # ---------------- toolbar actions (shared across every tab) ----------------

    def onCheckForUpdates(self, evt=None):
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        panel = self.notebook.GetPage(index)
        onCheck = getattr(panel, "onCheckForUpdates", None)
        if callable(onCheck):
            onCheck(None)

    def onNewPost(self, evt=None):
        index = self.notebook.GetSelection()
        panel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        identity = self._getTabIdentity(panel) if panel is not None else None
        isChatContext = identity is not None and (
            (identity["kind"] == "permanent" and identity["key"] == "chat")
            or identity["kind"] == "conversation"
        )
        isListsContext = identity is not None and identity["kind"] == "permanent" and identity["key"] == "lists"
        if isChatContext:
            account = db.get_active_account()
            if account is None:
                # Translators: Announced when trying to start a new chat with no active account.
                nvdaUi.message(_("No active account."))
                return
            dlg = chatWindow.NewChatDialog(self, account, on_started=self._openChatConvo)
            dlg.Show()
            return
        if isListsContext:
            onAddList = getattr(panel, "onAddList", None)
            if callable(onAddList):
                onAddList()
            return
        dlg = ComposeDialog(self, onClosed=None)
        dlg.Show()

    def _openChatConvo(self, convoId):
        # Switch to the Chat tab and select this conversation. onTabActivated
        # is suppressed: it also reloads and reselects, and two overlapping
        # TreeCtrl rebuilds crashed NVDA. One reload+select below instead.
        for i in range(self.notebook.GetPageCount()):
            candidatePanel = self.notebook.GetPage(i)
            if getattr(candidatePanel, "TAB_KEY", None) == "chat":
                wasSuppressed = self._activationSuppressed
                self._activationSuppressed = True
                try:
                    self.notebook.SetSelection(i)
                finally:
                    self._activationSuppressed = wasSuppressed
                reload = getattr(candidatePanel, "_loadFromCache", None)
                if callable(reload):
                    reload()
                selectConvo = getattr(candidatePanel, "_selectConvoById", None)
                if callable(selectConvo):
                    selectConvo(convoId)
                return

    def onSettings(self, evt=None):
        from . import open_settings_dialog
        open_settings_dialog()

    # ---------------- tab management ----------------

    def addTab(self, panel, label, select=True, removable=True, play_sound=True):
        """
        Adds `panel` (constructed with self.notebook as parent) as a tab.
        removable=False marks a permanent tab (Ctrl+W is a no-op); it has
        nothing to do with closing the window. play_sound=False is for
        restoring temp tabs at startup.
        """
        panel.TAB_REMOVABLE = removable
        if removable and play_sound:
            soundpack.play("open_tab")
        self.notebook.AddPage(panel, label, select)
        self._updateRemoveTabButton()

        # The panel's own _updateTitle ran before it was a page; re-run it now.
        refreshTitle = getattr(panel, "_updateTitle", None)
        if callable(refreshTitle):
            refreshTitle()

        if select:
            # AddPage(select=True) doesn't move real focus; this is the only
            # place it's grabbed for the initial tab, once layout settles.
            wx.CallAfter(self._focusPanel, panel)

    @uiutil.safe_ui_callback
    def _focusPanel(self, panel):
        if getattr(self, "_noFocusGrab", False):
            return
        restoreFocus = getattr(panel, "_restoreFocusPosition", None)
        if callable(restoreFocus) and getattr(panel, "_account", None) is not None:
            restoreFocus()
        else:
            panel.SetFocus()

    def getOpenTabs(self):
        return [self.notebook.GetPage(i) for i in range(self.notebook.GetPageCount())]

    def moveCurrentTab(self, delta):
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        newIndex = index + delta
        if newIndex < 0 or newIndex >= self.notebook.GetPageCount():
            # Translators: Announced when trying to move a tab past the first/last position.
            nvdaUi.message(_("Can't move the tab further in that direction."))
            return
        panel = self.notebook.GetPage(index)
        label = self.notebook.GetPageText(index)
        # RemovePage and InsertPage each fire page-changed (two announcements):
        # suppress both, then activate once.
        self._activationSuppressed = True
        try:
            self.notebook.RemovePage(index)
            self.notebook.InsertPage(newIndex, panel, label, select=True)
        finally:
            self._activationSuppressed = False
        onActivated = getattr(panel, "onTabActivated", None)
        if callable(onActivated):
            onActivated()
        self._persistTabOrder()

    def _persistTabOrder(self):
        # One combined, interleaved order for every tab (permanent AND
        # temp together) -- see db.get_tab_order's docstring for why
        # this replaced the old split scheme.
        account = db.get_active_account()
        if account is None:
            return
        order = []
        for i in range(self.notebook.GetPageCount()):
            identity = self._getTabIdentity(self.notebook.GetPage(i))
            if identity:
                order.append([identity["kind"], identity["key"]])
        db.set_tab_order(account["id"], order)

    def notifyConvoChanged(self, convoId, sourcePanel=None):
        # Cross-tab live sync after a local chat change: every OTHER open panel
        # re-reads the fresh local DB (no network).
        for panel in self.getOpenTabs():
            if panel is sourcePanel:
                continue
            reload = getattr(panel, "_reloadMessagesIfCurrent", None)
            if reload:
                try:
                    reload(convoId, moveFocus=False)
                except TypeError:
                    # ChatWindow's version has no moveFocus param (it never grabs focus).
                    reload(convoId)
            refreshLabel = getattr(panel, "_refreshConvoLabel", None)
            if refreshLabel:
                refreshLabel(convoId)

    def _updateRemoveTabButton(self):
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            self.removeTabButton.Hide()
        else:
            panel = self.notebook.GetPage(index)
            self.removeTabButton.Show(getattr(panel, "TAB_REMOVABLE", True))
        self.removeTabButton.GetContainingSizer().Layout()

    def _getTabIdentity(self, panel):
        """
        Tab identity {"kind", "key"}: a permanent tab's TAB_KEY, or a temp
        tab's TAB_TEMP_TYPE/TAB_TEMP_KEY (matching db.get_open_temp_tabs
        entries). Used to remember the last tab and persist order/renames.
        None if neither is set.
        """
        tabKey = getattr(panel, "TAB_KEY", None)
        if tabKey:
            return {"kind": "permanent", "key": tabKey}
        tempType = getattr(panel, "TAB_TEMP_TYPE", None)
        tempKey = getattr(panel, "TAB_TEMP_KEY", None)
        if tempType and tempKey is not None:
            return {"kind": tempType, "key": tempKey}
        return None

    def onPageChanging(self, evt):
        # Focus the notebook before the swap: a focused control being hidden
        # made Windows move focus into the new page too, doubling NVDA's
        # announcement (Ctrl+Tab/Ctrl+number only).
        self.notebook.SetFocus()
        evt.Skip()

    def onPageChanged(self, evt):
        index = evt.GetSelection()
        if index != wx.NOT_FOUND:
            panel = self.notebook.GetPage(index)
            refreshTitle = getattr(panel, "_updateTitle", None)
            if callable(refreshTitle):
                refreshTitle()
            pageIdentity = self._getTabIdentity(panel)
            isChatContext = pageIdentity is not None and (
                (pageIdentity["kind"] == "permanent" and pageIdentity["key"] == "chat")
                or pageIdentity["kind"] == "conversation"
            )
            isListsContext = pageIdentity is not None and pageIdentity["kind"] == "permanent" and pageIdentity["key"] == "lists"
            self.findListsButton.Show(isListsContext)
            if isChatContext:
                # Translators: Toolbar button relabeled while a Chat tab is active. Shows the Ctrl+N shortcut.
                self.newPostButton.SetLabel(_("&New chat... (Ctrl+N)"))
            elif isListsContext:
                # Translators: Toolbar button relabeled while a Lists tab is active. Shows the Ctrl+N shortcut.
                self.newPostButton.SetLabel(_("&New list... (Ctrl+N)"))
            else:
                # Translators: Toolbar button to compose a new post. Shows the Ctrl+N shortcut.
                self.newPostButton.SetLabel(_("&New post... (Ctrl+N)"))
            if not self._activationSuppressed:
                onActivated = getattr(panel, "onTabActivated", None)
                if callable(onActivated):
                    onActivated()
                # Remember the last-active tab (permanent or temp) per account.
                identity = self._getTabIdentity(panel)
                if identity:
                    account = db.get_active_account()
                    if account is not None:
                        db.set_ui_state(f"last_active_tab:{account['id']}", json.dumps(identity))
        self._updateRemoveTabButton()
        evt.Skip()

    def activateInitialTab(self, index):
        """
        Called once after every startup tab has been added: selects the
        remembered tab silently (no spoken tab name).
        """
        if index != self.notebook.GetSelection():
            self.notebook.SetSelection(index)  # still suppressed here -- silent
        else:
            # Already the selected page (e.g. Home, page 0's automatic
            # selection) -- SetSelection() would be a no-op and never
            # fire PAGE_CHANGED, so refresh the title directly this once.
            panel = self.notebook.GetPage(index)
            refreshTitle = getattr(panel, "_updateTitle", None)
            if callable(refreshTitle):
                refreshTitle()
        self._activationSuppressed = False
        self._updateRemoveTabButton()
        wx.CallAfter(self._focusPanel, self.notebook.GetPage(index))

    def focusTabByIdentity(self, identity):
        """
        Dedup helper for opening a "browse view" tab (thread, user
        timeline, followers/following) that shouldn't be opened twice
        for the same target. Returns True and switches to it if a tab
        with this exact identity is already open; False otherwise
        (caller should then create it). Unlike findTabIndexByIdentity,
        this has NO fallback to index 0 -- a miss must stay a miss.
        """
        if not identity:
            return False
        for i in range(self.notebook.GetPageCount()):
            if self._getTabIdentity(self.notebook.GetPage(i)) == identity:
                self.notebook.SetSelection(i)
                return True
        return False

    def findTabIndexByIdentity(self, identity):
        """
        `identity` is whatever _getTabIdentity() returns (or the
        json.loads() of a previously-stored one -- see
        GlobalPlugin.script_openFeed() in __init__.py). Matches against
        every currently-open page (permanent tabs added this startup,
        plus temp tabs already reconstructed from
        db.get_open_temp_tabs() before this is called). Falls back to
        Home (index 0) if nothing matches or identity is None/stale.
        """
        if not identity:
            return 0
        for i in range(self.notebook.GetPageCount()):
            if self._getTabIdentity(self.notebook.GetPage(i)) == identity:
                return i
        return 0
    def removeCurrentTab(self):
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        panel = self.notebook.GetPage(index)
        if not getattr(panel, "TAB_REMOVABLE", True):
            # Translators: Announced when Ctrl+W is pressed on a permanent (non-removable) tab.
            nvdaUi.message(_("This tab can't be removed."))
            return
        onRemoved = getattr(panel, "onTabRemoved", None)
        if callable(onRemoved):
            onRemoved()
        soundpack.play("close_tab")
        self.notebook.DeletePage(index)

    def renameCurrentTab(self):
        # Changes only TAB_NAME; permanent tabs can't be renamed (same as Ctrl+W).
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        panel = self.notebook.GetPage(index)
        if not getattr(panel, "TAB_REMOVABLE", True):
            # Translators: Announced when Ctrl+Shift+F2 is pressed on a permanent (non-renameable) tab.
            nvdaUi.message(_("This tab can't be renamed."))
            return
        currentName = getattr(panel, "TAB_NAME", self.notebook.GetPageText(index))
        dlg = wx.TextEntryDialog(
            # Translators: Prompt in the rename-tab dialog.
            # Translators: Title of the rename-tab dialog.
            self, _("New tab name:"), _("Rename tab"), value=currentName,
        )
        if dlg.ShowModal() == wx.ID_OK:
            newName = dlg.GetValue().strip()
            if newName:
                panel.TAB_NAME = newName
                refreshTitle = getattr(panel, "_updateTitle", None)
                if callable(refreshTitle):
                    refreshTitle()
                # Each temp tab class persists its own rename.
                onRenamed = getattr(panel, "onTabRenamed", None)
                if callable(onRenamed):
                    onRenamed(newName)
                # Translators: Announced after renaming a tab. {} is the new name.
                nvdaUi.message(_("Tab renamed to {}.").format(newName))
        dlg.Destroy()

    def onFindLists(self, evt=None):
        index = self.notebook.GetSelection()
        panel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        onFindLists = getattr(panel, "onSubscribeViaLink", None)
        if callable(onFindLists):
            onFindLists()

    def checkAllOpenTabs(self):
        # One shared thread syncs tabs sequentially (concurrent logins raced
        # on session refresh) and one summary is spoken at the end. Every tab
        # with new data is reloaded, not just the visible one (moveFocus=False
        # so a background reload can't steal focus).
        panels = [p for p in self.getOpenTabs() if callable(getattr(p, "_syncForBulkCheck", None))]
        if not panels:
            return
        # Translators: Announced while checking every open tab for updates (Ctrl+F5).
        nvdaUi.message(_("Checking all open tabs for updates, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
            except Exception as e:
                wx.CallAfter(self._onCheckAllOpenTabsDone, [], str(e))
                return
            updatedPanels = []
            for panel in panels:
                try:
                    if panel._syncForBulkCheck(atprotoClient):
                        updatedPanels.append(panel)
                except Exception as e:
                    log.error(f"NVSky: checkAllOpenTabs sync failed for a tab: {e}")
            wx.CallAfter(self._onCheckAllOpenTabsDone, updatedPanels, None)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onCheckAllOpenTabsDone(self, updatedPanels, error):
        if error:
            log.error(f"NVSky: checkAllOpenTabs failed: {error}")
            # Translators: Announced when checking every open tab for updates fails. {} is the error message.
            nvdaUi.message(_("Could not check for updates: {}").format(error))
            return
        index = self.notebook.GetSelection()
        activePanel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        for panel in updatedPanels:
            reload = getattr(panel, "_reloadAfterBulkCheck", None)
            if callable(reload):
                reload(moveFocus=(panel is activePanel))
        # The active tab reloads even with nothing new (e.g. its first render
        # after login was against an empty cache).
        if activePanel is not None and activePanel not in updatedPanels:
            reload = getattr(activePanel, "_reloadAfterBulkCheck", None)
            if callable(reload):
                reload(moveFocus=True)
        if updatedPanels:
            soundpack.play("ready")
            # Translators: Fallback tab name when a panel has none set.
            names = [getattr(p, "TAB_NAME", _("a tab")) for p in updatedPanels]
            # Translators: Announced after checking every open tab for updates finds changes. {} is a comma-separated list of tab names.
            nvdaUi.message(_("Updates in: {}.").format(", ".join(names)))
        else:
            # Translators: Announced after checking every open tab for updates finds nothing new.
            nvdaUi.message(_("No new updates in any open tab."))

    # ---------------- window-level keyboard shortcuts ----------------

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()

        # Escape/Alt+F4/Close close the whole window; Ctrl+W only removes the current (removable) tab.
        if keyCode == wx.WXK_ESCAPE:
            focused = self.FindFocus()
            if isinstance(focused, wx.TextCtrl) and focused.IsMultiLine() and focused.GetValue().strip():
                return
            self.Close()
            return
        if evt.ControlDown() and keyCode == ord("W"):
            self.removeCurrentTab()
            return
        if evt.ControlDown() and keyCode == ord("P"):
            self.onSettings()
            return
        if evt.ControlDown() and keyCode == wx.WXK_F2:
            self.renameCurrentTab()
            return
        if evt.ControlDown() and ord("1") <= keyCode <= ord("9"):
            index = keyCode - ord("1")
            if index < self.notebook.GetPageCount():
                self.notebook.SetSelection(index)
            return
        if evt.ControlDown() and keyCode == wx.WXK_F5:
            self.checkAllOpenTabs()
            return
        # F5/Ctrl+N fallbacks for when focus is outside a panel (panels handle
        # them themselves). Shift+F5 is panel-only ("load older").
        if keyCode == wx.WXK_F5 and not evt.ShiftDown():
            self.onCheckForUpdates()
            return
        if evt.ControlDown() and keyCode == ord("N"):
            self.onNewPost()
            return
        if evt.ControlDown() and evt.ShiftDown() and keyCode == wx.WXK_PAGEUP:
            self.moveCurrentTab(-1)
            return
        if evt.ControlDown() and evt.ShiftDown() and keyCode == wx.WXK_PAGEDOWN:
            self.moveCurrentTab(1)
            return

        evt.Skip()

    def onClose(self, evt):
        # Set FIRST: queued safe_ui_callback completions skip touching wx once closing.
        uiutil.app_closing = True

        # wx.Timer isn't part of the destroy cascade and would keep firing into
        # destroyed panels; stop every timer instance on each panel (found by
        # type, so new timers need no update here).
        for i in range(self.notebook.GetPageCount()):
            panel = self.notebook.GetPage(i)
            for value in vars(panel).values():
                if isinstance(value, wx.Timer):
                    value.Stop()
        self.Destroy()
