"""
NVSky settings UI.

Registers as a category panel inside NVDA's main multi-category Settings
dialog. Nested wx.Notebook pages, in this order: Accounts, General,
Display, Sound, Profile.
"""

import re
import threading
import webbrowser
import wx

import gui
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import timeutils

APP_PASSWORD_URL = "https://bsky.app/settings/app-passwords"
STRFTIME_REFERENCE_URL = "https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes"

APP_PASSWORD_PATTERN = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$", re.IGNORECASE)

TIME_MODE_CHOICES = [
    ("Relative (up to 24h, then full date/time)", "relative_24h"),
    ("Relative always (minutes/hours/days/months/years ago)", "relative_always"),
    ("Full date and time", "absolute"),
    ("Custom format...", "custom"),
]


class LoginDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Log in to Bluesky")

        sizer = wx.BoxSizer(wx.VERTICAL)

        handleLabel = wx.StaticText(self, label="Handle or email:")
        sizer.Add(handleLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.handleCtrl = wx.TextCtrl(self)
        sizer.Add(self.handleCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        passwordLabel = wx.StaticText(self, label="App Password (NOT your regular account password):")
        sizer.Add(passwordLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.passwordCtrl = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        sizer.Add(self.passwordCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        self.appPasswordButton = wx.Button(self, label="Generate App Password...")
        self.appPasswordButton.Bind(wx.EVT_BUTTON, self.onGenerateAppPassword)
        sizer.Add(self.appPasswordButton, flag=wx.LEFT | wx.TOP, border=10)

        self.statusLabel = wx.StaticText(self, label="")
        sizer.Add(self.statusLabel, flag=wx.LEFT | wx.TOP, border=10)

        buttonSizer = wx.StdDialogButtonSizer()
        self.loginButton = wx.Button(self, wx.ID_OK, label="Log in")
        cancelButton = wx.Button(self, wx.ID_CANCEL)
        buttonSizer.AddButton(self.loginButton)
        buttonSizer.AddButton(cancelButton)
        buttonSizer.Realize()
        sizer.Add(buttonSizer, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

        self.passwordCtrl.Bind(wx.EVT_TEXT, self.onPasswordChanged)
        self.loginButton.Bind(wx.EVT_BUTTON, self.onLogin)

        self.SetSizerAndFit(sizer)
        self.result = None
        self._passwordWarningAcknowledged = False

    def onGenerateAppPassword(self, evt):
        webbrowser.open(APP_PASSWORD_URL)

    def onPasswordChanged(self, evt):
        hasText = bool(self.passwordCtrl.GetValue())
        self.appPasswordButton.Show(not hasText)
        self.Layout()
        evt.Skip()

    def onLogin(self, evt):
        handle = self.handleCtrl.GetValue().strip()
        password = self.passwordCtrl.GetValue()

        if not handle or not password:
            self.statusLabel.SetLabel("Handle and App Password are both required.")
            return

        if not APP_PASSWORD_PATTERN.match(password) and not self._passwordWarningAcknowledged:
            self._passwordWarningAcknowledged = True
            self.statusLabel.SetLabel(
                "This doesn't look like an App Password (expected format: abcd-efgh-ijkl-mnop, "
                "not your regular account password). Press Log in again to continue anyway."
            )
            return

        self.loginButton.Disable()
        self.statusLabel.SetLabel("Logging in...")

        def worker():
            try:
                account = client.login(handle, password)
                error = None
            except client.LoginError as e:
                account = None
                error = str(e)
            wx.CallAfter(self._onLoginDone, account, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoginDone(self, account, error):
        if error:
            self.loginButton.Enable()
            self.statusLabel.SetLabel(error)
            return
        self.result = account
        self.EndModal(wx.ID_OK)

class AccountsPanel(wx.Panel):
    def __init__(self, parent, onAccountChanged=None):
        super().__init__(parent)
        self._onAccountChanged = onAccountChanged
        self._accounts = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        listLabel = wx.StaticText(self, label="Accounts:")
        sizer.Add(listLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.accountList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.accountList.InsertColumn(0, "Handle", width=300)
        sizer.Add(self.accountList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        self.addButton = wx.Button(self, label="Add account...")
        self.removeButton = wx.Button(self, label="Remove account")
        self.setActiveButton = wx.Button(self, label="Set as active")
        buttonRow.Add(self.addButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.removeButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.setActiveButton)
        sizer.Add(buttonRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.addButton.Bind(wx.EVT_BUTTON, self.onAdd)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.setActiveButton.Bind(wx.EVT_BUTTON, self.onSetActive)

        self.refresh()

    def refresh(self, focusAccountId=None):
        self._accounts = db.get_all_accounts()

        self.accountList.DeleteAllItems()
        selectIndex = 0
        for i, acc in enumerate(self._accounts):
            marker = " (active)" if acc["is_active"] else ""
            self.accountList.InsertItem(i, f'{acc["handle"]}{marker}')
            if focusAccountId is not None and acc["id"] == focusAccountId:
                selectIndex = i

        hasAccounts = bool(self._accounts)
        self.removeButton.Show(hasAccounts)
        self.setActiveButton.Show(hasAccounts)
        if hasAccounts:
            self.accountList.Focus(selectIndex)
            self.accountList.Select(selectIndex)

        self.Layout()

    def onTabActivated(self):
        # Single deliberate real-focus grab, once, matching MainWindow's
        # own tab-activation pattern -- replaces relying on native
        # Tab-key traversal into accountList (the old double-announce
        # trigger when this lived inside NVDA's nested Settings notebook).
        if self._accounts:
            self.accountList.SetFocus()

    def _getSelectedAccount(self):
        if not self._accounts:
            return None
        index = self.accountList.GetFirstSelected()
        if index == -1:
            return None
        return self._accounts[index]

    def onAdd(self, evt):
        dlg = LoginDialog(self)
        if dlg.ShowModal() == wx.ID_OK and dlg.result:
            self.refresh(focusAccountId=dlg.result["id"])
            self.accountList.SetFocus()
            nvdaUi.message(f'Logged in as {dlg.result["handle"]}')
            # This call was missing entirely -- onSetActive already had
            # it, but a fresh login via "Add account..." never told
            # anything it had happened. Confirmed as the cause of
            # "removed the only account, logged back in, still looks
            # stuck" -- an already-open MainWindow's tabs never found
            # out a new account existed.
            if self._onAccountChanged:
                self._onAccountChanged()
        dlg.Destroy()

    def onRemove(self, evt):
        account = self._getSelectedAccount()
        if account is None:
            nvdaUi.message("No account selected.")
            return

        confirm = wx.MessageDialog(
            self,
            f'Remove {account["handle"]}? This deletes its cached posts and stored '
            f"App Password from this computer. This can't be undone.",
            "Remove account",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        result = confirm.ShowModal()
        confirm.Destroy()
        if result != wx.ID_YES:
            return

        db.remove_account(account["id"])
        self.refresh()
        self.accountList.SetFocus()
        nvdaUi.message(f'Removed {account["handle"]}')

    def onSetActive(self, evt):
        account = self._getSelectedAccount()
        if account is None:
            nvdaUi.message("No account selected.")
            return
        db.set_active_account(account["id"])
        self.refresh(focusAccountId=account["id"])
        nvdaUi.message(f'{account["handle"]} is now the active account.')
        if self._onAccountChanged:
            self._onAccountChanged()

ENTER_ACTION_CHOICES = [
    ("view_thread", "View thread"),
    ("reply", "Reply"),
    ("quote", "Quote post"),
    ("repost", "Repost / Undo repost"),
    ("like", "Like / Unlike"),
    ("mark_read", "Toggle read/unread"),
]


class GeneralPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        note = wx.StaticText(
            self,
            label="Check intervals are saved but not applied automatically yet -- "
                  "\"Check for updates\" in the feed window is still manual for now.",
        )
        sizer.Add(note, flag=wx.ALL, border=10)

        enterLabel = wx.StaticText(self, label="Enter key action on a post:")
        sizer.Add(enterLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.enterActionChoice = wx.Choice(self, choices=[label for _, label in ENTER_ACTION_CHOICES])
        currentEnterAction = db.get_ui_state("enter_action") or "view_thread"
        selectedIndex = next(
            (i for i, (key, _) in enumerate(ENTER_ACTION_CHOICES) if key == currentEnterAction), 0
        )
        self.enterActionChoice.SetSelection(selectedIndex)
        sizer.Add(self.enterActionChoice, flag=wx.LEFT | wx.TOP, border=10)

        self.bgSyncAnnounceCheck = wx.CheckBox(self, label="&Speak when background sync finds new content")
        self.bgSyncAnnounceCheck.SetValue(db.get_bg_sync_announce())
        sizer.Add(self.bgSyncAnnounceCheck, flag=wx.LEFT | wx.TOP, border=10)

        bgSyncNote = wx.StaticText(
            self,
            label="Background sync automatically checks each category below for updates "
                  "even while you're not actively viewing that tab. Set to 0 to disable a "
                  "category entirely.",
        )
        sizer.Add(bgSyncNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        self._bgSyncSpins = {}
        bgSyncFields = [
            ("home", "&Home"), ("chat", "C&hat"), ("notifications", "&Notifications"),
            ("saved", "S&aved"), ("lists", "&Lists"), ("search", "S&earch / feed previews"),
            ("profile", "&Profile / followers"), ("thread", "T&hread"),
        ]
        for category, label in bgSyncFields:
            row = wx.BoxSizer(wx.HORIZONTAL)
            rowLabel = wx.StaticText(self, label=f"{label} background sync (minutes, 0 = off):")
            spin = wx.SpinCtrl(
                self, min=0, max=180, initial=db.get_bg_sync_interval(category),
                name=f"{label} background sync interval (minutes)",
            )
            row.Add(rowLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
            row.Add(spin)
            sizer.Add(row, flag=wx.LEFT | wx.TOP, border=10)
            self._bgSyncSpins[category] = spin

        # Operates on the active account -- lives here rather than the
        # Accounts tab since it's a maintenance action, not account
        # management.
        cacheLabel = wx.StaticText(self, label="Cached &Home posts (active account):")
        sizer.Add(cacheLabel, flag=wx.LEFT | wx.TOP, border=10)
        # NOTE: this only clears the Home feed's cached posts -- does
        # NOT touch notifications, chat, lists, or Saved/Explore feed
        # caches. Label made explicit about this scope rather than
        # widening db.clear_cache() itself for now (see plan-13.md §4).
        self.clearCacheButton = wx.Button(self, label="&Clear Home cache")
        sizer.Add(self.clearCacheButton, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.enterActionChoice.Bind(wx.EVT_CHOICE, self.onChanged)
        self.clearCacheButton.Bind(wx.EVT_BUTTON, self.onClearCache)
        for spin in self._bgSyncSpins.values():
            spin.Bind(wx.EVT_SPINCTRL, self.onChanged)
        self.bgSyncAnnounceCheck.Bind(wx.EVT_CHECKBOX, self.onChanged)

    def onTabActivated(self):
        self.enterActionChoice.SetFocus()

    def onChanged(self, evt):
        db.set_ui_state("enter_action", ENTER_ACTION_CHOICES[self.enterActionChoice.GetSelection()][0])
        for category, spin in self._bgSyncSpins.items():
            db.set_bg_sync_interval(category, spin.GetValue())
        db.set_bg_sync_announce(self.bgSyncAnnounceCheck.GetValue())
        evt.Skip()

    def onClearCache(self, evt):
        account = db.get_active_account()
        if account is None:
            nvdaUi.message("No active account.")
            return

        confirm = wx.MessageDialog(
            self,
            f'Clear all cached posts for {account["handle"]}? '
            f"You'll need to fetch from the network again to see them.",
            "Clear cache",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        result = confirm.ShowModal()
        confirm.Destroy()
        if result != wx.ID_YES:
            return

        db.clear_cache(account["id"])
        nvdaUi.message(f'Cache cleared for {account["handle"]}')


class DisplayPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        authorLabel = wx.StaticText(self, label="Show author as:")
        sizer.Add(authorLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.authorModeRadio = wx.RadioBox(
            self, choices=["Display name", "Handle"], majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        authorMode = db.get_ui_state("column1_display") or "display_name"
        self.authorModeRadio.SetSelection(0 if authorMode == "display_name" else 1)
        sizer.Add(self.authorModeRadio, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        timeLabel = wx.StaticText(self, label="Post time format:")
        sizer.Add(timeLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.timeModeChoice = wx.Choice(self, choices=[label for label, _ in TIME_MODE_CHOICES])
        currentMode = db.get_ui_state("time_format_mode") or "relative_24h"
        modeValues = [value for _, value in TIME_MODE_CHOICES]
        self.timeModeChoice.SetSelection(modeValues.index(currentMode) if currentMode in modeValues else 0)
        sizer.Add(self.timeModeChoice, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        customLabel = wx.StaticText(
            self, label="Custom format (Python strftime pattern, used when \"Custom format\" is selected above):"
        )
        sizer.Add(customLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.customPatternCtrl = wx.TextCtrl(
            self, value=db.get_ui_state("time_format_custom_pattern") or "%Y-%m-%d %H:%M:%S"
        )
        sizer.Add(self.customPatternCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        formatHelpButton = wx.Button(self, label="strftime format reference...")
        formatHelpButton.Bind(wx.EVT_BUTTON, self.onFormatHelp)
        sizer.Add(formatHelpButton, flag=wx.LEFT | wx.TOP, border=10)

        sortLabel = wx.StaticText(self, label="Feed order (not wired up to the feed yet):")
        sizer.Add(sortLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.sortOrderRadio = wx.RadioBox(
            self, choices=["Newest first", "Oldest first"], majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        currentSort = db.get_ui_state("sort_order") or "newest_first"
        self.sortOrderRadio.SetSelection(0 if currentSort == "newest_first" else 1)
        sizer.Add(self.sortOrderRadio, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.authorModeRadio.Bind(wx.EVT_RADIOBOX, self.onChanged)
        self.timeModeChoice.Bind(wx.EVT_CHOICE, self.onChanged)
        self.customPatternCtrl.Bind(wx.EVT_TEXT, self.onChanged)
        self.sortOrderRadio.Bind(wx.EVT_RADIOBOX, self.onChanged)

    def onTabActivated(self):
        self.authorModeRadio.SetFocus()

    def onFormatHelp(self, evt):
        webbrowser.open(STRFTIME_REFERENCE_URL)

    def onChanged(self, evt):
        authorMode = "display_name" if self.authorModeRadio.GetSelection() == 0 else "handle"
        db.set_ui_state("column1_display", authorMode)

        timeMode = TIME_MODE_CHOICES[self.timeModeChoice.GetSelection()][1]
        db.set_ui_state("time_format_mode", timeMode)

        db.set_ui_state("time_format_custom_pattern", self.customPatternCtrl.GetValue())
        timeutils.invalidate_time_format_cache()

        sortOrder = "newest_first" if self.sortOrderRadio.GetSelection() == 0 else "oldest_first"
        db.set_ui_state("sort_order", sortOrder)
        evt.Skip()

class FeedManagerPanel(wx.Panel):
    """Settings > NVSky > Feed manager. List/reorder/pin/remove the
    account's subscribed feeds (savedFeedsPrefV2, read+write via
    client.py's get_saved_feeds_pref/reorder_saved_feeds/
    set_feed_pinned/remove_feed_from_saved), plus a Browse & add
    dialog (FeedBrowseDialog, feedWindow.py) reusing the same search
    Explore's Feeds tab uses.
    Renders from db's local saved-feeds cache immediately on open
    (synchronous, before the panel is even shown) and refreshes from
    the server silently in the background -- avoids blocking the
    Settings dialog on a getPreferences+getFeedGenerators round trip
    every time this page is opened. Every action (move/pin/remove) is
    optimistic: the local list (and its cache) update immediately,
    the server call happens in the background.
    NOTE: browsed/managed feeds aren't wired into a Home tab filter
    dropdown yet -- this panel only manages the saved-feeds list
    itself, next step is making a saved feed actually viewable from
    Home."""

    def __init__(self, parent):
        super().__init__(parent)
        account = db.get_active_account()
        self._accountId = account["id"] if account else None
        # list of dicts: uri/display_name/creator_handle/pinned
        self._feeds = db.get_saved_feeds_cache(self._accountId) if self._accountId else []

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.statusLabel = wx.StaticText(self, label="Loading your feeds...")
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        self.feedList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.feedList.InsertColumn(0, "Name", width=150)
        self.feedList.InsertColumn(1, "Creator", width=100)
        self.feedList.InsertColumn(2, "Pinned", width=60)
        sizer.Add(self.feedList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        buttonRow1 = wx.BoxSizer(wx.HORIZONTAL)
        self.moveUpButton = wx.Button(self, label="Move &up")
        self.moveDownButton = wx.Button(self, label="Move &down")
        self.togglePinButton = wx.Button(self, label="&Pin selected")
        buttonRow1.Add(self.moveUpButton, flag=wx.RIGHT, border=5)
        buttonRow1.Add(self.moveDownButton, flag=wx.RIGHT, border=5)
        buttonRow1.Add(self.togglePinButton)
        sizer.Add(buttonRow1, flag=wx.LEFT | wx.TOP, border=10)

        buttonRow2 = wx.BoxSizer(wx.HORIZONTAL)
        self.removeButton = wx.Button(self, label="&Remove selected")
        buttonRow2.Add(self.removeButton)
        sizer.Add(buttonRow2, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        hasCached = bool(self._feeds)
        for btn in (self.moveUpButton, self.moveDownButton, self.togglePinButton, self.removeButton):
            if hasCached:
                btn.Enable()
            else:
                btn.Disable()

        self.moveUpButton.Bind(wx.EVT_BUTTON, lambda evt: self.onMove(-1))
        self.moveDownButton.Bind(wx.EVT_BUTTON, lambda evt: self.onMove(1))
        self.togglePinButton.Bind(wx.EVT_BUTTON, self.onTogglePin)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.feedList.Bind(wx.EVT_LIST_ITEM_FOCUSED, lambda evt: (self._updatePinButtonLabel(), evt.Skip()))

        # Render row 0 synchronously right now (Focus()/Select() only --
        # see _render's docstring) -- same pattern as every other
        # ListCtrl-backed panel in this dialog (MutedWordsPanel etc):
        # Focus()/Select() alone, before this panel ever has real OS
        # focus, is silent (doesn't fire a native focus-changed event),
        # so there's no EVT_SET_FOCUS workaround needed here anymore now
        # that this panel lives in its own standalone dialog instead of
        # nested inside NVDA's own Settings notebook.
        self.statusLabel.SetLabel(f"{len(self._feeds)} subscribed feed(s)." if hasCached else "Loading your feeds...")
        self._render(target_index=0)

        # Deliberately NOT calling self._refresh() here anymore -- see
        # onTabActivated below. Starting the background network sync
        # eagerly at construction time (while some OTHER tab, e.g.
        # Accounts, is still the one actually shown) meant it often
        # finished BEFORE the user ever tabbed over here, so its
        # completion handler's re-render landed in the same instant as
        # onTabActivated's own deliberate feedList.SetFocus() -- two
        # focus-worthy events on the same row, double-announced.
        self._refreshedOnce = False

    def reload(self):
        # Account switched -- re-key the cache and start over the same
        # way __init__ does (cached data first, then a silent refresh).
        account = db.get_active_account()
        self._accountId = account["id"] if account else None
        self._feeds = db.get_saved_feeds_cache(self._accountId) if self._accountId else []
        hasCached = bool(self._feeds)
        for btn in (self.moveUpButton, self.moveDownButton, self.togglePinButton, self.removeButton):
            btn.Enable() if hasCached else btn.Disable()
        self.statusLabel.SetLabel(f"{len(self._feeds)} subscribed feed(s)." if hasCached else "Loading your feeds...")
        self._render(target_index=0)
        self._refreshedOnce = False

    def onTabActivated(self):
        # Unconditional, matching MutedWordsPanel's proven-working
        # pattern -- SetFocus() on an empty ListCtrl is a harmless
        # no-op (nothing to announce), no need to gate on self._feeds.
        self.feedList.SetFocus()
        if not self._refreshedOnce:
            self._refreshedOnce = True
            self._refresh()

    def _stillAlive(self):
        # Best-effort guard against the Settings dialog having been
        # closed while a background load/save was still in flight --
        # doesn't fully rule out the NVDA-side "refreshGui on a
        # destroyed sizer" crash (that happens inside NVDA's own later
        # wx.CallAfter, outside this function's try/except reach), but
        # avoids doing any further UI work once the dialog is gone,
        # which is what triggers that stale callback in the first
        # place.
        try:
            top = wx.GetTopLevelParent(self)
            return bool(top) and not top.IsBeingDeleted()
        except RuntimeError:
            return False

    def _persistCache(self):
        if self._accountId is not None:
            db.set_saved_feeds_cache(self._accountId, self._feeds)

    def _notifyHomeFeedsChanged(self):
        # Best-effort: if MainWindow's Home tab is currently open, tell
        # it to rebuild its filter dropdown right away so an added/
        # removed/reordered feed shows up without needing NVSky
        # reopened. Silently does nothing if MainWindow isn't open --
        # Home will just pick up the current db cache next time it's
        # constructed anyway.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        for panel in mainWindow.getOpenTabs():
            refresh = getattr(panel, "refreshFeedFilterChoices", None)
            if callable(refresh):
                refresh()

    def _refresh(self):
        # Silent background sync against the server -- never blocks
        # the initial render, only corrects it once the real data is
        # back (and only if the panel's still around to see it).
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                items = client.get_saved_feeds_pref(atprotoClient)
                uris = [i.get("value") for i in items if isinstance(i, dict) and i.get("type") == "feed"]
                info = client.get_feed_generators_info(atprotoClient, uris)
                feeds = []
                for item in items:
                    if not (isinstance(item, dict) and item.get("type") == "feed"):
                        continue
                    uri = item.get("value")
                    meta = info.get(uri, {})
                    feeds.append({
                        "uri": uri,
                        "display_name": meta.get("display_name") or uri,
                        "creator_handle": meta.get("creator_handle", ""),
                        "pinned": bool(item.get("pinned")),
                    })
                error = None
            except Exception as e:
                feeds = None
                error = str(e)
            wx.CallAfter(self._onRefreshed, feeds, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onRefreshed(self, feeds, error):
        if not self._stillAlive():
            return
        if error:
            if not self._feeds:
                self.statusLabel.SetLabel(f"Could not load your feeds: {error}")
            return
        for btn in (self.moveUpButton, self.moveDownButton, self.togglePinButton, self.removeButton):
            btn.Enable()
        if feeds == self._feeds:
            # Nothing actually changed -- skip touching the ListCtrl
            # entirely. _render() always does DeleteAllItems()+reinsert,
            # which recreates native rows even at an unchanged focus
            # index -- confirmed as the cause of the extra "reads N
            # times" on first open, since this background completion
            # lands right after onTabActivated's own deliberate
            # feedList.SetFocus() with nothing genuinely new to show.
            return
        self._feeds = feeds
        self._persistCache()
        self.statusLabel.SetLabel(f"{len(self._feeds)} subscribed feed(s).")
        # Preserve whatever's currently focused (the user may already
        # be navigating the cached list) rather than jumping back to
        # row 0 again.
        self._render()
        self._notifyHomeFeedsChanged()

    def _render(self, target_index=None):
        # Focus()/Select() alone (not SetFocus()) only fires a native
        # accessible focus event when the control ALREADY has real OS
        # focus AND the item index is actually changing -- calling it
        # unconditionally here is silent the rest of the time (e.g.
        # during __init__, before this panel is ever shown). Same
        # pattern MutedWordsPanel._renderWords already uses.
        previouslyFocused = self.feedList.GetFocusedItem()
        self.feedList.Freeze()
        try:
            self.feedList.DeleteAllItems()
            for i, feed in enumerate(self._feeds):
                self.feedList.InsertItem(i, feed["display_name"])
                self.feedList.SetItem(i, 1, f"@{feed['creator_handle']}" if feed["creator_handle"] else "")
                self.feedList.SetItem(i, 2, "Yes" if feed["pinned"] else "No")

            if not self._feeds:
                index = None
            elif target_index is not None:
                index = max(0, min(target_index, len(self._feeds) - 1))
            elif previouslyFocused != -1:
                index = min(previouslyFocused, len(self._feeds) - 1)
            else:
                index = 0

            if index is not None:
                self.feedList.Focus(index)
                self.feedList.Select(index)
                self.feedList.EnsureVisible(index)
        finally:
            self.feedList.Thaw()

        self._updatePinButtonLabel()

    def _updatePinButtonLabel(self):
        index = self.feedList.GetFocusedItem()
        if index == -1 or index >= len(self._feeds):
            self.togglePinButton.SetLabel("&Pin selected")
            return
        pinned = self._feeds[index]["pinned"]
        self.togglePinButton.SetLabel("&Unpin selected" if pinned else "&Pin selected")

    def _selectedIndex(self):
        index = self.feedList.GetFocusedItem()
        if index == -1 or index >= len(self._feeds):
            nvdaUi.message("No feed selected.")
            return None
        return index

    def onMove(self, direction):
        index = self._selectedIndex()
        if index is None:
            return
        newIndex = index + direction
        if newIndex < 0 or newIndex >= len(self._feeds):
            nvdaUi.message("Can't move further.")
            return
        self._feeds[index], self._feeds[newIndex] = self._feeds[newIndex], self._feeds[index]
        self._persistCache()
        self._render(target_index=newIndex)
        self._saveOrder()
        self._notifyHomeFeedsChanged()

    def _saveOrder(self):
        # Fire-and-forget: the list is already re-rendered (and
        # cached) above, the order only matters for this add-on's own
        # display (not reflected anywhere on bsky.app), so there's
        # nothing further to tell the user about -- no button
        # disabling, no "saved" message, just persist it quietly.
        orderedUris = [f["uri"] for f in self._feeds]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.reorder_saved_feeds(atprotoClient, orderedUris)
            except Exception as e:
                log.error("NVSky: background saved-feeds reorder failed: %s", e, exc_info=True)

        threading.Thread(target=worker, daemon=True).start()

    def onTogglePin(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        feed = self._feeds[index]
        newPinned = not feed["pinned"]
        feed["pinned"] = newPinned
        self._persistCache()
        self._render(target_index=index)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.set_feed_pinned(atprotoClient, feed["uri"], newPinned)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onTogglePinDone, feed["uri"], newPinned, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onTogglePinDone(self, feed_uri, attemptedPinned, error):
        if not self._stillAlive() or not error:
            return
        # Roll back the optimistic flip -- pin state actually matters
        # (unlike display order), so a failure here needs to be both
        # corrected and surfaced.
        for feed in self._feeds:
            if feed["uri"] == feed_uri:
                feed["pinned"] = not attemptedPinned
        self._persistCache()
        self._render()
        nvdaUi.message(f"Could not change pin: {error}")

    def onRemove(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        feed = self._feeds[index]
        confirm = wx.MessageDialog(
            self, f'Remove "{feed["display_name"]}" from your feeds?', "Confirm remove", wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        # Optimistic: gone from the list (and cache) immediately, the
        # server call happens in the background.
        self._feeds = [f for f in self._feeds if f["uri"] != feed["uri"]]
        self._persistCache()
        self.statusLabel.SetLabel(f"{len(self._feeds)} subscribed feed(s).")
        nvdaUi.message(f'Removed "{feed["display_name"]}".')
        self._render(target_index=index)
        self._notifyHomeFeedsChanged()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_feed_from_saved(atprotoClient, feed["uri"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, index, feed, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onRemoveDone(self, index, feed, error):
        if not self._stillAlive() or not error:
            return
        # Roll back -- put it back where it was, clamped to the
        # current length.
        insertAt = max(0, min(index, len(self._feeds)))
        self._feeds.insert(insertAt, feed)
        self._persistCache()
        self.statusLabel.SetLabel(f"{len(self._feeds)} subscribed feed(s).")
        self._render(target_index=insertAt)
        self._notifyHomeFeedsChanged()
        nvdaUi.message(f"Could not remove feed: {error}")


class SoundPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(self, label="Sound settings are not implemented yet.")
        sizer.Add(note, flag=wx.ALL, border=10)
        self.SetSizer(sizer)


class ProfilePanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.statusLabel = wx.StaticText(self, label="Loading profile...")
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        nameLabel = wx.StaticText(self, label="Display name:")
        sizer.Add(nameLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.nameCtrl = wx.TextCtrl(self)
        sizer.Add(self.nameCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        bioLabel = wx.StaticText(self, label="Bio:")
        sizer.Add(bioLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.bioCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 80))
        sizer.Add(self.bioCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        self.saveTextButton = wx.Button(self, label="Save display name && bio")
        sizer.Add(self.saveTextButton, flag=wx.LEFT | wx.TOP, border=10)

        avatarRow = wx.BoxSizer(wx.HORIZONTAL)
        self.changeAvatarButton = wx.Button(self, label="Change avatar...")
        self.changeBannerButton = wx.Button(self, label="Change banner...")
        avatarRow.Add(self.changeAvatarButton, flag=wx.RIGHT, border=5)
        avatarRow.Add(self.changeBannerButton)
        sizer.Add(avatarRow, flag=wx.LEFT | wx.TOP, border=10)

        self.SetSizer(sizer)

        self.nameCtrl.Disable()
        self.bioCtrl.Disable()
        self.saveTextButton.Disable()
        self.changeAvatarButton.Disable()
        self.changeBannerButton.Disable()

        self.saveTextButton.Bind(wx.EVT_BUTTON, self.onSaveText)
        self.changeAvatarButton.Bind(wx.EVT_BUTTON, lambda e: self.onChangeImage("avatar"))
        self.changeBannerButton.Bind(wx.EVT_BUTTON, lambda e: self.onChangeImage("banner"))

        self._loadProfile()

    def onTabActivated(self):
        # nameCtrl starts Disabled until profile data arrives (see
        # _loadProfile/_onProfileLoaded) -- SetFocus() on a disabled
        # wx.TextCtrl is a silent no-op on Windows, so this only does
        # anything once loading has actually finished; _onProfileLoaded
        # below covers the "still loading when tab was entered" case.
        if self.nameCtrl.IsEnabled():
            self.nameCtrl.SetFocus()

    def _isActiveTabPage(self):
        parent = self.GetParent()
        if not isinstance(parent, wx.Notebook):
            return False
        index = parent.GetSelection()
        return index != wx.NOT_FOUND and parent.GetPage(index) is self

    def reload(self):
        # Same reasoning as MutedWordsPanel.reload() -- this tab is
        # per-account too and needs to re-fetch when the active account
        # changes elsewhere in the Settings dialog.
        self.nameCtrl.Disable()
        self.bioCtrl.Disable()
        self.saveTextButton.Disable()
        self.changeAvatarButton.Disable()
        self.changeBannerButton.Disable()
        self.statusLabel.SetLabel("Loading profile...")
        self._loadProfile()

    def _loadProfile(self):

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                account = db.get_active_account()
                profile = client.get_profile(atprotoClient, account["did"])
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onProfileLoaded, profile, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onProfileLoaded(self, profile, error):
        if error:
            self.statusLabel.SetLabel(f"Could not load profile: {error}")
            return
        self.nameCtrl.SetValue(profile.get("display_name") or "")
        self.bioCtrl.SetValue(profile.get("description") or "")
        self.statusLabel.SetLabel(f'Editing @{profile["handle"]}')
        self.nameCtrl.Enable()
        self.bioCtrl.Enable()
        self.saveTextButton.Enable()
        self.changeAvatarButton.Enable()
        self.changeBannerButton.Enable()
        if self._isActiveTabPage():
            self.nameCtrl.SetFocus()

    def onSaveText(self, evt):
        displayName = self.nameCtrl.GetValue()
        description = self.bioCtrl.GetValue()
        self.saveTextButton.Disable()
        self.statusLabel.SetLabel("Saving...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.update_profile_text(atprotoClient, displayName, description)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onSaveTextDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSaveTextDone(self, error):
        self.saveTextButton.Enable()
        if error:
            self.statusLabel.SetLabel(f"Failed to save: {error}")
            nvdaUi.message(f"Failed to save profile: {error}")
            return
        self.statusLabel.SetLabel("Profile saved.")
        nvdaUi.message("Profile saved.")

    def onChangeImage(self, kind):
        with wx.FileDialog(
            self, f"Choose a new {kind}",
            wildcard="Image files (*.jpg;*.jpeg;*.png)|*.jpg;*.jpeg;*.png",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()

        button = self.changeAvatarButton if kind == "avatar" else self.changeBannerButton
        button.Disable()
        self.statusLabel.SetLabel(f"Uploading {kind}...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if kind == "avatar":
                    client.update_profile_avatar(atprotoClient, path)
                else:
                    client.update_profile_banner(atprotoClient, path)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onImageDone, kind, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onImageDone(self, kind, error):
        button = self.changeAvatarButton if kind == "avatar" else self.changeBannerButton
        button.Enable()
        if error:
            self.statusLabel.SetLabel(f"Failed to update {kind}: {error}")
            nvdaUi.message(f"Failed to update {kind}: {error}")
            return
        self.statusLabel.SetLabel(f"{kind.capitalize()} updated.")
        nvdaUi.message(f"{kind.capitalize()} updated.")



class MutedWordsPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._words = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.statusLabel = wx.StaticText(self, label="Loading muted words...")
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        self.wordList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.wordList.InsertColumn(0, "Word / tag", width=300)
        sizer.Add(self.wordList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        self.addButton = wx.Button(self, label="&Add")
        self.removeButton = wx.Button(self, label="&Remove selected")
        buttonRow.Add(self.addButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.removeButton)
        sizer.Add(buttonRow, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.addButton.Disable()
        self.removeButton.Disable()

        self.addButton.Bind(wx.EVT_BUTTON, self.onAdd)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)

        self._load()

    def reload(self):
        self.addButton.Disable()
        self.removeButton.Disable()
        self.statusLabel.SetLabel("Loading muted words...")
        self._load()

    def onTabActivated(self):
        # Unconditional SetFocus() -- unlike Accounts/Feed manager,
        # this panel can genuinely have zero items the very first time
        # it's shown (no local cache, only ever server-backed). Focus
        # lands on the (possibly empty) list either way, consistent
        # with every other panel's tab-activation behavior; once data
        # arrives, _onLoaded below re-focuses the first row if this is
        # still the active page.
        self.wordList.SetFocus()

    def _isActiveTabPage(self):
        parent = self.GetParent()
        if not isinstance(parent, wx.Notebook):
            return False
        index = parent.GetSelection()
        return index != wx.NOT_FOUND and parent.GetPage(index) is self

    def _renderWords(self, target_index=None):
        previouslyFocused = self.wordList.GetFocusedItem()
        self.wordList.Freeze()
        try:
            self.wordList.DeleteAllItems()
            for i, w in enumerate(self._words):
                self.wordList.InsertItem(i, w["value"])
            self.statusLabel.SetLabel(f"{len(self._words)} muted word(s)/tag(s).")

            if not self._words:
                index = None
            elif target_index is not None:
                index = max(0, min(target_index, len(self._words) - 1))
            elif previouslyFocused != -1:
                index = min(previouslyFocused, len(self._words) - 1)
            else:
                index = 0

            if index is not None:
                self.wordList.Focus(index)
                self.wordList.Select(index)
                self.wordList.EnsureVisible(index)
        finally:
            self.wordList.Thaw()

    def _load(self):

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                words = client.get_muted_words(atprotoClient)
                error = None
            except Exception as e:
                words = None
                error = str(e)
            wx.CallAfter(self._onLoaded, words, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoaded(self, words, error):
        if error:
            self.statusLabel.SetLabel(f"Could not load muted words: {error}")
            return
        self._words = words or []
        self._renderWords(target_index=0)
        self.addButton.Enable()
        self.removeButton.Enable()
        # This panel may have been the active page BEFORE data finished
        # loading (see onTabActivated -- SetFocus() on an empty list has
        # nothing to announce) -- grant real focus now, once, but only
        # if the user hasn't since navigated to some other tab.
        if self._isActiveTabPage():
            self.wordList.SetFocus()

    def onAdd(self, evt):
        dlg = wx.TextEntryDialog(self, "Word or tag to mute:", "Add muted word")
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        value = dlg.GetValue().strip()
        dlg.Destroy()
        if not value:
            return
        if any(w["value"] == value for w in self._words):
            nvdaUi.message(f'"{value}" is already muted.')
            return

        # Optimistic: show it in the list right away, roll back on
        # failure -- matches the convention used throughout chatWindow.py
        # (reactions, mark-read, lock/unlock) instead of a "please wait"
        # round trip for something this low-risk.
        self._words.append({"id": None, "value": value, "targets": ["content", "tag"]})
        newIndex = len(self._words) - 1
        self._renderWords(target_index=newIndex)
        # No explicit "Added X" message -- Focus()/Select() inside
        # _renderWords above already makes NVDA read the newly added
        # row itself, so a separate spoken confirmation just repeats
        # the same word back.
        self.wordList.SetFocus()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.add_muted_word(atprotoClient, value)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onAddDone, value, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onAddDone(self, value, error):
        if not error:
            return
        # Roll back the optimistic add -- the server never actually
        # applied it, so leaving it showing would be a lie.
        self._words = [w for w in self._words if w["value"] != value]
        self._renderWords()
        nvdaUi.message(f'Could not add "{value}": {error}')

    def onRemove(self, evt):
        index = self.wordList.GetFocusedItem()
        if index == -1 or index >= len(self._words):
            nvdaUi.message("No word selected.")
            return
        removed = self._words[index]
        value = removed["value"]

        # Optimistic here too, for the same reason as onAdd -- and
        # restores focus to the item that slides into the removed
        # row's position (or the new last row), not back to the top.
        del self._words[index]
        newIndex = min(index, len(self._words) - 1)
        self._renderWords(target_index=newIndex)
        # _renderWords only updates the ListCtrl's internal item-focus
        # marker (Focus()/Select()) -- if real OS focus was on
        # removeButton (which is how this action normally gets
        # triggered), it stays there unless explicitly moved back here.
        self.wordList.SetFocus()
        nvdaUi.message(f'Removed "{value}".')

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_muted_word(atprotoClient, value)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, index, removed, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, index, removed, error):
        if not error:
            return
        # Roll back -- put it back where it was, clamped to the
        # current length.
        insertAt = max(0, min(index, len(self._words)))
        self._words.insert(insertAt, removed)
        self._renderWords(target_index=insertAt)
        nvdaUi.message(f'Could not remove "{removed["value"]}": {error}')

class NVSkySettingsDialog(wx.Dialog):
    """
    Standalone NVSky settings window -- replaces the old NVSkySettingsPanel
    that was nested inside NVDA's own multi-category Settings dialog.

    Root cause of the double-announcement fix: nesting our own
    wx.Notebook inside NVDA's Settings dialog (itself a notebook-like
    category switcher) meant native Tab-key traversal into a page's
    first control was real OS-driven focus transfer with no hook this
    codebase could intercept -- this is what caused ListCtrl-backed
    panels (Accounts, Feed manager, Muted words) to announce their
    focused row twice. Hosting our own single-level wx.Notebook inside
    a plain wx.Dialog (same pattern MainWindow already uses for its own
    tabs) removes the double-nesting, and reuses MainWindow's proven
    onPageChanging/onPageChanged pattern: move real focus onto the
    notebook itself right before a page swap, then grant real focus
    deliberately, exactly once, from onPageChanged via each panel's own
    onTabActivated() hook.
    """

    def __init__(self, parent, on_account_changed=None):
        super().__init__(
            parent, title="NVSky Settings", size=(700, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._onAccountChangedExternal = on_account_changed

        # Same purpose as MainWindow._activationSuppressed -- True until
        # every page has been added (AddPage on an empty notebook
        # auto-selects the first page and fires a real
        # EVT_NOTEBOOK_PAGE_CHANGED during construction, before the
        # dialog is even shown).
        self._activationSuppressed = True

        panel = wx.Panel(self)
        frameSizer = wx.BoxSizer(wx.VERTICAL)
        frameSizer.Add(panel, proportion=1, flag=wx.EXPAND)
        self.SetSizer(frameSizer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(panel)
        sizer.Add(self.notebook, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        closeBtn = wx.Button(panel, wx.ID_CLOSE, label="&Close")
        sizer.Add(closeBtn, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)

        panel.SetSizer(sizer)

        self.accountsPanel = AccountsPanel(self.notebook, onAccountChanged=self._onAccountChanged)
        self.generalPanel = GeneralPanel(self.notebook)
        self.displayPanel = DisplayPanel(self.notebook)
        self.feedManagerPanel = FeedManagerPanel(self.notebook)
        self.soundPanel = SoundPanel(self.notebook)
        self.profilePanel = ProfilePanel(self.notebook)
        self.mutedWordsPanel = MutedWordsPanel(self.notebook)

        self.notebook.AddPage(self.accountsPanel, "Accounts")
        self.notebook.AddPage(self.generalPanel, "General")
        self.notebook.AddPage(self.displayPanel, "Display")
        self.notebook.AddPage(self.feedManagerPanel, "Feed manager")
        self.notebook.AddPage(self.soundPanel, "Sound")
        self.notebook.AddPage(self.profilePanel, "Profile")
        self.notebook.AddPage(self.mutedWordsPanel, "Muted words")

        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.onPageChanging)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onPageChanged)
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.CentreOnScreen()
        self._activationSuppressed = False
        # Page 0 (Accounts) was auto-selected during AddPage above while
        # still suppressed -- focus it once now the same deliberate way
        # onPageChanged would, since no real PAGE_CHANGED event fired
        # for that initial automatic selection.
        wx.CallAfter(self._activatePage, 0)

    def _onAccountChanged(self):
        # Same actions as the old NVSkySettingsPanel.makeSettings'
        # onAccountChanged closure -- reload every per-account panel,
        # then tell an already-open MainWindow to rebuild its tabs.
        self.profilePanel.reload()
        self.mutedWordsPanel.reload()
        self.feedManagerPanel.reload()
        if self._onAccountChangedExternal:
            self._onAccountChangedExternal()

    def onPageChanging(self, evt):
        # Mirrors MainWindow.onPageChanging -- moves real focus to the
        # notebook itself BEFORE the page actually swaps, so no child
        # control is still focused right as its page becomes hidden.
        self.notebook.SetFocus()
        evt.Skip()

    def onPageChanged(self, evt):
        evt.Skip()
        if self._activationSuppressed:
            return
        index = evt.GetSelection()
        if index != wx.NOT_FOUND:
            self._activatePage(index)

    def _activatePage(self, index):
        panel = self.notebook.GetPage(index)
        # Announce the tab name FIRST, before granting real focus --
        # same ordering FeedWindow.onTabActivated etc already use in
        # MainWindow (speak "<tab> tab", then move focus), which is
        # confirmed to not lose the race against the focus-change
        # announcement that follows. Centralized here (rather than
        # repeated in every panel's own onTabActivated) since these
        # panels don't carry a TAB_NAME attribute the way MainWindow's
        # tabs do -- the notebook's own page text is the single source
        # of truth for the label either way.
        pageText = self.notebook.GetPageText(index)
        nvdaUi.message(f"{pageText} tab")
        onActivated = getattr(panel, "onTabActivated", None)
        if callable(onActivated):
            onActivated()

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == wx.WXK_ESCAPE:
            self.Close()
            return
        if evt.ControlDown() and ord("1") <= keyCode <= ord("9"):
            index = keyCode - ord("1")
            if index < self.notebook.GetPageCount():
                self.notebook.SetSelection(index)
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()
