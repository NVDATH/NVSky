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

from gui.settingsDialogs import SettingsPanel
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

    @uiutil.safe_ui_callback
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

        homeLabel = wx.StaticText(self, label="Main timeline check interval (minutes):")
        sizer.Add(homeLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.homeIntervalSpin = wx.SpinCtrl(
            self, min=1, max=60, initial=int(db.get_ui_state("fetch_interval_home_minutes") or 5),
            name="Main timeline check interval (minutes)",
        )
        sizer.Add(self.homeIntervalSpin, flag=wx.LEFT | wx.TOP, border=10)

        dmLabel = wx.StaticText(self, label="Direct message check interval (minutes):")
        sizer.Add(dmLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.dmIntervalSpin = wx.SpinCtrl(
            self, min=1, max=60, initial=int(db.get_ui_state("fetch_interval_dm_minutes") or 2),
            name="Direct message check interval (minutes)",
        )
        sizer.Add(self.dmIntervalSpin, flag=wx.LEFT | wx.TOP, border=10)
        enterLabel = wx.StaticText(self, label="Enter key action on a post:")
        sizer.Add(enterLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.enterActionChoice = wx.Choice(self, choices=[label for _, label in ENTER_ACTION_CHOICES])
        currentEnterAction = db.get_ui_state("enter_action") or "view_thread"
        selectedIndex = next(
            (i for i, (key, _) in enumerate(ENTER_ACTION_CHOICES) if key == currentEnterAction), 0
        )
        self.enterActionChoice.SetSelection(selectedIndex)
        sizer.Add(self.enterActionChoice, flag=wx.LEFT | wx.TOP, border=10)

        # Operates on the active account -- lives here rather than the
        # Accounts tab since it's a maintenance action, not account
        # management.
        cacheLabel = wx.StaticText(self, label="Cached posts (active account):")
        sizer.Add(cacheLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.clearCacheButton = wx.Button(self, label="Clear cache")
        sizer.Add(self.clearCacheButton, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

        self.SetSizer(sizer)

        self.homeIntervalSpin.Bind(wx.EVT_SPINCTRL, self.onChanged)
        self.dmIntervalSpin.Bind(wx.EVT_SPINCTRL, self.onChanged)
        self.enterActionChoice.Bind(wx.EVT_CHOICE, self.onChanged)
        self.clearCacheButton.Bind(wx.EVT_BUTTON, self.onClearCache)

    def onChanged(self, evt):
        db.set_ui_state("fetch_interval_home_minutes", str(self.homeIntervalSpin.GetValue()))
        db.set_ui_state("fetch_interval_dm_minutes", str(self.dmIntervalSpin.GetValue()))
        db.set_ui_state("enter_action", ENTER_ACTION_CHOICES[self.enterActionChoice.GetSelection()][0])
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

    @uiutil.safe_ui_callback
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
        self.addButton = wx.Button(self, label="Add")
        self.removeButton = wx.Button(self, label="Remove selected")
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

    @uiutil.safe_ui_callback
    def _onLoaded(self, words, error):
        if error:
            self.statusLabel.SetLabel(f"Could not load muted words: {error}")
            return
        self._words = words or []
        self.wordList.DeleteAllItems()
        for i, w in enumerate(self._words):
            self.wordList.InsertItem(i, w["value"])
        self.statusLabel.SetLabel(f"{len(self._words)} muted word(s)/tag(s).")
        self.addButton.Enable()
        self.removeButton.Enable()

    def onAdd(self, evt):
        dlg = wx.TextEntryDialog(self, "Word or tag to mute:", "Add muted word")
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        value = dlg.GetValue().strip()
        dlg.Destroy()
        if not value:
            return

        self.addButton.Disable()
        self.statusLabel.SetLabel(f'Adding "{value}"...')

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
        self.addButton.Enable()
        if error:
            self.statusLabel.SetLabel(f"Failed to add: {error}")
            nvdaUi.message(f"Failed to add muted word: {error}")
            return
        self.statusLabel.SetLabel(f'Added "{value}".')
        nvdaUi.message(f'Muted "{value}".')
        self._load()

    def onRemove(self, evt):
        index = self.wordList.GetFirstSelected()
        if index == -1:
            nvdaUi.message("No word selected.")
            return
        value = self._words[index]["value"]
        self.removeButton.Disable()
        self.statusLabel.SetLabel(f'Removing "{value}"...')

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_muted_word(atprotoClient, value)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, value, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, value, error):
        self.removeButton.Enable()
        if error:
            self.statusLabel.SetLabel(f"Failed to remove: {error}")
            nvdaUi.message(f"Failed to remove muted word: {error}")
            return
        self.statusLabel.SetLabel(f'Removed "{value}".')
        nvdaUi.message(f'Removed "{value}" from muted words.')
        self._load()

class NVSkySettingsPanel(SettingsPanel):
    title = "NVSky"

    def makeSettings(self, settingsSizer):
        notebook = wx.Notebook(self)
        profilePanel = ProfilePanel(notebook)
        mutedWordsPanel = MutedWordsPanel(notebook)

        def onAccountChanged():
            profilePanel.reload()
            mutedWordsPanel.reload()

        notebook.AddPage(AccountsPanel(notebook, onAccountChanged=onAccountChanged), "Accounts")
        notebook.AddPage(GeneralPanel(notebook), "General")
        notebook.AddPage(DisplayPanel(notebook), "Display")
        notebook.AddPage(SoundPanel(notebook), "Sound")
        notebook.AddPage(profilePanel, "Profile")
        notebook.AddPage(mutedWordsPanel, "Muted words")
        settingsSizer.Add(notebook, flag=wx.EXPAND, proportion=1)
        self.Layout()

    def onSave(self):
        pass
