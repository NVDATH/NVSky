import sys
import os
import json
import threading

# Add our own lib/ folder to sys.path FIRST (insert at index 0).
_addonDir = os.path.dirname(__file__)
_libDir = os.path.join(_addonDir, "lib")
if _libDir not in sys.path:
    sys.path.insert(0, _libDir)

import wx
import globalPluginHandler
import gui
import ui
import addonHandler
from logHandler import log

addonHandler.initTranslation()

from . import db
from . import bgsync
from . import client
from . import uiutil
from .settings import NVSkySettingsDialog
from .feedWindow import *
from .mainWindow import MainWindow
from .chatWindow import ChatWindow, ConvoTabWindow
from .compose import ComposeDialog

BG_SYNC_TICK_MS = 60_000  # check once a minute which categories are due


# Set/cleared by GlobalPlugin.__init__/terminate. Module-level because
# NVSkySettingsPanel is opened via NVDA's own global settings dialog
# (gui.mainFrame.popupSettingsDialog), which has no direct reference to
# GlobalPlugin or MainWindow to call into otherwise -- see
# rebuild_main_window_tabs() below and settings.py's onAccountChanged.
_activePlugin = None


def rebuild_main_window_tabs():
    """
    Tells an already-open MainWindow to rebuild every tab against
    whatever account is active now. Confirmed necessary via testing:
    AccountsPanel's onSetActive/onAdd (mainWindow.py) only ever updated
    Settings' own Profile/Muted words panels -- an open MainWindow's
    tabs (each of which cached self._account once, at construction)
    never found out an account had changed at all.
    """
    if _activePlugin is not None:
        _activePlugin._rebuildTabs()


def get_main_window():
    """
    Returns the live MainWindow instance, or None if it isn't open --
    same rationale as rebuild_main_window_tabs() above: code running
    inside NVDA's own Settings dialog (Feed manager's "View feed...")
    has no direct reference to it otherwise.
    """
    if _activePlugin is not None:
        return _activePlugin._mainWindow
    return None


