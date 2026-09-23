"""
Lists tab and related dialogs for NVSky.

Split out of feedWindow.py (see plan-17.md). ListsWindow is a flat
tree of every list the account created or subscribed to; ListTabWindow
is a single curation list's timeline popped out into its own removable
tab. The four dialogs here (AddListDialog, SubscribeListDialog,
ManageMembersDialog, AddToListDialog) are all list-management UI used
either from this tab or from UserActionMixin's "Add to list..."/
"View their lists..." actions elsewhere in the add-on.
"""
import threading
import wx

import gui
import gui.nvdaControls
from logHandler import log
import ui as nvdaUi

from . import db
from . import client
from . import uiutil
from . import soundpack
from .feedWindow import (
    RemovableTabMixin,
    FeedListMixin,
    ItemActionMixin,
    UserActionMixin,
    EmbedViewMixin,
    _announce_now,
    _describe_embed,
    _message_text,
    _format_post_time,
    _visible_embed_text,
    _visible_message_text,
)


class AddListDialog(wx.Dialog):
    def __init__(self, parent):
        # Translators: Title of the create-list dialog.
        super().__init__(parent, title=_("Add list"), size=(420, 320))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the list-name field.
        nameLabel = wx.StaticText(self, label=_("&Name:"))
        self.nameText = wx.TextCtrl(self)
        sizer.Add(nameLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.nameText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Translators: Label for the optional list-description field.
        descLabel = wx.StaticText(self, label=_("&Description (optional):"))
        self.descText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 80))
        sizer.Add(descLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        sizer.Add(self.descText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.purposeRadio = wx.RadioBox(
            # Translators: Label for the list-type radio group.
            self, label=_("List type"),
            choices=[
                # Translators: List-type radio choice.
                _("For browsing (see everyone's posts together as one timeline)"),
                # Translators: List-type radio choice.
                _("For moderation (mute or block this whole group of accounts)"),
            ],
        )
        sizer.Add(self.purposeRadio, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        btnSizer = wx.StdDialogButtonSizer()
        # Translators: Button to create the new list.
        okBtn = wx.Button(self, wx.ID_OK, label=_("&Create"))
        cancelBtn = wx.Button(self, wx.ID_CANCEL)
        btnSizer.AddButton(okBtn)
        btnSizer.AddButton(cancelBtn)
        btnSizer.Realize()
        sizer.Add(btnSizer, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()
        self.nameText.SetFocus()

    def getValues(self):
        purpose = client.LIST_PURPOSE_CURATE if self.purposeRadio.GetSelection() == 0 else client.LIST_PURPOSE_MOD
        return self.nameText.GetValue().strip(), self.descText.GetValue().strip(), purpose


class SubscribeListDialog(wx.Dialog):
    """
    Find someone else's lists by searching for their handle/name (same
    typeahead search as Manage members below), then either open one of
    their curation lists straight as a tab -- get_list_feed works off
    any public list uri, no subscription needed, this is the "browse
    their content" path -- or subscribe (mute/block) to one of their
    moderation lists, which DOES need a real subscription since that's
    what makes it apply to your own timeline.
    """

    def __init__(self, parent, on_subscribed=None, preselected_user=None):
        # preselected_user: {"did", "handle", "display_name"} -- skips
        # the search step entirely and loads straight to their lists.
        # Used by UserActionMixin.showUserLists ("View their lists..."),
        # which already has did/handle in hand from wherever the user
        # was found (a post, followers list, etc). The plain "Find
        # lists by user..." toolbar button still goes through the
        # normal search flow (preselected_user=None).
        self._userSuggestions = []
        self._selectedUser = preselected_user
        self._userLists = []
        self._onSubscribed = on_subscribed
        self._preselected = preselected_user is not None

        if self._preselected:
            # Translators: Fallback label for a user with no cached handle.
            label = f'@{preselected_user["handle"]}' if preselected_user.get("handle") else _("user")
            # Translators: Title of the subscribe-to-lists dialog when preselected for a specific user. {} is their label.
            title = _("Lists by {}").format(label)
        else:
            # Translators: Title of the find-lists-by-user dialog.
            title = _("Find lists by user")
        super().__init__(parent, title=title, size=(480, 480))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the user search field.
        userLabel = wx.StaticText(self, label=_("&Search for a user by handle or name:"))
        self.userSearchText = wx.TextCtrl(self)
        sizer.Add(userLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.userSearchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.userChoice = wx.Choice(self, choices=[])
        sizer.Add(self.userChoice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # Translators: Button to load a selected user's lists.
        self.loadListsButton = wx.Button(self, label=_("Sho&w their lists"))
        sizer.Add(self.loadListsButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.loadListsButton.Hide()  # nothing to load until a user is picked

        # Translators: Label above a user's lists checklist.
        self.listsLabel = wx.StaticText(self, label=_("Their lists:"))
        self.listsCheckBox = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.listsLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.listsCheckBox, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open checked lists as tabs.
        self.openTabButton = wx.Button(self, label=_("&Open checked as tabs"))
        # Translators: Button to mute checked lists.
        self.subscribeAsMuteButton = wx.Button(self, label=_("Subscribe checked (&mute)"))
        # Translators: Button to block checked lists.
        self.subscribeAsBlockButton = wx.Button(self, label=_("Subscribe checked (&block)"))
        actionRow.Add(self.openTabButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.subscribeAsMuteButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.subscribeAsBlockButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing to open/subscribe until their lists are actually loaded.
        self.listsLabel.Hide()
        self.listsCheckBox.Hide()
        self.openTabButton.Hide()
        self.subscribeAsMuteButton.Hide()
        self.subscribeAsBlockButton.Hide()

        closeBtn = wx.Button(self, label=_("&Close"))
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.userSearchText.Bind(wx.EVT_TEXT, self.onUserSearchChanged)
        self.loadListsButton.Bind(wx.EVT_BUTTON, self.onLoadLists)
        self.openTabButton.Bind(wx.EVT_BUTTON, self.onOpenAsTabs)
        self.subscribeAsMuteButton.Bind(wx.EVT_BUTTON, lambda e: self.onSubscribe("mute"))
        self.subscribeAsBlockButton.Bind(wx.EVT_BUTTON, lambda e: self.onSubscribe("block"))
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        if self._preselected:
            # Search controls never apply here -- hide them and go
            # straight to loading, same end state onLoadLists reaches.
            userLabel.Hide()
            self.userSearchText.Hide()
            self.userChoice.Hide()
            self.Layout()
            closeBtn.SetFocus()
            # Translators: Announced while loading a specific user's lists. {} is their label.
            nvdaUi.message(_("Loading lists for {}, please wait...").format(label))
            self._loadListsForSelectedUser()
        else:
            self.userSearchText.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        parent = self.GetParent()
        self.Destroy()
        # subscribeButton only exists when this dialog was opened from
        # ListsWindow's toolbar -- the preselected-user flow (View their
        # lists...) can be opened from any UserActionMixin host, which
        # has no such button.
        if parent is not None and hasattr(parent, "subscribeButton"):
            parent.subscribeButton.SetFocus()

    def onUserSearchChanged(self, evt):
        wx.CallLater(400, self._runUserSearch, self.userSearchText.GetValue())

    def _runUserSearch(self, query):
        if query != self.userSearchText.GetValue():
            return  # a newer keystroke already superseded this debounce
        if not query.strip():
            self.userChoice.Set([])
            self._userSuggestions = []
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                results = client.search_actors_typeahead(atprotoClient, query)
                error = None
            except Exception as e:
                results = []
                error = str(e)
            wx.CallAfter(self._onUserSearchDone, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserSearchDone(self, results, error):
        if error:
            return
        self._userSuggestions = results
        # Translators: List entry format for a search result with no display name set. {} is the handle.
        self.userChoice.Set([f'@{r["handle"]} ({r.get("display_name") or _("no display name")})' for r in results])
        self.loadListsButton.Show(bool(results))
        self.Layout()
        if results:
            self.userChoice.SetSelection(0)

    def onLoadLists(self, evt):
        index = self.userChoice.GetSelection()
        if not (0 <= index < len(self._userSuggestions)):
            # Translators: Announced when trying to load lists with no user selected from the search results.
            nvdaUi.message(_("Search for a user and pick one from the list first."))
            return
        self._selectedUser = self._userSuggestions[index]
        # Translators: Announced while loading a specific user's lists. {} is their handle.
        nvdaUi.message(_("Loading lists for @{}, please wait...").format(self._selectedUser["handle"]))
        self._loadListsForSelectedUser()

    def _loadListsForSelectedUser(self):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                entries = client.get_lists(atprotoClient, self._selectedUser["did"])
                error = None
            except Exception as e:
                entries = []
                error = str(e)
            wx.CallAfter(self._onUserListsLoaded, entries, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onUserListsLoaded(self, entries, error):
        if error:
            # Translators: Announced when loading a user's lists fails. {} is the error message.
            nvdaUi.message(_("Could not load their lists: {}").format(error))
            return
        # getLists(actor=them) can also include lists THEY subscribed
        # to (not just ones they created) -- only show ones they
        # actually authored, showing back a list they merely subscribe
        # to isn't useful here.
        self._userLists = [e for e in entries if e["creator_did"] == self._selectedUser["did"]]
        hasLists = bool(self._userLists)
        self.listsLabel.Show(hasLists)
        self.listsCheckBox.Show(hasLists)
        self.openTabButton.Show(hasLists)
        self.subscribeAsMuteButton.Show(hasLists)
        self.subscribeAsBlockButton.Show(hasLists)
        self.Layout()
        self.listsCheckBox.Set([self._listChoiceLabel(l) for l in self._userLists])
        self.listsCheckBox.CheckedItems = []
        if hasLists:
            self.listsCheckBox.SetSelection(0)
            self.listsCheckBox.SetFocus()
        if not self._userLists:
            # Translators: Announced when a user has no public lists. {} is their handle.
            nvdaUi.message(_("@{} has no public lists.").format(self._selectedUser["handle"]))
        else:
            # Translators: Announced after a user's lists finish loading. {} is the count.
            nvdaUi.message(_("{} lists loaded.").format(len(self._userLists)))

    def _listChoiceLabel(self, lst):
        # Translators: List-type suffix for a moderation list, e.g. "MyList (moderation list)".
        # Translators: List-type suffix for a curation list, e.g. "MyList (curation list)".
        kind = _("moderation list") if lst["purpose"] == client.LIST_PURPOSE_MOD else _("curation list")
        # Translators: Checklist entry combining a list's name and type. First {} is the name, second {} is the type.
        return _("{} ({})").format(lst["name"], kind)

    def onOpenAsTabs(self, evt):
        if not self.openTabButton.IsShown():
            return
        checked = [self._userLists[i] for i in self.listsCheckBox.CheckedItems if 0 <= i < len(self._userLists)]
        curateLists = [l for l in checked if l["purpose"] == client.LIST_PURPOSE_CURATE]
        if not curateLists:
            # Translators: Announced when trying to open lists as tabs with only moderation lists checked.
            nvdaUi.message(_("Check at least one curation list first -- moderation lists don't have a timeline to open."))
            return
        mainWindow = self.GetParent().GetTopLevelParent()
        account = db.get_active_account()
        for i, lst in enumerate(curateLists):
            tab = ListTabWindow(mainWindow.notebook, lst["uri"], lst["name"], origin_key="lists")
            mainWindow.addTab(tab, lst["name"], select=(i == len(curateLists) - 1), removable=True)
            if account is not None:
                db.add_open_temp_tab(account["id"], {
                    "type": "list", "key": lst["uri"], "list_uri": lst["uri"], "list_name": lst["name"],
                    "origin_key": "lists",
                })
        if len(curateLists) == 1:
            # Translators: Announced after opening exactly one list as a tab.
            nvdaUi.message(_("Opened 1 list as a tab."))
        else:
            # Translators: Announced after opening several lists as tabs. {} is the count.
            nvdaUi.message(_("Opened {} lists as tabs.").format(len(curateLists)))

    def onSubscribe(self, action):
        if not self.subscribeAsMuteButton.IsShown():
            return
        checked = [self._userLists[i] for i in self.listsCheckBox.CheckedItems if 0 <= i < len(self._userLists)]
        modLists = [l for l in checked if l["purpose"] == client.LIST_PURPOSE_MOD]
        if not modLists:
            # Translators: Announced when trying to subscribe to lists with only curation lists checked.
            nvdaUi.message(_("Check at least one moderation list first -- curation lists can't be muted/blocked, use Open checked as tabs instead."))
            return

        def worker():
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for lst in modLists:
                try:
                    if action == "mute":
                        client.mute_actor_list(atprotoClient, lst["uri"])
                    else:
                        client.block_actor_list(atprotoClient, lst["uri"])
                except Exception as e:
                    errors.append(f'{lst["name"]}: {e}')
            wx.CallAfter(self._onSubscribeDone, len(modLists) - len(errors), errors)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onSubscribeDone(self, successCount, errors):
        if successCount:
            if successCount == 1:
                # Translators: Announced after subscribing to exactly one list.
                nvdaUi.message(_("Subscribed to 1 list."))
            else:
                # Translators: Announced after subscribing to several lists. {} is the count.
                nvdaUi.message(_("Subscribed to {} lists.").format(successCount))
            if self._onSubscribed:
                self._onSubscribed()
        if errors:
            # Translators: Announced when some list subscriptions fail. {} is a semicolon-separated list of "name: error" entries.
            nvdaUi.message(_("Some subscriptions failed: {}").format("; ".join(errors)))


class ManageMembersDialog(wx.Dialog):
    """
    Add/remove members for a list. Read-only (no Add/Remove controls)
    if you're not the list's creator -- membership can only be edited
    by the creator per the AT Protocol's own permission model.

    UI mirrors ManageGroupMembersDialog (chatWindow.py): checklist-based
    multi-select add/remove instead of one-at-a-time. Deliberately no
    context menu -- decided unnecessary for a plain member-management
    checklist that already has explicit buttons.
    """

    def __init__(self, parent, account, list_info: dict):
        self._account = account
        self._listInfo = list_info
        self._members = list(list_info.get("members", []))
        self._isOwner = list_info["creator_did"] == account["did"]
        self._suggestions = []

        # Translators: Title of the manage-list-members dialog. {} is the list's name.
        super().__init__(parent, title=_("Manage members - {}").format(list_info['name']), size=(460, 520))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label above the current-members checklist.
        memberLabel = wx.StaticText(self, label=_("Current &members:"))
        sizer.Add(memberLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        if self._isOwner:
            self.memberList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
            sizer.Add(self.memberList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            # A CustomCheckListBox constructed/Set() with zero items
            # still renders one blank-looking checkable row on Windows
            # (confirmed by testing) -- hide the control entirely and
            # show this plain label instead, same fix already used by
            # SubscribeListDialog's listsCheckBox for the identical
            # issue.
            # Translators: Shown in place of the member checklist when a list has no members yet.
            self.emptyMembersLabel = wx.StaticText(self, label=_("No members yet -- add one below."))
            sizer.Add(self.emptyMembersLabel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            self._renderMembers()

            # Translators: Button to remove checked members from a list.
            self.removeButton = wx.Button(self, label=_("&Remove checked"))
            sizer.Add(self.removeButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            self.removeButton.Show(bool(self._members))

            # Translators: Label for the add-member search field.
            addLabel = wx.StaticText(self, label=_("&Add member (type a handle or name to search):"))
            sizer.Add(addLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
            self.searchText = wx.TextCtrl(self)
            sizer.Add(self.searchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

            # Translators: Label above the member-search results checklist.
            self.suggestLabel = wx.StaticText(self, label=_("Search results:"))
            self.suggestionList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
            sizer.Add(self.suggestLabel, flag=wx.LEFT | wx.TOP, border=10)
            sizer.Add(self.suggestionList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            # Translators: Button to add checked search results as list members.
            self.addButton = wx.Button(self, label=_("Add &checked"))
            sizer.Add(self.addButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            self.suggestLabel.Hide()
            self.suggestionList.Hide()
            self.addButton.Hide()
        else:
            self.memberList = wx.ListBox(self, choices=self._memberChoiceLabels())
            sizer.Add(self.memberList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
            # Translators: Note shown when the current account isn't the list's creator.
            note = wx.StaticText(self, label=_("You're not the creator of this list -- membership is read-only."))
            sizer.Add(note, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Translators: Button to close the manage-members dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        if self._isOwner:
            self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
            self.searchText.Bind(wx.EVT_TEXT, self.onSearchTextChanged)
            self.addButton.Bind(wx.EVT_BUTTON, self.onAddSuggestion)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        if self._isOwner and not self._members:
            self.searchText.SetFocus()
        else:
            self.memberList.SetFocus()

    def _memberChoiceLabels(self):
        # Translators: Fallback shown for a member with no display name. Used as "@handle ({})".
        return [f'@{m["handle"]} ({m.get("display_name") or _("no display name")})' for m in self._members]

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        parent = self.GetParent()
        self.Destroy()
        # Manage members is opened from the list tree's context menu
        # now, not a standing toolbar button -- return focus there
        # instead of the now-hidden manageMembersButton (was never
        # added to any sizer, a leftover widget that only still
        # existed to be clicked -- previously left focus on a control
        # the user could never see or reach any other way).
        if parent is not None and hasattr(parent, "listTree"):
            parent.listTree.SetFocus()

    def _renderMembers(self):
        hasMembers = bool(self._members)
        self.memberList.Show(hasMembers)
        self.emptyMembersLabel.Show(not hasMembers)
        if hasMembers:
            self.memberList.Set(self._memberChoiceLabels())
            self.memberList.CheckedItems = []
            self.memberList.SetSelection(0)
        self.Layout()

    def _persistCache(self):
        db.set_user_list_cache(self._account["id"], f"list_members:{self._listInfo['uri']}", self._members)

    def onRemove(self, evt):
        if not self.removeButton.IsShown():
            return
        indices = list(self.memberList.CheckedItems)
        if not indices:
            # Translators: Announced when removing list members with nothing checked.
            nvdaUi.message(_("No members checked."))
            return
        toRemove = [self._members[i] for i in indices if 0 <= i < len(self._members)]
        if not toRemove:
            return

        names = ", ".join(f'@{m["handle"]}' for m in toRemove)
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for removing list members. {} is a comma-separated list of handles.
            _("Remove {} from this list?").format(names),
            # Translators: Title of the confirm-remove-members dialog.
            _("Confirm remove"), wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        self.removeButton.Disable()
        # Translators: Announced while removing list members. {} is the count.
        nvdaUi.message(_("Removing {} member(s)...").format(len(toRemove)))

        def worker():
            removed = []
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for member in toRemove:
                try:
                    client.remove_list_member(atprotoClient, member["listitem_uri"])
                    removed.append(member)
                except Exception as e:
                    errors.append(f'@{member["handle"]}: {e}')
            wx.CallAfter(self._onRemoveDone, removed, errors)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, removed, errors):
        self.removeButton.Enable()
        removedDids = {m["did"] for m in removed}
        self._members = [m for m in self._members if m["did"] not in removedDids]
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self._persistCache()
        self.Layout()
        if removed:
            names = ", ".join(f'@{m["handle"]}' for m in removed)
            # Translators: Announced after removing list members. {} is a comma-separated list of handles.
            nvdaUi.message(_("Removed {}.").format(names))
        if errors:
            # Translators: Announced when some member removals fail. {} is a semicolon-separated list of "handle: error" entries.
            nvdaUi.message(_("Some members could not be removed: {}").format("; ".join(errors)))

    def onSearchTextChanged(self, evt):
        wx.CallLater(400, self._runSearch, self.searchText.GetValue())

    def _runSearch(self, query):
        if query != self.searchText.GetValue():
            return  # a newer keystroke already superseded this debounce
        if not query.strip():
            self.suggestionList.Set([])
            self._suggestions = []
            self.suggestLabel.Hide()
            self.suggestionList.Hide()
            self.addButton.Hide()
            self.Layout()
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                results = client.search_actors_typeahead(atprotoClient, query)
                error = None
            except Exception as e:
                results = []
                error = str(e)
            wx.CallAfter(self._onSearchDone, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSearchDone(self, results, error):
        if error:
            return
        existingDids = {m["did"] for m in self._members}
        self._suggestions = [r for r in results if r["did"] not in existingDids]
        hasResults = bool(self._suggestions)
        self.suggestLabel.Show(hasResults)
        self.suggestionList.Show(hasResults)
        self.addButton.Show(hasResults)
        self.Layout()
        # Translators: List entry format for a search result with no display name set. {} is the handle.
        self.suggestionList.Set([f'@{r["handle"]} ({r.get("display_name") or _("no display name")})' for r in self._suggestions])
        self.suggestionList.CheckedItems = []
        if self._suggestions:
            self.suggestionList.SetSelection(0)

    def onAddSuggestion(self, evt):
        if not self.addButton.IsShown():
            return
        indices = list(self.suggestionList.CheckedItems)
        if not indices:
            # Translators: Announced when adding list members with nothing checked in search results.
            nvdaUi.message(_("No suggestions checked."))
            return
        toAdd = [self._suggestions[i] for i in indices if 0 <= i < len(self._suggestions)]
        if not toAdd:
            return

        self.addButton.Disable()
        # Translators: Announced while adding list members. {} is the count.
        nvdaUi.message(_("Adding {} member(s)...").format(len(toAdd)))

        def worker():
            added = []
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for actor in toAdd:
                try:
                    listitem_uri = client.add_list_member(atprotoClient, self._listInfo["uri"], actor["did"])
                    added.append((actor, listitem_uri))
                except Exception as e:
                    errors.append(f'@{actor["handle"]}: {e}')
            wx.CallAfter(self._onAddDone, added, errors)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onAddDone(self, added, errors):
        self.addButton.Enable()
        for actor, listitem_uri in added:
            self._members.append({
                "listitem_uri": listitem_uri,
                "did": actor["did"],
                "handle": actor["handle"],
                "display_name": actor.get("display_name"),
            })
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self._persistCache()
        self.searchText.SetValue("")
        self.suggestionList.Set([])
        self.suggestLabel.Hide()
        self.suggestionList.Hide()
        self.addButton.Hide()
        self.Layout()
        if added:
            names = ", ".join(f'@{a["handle"]}' for a, _listitemUri in added)
            # Translators: Announced after adding list members. {} is a comma-separated list of handles.
            nvdaUi.message(_("Added {}.").format(names))
        if errors:
            # Translators: Announced when some member additions fail. {} is a semicolon-separated list of "handle: error" entries.
            nvdaUi.message(_("Some members could not be added: {}").format("; ".join(errors)))


class AddToListDialog(wx.Dialog):
    """
    User action's "Add to list..." -- checklist of every list the
    active account owns (both curation and moderation lists -- the
    listitem record applies to either purpose the same way, see
    client.add_list_member), so a user can be added to several lists
    in one action. No confirm dialog -- reversible via list management,
    matches Follow/Pin's no-confirm convention rather than Delete's.
    """

    def __init__(self, parent, account, did, handle, display_name=None):
        self._account = account
        self._did = did
        self._handle = handle
        self._lists = [l for l in db.get_lists(account["id"]) if l["creator_did"] == account["did"]]

        label = f"@{handle}" if handle else did
        # Translators: Title of the add-to-list dialog. {} is the user's label.
        super().__init__(parent, title=_("Add {} to list").format(label), size=(400, 400))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label above the owned-lists checklist.
        # Translators: Shown instead of the checklist when the account has no lists yet.
        introLabel = wx.StaticText(self, label=_("&Lists you own:") if self._lists else _("You haven't created any lists yet."))
        sizer.Add(introLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)

        self.listCheckBox = gui.nvdaControls.CustomCheckListBox(
            self, choices=[self._listChoiceLabel(l) for l in self._lists]
        )
        sizer.Add(self.listCheckBox, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        if self._lists:
            self.listCheckBox.SetSelection(0)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to add the user to the checked lists.
        self.addButton = wx.Button(self, label=_("&Add"))
        # Translators: Button to close the add-to-list dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
        buttonRow.Add(self.addButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(closeBtn)
        sizer.Add(buttonRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)
        self.addButton.Show(bool(self._lists))

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.addButton.Bind(wx.EVT_BUTTON, self.onAdd)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        if self._lists:
            self.listCheckBox.SetFocus()
        else:
            closeBtn.SetFocus()

    def _listChoiceLabel(self, lst):
        # Translators: List-type suffix for a moderation list, e.g. "MyList (moderation list)".
        # Translators: List-type suffix for a curation list, e.g. "MyList (curation list)".
        kind = _("moderation list") if lst["purpose"] == client.LIST_PURPOSE_MOD else _("curation list")
        # Translators: Checklist entry combining a list's name and type. First {} is the name, second {} is the type.
        return _("{} ({})").format(lst["name"], kind)

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

    def onAdd(self, evt):
        indices = list(self.listCheckBox.CheckedItems)
        if not indices:
            # Translators: Announced when adding a user to lists with nothing checked.
            nvdaUi.message(_("No lists checked."))
            return
        toAdd = [self._lists[i] for i in indices if 0 <= i < len(self._lists)]
        if not toAdd:
            return

        self.addButton.Disable()
        # Translators: Announced while adding a user to lists. {} is the count.
        nvdaUi.message(_("Adding to {} list(s)...").format(len(toAdd)))

        def worker():
            added = []
            errors = []
            atprotoClient = client.get_client_for_active_account()
            for lst in toAdd:
                try:
                    client.add_list_member(atprotoClient, lst["list_uri"], self._did)
                    added.append(lst)
                except Exception as e:
                    errors.append(f'{lst["name"]}: {e}')
            wx.CallAfter(self._onAddDone, added, errors)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onAddDone(self, added, errors):
        self.addButton.Enable()
        if added:
            names = ", ".join(l["name"] for l in added)
            # Translators: Announced after adding a user to lists. {} is a comma-separated list of list names.
            nvdaUi.message(_("Added to: {}.").format(names))
        if errors:
            # Translators: Announced when adding to some lists fails. {} is a semicolon-separated list of "name: error" entries.
            nvdaUi.message(_("Some lists failed: {}").format("; ".join(errors)))
        if added and not errors:
            self.Close()


class ListsWindow(FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    TAB_KEY = "lists"
    # Same content shape as Home, so it gets the full shortcut set too
    # (see plan-09.md) -- Alt+number/Alt+U still branch on list purpose
    # via the _onAltNumber/_onAltU overrides below, independent of
    # these flags.
    SUPPORTS_NEW_POST = True
    SUPPORTS_FOCUS_NEXT_UNREAD = True
    SUPPORTS_SELECT_ALL = True
    SUPPORTS_JUMP_TO_USER = True

    """
    "My lists" -- a flat tree of every list this account created or
    subscribed to (mute/block), same set bsky.app's "My lists" page
    shows. Selecting a curation list shows its timeline on the right
    (a normal post list -- Post action/User action apply unchanged,
    feed_key is just the list's own at:// uri, so FeedListMixin/
    get_feed_page/check-for-updates all work exactly like Home/Saved).
    Selecting a moderation list has no timeline (modlists aren't a
    feed), so the right side swaps to a member roster instead.
    List-level management (create/delete, open in its own tab, edit
    membership, subscribe to someone else's moderation list) lives in
    this tab's own local toolbar, not the shared MainWindow one.
    """

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        # Translators: Permanent tab label for the Lists tab.
        self.TAB_NAME = _("Lists")
        self._lists = []
        self._selectedList = None  # the dict for whichever tree row is selected
        self._feedKey = None
        self._initFeedListState()

        sizer = wx.BoxSizer(wx.VERTICAL)

        splitRow = wx.BoxSizer(wx.HORIZONTAL)

        self.listTree = wx.TreeCtrl(
            self, style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_SINGLE | wx.TR_LINES_AT_ROOT
        )
        # Translators: Hidden root label of the lists tree (used as its accessible name).
        self._listRoot = self.listTree.AddRoot(_("Lists"))
        splitRow.Add(self.listTree, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=5)

        self.postList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildFeedListColumns()
        splitRow.Add(self.postList, proportion=2, flag=wx.EXPAND)

        self.memberList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for a member's handle.
        self.memberList.InsertColumn(0, _("Handle"), width=220)
        # Translators: Column header for a member's display name.
        self.memberList.InsertColumn(1, _("Display name"), width=220)
        splitRow.Add(self.memberList, proportion=2, flag=wx.EXPAND)
        self.memberList.Hide()

        sizer.Add(splitRow, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to open the Post action menu. Shows the Alt+A shortcut.
        self.postActionButton = wx.Button(self, label=_("&Post action... (Alt+A)"))
        # Translators: Button to open the User action menu. Shows the Alt+U shortcut.
        self.userActionButton = wx.Button(self, label=_("&User action... (Alt+U)"))
        actionRow.Add(self.postActionButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.userActionButton)
        sizer.Add(actionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing is focused/selectable at construction time yet --
        # _showSelectedList()/_updateActionButtons() re-show these once
        # a curation list with posts actually gets focused. Disable()
        # too, not just Hide() -- see _showSelectedList's matching fix
        # for why (mnemonic dispatch ignores visibility).
        self.postActionButton.Hide()
        self.postActionButton.Disable()
        self.userActionButton.Hide()
        self.userActionButton.Disable()

        toolbarRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to create a new list. (Hidden -- kept as a real widget for onAddList()'s focus-restore calls; the shared toolbar's New-post button becomes "New list..." on this tab instead.)
        self.addListButton = wx.Button(self, label=_("Add list..."))
        # Translators: Button to remove the selected list. (Hidden -- moved into the list tree's context menu.)
        self.removeListButton = wx.Button(self, label=_("Remove list"))
        # Translators: Button to open the selected list in its own tab. (Hidden -- moved into the list tree's context menu.)
        self.showInNewTabButton = wx.Button(self, label=_("Show in new tab"))
        # Translators: Button to manage the selected list's members. (Hidden -- moved into the list tree's context menu.)
        self.manageMembersButton = wx.Button(self, label=_("Manage members..."))
        # Translators: Button to find and subscribe to lists by another user.
        self.subscribeButton = wx.Button(self, label=_("&Find lists by user..."))
        for button in (self.addListButton, self.removeListButton, self.showInNewTabButton, self.manageMembersButton, self.subscribeButton):
            button.Hide()
            button.Disable()
        #self.addListButton.Hide()
        # Remove/Show in new tab/Manage members moved into the list
        # tree's context menu (see onListContextMenu) -- mirrors Chat's
        # conversation-tree menu. "Add list..." moved to the shared
        # toolbar's New-post button (becomes "New list..." on this tab,
        # see mainWindow.py's onNewPost) -- kept as a real widget
        # (just hidden) rather than deleted so onAddList()'s existing
        # focus-restore calls (parent.addListButton.SetFocus()-style,
        # if any) don't need touching.
        toolbarRow.Add(self.subscribeButton, flag=wx.RIGHT, border=5)
        sizer.Add(toolbarRow, flag=wx.ALL, border=10)

# note: removeListButton/showInNewTabButton/manageMembersButton are intentionally left OUT of toolbarRow.Add() above -- only subscribeButton gets added now, the other three just aren't placed in a sizer (still constructed further down where .Bind() references them, but never shown).

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self.addListButton.Bind(wx.EVT_BUTTON, self.onAddList)
        self.removeListButton.Bind(wx.EVT_BUTTON, self.onRemoveList)
        self.showInNewTabButton.Bind(wx.EVT_BUTTON, self.onShowInNewTab)
        self.manageMembersButton.Bind(wx.EVT_BUTTON, self.onManageMembers)
        self.subscribeButton.Bind(wx.EVT_BUTTON, self.onSubscribeViaLink)
        self.listTree.Bind(wx.EVT_TREE_SEL_CHANGED, self.onListSelected)
        self.listTree.Bind(wx.EVT_TREE_ITEM_MENU, self.onListContextMenu)
        self.listTree.Bind(wx.EVT_CHAR_HOOK, self.onListTreeCharHook)
        self.postActionButton.Bind(wx.EVT_BUTTON, self.onPostAction)
        self.userActionButton.Bind(wx.EVT_BUTTON, self.onUserAction)
        self.postList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onItemFocused)
        self.postList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onItemActivated)
        self.postList.Bind(wx.EVT_CONTEXT_MENU, self.onPostAction)
        self.memberList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onMemberAction)
        self.memberList.Bind(wx.EVT_CONTEXT_MENU, self.onMemberContextMenu)
        self.memberList.Bind(wx.EVT_CHAR_HOOK, self.onMemberListCharHook)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._loadListsFromCache()

        if self._account is None:
            # Translators: Announced when opening a tab with no active account.
            nvdaUi.message(_("No active account. Log in from Settings first."))

    # ---------------- MainWindow integration hooks ----------------

    def onTabActivated(self):
        # Translators: Announced when switching to this tab. {} is the tab name.
        nvdaUi.message(_("{} tab").format(self.TAB_NAME))
        self.listTree.SetFocus()

    def _restoreFocusPosition(self, moveFocus=True):
        # Lists behaves like Chat: the tree is this tab's "home"
        # control, not whichever list happens to be showing on the
        # right -- MainWindow's addTab()/_focusPanel() call this
        # expecting to land real focus somewhere sane on tab open, and
        # for this tab that's always the tree. The per-list post-
        # scroll-position restore (used when switching which list is
        # selected, see _showSelectedList below) is a separate concern
        # and calls FeedListMixin._restoreFocusPosition directly
        # instead of going through this override.
        if moveFocus:
            self.listTree.SetFocus()

    # ---------------- lists tree ----------------

    def _loadListsFromCache(self, select_uri=None):
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot(_("Lists"))
            self._lists = db.get_lists(self._account["id"]) if self._account else []

            targetItem = None
            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])
                if select_uri and lst["list_uri"] == select_uri:
                    targetItem = item

            if targetItem is not None:
                self.listTree.SelectItem(targetItem)
            else:
                firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
                if firstItem.IsOk():
                    self.listTree.SelectItem(firstItem)
                else:
                    self._selectedList = None
                    self.postList.DeleteAllItems()
                    self.memberList.DeleteAllItems()
                    self.postActionButton.Hide()
                    self.userActionButton.Hide()
                    self.Layout()
        finally:
            self.listTree.Thaw()

        self._updateListsStatusBar()
        self._updateToolbarVisibility()
        # Deliberately NOT auto-syncing from the server here anymore.
        # This used to fire unconditionally on every __init__ (i.e.
        # every MainWindow open), unlike the other 4 permanent tabs
        # which only ever load from local cache at construction time
        # -- this was Stage 2 of the original crash-hardening plan
        # (plan-07.md), agreed on but never actually done; Stage 0's
        # safe_ui_callback fix made the resulting race SAFE at the
        # Python level (caught RuntimeError instead of a hard crash)
        # but never removed the race itself. Confirmed by testing
        # (see plan-09.md): every "quick close after opening
        # MainWindow" crash report so far shows this exact background
        # sync's completion handler as the immediately-preceding
        # event, every time, regardless of which tab the user actually
        # interacted with -- removing the automatic call here matches
        # ListsWindow's behavior to the other 4 tabs (cache-only on
        # open, sync only ever on explicit user action -- F5/Shift+F5/
        # after add-list/remove-list/subscribe, which still call
        # _syncListsFromServer() directly and are untouched by this).

    def _updateToolbarVisibility(self):
        # No-op now -- Remove/Show in new tab/Manage members live in
        # the list tree's context menu (see onListContextMenu), which
        # naturally only appears on an actual item, so there's nothing
        # left to show/hide here. Kept as a callable stub since it's
        # still called from a couple of places below.
        pass

    def onListContextMenu(self, evt):
        item = evt.GetItem()
        if not item.IsOk() or item == self._listRoot:
            return
        self.listTree.SelectItem(item)
        lst = next((l for l in self._lists if l["list_uri"] == self.listTree.GetItemData(item)), None)
        if lst is None:
            return

        menu = wx.Menu()
        # Translators: Context menu item to remove a list.
        removeItem = menu.Append(wx.ID_ANY, _("&Remove list"))
        # Translators: Context menu item to open a list in its own tab.
        openTabItem = menu.Append(wx.ID_ANY, _("S&how in new tab"))
        # Translators: Context menu item to manage a list's members.
        membersItem = menu.Append(wx.ID_ANY, _("&Manage members..."))
        self.Bind(wx.EVT_MENU, lambda e: self.onRemoveList(), removeItem)
        self.Bind(wx.EVT_MENU, lambda e: self.onShowInNewTab(), openTabItem)
        self.Bind(wx.EVT_MENU, lambda e: self.onManageMembers(), membersItem)

        # Mute/Block only apply to moderation lists -- a curation list
        # has no such server-side effect (see client.mute_actor_list's
        # docstring: it's a private mute-list-membership procedure,
        # only meaningful for lists that gate your own timeline/DMs).
        if lst["purpose"] == client.LIST_PURPOSE_MOD:
            menu.AppendSeparator()
            isMuted = bool(lst.get("muted"))
            isBlocked = bool(lst.get("blocked_uri"))
            # Translators: Context menu item (list already muted).
            # Translators: Context menu item (list not yet muted).
            muteItem = menu.Append(wx.ID_ANY, _("Un&mute list") if isMuted else _("&Mute list"))
            # Translators: Context menu item (list already blocked).
            # Translators: Context menu item (list not yet blocked).
            blockItem = menu.Append(wx.ID_ANY, _("Unbloc&k list") if isBlocked else _("Bloc&k list"))
            self.Bind(wx.EVT_MENU, lambda e: self._toggleListMuted(lst), muteItem)
            self.Bind(wx.EVT_MENU, lambda e: self._toggleListBlocked(lst), blockItem)

        self.PopupMenu(menu)
        menu.Destroy()

    def _listLabel(self, lst):
        # Translators: List-type suffix for a moderation list, e.g. "MyList (moderation list)".
        # Translators: List-type suffix for a curation list, e.g. "MyList (curation list)".
        kind = _("moderation list") if lst["purpose"] == client.LIST_PURPOSE_MOD else _("curation list")
        tags = []
        if lst["purpose"] == client.LIST_PURPOSE_MOD:
            if lst.get("muted"):
                # Translators: Tag shown for a muted list.
                tags.append(_("muted"))
            if lst.get("blocked_uri"):
                # Translators: Tag shown for a blocked list.
                tags.append(_("blocked"))
        suffix = ", " + ", ".join(tags) if tags else ""
        # Translators: Tree label combining a list's name, type, and tags. First {} is the name, second {} is "{type}{tags}".
        return _("{} ({})").format(lst["name"], f"{kind}{suffix}")

    def _updateListsStatusBar(self):
        # Translators: Lists tab status bar text. {} is the total count.
        self.statusBar.SetStatusText(_("Lists {} total").format(len(self._lists)))

    def _syncListsFromServer(self):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                entries = client.get_lists(atprotoClient, self._account["did"])
                for entry in entries:
                    db.upsert_list({
                        "account_id": self._account["id"],
                        "list_uri": entry["uri"],
                        "cid": entry["cid"],
                        "name": entry["name"],
                        "description": entry["description"],
                        "purpose": entry["purpose"],
                        "creator_did": entry["creator_did"],
                        "creator_handle": entry["creator_handle"],
                        "muted": int(entry["muted"]),
                        "blocked_uri": entry["blocked_uri"],
                    })
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onSyncListsDone, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onSyncListsDone(self, error):
        if error:
            log.error(f"NVSky: list sync failed: {error}")
            # Translators: Announced when refreshing lists fails. {} is the error message.
            nvdaUi.message(_("Could not refresh lists: {}").format(error))
            return
        selectedUri = self._selectedList["list_uri"] if self._selectedList else None
        self._lists = db.get_lists(self._account["id"])
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot(_("Lists"))
            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])

            item, cookie = self.listTree.GetFirstChild(self._listRoot)
            selected = False
            while item.IsOk():
                if self.listTree.GetItemData(item) == selectedUri:
                    self.listTree.SelectItem(item)
                    selected = True
                    break
                item, cookie = self.listTree.GetNextChild(self._listRoot, cookie)
            if not selected:
                firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
                if firstItem.IsOk():
                    self.listTree.SelectItem(firstItem)
                else:
                    self._selectedList = None
                    self.postList.DeleteAllItems()
                    self.memberList.DeleteAllItems()
                    self.postActionButton.Hide()
                    self.userActionButton.Hide()
                    self.Layout()
        finally:
            self.listTree.Thaw()
        self._updateListsStatusBar()
        self._updateToolbarVisibility()

    def _checkListTreeBoundaryBeforeKey(self, keyCode):
        # Same deterministic-boundary reasoning as chatWindow.py's
        # _checkConvoTreeBoundaryBeforeKey -- TreeCtrl sibling-based
        # navigation instead of a flat index.
        item = self.listTree.GetSelection()
        if not item.IsOk() or item == self._listRoot:
            return
        if keyCode == wx.WXK_UP:
            prevItem = self.listTree.GetPrevSibling(item)
            if not prevItem.IsOk():
                soundpack.play("boundary")
        elif keyCode == wx.WXK_DOWN:
            nextItem = self.listTree.GetNextSibling(item)
            if not nextItem.IsOk():
                soundpack.play("boundary")

    def onListTreeCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode in (wx.WXK_UP, wx.WXK_DOWN) and not evt.HasAnyModifiers():
            self._checkListTreeBoundaryBeforeKey(keyCode)
        evt.Skip()

    def _checkMemberListBoundaryBeforeKey(self, keyCode):
        if not getattr(self, "_currentMembers", None):
            return
        index = self.memberList.GetFocusedItem()
        if index == -1:
            return
        if keyCode == wx.WXK_UP and index == 0:
            soundpack.play("boundary")
        elif keyCode == wx.WXK_DOWN and index == len(self._currentMembers) - 1:
            soundpack.play("boundary")

    def onMemberListCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode in (wx.WXK_UP, wx.WXK_DOWN) and not evt.HasAnyModifiers():
            self._checkMemberListBoundaryBeforeKey(keyCode)
        evt.Skip()

    def onListSelected(self, evt):
        item = evt.GetItem()
        if item.IsOk() and item != self._listRoot:
            listUri = self.listTree.GetItemData(item)
            self._selectedList = next((l for l in self._lists if l["list_uri"] == listUri), None)
            self._showSelectedList()
        evt.Skip()

    def _showSelectedList(self):
        if self._selectedList is None:
            return
        if self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self.postList.Hide()
            self.memberList.Show()
            # Disable() too, not just Hide() -- CONFIRMED bug pattern
            # elsewhere (ChatWindow's Accept button): a hidden-but-
            # enabled button still fires its own mnemonic. Alt+U here
            # collides with _onAltU's own list-purpose branching in
            # this same class.
            self.postActionButton.Hide()
            self.postActionButton.Disable()
            self.userActionButton.Hide()
            self.userActionButton.Disable()
            self.Layout()
            self._loadMembersLive()
        else:
            self.memberList.Hide()
            self.postList.Show()
            self.Layout()
            self._feedKey = self._selectedList["list_uri"]
            self._loadFromCache(reset=True)
            FeedListMixin._restoreFocusPosition(self, moveFocus=False)

    # ---------------- curation list timeline (FeedListMixin hooks) ----------------

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _visible_embed_text(post))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _visible_message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_list_feed(atprotoClient, self._account["id"], self._feedKey, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    # ---------------- moderation list members ----------------

    def _loadMembersLive(self):
        self.memberList.DeleteAllItems()
        # Translators: Announced while loading a moderation list's members.
        nvdaUi.message(_("Loading members, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                info = client.get_list(atprotoClient, self._selectedList["list_uri"])
                error = None
            except Exception as e:
                info = None
                error = str(e)
            wx.CallAfter(self._onMembersLoaded, info, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onMembersLoaded(self, info, error):
        if error:
            # Translators: Announced when loading a list's members fails. {} is the error message.
            nvdaUi.message(_("Could not load members: {}").format(error))
            return
        self._currentMembers = info["members"]
        self.memberList.Freeze()
        try:
            self.memberList.DeleteAllItems()
            for i, m in enumerate(self._currentMembers):
                self.memberList.InsertItem(i, f'@{m["handle"]}')
                self.memberList.SetItem(i, 1, m.get("display_name") or "")
        finally:
            self.memberList.Thaw()
        # Translators: Status bar text for a moderation list's member roster. First {} is the list name, second {} is the count.
        self.statusBar.SetStatusText(_("{} {} members").format(self._selectedList['name'], len(self._currentMembers)))
        if self._currentMembers:
            self.memberList.Focus(0)
            self.memberList.Select(0)

    def _getFocusedMember(self):
        index = self.memberList.GetFocusedItem()
        if 0 <= index < len(self._currentMembers):
            return self._currentMembers[index]
        return None

    def onMemberAction(self, evt=None):
        member = self._getFocusedMember()
        if member is None:
            # Translators: Announced when the user-action menu is invoked with no member focused.
            nvdaUi.message(_("No member selected."))
            return
        self.showUserActionMenu(member["did"], member["handle"], member.get("display_name"))

    def onMemberContextMenu(self, evt):
        self.onMemberAction()

    # ---------------- toolbar actions ----------------

    def onNewPost(self, evt=None):
        # SUPPORTS_NEW_POST=True routes Ctrl+N here via
        # FeedListMixin.onCharHook -- without this override it fell
        # through to FeedListMixin's own generic onNewPost (opens a
        # post ComposeDialog), wrong for this tab. The toolbar's
        # "New list..." button already worked (MainWindow.onNewPost
        # has its own identity check calling onAddList directly) --
        # only the keyboard path was missing this.
        self.onAddList(evt)

    def onAddList(self, evt=None):
        if self._account is None:
            # Translators: Announced when trying to create a list with no active account.
            nvdaUi.message(_("No active account."))
            return
        gui.mainFrame.prePopup()
        dlg = AddListDialog(self)
        result = dlg.ShowModal()
        name, description, purpose = dlg.getValues()
        dlg.Destroy()
        gui.mainFrame.postPopup()
        self.addListButton.SetFocus()
        if result != wx.ID_OK:
            return
        if not name:
            # Translators: Announced when trying to create a list with no name typed.
            nvdaUi.message(_("A list needs a name."))
            return
        self._createList(name, description, purpose)

    def _createList(self, name, description, purpose):
        # Translators: Announced while creating a new list.
        nvdaUi.message(_("Creating list, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                result = client.create_list(atprotoClient, name, description, purpose)
                error = None
            except Exception as e:
                result = None
                error = str(e)
            wx.CallAfter(self._onCreateListDone, name, description, purpose, result, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onCreateListDone(self, name, description, purpose, result, error):
        if error:
            # Translators: Announced when creating a list fails. {} is the error message.
            nvdaUi.message(_("Could not create list: {}").format(error))
            return
        # Optimistic: insert directly from create_list's own uri/cid
        # response instead of a second full _syncListsFromServer()
        # network round trip.
        db.upsert_list({
            "account_id": self._account["id"],
            "list_uri": result["uri"],
            "cid": result["cid"],
            "name": name,
            "description": description,
            "purpose": purpose,
            "creator_did": self._account["did"],
            "creator_handle": self._account["handle"],
            "muted": 0,
            "blocked_uri": None,
        })
        # Translators: Announced after successfully creating a list.
        nvdaUi.message(_("List created."))
        self._loadListsFromCache(select_uri=result["uri"])

    def onRemoveList(self, evt=None):
        if self._selectedList is None:
            # Translators: Announced when trying to remove a list with none focused.
            nvdaUi.message(_("No list selected."))
            return
        if self._selectedList["creator_did"] != self._account["did"]:
            # Translators: Announced when trying to remove a list owned by someone else.
            nvdaUi.message(_("You can only remove a list you created -- use Find lists by user's Mute/Block to leave someone else's list."))
            return

        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for removing a list. {} is the list's name.
            _('Remove the list "{}"? This can\'t be undone.').format(self._selectedList["name"]),
            # Translators: Title of the confirm-remove-list dialog.
            _("Confirm remove"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        listUri = self._selectedList["list_uri"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.delete_list(atprotoClient, listUri)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveListDone, listUri, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRemoveListDone(self, listUri, error):
        if error:
            soundpack.play("error")
            # Translators: Announced when removing a list fails. {} is the error message.
            nvdaUi.message(_("Could not remove list: {}").format(error))
            return
        soundpack.play("delete")
        db.delete_list(self._account["id"], listUri)
        # Translators: Announced after removing a list.
        nvdaUi.message(_("List removed."))
        self._selectedList = None
        self._loadListsFromCache()

    def onShowInNewTab(self, evt=None):
        if self._selectedList is None:
            # Translators: Announced when trying to open a list in a tab with none focused.
            nvdaUi.message(_("No list selected."))
            return
        if self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            # Translators: Announced when trying to open a moderation list in a tab.
            nvdaUi.message(_("Moderation lists don't have a timeline to open in a tab."))
            return
        mainWindow = self.GetTopLevelParent()
        tab = ListTabWindow(
            mainWindow.notebook, self._selectedList["list_uri"], self._selectedList["name"], origin_key="lists"
        )
        mainWindow.addTab(tab, self._selectedList["name"], select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "list",
            "key": self._selectedList["list_uri"],
            "list_uri": self._selectedList["list_uri"],
            "list_name": self._selectedList["name"],
            "origin_key": "lists",
        })

    def _refreshListTreeLabel(self, listUri):
        item, cookie = self.listTree.GetFirstChild(self._listRoot)
        while item.IsOk():
            if self.listTree.GetItemData(item) == listUri:
                lst = next((l for l in self._lists if l["list_uri"] == listUri), None)
                if lst is not None:
                    self.listTree.SetItemText(item, self._listLabel(lst))
                break
            item, cookie = self.listTree.GetNextChild(self._listRoot, cookie)

    def _toggleListMuted(self, lst):
        listUri = lst["list_uri"]
        wasMuted = bool(lst.get("muted"))
        lst["muted"] = not wasMuted
        db.set_list_muted(self._account["id"], listUri, not wasMuted)
        self._refreshListTreeLabel(listUri)
        # Translators: Announced after unmuting a list.
        # Translators: Announced after muting a list.
        _announce_now(_("List unmuted.") if wasMuted else _("List muted."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if wasMuted:
                    client.unmute_actor_list(atprotoClient, listUri)
                else:
                    client.mute_actor_list(atprotoClient, listUri)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onToggleListMutedDone, lst, wasMuted, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onToggleListMutedDone(self, lst, previousMuted, error):
        if not error:
            return
        lst["muted"] = previousMuted
        db.set_list_muted(self._account["id"], lst["list_uri"], previousMuted)
        self._refreshListTreeLabel(lst["list_uri"])
        if previousMuted:
            # Translators: Announced when unmuting a list fails. {} is the error message.
            nvdaUi.message(_("Could not unmute list: {}").format(error))
        else:
            # Translators: Announced when muting a list fails. {} is the error message.
            nvdaUi.message(_("Could not mute list: {}").format(error))

    def _toggleListBlocked(self, lst):
        listUri = lst["list_uri"]
        previousBlockedUri = lst.get("blocked_uri")
        wasBlocked = bool(previousBlockedUri)
        lst["blocked_uri"] = None if wasBlocked else "pending"
        db.set_list_blocked_uri(self._account["id"], listUri, lst["blocked_uri"])
        self._refreshListTreeLabel(listUri)
        # Translators: Announced after unblocking a list.
        # Translators: Announced after blocking a list.
        _announce_now(_("List unblocked.") if wasBlocked else _("List blocked."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if wasBlocked:
                    client.unblock_actor_list(atprotoClient, previousBlockedUri)
                    newUri = None
                else:
                    newUri = client.block_actor_list(atprotoClient, listUri)
                error = None
            except Exception as e:
                newUri = None
                error = str(e)
            wx.CallAfter(self._onToggleListBlockedDone, lst, previousBlockedUri, newUri, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onToggleListBlockedDone(self, lst, previousBlockedUri, newUri, error):
        if error:
            lst["blocked_uri"] = previousBlockedUri
            db.set_list_blocked_uri(self._account["id"], lst["list_uri"], previousBlockedUri)
            self._refreshListTreeLabel(lst["list_uri"])
            if previousBlockedUri:
                # Translators: Announced when unblocking a list fails. {} is the error message.
                nvdaUi.message(_("Could not unblock list: {}").format(error))
            else:
                # Translators: Announced when blocking a list fails. {} is the error message.
                nvdaUi.message(_("Could not block list: {}").format(error))
            return
        if not previousBlockedUri:
            lst["blocked_uri"] = newUri
            db.set_list_blocked_uri(self._account["id"], lst["list_uri"], newUri)
            self._refreshListTreeLabel(lst["list_uri"])

    def onManageMembers(self, evt=None):
        if self._selectedList is None:
            # Translators: Announced when trying to manage members with no list focused.
            nvdaUi.message(_("No list selected."))
            return
        listUri = self._selectedList["list_uri"]
        cachedMembers = db.get_user_list_cache(self._account["id"], f"list_members:{listUri}")
        if cachedMembers is not None:
            # Cache-first: open immediately, refresh the cache silently
            # in the background for next time (matches settings.py's
            # FeedManagerPanel/MutedWordsPanel convention).
            info = {
                "uri": listUri,
                "name": self._selectedList["name"],
                "creator_did": self._selectedList["creator_did"],
                "members": cachedMembers,
            }
            gui.mainFrame.prePopup()
            dlg = ManageMembersDialog(self, self._account, info)
            dlg.Show()
            self._refreshListMembersCache(listUri)
            return

        # Translators: Announced while loading a moderation list's members.
        nvdaUi.message(_("Loading members, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                info = client.get_list(atprotoClient, listUri)
                error = None
            except Exception as e:
                info = None
                error = str(e)
            wx.CallAfter(self._onManageMembersInfoReady, info, error)

        uiutil.start_worker(worker)

    def _refreshListMembersCache(self, listUri):
        # Silent background refresh -- the dialog already showing
        # cached data doesn't need to know about this.
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                info = client.get_list(atprotoClient, listUri)
                db.set_user_list_cache(self._account["id"], f"list_members:{listUri}", info["members"])
            except Exception as e:
                log.error(f"NVSky: background list-members refresh failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onManageMembersInfoReady(self, info, error):
        if error:
            # Translators: Announced when loading a list's info fails. {} is the error message.
            nvdaUi.message(_("Could not load list: {}").format(error))
            return
        db.set_user_list_cache(self._account["id"], f"list_members:{info['uri']}", info["members"])
        gui.mainFrame.prePopup()
        dlg = ManageMembersDialog(self, self._account, info)
        dlg.Show()

    def onSubscribeViaLink(self, evt=None):
        gui.mainFrame.prePopup()
        dlg = SubscribeListDialog(self, on_subscribed=self._syncListsFromServer)
        dlg.Show()

    # ---------------- keyboard ----------------

    # onCharHook is inherited from FeedListMixin -- Alt+number/Alt+U
    # stay class-specific here since they branch on list purpose.

    def _onAltNumber(self, n):
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            self._announceNthNewestPost(n)

    def _onAltU(self):
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self.onMemberAction()
        else:
            self.onUserAction()
        
    def _syncForBulkCheck(self, atprotoClient):
        # Was calling self._syncListsFromServer(), which spawns its OWN
        # separate background thread and its OWN separate
        # client.get_client_for_active_account() login -- called from
        # INSIDE the bulk-check flow's already-shared background thread,
        # this raced a second concurrent login against the one
        # checkAllOpenTabs already holds, exactly the class of bug the
        # shared-thread design was meant to avoid (see MainWindow.
        # checkAllOpenTabs' own comment). Also fire-and-forget, so this
        # method's return value never reflected whether anything
        # actually changed -- confirmed as the cause of "Lists doesn't
        # announce AND doesn't update" during Ctrl+F5. Do the list-sync
        # work directly here instead, synchronously, on the SAME
        # atprotoClient/thread the caller already established.
        beforeSnapshot = {l["list_uri"]: l.get("muted") for l in self._lists}
        entries = client.get_lists(atprotoClient, self._account["did"])
        for entry in entries:
            db.upsert_list({
                "account_id": self._account["id"],
                "list_uri": entry["uri"],
                "cid": entry["cid"],
                "name": entry["name"],
                "description": entry["description"],
                "purpose": entry["purpose"],
                "creator_did": entry["creator_did"],
                "creator_handle": entry["creator_handle"],
                "muted": int(entry["muted"]),
                "blocked_uri": entry["blocked_uri"],
            })
        afterLists = db.get_lists(self._account["id"])
        afterSnapshot = {l["list_uri"]: l.get("muted") for l in afterLists}
        listsChanged = afterSnapshot != beforeSnapshot

        curateChanged = False
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            curateChanged = super()._syncForBulkCheck(atprotoClient)

        return listsChanged or curateChanged

    def _reloadAfterBulkCheck(self, moveFocus=True):
        # _syncForBulkCheck above already wrote fresh list metadata to
        # the DB synchronously -- rebuild the tree from it here instead
        # of leaving it stale until the next explicit F5 (which still
        # goes through the async _syncListsFromServer/_onSyncListsDone
        # path unchanged).
        selectedUri = self._selectedList["list_uri"] if self._selectedList else None
        self._lists = db.get_lists(self._account["id"])
        self.listTree.Freeze()
        try:
            self.listTree.DeleteAllItems()
            self._listRoot = self.listTree.AddRoot(_("Lists"))
            for lst in self._lists:
                item = self.listTree.AppendItem(self._listRoot, self._listLabel(lst))
                self.listTree.SetItemData(item, lst["list_uri"])
            item, cookie = self.listTree.GetFirstChild(self._listRoot)
            selected = False
            while item.IsOk():
                if self.listTree.GetItemData(item) == selectedUri:
                    self.listTree.SelectItem(item)
                    selected = True
                    break
                item, cookie = self.listTree.GetNextChild(self._listRoot, cookie)
            if not selected:
                firstItem, _cookie = self.listTree.GetFirstChild(self._listRoot)
                if firstItem.IsOk():
                    self.listTree.SelectItem(firstItem)
        finally:
            self.listTree.Thaw()
        self._updateListsStatusBar()

        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            super()._reloadAfterBulkCheck(moveFocus=moveFocus)
        elif self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self._loadMembersLive()

    def onCheckForUpdates(self, evt):
        self._syncListsFromServer()
        if self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_CURATE:
            super().onCheckForUpdates(evt)
        elif self._selectedList and self._selectedList["purpose"] == client.LIST_PURPOSE_MOD:
            self._loadMembersLive()

    def onClearCache(self, evt=None):
        # Only clears the SELECTED curation list's timeline cache --
        # never touches the `lists` table itself (list metadata isn't
        # a post cache) and no-ops on a moderation list (no timeline).
        if self._account is None:
            # Translators: Announced when clearing cache with no active account.
            nvdaUi.message(_("No active account."))
            return
        if self._selectedList is None or self._selectedList["purpose"] != client.LIST_PURPOSE_CURATE:
            # Translators: Announced when trying to clear cache without a curation list focused.
            nvdaUi.message(_("Nothing to clear -- select a curation list first."))
            return
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation to clear a list's cached posts. {} is the list's name.
            _("Clear cached posts for {}? This can't be undone.").format(self._selectedList['name']),
            # Translators: Title of the clear-cache confirmation dialog.
            _("Clear cache"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        db.clear_feed_key_cache(self._account["id"], self._feedKey)
        self._loadFromCache(reset=True)
        if self._posts:
            self.postList.SetFocus()
            return
        mainWindow = self.GetTopLevelParent()
        checkButton = getattr(mainWindow, "checkUpdatesButton", None)
        if checkButton is not None:
            checkButton.SetFocus()


class ListTabWindow(RemovableTabMixin, FeedListMixin, ItemActionMixin, UserActionMixin, EmbedViewMixin, wx.Panel):
    """
    A single curation list's timeline, popped out into its own
    removable tab via Lists' "Show in new tab" -- structurally a clone
    of SavedWindow fixed to one list_uri instead of a filter choice,
    so it can stay open and be checked for updates independently of
    the main Lists tab.
    """

    def __init__(self, parent, list_uri: str, list_name: str, origin_key: str = None):
        super().__init__(parent)

        self._account = db.get_active_account()
        self.TAB_NAME = list_name
        # Generic identity for MainWindow's remember-last-tab feature.
        self.TAB_TEMP_TYPE = "list"
        self.TAB_TEMP_KEY = list_uri
        self._feedKey = list_uri
        self._originTabKey = origin_key
        self._initFeedListState()

        self._buildStandardFeedSizer()
        self._bindStandardFeedEvents()
        self._finishStandardFeedInit(sync_if_empty=True)

    def onTabActivated(self):
        self._render()
        if self._account is not None:
            # Translators: Announced when switching to this tab. {} is the tab name.
            nvdaUi.message(_("{} tab").format(self.TAB_NAME))
            self._restoreFocusPosition()

    def _getActionablePost(self):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the post-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
        return post

    def _insertRow(self, index: int, post: dict, mode: str):
        self.postList.InsertItem(index, _visible_embed_text(post))
        self.postList.SetItem(index, 1, self._authorLabel(post, mode))
        self.postList.SetItem(index, 2, _visible_message_text(post))
        self.postList.SetItem(index, 3, _format_post_time(post.get("indexed_at")))

    def _dbGetPage(self, before_indexed_at=None, limit=None):
        return db.get_feed_page(self._account["id"], self._feedKey, before_indexed_at=before_indexed_at, limit=limit)

    def _dbGetUnreadCount(self):
        return db.get_unread_count(self._account["id"], self._feedKey)

    def _syncPage(self, atprotoClient, cursor, limit):
        return client.sync_list_feed(atprotoClient, self._account["id"], self._feedKey, cursor=cursor, limit=limit)

    def _markItemRead(self, post):
        db.mark_post_read(post["uri"])

    def onUserAction(self, evt=None):
        post = self._getFocusedPost()
        if post is None:
            # Translators: Announced when the user-action menu is invoked with no post focused.
            nvdaUi.message(_("No post selected."))
            return
        self.showUserActionMenu(post["author_did"], post.get("handle"), post.get("display_name"))

    def onTabRemoved(self):
        # MainWindow.removeCurrentTab() calls this (if present) right
        # before DeletePage() -- see the mainWindow.py edit -- so a
        # temp tab the user closes with Ctrl+W doesn't come back next
        # time NVSky opens. Scans for every wx.Timer instance rather
        # than calling _stopTimeRefreshTimer() by name (see plan-09.md
        # -- MainWindow.onClose had the identical gap: only knew about
        # _timeRefreshTimer and missed _loadingTimer, the F5-in-progress
        # beep, which this tab can also start via onCheckForUpdates).
        for value in vars(self).values():
            if isinstance(value, wx.Timer):
                value.Stop()
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "list", self._feedKey)
        self._jumpBackToOrigin()

    def onTabRenamed(self, newName):
        # MainWindow.renameCurrentTab() calls this (if present) right
        # after updating panel.TAB_NAME in memory -- persists the new
        # name into this tab's existing db.get_open_temp_tabs() entry
        # so it survives past this session (previously session-only).
        # Overrides RemovableTabMixin's no-op stub -- ListTabWindow IS
        # user-renameable, unlike the other RemovableTabMixin hosts.
        if self._account is not None:
            db.set_temp_tab_custom_name(self._account["id"], "list", self._feedKey, newName)
    
    # onCharHook is inherited from FeedListMixin -- no SUPPORTS_* flags
    # needed here, this class never had Space/Ctrl+A/Left-Right/Ctrl+N.