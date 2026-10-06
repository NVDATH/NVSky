"""
UserActionMixin + UserListMixin for NVSky: the shared "act on a user"
menu (Alt+U) used by every tab or dialog that shows posts or users.

feedWindow is imported as a module (not by name) because it imports this
file; its helpers are only touched inside method bodies.
"""
import threading
import webbrowser

import wx

import gui
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import soundpack
from . import uiutil
from . import feedWindow


class UserActionMixin:
    """
    Shared "act on a user" menu + implementations, mixed into any dialog
    that needs to offer user actions -- FeedWindow's user action menu,
    UserListDialog, and ProfileDialog (via Alt+U) all share this single
    implementation instead of drifting copies.
    """

    def _addMenuItem(self, menu, label, callback):
        item = menu.Append(wx.ID_ANY, label)
        self.Bind(wx.EVT_MENU, lambda evt: callback(), item)
        return item

    def _populateUserActionMenu(self, menu, did, handle, display_name=None):
        browseMenu = wx.Menu()
        # Translators: Submenu item under "View...", opens the user's profile.
        self._addMenuItem(browseMenu, _("&Profile..."), lambda: self.viewProfile(did))
        # Translators: Submenu item under "View...", opens the user's timeline.
        self._addMenuItem(browseMenu, _("&Timeline..."), lambda: self.showTimeline(did, handle, display_name))
        # Translators: Submenu item under "View...", opens the user's followers list.
        self._addMenuItem(browseMenu, _("&Followers..."), lambda: self.showFollowers(did, handle, display_name))
        # Translators: Submenu item under "View...", opens who the user follows.
        self._addMenuItem(browseMenu, _("Follo&wing..."), lambda: self.showFollowing(did, handle, display_name))
        # Translators: Submenu item under "View...", opens followers you both share.
        self._addMenuItem(browseMenu, _("Kn&own followers..."), lambda: self.showKnownFollowers(did, handle, display_name))
        # Translators: Submenu item under "View...", opens lists the user is on.
        self._addMenuItem(browseMenu, _("&Lists..."), lambda: self.showUserLists(did, handle, display_name))
        # Translators: User action submenu label.
        menu.AppendSubMenu(browseMenu, _("&View..."))
        # No mnemonic on purpose -- this is destructive-adjacent (can't
        # be undone once a message is actually sent) and sits right
        # next to Follow/Timeline in the menu, easy to hit by accident
        # with a stray Alt+S.
        # Translators: User action menu item.
        self._addMenuItem(menu, _("Start chat..."), lambda: self.startChat(did, handle))
        menu.AppendSeparator()

        # Cached relation state gives truthful labels and no confirm dialog;
        # otherwise fall back to the ambiguous toggle (network check + confirm).
        cached = db.get_author(did)
        if cached is not None:
            followingUri = cached.get("viewer_following")
            isMuted = bool(cached.get("viewer_muted"))
            blockingUri = cached.get("viewer_blocking")
            isSubscribed = bool(cached.get("viewer_activity_subscription"))
            # Translators: User action menu item (already following).
            # Translators: User action menu item (not yet following).
            self._addMenuItem(menu, _("Un&follow") if followingUri else _("&Follow"),
                               lambda: self._toggleRelationCached(did, handle, "follow", followingUri))
            # Translators: User action menu item (already muted).
            # Translators: User action menu item (not yet muted).
            self._addMenuItem(menu, _("Un&mute") if isMuted else _("Mu&te"),
                               lambda: self._toggleRelationCached(did, handle, "mute", isMuted))
            # Translators: User action menu item (already blocked).
            # Translators: User action menu item (not yet blocked).
            self._addMenuItem(menu, _("Un&block") if blockingUri else _("&Block"),
                               lambda: self._toggleRelationCached(did, handle, "block", blockingUri))
            # Translators: User action menu item (already subscribed to this user's post notifications).
            # Translators: User action menu item (not yet subscribed to this user's post notifications).
            self._addMenuItem(menu, _("Un&subscribe from posts") if isSubscribed else _("&Subscribe to posts"),
                               lambda: self._toggleRelationCached(did, handle, "activity_sub", isSubscribed))
        else:
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("&Follow / Unfollow"), lambda: self._toggleRelation(did, handle, "follow"))
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("Mu&te / Unmute"), lambda: self._toggleRelation(did, handle, "mute"))
            # Translators: User action menu item, ambiguous fallback when relation state isn't cached.
            self._addMenuItem(menu, _("&Block / Unblock"), lambda: self._toggleRelation(did, handle, "block"))
        menu.AppendSeparator()

        # Translators: User action menu item.
        self._addMenuItem(menu, _("&Add to list..."), lambda: self.addToList(did, handle, display_name))
        menu.AppendSeparator()

        copyMenu = wx.Menu()
        # Translators: Submenu item under "Copy...", copies the user's bsky.app profile URL.
        self._addMenuItem(copyMenu, _("Copy &profile URL"), lambda: self.copyProfileUrl(did, handle))
        # Translators: Submenu item under "Copy...", opens the user's profile in a web browser.
        self._addMenuItem(copyMenu, _("&Open on bsky.app"),
                           lambda: webbrowser.open(f"https://bsky.app/profile/{handle or did}"))
        # Translators: User action submenu label.
        menu.AppendSubMenu(copyMenu, _("&Copy..."))
        menu.AppendSeparator()

        # Translators: User action menu item.
        self._addMenuItem(menu, _("&Report user..."), lambda: self._reportActor(did, handle))

    def showUserActionMenu(self, did, handle, display_name=None):
        menu = wx.Menu()
        self._populateUserActionMenu(menu, did, handle, display_name)
        self.PopupMenu(menu)
        menu.Destroy()

    def showTimeline(self, did, handle, display_name=None):
        # Opens or focuses the user's timeline tab (needs the real MainWindow).
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "user_timeline", "key": did}
        if mainWindow.focusTabByIdentity(identity):
            return

        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        ownerLabel = self._displayLabel(handle, display_name)
        tab = feedTabs.UserTimelineTabWindow(mainWindow.notebook, did, ownerLabel, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.add_open_temp_tab(account["id"], {
                "type": "user_timeline", "key": did, "did": did,
                "owner_label": ownerLabel, "origin_key": originKey,
            })

    def showFollowers(self, did, handle=None, display_name=None):
        self._openUserListTab("followers", did, handle, display_name)

    def showFollowing(self, did, handle=None, display_name=None):
        self._openUserListTab("following", did, handle, display_name)

    def showKnownFollowers(self, did, handle=None, display_name=None):
        # _openUserListTab is already generic on kind -- no changes
        # needed there, just a new kind string flowing through.
        self._openUserListTab("known_followers", did, handle, display_name)

    def _openUserListTab(self, kind, did, handle=None, display_name=None):
        # Opens or focuses a UserListTabWindow. Needs the real MainWindow,
        # not the dialog this mixin may be mixed into.
        from . import get_main_window
        from . import feedTabs
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return

        identity = {"kind": "user_list", "key": f"{kind}:{did}"}
        if mainWindow.focusTabByIdentity(identity):
            return

        # origin_key only set when opened from a PERMANENT tab -- a
        # removable-tab origin just lets wx.Notebook auto-select
        # whatever's next when this tab closes, no special fallback.
        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None

        ownerLabel = self._displayLabel(handle, display_name)
        tab = feedTabs.UserListTabWindow(mainWindow.notebook, kind, did, ownerLabel, origin_key=originKey)
        mainWindow.addTab(tab, tab.TAB_NAME, select=True, removable=True)
        account = db.get_active_account()
        if account is not None:
            db.add_open_temp_tab(account["id"], {
                "type": "user_list",
                "key": f"{kind}:{did}",
                "list_kind": kind,
                "did": did,
                "owner_label": ownerLabel,
                "origin_key": originKey,
            })

    def _displayLabel(self, handle, display_name=None):
        mode = db.get_ui_state("column1_display") or feedWindow.COLUMN_DISPLAY_NAME
        if mode == feedWindow.COLUMN_DISPLAY_NAME and display_name:
            return display_name
        # Translators: Fallback user label when neither display name nor handle is known.
        return f"@{handle}" if handle else _("unknown user")

    def viewProfile(self, did):
        # Translators: Announced while loading a user's profile.
        feedWindow._announce_now(_("Loading profile, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onProfileFetched, profile, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onProfileFetched(self, profile, error):
        if error:
            # Translators: Announced when loading a profile fails. {} is the error message.
            nvdaUi.message(_("Could not load profile: {}").format(error))
            return
        from . import feedTabs
        gui.mainFrame.prePopup()
        dlg = feedTabs.ProfileDialog(self, profile)
        dlg.Show()

    def _copyToClipboard(self, text: str):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def copyProfileUrl(self, did, handle):
        url = f"https://bsky.app/profile/{handle or did}"
        self._copyToClipboard(url)
        label = f"@{handle}" if handle else did
        # Translators: Announced after copying a user's profile URL. {} is the user's label.
        feedWindow._announce_now(_("Profile URL for {} copied to clipboard.").format(label))

    def startChat(self, did, handle):
        # Opens or focuses a ConvoTabWindow for this user (not the shared
        # Chat tab, whose selection would change).
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            # Translators: Announced when an action needs MainWindow but it isn't open.
            nvdaUi.message(_("Open NVSky's main window first."))
            return
        account = db.get_active_account()
        if account is None:
            # Translators: Announced when trying to start a chat with no active account.
            nvdaUi.message(_("No active account."))
            return
        if not account.get("chat_supported", 1):
            # Translators: Announced when the active account doesn't support DMs.
            nvdaUi.message(_("This account doesn't support direct messages."))
            return
        if did == account.get("did"):
            # Checked before any request (the server's error for this is cryptic).
            # Translators: Announced when trying to start a chat with your own account.
            nvdaUi.message(_("You can't start a chat with yourself."))
            return
        label = f"@{handle}" if handle else did
        # Translators: Announced while starting a new chat. {} is the recipient's label.
        feedWindow._announce_now(_("Starting chat with {}, please wait...").format(label))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                try:
                    availability = client.get_convo_availability(atprotoClient, [did])
                    canChat = getattr(availability, "can_chat", getattr(availability, "canChat", True))
                except Exception:
                    canChat = True
                if not canChat:
                    # Translators: Reported when the recipient doesn't accept messages from this account. {} is their label.
                    wx.CallAfter(self._onStartChatDone, None, _("{} isn't accepting messages from you.").format(label))
                    return
                convo = client.get_or_create_convo_for_member(atprotoClient, did)
                convoId = convo.get("id") if convo else None
                if convoId:
                    client.sync_convo_messages(atprotoClient, account["id"], convoId)
                    if db.get_convo(account["id"], convoId) is None:
                        client.sync_convos(atprotoClient, account["id"], account["did"])
                error = None
            except Exception as e:
                convo = None
                error = str(e)
            wx.CallAfter(self._onStartChatDone, convo, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onStartChatDone(self, convo, error):
        soundpack.stop_progress()
        if error or not convo or not convo.get("id"):
            # Translators: Fallback error when the server doesn't explain why opening a chat failed.
            errorText = error or _("no conversation was returned")
            # Translators: Announced when opening a chat fails. {} is the error message.
            nvdaUi.message(_("Could not open chat: {}").format(errorText))
            return
        from . import get_main_window
        mainWindow = get_main_window()
        if mainWindow is None:
            return
        convoId = convo["id"]
        identity = {"kind": "conversation", "key": convoId}
        if mainWindow.focusTabByIdentity(identity):
            return
        account = db.get_active_account()
        if account is None:
            return
        convoRow = db.get_convo(account["id"], convoId)
        if convoRow is None:
            # sync_convos above should have cached it already -- fall
            # back to the shared Chat tab if it somehow isn't there yet.
            mainWindow._openChatConvo(convoId)
            return
        from . import chatWindow
        members = db.get_convo_members(account["id"], convoId)
        activeIndex = mainWindow.notebook.GetSelection()
        activePanel = mainWindow.notebook.GetPage(activeIndex) if activeIndex != wx.NOT_FOUND else None
        activeIdentity = mainWindow._getTabIdentity(activePanel) if activePanel is not None else None
        originKey = activeIdentity["key"] if activeIdentity and activeIdentity["kind"] == "permanent" else None
        panel = chatWindow.ConvoTabWindow(mainWindow.notebook, convoRow, account, members, origin_key=originKey)
        tabLabel = db.describe_convo_from_members(convoRow, members)
        # Translators: Title of a popped-out conversation tab. {} is the conversation's display name.
        mainWindow.addTab(panel, _("Chat: {}").format(tabLabel), select=True, removable=True)
        db.add_open_temp_tab(account["id"], {
            "type": "conversation",
            "key": convoId,
            "convo_id": convoId,
            "origin_key": originKey,
        })

    def addToList(self, did, handle, display_name=None):
        account = db.get_active_account()
        if account is None:
            # Translators: Announced when trying to add a user to a list with no active account.
            nvdaUi.message(_("No active account."))
            return
        from . import listsWindow
        gui.mainFrame.prePopup()
        dlg = listsWindow.AddToListDialog(self, account, did, handle, display_name)
        dlg.Show()

    def showUserLists(self, did, handle, display_name=None):
        # Reuses the existing "Find lists by user..." dialog but skips
        # the search step -- did/handle are already known here.
        from . import listsWindow
        gui.mainFrame.prePopup()
        dlg = listsWindow.SubscribeListDialog(self, preselected_user={"did": did, "handle": handle, "display_name": display_name})
        dlg.Show()

    def _toggleRelation(self, did, handle, kind):
        """
        Shared confirm-then-act flow for Follow/Mute/Block (kind is
        "follow"/"mute"/"block"). Fetches the real current state first
        (with a "please wait" -- there's no cached follow/mute/block
        status anywhere, so this is unavoidable network latency), THEN
        asks a Yes/No confirmation worded for whichever direction is
        actually correct ("Follow @x?" vs "Unfollow @x?"), instead of
        silently toggling. The menu itself still opens instantly either
        way -- only clicking one of these three items waits.
        """
        # Translators: Announced while checking a user's follow/mute/block status.
        nvdaUi.message(_("Checking status, please wait..."))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                profile = client.get_profile(atprotoClient, did)
                error = None
            except Exception as e:
                profile = None
                error = str(e)
            wx.CallAfter(self._onRelationStatusChecked, did, handle, kind, profile, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onRelationStatusChecked(self, did, handle, kind, profile, error):
        if error:
            # Translators: Announced when checking a user's status fails. {} is the error message.
            nvdaUi.message(_("Could not check status: {}").format(error))
            return

        viewer = (profile or {}).get("viewer") or {}
        label = f"@{handle}" if handle else did

        if kind == "follow":
            currentValue = viewer.get("following")
            # Translators: Confirmation to unfollow a user. {} is their label.
            # Translators: Confirmation to follow a user. {} is their label.
            question = _("Unfollow {}?").format(label) if currentValue else _("Follow {}?").format(label)
        elif kind == "mute":
            currentValue = viewer.get("muted")
            # Translators: Confirmation to unmute a user. {} is their label.
            # Translators: Confirmation to mute a user. {} is their label.
            question = _("Unmute {}?").format(label) if currentValue else _("Mute {}?").format(label)
        else:
            currentValue = viewer.get("blocking")
            # Translators: Confirmation to unblock a user. {} is their label.
            # Translators: Confirmation to block a user. {} is their label.
            question = _("Unblock {}?").format(label) if currentValue else _("Block {}?").format(label)

        # Translators: Title of a Yes/No confirmation dialog.
        confirm = wx.MessageDialog(self, question, _("Confirm"), wx.YES_NO | wx.NO_DEFAULT)
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        if kind == "follow":
            self._runRelationAction(client.unfollow_actor, currentValue) if currentValue else \
                self._runRelationAction(client.follow_actor, did)
        elif kind == "mute":
            self._runRelationAction(client.unmute_actor, did) if currentValue else \
                self._runRelationAction(client.mute_actor, did)
        else:
            self._runRelationAction(client.unblock_actor, currentValue) if currentValue else \
                self._runRelationAction(client.block_actor, did)

    def _runRelationAction(self, action_fn, arg):
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                action_fn(atprotoClient, arg)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after a follow/mute/block action succeeds.
            wx.CallAfter(self._onUserActionDone, _("Done.") if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserActionDone(self, message, error):
        if error:
            log.error(f"NVSky: user action failed: {error}")
            # Translators: Announced when a user action fails. {} is the error message.
            nvdaUi.message(_("Action failed: {}").format(error))
            return
        if message:
            nvdaUi.message(message)

    def _toggleRelationCached(self, did, handle, kind, currentValue):
        if currentValue == "pending":
            # Translators: Announced when an action is repeated while the previous one is still in progress.
            feedWindow._announce_now(_("Please wait..."))
            return
        label = f"@{handle}" if handle else did
        if kind == "follow":
            db.set_author_following(did, None if currentValue else "pending")
            soundpack.play("unfollow" if currentValue else "follow")
            # Translators: Announced after unfollowing a user. {} is their label.
            # Translators: Announced after following a user. {} is their label.
            feedWindow._announce_now(_("Unfollowed {}.").format(label) if currentValue else _("Followed {}.").format(label))
        elif kind == "mute":
            db.set_author_muted(did, not currentValue)
            soundpack.play("block_mute")
            # Translators: Announced after unmuting a user. {} is their label.
            # Translators: Announced after muting a user. {} is their label.
            feedWindow._announce_now(_("Unmuted {}.").format(label) if currentValue else _("Muted {}.").format(label))
        elif kind == "activity_sub":
            db.set_author_activity_subscription(did, not currentValue)
            soundpack.play("block_mute")
            # Translators: Announced after unsubscribing from a user's post notifications. {} is their label.
            # Translators: Announced after subscribing to a user's post notifications. {} is their label.
            feedWindow._announce_now(_("Unsubscribed from {}'s posts.").format(label) if currentValue else _("Subscribed to {}'s posts.").format(label))
        else:
            db.set_author_blocking(did, None if currentValue else "pending")
            soundpack.play("block_mute")
            # Translators: Announced after unblocking a user. {} is their label.
            # Translators: Announced after blocking a user. {} is their label.
            feedWindow._announce_now(_("Unblocked {}.").format(label) if currentValue else _("Blocked {}.").format(label))

        # Lists of one's own relationships drop the user right away (see UserListMixin).
        afterToggle = getattr(self, "_afterRelationToggled", None)
        if callable(afterToggle) and currentValue:
            afterToggle(did, kind)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if kind == "follow":
                    resultValue = None if currentValue else client.follow_actor(atprotoClient, did)
                    if currentValue:
                        client.unfollow_actor(atprotoClient, currentValue)
                elif kind == "mute":
                    if currentValue:
                        client.unmute_actor(atprotoClient, did)
                    else:
                        client.mute_actor(atprotoClient, did)
                    resultValue = not currentValue
                elif kind == "activity_sub":
                    client.set_activity_subscription(atprotoClient, did, not currentValue)
                    resultValue = not currentValue
                else:
                    resultValue = None if currentValue else client.block_actor(atprotoClient, did)
                    if currentValue:
                        client.unblock_actor(atprotoClient, currentValue)
                error = None
            except Exception as e:
                resultValue = currentValue
                error = str(e)
            wx.CallAfter(self._onRelationCachedDone, did, kind, currentValue, resultValue, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRelationCachedDone(self, did, kind, previousValue, resultValue, error):
        finalValue = previousValue if error else resultValue
        if kind == "follow":
            db.set_author_following(did, finalValue)
        elif kind == "mute":
            db.set_author_muted(did, finalValue)
        elif kind == "activity_sub":
            db.set_author_activity_subscription(did, finalValue)
        else:
            db.set_author_blocking(did, finalValue)
        if error:
            # Translators: Announced when a follow/mute/block/subscribe action fails after the optimistic UI update. {} is the error message.
            nvdaUi.message(_("Action failed: {}").format(error))

    def _reportActor(self, did, handle):
        # NOTE: was "for label, _ in REPORT_REASONS" -- bare `_` shadowed
        # gettext within this function's scope. Renamed to avoid that trap.
        labels = [label for label, _reasonCode in feedWindow.REPORT_REASONS]
        # Translators: Prompt in the report-user reason picker.
        # Translators: Title of the report-user reason picker.
        dlg = wx.SingleChoiceDialog(self, _("Reason for reporting this user:"), _("Report user"), labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        reasonType = feedWindow.REPORT_REASONS[dlg.GetSelection()][1]
        dlg.Destroy()

        label = f"@{handle}" if handle else did
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation before sending a user report to Bluesky moderation. {} is the user's label.
            _("Send this report for {} to Bluesky moderation?").format(label),
            # Translators: Title of the confirm-report dialog.
            _("Confirm report"), wx.YES_NO | wx.NO_DEFAULT
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.create_actor_report(atprotoClient, did, reasonType)
                error = None
            except Exception as e:
                error = str(e)
            if not error:
                soundpack.play("block_mute")
            # Translators: Announced after successfully sending a user report.
            wx.CallAfter(self._onUserActionDone, _("Report sent.") if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()


class UserListMixin:
    """
    Shared "browsable list of users" render/focus/action behavior --
    used by UserListTabWindow (followers/following) and Explore's
    People results. No local DB cache/pagination like FeedListMixin --
    always a single fresh network fetch, matching what the old
    UserListDialog already did.

    A host class must set self._users (list of dicts with did/handle,
    optionally display_name/description), self.userList, self.statusBar
    before render, and implement self._userListLabel() -> str.
    """

    def _buildUserListColumns(self):
        # Translators: Column header for a user's handle in a user list.
        self.userList.InsertColumn(0, _("Handle"), width=180)
        # Translators: Column header for a user's display name in a user list.
        self.userList.InsertColumn(1, _("Display name"), width=180)
        # Translators: Column header for a user's bio in a user list.
        self.userList.InsertColumn(2, _("Bio"), width=300)

    def _insertUserRow(self, index, user):
        self.userList.InsertItem(index, f'@{user["handle"]}')
        self.userList.SetItem(index, 1, user.get("display_name") or "")
        self.userList.SetItem(index, 2, (user.get("description") or "").replace("\n", " "))

    def _renderUsers(self, target_index=None):
        previouslyFocused = self.userList.GetFocusedItem()
        self.userList.Freeze()
        try:
            self.userList.DeleteAllItems()
            for i, user in enumerate(self._users):
                self._insertUserRow(i, user)
            if not self._users:
                index = None
            elif target_index is not None:
                index = max(0, min(target_index, len(self._users) - 1))
            elif previouslyFocused != -1:
                index = min(previouslyFocused, len(self._users) - 1)
            else:
                index = 0
            if index is not None:
                self.userList.Focus(index)
                self.userList.Select(index)
                self.userList.EnsureVisible(index)
        finally:
            self.userList.Thaw()
        if hasattr(self, "statusBar"):
            self.statusBar.SetStatusText(self._userListLabel())
        updateButtons = getattr(self, "_updateButtons", None)
        if callable(updateButtons):
            updateButtons()

    def _getFocusedUser(self):
        index = self.userList.GetFocusedItem()
        if 0 <= index < len(self._users):
            return self._users[index]
        return None

    # Relation that removes a user from each kind of list.
    _REMOVING_RELATION = {"following": "follow", "muted": "mute", "blocked": "block", "subscriptions": "activity_sub"}

    def _listIsMine(self):
        return False

    def _usersChanged(self):
        pass

    @uiutil.safe_ui_callback
    def _focusWhenEmpty(self):
        # An emptied ListCtrl that keeps focus makes NVDA report "unknown".
        uiutil.focus_check_updates_button(self)

    def _afterRelationToggled(self, did, relation):
        # The row leaves at once (not after the server answers) so no later
        # re-read of the list lands on top of the announcement.
        if self._REMOVING_RELATION.get(getattr(self, "_kind", None)) != relation or not self._listIsMine():
            return
        index = next((i for i, u in enumerate(self._users) if u["did"] == did), None)
        if index is None:
            return
        del self._users[index]
        self.userList.DeleteItem(index)
        if self._users:
            newIndex = min(index, len(self._users) - 1)
            self.userList.Focus(newIndex)
            self.userList.Select(newIndex)
        else:
            wx.CallAfter(self._focusWhenEmpty)
        self.statusBar.SetStatusText(self._userListLabel())
        self._usersChanged()

    def onUserAction(self, evt=None):
        user = self._getFocusedUser()
        if user is None:
            # Translators: Announced when the user-action menu (Alt+U) is invoked with no user focused.
            nvdaUi.message(_("No user selected."))
            return
        self.showUserActionMenu(user["did"], user["handle"], user.get("display_name"))

    def onUserListCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == ord("C") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.userList:
            if uiutil.copy_focused_row(self.userList):
                return
        if keyCode == ord("J") and evt.ControlDown() and not evt.ShiftDown() and self.FindFocus() is self.userList:
            uiutil.jump_to_row(self, self.userList)
            return
        if evt.AltDown() and keyCode == ord("U"):
            self.onUserAction()
            return
        if keyCode == wx.WXK_F5 and not evt.ShiftDown() and not evt.ControlDown():
            self.onCheckForUpdates(None)
            return
        if evt.ControlDown() and keyCode == wx.WXK_DELETE:
            self.onClearCache()
            return
        evt.Skip()