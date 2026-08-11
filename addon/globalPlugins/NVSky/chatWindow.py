"""
Chat (DM) tab for NVSky.

Structurally different from Home/Notifications/Saved -- those are all
flat post lists sharing FeedListMixin/ItemActionMixin. Chat has two
levels (conversations -> messages) and its own send/mark-read/mute
actions that don't map onto a post at all, so it's its own thing here
rather than shoehorned into the existing mixins.

REFACTOR NOTE: ChatWindow (the permanent tab, conversation tree +
messages) and ConvoTabWindow (a single conversation popped into its
own removable tab) used to duplicate almost their entire message-pane
logic -- multiple rounds of bug fixes only landed in one class and not
the other because of it (Alt+number mark-read, Left/Right reply-jump,
the _reactToMessage argument-count crash). _ChatMessagePanelMixin below
now owns everything that only needs "the currently displayed
conversation's message list" (self._currentConvoId) -- reactions,
Alt+number, Space-jump-to-unread, reply/copy/delete, the emoji picker,
sending, Check for updates' shared bits. What's left on each class is
only what's genuinely different: ChatWindow has the conversation tree
and per-conversation actions (accept/decline/mute/leave/mark read);
ConvoTabWindow has a fixed single conversation and its own tab-life-
cycle hooks (onTabRemoved/onTabRenamed, TAB_TEMP_TYPE/KEY).

Confirmed design (several rounds of back-and-forth):
  - ONE permanent "Chat" tab, not a tab per conversation.
  - wx.TreeCtrl lists conversations (flat, one level); wx.ListCtrl
    alongside it shows the messages of whichever conversation is
    currently selected. Tab moves focus between them like any two
    sibling controls.
  - Expanding/selecting a conversation must be INSTANT -- messages are
    read from the local cache only, never fetched on demand. Check for
    updates (F5) syncs the conversation list AND every conversation's
    messages in one pass specifically so this holds.
  - A compose box + Send button at the bottom sends to whichever
    conversation is currently selected.
  - "Open in new tab" (from a conversation's context menu) pops it out
    into its own REMOVABLE tab (title = the other person's name) with
    just that conversation's messages + its own compose box -- opt-in,
    not the default.
  - Message-level read state (is_read) is re-derived from the server's
    per-conversation unread_count on every sync (db.
    reconcile_message_read_state), not guessed locally -- see that
    function's docstring for why an earlier attempt at this got it
    wrong. Reads are also pushed back to the server in the background
    (client.mark_message_read) so other Bluesky clients stay in sync;
    never surfaced to this UI either way.

EXPERIMENTAL: first use of chat.bsky.convo.* in NVSky. Field names
below were verified against the installed atproto Python SDK's actual
model classes where possible, but several endpoints (addReaction/
removeReaction, updateAllRead, getConvoForMembers, updateRead's
messageId param) have never been exercised against a real account as
of this writing. Paste back any traceback. Also see get_chat_client()'s
docstring in client.py -- an account that has never opened Chat in the
official Bluesky app may not have DMs enabled server-side yet.
"""
import json
import threading
import traceback

import wx

import gui
import ui as nvdaUi
from logHandler import log

from . import client
from . import db
from . import timeutils
from . import uiutil

# LOW CONFIDENCE: Bluesky's DM character limit isn't published on the
# docs site the way the 300-grapheme post limit is -- this number is a
# best guess, not confirmed against a real send. If the server rejects
# a message under this length (or accepts one over it), paste the
# error back and this gets corrected.
CHAT_MESSAGE_MAX_LENGTH = 1000

# How many characters go in the "Message" column before the rest
# spills into "Message (more)" -- kept well under the ~511-character
# native ListCtrl cell limit confirmed by testing (see plan-09.md) to
# leave a safety margin, since the exact cutoff may vary slightly with
# content (e.g. astral-plane emoji use a UTF-16 surrogate pair on
# Windows despite counting as one Python character). Doesn't need to
# be exact -- NVDA reads both columns back to back automatically when
# arrow-navigating a row, so the user never notices the split.
MESSAGE_COLUMN_SPLIT_THRESHOLD = 450


def _split_for_columns(text):
    return client.split_at_grapheme_boundary(text, MESSAGE_COLUMN_SPLIT_THRESHOLD)


def _format_time(value):
    mode, pattern = timeutils.current_mode_and_pattern(db)
    return timeutils.format_timestamp(value, mode=mode, custom_pattern=pattern)


def _focused_list_index(listCtrl):
    # wx.ListCtrl has no EVT_LIST_ITEM_MENU (unlike TreeCtrl's
    # EVT_TREE_ITEM_MENU) -- EVT_CONTEXT_MENU covers both right-click
    # and the keyboard Menu/Shift+F10 key, but doesn't hand back which
    # row triggered it, so find the currently focused one directly.
    for i in range(listCtrl.GetItemCount()):
        if listCtrl.GetItemState(i, wx.LIST_STATE_FOCUSED):
            return i
    return -1


def _describe_reactions(reactions_json) -> str:
    if not reactions_json:
        return ""
    try:
        reactions = json.loads(reactions_json)
    except (ValueError, TypeError):
        return ""
    if not reactions:
        return ""
    counts = {}
    for r in reactions:
        value = r.get("value", "")
        counts[value] = counts.get(value, 0) + 1
    return " ".join(f"{emoji}x{n}" if n > 1 else emoji for emoji, n in counts.items())


_COMMON_EMOJI = [
    ("\U0001F600", "Grinning face"),
    ("\U0001F602", "Face with tears of joy"),
    ("\U0001F60A", "Smiling face"),
    ("\U0001F60D", "Heart eyes"),
    ("\U0001F622", "Crying face"),
    ("\U0001F62E", "Surprised face"),
    ("\U0001F621", "Angry face"),
    ("\U0001F44D", "Thumbs up"),
    ("\U0001F44E", "Thumbs down"),
    ("\U0001F64F", "Folded hands"),
    ("\U0001F44F", "Clapping hands"),
    ("\U0001F389", "Party popper"),
    ("\u2764\uFE0F", "Red heart"),
    ("\U0001F525", "Fire"),
    ("\U0001F4AF", "Hundred points"),
    ("\U0001F634", "Sleeping face"),
]


def _pick_emoji(parent, title):
    # A real picker instead of a blank text box -- wx.SingleChoiceDialog
    # is a plain listbox under the hood, so it's fully keyboard/NVDA
    # navigable (arrows to browse, each item read as "emoji + name").
    # "Custom..." falls back to typing/pasting (e.g. from the Windows
    # emoji panel, Win+.) for anything not in the curated list.
    choices = [f"{emoji} {label}" for emoji, label in _COMMON_EMOJI] + ["Custom..."]
    dlg = wx.SingleChoiceDialog(parent, "Choose an emoji:", title, choices)
    result = None
    if dlg.ShowModal() == wx.ID_OK:
        index = dlg.GetSelection()
        if index == len(_COMMON_EMOJI):
            entryDlg = wx.TextEntryDialog(parent, "Type or paste one emoji:", title)
            if entryDlg.ShowModal() == wx.ID_OK:
                result = entryDlg.GetValue().strip()
            entryDlg.Destroy()
        else:
            result = _COMMON_EMOJI[index][0]
    dlg.Destroy()
    return result


