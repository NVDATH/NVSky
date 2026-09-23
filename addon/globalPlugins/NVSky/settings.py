"""
NVSky settings UI.

Standalone dialog with OK / Cancel / Apply. Nothing is saved until OK or
Apply: local settings implement apply(), server-backed settings implement
pendingTasks() (a list of _Task run in one background thread).
Account actions (add/remove/switch) and Clear all cache stay immediate.
"""

import os
import re
import threading
import webbrowser
import wx

import gui
import gui.nvdaControls
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import timeutils
from . import soundpack

APP_PASSWORD_URL = "https://bsky.app/settings/app-passwords"
STRFTIME_REFERENCE_URL = "https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes"

APP_PASSWORD_PATTERN = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$", re.IGNORECASE)

TIME_MODE_CHOICES = [
    ("Relative (up to 24h, then full date/time)", "relative_24h"),
    ("Relative always (minutes/hours/days/months/years ago)", "relative_always"),
    ("Full date and time", "absolute"),
    ("Custom format...", "custom"),
]


class _Task:
    """One staged server-side change: run(client) on the worker thread, commit() on the UI thread after success."""

    def __init__(self, label, run, commit):
        self.label = label
        self.run = run
        self.commit = commit


class LoginDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Log in to Bluesky")

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the handle/email login field.
        handleLabel = wx.StaticText(self, label=_("&Handle or email:"))
        sizer.Add(handleLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.handleCtrl = wx.TextCtrl(self)
        sizer.Add(self.handleCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        # Translators: Label for the App Password login field.
        passwordLabel = wx.StaticText(self, label=_("App &Password (NOT your regular account password):"))
        sizer.Add(passwordLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.passwordCtrl = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        sizer.Add(self.passwordCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        # Translators: Button that opens the Bluesky App Password generation page in a browser.
        self.appPasswordButton = wx.Button(self, label=_("&Generate App Password..."))
        self.appPasswordButton.Bind(wx.EVT_BUTTON, self.onGenerateAppPassword)
        sizer.Add(self.appPasswordButton, flag=wx.LEFT | wx.TOP, border=10)

        self.statusLabel = wx.StaticText(self, label="")
        sizer.Add(self.statusLabel, flag=wx.LEFT | wx.TOP, border=10)

        buttonSizer = wx.StdDialogButtonSizer()
        # Translators: Button to submit the login form.
        self.loginButton = wx.Button(self, wx.ID_OK, label=_("&Log in"))
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
            # Translators: Status text when the handle or App Password field is empty.
            self.statusLabel.SetLabel(_("Handle and App Password are both required."))
            return

        if not APP_PASSWORD_PATTERN.match(password) and not self._passwordWarningAcknowledged:
            self._passwordWarningAcknowledged = True
            # Translators: Warning shown when the entered password doesn't look like an App Password.
            self.statusLabel.SetLabel(
                _("This doesn't look like an App Password (expected format: abcd-efgh-ijkl-mnop, "
                  "not your regular account password). Press Log in again to continue anyway.")
            )
            return

        self.loginButton.Disable()
        # Translators: Status text while a login attempt is in progress.
        self.statusLabel.SetLabel(_("Logging in..."))
        soundpack.start_progress()

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
        soundpack.stop_progress()
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

        # Translators: Label above the list of logged-in Bluesky accounts.
        listLabel = wx.StaticText(self, label=_("Accounts:"))
        sizer.Add(listLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.accountList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for the account handle in the accounts list.
        self.accountList.InsertColumn(0, _("Handle"), width=300)
        sizer.Add(self.accountList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to log into and add a new Bluesky account.
        self.addButton = wx.Button(self, label=_("&Add account..."))
        # Translators: Button to make the selected account the active one.
        self.setActiveButton = wx.Button(self, label=_("&Set as active"))
        # Translators: Button to remove the selected Bluesky account.
        self.removeButton = wx.Button(self, label=_("&Remove account"))
        buttonRow.Add(self.addButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.setActiveButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.removeButton)
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
            # Translators: Suffix appended to the currently-active account's handle in the accounts list.
            marker = _(" (active)") if acc["is_active"] else ""
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
        if self._accounts:
            self.accountList.SetFocus()
        else:
            self.addButton.SetFocus()

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
            # Translators: Announced after successfully logging into a new account. {} is the account handle.
            nvdaUi.message(_("Logged in as {}").format(dlg.result["handle"]))
            if self._onAccountChanged:
                self._onAccountChanged()
        dlg.Destroy()

    def onRemove(self, evt):
        account = self._getSelectedAccount()
        if account is None:
            # Translators: Spoken when no account row is selected for an action that needs one.
            nvdaUi.message(_("No account selected."))
            return

        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body when removing an account. {} is the account handle.
            _("Remove {}? This deletes its cached posts and stored "
              "App Password from this computer. This can't be undone.").format(account["handle"]),
            # Translators: Title of the confirm-remove-account dialog.
            _("Remove account"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        result = confirm.ShowModal()
        confirm.Destroy()
        if result != wx.ID_YES:
            return

        db.remove_account(account["id"])
        self.refresh()
        self.addButton.SetFocus()
        # Translators: Announced after removing an account. {} is the account handle.
        nvdaUi.message(_("Removed {}").format(account["handle"]))
        if self._onAccountChanged:
            self._onAccountChanged()

    def onSetActive(self, evt):
        account = self._getSelectedAccount()
        if account is None:
            nvdaUi.message(_("No account selected."))
            return
        db.set_active_account(account["id"])
        self.refresh(focusAccountId=account["id"])
        # Translators: Announced after switching the active account. {} is the account handle.
        nvdaUi.message(_("{} is now the active account.").format(account["handle"]))
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

BG_SYNC_CATEGORY_LABELS = [
    # Translators: Background sync category label (Home feed).
    ("home", _("Home")),
    # Translators: Background sync category label (Notifications).
    ("notifications", _("Notifications")),
    # Translators: Background sync category label (Saved posts).
    ("saved", _("Saved / Likes")),
    # Translators: Background sync category label (Chat).
    ("chat", _("Chat")),
    # Translators: Background sync category label (Lists).
    ("lists", _("Lists")),
    # Translators: Background sync category label (search results / feed previews).
    ("search", _("Search / feed previews")),
    # Translators: Background sync category label (profile, followers, and post-related people lists).
    ("profile", _("Profile / people & post lists")),
    # Translators: Background sync category label (thread views).
    ("thread", _("Thread")),
]

# Human-readable label per soundpack.EVENT_KEYS entry.
SOUND_EVENT_LABELS = {
    # Translators: Sound event label.
    "like": _("Like"),
    # Translators: Sound event label.
    "unlike": _("Unlike"),
    # Translators: Sound event label.
    "repost": _("Repost"),
    # Translators: Sound event label.
    "unrepost": _("Undo repost"),
    # Translators: Sound event label.
    "save": _("Save"),
    # Translators: Sound event label.
    "unsave": _("Unsave"),
    # Translators: Sound event label.
    "send_post": _("Post sent"),
    # Translators: Sound event label.
    "delete": _("Delete / remove"),
    # Translators: Sound event label.
    "follow": _("Follow"),
    # Translators: Sound event label.
    "unfollow": _("Unfollow"),
    # Translators: Sound event label.
    "block_mute": _("Mute / block / report"),
    # Translators: Sound event label.
    "send_message": _("Chat message sent"),
    # Translators: Sound event label.
    "new_message": _("New chat message received"),
    # Translators: Sound event label.
    "notification": _("New notification"),
    # Translators: Sound event label.
    "open_tab": _("Tab opened"),
    # Translators: Sound event label.
    "close_tab": _("Tab closed"),
    # Translators: Sound event label.
    "boundary": _("Reached the end of a list"),
    # Translators: Sound event label.
    "error": _("Error"),
    # Translators: Sound event label.
    "ready": _("Sync / loading finished"),
    # Translators: Sound event label.
    "max_length": _("Text exceeds the length limit"),
    # Translators: Sound event label.
    "content_warning": _("Content-warned post focused"),
    # Translators: Sound event label.
    "main_open": _("NVSky window opened"),
    # Translators: Sound event label.
    "main_close": _("NVSky window closed"),
}


class GeneralPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the Enter-key-action dropdown in Settings > General.
        enterLabel = wx.StaticText(self, label=_("Enter &key action on a post:"))
        sizer.Add(enterLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.enterActionChoice = wx.Choice(self, choices=[label for _key, label in ENTER_ACTION_CHOICES])
        currentEnterAction = db.get_ui_state("enter_action") or "view_thread"
        selectedIndex = next(
            (i for i, (key, _label) in enumerate(ENTER_ACTION_CHOICES) if key == currentEnterAction), 0
        )
        self.enterActionChoice.SetSelection(selectedIndex)
        sizer.Add(self.enterActionChoice, flag=wx.LEFT | wx.TOP, border=10)

        # Translators: Label above the per-category background-sync speech checklist.
        bgSyncAnnounceLabel = wx.StaticText(self, label=_("&Speak when background sync finds new content in:"))
        sizer.Add(bgSyncAnnounceLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.bgSyncAnnounceList = gui.nvdaControls.CustomCheckListBox(
            self, choices=[label for _key, label in BG_SYNC_CATEGORY_LABELS]
        )
        announceCategories = db.get_bg_sync_announce_categories()
        self.bgSyncAnnounceList.CheckedItems = [
            i for i, (key, _label) in enumerate(BG_SYNC_CATEGORY_LABELS) if key in announceCategories
        ]
        if BG_SYNC_CATEGORY_LABELS:
            self.bgSyncAnnounceList.SetSelection(0)
        sizer.Add(self.bgSyncAnnounceList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        bgSyncNote = wx.StaticText(
            self,
            # Translators: Explanatory note above the background sync interval fields.
            label=_("Background sync automatically checks each category below for updates "
                    "even while you're not actively viewing that tab. Set to 0 to disable a "
                    "category entirely."),
        )
        sizer.Add(bgSyncNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        self._bgSyncSpins = {}
        bgSyncFields = [
            # Translators: Background sync category label (Home feed).
            ("home", _("&Home")),
            # Translators: Background sync category label (Notifications).
            ("notifications", _("&Notifications")),
            # Translators: Background sync category label (Saved posts).
            ("saved", _("&Saved / Likes")),
            # Translators: Background sync category label (Chat).
            ("chat", _("&Chat")),
            # Translators: Background sync category label (Lists).
            ("lists", _("&Lists")),
            # Translators: Background sync category label (search results / feed previews).
            ("search", _("S&earch / feed previews")),
            # Translators: Background sync category label (profile, followers, and post-related people lists).
            ("profile", _("&Profile / people & post lists")),
            # Translators: Background sync category label (thread views).
            ("thread", _("&Thread")),
        ]
        for category, label in bgSyncFields:
            row = wx.BoxSizer(wx.HORIZONTAL)
            # Translators: Label for one background-sync-interval spin control. {} is the category name (e.g. "Home").
            rowLabel = wx.StaticText(self, label=_("{} background sync (minutes, 0 = off):").format(label))
            spin = wx.SpinCtrl(
                self, min=0, max=180, initial=db.get_bg_sync_interval(category),
                name=f"{label} background sync interval (minutes)",
            )
            row.Add(rowLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
            row.Add(spin)
            sizer.Add(row, flag=wx.LEFT | wx.TOP, border=10)
            self._bgSyncSpins[category] = spin

        # Translators: Label above the "clear all cache" section in Settings > General.
        cacheLabel = wx.StaticText(self, label=_("All cached data (active account):"))
        sizer.Add(cacheLabel, flag=wx.LEFT | wx.TOP, border=10)
        cacheNote = wx.StaticText(
            self,
            # Translators: Explanatory note above the "Clear all cache" button.
            label=_("Clears every cached post, notification, chat message, and list "
                    "for the active account -- similar to removing and re-adding the "
                    "account, but you stay logged in. Every tab comes up empty until "
                    "the next sync."),
        )
        cacheNote.Wrap(500)
        sizer.Add(cacheNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        # Translators: Button that clears all cached data for the active account.
        self.clearCacheButton = wx.Button(self, label=_("&Clear all cache"))
        sizer.Add(self.clearCacheButton, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.clearCacheButton.Bind(wx.EVT_BUTTON, self.onClearCache)

    def onTabActivated(self):
        self.enterActionChoice.SetFocus()

    def apply(self):
        db.set_ui_state("enter_action", ENTER_ACTION_CHOICES[self.enterActionChoice.GetSelection()][0])
        for category, spin in self._bgSyncSpins.items():
            db.set_bg_sync_interval(category, spin.GetValue())
        checkedCategories = {BG_SYNC_CATEGORY_LABELS[i][0] for i in self.bgSyncAnnounceList.CheckedItems}
        db.set_bg_sync_announce_categories(checkedCategories)

    def onClearCache(self, evt):
        account = db.get_active_account()
        if account is None:
            nvdaUi.message(_("No active account."))
            return

        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for clearing all cached data. {} is the account handle.
            _("Clear ALL cached data for {}? This clears every "
              "cached post, notification, chat message, and list -- you'll need "
              "to sync from the network again. This can't be undone.").format(account["handle"]),
            # Translators: Title of the clear-all-cache confirmation dialog.
            _("Clear all cache"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        result = confirm.ShowModal()
        confirm.Destroy()
        if result != wx.ID_YES:
            return

        db.clear_all_cache(account["id"])
        # Translators: Announced after clearing all cached data. {} is the account handle.
        nvdaUi.message(_("All cache cleared for {}").format(account["handle"]))

        # Rebuild every tab so it reflects the now-empty cache.
        from . import rebuild_main_window_tabs
        rebuild_main_window_tabs()


class DisplayPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the feed sort-order dropdown.
        sortLabel = wx.StaticText(self, label=_("Feed &order:"))
        sizer.Add(sortLabel, flag=wx.LEFT | wx.TOP, border=10)
        # Translators: Sort-order dropdown choices: newest posts first, oldest posts first.
        self.sortOrderChoice = wx.Choice(self, choices=[_("Newest first"), _("Oldest first")])
        currentSort = db.get_ui_state("sort_order") or "newest_first"
        self.sortOrderChoice.SetSelection(0 if currentSort == "newest_first" else 1)
        sizer.Add(self.sortOrderChoice, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label for the "show author as display name or handle" dropdown.
        authorLabel = wx.StaticText(self, label=_("Show &author as:"))
        sizer.Add(authorLabel, flag=wx.LEFT | wx.TOP, border=10)
        # Translators: Author dropdown choices: the author's display name, the author's @handle.
        self.authorModeChoice = wx.Choice(self, choices=[_("Display name"), _("Handle")])
        authorMode = db.get_ui_state("column1_display") or "display_name"
        self.authorModeChoice.SetSelection(0 if authorMode == "display_name" else 1)
        sizer.Add(self.authorModeChoice, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label for the post-time-format dropdown.
        timeLabel = wx.StaticText(self, label=_("Post &time format:"))
        sizer.Add(timeLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.timeModeChoice = wx.Choice(self, choices=[label for label, _key in TIME_MODE_CHOICES])
        currentMode = db.get_ui_state("time_format_mode") or "relative_24h"
        modeValues = [value for _label, value in TIME_MODE_CHOICES]
        self.timeModeChoice.SetSelection(modeValues.index(currentMode) if currentMode in modeValues else 0)
        self.timeModeChoice.Bind(wx.EVT_CHOICE, lambda e: self._updateCustomPatternState())
        sizer.Add(self.timeModeChoice, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        customLabel = wx.StaticText(
            self,
            # Translators: Label for the custom strftime pattern field, used when "Custom format" is selected above.
            label=_("Custo&m format (Python strftime pattern, used when \"Custom format\" is selected above):"),
        )
        sizer.Add(customLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.customPatternCtrl = wx.TextCtrl(
            self, value=db.get_ui_state("time_format_custom_pattern") or "%Y-%m-%d %H:%M:%S"
        )
        sizer.Add(self.customPatternCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Button that opens the strftime format reference page in a browser.
        self.formatHelpButton = wx.Button(self, label=_("&strftime format reference..."))
        self.formatHelpButton.Bind(wx.EVT_BUTTON, self.onFormatHelp)
        sizer.Add(self.formatHelpButton, flag=wx.LEFT | wx.TOP, border=10)
        self._updateCustomPatternState()

        # Translators: Label above the tabs checklist in Settings > Display.
        tabsLabel = wx.StaticText(self, label=_("Show these ta&bs (Home is always shown):"))
        sizer.Add(tabsLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.tabsList = gui.nvdaControls.CustomCheckListBox(
            self, choices=[label for _key, label in OPTIONAL_TAB_CHOICES]
        )
        enabledTabs = db.get_enabled_tabs()
        self.tabsList.CheckedItems = [
            i for i, (key, _label) in enumerate(OPTIONAL_TAB_CHOICES) if key in enabledTabs
        ]
        self.tabsList.SetSelection(0)
        sizer.Add(self.tabsList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        contentLabelNote = wx.StaticText(
            self,
            # Translators: Explanatory note above the content-label visibility list.
            label=_(
                "The first row turns adult content on or off, as in Bluesky's own settings; "
                "while it is off, the adult categories (all except Nudity) are hidden. "
                "Press Space on a row to cycle Show -> Warn -> Hide. \"Warn\" hides "
                "the post's text and embed behind a content-warning placeholder -- "
                "press Ctrl+Space on the focused post (in a feed) to hear it once. "
                "\"Hide\" removes the post from feeds entirely. "
                "Changes are saved when you press OK or Apply."
            ),
        )
        contentLabelNote.Wrap(500)
        sizer.Add(contentLabelNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Status text while content label settings load.
        self.contentLabelStatusLabel = wx.StaticText(self, label=_("Loading content label settings..."))
        sizer.Add(self.contentLabelStatusLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label above the content-label categories list.
        contentLabelListLabel = wx.StaticText(self, label=_("Content &label categories:"))
        sizer.Add(contentLabelListLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.contentLabelList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for a content label's display name.
        self.contentLabelList.InsertColumn(0, _("Category"), width=220)
        # Translators: Column header for a content label's current visibility.
        self.contentLabelList.InsertColumn(1, _("Visibility"), width=120)
        sizer.Add(self.contentLabelList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        notificationNote = wx.StaticText(
            self,
            # Translators: Explanatory note above the notification categories list.
            label=_(
                "Controls which activity notifies you (server-side -- affects the "
                "official app too). Press Space on a row to cycle Off, Everyone, "
                "People you follow (or just Off/On for categories with no filter). "
                "Changes are saved when you press OK or Apply."
            ),
        )
        notificationNote.Wrap(500)
        sizer.Add(notificationNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Status text while notification settings load.
        self.notificationStatusLabel = wx.StaticText(self, label=_("Loading notification settings..."))
        sizer.Add(self.notificationStatusLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label above the notification categories list.
        notificationListLabel = wx.StaticText(self, label=_("&Notify me about:"))
        sizer.Add(notificationListLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.notificationCategoryList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for a notification category's name.
        self.notificationCategoryList.InsertColumn(0, _("Category"), width=260)
        # Translators: Column header for a notification category's current setting.
        self.notificationCategoryList.InsertColumn(1, _("Notify from"), width=140)
        sizer.Add(self.notificationCategoryList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.contentLabelList.Bind(wx.EVT_CHAR_HOOK, self.onContentLabelCharHook)
        self.notificationCategoryList.Bind(wx.EVT_CHAR_HOOK, self.onNotificationCategoryCharHook)

        self.tabsChanged = False
        self._contentLabelPrefs = {}
        self._contentLabelBaseline = {}
        self._contentLabelReady = False
        self._contentLabelLoadedOnce = False
        self._notificationPrefs = {}
        self._notificationBaseline = {}
        self._notificationReady = False
        self._notificationLoadedOnce = False

    def onTabActivated(self):
        self.sortOrderChoice.SetFocus()
        if not self._contentLabelLoadedOnce:
            self._contentLabelLoadedOnce = True
            self._loadContentLabelPrefs()
        if not self._notificationLoadedOnce:
            self._notificationLoadedOnce = True
            self._loadNotificationPrefs()

    def _updateCustomPatternState(self):
        isCustom = TIME_MODE_CHOICES[self.timeModeChoice.GetSelection()][1] == "custom"
        self.customPatternCtrl.Enable(isCustom)
        self.formatHelpButton.Enable(isCustom)

    def onFormatHelp(self, evt):
        webbrowser.open(STRFTIME_REFERENCE_URL)

    def apply(self):
        authorMode = "display_name" if self.authorModeChoice.GetSelection() == 0 else "handle"
        db.set_ui_state("column1_display", authorMode)

        timeMode = TIME_MODE_CHOICES[self.timeModeChoice.GetSelection()][1]
        db.set_ui_state("time_format_mode", timeMode)

        db.set_ui_state("time_format_custom_pattern", self.customPatternCtrl.GetValue())
        timeutils.invalidate_time_format_cache()

        sortOrder = "newest_first" if self.sortOrderChoice.GetSelection() == 0 else "oldest_first"
        db.set_ui_state("sort_order", sortOrder)

        checkedTabs = {OPTIONAL_TAB_CHOICES[i][0] for i in self.tabsList.CheckedItems}
        if checkedTabs != db.get_enabled_tabs():
            db.set_enabled_tabs(checkedTabs)
            self.tabsChanged = True

    def pendingTasks(self):
        return self._contentLabelTasks() + self._notificationTasks()

    # ---------------- content label visibility ----------------

    def _adultContentEnabled(self):
        return self._contentLabelPrefs.get(client.ADULT_CONTENT_PREF_KEY, True)

    def _renderContentLabels(self, target_index=None):
        previouslyFocused = self.contentLabelList.GetFocusedItem()
        self.contentLabelList.Freeze()
        try:
            self.contentLabelList.DeleteAllItems()
            adultOn = self._adultContentEnabled()
            # Translators: First row of the content label list, the master adult-content switch.
            self.contentLabelList.InsertItem(0, _("Adult content"))
            # Translators: State of the adult-content switch.
            self.contentLabelList.SetItem(0, 1, _("Enabled") if adultOn else _("Disabled"))
            for i, key in enumerate(client.CONTENT_LABEL_KEYS, start=1):
                self.contentLabelList.InsertItem(i, CONTENT_LABEL_DISPLAY_NAMES.get(key, key))
                if key in client.CONTENT_LABEL_ADULT_ONLY and not adultOn:
                    # Translators: Visibility shown for a content label while adult content is off.
                    visLabel = _("Hidden (adult content is off)")
                else:
                    visibility = client.label_setting(self._contentLabelPrefs, key)
                    visLabel = dict(CONTENT_LABEL_VISIBILITY_CHOICES).get(visibility, visibility)
                self.contentLabelList.SetItem(i, 1, visLabel)

            if target_index is not None:
                index = target_index
            elif previouslyFocused != -1:
                index = previouslyFocused
            else:
                index = 0
            index = max(0, min(index, len(client.CONTENT_LABEL_KEYS)))
            self.contentLabelList.Focus(index)
            self.contentLabelList.Select(index)
        finally:
            self.contentLabelList.Thaw()

    def _loadContentLabelPrefs(self):
        # Translators: Status text while content label settings load.
        self.contentLabelStatusLabel.SetLabel(_("Loading content label settings..."))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                prefs = client.get_content_label_prefs(atprotoClient)
                error = None
            except Exception as e:
                prefs = None
                error = str(e)
            wx.CallAfter(self._onContentLabelPrefsLoaded, prefs, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onContentLabelPrefsLoaded(self, prefs, error):
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading content label settings fails. {} is the error message.
            self.contentLabelStatusLabel.SetLabel(_("Could not load content label settings: {}").format(error))
            return
        self._contentLabelPrefs = dict(prefs or {})
        self._contentLabelBaseline = dict(self._contentLabelPrefs)
        self._contentLabelReady = True
        account = db.get_active_account()
        if account is not None:
            db.set_content_label_prefs_cache(account["id"], self._contentLabelPrefs)
        self._renderContentLabels(target_index=0)
        # Translators: Status text after content label settings finish loading.
        self.contentLabelStatusLabel.SetLabel(_("Content label settings loaded."))

    def reloadContentLabels(self):
        # Account switched: pending edits belong to the old account, drop them.
        self._contentLabelLoadedOnce = True
        self._contentLabelReady = False
        self._contentLabelPrefs = {}
        self._contentLabelBaseline = {}
        self._loadContentLabelPrefs()

    def onContentLabelCharHook(self, evt):
        # EVT_CHAR_HOOK, not EVT_LIST_KEY_DOWN: a plain ListCtrl consumes Space natively first.
        if evt.GetKeyCode() != wx.WXK_SPACE or evt.HasAnyModifiers():
            evt.Skip()
            return
        if self.FindFocus() is not self.contentLabelList:
            evt.Skip()
            return
        index = self.contentLabelList.GetFocusedItem()
        if index == -1 or index > len(client.CONTENT_LABEL_KEYS):
            return
        if not self._contentLabelReady:
            # Translators: Announced when a settings list is changed before its data finished loading.
            nvdaUi.message(_("Still loading, please wait."))
            return
        if index == 0:
            self._contentLabelPrefs[client.ADULT_CONTENT_PREF_KEY] = not self._adultContentEnabled()
            self._renderContentLabels(target_index=index)
            return
        labelKey = client.CONTENT_LABEL_KEYS[index - 1]
        if labelKey in client.CONTENT_LABEL_ADULT_ONLY and not self._adultContentEnabled():
            # Translators: Announced when changing a content label while adult content is off.
            nvdaUi.message(_("Turn adult content on first."))
            return
        previousVisibility = client.label_setting(self._contentLabelPrefs, labelKey)
        order = [k for k, _n in CONTENT_LABEL_VISIBILITY_CHOICES]
        currentPos = order.index(previousVisibility) if previousVisibility in order else 0
        self._contentLabelPrefs[labelKey] = order[(currentPos + 1) % len(order)]
        self._renderContentLabels(target_index=index)

    def _contentLabelTasks(self):
        if not self._contentLabelReady:
            return []
        changes = {}
        adultNow = self._adultContentEnabled()
        if adultNow != self._contentLabelBaseline.get(client.ADULT_CONTENT_PREF_KEY, True):
            changes[client.ADULT_CONTENT_PREF_KEY] = adultNow
        for key in client.CONTENT_LABEL_KEYS:
            now = client.label_setting(self._contentLabelPrefs, key)
            if now != client.label_setting(self._contentLabelBaseline, key):
                changes[key] = now
        if not changes:
            return []
        snapshot = dict(self._contentLabelPrefs)

        def run(atprotoClient):
            for key, value in changes.items():
                if key == client.ADULT_CONTENT_PREF_KEY:
                    client.set_adult_content_enabled(atprotoClient, value)
                else:
                    client.set_content_label_pref(atprotoClient, key, value)

        def commit():
            self._contentLabelBaseline = dict(snapshot)
            account = db.get_active_account()
            if account is not None:
                db.set_content_label_prefs_cache(account["id"], snapshot)

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [_Task(_("content labels"), run, commit)]

    # ---------------- notification preferences ----------------

    def _notificationStateFor(self, category, pref):
        if not pref.get("list"):
            return "off"
        if category in client.NOTIFICATION_FILTERABLE_CATEGORIES:
            return "following" if pref.get("include") == "follows" else "everyone"
        return "on"

    def _renderNotificationPrefs(self, target_index=None):
        previouslyFocused = self.notificationCategoryList.GetFocusedItem()
        self.notificationCategoryList.Freeze()
        try:
            self.notificationCategoryList.DeleteAllItems()
            for i, category in enumerate(client.NOTIFICATION_ALL_CATEGORIES):
                self.notificationCategoryList.InsertItem(i, NOTIFICATION_CATEGORY_DISPLAY_NAMES.get(category, category))
                pref = self._notificationPrefs.get(category, {})
                state = self._notificationStateFor(category, pref)
                self.notificationCategoryList.SetItem(i, 1, NOTIFICATION_STATE_DISPLAY_NAMES.get(state, state))

            if target_index is not None:
                index = target_index
            elif previouslyFocused != -1:
                index = previouslyFocused
            else:
                index = 0
            index = max(0, min(index, len(client.NOTIFICATION_ALL_CATEGORIES) - 1))
            self.notificationCategoryList.Focus(index)
            self.notificationCategoryList.Select(index)
        finally:
            self.notificationCategoryList.Thaw()

    def _loadNotificationPrefs(self):
        # Translators: Status text while notification settings load.
        self.notificationStatusLabel.SetLabel(_("Loading notification settings..."))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                prefs = client.get_notification_prefs(atprotoClient)
                error = None
            except Exception as e:
                prefs = None
                error = str(e)
            wx.CallAfter(self._onNotificationPrefsLoaded, prefs, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onNotificationPrefsLoaded(self, prefs, error):
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading notification settings fails. {} is the error message.
            self.notificationStatusLabel.SetLabel(_("Could not load notification settings: {}").format(error))
            return
        self._notificationPrefs = {k: dict(v) for k, v in (prefs or {}).items()}
        self._notificationBaseline = {k: dict(v) for k, v in self._notificationPrefs.items()}
        self._notificationReady = True
        self._renderNotificationPrefs(target_index=0)
        # Translators: Status text after notification settings finish loading.
        self.notificationStatusLabel.SetLabel(_("Notification settings loaded."))

    def reloadNotificationPrefs(self):
        # Account switched: pending edits belong to the old account, drop them.
        self._notificationLoadedOnce = True
        self._notificationReady = False
        self._notificationPrefs = {}
        self._notificationBaseline = {}
        self._loadNotificationPrefs()

    def onNotificationCategoryCharHook(self, evt):
        if evt.GetKeyCode() != wx.WXK_SPACE or evt.HasAnyModifiers():
            evt.Skip()
            return
        if self.FindFocus() is not self.notificationCategoryList:
            evt.Skip()
            return
        index = self.notificationCategoryList.GetFocusedItem()
        if index == -1 or index >= len(client.NOTIFICATION_ALL_CATEGORIES):
            return
        if not self._notificationReady:
            nvdaUi.message(_("Still loading, please wait."))
            return
        category = client.NOTIFICATION_ALL_CATEGORIES[index]
        current = self._notificationPrefs.get(category, {})
        isFilterable = category in client.NOTIFICATION_FILTERABLE_CATEGORIES
        order = ["off", "everyone", "following"] if isFilterable else ["off", "on"]
        currentState = self._notificationStateFor(category, current)
        currentPos = order.index(currentState) if currentState in order else 0
        newState = order[(currentPos + 1) % len(order)]

        if newState == "off":
            newListEnabled, newInclude = False, current.get("include")
        elif newState == "on":
            newListEnabled, newInclude = True, None
        elif newState == "everyone":
            newListEnabled, newInclude = True, "all"
        else:  # "following"
            newListEnabled, newInclude = True, "follows"

        self._notificationPrefs[category] = {
            "list": newListEnabled, "include": newInclude, "push": current.get("push", False),
        }
        self._renderNotificationPrefs(target_index=index)

    def _notificationTasks(self):
        if not self._notificationReady:
            return []
        changed = any(
            self._notificationStateFor(c, self._notificationPrefs.get(c, {}))
            != self._notificationStateFor(c, self._notificationBaseline.get(c, {}))
            for c in client.NOTIFICATION_ALL_CATEGORIES
        )
        if not changed:
            return []
        snapshot = {k: dict(v) for k, v in self._notificationPrefs.items()}

        def run(atprotoClient):
            # set_notification_category writes every category from snapshot; its other arguments are unused.
            client.set_notification_category(atprotoClient, snapshot, "follow", False, False)

        def commit():
            self._notificationBaseline = {k: dict(v) for k, v in snapshot.items()}

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [_Task(_("notification settings"), run, commit)]


class FeedManagerPanel(wx.Panel):
    """Settings > Feed manager. Reorder/pin/remove subscribed feeds; changes are
    staged in self._feeds and sent on OK/Apply via the existing per-action writers.
    self._baselineFeeds is what the server had when last loaded/saved."""

    def __init__(self, parent):
        super().__init__(parent)
        account = db.get_active_account()
        self._accountId = account["id"] if account else None
        # list of dicts: uri/display_name/creator_handle/pinned
        self._feeds = db.get_saved_feeds_cache(self._accountId) if self._accountId else []
        self._baselineFeeds = [dict(f) for f in self._feeds]

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Initial status text in Settings > Feed manager before the local cache loads.
        self.statusLabel = wx.StaticText(self, label=_("Loading your feeds..."))
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        self.feedList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for the feed's display name.
        self.feedList.InsertColumn(0, _("Name"), width=150)
        # Translators: Column header for the feed's creator handle.
        self.feedList.InsertColumn(1, _("Creator"), width=100)
        # Translators: Column header for whether the feed is pinned.
        self.feedList.InsertColumn(2, _("Pinned"), width=60)
        sizer.Add(self.feedList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        buttonRow1 = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to move the selected feed up in the order.
        self.moveUpButton = wx.Button(self, label=_("Move &up"))
        # Translators: Button to move the selected feed down in the order.
        self.moveDownButton = wx.Button(self, label=_("Move &down"))
        # Translators: Button to pin the selected feed.
        self.togglePinButton = wx.Button(self, label=_("&Pin selected"))
        buttonRow1.Add(self.moveUpButton, flag=wx.RIGHT, border=5)
        buttonRow1.Add(self.moveDownButton, flag=wx.RIGHT, border=5)
        buttonRow1.Add(self.togglePinButton)
        sizer.Add(buttonRow1, flag=wx.LEFT | wx.TOP, border=10)

        buttonRow2 = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to remove the selected feed from the subscribed list.
        self.removeButton = wx.Button(self, label=_("&Remove selected"))
        buttonRow2.Add(self.removeButton)
        sizer.Add(buttonRow2, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self._setButtonsEnabled(bool(self._feeds))

        self.moveUpButton.Bind(wx.EVT_BUTTON, lambda evt: self.onMove(-1))
        self.moveDownButton.Bind(wx.EVT_BUTTON, lambda evt: self.onMove(1))
        self.togglePinButton.Bind(wx.EVT_BUTTON, self.onTogglePin)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.feedList.Bind(wx.EVT_LIST_ITEM_FOCUSED, lambda evt: (self._updatePinButtonLabel(), evt.Skip()))

        self._updateStatus()
        self._render(target_index=0)

        # The background refresh starts on first activation, not here, so it
        # can't land at the same instant as the deliberate focus grab.
        self._refreshedOnce = False

    def _setButtonsEnabled(self, enabled):
        for btn in (self.moveUpButton, self.moveDownButton, self.togglePinButton, self.removeButton):
            btn.Enable(enabled)

    def _updateStatus(self):
        if self._feeds or self._baselineFeeds:
            # Translators: Status text showing how many feeds are subscribed. {} is the count.
            self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)))
        else:
            self.statusLabel.SetLabel(_("Loading your feeds..."))

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        account = db.get_active_account()
        self._accountId = account["id"] if account else None
        self._feeds = db.get_saved_feeds_cache(self._accountId) if self._accountId else []
        self._baselineFeeds = [dict(f) for f in self._feeds]
        self._setButtonsEnabled(bool(self._feeds))
        self._updateStatus()
        self._render(target_index=0)
        self._refreshedOnce = False

    def onTabActivated(self):
        self.feedList.SetFocus()
        if not self._refreshedOnce:
            self._refreshedOnce = True
            self._refresh()

    def _stillAlive(self):
        try:
            top = wx.GetTopLevelParent(self)
            return bool(top) and not top.IsBeingDeleted()
        except RuntimeError:
            return False

    def _notifyHomeFeedsChanged(self):
        # Best effort: rebuild Home's filter dropdown if MainWindow is open.
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        for panel in mainWindow.getOpenTabs():
            refresh = getattr(panel, "refreshFeedFilterChoices", None)
            if callable(refresh):
                refresh()

    # ---------------- staged changes ----------------

    def _feedChanges(self):
        baseUris = [f["uri"] for f in self._baselineFeeds]
        curUris = [f["uri"] for f in self._feeds]
        curSet = set(curUris)
        removed = set(baseUris) - curSet
        basePinned = {f["uri"]: f["pinned"] for f in self._baselineFeeds}
        pinned = {f["uri"]: f["pinned"] for f in self._feeds if basePinned.get(f["uri"]) != f["pinned"]}
        reordered = [u for u in baseUris if u in curSet] != curUris
        return removed, pinned, reordered, curUris

    def pendingTasks(self):
        removed, pinned, reordered, curUris = self._feedChanges()
        if not (removed or pinned or reordered):
            return []
        snapshot = [dict(f) for f in self._feeds]
        orderedUris = curUris if reordered else []
        accountId = self._accountId

        def run(atprotoClient):
            for uri in sorted(removed):
                client.remove_feed_from_saved(atprotoClient, uri)
            for uri, value in pinned.items():
                if uri not in removed:
                    client.set_feed_pinned(atprotoClient, uri, value)
            if orderedUris and not client.reorder_saved_feeds(atprotoClient, orderedUris):
                raise RuntimeError("the feed list changed on the server; refresh and try again")

        def commit():
            self._baselineFeeds = [dict(f) for f in snapshot]
            if accountId is not None:
                db.set_saved_feeds_cache(accountId, snapshot)
            self._notifyHomeFeedsChanged()

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [_Task(_("feeds"), run, commit)]

    # ---------------- refresh from server ----------------

    def _refresh(self):
        soundpack.start_progress()

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
        soundpack.stop_progress()
        if not self._stillAlive():
            return
        if error:
            if not self._feeds:
                # Translators: Status text when loading subscribed feeds fails. {} is the error message.
                self.statusLabel.SetLabel(_("Could not load your feeds: {}").format(error))
            return
        self._setButtonsEnabled(True)
        removed, pinned, reordered, _uris = self._feedChanges()
        if removed or pinned or reordered:
            # Never clobber the user's pending edits with a late refresh.
            return
        if feeds == self._feeds:
            self._baselineFeeds = [dict(f) for f in feeds]
            return
        self._feeds = feeds
        self._baselineFeeds = [dict(f) for f in feeds]
        if self._accountId is not None:
            db.set_saved_feeds_cache(self._accountId, feeds)
        self._updateStatus()
        self._render()
        self._notifyHomeFeedsChanged()

    def _render(self, target_index=None):
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
            self.togglePinButton.SetLabel(_("&Pin selected"))
            return
        pinned = self._feeds[index]["pinned"]
        # Translators: Button to unpin the selected, currently-pinned feed.
        self.togglePinButton.SetLabel(_("&Unpin selected") if pinned else _("&Pin selected"))

    def _selectedIndex(self):
        index = self.feedList.GetFocusedItem()
        if index == -1 or index >= len(self._feeds):
            nvdaUi.message(_("No feed selected."))
            return None
        return index

    def onMove(self, direction):
        index = self._selectedIndex()
        if index is None:
            return
        newIndex = index + direction
        if newIndex < 0 or newIndex >= len(self._feeds):
            nvdaUi.message(_("Can't move further."))
            return
        self._feeds[index], self._feeds[newIndex] = self._feeds[newIndex], self._feeds[index]
        self._render(target_index=newIndex)

    def onTogglePin(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        feed = self._feeds[index]
        feed["pinned"] = not feed["pinned"]
        self._render(target_index=index)

    def onRemove(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        feed = self._feeds[index]
        del self._feeds[index]
        self._updateStatus()
        # Translators: Announced after removing a feed. {} is the feed's display name.
        nvdaUi.message(_('Removed "{}".').format(feed["display_name"]))
        self._render(target_index=index)


class SoundPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the sound-pack selection dropdown.
        packLabel = wx.StaticText(self, label=_("Sound &pack:"))
        sizer.Add(packLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.packChoice = wx.Choice(self)
        sizer.Add(self.packChoice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        packNote = wx.StaticText(
            self,
            # Translators: Explanatory note below the sound-pack picker.
            label=_(
                "Sound packs live in the add-on's SoundPack folder, one subfolder "
                "per pack -- add your own by creating a new subfolder there with "
                ".wav files named after each event below. A pack doesn't need "
                "every file; missing sounds just stay silent."
            ),
        )
        packNote.Wrap(500)
        sizer.Add(packNote, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label above the per-event sound checklist.
        eventsLabel = wx.StaticText(self, label=_("&Play a sound for:"))
        sizer.Add(eventsLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.eventList = gui.nvdaControls.CustomCheckListBox(
            self, choices=[SOUND_EVENT_LABELS.get(key, key) for key in soundpack.EVENT_KEYS]
        )
        sizer.Add(self.eventList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self._loadPackChoices()
        self._loadEventChecks()

    def onTabActivated(self):
        self.packChoice.SetFocus()

    def _loadPackChoices(self):
        self._packNames = [soundpack.SILENT_PACK] + soundpack.list_packs()
        # Translators: Sound-pack picker entry to disable all NVSky sounds.
        labels = [_("Silent / No sound")] + self._packNames[1:]
        self.packChoice.Set(labels)
        selected = db.get_soundpack_selected()
        try:
            index = self._packNames.index(selected)
        except ValueError:
            index = 0
        self.packChoice.SetSelection(index)

    def _loadEventChecks(self):
        disabled = db.get_soundpack_disabled_events()
        self.eventList.CheckedItems = [
            i for i, key in enumerate(soundpack.EVENT_KEYS) if key not in disabled
        ]
        if soundpack.EVENT_KEYS:
            self.eventList.SetSelection(0)

    def apply(self):
        index = self.packChoice.GetSelection()
        packName = self._packNames[index] if 0 <= index < len(self._packNames) else soundpack.SILENT_PACK
        db.set_soundpack_selected(packName)

        checkedKeys = {soundpack.EVENT_KEYS[i] for i in self.eventList.CheckedItems}
        disabledKeys = set(soundpack.EVENT_KEYS) - checkedKeys
        db.set_soundpack_disabled_events(disabledKeys)

        soundpack.reload()


class ProfilePanel(wx.Panel):
    """Settings > Profile. Text edits and picked avatar/banner files are staged
    and uploaded on OK/Apply."""

    def __init__(self, parent):
        super().__init__(parent)

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Initial status text in Settings > Profile before the account's profile loads.
        self.statusLabel = wx.StaticText(self, label=_("Loading profile..."))
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        # Translators: Label for the display-name field.
        nameLabel = wx.StaticText(self, label=_("&Display name:"))
        sizer.Add(nameLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.nameCtrl = wx.TextCtrl(self)
        sizer.Add(self.nameCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        # Translators: Label for the bio/description field.
        bioLabel = wx.StaticText(self, label=_("&Bio:"))
        sizer.Add(bioLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.bioCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 80))
        sizer.Add(self.bioCtrl, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        avatarRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to choose a new profile avatar image.
        self.changeAvatarButton = wx.Button(self, label=_("Change &avatar..."))
        # Translators: Button to choose a new profile banner image.
        self.changeBannerButton = wx.Button(self, label=_("Change ban&ner..."))
        avatarRow.Add(self.changeAvatarButton, flag=wx.RIGHT, border=5)
        avatarRow.Add(self.changeBannerButton)
        sizer.Add(avatarRow, flag=wx.LEFT | wx.TOP, border=10)

        self.SetSizer(sizer)

        self.nameCtrl.Disable()
        self.bioCtrl.Disable()
        self.changeAvatarButton.Disable()
        self.changeBannerButton.Disable()

        self.changeAvatarButton.Bind(wx.EVT_BUTTON, lambda e: self.onChangeImage("avatar"))
        self.changeBannerButton.Bind(wx.EVT_BUTTON, lambda e: self.onChangeImage("banner"))

        self._loaded = False
        self._baselineName = ""
        self._baselineBio = ""
        self._pendingAvatar = None
        self._pendingBanner = None
        # Lazy: fetch only when this tab is first opened.
        self._loadedOnce = False

    def onTabActivated(self):
        if not self._loadedOnce:
            self._loadedOnce = True
            self._loadProfile()
        elif self.nameCtrl.IsEnabled():
            self.nameCtrl.SetFocus()

    def _isActiveTabPage(self):
        parent = self.GetParent()
        if not isinstance(parent, wx.Notebook):
            return False
        index = parent.GetSelection()
        return index != wx.NOT_FOUND and parent.GetPage(index) is self

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        self._loadedOnce = True
        self._loaded = False
        self._pendingAvatar = None
        self._pendingBanner = None
        self.nameCtrl.Disable()
        self.bioCtrl.Disable()
        self.changeAvatarButton.Disable()
        self.changeBannerButton.Disable()
        self.statusLabel.SetLabel(_("Loading profile..."))
        self._loadProfile()

    def _loadProfile(self):
        soundpack.start_progress()

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
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading the profile fails. {} is the error message.
            self.statusLabel.SetLabel(_("Could not load profile: {}").format(error))
            return
        self._baselineName = profile.get("display_name") or ""
        self._baselineBio = profile.get("description") or ""
        self.nameCtrl.SetValue(self._baselineName)
        self.bioCtrl.SetValue(self._baselineBio)
        # Translators: Status text showing which account's profile is being edited. {} is the handle.
        self.statusLabel.SetLabel(_("Editing @{}").format(profile["handle"]))
        self.nameCtrl.Enable()
        self.bioCtrl.Enable()
        self.changeAvatarButton.Enable()
        self.changeBannerButton.Enable()
        self._loaded = True
        if self._isActiveTabPage():
            self.nameCtrl.SetFocus()

    def onChangeImage(self, kind):
        # Translators: File-picker dialog title for choosing a new avatar image.
        # Translators: File-picker dialog title for choosing a new banner image.
        title = _("Choose a new avatar") if kind == "avatar" else _("Choose a new banner")
        with wx.FileDialog(
            self, title,
            # Translators: File type filter shown in the avatar/banner file picker.
            wildcard=_("Image files (*.jpg;*.jpeg;*.png)|*.jpg;*.jpeg;*.png"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()

        if kind == "avatar":
            self._pendingAvatar = path
        else:
            self._pendingBanner = path
        # Translators: The word "Avatar", used in status/spoken messages about the profile image.
        # Translators: The word "Banner", used in status/spoken messages about the profile banner.
        kindLabel = _("Avatar") if kind == "avatar" else _("Banner")
        # Translators: Announced after picking a profile image. First {} is "Avatar"/"Banner", second {} is the file name.
        message = _("{} selected: {}. It uploads when you press OK or Apply.").format(kindLabel, os.path.basename(path))
        self.statusLabel.SetLabel(message)
        nvdaUi.message(message)

    def _imageTask(self, kind, path):
        def run(atprotoClient):
            if kind == "avatar":
                client.update_profile_avatar(atprotoClient, path)
            else:
                client.update_profile_banner(atprotoClient, path)

        def commit():
            if kind == "avatar":
                self._pendingAvatar = None
            else:
                self._pendingBanner = None

        # Translators: Name of a settings item, used in "Could not save: {}" messages.
        # Translators: Name of a settings item, used in "Could not save: {}" messages.
        return _Task(_("avatar") if kind == "avatar" else _("banner"), run, commit)

    def pendingTasks(self):
        if not self._loaded:
            return []
        tasks = []
        name = self.nameCtrl.GetValue()
        bio = self.bioCtrl.GetValue()
        if name != self._baselineName or bio != self._baselineBio:
            def runText(atprotoClient):
                client.update_profile_text(atprotoClient, name, bio)

            def commitText():
                self._baselineName = name
                self._baselineBio = bio

            # Translators: Name of a settings group, used in "Could not save: {}" messages.
            tasks.append(_Task(_("profile text"), runText, commitText))
        if self._pendingAvatar:
            tasks.append(self._imageTask("avatar", self._pendingAvatar))
        if self._pendingBanner:
            tasks.append(self._imageTask("banner", self._pendingBanner))
        return tasks


class MutedWordsPanel(wx.Panel):
    """Settings > Muted words. Adds/removals are staged and sent on OK/Apply."""

    def __init__(self, parent):
        super().__init__(parent)
        self._words = []
        self._baselineWords = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Initial status text in Settings > Muted words before the list loads.
        self.statusLabel = wx.StaticText(self, label=_("Loading muted words..."))
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        self.wordList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for a muted word or tag.
        self.wordList.InsertColumn(0, _("Word / tag"), width=300)
        sizer.Add(self.wordList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to add a new muted word or tag.
        self.addButton = wx.Button(self, label=_("&Add"))
        # Translators: Button to remove the selected muted word or tag.
        self.removeButton = wx.Button(self, label=_("&Remove selected"))
        buttonRow.Add(self.addButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.removeButton)
        sizer.Add(buttonRow, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.addButton.Disable()
        self.removeButton.Disable()

        self.addButton.Bind(wx.EVT_BUTTON, self.onAdd)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)

        # Lazy: fetch only when this tab is first opened.
        self._loadedOnce = False

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        self._loadedOnce = True
        self._words = []
        self._baselineWords = []
        self.addButton.Disable()
        self.removeButton.Disable()
        self.statusLabel.SetLabel(_("Loading muted words..."))
        self._load()

    def onTabActivated(self):
        if not self._loadedOnce:
            self._loadedOnce = True
            self._load()
        else:
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
            # Translators: Status text showing how many words/tags are muted. {} is the count.
            self.statusLabel.SetLabel(_("{} muted word(s)/tag(s).").format(len(self._words)))

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
        soundpack.start_progress()

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
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading muted words fails. {} is the error message.
            self.statusLabel.SetLabel(_("Could not load muted words: {}").format(error))
            return
        self._words = list(words or [])
        self._baselineWords = [dict(w) for w in self._words]
        self._renderWords(target_index=0)
        self.addButton.Enable()
        self.removeButton.Enable()
        if self._isActiveTabPage():
            self.wordList.SetFocus()

    def onAdd(self, evt):
        dlg = wx.TextEntryDialog(
            self,
            # Translators: Prompt in the add-muted-word dialog.
            _("Word or tag to mute:"),
            # Translators: Title of the add-muted-word dialog.
            _("Add muted word"),
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        value = dlg.GetValue().strip()
        dlg.Destroy()
        if not value:
            return
        if any(w["value"] == value for w in self._words):
            # Translators: Spoken when the word being added is already muted. {} is the word/tag.
            nvdaUi.message(_('"{}" is already muted.').format(value))
            return

        self._words.append({"id": None, "value": value, "targets": ["content", "tag"]})
        self._renderWords(target_index=len(self._words) - 1)
        self.wordList.SetFocus()

    def onRemove(self, evt):
        index = self.wordList.GetFocusedItem()
        if index == -1 or index >= len(self._words):
            nvdaUi.message(_("No word selected."))
            return
        value = self._words[index]["value"]
        del self._words[index]
        self._renderWords(target_index=min(index, len(self._words) - 1))
        self.wordList.SetFocus()
        # Translators: Announced after removing a muted word. {} is the word/tag.
        nvdaUi.message(_('Removed "{}".').format(value))

    def pendingTasks(self):
        current = {w["value"] for w in self._words}
        base = {w["value"] for w in self._baselineWords}
        added = sorted(current - base)
        removed = sorted(base - current)
        if not (added or removed):
            return []
        snapshot = [dict(w) for w in self._words]

        def run(atprotoClient):
            for value in removed:
                client.remove_muted_word(atprotoClient, value)
            for value in added:
                client.add_muted_word(atprotoClient, value)

        def commit():
            self._baselineWords = [dict(w) for w in snapshot]

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [_Task(_("muted words"), run, commit)]


ACTOR_LIST_CATEGORY_KEYS = ["muted", "blocked"]


class MutedBlockedActorsPanel(wx.Panel):
    """
    Settings > Muted users / Blocked users (one instance per category). The
    list is re-fetched fresh on tab activation. "Undo checked" only stages the change:
    checked users leave the list now and are unmuted/unblocked on OK/Apply.

    A CustomCheckListBox with zero items raises a real wxAssertionError on
    Windows, so it stays hidden whenever there is nothing to show and focus
    falls back to the Refresh button.
    """

    def __init__(self, parent, category):
        super().__init__(parent)
        self._category = category  # "muted" or "blocked"
        self._actors = []
        self._pendingRemovals = {"muted": {}, "blocked": {}}  # category -> {did: actor}

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Initial status text while the user list loads.
        self.statusLabel = wx.StaticText(self, label=_("Loading, please wait..."))
        sizer.Add(self.statusLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label above the users checklist.
        self.listLabel = wx.StaticText(self, label=_("&Users (check to select several):"))
        sizer.Add(self.listLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.actorList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.actorList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.listLabel.Hide()
        self.actorList.Hide()

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to stage unmuting/unblocking the checked users. Relabeled per category.
        self.removeButton = wx.Button(self, label=_("&Undo checked"))
        # Translators: Button to refresh the user list.
        self.refreshButton = wx.Button(self, label=_("&Refresh"))
        buttonRow.Add(self.removeButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.refreshButton)
        sizer.Add(buttonRow, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.removeButton.Hide()
        self.removeButton.Disable()
        self.refreshButton.Disable()

        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.refreshButton.Bind(wx.EVT_BUTTON, lambda e: self._load())

        self._updateRemoveButtonLabel()

    def _currentCategory(self):
        return self._category

    def _updateRemoveButtonLabel(self):
        labels = {
            # Translators: Button to unmute every checked user.
            "muted": _("Un&mute checked"),
            # Translators: Button to unblock every checked user.
            "blocked": _("Unbloc&k checked"),
        }
        self.removeButton.SetLabel(labels[self._currentCategory()])

    def onTabActivated(self):
        if self._actors:
            self.actorList.SetFocus()
        else:
            self.refreshButton.SetFocus()
        self._load()

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        self._pendingRemovals = {"muted": {}, "blocked": {}}
        self._load()

    def _isActiveTabPage(self):
        parent = self.GetParent()
        if not isinstance(parent, wx.Notebook):
            return False
        index = parent.GetSelection()
        return index != wx.NOT_FOUND and parent.GetPage(index) is self

    def _actorLabel(self, actor):
        # Translators: Fallback shown for a user with no display name. Used as "@handle ({})".
        return f'@{actor["handle"]} ({actor.get("display_name") or _("no display name")})'

    def _categoryCountText(self, category, count):
        texts = {
            # Translators: Status text showing how many users are muted. {} is the count.
            "muted": _("{} muted user(s).").format(count),
            # Translators: Status text showing how many users are blocked. {} is the count.
            "blocked": _("{} blocked user(s).").format(count),
        }
        return texts[category]

    def _renderActors(self, target_index=None):
        hasActors = bool(self._actors)
        self.listLabel.Show(hasActors)
        self.actorList.Show(hasActors)
        self.removeButton.Show(hasActors)
        self.removeButton.Enable(hasActors)

        self.statusLabel.SetLabel(self._categoryCountText(self._currentCategory(), len(self._actors)))

        if not hasActors:
            self.actorList.Set([])
            self.Layout()
            return

        previousSelection = self.actorList.GetSelection()
        self.actorList.Set([self._actorLabel(a) for a in self._actors])
        self.actorList.CheckedItems = []
        if target_index is not None:
            index = max(0, min(target_index, len(self._actors) - 1))
        elif previousSelection != wx.NOT_FOUND and previousSelection < len(self._actors):
            index = previousSelection
        else:
            index = 0
        self.actorList.SetSelection(index)
        self.Layout()

    def _load(self):
        # Translators: Status text while the user list loads.
        self.statusLabel.SetLabel(_("Loading, please wait..."))
        self.removeButton.Disable()
        self.refreshButton.Disable()
        soundpack.start_progress()
        category = self._currentCategory()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if category == "muted":
                    actors = client.get_muted_actors(atprotoClient)
                else:
                    actors = client.get_blocked_actors(atprotoClient)
                error = None
            except Exception as e:
                actors = None
                error = str(e)
            wx.CallAfter(self._onLoaded, category, actors, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoaded(self, category, actors, error):
        soundpack.stop_progress()
        if category != self._currentCategory():
            return  # user already switched categories again -- stale result
        if error:
            labels = {
                # Translators: Status text when loading muted users fails. {} is the error message.
                "muted": _("Could not load muted users: {}").format(error),
                # Translators: Status text when loading blocked users fails. {} is the error message.
                "blocked": _("Could not load blocked users: {}").format(error),
            }
            self.statusLabel.SetLabel(labels[category])
            self.refreshButton.Enable()
            return
        pending = self._pendingRemovals[category]
        self._actors = [a for a in (actors or []) if a["did"] not in pending]
        self._renderActors(target_index=0)
        self.removeButton.Enable(bool(self._actors))
        self.refreshButton.Enable()
        if self._isActiveTabPage():
            if self._actors:
                self.actorList.SetFocus()
            else:
                self.refreshButton.SetFocus()

    def onRemove(self, evt):
        indices = list(self.actorList.CheckedItems)
        if not self._actors or not indices:
            # Translators: Spoken when trying to undo relationships with nothing checked.
            nvdaUi.message(_("No users checked."))
            return
        staged = [self._actors[i] for i in indices if 0 <= i < len(self._actors)]
        if not staged:
            return
        category = self._currentCategory()
        for actor in staged:
            self._pendingRemovals[category][actor["did"]] = actor
        stagedDids = {a["did"] for a in staged}
        self._actors = [a for a in self._actors if a["did"] not in stagedDids]
        self._renderActors()
        if self._actors:
            self.actorList.SetFocus()
        else:
            self.refreshButton.SetFocus()
        doneTexts = {
            # Translators: Announced after staging users to unmute. {} is the count.
            "muted": _("{} user(s) will be unmuted when you press OK or Apply.").format(len(staged)),
            # Translators: Announced after staging users to unblock. {} is the count.
            "blocked": _("{} user(s) will be unblocked when you press OK or Apply.").format(len(staged)),
        }
        nvdaUi.message(doneTexts[category])

    def _actorTask(self, category, actor):
        def run(atprotoClient):
            if category == "muted":
                client.unmute_actor(atprotoClient, actor["did"])
            else:
                blockingUri = actor.get("blocking_uri")
                if not blockingUri:
                    raise RuntimeError("no block record uri cached for this user")
                client.unblock_actor(atprotoClient, blockingUri)

        def commit():
            self._pendingRemovals[category].pop(actor["did"], None)
            if category == "muted":
                db.set_author_muted(actor["did"], False)
            else:
                db.set_author_blocking(actor["did"], None)

        return _Task(f'@{actor["handle"]}', run, commit)

    def pendingTasks(self):
        tasks = []
        for category in ACTOR_LIST_CATEGORY_KEYS:
            for actor in list(self._pendingRemovals[category].values()):
                tasks.append(self._actorTask(category, actor))
        return tasks


CONTENT_LABEL_DISPLAY_NAMES = {
    # Translators: Content label display name.
    "porn": _("Pornography"),
    # Translators: Content label display name.
    "sexual": _("Sexually suggestive"),
    # Translators: Content label display name.
    "nudity": _("Nudity"),
    # Translators: Content label display name.
    "graphic-media": _("Graphic media"),
}

OPTIONAL_TAB_CHOICES = [
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("notifications", _("Notifications")),
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("explore", _("Explore")),
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("saved", _("Saved")),
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("likes", _("Likes")),
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("chat", _("Chat")),
    # Translators: Tab name in the Settings > Display tabs checklist.
    ("lists", _("Lists")),
]

CONTENT_LABEL_VISIBILITY_CHOICES = [
    # Translators: Content label visibility choice -- shown normally.
    ("show", _("Show")),
    # Translators: Content label visibility choice -- text/embed hidden behind a warning until Ctrl+Space.
    ("warn", _("Warn")),
    # Translators: Content label visibility choice -- post removed from feeds entirely.
    ("hide", _("Hide")),
]

NOTIFICATION_CATEGORY_DISPLAY_NAMES = {
    # Translators: Notification category display name.
    "follow": _("New followers"),
    # Translators: Notification category display name.
    "like": _("Likes"),
    # Translators: Notification category display name.
    "like_via_repost": _("Likes on your reposts"),
    # Translators: Notification category display name.
    "mention": _("Mentions"),
    # Translators: Notification category display name.
    "quote": _("Quotes"),
    # Translators: Notification category display name.
    "reply": _("Replies"),
    # Translators: Notification category display name.
    "repost": _("Reposts"),
    # Translators: Notification category display name.
    "repost_via_repost": _("Reposts of your reposts"),
    # Translators: Notification category display name.
    "starterpack_joined": _("Someone joins your starter pack"),
    # Translators: Notification category display name -- "subscribe" is a per-account bell icon on a profile, separate from following.
    "subscribed_post": _("New posts from accounts you've bell-subscribed to"),
    # Translators: Notification category display name -- Bluesky's identity-verification checkmark being removed from your account.
    "unverified": _("Your account's verification is removed"),
    # Translators: Notification category display name -- Bluesky's identity-verification checkmark being granted to your account.
    "verified": _("Your account gets verified"),
}

NOTIFICATION_STATE_DISPLAY_NAMES = {
    # Translators: Notification state -- this category is disabled entirely.
    "off": _("Off"),
    # Translators: Notification state -- notified for this category from anyone.
    "everyone": _("Everyone"),
    # Translators: Notification state -- notified for this category only from accounts you follow.
    "following": _("People you follow"),
    # Translators: Notification state -- this category is enabled (no filtering available).
    "on": _("On"),
}


class NVSkySettingsDialog(wx.Dialog):
    """
    Standalone NVSky settings window with OK / Cancel / Apply. Cancel and
    Escape discard everything not yet applied. Hosting our own single-level
    wx.Notebook in a plain wx.Dialog (same pattern as MainWindow) avoids the
    double-announcement of nesting inside NVDA's own Settings notebook.
    """

    def __init__(self, parent, on_account_changed=None):
        super().__init__(
            # Translators: Title of the NVSky Settings dialog.
            parent, title=_("NVSky Settings"), size=(700, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._onAccountChangedExternal = on_account_changed
        self._applying = False

        # True until every page has been added (AddPage on an empty notebook
        # auto-selects the first page and fires a real PAGE_CHANGED).
        self._activationSuppressed = True

        panel = wx.Panel(self)
        frameSizer = wx.BoxSizer(wx.VERTICAL)
        frameSizer.Add(panel, proportion=1, flag=wx.EXPAND)
        self.SetSizer(frameSizer)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(panel)
        sizer.Add(self.notebook, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to save settings and close the NVSky Settings dialog.
        self.okBtn = wx.Button(panel, wx.ID_OK, label=_("OK"))
        # Translators: Button to close the NVSky Settings dialog without saving.
        self.cancelBtn = wx.Button(panel, wx.ID_CANCEL, label=_("Cancel"))
        # Translators: Button to save settings without closing the NVSky Settings dialog.
        self.applyBtn = wx.Button(panel, wx.ID_APPLY, label=_("Appl&y"))
        self.okBtn.SetDefault()
        buttonRow.Add(self.okBtn, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.cancelBtn, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.applyBtn)
        sizer.Add(buttonRow, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)

        panel.SetSizer(sizer)

        self.accountsPanel = AccountsPanel(self.notebook, onAccountChanged=self._onAccountChanged)
        self.generalPanel = GeneralPanel(self.notebook)
        self.displayPanel = DisplayPanel(self.notebook)
        self.feedManagerPanel = FeedManagerPanel(self.notebook)
        self.soundPanel = SoundPanel(self.notebook)
        self.profilePanel = ProfilePanel(self.notebook)
        self.mutedWordsPanel = MutedWordsPanel(self.notebook)
        self.mutedUsersPanel = MutedBlockedActorsPanel(self.notebook, "muted")
        self.blockedUsersPanel = MutedBlockedActorsPanel(self.notebook, "blocked")

        # Translators: Settings dialog tab name (accounts management).
        self.notebook.AddPage(self.accountsPanel, _("Accounts"))
        # Translators: Settings dialog tab name (general options).
        self.notebook.AddPage(self.generalPanel, _("General"))
        # Translators: Settings dialog tab name (display/formatting options, tabs, content-label visibility, notifications).
        self.notebook.AddPage(self.displayPanel, _("Display"))
        # Translators: Settings dialog tab name (subscribed feeds management).
        self.notebook.AddPage(self.feedManagerPanel, _("Feed manager"))
        # Translators: Settings dialog tab name (sound options).
        self.notebook.AddPage(self.soundPanel, _("Sound"))
        # Translators: Settings dialog tab name (profile editing).
        self.notebook.AddPage(self.profilePanel, _("Profile"))
        # Translators: Settings dialog tab name (muted words/tags management).
        self.notebook.AddPage(self.mutedWordsPanel, _("Muted words"))
        # Translators: Settings dialog tab name (muted user accounts management).
        self.notebook.AddPage(self.mutedUsersPanel, _("Muted users"))
        # Translators: Settings dialog tab name (blocked user accounts management).
        self.notebook.AddPage(self.blockedUsersPanel, _("Blocked users"))

        self.okBtn.Bind(wx.EVT_BUTTON, self.onOk)
        self.cancelBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.applyBtn.Bind(wx.EVT_BUTTON, self.onApply)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.onPageChanging)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onPageChanged)
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.CentreOnScreen()
        self._activationSuppressed = False
        # Page 0 was auto-selected while suppressed; focus it once deliberately.
        wx.CallAfter(self._activatePage, 0)

    def _pages(self):
        return [self.notebook.GetPage(i) for i in range(self.notebook.GetPageCount())]

    def _hasPendingServerChanges(self):
        return any(
            callable(getattr(page, "pendingTasks", None)) and page.pendingTasks() for page in self._pages()
        )

    def _onAccountChanged(self):
        # Pending server-side edits belong to the previous account: the panels drop them on reload.
        if self._hasPendingServerChanges():
            # Translators: Announced when switching accounts discards unsaved server-side settings changes.
            nvdaUi.message(_("Unsaved changes for the previous account were discarded."))
        self.profilePanel.reload()
        self.mutedWordsPanel.reload()
        self.feedManagerPanel.reload()
        self.mutedUsersPanel.reload()
        self.blockedUsersPanel.reload()
        self.displayPanel.reloadContentLabels()
        self.displayPanel.reloadNotificationPrefs()
        if self._onAccountChangedExternal:
            self._onAccountChangedExternal()

    def onPageChanging(self, evt):
        # Move real focus to the notebook before the page swaps (see MainWindow.onPageChanging).
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
        # Announce the tab name first, then grant real focus (the order that avoids losing the race).
        pageText = self.notebook.GetPageText(index)
        # Translators: Announced when switching to a Settings dialog tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(pageText))
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

    # ---------------- OK / Apply ----------------

    def onOk(self, evt):
        self._startApply(closeAfter=True)

    def onApply(self, evt):
        self._startApply(closeAfter=False)

    def _startApply(self, closeAfter):
        if self._applying:
            return
        self._applying = True
        self.okBtn.Disable()
        self.applyBtn.Disable()

        tasks = []
        for page in self._pages():
            applyFn = getattr(page, "apply", None)
            if callable(applyFn):
                applyFn()
            tasksFn = getattr(page, "pendingTasks", None)
            if callable(tasksFn):
                tasks.extend(tasksFn())

        if not tasks:
            self._finishApply(closeAfter, [], [])
            return

        def worker():
            done = []
            failures = []
            try:
                atprotoClient = client.get_client_for_active_account()
            except Exception as e:
                wx.CallAfter(self._finishApply, closeAfter, [], [(t, str(e)) for t in tasks])
                return
            for task in tasks:
                try:
                    task.run(atprotoClient)
                    done.append(task)
                except Exception as e:
                    log.error(f"NVSky: saving {task.label} failed: {e}")
                    failures.append((task, str(e)))
            wx.CallAfter(self._finishApply, closeAfter, done, failures)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _finishApply(self, closeAfter, done, failures):
        self._applying = False
        self.okBtn.Enable()
        self.applyBtn.Enable()
        for task in done:
            task.commit()
        if failures:
            soundpack.play("error")
            details = "; ".join(f"{task.label}: {error}" for task, error in failures)
            # Translators: Announced when saving some settings to the server fails. {} lists what failed and why.
            nvdaUi.message(_("Could not save: {}").format(details))
            return
        if done:
            soundpack.play("ready")
        if closeAfter:
            self.Close()
        else:
            # Translators: Announced after the Apply button saves the settings.
            nvdaUi.message(_("Settings applied."))

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        needsRebuild = self.displayPanel.tabsChanged
        self.Destroy()
        if needsRebuild:
            from . import rebuild_main_window_tabs
            wx.CallAfter(rebuild_main_window_tabs, True)
