import sys
import os
import json

# Add our own lib/ folder to sys.path FIRST (insert at index 0).
_addonDir = os.path.dirname(__file__)
_libDir = os.path.join(_addonDir, "lib")
if _libDir not in sys.path:
    sys.path.insert(0, _libDir)

import wx
import globalPluginHandler
import gui
from gui.settingsDialogs import NVDASettingsDialog
import ui
import addonHandler
from logHandler import log

addonHandler.initTranslation()

from . import db
from .settings import NVSkySettingsPanel
from .feedWindow import *
from .mainWindow import MainWindow
from .chatWindow import ChatWindow, ConvoTabWindow
from .compose import ComposeDialog


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("NVSky")

    __gestures = {
        "kb:NVDA+alt+b": "openFeed",
        "kb:NVDA+alt+n": "openNotifications",
    }

    def __init__(self):
        super().__init__()
        self._checkLibs()
        self._initDatabase()
        NVDASettingsDialog.categoryClasses.append(NVSkySettingsPanel)
        self._mainWindow = None

    def terminate(self):
        if NVSkySettingsPanel in NVDASettingsDialog.categoryClasses:
            NVDASettingsDialog.categoryClasses.remove(NVSkySettingsPanel)
        if self._mainWindow:
            try:
                self._mainWindow.Close()
            except Exception:
                pass
        super().terminate()

    def _checkLibs(self):
        libsToCheck = [
            "atproto", "httpx", "pydantic", "libipld",
            "cryptography", "websockets", "cffi",
        ]
        failed = []
        for name in libsToCheck:
            try:
                __import__(name)
            except Exception as e:
                failed.append(f"{name}: {e}")

        if failed:
            log.error("NVSky: lib import failed:\n" + "\n".join(failed))
            ui.message(_("NVSky failed to load required libraries. Check the NVDA log for details."))

    def _initDatabase(self):
        try:
            db.init_db()
        except Exception as e:
            log.error(f"NVSky: database init failed: {e}")
            ui.message(_("NVSky failed to initialize its local database. Check the NVDA log for details."))

    def script_openFeed(self, gesture):
        if self._mainWindow is not None:
            self._mainWindow.Raise()
            return

        gui.mainFrame.prePopup()
        self._mainWindow = MainWindow(gui.mainFrame)
        self._mainWindow.Bind(wx.EVT_CLOSE, self._onMainWindowClosed)

        homeTab = FeedWindow(self._mainWindow.notebook)
        self._mainWindow.addTab(homeTab, "Home", select=True, removable=False)

        notificationsTab = NotificationsWindow(self._mainWindow.notebook)
        self._mainWindow.addTab(notificationsTab, "Notifications", select=False, removable=False)

        savedTab = SavedWindow(self._mainWindow.notebook)
        self._mainWindow.addTab(savedTab, "Saved", select=False, removable=False)

        chatTab = ChatWindow(self._mainWindow.notebook)
        self._mainWindow.addTab(chatTab, "Chat", select=False, removable=False)

        listsTab = ListsWindow(self._mainWindow.notebook)
        self._mainWindow.addTab(listsTab, "Lists", select=False, removable=False)

        account = db.get_active_account()
        if account is not None:
            for entry in db.get_open_temp_tabs(account["id"]):
                if entry.get("type") == "list":
                    # entry["custom_name"] wins over the list's own name
                    # if the user renamed this tab last session (see
                    # ListTabWindow.onTabRenamed / db.set_temp_tab_custom_name).
                    listName = entry.get("custom_name") or entry.get("list_name", "List")
                    tempTab = ListTabWindow(self._mainWindow.notebook, entry["list_uri"], listName)
                    self._mainWindow.addTab(tempTab, listName, select=False, removable=True)
                elif entry.get("type") == "conversation":
                    convo = db.get_convo(account["id"], entry.get("convo_id") or entry.get("key"))
                    if convo is None:
                        # Conversation no longer in the local cache (e.g.
                        # left/deleted since last session) -- drop the
                        # stale entry instead of trying to reopen it
                        # every time.
                        db.remove_open_temp_tab(account["id"], "conversation", entry.get("key"))
                        continue
                    label = entry.get("custom_name") or convo.get("member_display_name") or convo.get("member_handle") or "Conversation"
                    convoTab = ConvoTabWindow(self._mainWindow.notebook, convo, account)
                    # ConvoTabWindow's own __init__ always computes
                    # TAB_NAME from the convo record -- override it here
                    # so a persisted rename sticks. addTab() below is
                    # what re-syncs the tab strip/title from this (via
                    # panel._updateTitle()), so setting it before that
                    # call is enough.
                    convoTab.TAB_NAME = label
                    self._mainWindow.addTab(convoTab, label, select=False, removable=True)
        # Reopen on whichever tab (permanent OR temp -- e.g. a
        # conversation popped into its own tab) was last active for
        # this account (Home/index 0 by default, or the first time
        # ever). Temp tabs referenced here were already reconstructed
        # by the loop above, so they're available for
        # findTabIndexByIdentity() to match against.
        lastTabRaw = db.get_ui_state(f"last_active_tab:{account['id']}") if account is not None else None
        lastTabIdentity = None
        if lastTabRaw:
            try:
                lastTabIdentity = json.loads(lastTabRaw)
            except (ValueError, TypeError):
                # Pre-upgrade format: a bare permanent TAB_KEY string
                # (e.g. "chat"), not yet the {"kind", "key"} shape.
                lastTabIdentity = {"kind": "permanent", "key": lastTabRaw}
        targetIndex = self._mainWindow.findTabIndexByIdentity(lastTabIdentity)
        self._mainWindow.activateInitialTab(targetIndex)
        self._mainWindow.Show()

    def _onMainWindowClosed(self, evt):
        self._mainWindow = None
        gui.mainFrame.postPopup()
        evt.Skip()

    script_openFeed.__doc__ = _("Open the NVSky main window")

    def script_openNotifications(self, gesture):
        if self._mainWindow is None:
            self.script_openFeed(gesture)
        else:
            self._mainWindow.Raise()
        self._mainWindow.selectTab(NotificationsWindow)

    script_openNotifications.__doc__ = _("Switch to the NVSky Notifications tab")

    def script_openNotifications(self, gesture):
        if self._notificationsWindow is not None:
            self._notificationsWindow.Raise()
            return

        gui.mainFrame.prePopup()
        self._notificationsWindow = NotificationsWindow(gui.mainFrame)
        self._notificationsWindow.Bind(wx.EVT_CLOSE, self._onNotificationsWindowClosed)
        self._notificationsWindow.Show()

    def _onNotificationsWindowClosed(self, evt):
        self._notificationsWindow = None
        gui.mainFrame.postPopup()
        evt.Skip()

    script_openNotifications.__doc__ = _("Open the NVSky notifications window")

    def script_quickNewPost(self, gesture):
        # ComposeDialog is shown non-modally (Show(), never ShowModal()) --
        # calling ShowModal() straight from an NVDA gesture can crash NVDA
        # outright (same class of bug hit before in YoutubePlus).
        # postPopup() must fire when the dialog actually closes, not right
        # after Show() -- Show() returns immediately, so calling it inline
        # here used to tell NVDA the popup was gone while it was still open.
        gui.mainFrame.prePopup()
        dlg = ComposeDialog(gui.mainFrame, onClosed=None)
        dlg.Bind(wx.EVT_CLOSE, self._onQuickComposeClosed)
        dlg.Show()

    def _onQuickComposeClosed(self, evt):
        gui.mainFrame.postPopup()
        evt.Skip()

    script_quickNewPost.__doc__ = _("Open a quick new post window")