def _show_message_dialog(parent, title, text):
    # Read-only, freely-scrollable full text -- same idea as the
    # Column Review add-on's cell-inspection dialog (which the user
    # used to independently confirm the ~511-char cutoff is native to
    # the ListCtrl itself, not specific to LC_VIRTUAL -- see
    # plan-09.md). Exists specifically to bypass the ListCtrl's own
    # text storage/retrieval entirely.
    dlg = wx.Dialog(parent, title=title, size=(500, 350))
    sizer = wx.BoxSizer(wx.VERTICAL)
    textCtrl = wx.TextCtrl(dlg, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
    sizer.Add(textCtrl, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)
    closeBtn = wx.Button(dlg, id=wx.ID_CLOSE, label="&Close")
    sizer.Add(closeBtn, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)
    dlg.SetSizer(sizer)
    dlg.CentreOnScreen()

    def onCharHook(evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            dlg.EndModal(wx.ID_CLOSE)
            return
        evt.Skip()

    closeBtn.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
    dlg.Bind(wx.EVT_CHAR_HOOK, onCharHook)
    textCtrl.SetFocus()
    dlg.ShowModal()
    dlg.Destroy()


class _ChatMessagePanelMixin:
    """
    Shared behavior for anything that displays "the currently selected
    conversation's" message list -- ChatWindow's message pane and
    ConvoTabWindow both mix this in.

    A host class must, before user interaction starts, set:
      self._account            -- account dict or None
      self._currentConvoId     -- str, the conversation this pane shows
      self._replyToMessageId   -- None initially
      self._jumpBackMessageId  -- None initially
      self._suppressFocusEvents -- False initially
    and create these widgets on itself:
      self.messageList, self.composeText, self.emojiButton,
      self.sendButton, self.charCountLabel, self.replyingToLabel,
      self.cancelReplyButton, self.statusBar
    and implement these hooks:
      self._afterMessageRead(convoId)     -- refresh whatever unread
                                              indicator this host shows
      self._reloadMessagesIfCurrent(convoId) -- re-render from the DB,
                                              only if convoId is still
                                              what's on screen
    """

    def _buildMessageListColumns(self):
        self.messageList.InsertColumn(0, "Reactions", width=100)
        self.messageList.InsertColumn(1, "From", width=140)
        self.messageList.InsertColumn(2, "Message", width=350)
        self.messageList.InsertColumn(3, "Message (more)", width=250)
        self.messageList.InsertColumn(4, "Sent", width=140)

    # ---------------- read tracking ----------------

    def onMessageFocused(self, evt):
        if self._suppressFocusEvents:
            evt.Skip()
            return
        index = evt.GetIndex()
        messages = getattr(self, "_currentMessages", [])
        convoId = self._currentConvoId
        if self._account and convoId and 0 <= index < len(messages):
            message = messages[index]
            messageId = message.get("message_id")
            # tempMessage placeholders (optimistic send, see onSend)
            # have no message_id yet -- nothing to mark until the
            # resync swaps it for the real, DB-backed row.
            if messageId and not message.get("is_read"):
                db.mark_message_read(self._account["id"], convoId, messageId)
                message["is_read"] = 1
                self._afterMessageRead(convoId)
                self._pushMessageReadToServer(convoId, messageId)
        evt.Skip()

    def _focusNextUnreadMessage(self):
        messages = getattr(self, "_currentMessages", [])
        for i, message in enumerate(messages):
            if message.get("message_id") and not message.get("is_read"):
                self.messageList.Focus(i)
                self.messageList.Select(i)
                self.messageList.EnsureVisible(i)
                return
        nvdaUi.message("No unread messages.")

    def _pushMessageReadToServer(self, convoId, messageId):
        # Silent, background-only -- see client.mark_message_read's
        # docstring. Never surfaces success or failure to the user;
        # this purely keeps the server's own state in sync with what
        # the local DB (the actual source of truth for this app's UI)
        # already reflects.
        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.mark_message_read(atprotoClient, convoId, messageId)
            except Exception as e:
                log.error(f"NVSky: background mark-message-read failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _announceNthNewestMessage(self, n: int):
        # self._currentMessages is always in the SAME order as what's
        # on screen (follows Settings > Display sort order), so index
        # math flips depending on which end "newest" currently is.
        # Doesn't move focus, just speaks it, so the user can stay
        # wherever they were (e.g. typing in the compose box). Reads
        # every field straight from self._currentMessages instead of
        # the ListCtrl's own GetItemText -- confirmed by testing (see
        # plan-09.md) that GetItemText's returned text is itself capped
        # at ~511 characters on Windows regardless of LC_VIRTUAL, so it
        # can never be trusted to carry a full-length message.
        messages = getattr(self, "_currentMessages", [])
        newestFirst = getattr(self, "_messagesNewestFirst", False)
        index = (n - 1) if newestFirst else (len(messages) - n)
        if not (0 <= index < len(messages)):
            nvdaUi.message(f"No message {n}.")
            return
        message = messages[index]
        # Alt+number doesn't move focus, so onMessageFocused never
        # fires for it -- without this, reading a message aloud this
        # way never marked it read.
        if uiutil.move_focus_and_check_announce(self.messageList, index):
            parts = [
                _describe_reactions(message.get("reactions_json")),
                self._messageFromLabel(message),
                self._messageDisplayText(message),
                _format_time(message["sent_at"]) if message.get("sent_at") else "Sending...",
            ]
            nvdaUi.message(", ".join(p for p in parts if p))
        messageId = message.get("message_id")
        convoId = self._currentConvoId
        if self._account and convoId and messageId and not message.get("is_read"):
            db.mark_message_read(self._account["id"], convoId, messageId)
            message["is_read"] = 1
            self._afterMessageRead(convoId)
            self._pushMessageReadToServer(convoId, messageId)

    # ---------------- reply navigation ----------------

    def _jumpToRepliedMessage(self):
        index = _focused_list_index(self.messageList)
        if index == -1:
            return
        messages = self._currentMessages
        if index >= len(messages):
            return
        replyToId = messages[index].get("reply_to_message_id")
        if not replyToId:
            nvdaUi.message("This message isn't a reply.")
            return
        for i, m in enumerate(messages):
            if m["message_id"] == replyToId:
                self._jumpBackMessageId = messages[index]["message_id"]
                self.messageList.Focus(i)
                self.messageList.Select(i)
                self.messageList.EnsureVisible(i)
                nvdaUi.message("Jumped to original message.")
                return
        nvdaUi.message("Original message isn't loaded in this view.")

    def _jumpBackToReply(self):
        index = _focused_list_index(self.messageList)
        if index == -1:
            return
        messages = self._currentMessages
        if index >= len(messages):
            return

        if self._jumpBackMessageId is not None:
            targetId = self._jumpBackMessageId
            self._jumpBackMessageId = None
        else:
            currentId = messages[index]["message_id"]
            targetId = next(
                (m["message_id"] for m in messages if m.get("reply_to_message_id") == currentId), None
            )
            if targetId is None:
                nvdaUi.message("No reply to this message found.")
                return

        for i, m in enumerate(messages):
            if m["message_id"] == targetId:
                self.messageList.Focus(i)
                self.messageList.Select(i)
                self.messageList.EnsureVisible(i)
                nvdaUi.message("Jumped forward.")
                return

    # ---------------- message-level actions ----------------

    def _startReply(self, message):
        self._replyToMessageId = message["message_id"]
        preview = (message.get("text") or "")[:40]
        self.replyingToLabel.SetLabel(f"Replying to: {preview}")
        self.replyingToLabel.Show()
        self.cancelReplyButton.Show()
        self.Layout()
        self.composeText.SetFocus()
        nvdaUi.message(f"Replying to: {preview}")

    def onCancelReply(self, evt):
        self._replyToMessageId = None
        self.replyingToLabel.Hide()
        self.cancelReplyButton.Hide()
        self.Layout()
        nvdaUi.message("Reply canceled.")

    def _copyMessageText(self, message):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(message.get("text", "")))
            wx.TheClipboard.Close()
        nvdaUi.message("Message text copied to clipboard.")

    def _showFullMessage(self, message):
        fromLabel = self._messageFromLabel(message)
        text = self._messageDisplayText(message)
        _show_message_dialog(self, f"Message from {fromLabel}", text)

    def _deleteMessageForSelf(self, message):
        convoId = self._currentConvoId
        messageId = message["message_id"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.delete_message_for_self(atprotoClient, convoId, messageId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onDeleteMessageDone, convoId, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onDeleteMessageDone(self, convoId, error):
        if error:
            log.error(f"NVSky: delete message failed: {error}")
            nvdaUi.message(f"Could not delete message: {error}")
            return
        nvdaUi.message("Message deleted.")
        self._reloadMessagesIfCurrent(convoId)

    # ---------------- reactions ----------------

    def _reactToMessage(self, convoId, message):
        messageId = message.get("message_id")
        if not messageId:
            nvdaUi.message("This message hasn't finished sending yet.")
            return
        myDid = self._account["did"] if self._account else None
        try:
            existingReactions = json.loads(message.get("reactions_json") or "[]")
        except (ValueError, TypeError):
            existingReactions = []
        myReaction = next((r for r in existingReactions if r.get("sender", {}).get("did") == myDid), None)

        if myReaction:
            emoji = myReaction.get("value", "")
            if wx.MessageBox(f"Remove your {emoji} reaction?", "Remove reaction", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return

            def worker():
                try:
                    atprotoClient = client.get_client_for_active_account()
                    client.remove_reaction(atprotoClient, convoId, messageId, emoji)
                    client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                    error = None
                except Exception as e:
                    error = str(e)
                wx.CallAfter(self._onReactionDone, convoId, error)

            threading.Thread(target=worker, daemon=True).start()
            return

        emoji = _pick_emoji(self, "React to message")
        if emoji:
            if client.count_graphemes(emoji) != 1:
                nvdaUi.message("Please pick or enter exactly one emoji.")
                return

            def worker():
                try:
                    atprotoClient = client.get_client_for_active_account()
                    client.add_reaction(atprotoClient, convoId, messageId, emoji)
                    client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                    error = None
                except Exception as e:
                    error = str(e)
                wx.CallAfter(self._onReactionDone, convoId, error)

            threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onReactionDone(self, convoId, error):
        if error:
            log.error(f"NVSky: reaction action failed: {error}")
            nvdaUi.message(f"Reaction failed: {error}")
            return
        self._reloadMessagesIfCurrent(convoId)
        nvdaUi.message("Reactions updated.")

    def onInsertEmoji(self, evt):
        if not self.composeText.IsShown():
            return
        emoji = _pick_emoji(self, "Insert emoji")
        if emoji:
            self.composeText.WriteText(emoji)
        self.composeText.SetFocus()

    # ---------------- message context menu ----------------

    def onMessageContextMenu(self, evt):
        index = _focused_list_index(self.messageList)
        if index == -1:
            nvdaUi.message("No message selected.")
            return
        convoId = self._currentConvoId
        if not convoId:
            return
        messages = getattr(self, "_currentMessages", [])
        if index >= len(messages):
            return
        message = messages[index]

        menu = wx.Menu()
        replyItem = menu.Append(wx.ID_ANY, "Reply")
        reactItem = menu.Append(wx.ID_ANY, "React...")
        copyItem = menu.Append(wx.ID_ANY, "Copy text")
        showItem = menu.Append(wx.ID_ANY, "Show message...")
        deleteItem = menu.Append(wx.ID_ANY, "Delete for me...")

        self.Bind(wx.EVT_MENU, lambda e: self._startReply(message), replyItem)
        self.Bind(wx.EVT_MENU, lambda e: self._reactToMessage(convoId, message), reactItem)
        self.Bind(wx.EVT_MENU, lambda e: self._copyMessageText(message), copyItem)
        self.Bind(wx.EVT_MENU, lambda e: self._showFullMessage(message), showItem)
        self.Bind(wx.EVT_MENU, lambda e: self._deleteMessageForSelf(message), deleteItem)

        self.PopupMenu(menu)
        menu.Destroy()

    # ---------------- composing / sending ----------------

    def onComposeTextChanged(self, evt):
        length = client.count_graphemes(self.composeText.GetValue())
        self.charCountLabel.SetLabel(f"{length} / {CHAT_MESSAGE_MAX_LENGTH}" if length else "")
        evt.Skip()

    def onSend(self, evt):
        if not self.composeText.IsShown():
            return
        text = self.composeText.GetValue().strip()
        if not text:
            return
        length = client.count_graphemes(text)
        if length > CHAT_MESSAGE_MAX_LENGTH:
            nvdaUi.message(f"Message is too long ({length}/{CHAT_MESSAGE_MAX_LENGTH}).")
            return
        convoId = self._currentConvoId
        if not convoId:
            nvdaUi.message("Select a conversation first.")
            return

        self.composeText.SetValue("")
        replyToMessageId = self._replyToMessageId
        self._replyToMessageId = None
        self.replyingToLabel.Hide()
        self.cancelReplyButton.Hide()
        self.Layout()

        # Optimistic UI: render the message and show it right away
        # instead of waiting on two network round-trips (send + full
        # resync). send_message's own echoed-back response can't be
        # trusted (see the SDK-bug note on send_message itself in
        # client.py), so a real message_id only ever arrives via the
        # silent resync below -- this temp row just holds the spot.
        newestFirst = getattr(self, "_messagesNewestFirst", False)
        tempIndex = 0 if newestFirst else self.messageList.GetItemCount()
        mainText, moreText = _split_for_columns(text)
        self.messageList.InsertItem(tempIndex, "")
        self.messageList.SetItem(tempIndex, 1, "You")
        self.messageList.SetItem(tempIndex, 2, mainText)
        self.messageList.SetItem(tempIndex, 3, moreText)
        self.messageList.SetItem(tempIndex, 4, "Sending...")
        self.messageList.Focus(tempIndex)
        self.messageList.Select(tempIndex)
        self.messageList.EnsureVisible(tempIndex)
        if not hasattr(self, "_currentMessages"):
            self._currentMessages = []
        # is_read=1 -- it's my own message, nothing to "catch up" on;
        # the real row that lands via resync gets this from
        # client._sync_convo_messages -> db.reconcile_message_read_state
        # too, this just keeps the placeholder from flashing unread.
        tempMessage = {"sender_did": self._account["did"], "text": text, "sent_at": None, "is_read": 1}
        self._currentMessages.insert(0, tempMessage) if newestFirst else self._currentMessages.append(tempMessage)
        # The row above is already rendered synchronously by this point
        # -- this pause is only so the spoken confirmation doesn't land
        # in the exact same instant as the keypress.
        wx.CallLater(250, nvdaUi.message, "Message sent.")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.send_message(atprotoClient, convoId, text, reply_to_message_id=replyToMessageId)
            except Exception as e:
                log.error(f"NVSky: send_message full traceback:\n{traceback.format_exc()}")
                wx.CallAfter(self._onSendComplete, convoId, str(e))
                return

            # The send itself already succeeded -- from here on this is
            # just swapping the temp row for the real, server-confirmed
            # one, silently, whenever it happens to land. A failure here
            # is NOT a failed send, just a stale local cache until the
            # next F5/tab-switch.
            try:
                atprotoClient = client.get_client_for_active_account()
                client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
            except Exception as e:
                log.error(f"NVSky: post-send resync failed: {e}")
                return

            wx.CallAfter(self._onSendComplete, convoId, None)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSendComplete(self, convoId, error):
        if error:
            nvdaUi.message(f"Could not send message: {error}")
        self._reloadMessagesIfCurrent(convoId)  # drops/swaps the optimistic row either way

    # ---------------- keyboard shortcuts ----------------

    def onCheckAllConvos(self):
        # Shift+F5 -- syncs EVERY conversation (client.sync_convos),
        # not just whichever one is currently on screen, with a
        # generic summary instead of onCheckForUpdates' per-conversation
        # wording (which only ever reports on the currently selected
        # convo -- correct for plain F5, misleading here).
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        nvdaUi.message("Checking all chats for updates, please wait...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onCheckAllConvosDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCheckAllConvosDone(self, error):
        if error:
            log.error(f"NVSky: Chat sync failed: {error}")
            nvdaUi.message(f"Could not check all chats for updates: {error}")
            return
        self._reloadMessagesIfCurrent(self._currentConvoId)
        nvdaUi.message("All chats checked.")

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == wx.WXK_F5 and evt.ControlDown():
            # Let this bubble up to MainWindow's own Ctrl+F5 handler
            # (checkAllOpenTabs -- updates every open tab in the addon,
            # not just Chat's conversations).
            evt.Skip()
            return
        if keyCode == wx.WXK_F5 and evt.ShiftDown():
            self.onCheckAllConvos()
            return
        if keyCode == wx.WXK_F5:
            self.onRefreshSelectedConvo()
            return
        if keyCode == wx.WXK_RETURN and evt.ControlDown():
            self.onSend(None)
            return
        if evt.AltDown() and ord("1") <= keyCode <= ord("9"):
            self._announceNthNewestMessage(keyCode - ord("0"))
            return
        if keyCode == wx.WXK_LEFT and not evt.HasAnyModifiers() and self.FindFocus() is self.messageList:
            self._jumpToRepliedMessage()
            return
        if keyCode == wx.WXK_RIGHT and not evt.HasAnyModifiers() and self.FindFocus() is self.messageList:
            self._jumpBackToReply()
            return
        if keyCode == wx.WXK_SPACE and self.FindFocus() is self.messageList:
            self._focusNextUnreadMessage()
            return
        evt.Skip()


class NewChatDialog(wx.Dialog):
    """
    Start (or reopen) a 1:1 conversation with someone not already in
    your conversation list -- same typeahead-search pattern as
    feedWindow.py's SubscribeListDialog/ManageMembersDialog. Requires
    an actual first message: chat.bsky.convo.getConvoForMembers only
    resolves/creates a conversation record, it doesn't put anything in
    it -- an empty conversation isn't listed anywhere (not even on
    bsky.app) until a real message is sent to it.
    """

    def __init__(self, parent, account, on_started=None):
        self._account = account
        self._userSuggestions = []
        self._onStarted = on_started

        super().__init__(parent, title="Start a new chat", size=(420, 260))

        sizer = wx.BoxSizer(wx.VERTICAL)

        userLabel = wx.StaticText(self, label="Search for a user by handle or name:")
        self.userSearchText = wx.TextCtrl(self)
        sizer.Add(userLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.userSearchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.userChoice = wx.Choice(self, choices=[])
        sizer.Add(self.userChoice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        messageLabel = wx.StaticText(self, label="First message:")
        self.messageText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(messageLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        sizer.Add(self.messageText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.startButton = wx.Button(self, label="Start chat")
        closeBtn = wx.Button(self, label="&Cancel")
        actionRow.Add(self.startButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.userSearchText.Bind(wx.EVT_TEXT, self.onUserSearchChanged)
        self.startButton.Bind(wx.EVT_BUTTON, self.onStartChat)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.userSearchText.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

    def onUserSearchChanged(self, evt):
        wx.CallLater(400, self._runUserSearch, self.userSearchText.GetValue())

    def _runUserSearch(self, query):
        if query != self.userSearchText.GetValue():
            return
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
        self.userChoice.Set([f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in results])
        if results:
            self.userChoice.SetSelection(0)

    def onStartChat(self, evt):
        index = self.userChoice.GetSelection()
        if not (0 <= index < len(self._userSuggestions)):
            nvdaUi.message("Search for a user and pick one from the list first.")
            return
        text = self.messageText.GetValue().strip()
        if not text:
            nvdaUi.message("Type a first message before starting the chat.")
            return
        user = self._userSuggestions[index]
        nvdaUi.message(f'Starting chat with @{user["handle"]}, please wait...')

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                convo = client.get_or_create_convo_for_member(atprotoClient, user["did"])
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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onStartChatDone(self, convo, error):
        if error:
            log.error(f"NVSky: start new chat failed: {error}")
            nvdaUi.message(f"Could not start chat: {error}")
            return
        if not convo or not convo.get("id"):
            log.error(f"NVSky: getConvoForMembers returned no usable convo: {convo!r}")
            nvdaUi.message("Could not start chat: no conversation was returned.")
            return
        gui.mainFrame.postPopup()
        self.Destroy()
        if self._onStarted:
            self._onStarted(convo.get("id"))


class ChatWindow(_ChatMessagePanelMixin, wx.Panel):
    TAB_NAME = "Chat"
    TAB_KEY = "chat"

    def __init__(self, parent):
        super().__init__(parent)

        self._account = db.get_active_account()
        self._convos = []
        self._replyToMessageId = None
        self._jumpBackMessageId = None
        self._currentConvoId = None
        self._suppressFocusEvents = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        splitRow = wx.BoxSizer(wx.HORIZONTAL)

        self.convoTree = wx.TreeCtrl(
            self, style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_SINGLE | wx.TR_LINES_AT_ROOT
        )
        self._convoRoot = self.convoTree.AddRoot("Conversations")
        splitRow.Add(self.convoTree, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=5)

        self.messageList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildMessageListColumns()
        splitRow.Add(self.messageList, proportion=2, flag=wx.EXPAND)

        # Explicit label for composeText -- without one, wx/NVDA falls
        # back to guessing a label from the nearest static text in tab
        # order, which is exactly how requestNotice's warning text ended
        # up getting read out as if it were this edit box's label.
        self.requestNotice = wx.StaticText(
            self, label="This is a message request. Accept it (see the conversation's menu) to view messages."
        )
        self.requestNotice.Hide()
        splitRow.Add(self.requestNotice, proportion=2, flag=wx.EXPAND)

        sizer.Add(splitRow, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        composeRow = wx.BoxSizer(wx.HORIZONTAL)
        self.composeLabel = wx.StaticText(self, label="M&essage:")
        self.composeText = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER, size=(-1, 60))
        self.emojiButton = wx.Button(self, label="Emoji...")
        self.sendButton = wx.Button(self, label="Send (Ctrl+Enter)")
        composeRow.Add(self.composeLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        composeRow.Add(self.composeText, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=5)
        composeRow.Add(self.emojiButton, flag=wx.RIGHT, border=5)
        composeRow.Add(self.sendButton)
        sizer.Add(composeRow, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.charCountLabel = wx.StaticText(self, label="")
        sizer.Add(self.charCountLabel, flag=wx.LEFT | wx.BOTTOM, border=10)

        # Separate, always-visible buttons for a request conversation --
        # NOT tucked into the context menu, swapped in place of the
        # compose row above (never both at once) via _updateActionArea().
        requestActionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.acceptButton = wx.Button(self, label="Accept")
        self.declineButton = wx.Button(self, label="Decline...")
        requestActionRow.Add(self.acceptButton, flag=wx.RIGHT, border=5)
        requestActionRow.Add(self.declineButton)
        sizer.Add(requestActionRow, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        replyRow = wx.BoxSizer(wx.HORIZONTAL)
        self.replyingToLabel = wx.StaticText(self, label="")
        self.cancelReplyButton = wx.Button(self, label="Cancel reply")
        replyRow.Add(self.replyingToLabel, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        replyRow.Add(self.cancelReplyButton)
        sizer.Add(replyRow, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.replyingToLabel.Hide()
        self.cancelReplyButton.Hide()

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self.convoTree.Bind(wx.EVT_TREE_SEL_CHANGED, self.onConvoSelected)
        self.convoTree.Bind(wx.EVT_TREE_ITEM_MENU, self.onConvoContextMenu)
        self.messageList.Bind(wx.EVT_CONTEXT_MENU, self.onMessageContextMenu)
        self.messageList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onMessageFocused)
        self.sendButton.Bind(wx.EVT_BUTTON, self.onSend)
        self.emojiButton.Bind(wx.EVT_BUTTON, self.onInsertEmoji)
        self.composeText.Bind(wx.EVT_TEXT, self.onComposeTextChanged)
        self.acceptButton.Bind(wx.EVT_BUTTON, self.onAcceptButton)
        self.declineButton.Bind(wx.EVT_BUTTON, self.onDeclineButton)
        self.cancelReplyButton.Bind(wx.EVT_BUTTON, self.onCancelReply)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._updateActionArea(None)
        self._loadFromCache()
        self._startTimeRefreshTimer()

        if self._account is None:
            nvdaUi.message("No active account. Log in from Settings first.")

    # ---------------- MainWindow integration hooks ----------------

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        # Local-only refresh (no network) -- picks up anything written
        # to the shared local DB by another tab (e.g. a message sent
        # from a popped-out ConvoTabWindow) since Chat was last shown,
        # without needing an explicit F5.
        currentConvo = self._currentConvo()
        self._loadFromCache()
        if currentConvo:
            self._selectConvoById(currentConvo["convo_id"])
        self.convoTree.SetFocus()

    def _startTimeRefreshTimer(self):
        self._timeRefreshTimer = wx.Timer(self.messageList)
        self.messageList.Bind(wx.EVT_TIMER, self._onTimeRefreshTick, self._timeRefreshTimer)
        self._timeRefreshTimer.Start(60000)

    def _onTimeRefreshTick(self, evt):
        mode, _ = timeutils.current_mode_and_pattern(db)
        if mode not in ("relative_24h", "relative_always"):
            return
        if not self.messageList.IsShownOnScreen():
            return
        for i, message in enumerate(getattr(self, "_currentMessages", [])):
            sentAt = message.get("sent_at")
            if sentAt:  # skip the still-"Sending..." optimistic row
                self.messageList.SetItem(i, 4, _format_time(sentAt))

    def _updateTitle(self):
        # Same pattern as FeedListMixin._updateTitle in feedWindow.py --
        # short tab label, full "<tab> - NVSky - <handle>" on the shared
        # MainWindow title bar only while this tab is the active one.
        accountLabel = self._account["handle"] if self._account else "no account"
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky - {accountLabel}")

    # ---------------- loading from local cache (instant, no network) ----------------

    def _loadFromCache(self):
        if getattr(self, "_reloadingConvos", False):
            # Reentrancy guard -- see plan-09.md. SelectItem() below
            # fires EVT_TREE_SEL_CHANGED synchronously (-> onConvoSelected
            # -> _showMessages), all before this method itself returns.
            # Several call sites reach _loadFromCache() via wx.CallAfter
            # from a background thread (e.g. _onCheckForUpdatesDone from
            # F5 sync) -- if one of those lands while an earlier call's
            # SelectItem-triggered chain is still unwinding, its
            # DeleteAllItems() would tear down convoTree out from under
            # the still-running outer call. This is the leading
            # hypothesis for the "wrapped C/C++ object of type TreeCtrl
            # has been deleted" crash (not yet confirmed against a live
            # repro) -- turning the reentrant call into a no-op removes
            # that overlap regardless of the exact trigger.
            return
        self._reloadingConvos = True
        try:
            self.convoTree.Freeze()
            self.convoTree.DeleteAllItems()
            self._convoRoot = self.convoTree.AddRoot("Conversations")
            allConvos = db.get_convos(self._account["id"]) if self._account else []
            # Requests always come first regardless of last-message time, so
            # they're the priority thing the user sees and acts on.
            requests = [c for c in allConvos if c.get("status") == "request"]
            accepted = [c for c in allConvos if c.get("status") != "request"]
            self._convos = requests + accepted

            for convo in self._convos:
                item = self.convoTree.AppendItem(self._convoRoot, self._convoLabel(convo))
                self.convoTree.SetItemData(item, convo["convo_id"])

            self._updateStatusBar()

            firstItem, _cookie = self.convoTree.GetFirstChild(self._convoRoot)
            if firstItem.IsOk():
                self.convoTree.SelectItem(firstItem)
            else:
                self.messageList.DeleteAllItems()
                self._currentConvoId = None
                self._updateActionArea(None)
            self.convoTree.Thaw()
        finally:
            self._reloadingConvos = False

    def _convoLabel(self, convo):
        name = convo.get("member_display_name") or convo.get("member_handle") or "Unknown"
        # db.get_unread_message_count (kept in sync with the server via
        # reconcile_message_read_state + client.mark_message_read)
        # instead of convo["unread_count"] directly -- that field only
        # changes on a full resync or the explicit "Mark read" action.
        unread = db.get_unread_message_count(self._account["id"], convo["convo_id"]) if self._account else 0
        suffix = f", {unread} unread" if unread else ""
        prefix = "(request) " if convo.get("status") == "request" else ""
        return f"{prefix}{name}{suffix}"

    def _updateStatusBar(self):
        totalUnread = 0
        if self._account:
            totalUnread = sum(
                db.get_unread_message_count(self._account["id"], c["convo_id"]) for c in self._convos
            )
        self.statusBar.SetStatusText(f"Chat {totalUnread} unread {len(self._convos)} conversations")

    def _refreshConvoLabel(self, convoId):
        # Updates just this one conversation's tree label + the overall
        # status bar, in place -- so reading a message reflects
        # immediately, without needing a full _loadFromCache() or a tab
        # switch.
        item, cookie = self.convoTree.GetFirstChild(self._convoRoot)
        while item.IsOk():
            if self.convoTree.GetItemData(item) == convoId:
                convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
                if convo is not None:
                    self.convoTree.SetItemText(item, self._convoLabel(convo))
                break
            item, cookie = self.convoTree.GetNextChild(self._convoRoot, cookie)
        self._updateStatusBar()

    def _afterMessageRead(self, convoId):
        self._refreshConvoLabel(convoId)

    def _reloadMessagesIfCurrent(self, convoId):
        convo = self._currentConvo()
        if convo and convo["convo_id"] == convoId:
            self._showMessages(convoId)

    # ---------------- full-text hooks (Alt+number / Show message...) ----------------

    def _messageFromLabel(self, message):
        myDid = self._account["did"] if self._account else None
        return "You" if message.get("sender_did") == myDid else self._senderLabel(self._currentConvoId)

    def _messageDisplayText(self, message):
        text = message.get("text", "")
        replyPreview = message.get("reply_to_text")
        if replyPreview:
            text = f"(Reply to: {replyPreview[:30]}) {text}"
        return text

    def onConvoSelected(self, evt):
        # Defensive guard added after the _openChatConvo suppression fix
        # (mainWindow.py) alone didn't stop every crash -- the tree can
        # apparently still get torn down from some other path this
        # hasn't been pinned down yet. try/except here can't fix that
        # underlying cause, but it turns a repeat into a silent no-op
        # instead of an unhandled RuntimeError that force-restarts NVDA,
        # which is the important part while the real cause is still
        # unconfirmed. Please keep reporting if this still fires -- it
        # means there's a genuine bug being papered over, not just
        # defensive style.
        try:
            if not getattr(self, "convoTree", None):
                return
            item = evt.GetItem()
            if item.IsOk() and item != self._convoRoot:
                self._showMessages(self.convoTree.GetItemData(item))
        except (RuntimeError, AttributeError):
            pass
        finally:
            evt.Skip()

    def _showMessages(self, convoId):
        self.messageList.Freeze()
        try:
            self.messageList.DeleteAllItems()
            if self._account is None:
                return
            self._currentConvoId = convoId
            messages = db.get_messages_for_convo(self._account["id"], convoId)
            # Same setting/convention as FeedListMixin._applySortOrder --
            # the DB always returns oldest-first, reverse here so Chat
            # follows Settings > Display like every other tab.
            newestFirst = db.get_ui_state("sort_order") != "oldest_first"
            if newestFirst:
                messages = list(reversed(messages))
            convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
            self._updateActionArea(convo)

            if not messages and convo is not None and convo.get("status") == "request":
                self.messageList.Hide()
                self.requestNotice.Show()
                self.Layout()
                return

            self.messageList.Show()
            self.requestNotice.Hide()
            self.Layout()

            self._currentMessages = messages
            self._messagesNewestFirst = newestFirst
            # Suppressed so rendering the list and giving the newest
            # message initial focus doesn't itself mark it read -- only the
            # user actually moving focus onto it (or Alt+number) should.
            self._suppressFocusEvents = True
            try:
                for i, message in enumerate(messages):
                    fromLabel = self._messageFromLabel(message)
                    text = uiutil.single_line(self._messageDisplayText(message))
                    mainText, moreText = _split_for_columns(text)
                    self.messageList.InsertItem(i, _describe_reactions(message.get("reactions_json")))
                    self.messageList.SetItem(i, 1, fromLabel)
                    self.messageList.SetItem(i, 2, mainText)
                    self.messageList.SetItem(i, 3, moreText)
                    self.messageList.SetItem(i, 4, _format_time(message.get("sent_at")))
                if messages:
                    newestIndex = 0 if newestFirst else len(messages) - 1
                    self.messageList.Focus(newestIndex)
                    self.messageList.Select(newestIndex)
                    self.messageList.EnsureVisible(newestIndex)
            finally:
                self._suppressFocusEvents = False
        finally:
            self.messageList.Thaw()

    def _updateActionArea(self, convo):
        # Exactly one of (compose+Send) / (Accept+Decline) is visible at
        # a time, based on the selected conversation's status -- never
        # both, never neither (unless nothing is selected at all).
        isRequest = convo is not None and convo.get("status") == "request"
        hasConvo = convo is not None

        self.composeLabel.Show(hasConvo and not isRequest)
        self.composeText.Show(hasConvo and not isRequest)
        self.emojiButton.Show(hasConvo and not isRequest)
        self.sendButton.Show(hasConvo and not isRequest)
        self.acceptButton.Show(isRequest)
        self.declineButton.Show(isRequest)
        self.Layout()

    def onAcceptButton(self, evt):
        convo = self._currentConvo()
        if convo is not None:
            self._acceptConvo(convo)

    def onDeclineButton(self, evt):
        convo = self._currentConvo()
        if convo is not None:
            self._declineConvo(convo)

    def _senderLabel(self, convoId):
        convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
        if convo is None:
            return "Them"
        return convo.get("member_display_name") or convo.get("member_handle") or "Them"

    def _currentConvo(self):
        item = self.convoTree.GetSelection()
        if not item.IsOk() or item == self._convoRoot:
            return None
        convoId = self.convoTree.GetItemData(item)
        return next((c for c in self._convos if c["convo_id"] == convoId), None)

    def _selectConvoById(self, convoId):
        item, cookie = self.convoTree.GetFirstChild(self._convoRoot)
        while item.IsOk():
            if self.convoTree.GetItemData(item) == convoId:
                self.convoTree.SelectItem(item)
                return
            item, cookie = self.convoTree.GetNextChild(self._convoRoot, cookie)

    # ---------------- conversation-level actions ----------------

    def onConvoContextMenu(self, evt):
        item = evt.GetItem()
        if not item.IsOk() or item == self._convoRoot:
            return
        self.convoTree.SelectItem(item)
        convo = self._currentConvo()
        if convo is None or convo.get("status") == "request":
            # Accept/Decline are dedicated always-visible buttons now
            # (see _updateActionArea), not menu items -- nothing else
            # applies to a not-yet-accepted request.
            return

        menu = wx.Menu()
        markReadItem = menu.Append(wx.ID_ANY, "Mark read")
        markAllReadItem = menu.Append(wx.ID_ANY, "Mark all read")
        muteItem = menu.Append(wx.ID_ANY, "Unmute" if convo.get("muted") else "Mute")
        leaveItem = menu.Append(wx.ID_ANY, "Leave conversation...")
        openTabItem = menu.Append(wx.ID_ANY, "Open in new tab...")
        self.Bind(wx.EVT_MENU, lambda e: self._markConvoRead(convo), markReadItem)
        self.Bind(wx.EVT_MENU, lambda e: self._markAllConvosRead(), markAllReadItem)
        self.Bind(wx.EVT_MENU, lambda e: self._toggleMuteConvo(convo), muteItem)
        self.Bind(wx.EVT_MENU, lambda e: self._leaveConvo(convo), leaveItem)
        self.Bind(wx.EVT_MENU, lambda e: self._openInNewTab(convo), openTabItem)

        self.PopupMenu(menu)
        menu.Destroy()

    def _acceptConvo(self, convo):
        convoId = convo["convo_id"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.accept_convo(atprotoClient, convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onConvoActionDone, "Accepted." if not error else None, error)

        threading.Thread(target=worker, daemon=True).start()

    def _declineConvo(self, convo):
        confirm = wx.MessageDialog(
            self, f"Decline this message request from {convo.get('member_handle')}?",
            "Decline request", wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        # No separate "decline" endpoint -- leaving an unaccepted convo
        # is how Bluesky itself represents declining a request.
        self._leaveConvo(convo)

    def _markConvoRead(self, convo):
        convoId = convo["convo_id"]
        # Zero the local unread state immediately instead of waiting on
        # the server round-trip. Deliberately does NOT call
        # onCheckForUpdates() after this -- a full resync could pull the
        # old unread_count back from the server if it hasn't caught up
        # yet, undoing the fix it's meant to provide.
        db.mark_convo_read_local(self._account["id"], convoId)
        db.mark_all_messages_read(self._account["id"], convoId)
        self._loadFromCache()
        self._selectConvoById(convoId)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.mark_convo_read(atprotoClient, convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onMarkReadDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onMarkReadDone(self, error):
        if error:
            log.error(f"NVSky: mark convo read failed server-side: {error}")
            nvdaUi.message(f"Marked as read locally, but the server update failed: {error}")
            return
        nvdaUi.message("Marked as read.")

    def _markAllConvosRead(self):
        if self._account is None:
            return
        for convo in self._convos:
            db.mark_convo_read_local(self._account["id"], convo["convo_id"])
            db.mark_all_messages_read(self._account["id"], convo["convo_id"])
        self._loadFromCache()
        nvdaUi.message("All conversations marked read.")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.mark_all_convos_read(atprotoClient)
            except Exception as e:
                log.error(f"NVSky: background mark-all-read failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _toggleMuteConvo(self, convo):
        convoId = convo["convo_id"]
        wasMuted = bool(convo.get("muted"))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if wasMuted:
                    client.unmute_convo(atprotoClient, convoId)
                    message = "Unmuted."
                else:
                    client.mute_convo(atprotoClient, convoId)
                    message = "Muted."
                error = None
            except Exception as e:
                error = str(e)
                message = None
            wx.CallAfter(self._onConvoActionDone, message, error)

        threading.Thread(target=worker, daemon=True).start()

    def _leaveConvo(self, convo):
        confirm = wx.MessageDialog(
            self,
            f"Leave this conversation with {convo.get('member_handle')}? "
            "It will be removed from your list.",
            "Leave conversation", wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return

        convoId = convo["convo_id"]

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.leave_convo(atprotoClient, convoId)
                db.delete_convo(self._account["id"], convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onLeaveConvoDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onLeaveConvoDone(self, error):
        if error:
            log.error(f"NVSky: leave conversation failed: {error}")
            nvdaUi.message(f"Could not leave conversation: {error}")
            return
        nvdaUi.message("Left conversation.")
        self._loadFromCache()

    @uiutil.safe_ui_callback
    def _onConvoActionDone(self, message, error):
        if error:
            log.error(f"NVSky: conversation action failed: {error}")
            nvdaUi.message(f"Action failed: {error}")
            return
        nvdaUi.message(message)
        self.onCheckForUpdates(None)

    def _openInNewTab(self, convo):
        mainWindow = self.GetTopLevelParent()
        panel = ConvoTabWindow(mainWindow.notebook, dict(convo), self._account)
        label = convo.get("member_display_name") or convo.get("member_handle") or "Conversation"
        mainWindow.addTab(panel, f"Chat: {label}", select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "conversation",
            "key": convo["convo_id"],
            "convo_id": convo["convo_id"],
        })

    # ---------------- check for updates (syncs EVERY conversation) ----------------

    def onRefreshSelectedConvo(self):
        # Plain F5 -- cheap, single-conversation refresh (the full
        # sweep is Ctrl+F5, see onCheckForUpdates below, which
        # MainWindow's checkAllOpenTabs() also calls for every tab).
        convoId = self._currentConvoId
        if not convoId or self._account is None:
            nvdaUi.message("Select a conversation first.")
            return
        nvdaUi.message("Checking for updates, please wait...")
        previousMessageCount = len(getattr(self, "_currentMessages", []))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRefreshSelectedConvoDone, convoId, error, previousMessageCount)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRefreshSelectedConvoDone(self, convoId, error, previousMessageCount):
        if error:
            log.error(f"NVSky: conversation refresh failed: {error}")
            nvdaUi.message(f"Could not check for updates: {error}")
            return
        self._reloadMessagesIfCurrent(convoId)
        name = self._senderLabel(convoId)
        newCount = len(getattr(self, "_currentMessages", []))
        if newCount <= previousMessageCount:
            nvdaUi.message(f"No new chat for {name}.")
        else:
            nvdaUi.message(f"{name} updated.")

    def _syncForBulkCheck(self, atprotoClient):
        beforeUnread = sum(c.get("unread_count") or 0 for c in self._convos)
        client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
        afterConvos = db.get_convos(self._account["id"])
        afterUnread = sum(c.get("unread_count") or 0 for c in afterConvos)
        return afterUnread != beforeUnread

    def _reloadAfterBulkCheck(self):
        currentConvo = self._currentConvo()
        self._loadFromCache()
        if currentConvo:
            self._selectConvoById(currentConvo["convo_id"])

    def onCheckForUpdates(self, evt=None):
        if self._account is None:
            nvdaUi.message("No active account.")
            return
        nvdaUi.message("Checking Chat for updates, please wait...")
        currentConvo = self._currentConvo()
        previousMessageCount = len(getattr(self, "_currentMessages", [])) if currentConvo else 0

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onCheckForUpdatesDone, error, previousMessageCount)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCheckForUpdatesDone(self, error, previousMessageCount=0):
        if error:
            log.error(f"NVSky: Chat sync failed: {error}")
            nvdaUi.message(f"Could not check Chat for updates: {error}")
            return
        currentConvo = self._currentConvo()
        self._loadFromCache()
        if currentConvo:
            self._selectConvoById(currentConvo["convo_id"])
        newMessageCount = len(getattr(self, "_currentMessages", []))
        if currentConvo and newMessageCount <= previousMessageCount:
            name = currentConvo.get("member_display_name") or currentConvo.get("member_handle") or "this conversation"
            nvdaUi.message(f"No new chat for {name}.")
        else:
            nvdaUi.message("Chat updated.")


class ConvoTabWindow(_ChatMessagePanelMixin, wx.Panel):
    """
    Pop-out single-conversation view, opened via "Open in new tab..."
    from ChatWindow's conversation context menu -- opt-in, not shown by
    default. Just a message list + its own compose box for this one
    conversation. Removable (Ctrl+W) unlike the main Chat tab.
    """

    def __init__(self, parent, convo, account):
        super().__init__(parent)

        self._convo = convo
        self._account = account
        self._replyToMessageId = None
        self._jumpBackMessageId = None
        self._currentConvoId = convo["convo_id"]
        self._suppressFocusEvents = False
        self.TAB_NAME = convo.get("member_display_name") or convo.get("member_handle") or "Conversation"
        # Generic identity MainWindow uses for its remember-last-tab
        # feature (see _getTabIdentity in mainWindow.py) -- matches the
        # "type"/"key" fields this tab is already stored under via
        # db.add_open_temp_tab (see ChatWindow._openInNewTab).
        self.TAB_TEMP_TYPE = "conversation"
        self.TAB_TEMP_KEY = convo["convo_id"]

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.messageList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self._buildMessageListColumns()
        sizer.Add(self.messageList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        composeRow = wx.BoxSizer(wx.HORIZONTAL)
        self.composeText = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER, size=(-1, 60))
        self.emojiButton = wx.Button(self, label="Emoji...")
        self.sendButton = wx.Button(self, label="Send (Ctrl+Enter)")
        composeRow.Add(self.composeText, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=5)
        composeRow.Add(self.emojiButton, flag=wx.RIGHT, border=5)
        composeRow.Add(self.sendButton)
        sizer.Add(composeRow, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.charCountLabel = wx.StaticText(self, label="")
        sizer.Add(self.charCountLabel, flag=wx.LEFT | wx.BOTTOM, border=10)

        replyRow = wx.BoxSizer(wx.HORIZONTAL)
        self.replyingToLabel = wx.StaticText(self, label="")
        self.cancelReplyButton = wx.Button(self, label="Cancel reply")
        replyRow.Add(self.replyingToLabel, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        replyRow.Add(self.cancelReplyButton)
        sizer.Add(replyRow, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.replyingToLabel.Hide()
        self.cancelReplyButton.Hide()

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizer(sizer)

        self.sendButton.Bind(wx.EVT_BUTTON, self.onSend)
        self.emojiButton.Bind(wx.EVT_BUTTON, self.onInsertEmoji)
        self.composeText.Bind(wx.EVT_TEXT, self.onComposeTextChanged)
        self.cancelReplyButton.Bind(wx.EVT_BUTTON, self.onCancelReply)
        self.messageList.Bind(wx.EVT_CONTEXT_MENU, self.onMessageContextMenu)
        self.messageList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onMessageFocused)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._updateTitle()
        self._loadMessages(moveFocus=False)
        # moveFocus=False -- only here so MainWindow.addTab()'s
        # getattr(panel, "_restoreFocusPosition", None) check has
        # something to find. Without this method, addTab()'s
        # wx.CallAfter(_focusPanel) fell back to a bare panel.SetFocus(),
        # which ran AFTER onTabActivated() below had already focused
        # messageList -- stealing focus back to the panel and causing a
        # spurious extra "<tab> tab" announcement right after the tab
        # was created. Same pattern FeedListMixin/NotificationsWindow/
        # ListsWindow/ListTabWindow already use.
        self._restoreFocusPosition(moveFocus=False)

    def _restoreFocusPosition(self, moveFocus=True):
        if moveFocus:
            self.messageList.SetFocus()

    def _updateTitle(self):
        notebook = self.GetParent()
        index = notebook.FindPage(self)
        if index != wx.NOT_FOUND:
            notebook.SetPageText(index, self.TAB_NAME)
            if index == notebook.GetSelection():
                self.GetTopLevelParent().SetTitle(f"{self.TAB_NAME} - NVSky")

    def onTabActivated(self):
        nvdaUi.message(f"{self.TAB_NAME} tab")
        self._loadMessages()  # moveFocus=True by default -- handles it internally now

    def onTabRemoved(self):
        # MainWindow.removeCurrentTab() calls this (if present) right
        # before DeletePage() -- so a conversation tab the user closes
        # with Ctrl+W doesn't come back next time NVSky opens.
        if self._account is not None:
            db.remove_open_temp_tab(self._account["id"], "conversation", self._convo["convo_id"])

    def onTabRenamed(self, newName):
        # MainWindow.renameCurrentTab() calls this (if present) right
        # after updating panel.TAB_NAME in memory -- persists the new
        # name into this tab's existing db.get_open_temp_tabs() entry
        # so it survives past this session.
        if self._account is not None:
            db.set_temp_tab_custom_name(self._account["id"], "conversation", self._convo["convo_id"], newName)

    def _afterMessageRead(self, convoId):
        self._updateStatusBar()

    def _reloadMessagesIfCurrent(self, convoId):
        # This tab only ever shows one fixed conversation, so "current"
        # is unconditional.
        self._loadMessages()

    # ---------------- full-text hooks (Alt+number / Show message...) ----------------

    def _messageFromLabel(self, message):
        myDid = self._account["did"] if self._account else None
        return "You" if message.get("sender_did") == myDid else self.TAB_NAME

    def _messageDisplayText(self, message):
        text = message.get("text", "")
        replyToId = message.get("reply_to_message_id")
        if replyToId:
            messageById = {m["message_id"]: m for m in getattr(self, "_currentMessages", [])}
            replyMessage = messageById.get(replyToId)
            if replyMessage is not None:
                preview = (replyMessage.get("text") or "")[:30]
                text = f"(Reply to: {preview}) {text}"
        return text

    def _updateStatusBar(self):
        unread = db.get_unread_message_count(self._account["id"], self._convo["convo_id"]) if self._account else 0
        messages = getattr(self, "_currentMessages", [])
        self.statusBar.SetStatusText(f"{self.TAB_NAME} {unread} unread {len(messages)} messages")

    def _loadMessages(self, moveFocus=True):
        self.messageList.Freeze()
        self.messageList.DeleteAllItems()
        messages = db.get_messages_for_convo(self._account["id"], self._convo["convo_id"])
        newestFirst = db.get_ui_state("sort_order") != "oldest_first"
        if newestFirst:
            messages = list(reversed(messages))
        self._currentMessages = messages
        self._messagesNewestFirst = newestFirst
        self._suppressFocusEvents = True
        try:
            for i, message in enumerate(messages):
                fromLabel = self._messageFromLabel(message)
                text = uiutil.single_line(self._messageDisplayText(message))
                mainText, moreText = _split_for_columns(text)
                self.messageList.InsertItem(i, _describe_reactions(message.get("reactions_json")))
                self.messageList.SetItem(i, 1, fromLabel)
                self.messageList.SetItem(i, 2, mainText)
                self.messageList.SetItem(i, 3, moreText)
                self.messageList.SetItem(i, 4, _format_time(message.get("sent_at")))
            if messages:
                newestIndex = 0 if newestFirst else len(messages) - 1
                # SetFocus() BEFORE Focus()/Select() -- reversed from
                # before. Confirmed by testing: switching tabs via
                # Ctrl+Tab/Ctrl+number and landing here announced the
                # correct item TWICE, while the equivalent tree-based
                # switch (ChatWindow's convoTree, which doesn't have
                # this quirk) never doubled. Moving real focus first,
                # then setting the item, leaves only one state change
                # for NVDA to react to instead of two.
                if moveFocus:
                    self.messageList.SetFocus()
                self.messageList.Focus(newestIndex)
                self.messageList.Select(newestIndex)
                self.messageList.EnsureVisible(newestIndex)
        finally:
            self._suppressFocusEvents = False
            self.messageList.Thaw()
        self._updateStatusBar()

    # ---------------- check for updates (syncs THIS conversation only) ----------------

    def onRefreshSelectedConvo(self):
        # This tab only ever shows one conversation, so plain F5 and
        # Ctrl+F5 mean the same thing here already.
        self.onCheckForUpdates(None)

    def _syncForBulkCheck(self, atprotoClient):
        convoId = self._convo["convo_id"]
        before = len(db.get_messages_for_convo(self._account["id"], convoId))
        client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
        after = len(db.get_messages_for_convo(self._account["id"], convoId))
        return after != before

    def _reloadAfterBulkCheck(self):
        self._loadMessages()

    def onCheckForUpdates(self, evt=None):
        convoId = self._convo["convo_id"]
        nvdaUi.message(f"Checking {self.TAB_NAME} for updates, please wait...")
        previousMessageCount = len(getattr(self, "_currentMessages", []))

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onCheckForUpdatesDone, error, previousMessageCount)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onCheckForUpdatesDone(self, error, previousMessageCount=0):
        if error:
            log.error(f"NVSky: conversation sync failed: {error}")
            nvdaUi.message(f"Could not check for updates: {error}")
            return
        self._loadMessages()
        if len(getattr(self, "_currentMessages", [])) <= previousMessageCount:
            nvdaUi.message(f"No new chat for {self.TAB_NAME}.")
        else:
            nvdaUi.message(f"{self.TAB_NAME} updated.")
