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
        # Translators: Button to remove the selected Bluesky account.
        self.removeButton = wx.Button(self, label=_("&Remove account"))
        # Translators: Button to make the selected account the active one.
        self.setActiveButton = wx.Button(self, label=_("&Set as active"))
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
        # Single deliberate real-focus grab, once, matching MainWindow's
        # own tab-activation pattern -- replaces relying on native
        # Tab-key traversal into accountList (the old double-announce
        # trigger when this lived inside NVDA's nested Settings notebook).
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
    # Translators: Background sync category label (Chat).
    ("chat", _("Chat")),
    # Translators: Background sync category label (Notifications).
    ("notifications", _("Notifications")),
    # Translators: Background sync category label (Saved posts).
    ("saved", _("Saved")),
    # Translators: Background sync category label (Lists).
    ("lists", _("Lists")),
    # Translators: Background sync category label (search results / feed previews).
    ("search", _("Search / feed previews")),
    # Translators: Background sync category label (profile, followers, and post-related people lists).
    ("profile", _("Profile / people & post lists")),
    # Translators: Background sync category label (thread views).
    ("thread", _("Thread")),
]

# Human-readable label per soundpack.EVENT_KEYS entry -- keys not
# listed here fall back to the raw key string (shouldn't normally
# happen, just a safety net if EVENT_KEYS gains an entry before this
# dict is updated to match).
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
}


class GeneralPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        note = wx.StaticText(
            self,
            # Translators: Explanatory note at the top of Settings > General.
            label=_("Check intervals are saved but not applied automatically yet -- "
                    "\"Check for updates\" in the feed window is still manual for now."),
        )
        sizer.Add(note, flag=wx.ALL, border=10)

        # Translators: Label for the Enter-key-action dropdown in Settings > General.
        enterLabel = wx.StaticText(self, label=_("Enter &key action on a post:"))
        sizer.Add(enterLabel, flag=wx.LEFT | wx.TOP, border=10)

        self.enterActionChoice = wx.Choice(self, choices=[label for _, label in ENTER_ACTION_CHOICES])
        currentEnterAction = db.get_ui_state("enter_action") or "view_thread"
        selectedIndex = next(
            (i for i, (key, _) in enumerate(ENTER_ACTION_CHOICES) if key == currentEnterAction), 0
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
            # Translators: Background sync category label (Chat).
            ("chat", _("&Chat")),
            # Translators: Background sync category label (Notifications).
            ("notifications", _("&Notifications")),
            # Translators: Background sync category label (Saved posts).
            ("saved", _("&Saved")),
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

        # Operates on the active account -- lives here rather than the
        # Accounts tab since it's a maintenance action, not account
        # management.
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

        self.enterActionChoice.Bind(wx.EVT_CHOICE, self.onChanged)
        self.clearCacheButton.Bind(wx.EVT_BUTTON, self.onClearCache)
        for spin in self._bgSyncSpins.values():
            spin.Bind(wx.EVT_SPINCTRL, self.onChanged)
        self.bgSyncAnnounceList.Bind(wx.EVT_CHECKLISTBOX, self.onChanged)

    def onTabActivated(self):
        self.enterActionChoice.SetFocus()

    def onChanged(self, evt):
        db.set_ui_state("enter_action", ENTER_ACTION_CHOICES[self.enterActionChoice.GetSelection()][0])
        for category, spin in self._bgSyncSpins.items():
            db.set_bg_sync_interval(category, spin.GetValue())
        checkedCategories = {BG_SYNC_CATEGORY_LABELS[i][0] for i in self.bgSyncAnnounceList.CheckedItems}
        db.set_bg_sync_announce_categories(checkedCategories)
        evt.Skip()

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

        from . import rebuild_main_window_tabs
        rebuild_main_window_tabs()

        # If MainWindow is open, rebuild every tab from scratch so it
        # reflects the now-empty cache immediately instead of showing
        # stale in-memory data until the next manual switch/restart --
        # same rebuild path used when the active account changes.
        from . import rebuild_main_window_tabs
        rebuild_main_window_tabs()


class DisplayPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the "show author as display name or handle" radio group.
        authorLabel = wx.StaticText(self, label=_("Show &author as:"))
        sizer.Add(authorLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.authorModeRadio = wx.RadioBox(
            # Translators: Radio option: show the author's display name.
            # Translators: Radio option: show the author's @handle instead of display name.
            self, choices=[_("Display name"), _("Handle")], majorDimension=1, style=wx.RA_SPECIFY_ROWS
        )
        authorMode = db.get_ui_state("column1_display") or "display_name"
        self.authorModeRadio.SetSelection(0 if authorMode == "display_name" else 1)
        sizer.Add(self.authorModeRadio, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        # Translators: Label for the post-time-format dropdown.
        timeLabel = wx.StaticText(self, label=_("Post &time format:"))
        sizer.Add(timeLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.timeModeChoice = wx.Choice(self, choices=[label for label, _key in TIME_MODE_CHOICES])
        currentMode = db.get_ui_state("time_format_mode") or "relative_24h"
        modeValues = [value for _label, value in TIME_MODE_CHOICES]
        self.timeModeChoice.SetSelection(modeValues.index(currentMode) if currentMode in modeValues else 0)
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
        formatHelpButton = wx.Button(self, label=_("&strftime format reference..."))
        formatHelpButton.Bind(wx.EVT_BUTTON, self.onFormatHelp)
        sizer.Add(formatHelpButton, flag=wx.LEFT | wx.TOP, border=10)

        # Translators: Label for the feed sort-order radio group.
        sortLabel = wx.StaticText(self, label=_("Feed &order:"))
        sizer.Add(sortLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.sortOrderRadio = wx.RadioBox(
            # Translators: Radio option: show newest posts first.
            # Translators: Radio option: show oldest posts first.
            self, choices=[_("Newest first"), _("Oldest first")], majorDimension=1, style=wx.RA_SPECIFY_ROWS
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
        # Translators: Status text showing how many feeds are subscribed. {} is the count.
        self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)) if hasCached else _("Loading your feeds..."))
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
        self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)) if hasCached else _("Loading your feeds..."))
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
        self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)))
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
        # Translators: Announced when un/pinning a feed fails. {} is the error message.
        nvdaUi.message(_("Could not change pin: {}").format(error))

    def onRemove(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        feed = self._feeds[index]
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for removing a subscribed feed. {} is the feed's display name.
            _('Remove "{}" from your feeds?').format(feed["display_name"]),
            # Translators: Title of the confirm-remove-feed dialog.
            _("Confirm remove"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        # Optimistic: gone from the list (and cache) immediately, the
        # server call happens in the background.
        self._feeds = [f for f in self._feeds if f["uri"] != feed["uri"]]
        self._persistCache()
        self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)))
        # Translators: Announced after removing a feed. {} is the feed's display name.
        nvdaUi.message(_('Removed "{}".').format(feed["display_name"]))
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
        self.statusLabel.SetLabel(_("{} subscribed feed(s).").format(len(self._feeds)))
        self._render(target_index=insertAt)
        self._notifyHomeFeedsChanged()
        # Translators: Announced when removing a feed fails. {} is the error message.
        nvdaUi.message(_("Could not remove feed: {}").format(error))


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

        self.packChoice.Bind(wx.EVT_CHOICE, self.onChanged)
        self.eventList.Bind(wx.EVT_CHECKLISTBOX, self.onChanged)

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

    def onChanged(self, evt):
        index = self.packChoice.GetSelection()
        packName = self._packNames[index] if 0 <= index < len(self._packNames) else soundpack.SILENT_PACK
        db.set_soundpack_selected(packName)

        checkedKeys = {soundpack.EVENT_KEYS[i] for i in self.eventList.CheckedItems}
        disabledKeys = set(soundpack.EVENT_KEYS) - checkedKeys
        db.set_soundpack_disabled_events(disabledKeys)

        soundpack.reload()
        evt.Skip()