def open_settings_dialog():
    """
    Accessor for code with no direct GlobalPlugin reference (e.g.
    MainWindow's toolbar Settings button) -- same rationale as
    get_main_window()/rebuild_main_window_tabs() above.
    """
    if _activePlugin is not None:
        _activePlugin.onOpenSettings(None)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("NVSky")

    __gestures = {
        "kb:NVDA+shift+y": "openFeed",
    }

    def __init__(self):
        super().__init__()
        global _activePlugin
        self._checkLibs()
        self._initDatabase()
        self._mainWindow = None
        self._settingsDialog = None

        # NVDA's own Preferences-menu convention for add-on settings --
        # replaces the old NVDASettingsDialog.categoryClasses hook,
        # which was also the actual root cause of the ListCtrl-panel
        # double-announcement bug (nesting our own notebook inside
        # NVDA's own Settings dialog notebook). See settings.py's
        # NVSkySettingsDialog docstring for the full explanation.
        self._settingsMenuItem = gui.mainFrame.sysTrayIcon.preferencesMenu.Append(
            wx.ID_ANY, _("NVSk&y Settings..."), _("Configure NVSky")
        )
        gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self.onOpenSettings, self._settingsMenuItem)

        self._bgSyncRunning = False
        self._bgSyncTimer = wx.Timer(gui.mainFrame)
        gui.mainFrame.Bind(wx.EVT_TIMER, self._onBgSyncTick, self._bgSyncTimer)
        self._bgSyncTimer.Start(BG_SYNC_TICK_MS)

        _activePlugin = self

    def terminate(self):
        global _activePlugin
        try:
            self._bgSyncTimer.Stop()
        except Exception:
            pass
        try:
            gui.mainFrame.sysTrayIcon.preferencesMenu.Remove(self._settingsMenuItem)
        except Exception:
            pass
        if self._settingsDialog:
            try:
                self._settingsDialog.Close()
            except Exception:
                pass
        if self._mainWindow:
            try:
                self._mainWindow.Close()
            except Exception:
                pass
        db.close_all_connections()
        _activePlugin = None
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

    def onOpenSettings(self, evt):
        if self._settingsDialog is not None:
            self._settingsDialog.Raise()
            return
        gui.mainFrame.prePopup()
        self._settingsDialog = NVSkySettingsDialog(
            gui.mainFrame, on_account_changed=self._onSettingsAccountChanged
        )
        self._settingsDialog.Bind(wx.EVT_CLOSE, self._onSettingsDialogClosed)
        self._settingsDialog.Show()

    def _onSettingsAccountChanged(self):
        self._rebuildTabs()

    def _onSettingsDialogClosed(self, evt):
        # NVSkySettingsDialog.onClose already calls gui.mainFrame.postPopup()
        # itself (same self-contained pattern as JoinGroupDialog etc in
        # chatWindow.py) -- nothing left to do here besides clearing the
        # reference so onOpenSettings knows it's free to reopen.
        self._settingsDialog = None
        evt.Skip()

    def _buildTabs(self):
        """
        (Re)builds every permanent + restored-temp tab into
        self._mainWindow.notebook, ordered per db.get_tab_order() --
        ONE combined interleaved order covering permanent AND temp
        tabs together, so a temp tab moved to sit before/between
        permanent tabs (via Ctrl+Shift+PageUp/PageDown) stays there
        across restarts instead of always landing after every
        permanent tab (see db.get_tab_order's docstring). Falls back to
        each tab's default position (permanent tabs in their built-in
        order, then temp tabs in db.get_open_temp_tabs()'s own order)
        for anything never seen in a saved order -- list.sort() is
        stable, so ties keep this relative order automatically.

        Shared by script_openFeed (fresh MainWindow, empty notebook)
        and _rebuildTabs (MainWindow already open, notebook cleared
        right before this runs). Caller is responsible for suppressing/
        restoring activation speech (_activationSuppressed) and for the
        final activateInitialTab()/Show().

        Returns the active account (or None).
        """
        account = db.get_active_account()
        # LOW CONFIDENCE: chat_supported is set once at login time via
        # client.check_chat_supported (a probe call, not a documented
        # capability flag -- see its docstring). Defaults to True/1 for
        # an account that predates this column, or when nothing is
        # logged in yet, so nothing changes for the common case.
        chatSupported = account is None or bool(account.get("chat_supported", 1))

        homeTab = FeedWindow(self._mainWindow.notebook)
        notificationsTab = NotificationsWindow(self._mainWindow.notebook)
        exploreTab = ExploreWindow(self._mainWindow.notebook)
        savedTab = SavedWindow(self._mainWindow.notebook)
        listsTab = ListsWindow(self._mainWindow.notebook)

        # (kind, key, panel, label, removable) -- kind/key double as
        # the identity db.get_tab_order() entries are matched against.
        allTabs = [
            ("permanent", "home", homeTab, "Home", False),
            ("permanent", "notifications", notificationsTab, "Notifications", False),
            ("permanent", "explore", exploreTab, "Explore", False),
            ("permanent", "saved", savedTab, "Saved", False),
        ]
        if chatSupported:
            chatTab = ChatWindow(self._mainWindow.notebook)
            allTabs.append(("permanent", "chat", chatTab, "Chat", False))
        else:
            log.info(f"NVSky: chat not supported for {account['handle']}, Chat tab skipped")
            nvdaUi.message("This account doesn't support direct messages -- the Chat tab has been hidden.")
        allTabs.append(("permanent", "lists", listsTab, "Lists", False))

        if account is not None:
            for entry in db.get_open_temp_tabs(account["id"]):
                if entry.get("type") == "list":
                    # entry["custom_name"] wins over the list's own name
                    # if the user renamed this tab last session.
                    listName = entry.get("custom_name") or entry.get("list_name", "List")
                    tempTab = ListTabWindow(
                        self._mainWindow.notebook, entry["list_uri"], listName, origin_key=entry.get("origin_key"),
                    )
                    allTabs.append(("list", entry["list_uri"], tempTab, listName, True))
                elif entry.get("type") == "search_preview":
                    if not entry.get("source_key"):
                        # Stale entry from before "source_key" existed
                        # (used to be "uri"/"query") -- drop it instead
                        # of crashing every refresh with a None feed/query.
                        db.remove_open_temp_tab(account["id"], "search_preview", entry.get("key"))
                        continue
                    name = entry.get("name", "Preview")
                    tempTab = FeedPreviewTabWindow(
                        self._mainWindow.notebook, name, entry.get("kind", "feed"), entry.get("source_key"),
                        filters=entry.get("filters"), feed_key=entry.get("key"), origin_key=entry.get("origin_key"),
                    )
                    allTabs.append(("search_preview", entry.get("key"), tempTab, name, True))
                elif entry.get("type") == "user_list":
                    kind = entry.get("list_kind")
                    userListTab = None
                    if kind in ("followers", "following"):
                        did = entry.get("did")
                        if did:
                            ownerLabel = entry.get("custom_name") or entry.get("owner_label", "user")
                            userListTab = UserListTabWindow(
                                self._mainWindow.notebook, kind, did, ownerLabel, origin_key=entry.get("origin_key"),
                            )
                    elif kind == "search":
                        query = entry.get("query")
                        if query:
                            userListTab = UserListTabWindow(
                                self._mainWindow.notebook, kind, query, origin_key=entry.get("origin_key"),
                            )
                    if userListTab is None:
                        db.remove_open_temp_tab(account["id"], "user_list", entry.get("key"))
                        continue
                    if entry.get("custom_name"):
                        userListTab.TAB_NAME = entry["custom_name"]
                    allTabs.append(("user_list", entry.get("key"), userListTab, userListTab.TAB_NAME, True))
                elif entry.get("type") == "user_timeline":
                    did = entry.get("did")
                    if not did:
                        db.remove_open_temp_tab(account["id"], "user_timeline", entry.get("key"))
                        continue
                    ownerLabel = entry.get("custom_name") or entry.get("owner_label", "user")
                    timelineTab = UserTimelineTabWindow(
                        self._mainWindow.notebook, did, ownerLabel, origin_key=entry.get("origin_key"),
                    )
                    allTabs.append(("user_timeline", did, timelineTab, timelineTab.TAB_NAME, True))
                elif entry.get("type") == "thread":
                    rootUri = entry.get("root_uri")
                    cachedPosts = db.get_user_list_cache(account["id"], f"thread:{rootUri}") if rootUri else None
                    if not rootUri or cachedPosts is None:
                        # No cached copy to restore from (e.g. cache was
                        # cleared, or this entry predates the cache
                        # existing at all) -- drop it rather than
                        # opening a genuinely empty thread tab.
                        db.remove_open_temp_tab(account["id"], "thread", entry.get("key"))
                        continue
                    targetIndex = next(
                        (i for i, p in enumerate(cachedPosts) if p.get("uri") == rootUri), 0
                    )
                    threadTab = ThreadTabWindow(
                        self._mainWindow.notebook, rootUri, cachedPosts, targetIndex,
                        origin_key=entry.get("origin_key"),
                    )
                    allTabs.append(("thread", rootUri, threadTab, threadTab.TAB_NAME, True))
                elif entry.get("type") == "conversation":
                    if not chatSupported:
                        continue
                    convo = db.get_convo(account["id"], entry.get("convo_id") or entry.get("key"))
                    if convo is None:
                        # Conversation no longer in the local cache (e.g.
                        # left/deleted since last session) -- drop the
                        # stale entry instead of trying to reopen it
                        # every time.
                        db.remove_open_temp_tab(account["id"], "conversation", entry.get("key"))
                        continue
                    convoMembers = db.get_convo_members(account["id"], convo["convo_id"])
                    label = entry.get("custom_name") or db.describe_convo_from_members(convo, convoMembers)
                    convoTab = ConvoTabWindow(
                        self._mainWindow.notebook, convo, account, convoMembers,
                        origin_key=entry.get("origin_key"),
                    )
                    # ConvoTabWindow's own __init__ always computes
                    # TAB_NAME from the convo record -- override it here
                    # so a persisted rename sticks.
                    convoTab.TAB_NAME = label
                    allTabs.append(("conversation", convo["convo_id"], convoTab, label, True))

        savedOrder = db.get_tab_order(account["id"]) if account is not None else []
        orderIndex = {(kind, key): i for i, (kind, key) in enumerate(savedOrder)}
        allTabs.sort(key=lambda t: orderIndex.get((t[0], t[1]), len(savedOrder)))

        for i, (_kind, _key, panel, label, removable) in enumerate(allTabs):
            self._mainWindow.addTab(panel, label, select=(i == 0), removable=removable)

        return account

    def _onBgSyncTick(self, evt):
        if self._bgSyncRunning:
            return
        account = db.get_active_account()
        if account is None:
            return

        due = []
        for category in bgsync.SYNC_FUNCTIONS:
            intervalMin = db.get_bg_sync_interval(category)
            if intervalMin <= 0:
                continue
            lastSync = db.get_bg_sync_last(account["id"], category)
            if self._bgSyncDue(lastSync, intervalMin):
                due.append(category)

        if due:
            self._runBgSync(account, due)

    def _bgSyncDue(self, lastSyncIso, intervalMinutes):
        if not lastSyncIso:
            return True
        try:
            import datetime
            last = datetime.datetime.fromisoformat(lastSyncIso)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (now - last).total_seconds() >= intervalMinutes * 60
        except (ValueError, TypeError):
            return True

    def _runBgSync(self, account, categories):
        self._bgSyncRunning = True

        def worker():
            import datetime
            try:
                atprotoClient = client.get_client_for_active_account()
            except Exception as e:
                log.error(f"NVSky: background sync login failed: {e}")
                wx.CallAfter(self._onBgSyncAllDone)
                return

            try:
                for category in categories:
                    syncFn, needsMyDid = bgsync.SYNC_FUNCTIONS[category]
                    try:
                        if needsMyDid:
                            changed, names = syncFn(atprotoClient, account["id"], account["did"])
                        else:
                            changed, names = syncFn(atprotoClient, account["id"])
                    except Exception as e:
                        log.error(f"NVSky: background sync failed for {category}: {e}")
                        changed, names = False, []

                    nowIso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    db.set_bg_sync_last(account["id"], category, nowIso)
                    wx.CallAfter(self._onBgSyncCategoryDone, category, changed, names)
            finally:
                # This worker runs in a FRESH thread every tick (see
                # _runBgSync) -- db._get_connection() keeps one sqlite
                # connection alive per thread for the thread's whole
                # lifetime by design (see its own docstring), so without
                # explicitly closing it here, every tick leaked one more
                # zombie connection that never got cleaned up. Confirmed
                # as the actual cause of the MemoryError/disk I/O error/
                # resolve_posts-with-no-message crashes seen in testing
                # -- connections piling up, not a real DB corruption.
                db.close_all_connections()

            wx.CallAfter(self._onBgSyncAllDone)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onBgSyncCategoryDone(self, category, changed, names):
        if not changed:
            return
        if db.get_bg_sync_announce() and names:
            if len(names) == 1:
                nvdaUi.message(f"New activity: {names[0]}")
            else:
                nvdaUi.message(f"New activity: {', '.join(names)}")
        if self._mainWindow is None:
            return
        # Only reload the tab actually ON SCREEN right now -- reloading
        # a HIDDEN panel (e.g. ChatWindow while a ConvoTabWindow is the
        # visible tab) can silently steal real OS focus away from
        # whatever IS visible. Confirmed via testing: ChatWindow's
        # convoTree rebuild (DeleteAllItems + re-append) pulls real
        # focus onto itself even while the panel isn't shown at all.
        # A background/invisible panel doesn't need a live visual
        # refresh anyway -- it already re-reads fresh data from the DB
        # on its own via onTabActivated() the next time it's shown.
        activeIndex = self._mainWindow.notebook.GetSelection()
        activePanel = self._mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        if activePanel is None or not self._panelMatchesCategory(activePanel, category):
            return
        reload = getattr(activePanel, "_reloadAfterBulkCheck", None)
        if callable(reload):
            try:
                reload(moveFocus=False)
            except Exception as e:
                log.error(f"NVSky: panel reload after background sync failed: {e}")

    def _panelMatchesCategory(self, panel, category):
        tabKey = getattr(panel, "TAB_KEY", None)
        tempType = getattr(panel, "TAB_TEMP_TYPE", None)
        if category == "home":
            return tabKey == "home"
        if category == "notifications":
            return tabKey == "notifications"
        if category == "saved":
            return tabKey == "saved"
        if category == "chat":
            return tabKey == "chat" or tempType == "conversation"
        if category == "lists":
            return tabKey == "lists" or tempType == "list"
        if category == "search":
            return tempType == "search_preview"
        if category == "profile":
            return tempType in ("user_list", "user_timeline")
        if category == "thread":
            return tempType == "thread"
        return False

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onBgSyncAllDone(self):
        self._bgSyncRunning = False

    def script_openFeed(self, gesture):
        if self._mainWindow is not None:
            self._mainWindow.Raise()
            return

        gui.mainFrame.prePopup()
        self._mainWindow = MainWindow(gui.mainFrame)
        self._mainWindow.Bind(wx.EVT_CLOSE, self._onMainWindowClosed)

        account = self._buildTabs()

        # Reopen on whichever tab (permanent OR temp -- e.g. a
        # conversation popped into its own tab) was last active for
        # this account (Home/index 0 by default, or the first time
        # ever). Temp tabs referenced here were already reconstructed
        # by _buildTabs(), so they're available for
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

    def _rebuildTabs(self):
        """
        Called via the module-level rebuild_main_window_tabs() when the
        active account changes (Settings > Accounts) while MainWindow
        is already open. Tears down every tab and rebuilds from
        scratch instead of trying to patch each panel in place --
        deliberately simpler than reconciling which temp tabs (a
        conversation, a pinned list) still make sense under a
        different account, or whether the Chat tab itself needs to
        appear/disappear because chat_supported changed.
        """
        if self._mainWindow is None:
            return
        self._mainWindow._activationSuppressed = True
        self._mainWindow.notebook.DeleteAllPages()
        account = self._buildTabs()
        self._mainWindow.activateInitialTab(0)
        label = account["handle"] if account else "no account"
        nvdaUi.message(f"Switched to {label}.")

    def _onMainWindowClosed(self, evt):
        self._mainWindow = None
        gui.mainFrame.postPopup()
        evt.Skip()

    script_openFeed.__doc__ = _("Open the NVSky main window")

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