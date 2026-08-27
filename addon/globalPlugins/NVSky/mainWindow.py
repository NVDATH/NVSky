"""
MainWindow for NVSky -- single top-level window holding every tab,
plus a persistent toolbar (Check for updates / New post / Settings /
Remove current tab / Close) that's shared across all tabs instead of
being duplicated in each panel.

Uses plain wx.Notebook, NOT wx.aui.AuiNotebook -- the AUI notebook
doesn't expose per-tab accessibility properly (NVDA read both tab
names concatenated together, "HomeNotifications", instead of one tab
at a time), while wx.Notebook is a standard, well-supported control
("Home tab selected" reads cleanly, matching YoutubePlus's own tab
UI). No drag-to-reorder tabs as a result -- not needed; reordering
will be a command instead (same approach YoutubePlus already uses),
not implemented yet. No built-in per-tab close button either (core
wx.aui.AuiNotebook didn't properly support that anyway) -- Ctrl+W /
removeCurrentTab() is the only way to take a tab out.

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

NOTE: Ctrl+Delete ("clear current tab's cache") is intentionally NOT
implemented yet -- it needs a new db.clear_feed_cache(account_id,
feed_key) plus a notifications equivalent that don't exist yet. Add it
here once those land.
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
from .compose import ComposeDialog
from . import client

class MainWindow(wx.Frame):
    def __init__(self, parent):
        # Title starts plain -- the first tab added via addTab() calls
        # panel._updateTitle(), which immediately sets the real
        # "<tab name> - NVSky - <handle>" title since it's the
        # initially-selected tab. See _updateTitle() in feedWindow.py's
        # FeedListMixin and onPageChanged() below, which keeps the title
        # in sync as the user switches tabs.
        super().__init__(parent, title="NVSky", size=(900, 550))

        # Reset here (not just set True in onClose below) so a second
        # NVSky open within the same NVDA session isn't permanently
        # blocked by the previous session's shutdown flag.
        uiutil.app_closing = False

        # True until activateInitialTab() flips it off once startup has
        # picked and focused the remembered tab -- while True, onPageChanged
        # below still updates the tab-strip/window title but skips calling
        # onTabActivated() (which is what speaks "<tab> tab" and re-runs
        # each panel's own activation logic). Without this, every
        # permanent tab's addTab()/AddPage() -- and wx.Notebook
        # auto-selecting the very first page added to an empty notebook,
        # regardless of the select argument -- fires a real
        # EVT_NOTEBOOK_PAGE_CHANGED during construction, so "Home tab" got
        # spoken every single time NVSky opened, before the window was
        # even shown.
        self._activationSuppressed = True

        # Everything lives inside one wx.Panel rather than being parented
        # directly to the Frame -- wx.Frame does NOT apply dialog-style
        # Tab traversal across its direct children the way wx.Panel (and
        # wx.Dialog, which FeedWindow/NotificationsWindow used to be)
        # does. Without this wrapper, Tab only cycled within the
        # notebook's own internal group (tab strip/list/action buttons)
        # and could never reach the toolbar at all.
        panel = wx.Panel(self)
        frameSizer = wx.BoxSizer(wx.VERTICAL)
        frameSizer.Add(panel, proportion=1, flag=wx.EXPAND)
        self.SetSizer(frameSizer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Persistent toolbar -- shared across every tab instead of each
        # panel duplicating its own Check for updates/New post buttons
        # (which is what FeedWindow/NotificationsWindow used to do).
        toolbarSizer = wx.BoxSizer(wx.HORIZONTAL)
        self.checkUpdatesButton = wx.Button(panel, label="Check for updates (F5)")
        self.newPostButton = wx.Button(panel, label="New post... (Ctrl+N)")
        self.findListsButton = wx.Button(panel, label="Find lists by user...")
        self.findListsButton.Hide()  # only shown while a Lists tab is active, see onPageChanged
        self.settingsButton = wx.Button(panel, label="Settings...")
        self.removeTabButton = wx.Button(panel, label="Remove current tab (Ctrl+W)")
        self.closeButton = wx.Button(panel, label="Close")
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
                nvdaUi.message("No active account.")
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
        # Switch to the permanent Chat tab and select this conversation
        # -- called once NewChatDialog resolves/creates it.
        #
        # CRASH FIX: SetSelection() below fires a real
        # EVT_NOTEBOOK_PAGE_CHANGED, which (unless suppressed) calls
        # ChatWindow.onTabActivated() -- which ALSO does its own
        # _loadFromCache() + reselect. Previously this method then
        # immediately did a SECOND, separate _loadFromCache/select pass
        # right after -- two overlapping rebuild-and-reselect sequences
        # on the same TreeCtrl, seemingly racing each other, which
        # produced "wrapped C/C++ object of type TreeCtrl has been
        # deleted" and crashed NVDA. Suppressing onTabActivated for just
        # this one programmatic switch and doing a single controlled
        # reload+select here instead should eliminate that race -- not
        # fully certain this is wx's exact internal mechanism, so watch
        # for a repeat.
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

    def addTab(self, panel, label, select=True, removable=True):
        """
        Adds `panel` (already constructed with self.notebook as its
        parent) as a new tab. `removable=False` marks a permanent tab
        (Home, Notifications, and future primary sections) -- Ctrl+W
        becomes a no-op for it. NOTE: "removable" here only ever means
        "can Ctrl+W take this tab out of the notebook" -- it has
        nothing to do with closing the MainWindow itself (Escape/
        Alt+F4/the toolbar's Close button), see onCharHook below.
        """
        panel.TAB_REMOVABLE = removable
        self.notebook.AddPage(panel, label, select)
        self._updateRemoveTabButton()

        # The panel's own __init__ runs (and may call self._updateTitle())
        # BEFORE it's added as a page here, so notebook.FindPage(self)
        # inside _updateTitle returns nothing yet and both the tab label
        # and (if this is the selected tab) the MainWindow title update
        # are silently skipped. Re-run it now that the page index is valid.
        refreshTitle = getattr(panel, "_updateTitle", None)
        if callable(refreshTitle):
            refreshTitle()

        if select:
            # AddPage(select=True) updates which page is visually shown,
            # but doesn't move REAL keyboard/screen-reader focus there by
            # itself -- and a panel's own __init__ (see
            # _restoreFocusPosition in feedWindow.py) only restores its
            # internal list position/selection at construction time, NOT
            # real focus (moveFocus=False there), specifically so this
            # is the ONLY place real focus gets grabbed for the
            # initially-selected tab, once notebook layout has settled.
            wx.CallAfter(self._focusPanel, panel)

    @uiutil.safe_ui_callback
    def _focusPanel(self, panel):
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
            nvdaUi.message("Can't move the tab further in that direction.")
            return
        panel = self.notebook.GetPage(index)
        label = self.notebook.GetPageText(index)
        # RemovePage() auto-selects a neighboring tab and fires
        # EVT_NOTEBOOK_PAGE_CHANGED for IT first, then InsertPage(select=True)
        # fires again for the real target -- two real announcements back
        # to back. Suppress both via the existing flag onPageChanged
        # already checks, then trigger onTabActivated manually once.
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
        # Cross-tab live sync -- called by chatWindow.py's
        # _ChatMessagePanelMixin._notifyConvoChanged after any local
        # state change (send/react/delete/mark-read/lock) to a
        # conversation. Every OTHER open panel (skips sourcePanel, which
        # already updated its own UI) gets a chance to re-render from
        # the now-fresh local DB -- cheap, no network call. Covers
        # ChatWindow (has both hooks) and any ConvoTabWindow(s) for the
        # same convoId (only has _reloadMessagesIfCurrent).
        for panel in self.getOpenTabs():
            if panel is sourcePanel:
                continue
            reload = getattr(panel, "_reloadMessagesIfCurrent", None)
            if reload:
                try:
                    reload(convoId, moveFocus=False)
                except TypeError:
                    # ChatWindow's version has no moveFocus param -- it
                    # never grabs real OS focus in _showMessages anyway
                    # (Focus()/Select() on a ListCtrl that doesn't
                    # already have focus don't steal it), unlike
                    # ConvoTabWindow's _loadMessages, which does via an
                    # explicit SetFocus().
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
        Generalized identity used for MainWindow's remember-last-tab
        feature (see onPageChanged/activateInitialTab below) and for
        tab rename persistence (see renameCurrentTab). Permanent tabs
        are identified by their TAB_KEY string (see feedWindow.py/
        chatWindow.py). Temp tabs (ListTabWindow, ConvoTabWindow) carry
        no TAB_KEY, but each now sets TAB_TEMP_TYPE/TAB_TEMP_KEY,
        matching the "type"/"key" fields already used by
        db.get_open_temp_tabs() -- letting the same identity double as
        a lookup into that list. Returns None if neither is present.
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
        # Fired BEFORE the page actually swaps (unlike onPageChanged),
        # for every cause including native Ctrl+Tab. Moving focus to
        # the notebook itself here means no child control is still
        # focused right as it becomes hidden -- that "focused control
        # about to be hidden" moment is what seemed to trigger Windows'
        # own native focus-reassignment into the new page, landing an
        # extra, uncontrolled focus (and NVDA announcement) alongside
        # onTabActivated()'s own explicit one right after. Confirmed by
        # testing: doubled specifically for Ctrl+Tab/Ctrl+number
        # (focus starts deep in the OLD page's content) but not
        # tab-strip navigation (focus already on the strip itself).
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
                self.newPostButton.SetLabel("New chat... (Ctrl+N)")
            elif isListsContext:
                self.newPostButton.SetLabel("New list... (Ctrl+N)")
            else:
                self.newPostButton.SetLabel("New post... (Ctrl+N)")
            if not self._activationSuppressed:
                onActivated = getattr(panel, "onTabActivated", None)
                if callable(onActivated):
                    onActivated()
                # Remember which tab this was (permanent OR temp -- see
                # _getTabIdentity above), per account, so the next NVSky
                # session can reopen on it instead of always defaulting
                # to Home. Previously only permanent tabs (TAB_KEY) were
                # remembered here, so leaving NVSky focused on a temp
                # tab (e.g. a conversation popped into its own tab)
                # silently fell back to whichever permanent tab was
                # active before that -- fixed by widening this to any
                # tab with a resolvable identity.
                identity = self._getTabIdentity(panel)
                if identity:
                    account = db.get_active_account()
                    if account is not None:
                        db.set_ui_state(f"last_active_tab:{account['id']}", json.dumps(identity))
        self._updateRemoveTabButton()
        evt.Skip()
    def activateInitialTab(self, index):
        """
        Called once by GlobalPlugin.script_openFeed() after every startup
        tab (permanent + restored temp tabs) has been added, to select
        whichever tab was remembered as last-active (Home by
        default/fallback) WITHOUT speaking its name -- silent counterpart
        to a real mid-session tab switch.
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
            nvdaUi.message("This tab can't be removed.")
            return
        onRemoved = getattr(panel, "onTabRemoved", None)
        if callable(onRemoved):
            onRemoved()
        self.notebook.DeletePage(index)

    def renameCurrentTab(self):
        # Only ever changes panel.TAB_NAME (the short label in the tab
        # strip and the first part of the window title) -- the
        # " - NVSky - <handle>" suffix always comes from _updateTitle()
        # and is never touched here. Permanent tabs (TAB_REMOVABLE False)
        # are never renameable, same restriction as Ctrl+W.
        index = self.notebook.GetSelection()
        if index == wx.NOT_FOUND:
            return
        panel = self.notebook.GetPage(index)
        if not getattr(panel, "TAB_REMOVABLE", True):
            nvdaUi.message("This tab can't be renamed.")
            return
        currentName = getattr(panel, "TAB_NAME", self.notebook.GetPageText(index))
        dlg = wx.TextEntryDialog(self, "New tab name:", "Rename tab", value=currentName)
        if dlg.ShowModal() == wx.ID_OK:
            newName = dlg.GetValue().strip()
            if newName:
                panel.TAB_NAME = newName
                refreshTitle = getattr(panel, "_updateTitle", None)
                if callable(refreshTitle):
                    refreshTitle()
                # Persist the rename past this session -- each temp tab
                # class owns the actual db write (it already knows its
                # own identity/db.get_open_temp_tabs() entry), same
                # delegation pattern as onTabRemoved.
                onRenamed = getattr(panel, "onTabRenamed", None)
                if callable(onRenamed):
                    onRenamed(newName)
                nvdaUi.message(f"Tab renamed to {newName}.")
        dlg.Destroy()
    def onFindLists(self, evt=None):
        index = self.notebook.GetSelection()
        panel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        onFindLists = getattr(panel, "onSubscribeViaLink", None)
        if callable(onFindLists):
            onFindLists()

    def checkAllOpenTabs(self):
        # ONE shared background thread syncing every tab sequentially
        # (not N concurrent ones -- that raced on
        # client.get_client_for_active_account()'s session/token
        # refresh and caused sporadic "re-login failed" errors), and
        # ONE spoken summary at the end (not N "no new X" announcements
        # firing back to back).
        #
        # NOTE: this used to only re-render whichever tab was currently
        # visible, on the assumption every other tab would re-render
        # itself next time it was activated (see each panel's
        # onTabActivated). That assumption was wrong -- onTabActivated
        # only calls _restoreFocusPosition(), which re-shows whatever
        # was already rendered, it never re-reads the DB. Confirmed
        # bug: after a fresh login, Home/Saved/etc all render once
        # against an empty cache; running Ctrl+F5 from Chat correctly
        # refreshed Chat (the active tab) but Home stayed empty even
        # after switching to it, despite its own status bar (which
        # queries the DB fresh) already showing the right unread count.
        # Fixed below by reloading every tab that actually got new
        # data, not just the active one -- moveFocus=False keeps a
        # background reload from stealing real keyboard focus.
        panels = [p for p in self.getOpenTabs() if callable(getattr(p, "_syncForBulkCheck", None))]
        if not panels:
            return
        nvdaUi.message("Checking all open tabs for updates, please wait...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCheckAllOpenTabsDone(self, updatedPanels, error):
        if error:
            log.error(f"NVSky: checkAllOpenTabs failed: {error}")
            nvdaUi.message(f"Could not check for updates: {error}")
            return
        index = self.notebook.GetSelection()
        activePanel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        for panel in updatedPanels:
            reload = getattr(panel, "_reloadAfterBulkCheck", None)
            if callable(reload):
                reload(moveFocus=(panel is activePanel))
        # The active tab reloads even if it personally had nothing new
        # (e.g. right after a fresh login, its very first render
        # happened against an empty local cache) -- matches the old
        # always-reload-the-active-tab behavior.
        if activePanel is not None and activePanel not in updatedPanels:
            reload = getattr(activePanel, "_reloadAfterBulkCheck", None)
            if callable(reload):
                reload(moveFocus=True)
        if updatedPanels:
            names = [getattr(p, "TAB_NAME", "a tab") for p in updatedPanels]
            nvdaUi.message(f"Updates in: {', '.join(names)}.")
        else:
            nvdaUi.message("No new updates in any open tab.")

    # ---------------- window-level keyboard shortcuts ----------------

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()

        # "Close" always means closing this whole app window (Escape/
        # Alt+F4/toolbar Close button); Ctrl+W only ever REMOVES the
        # current tab from the notebook (browser-style) and no-ops on
        # permanent tabs -- the two concepts are intentionally separate.
        if keyCode == wx.WXK_ESCAPE:
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
        # Plain F5/Ctrl+N fallbacks -- each tab panel already handles
        # these itself (via its own EVT_CHAR_HOOK) whenever it actually
        # has focus, so this only fires when focus is somewhere else in
        # the window (e.g. on the toolbar itself). Shift+F5 ("fetch
        # previous posts") is deliberately NOT handled here -- that's
        # tab-specific "load more" behavior, panels only.
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
        # Set FIRST, before anything else -- any wx.CallAfter-queued
        # background completion (safe_ui_callback-guarded) that lands
        # from this point on will see this and skip touching wx
        # entirely, rather than trying and hoping RuntimeError catches
        # it cleanly. See uiutil.app_closing for the full reasoning.
        uiutil.app_closing = True

        # wx.Timer isn't part of the parent-child window destroy
        # cascade -- Destroy() below tears down every child panel's
        # C++ object, but any still-running timer keeps firing into
        # those now-destroyed panels regardless. Confirmed by checking
        # source (see plan-09.md): scanning for every wx.Timer INSTANCE
        # stored on each panel (rather than checking specific known
        # attribute names) on purpose -- an earlier version of this
        # fix only checked for _timeRefreshTimer by name and missed
        # _loadingTimer (the ~1s F5-in-progress beep, only ever
        # stopped from inside _onCheckForUpdatesDone), which is a much
        # more likely crash trigger than the 60s one since it fires so
        # much more often during exactly the "refresh then immediately
        # close" window. Scanning by type instead of by name means any
        # future timer added anywhere doesn't need this method updated
        # again to stay covered.
        for i in range(self.notebook.GetPageCount()):
            panel = self.notebook.GetPage(i)
            for value in vars(panel).values():
                if isinstance(value, wx.Timer):
                    value.Stop()
        self.Destroy()