class ProfilePanel(wx.Panel):
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

        # Translators: Button to save the display name and bio.
        self.saveTextButton = wx.Button(self, label=_("&Save display name && bio"))
        sizer.Add(self.saveTextButton, flag=wx.LEFT | wx.TOP, border=10)

        avatarRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to change the profile avatar image.
        self.changeAvatarButton = wx.Button(self, label=_("Change &avatar..."))
        # Translators: Button to change the profile banner image.
        self.changeBannerButton = wx.Button(self, label=_("Change &banner..."))
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

        # Lazy -- only fetches once this tab is actually opened, not at
        # dialog-construction time (every panel gets constructed eagerly
        # by NVSkySettingsDialog.__init__ regardless of which tab is
        # active) -- otherwise the progress sound/network call fires the
        # instant Settings opens, before the user ever tabs here.
        self._loadedOnce = False

    def onTabActivated(self):
        if not self._loadedOnce:
            self._loadedOnce = True
            self._loadProfile()
        # nameCtrl starts Disabled until profile data arrives (see
        # _loadProfile/_onProfileLoaded) -- SetFocus() on a disabled
        # wx.TextCtrl is a silent no-op on Windows, so this only does
        # anything once loading has actually finished; _onProfileLoaded
        # below covers the "still loading when tab was entered" case.
        elif self.nameCtrl.IsEnabled():
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
        self._loadedOnce = True
        self.nameCtrl.Disable()
        self.bioCtrl.Disable()
        self.saveTextButton.Disable()
        self.changeAvatarButton.Disable()
        self.changeBannerButton.Disable()
        self.statusLabel.SetLabel("Loading profile...")
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
        self.nameCtrl.SetValue(profile.get("display_name") or "")
        self.bioCtrl.SetValue(profile.get("description") or "")
        # Translators: Status text showing which account's profile is being edited. {} is the handle.
        self.statusLabel.SetLabel(_("Editing @{}").format(profile["handle"]))
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
        soundpack.start_progress()

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
        soundpack.stop_progress()
        self.saveTextButton.Enable()
        if error:
            # Translators: Status text when saving the profile fails. {} is the error message.
            self.statusLabel.SetLabel(_("Failed to save: {}").format(error))
            # Translators: Spoken announcement when saving the profile fails. {} is the error message.
            nvdaUi.message(_("Failed to save profile: {}").format(error))
            return
        # Translators: Status text after successfully saving the profile.
        self.statusLabel.SetLabel(_("Profile saved."))
        nvdaUi.message(_("Profile saved."))

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

        button = self.changeAvatarButton if kind == "avatar" else self.changeBannerButton
        button.Disable()
        # Translators: Status text while uploading the avatar or banner image. {} is "avatar" or "banner".
        self.statusLabel.SetLabel(_("Uploading {}...").format(kind))
        soundpack.start_progress()

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
        soundpack.stop_progress()
        button = self.changeAvatarButton if kind == "avatar" else self.changeBannerButton
        button.Enable()
        # Translators: The word "Avatar", used in status/spoken messages about the profile image.
        # Translators: The word "Banner", used in status/spoken messages about the profile banner.
        kindLabel = _("Avatar") if kind == "avatar" else _("Banner")
        if error:
            # Translators: Status text when updating the avatar/banner fails. First {} is "Avatar"/"Banner", second {} is the error message.
            self.statusLabel.SetLabel(_("Failed to update {}: {}").format(kindLabel, error))
            nvdaUi.message(_("Failed to update {}: {}").format(kindLabel, error))
            return
        # Translators: Status text after successfully updating the avatar/banner. {} is "Avatar"/"Banner".
        self.statusLabel.SetLabel(_("{} updated.").format(kindLabel))
        nvdaUi.message(_("{} updated.").format(kindLabel))



class MutedWordsPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._words = []

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

        # Lazy -- same reasoning as ProfilePanel: don't fetch (and
        # don't play the progress sound) until this tab is actually
        # opened, not at Settings-dialog-construction time.
        self._loadedOnce = False

    def reload(self):
        self._loadedOnce = True
        self.addButton.Disable()
        self.removeButton.Disable()
        self.statusLabel.SetLabel("Loading muted words...")
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
        # Translators: Announced when adding a muted word fails. First {} is the word/tag, second {} is the error message.
        nvdaUi.message(_('Could not add "{}": {}').format(value, error))

    def onRemove(self, evt):
        index = self.wordList.GetFocusedItem()
        if index == -1 or index >= len(self._words):
            nvdaUi.message(_("No word selected."))
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
        # Translators: Announced after removing a muted word. {} is the word/tag.
        nvdaUi.message(_('Removed "{}".').format(value))

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
        # Translators: Announced when removing a muted word fails. First {} is the word/tag, second {} is the error message.
        nvdaUi.message(_('Could not remove "{}": {}').format(removed["value"], error))


