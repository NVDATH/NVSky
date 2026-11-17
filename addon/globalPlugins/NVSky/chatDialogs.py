"""
Chat dialogs for NVSky: start a chat, join a group by invite, manage a group (members, invite
link, join requests) and share a post to a chat. Opened from ChatWindow, the toolbar and the
Post action menu.
"""
import wx

import gui
import gui.nvdaControls
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import soundpack
from . import uiutil
from .userActions import UserActionMixin
from .userPicker import UserSearchMixin, take_checked
from .postText import _format_post_time


def _extract_join_code(text: str) -> str:
    # Invites are plain codes; a pasted link is reduced to its last path segment.
    text = text.strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


class JoinGroupDialog(uiutil.EscapeCloseDialog):
    """Join a group from an invite link or code, with a preview of the group."""

    PREVIEW_DEBOUNCE_MS = 800

    def __init__(self, parent, account, on_joined=None):
        self._account = account
        self._onJoined = on_joined
        self._previewedCode = None
        self._previewConvoId = None

        # Translators: Title of the Join a group dialog.
        super().__init__(parent, title=_("Join a group"), size=(420, 280))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the group join link/code field.
        codeLabel = wx.StaticText(self, label=_("Paste a group join &link or code:"))
        self.codeText = wx.TextCtrl(self)
        sizer.Add(codeLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.codeText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # Translators: Label above the read-only group info preview.
        previewLabel = wx.StaticText(self, label=_("&Group info:"))
        sizer.Add(previewLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        self.previewText = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(self.previewText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing to preview until a real code/link is typed --
        # hidden until _loadPreview actually has something to show.
        self._previewLabel = previewLabel
        previewLabel.Hide()
        self.previewText.Hide()

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to join the group previewed above.
        self.joinButton = wx.Button(self, label=_("&Join"))
        # Translators: Button to cancel and close a dialog.
        closeBtn = wx.Button(self, label=_("&Cancel"))
        actionRow.Add(self.joinButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)
        self.joinButton.Disable()

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self._debounceTimer = wx.Timer(self)

        self.codeText.Bind(wx.EVT_TEXT, self.onCodeTextChanged)
        self.Bind(wx.EVT_TIMER, self.onDebounceTimer, self._debounceTimer)
        self.joinButton.Bind(wx.EVT_BUTTON, self.onJoin)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        self.codeText.SetFocus()

    def onClose(self, evt):
        self._debounceTimer.Stop()
        super().onClose(evt)

    def onCodeTextChanged(self, evt):
        self.joinButton.Disable()
        self._previewedCode = None
        self._debounceTimer.Stop()
        if _extract_join_code(self.codeText.GetValue()):
            self._debounceTimer.StartOnce(self.PREVIEW_DEBOUNCE_MS)
        else:
            self.previewText.SetValue("")
            self._previewLabel.Hide()
            self.previewText.Hide()
            self.Layout()

    def onDebounceTimer(self, evt):
        self._loadPreview()

    def _loadPreview(self):
        code = _extract_join_code(self.codeText.GetValue())
        if not code:
            return
        self._previewLabel.Show()
        self.previewText.Show()
        self.Layout()
        # Translators: Status text while loading a group's info preview.
        loadingText = _("Loading group info...")
        self.previewText.SetValue(loadingText)
        nvdaUi.message(loadingText)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                previews = client.get_join_link_previews(atprotoClient, [code])
                error = None
            except Exception as e:
                previews = []
                error = str(e)
            wx.CallAfter(self._onPreviewDone, code, previews, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onPreviewDone(self, code, previews, error):
        if code != _extract_join_code(self.codeText.GetValue()):
            return
        if error:
            # Translators: Status text when loading a group preview fails. {} is the error message.
            text = _("Could not load preview: {}").format(error)
            self.previewText.SetValue(text)
            nvdaUi.message(text)
            return
        preview = previews[0] if previews else None
        if not preview or "name" not in preview:
            # Translators: Status text when the pasted join link/code is disabled or invalid.
            text = _("This link is disabled or invalid.")
            self.previewText.SetValue(text)
            nvdaUi.message(text)
            return
        owner = preview.get("owner") or {}
        # Translators: Group preview line: joining requires the group owner's approval.
        # Translators: Group preview line: joining is instant, no approval needed.
        approvalText = _("Requires owner approval") if preview.get("requireApproval") else _("Joins immediately")
        # Translators: Multi-line group preview text. Placeholders: group name, owner handle, member count, member limit, then the approval line above.
        text = _(
            "Group: {}\n"
            "Owner: @{}\n"
            "Members: {}/{}\n"
            "{}"
        ).format(
            # Translators: Fallback group name when the server didn't provide one.
            preview.get("name") or _("Group"),
            # Translators: Fallback owner handle when the server didn't provide one.
            owner.get("handle", _("unknown")),
            preview.get("memberCount", "?"),
            preview.get("memberLimit", "?"),
            approvalText,
        )
        self.previewText.SetValue(text)
        self.previewText.SetFocus()
        nvdaUi.message(text)
        self._previewedCode = code
        self._previewConvoId = preview.get("convoId")
        self.joinButton.Enable()

    def onJoin(self, evt):
        if not self._previewedCode:
            return
        self.joinButton.Disable()
        # Translators: Announced while a group-join request is in progress.
        nvdaUi.message(_("Joining..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                result = client.request_join_group(atprotoClient, self._previewedCode)
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                result = None
                error = str(e)
            wx.CallAfter(self._onJoinDone, result, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onJoinDone(self, result, error):
        if error:
            self.joinButton.Enable()
            # Translators: Announced when joining a group fails. {} is the error message.
            nvdaUi.message(_("Could not join: {}").format(error))
            return
        status = (result or {}).get("status")
        if status == "joined":
            # Translators: Announced after successfully joining a group.
            nvdaUi.message(_("Joined the group."))
            if self._onJoined and self._previewConvoId:
                self._onJoined(self._previewConvoId)
            self.Close()
            return
        # Pending approval -- keep the dialog open with a way to back
        # out, instead of closing on a request the user might want to
        # cancel (no other UI surfaces a pending outgoing request at all).
        # Translators: Announced when a join request needs owner approval. {} is the request status.
        nvdaUi.message(_("Join request sent (status: {}). You can withdraw it below.").format(status or "pending"))
        # Translators: Appended to the group preview text while a join request is pending.
        self.previewText.SetValue(self.previewText.GetValue() + "\n" + _("Request pending approval."))
        self.joinButton.Destroy()
        # Translators: Button to withdraw a pending group join request.
        self.withdrawButton = wx.Button(self, label=_("&Withdraw request"))
        self.GetSizer().Add(self.withdrawButton, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)
        self.withdrawButton.Bind(wx.EVT_BUTTON, self.onWithdraw)
        self.Layout()
        self.withdrawButton.SetFocus()

    def onWithdraw(self, evt):
        self.withdrawButton.Disable()
        # Translators: Announced while withdrawing a pending group join request.
        nvdaUi.message(_("Withdrawing request..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.withdraw_join_request(atprotoClient, self._previewConvoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onWithdrawDone, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onWithdrawDone(self, error):
        if error:
            self.withdrawButton.Enable()
            # Translators: Announced when withdrawing a group join request fails. {} is the error message.
            nvdaUi.message(_("Could not withdraw request: {}").format(error))
            return
        # Translators: Announced after successfully withdrawing a group join request.
        nvdaUi.message(_("Request withdrawn."))
        self.Close()


class NewChatDialog(UserSearchMixin, uiutil.EscapeCloseDialog):
    """
    Start a new 1:1 chat, or a group chat when more than one recipient is queued or a group name is
    set. Search results and queued recipients are check lists, like ManageGroupMembersDialog.

    A first message is required: both calls only create the conversation record, and an empty
    conversation isn't listed anywhere until a message is sent.
    """

    def __init__(self, parent, account, on_started=None):
        self._account = account
        self._recipients = []  # list of {"did", "handle", "display_name"}
        self._onStarted = on_started

        # Translators: Title of the Start a new chat dialog.
        super().__init__(parent, title=_("Start a new chat"), size=(440, 560))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the user search field in the new-chat dialog.
        userLabel = wx.StaticText(self, label=_("&Search for a user by handle or name:"))
        self.userSearchText = wx.TextCtrl(self)
        sizer.Add(userLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.userSearchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # Translators: Label above the user search results checklist.
        self.searchResultsLabel = wx.StaticText(self, label=_("Search results:"))
        self.searchResultsList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.searchResultsLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.searchResultsList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Translators: Button to add checked search results as recipients.
        self.addRecipientButton = wx.Button(self, label=_("Add chec&ked"))
        sizer.Add(self.addRecipientButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.searchResultsLabel.Hide()
        self.searchResultsList.Hide()
        self.addRecipientButton.Hide()

        # Translators: Label above the queued-recipients checklist.
        self.recipientsLabel = wx.StaticText(self, label=_("&Recipients:"))
        self.recipientsList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.recipientsLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.recipientsList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        # Translators: Button to remove checked recipients from the queue.
        self.removeRecipientButton = wx.Button(self, label=_("Re&move checked"))
        sizer.Add(self.removeRecipientButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing to show/remove until at least one recipient has been
        # added -- an empty check-list box otherwise has odd, screen-
        # reader-confusing focus/read behavior of its own.
        self.recipientsLabel.Hide()
        self.recipientsList.Hide()
        self.removeRecipientButton.Hide()

        groupNameLabel = wx.StaticText(
            self,
            # Translators: Label for the optional group name field. Setting it forces a group chat even with one recipient.
            label=_("&Group name (optional -- set this to force a group chat even with a single recipient):"),
        )
        self.groupNameText = wx.TextCtrl(self)
        sizer.Add(groupNameLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.groupNameText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        # Translators: Label for the first-message field, required to actually create the conversation.
        messageLabel = wx.StaticText(self, label=_("&First message:"))
        self.messageText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(messageLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        sizer.Add(self.messageText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to start the new chat.
        self.startButton = wx.Button(self, label=_("St&art chat"))
        # Translators: Button to open the join-a-group dialog instead of starting a new chat.
        self.joinGroupButton = wx.Button(self, label=_("&Join group..."))
        # Translators: Button to cancel and close a dialog.
        closeBtn = wx.Button(self, label=_("&Cancel"))
        actionRow.Add(self.startButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.joinGroupButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self._bindUserSearch(
            self.userSearchText, self.searchResultsLabel, self.searchResultsList, self.addRecipientButton
        )
        self.addRecipientButton.Bind(wx.EVT_BUTTON, self.onAddRecipient)
        self.removeRecipientButton.Bind(wx.EVT_BUTTON, self.onRemoveRecipient)
        self.startButton.Bind(wx.EVT_BUTTON, self.onStartChat)
        self.joinGroupButton.Bind(wx.EVT_BUTTON, self.onJoinGroup)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        self.userSearchText.SetFocus()

    def _excludedDids(self):
        # Already-added recipients don't need to show up again.
        return {r["did"] for r in self._recipients}

    def onAddRecipient(self, evt):
        toAdd = self._takeCheckedSuggestions(
            # Translators: Announced when adding recipients with nothing checked in the search results.
            _("No search results checked.")
        )
        if not toAdd:
            return
        wasEmpty = not self._recipients
        self._recipients.extend(toAdd)
        self.recipientsList.Set(
            [uiutil.user_choice_label(r) for r in self._recipients]
        )
        self.recipientsList.CheckedItems = []
        if self._recipients:
            self.recipientsList.SetSelection(0)
        self._clearUserSearch()
        if wasEmpty:
            self.recipientsLabel.Show()
            self.recipientsList.Show()
            self.removeRecipientButton.Show()
        self.Layout()
        # Translators: Announced after adding recipients. First {} is how many were added, second {} is the new total.
        nvdaUi.message(_("Added {}. {} recipient(s) total.").format(len(toAdd), len(self._recipients)))

    def onRemoveRecipient(self, evt):
        checked = take_checked(
            self.recipientsList, self._recipients, self.removeRecipientButton,
            # Translators: Announced when removing recipients with nothing checked.
            _("No recipients checked."),
        )
        if not checked:
            return
        toRemove = {r["did"] for r in checked}
        removedCount = len(toRemove)
        self._recipients = [r for r in self._recipients if r["did"] not in toRemove]
        self.recipientsList.Set(
            [uiutil.user_choice_label(r) for r in self._recipients]
        )
        self.recipientsList.CheckedItems = []
        if not self._recipients:
            self.recipientsLabel.Hide()
            self.recipientsList.Hide()
            self.removeRecipientButton.Hide()
            self.Layout()
            self.userSearchText.SetFocus()
        # Translators: Announced after removing recipients. First {} is how many were removed, second {} is how many remain.
        nvdaUi.message(_("Removed {}. {} recipient(s) left.").format(removedCount, len(self._recipients)))

    def onStartChat(self, evt):
        if not self._recipients:
            # Translators: Announced when starting a chat with no recipient queued.
            nvdaUi.message(_("Add at least one recipient first."))
            return
        text = self.messageText.GetValue().strip()
        if not text:
            # Translators: Announced when starting a chat with an empty first message.
            nvdaUi.message(_("Type a first message before starting the chat."))
            return
        groupName = self.groupNameText.GetValue().strip() or None
        recipients = list(self._recipients)
        # A group name forces a group even with a single recipient.
        isGroup = len(recipients) > 1 or bool(groupName)
        nvdaUi.message(
            # Translators: Announced while creating a new group chat.
            _("Starting group chat, please wait...") if isGroup
            # Translators: Announced while starting a new 1:1 chat. {} is the recipient's handle.
            else _("Starting chat with @{}, please wait...").format(recipients[0]["handle"])
        )
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if isGroup:
                    convo = client.create_group(atprotoClient, [r["did"] for r in recipients], groupName)
                else:
                    convo = client.get_or_create_convo_for_member(atprotoClient, recipients[0]["did"])
                convoId = convo.get("id") if convo else None
                if not convoId:
                    wx.CallAfter(self._onStartChatDone, None, "no conversation was returned")
                    return
                client.send_message(atprotoClient, convoId, text)
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                convo = None
                error = str(e)
            wx.CallAfter(self._onStartChatDone, convo, error)

        uiutil.start_worker(worker, progress=False)

    @uiutil.safe_ui_callback
    def _onStartChatDone(self, convo, error):
        soundpack.stop_progress()
        if error:
            log.error(f"NVSky: start new chat failed: {error}")
            # Translators: Announced when starting a new chat fails. {} is the error message.
            nvdaUi.message(_("Could not start chat: {}").format(error))
            return
        if not convo or not convo.get("id"):
            log.error(f"NVSky: start chat returned no usable convo: {convo!r}")
            # Translators: Announced when the server didn't return a usable conversation.
            nvdaUi.message(_("Could not start chat: no conversation was returned."))
            return
        gui.mainFrame.postPopup()
        self.Destroy()
        # Translators: Announced after successfully starting a new chat.
        nvdaUi.message(_("Chat started."))
        if self._onStarted:
            self._onStarted(convo.get("id"))

    def onJoinGroup(self, evt):
        onJoined = self._onStarted
        self.Close()
        JoinGroupDialog(gui.mainFrame, self._account, on_joined=onJoined).Show()


class JoinRequestsDialog(uiutil.EscapeCloseDialog):
    """Pending join requests of a group, to approve or reject."""

    def __init__(self, parent, account, convo):
        self._account = account
        self._convo = convo
        self._requests = []

        # Translators: Title of the Join requests dialog.
        super().__init__(parent, title=_("Join requests"), size=(420, 320))

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.requestsList = wx.ListCtrl(self, style=wx.LC_REPORT)
        # Translators: Column header for the requester's handle in the join requests list.
        self.requestsList.InsertColumn(0, _("Requester"), width=200)
        # Translators: Column header for when the join request was made.
        self.requestsList.InsertColumn(1, _("Requested"), width=160)
        sizer.Add(self.requestsList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to approve the selected join request.
        self.approveButton = wx.Button(self, label=_("&Approve"))
        # Translators: Button to reject the selected join request.
        self.rejectButton = wx.Button(self, label=_("&Reject"))
        # Translators: Button to close a dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
        actionRow.Add(self.approveButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.rejectButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.approveButton.Bind(wx.EVT_BUTTON, lambda e: self._resolveSelected(True))
        self.rejectButton.Bind(wx.EVT_BUTTON, lambda e: self._resolveSelected(False))
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        self._loadRequests()

    def _loadRequests(self):
        # Translators: Announced while loading join requests.
        nvdaUi.message(_("Loading join requests..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                requests_ = client.list_join_requests(atprotoClient, self._convo["convo_id"])
                client.mark_join_requests_read(atprotoClient, self._convo["convo_id"])
                error = None
            except Exception as e:
                requests_ = []
                error = str(e)
            wx.CallAfter(self._onLoadDone, requests_, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onLoadDone(self, requests_, error):
        if error:
            # Translators: Announced when loading join requests fails. {} is the error message.
            nvdaUi.message(_("Could not load join requests: {}").format(error))
            return
        self._requests = requests_
        self.requestsList.DeleteAllItems()
        for i, r in enumerate(requests_):
            requestedBy = r.get("requestedBy") or {}
            self.requestsList.InsertItem(i, f'@{requestedBy.get("handle", "unknown")}')
            self.requestsList.SetItem(i, 1, _format_post_time(r.get("requestedAt")))
        if requests_:
            self.requestsList.Focus(0)
            self.requestsList.Select(0)
        # Translators: Announced after loading join requests. {} is the count.
        nvdaUi.message(_("{} join request(s).").format(len(requests_)))

    def _resolveSelected(self, approve):
        index = self.requestsList.GetFocusedItem()
        if index == -1 or index >= len(self._requests):
            # Translators: Announced when approving/rejecting a join request with none selected.
            nvdaUi.message(_("No request selected."))
            return
        member = (self._requests[index].get("requestedBy") or {}).get("did")
        if not member:
            return
        self.approveButton.Disable()
        self.rejectButton.Disable()
        # Translators: Announced while approving a join request.
        # Translators: Announced while rejecting a join request.
        nvdaUi.message(_("Approving...") if approve else _("Rejecting..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if approve:
                    client.approve_join_request(atprotoClient, self._convo["convo_id"], member)
                else:
                    client.reject_join_request(atprotoClient, self._convo["convo_id"], member)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onResolveDone, index, approve, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onResolveDone(self, index, approve, error):
        self.approveButton.Enable()
        self.rejectButton.Enable()
        if error:
            # Translators: Announced when approving a join request fails. {} is the error message.
            # Translators: Announced when rejecting a join request fails. {} is the error message.
            message = _("Could not approve: {}").format(error) if approve else _("Could not reject: {}").format(error)
            nvdaUi.message(message)
            return
        # Translators: Announced after approving a join request.
        # Translators: Announced after rejecting a join request.
        nvdaUi.message(_("Approved.") if approve else _("Rejected."))
        if 0 <= index < len(self._requests):
            del self._requests[index]
        self.requestsList.DeleteItem(index)
        if self.requestsList.GetItemCount():
            newIndex = min(index, self.requestsList.GetItemCount() - 1)
            self.requestsList.Focus(newIndex)
            self.requestsList.Select(newIndex)


class InviteLinkDialog(uiutil.EscapeCloseDialog):
    """
    Create, edit, enable or disable a group's invite link. Two sibling panels are toggled with
    Show/Hide: rebuilding controls in place broke wx's tab order.
    """

    JOIN_RULE_OPTIONS = [
        # Translators: Invite-link join rule: anyone with the link joins immediately.
        (_("Anyone can join instantly"), "anyone", False),
        # Translators: Invite-link join rule: anyone with the link must request approval.
        (_("Anyone can request to join"), "anyone", True),
        # Translators: Invite-link join rule: only people you follow join immediately.
        (_("People I follow can join instantly"), "followedByOwner", False),
        # Translators: Invite-link join rule: only people you follow can request approval.
        (_("People I follow can request to join"), "followedByOwner", True),
    ]
    # Group invites have no shareable URL, only the code (pasted into the Join group dialog).

    def __init__(self, parent, account, convo):
        self._account = account
        self._convo = convo
        self._joinLink = None

        # Translators: Title of the Invite link dialog.
        super().__init__(parent, title=_("Invite link"), size=(420, 320))
        outerSizer = wx.BoxSizer(wx.VERTICAL)

        self.setupPanel = wx.Panel(self)
        self._buildSetupPanel(self.setupPanel)
        outerSizer.Add(self.setupPanel, proportion=1, flag=wx.EXPAND)

        self.resultPanel = wx.Panel(self)
        self._buildResultPanel(self.resultPanel)
        outerSizer.Add(self.resultPanel, proportion=1, flag=wx.EXPAND)
        self.resultPanel.Hide()
        # Disable too: Hide() alone leaves the panel's children in the tab order.
        self.resultPanel.Enable(False)

        self.SetSizer(outerSizer)
        self.CentreOnScreen()

        self.ruleRadio.SetFocus()

    def _buildSetupPanel(self, panel):
        sizer = wx.BoxSizer(wx.VERTICAL)
        infoLabel = wx.StaticText(
            panel,
            # Translators: Explanatory text in the invite-link setup panel.
            label=_("An invite link lets people join this group without being added directly. "
                    "Your name, avatar, the group name, and member count are visible to anyone with the link. "
                    "There's no way to check whether one already exists -- Save creates a new one, or updates "
                    "the existing one if it turns out there already is one."),
        )
        sizer.Add(infoLabel, flag=wx.EXPAND | wx.ALL, border=10)

        self.ruleRadio = wx.RadioBox(
            # Translators: Label for the "who can join via this link" radio group.
            panel, label=_("Who can join"), choices=[o[0] for o in self.JOIN_RULE_OPTIONS], style=wx.RA_SPECIFY_ROWS
        )
        sizer.Add(self.ruleRadio, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to create or update the invite link.
        self.generateButton = wx.Button(panel, label=_("&Save"))
        # Translators: Initial label before a link exists -- overwritten dynamically by _showSetupPanel afterward.
        self.setupCloseButton = wx.Button(panel, label=_("&Cancel"))
        actionRow.Add(self.generateButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.setupCloseButton)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        panel.SetSizer(sizer)
        self.generateButton.Bind(wx.EVT_BUTTON, self.onGenerate)
        self.setupCloseButton.Bind(wx.EVT_BUTTON, self.onSetupCancel)

    def onSetupCancel(self, evt):
        if self._joinLink:
            self._showResultPanel()
        else:
            self.Close()

    def _buildResultPanel(self, panel):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label for the read-only invite code field.
        urlLabel = wx.StaticText(panel, label=_("Invite code (share this text with people you want to invite):"))
        sizer.Add(urlLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.urlText = wx.TextCtrl(panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
        sizer.Add(self.urlText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.statusLabel = wx.StaticText(panel, label="")
        sizer.Add(self.statusLabel, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to copy the invite code to the clipboard.
        self.copyButton = wx.Button(panel, label=_("&Copy"))
        # Translators: Button to change the invite link's join rule.
        self.editButton = wx.Button(panel, label=_("&Edit permissions..."))
        # Translators: Button to disable the currently-enabled invite link.
        self.toggleButton = wx.Button(panel, label=_("&Disable link"))
        # Translators: Button to close the invite-link dialog. Mnemonic on "l" since Copy already claims "C" in this same panel.
        self.resultCloseButton = wx.Button(panel, label=_("C&lose"))
        actionRow.Add(self.copyButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.editButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.toggleButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.resultCloseButton)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        panel.SetSizer(sizer)
        self.copyButton.Bind(wx.EVT_BUTTON, lambda e: self._copyUrl())
        self.editButton.Bind(wx.EVT_BUTTON, lambda e: self._showSetupPanel())
        self.toggleButton.Bind(wx.EVT_BUTTON, self.onToggleEnabled)
        self.resultCloseButton.Bind(wx.EVT_BUTTON, lambda e: self.Close())

    def _ruleIndexFor(self, joinLink):
        return next(
            (i for i, o in enumerate(self.JOIN_RULE_OPTIONS)
             if o[1] == joinLink.get("joinRule") and o[2] == joinLink.get("requireApproval")),
            0,
        )

    def _showSetupPanel(self):
        if self._joinLink:
            self.ruleRadio.SetSelection(self._ruleIndexFor(self._joinLink))
        # Translators: Button label when going back to setup from an already-generated link (cancels editing).
        # Translators: Button label when this is the only screen shown yet (closes the whole dialog).
        self.setupCloseButton.SetLabel(_("&Cancel") if self._joinLink else _("&Close"))
        self.resultPanel.Hide()
        self.resultPanel.Enable(False)
        self.setupPanel.Show()
        self.setupPanel.Enable(True)
        self.Layout()
        # Force a full repaint -- Layout() alone can leave stale pixels
        # from the sibling panel that just got hidden, since both
        # occupy the exact same rect (proportion=1, EXPAND).
        self.Refresh()
        self.ruleRadio.SetFocus()

    def _showResultPanel(self):
        self.urlText.SetValue(self._joinLink.get("code", ""))
        ruleIndex = self._ruleIndexFor(self._joinLink)
        enabled = self._joinLink.get("enabledStatus") == "enabled"
        # Translators: Word shown when the invite link is currently enabled.
        # Translators: Word shown when the invite link is currently disabled.
        statusState = _("enabled") if enabled else _("disabled")
        # Translators: Status line showing the current join rule and enabled/disabled state. First {} is the rule text, second {} is "enabled"/"disabled".
        self.statusLabel.SetLabel(_("{} -- {}").format(self.JOIN_RULE_OPTIONS[ruleIndex][0], statusState))
        self.toggleButton.SetLabel(_("&Disable link") if enabled else _("&Enable link"))
        self.setupPanel.Hide()
        self.setupPanel.Enable(False)
        self.resultPanel.Show()
        self.resultPanel.Enable(True)
        self.Layout()
        # Force a full repaint -- Layout() alone can leave stale pixels
        # from the sibling panel that just got hidden, since both
        # occupy the exact same rect (proportion=1, EXPAND).
        self.Refresh()
        self.urlText.SetFocus()
        self.urlText.SelectAll()

    def _copyUrl(self):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.urlText.GetValue()))
            wx.TheClipboard.Close()
        # Translators: Announced after copying the invite code to the clipboard.
        nvdaUi.message(_("Invite code copied."))

    def onGenerate(self, evt):
        _label, joinRule, requireApproval = self.JOIN_RULE_OPTIONS[self.ruleRadio.GetSelection()]
        self.generateButton.Disable()
        # Translators: Announced while creating or updating the invite link.
        nvdaUi.message(_("Saving invite link..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                try:
                    if self._joinLink:
                        joinLink = client.edit_join_link(atprotoClient, self._convo["convo_id"], joinRule, requireApproval)
                    else:
                        joinLink = client.create_join_link(atprotoClient, self._convo["convo_id"], joinRule, requireApproval)
                except Exception as e:
                    if "EnabledJoinLinkAlreadyExists" in str(e):
                        joinLink = client.edit_join_link(atprotoClient, self._convo["convo_id"], joinRule, requireApproval)
                    else:
                        raise
                error = None
            except Exception as e:
                joinLink = None
                error = str(e)
            wx.CallAfter(self._onGenerateDone, joinLink, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onGenerateDone(self, joinLink, error):
        self.generateButton.Enable()
        if error:
            # Translators: Announced when saving the invite link fails. {} is the error message.
            nvdaUi.message(_("Could not save invite link: {}").format(error))
            return
        if not joinLink:
            # Translators: Announced when the server response for the invite link is unexpectedly empty.
            nvdaUi.message(_("The server didn't return link details."))
            return
        self._joinLink = joinLink
        # Translators: Announced after the invite link is created/updated.
        nvdaUi.message(_("Invite link ready."))
        self._showResultPanel()

    def onToggleEnabled(self, evt):
        enabling = self._joinLink.get("enabledStatus") != "enabled"
        # Translators: Announced while enabling the invite link.
        # Translators: Announced while disabling the invite link.
        nvdaUi.message(_("Enabling invite link...") if enabling else _("Disabling invite link..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if enabling:
                    joinLink = client.enable_join_link(atprotoClient, self._convo["convo_id"])
                else:
                    client.disable_join_link(atprotoClient, self._convo["convo_id"])
                    joinLink = dict(self._joinLink, enabledStatus="disabled")
                error = None
            except Exception as e:
                joinLink = None
                error = str(e)
            wx.CallAfter(self._onToggleDone, joinLink, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onToggleDone(self, joinLink, error):
        if error:
            # Translators: Announced when enabling/disabling the invite link fails. {} is the error message.
            nvdaUi.message(_("Could not update invite link: {}").format(error))
            return
        if not joinLink:
            # Translators: Announced when the server response for the invite link is unexpectedly empty.
            nvdaUi.message(_("The server didn't return link details."))
            return
        self._joinLink = joinLink
        # Translators: Announced after enabling/disabling the invite link.
        nvdaUi.message(_("Invite link updated."))
        self._showResultPanel()


class ManageGroupMembersDialog(UserActionMixin, UserSearchMixin, uiutil.EscapeCloseDialog):
    """
    Add or remove members of a group conversation. Both lists are check-list boxes, so several
    members can be added or removed at once (same UX as the Lists member dialog). The per-member
    context menu reuses UserActionMixin plus Message/Remove.
    """

    def __init__(self, parent, account, convo, members):
        self._account = account
        self._convo = convo
        self._members = list(members)

        # Translators: Fallback group name shown when the group has none set. Used as "Manage members - {}".
        title = convo.get("group_name") or _("Group")
        # Translators: Title of the manage-group-members dialog. {} is the group name.
        super().__init__(parent, title=_("Manage members - {}").format(title), size=(460, 520))

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label above the current group members checklist.
        memberLabel = wx.StaticText(self, label=_("Current members:"))
        sizer.Add(memberLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.memberList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        self._renderMembers()
        sizer.Add(self.memberList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Translators: Button to remove checked members from the group.
        self.removeButton = wx.Button(self, label=_("&Remove checked"))
        sizer.Add(self.removeButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.removeButton.Show(bool(self._members))

        # Translators: Label for the add-member search field.
        addLabel = wx.StaticText(self, label=_("Add &member (type a handle or name to search):"))
        sizer.Add(addLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.searchText = wx.TextCtrl(self)
        sizer.Add(self.searchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        # Translators: Label above the member-search results checklist.
        self.suggestLabel = wx.StaticText(self, label=_("Search results:"))
        self.suggestionList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.suggestLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.suggestionList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Translators: Button to add checked search results as group members.
        self.addButton = wx.Button(self, label=_("&Add checked"))
        sizer.Add(self.addButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.suggestLabel.Hide()
        self.suggestionList.Hide()
        self.addButton.Hide()

        # Translators: Button to close the manage-group-members dialog.
        closeBtn = wx.Button(self, label=_("&Close"))
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self._bindUserSearch(self.searchText, self.suggestLabel, self.suggestionList, self.addButton)
        self.addButton.Bind(wx.EVT_BUTTON, self.onAddSuggestion)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        self.memberList.SetFocus()

    def _renderMembers(self):
        self.memberList.Set([uiutil.user_choice_label(m) for m in self._members])
        self.memberList.CheckedItems = []
        if self._members:
            self.memberList.SetSelection(0)

    def onRemove(self, evt):
        toRemove = take_checked(
            self.memberList, self._members, self.removeButton,
            # Translators: Announced when removing group members with nothing checked.
            _("No members checked."),
        )
        if not toRemove:
            return

        names = ", ".join(f'@{m["handle"]}' for m in toRemove)
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for removing group members. {} is a comma-separated list of handles.
            _("Remove {} from this group?").format(names),
            # Translators: Title of the confirm-remove-group-members dialog.
            _("Confirm remove"), wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        # Optimistic: update the UI immediately instead of waiting on
        # the server round-trip, roll back if the actual request fails.
        removedDids = {m["did"] for m in toRemove}
        self._members = [m for m in self._members if m["did"] not in removedDids]
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self.Layout()
        # Translators: Announced while removing group members. {} is the count.
        nvdaUi.message(_("Removing {} member(s)...").format(len(toRemove)))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_group_members(atprotoClient, self._convo["convo_id"], list(removedDids))
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, toRemove, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, removed, error):
        if error:
            # Roll back the optimistic removal.
            self._members.extend(removed)
            self._renderMembers()
            self.removeButton.Show(bool(self._members))
            self.Layout()
            # Translators: Announced when removing group members fails. {} is the error message.
            nvdaUi.message(_("Could not remove member(s): {}").format(error))
            return
        # Translators: Announced after removing group members. {} is the count.
        nvdaUi.message(_("Removed {} member(s).").format(len(removed)))

    def _excludedDids(self):
        return {m["did"] for m in self._members}

    def onAddSuggestion(self, evt):
        toAdd = self._takeCheckedSuggestions(
            # Translators: Announced when adding group members with nothing checked in search results.
            _("No suggestions checked.")
        )
        if not toAdd:
            return

        # Optimistic here too, for consistency with member removal.
        self._members.extend(toAdd)
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self._clearUserSearch()
        # Translators: Announced while adding group members. {} is the count.
        nvdaUi.message(_("Adding {} member(s)...").format(len(toAdd)))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.add_group_members(atprotoClient, self._convo["convo_id"], [u["did"] for u in toAdd])
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onAddDone, toAdd, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onAddDone(self, added, error):
        if error:
            # Roll back.
            addedDids = {u["did"] for u in added}
            self._members = [m for m in self._members if m["did"] not in addedDids]
            self._renderMembers()
            self.removeButton.Show(bool(self._members))
            self.Layout()
            # Translators: Announced when adding group members fails. {} is the error message.
            nvdaUi.message(_("Could not add member(s): {}").format(error))
            return
        # Translators: Announced after adding group members. {} is the count.
        nvdaUi.message(_("Added {} member(s).").format(len(added)))


class ShareToChatDialog(uiutil.EscapeCloseDialog):
    """
    Post action's "Share to chat..." -- picks an existing accepted
    conversation and sends the post as an app.bsky.embed.record embed
    (see client.send_message's embed_ref param). No "start a new
    conversation from here" option -- NewChatDialog already covers
    that, keeping this dialog to a single, simple job.
    """

    def __init__(self, parent, account, post):
        self._account = account
        self._post = post
        self._convos = []

        # Translators: Title of the share-post-to-chat dialog.
        super().__init__(parent, title=_("Share to chat"), size=(420, 400))

        sizer = wx.BoxSizer(wx.VERTICAL)

        previewText = (post.get("text") or "")[:60]
        if len(post.get("text") or "") > 60:
            previewText += "..."
        # Translators: Preview line showing which post is being shared. {} is a truncated preview of the post text.
        previewLabel = wx.StaticText(self, label=_("Sharing: {}").format(previewText))
        sizer.Add(previewLabel, flag=wx.EXPAND | wx.ALL, border=10)

        # Translators: Label above the conversation list to share a post to. Mnemonic on "to" since Send is taken by the button below.
        listLabel = wx.StaticText(self, label=_("Send &to:"))
        sizer.Add(listLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.convoList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for the conversation list in the share-to-chat dialog.
        self.convoList.InsertColumn(0, _("Conversation"), width=380)
        sizer.Add(self.convoList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Translators: Label for the optional message field in the share-to-chat dialog.
        messageLabel = wx.StaticText(self, label=_("&Message (optional):"))
        sizer.Add(messageLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.messageText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(self.messageText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to send the shared post.
        self.sendButton = wx.Button(self, label=_("&Send"))
        closeBtn = wx.Button(self, label=_("&Cancel"))
        buttonRow.Add(self.sendButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(closeBtn)
        sizer.Add(buttonRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.sendButton.Bind(wx.EVT_BUTTON, self.onSend)
        self.convoList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onSend)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        self._loadConvos()

    def _loadConvos(self):
        allConvos = db.get_convos(self._account["id"])
        self._convos = [c for c in allConvos if c.get("status") != "request"]
        self.convoList.DeleteAllItems()
        for i, convo in enumerate(self._convos):
            members = db.get_convo_members(self._account["id"], convo["convo_id"])
            self.convoList.InsertItem(i, db.describe_convo_from_members(convo, members))
        if self._convos:
            self.convoList.Focus(0)
            self.convoList.Select(0)
            self.convoList.SetFocus()
        else:
            # Translators: Announced when there are no conversations to share a post to.
            nvdaUi.message(_("No conversations to share to yet -- start one from the Chat tab first."))

    def onSend(self, evt):
        index = self.convoList.GetFocusedItem()
        if not (0 <= index < len(self._convos)):
            # Translators: Announced when sharing a post to chat with no conversation selected.
            nvdaUi.message(_("No conversation selected."))
            return
        convo = self._convos[index]
        convoId = convo["convo_id"]
        embedRef = {"uri": self._post["uri"], "cid": self._post["cid"]}
        text = self.messageText.GetValue().strip()

        self.sendButton.Disable()
        # Translators: Announced while sharing a post to a chat conversation.
        nvdaUi.message(_("Sharing, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.send_message(atprotoClient, convoId, text, embed_ref=embedRef)
                client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onSendDone, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onSendDone(self, error):
        self.sendButton.Enable()
        if error:
            log.error(f"NVSky: share to chat failed: {error}")
            # Translators: Announced when sharing a post to chat fails. {} is the error message.
            nvdaUi.message(_("Could not share: {}").format(error))
            return
        # Translators: Announced after successfully sharing a post to chat.
        nvdaUi.message(_("Shared."))
        self.Close()