class MutedBlockedActorsPanel(wx.Panel):
    """
    Settings > Muted users / Blocked users -- checklist-based bulk
    unmute/unblock (CustomCheckListBox, same pattern as
    ManageGroupMembersDialog/SubscribeListDialog's checklists) so
    several accounts can be un-muted/un-blocked in one action. Always
    re-fetched fresh on every tab activation, no local cache -- not
    checked often enough to need one.

    A CustomCheckListBox constructed/Set() with zero items still
    renders one blank-looking checkable row on Windows AND raises a
    real wxAssertionError ("bad wxCheckListBox index") the moment NVDA
    tries to read that row's state -- confirmed via a real crash log.
    Same fix already used by ManageGroupMembersDialog/SubscribeListDialog
    for the identical issue: hide the checklist entirely whenever there
    are zero items (loading or genuinely empty, doesn't matter which),
    and fall back focus to the Refresh button instead.
    """

    def __init__(self, parent, kind: str):
        super().__init__(parent)
        self._kind = kind  # "muted" or "blocked"
        self._actors = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Initial status text while the muted/blocked user list loads.
        self.statusLabel = wx.StaticText(self, label=_("Loading, please wait..."))
        sizer.Add(self.statusLabel, flag=wx.ALL, border=10)

        # Translators: Label above the muted/blocked users checklist.
        self.listLabel = wx.StaticText(self, label=_("&Users (check to select several):"))
        sizer.Add(self.listLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.actorList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.actorList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.listLabel.Hide()
        self.actorList.Hide()

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        if kind == "muted":
            # Translators: Button to unmute every checked user.
            removeLabel = _("Un&mute checked")
        else:
            # Translators: Button to unblock every checked user.
            removeLabel = _("Unbloc&k checked")
        self.removeButton = wx.Button(self, label=removeLabel)
        # Translators: Button to refresh the muted/blocked user list.
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

    def onTabActivated(self):
        if self._actors:
            self.actorList.SetFocus()
        else:
            self.refreshButton.SetFocus()
        self._load()

    def reload(self):
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

    def _renderActors(self, target_index=None):
        hasActors = bool(self._actors)
        self.listLabel.Show(hasActors)
        self.actorList.Show(hasActors)
        self.removeButton.Show(hasActors)

        if self._kind == "muted":
            # Translators: Status text showing how many users are muted. {} is the count.
            self.statusLabel.SetLabel(_("{} muted user(s).").format(len(self._actors)))
        else:
            # Translators: Status text showing how many users are blocked. {} is the count.
            self.statusLabel.SetLabel(_("{} blocked user(s).").format(len(self._actors)))

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
        # Translators: Status text while the muted/blocked user list loads.
        self.statusLabel.SetLabel(_("Loading, please wait..."))
        self.removeButton.Disable()
        self.refreshButton.Disable()
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if self._kind == "muted":
                    actors = client.get_muted_actors(atprotoClient)
                else:
                    actors = client.get_blocked_actors(atprotoClient)
                error = None
            except Exception as e:
                actors = None
                error = str(e)
            wx.CallAfter(self._onLoaded, actors, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoaded(self, actors, error):
        soundpack.stop_progress()
        if error:
            if self._kind == "muted":
                # Translators: Status text when loading muted users fails. {} is the error message.
                self.statusLabel.SetLabel(_("Could not load muted users: {}").format(error))
            else:
                # Translators: Status text when loading blocked users fails. {} is the error message.
                self.statusLabel.SetLabel(_("Could not load blocked users: {}").format(error))
            self.refreshButton.Enable()
            return
        self._actors = actors or []
        self._renderActors(target_index=0)
        self.removeButton.Enable()
        self.refreshButton.Enable()
        if self._actors:
            if self._kind == "muted":
                # Translators: Announced after the muted users list finishes loading. {} is the count.
                nvdaUi.message(_("{} muted user(s) loaded.").format(len(self._actors)))
            else:
                # Translators: Announced after the blocked users list finishes loading. {} is the count.
                nvdaUi.message(_("{} blocked user(s) loaded.").format(len(self._actors)))
        else:
            if self._kind == "muted":
                # Translators: Announced when the muted users list loads with nothing in it.
                nvdaUi.message(_("No muted users."))
            else:
                # Translators: Announced when the blocked users list loads with nothing in it.
                nvdaUi.message(_("No blocked users."))
        if self._isActiveTabPage():
            if self._actors:
                self.actorList.SetFocus()
            else:
                self.refreshButton.SetFocus()

    def onRemove(self, evt):
        if not self._actors:
            # Translators: Spoken when trying to unmute/unblock with nothing in the list.
            nvdaUi.message(_("No users checked."))
            return
        indices = list(self.actorList.CheckedItems)
        if not indices:
            # Translators: Spoken when trying to unmute/unblock with nothing checked.
            nvdaUi.message(_("No users checked."))
            return
        toRemove = [self._actors[i] for i in indices if 0 <= i < len(self._actors)]
        if not toRemove:
            return

        self.removeButton.Disable()
        if self._kind == "muted":
            # Translators: Announced while unmuting checked users. {} is the count.
            nvdaUi.message(_("Unmuting {} user(s)...").format(len(toRemove)))
        else:
            # Translators: Announced while unblocking checked users. {} is the count.
            nvdaUi.message(_("Unblocking {} user(s)...").format(len(toRemove)))

        def worker():
            removed = []
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for actor in toRemove:
                try:
                    if self._kind == "muted":
                        client.unmute_actor(atprotoClient, actor["did"])
                    else:
                        blockingUri = actor.get("blocking_uri")
                        if not blockingUri:
                            raise RuntimeError("no block record uri cached for this user")
                        client.unblock_actor(atprotoClient, blockingUri)
                    removed.append(actor)
                except Exception as e:
                    errors.append(f'@{actor["handle"]}: {e}')
            wx.CallAfter(self._onRemoveDone, removed, errors)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, removed, errors):
        self.removeButton.Enable()
        removedDids = {a["did"] for a in removed}
        self._actors = [a for a in self._actors if a["did"] not in removedDids]
        self._renderActors()
        if self._actors:
            self.actorList.SetFocus()
        else:
            self.refreshButton.SetFocus()
        if removed:
            names = ", ".join(f'@{a["handle"]}' for a in removed)
            if self._kind == "muted":
                # Translators: Announced after unmuting checked users. {} is a comma-separated list of handles.
                nvdaUi.message(_("Unmuted: {}.").format(names))
            else:
                # Translators: Announced after unblocking checked users. {} is a comma-separated list of handles.
                nvdaUi.message(_("Unblocked: {}.").format(names))
        if errors:
            # Translators: Announced when some unmute/unblock actions fail. {} is a semicolon-separated list of "handle: error" entries.
            nvdaUi.message(_("Some actions failed: {}").format("; ".join(errors)))


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
            # Translators: Title of the NVSky Settings dialog.
            parent, title=_("NVSky Settings"), size=(700, 520),
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

        # Translators: Button to close the NVSky Settings dialog.
        closeBtn = wx.Button(panel, wx.ID_CLOSE, label=_("&Close"))
        sizer.Add(closeBtn, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)

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
        # Translators: Settings dialog tab name (display/formatting options).
        self.notebook.AddPage(self.displayPanel, _("Display"))
        # Translators: Settings dialog tab name (subscribed feeds management).
        self.notebook.AddPage(self.feedManagerPanel, _("Feed manager"))
        # Translators: Settings dialog tab name (sound options, not yet implemented).
        self.notebook.AddPage(self.soundPanel, _("Sound"))
        # Translators: Settings dialog tab name (profile editing).
        self.notebook.AddPage(self.profilePanel, _("Profile"))
        # Translators: Settings dialog tab name (muted words/tags management).
        self.notebook.AddPage(self.mutedWordsPanel, _("Muted words"))
        # Translators: Settings dialog tab name (muted user accounts management).
        self.notebook.AddPage(self.mutedUsersPanel, _("Muted users"))
        # Translators: Settings dialog tab name (blocked user accounts management).
        self.notebook.AddPage(self.blockedUsersPanel, _("Blocked users"))

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
        self.mutedUsersPanel.reload()
        self.blockedUsersPanel.reload()
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

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()
