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
from . import feedWindow
from .feedWindow import RemovableTabMixin
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

    def _notifyConvoChanged(self, convoId):
        # Cross-tab live sync: propagate a locally-known state change (a
        # send, reaction, delete, or read-state change) to any OTHER
        # currently-open panel showing this same conversation. Before
        # this, a send/react/etc. only refreshed the panel where the
        # action was taken -- an already-open ChatWindow and a
        # ConvoTabWindow for the same convo (or two ConvoTabWindows,
        # after Open in new tab used twice) stayed stale until their own
        # next F5/Ctrl+F5/reopen. Cheap: the target panel just re-reads
        # from the local DB (already fresh from this action) -- no extra
        # network call. NOTE: this only covers actions taken locally in
        # one of these panels -- a background/full Chat sync (Shift+F5/
        # Ctrl+F5) discovering brand-new messages for a convo open
        # elsewhere is a separate, still-uncovered case.
        mainWindow = self.GetTopLevelParent()
        notify = getattr(mainWindow, "notifyConvoChanged", None)
        if notify:
            notify(convoId, sourcePanel=self)

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
        # Previously only moved focus (Focus/Select/EnsureVisible) and
        # relied on onMessageFocused (EVT_LIST_ITEM_FOCUSED) firing as a
        # side effect to do the actual read-marking. Per
        # uiutil.move_focus_and_check_announce's own docstring, a
        # ListCtrl only fires that event when the focused index is
        # actually CHANGING -- so when the next unread message is
        # already the focused row (e.g. the newest message, which gets
        # default focus when a conversation is first opened), Focus(i)
        # on the same index is a no-op state change: no event, nothing
        # ever gets marked read, and Space appears to do nothing. Now
        # marks read explicitly here, the same way _announceNthNewestMessage
        # already does, instead of depending on the event firing at all.
        messages = getattr(self, "_currentMessages", [])
        # Must always progress OLDEST-unread-first chronologically,
        # regardless of Settings > Display sort order -- self._currentMessages'
        # array order follows the on-screen display order (already
        # reversed for newest-first), so iterating it directly picked
        # the NEWEST unread message first when newest-first was set
        # (jumping to the top, then working backward down through
        # older messages) -- backwards from the intended "catch up from
        # where you left off" behavior. Reverse the scan order when
        # newest-first so this always starts from the oldest unread.
        newestFirst = getattr(self, "_messagesNewestFirst", False)
        indices = range(len(messages) - 1, -1, -1) if newestFirst else range(len(messages))
        for i in indices:
            message = messages[i]
            if message.get("message_id") and not message.get("is_read"):
                if uiutil.move_focus_and_check_announce(self.messageList, i):
                    parts = [
                        _describe_reactions(message.get("reactions_json")),
                        self._messageFromLabel(message),
                        self._messageDisplayText(message),
                        _format_time(message["sent_at"]) if message.get("sent_at") else "Sending...",
                    ]
                    nvdaUi.message(", ".join(p for p in parts if p))
                convoId = self._currentConvoId
                if self._account and convoId:
                    db.mark_message_read(self._account["id"], convoId, message["message_id"])
                    message["is_read"] = 1
                    self._afterMessageRead(convoId)
                    self._pushMessageReadToServer(convoId, message["message_id"])
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
                # Keep the cached convos.unread_count in step with what
                # was just pushed -- otherwise the NEXT sync's
                # reconcile_message_read_state() re-derives is_read from
                # a stale, too-high unread_count and undoes this local
                # read progress (confirmed: "read locally, but a
                # refresh brings the unread count back").
                if self._account:
                    remaining = db.get_unread_message_count(self._account["id"], convoId)
                    db.set_convo_unread_count(self._account["id"], convoId, remaining)
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
        self._notifyConvoChanged(convoId)

    # ---------------- reactions ----------------

    def _applyReactionsLocally(self, convoId, message, reactions, announcement):
        # Optimistic UI, matching the project-wide convention (already
        # used for send/mark-read/mark-convo-read): update local DB +
        # the on-screen row immediately instead of waiting on the
        # add/remove-reaction round trip AND a full sync_convo_messages
        # resync before the reaction even appeared, which is how this
        # used to work.
        reactionsJson = json.dumps(reactions)
        message["reactions_json"] = reactionsJson
        db.set_message_reactions(self._account["id"], convoId, message["message_id"], reactionsJson)
        messages = getattr(self, "_currentMessages", [])
        for i, m in enumerate(messages):
            if m.get("message_id") == message.get("message_id"):
                self.messageList.SetItem(i, 0, _describe_reactions(reactionsJson))
                break
        nvdaUi.message(announcement)
        self._notifyConvoChanged(convoId)

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
            newReactions = [r for r in existingReactions if r is not myReaction]
            self._applyReactionsLocally(convoId, message, newReactions, f"{emoji} reaction removed.")

            def worker():
                try:
                    atprotoClient = client.get_client_for_active_account()
                    client.remove_reaction(atprotoClient, convoId, messageId, emoji)
                    # Silent background reconcile -- picks up anyone
                    # else's reactions too, but doesn't re-announce our
                    # own result (already spoken above) or roll anything
                    # back on its own; that only happens on real error,
                    # see _onReactionDone.
                    client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                    error = None
                except Exception as e:
                    error = str(e)
                wx.CallAfter(self._onReactionDone, convoId, message, existingReactions, error)

            threading.Thread(target=worker, daemon=True).start()
            return

        emoji = _pick_emoji(self, "React to message")
        if emoji:
            if client.count_graphemes(emoji) != 1:
                nvdaUi.message("Please pick or enter exactly one emoji.")
                return
            newReactions = existingReactions + [{"value": emoji, "sender": {"did": myDid}}]
            self._applyReactionsLocally(convoId, message, newReactions, f"Reacted with {emoji}.")

            def worker():
                try:
                    atprotoClient = client.get_client_for_active_account()
                    client.add_reaction(atprotoClient, convoId, messageId, emoji)
                    client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                    error = None
                except Exception as e:
                    error = str(e)
                wx.CallAfter(self._onReactionDone, convoId, message, existingReactions, error)

            threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onReactionDone(self, convoId, message, previousReactions, error):
        if error:
            log.error(f"NVSky: reaction action failed: {error}")
            # Roll back the optimistic update -- the server never
            # actually applied it, so leaving the UI showing it would be
            # a lie.
            self._applyReactionsLocally(convoId, message, previousReactions, f"Reaction failed: {error}")
            return
        # sync_convo_messages already landed the authoritative state
        # (ours plus anyone else's) in the DB above -- just re-render in
        # case something besides our own reaction also changed. No
        # extra announcement -- the user already heard the result
        # immediately when they took the action.
        self._reloadMessagesIfCurrent(convoId)

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
        # LOW CONFIDENCE fix for a reported "status bar shows just
        # 'Send' after sending" -- no code was found that sets any
        # status bar to that text, so the best guess is real keyboard
        # focus lingering on the Send button after invoking it (normal
        # wx behavior after a button click) and NVDA reading the
        # button's own label instead. Returning focus to the compose
        # box is also just better chat UX regardless. Please confirm
        # this actually fixes what you saw -- if not, paste back
        # exactly how you triggered send (Send button vs Ctrl+Enter)
        # and what NVDA command you used to read the status bar.
        self.composeText.SetFocus()

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
        if not error:
            self._notifyConvoChanged(convoId)

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
        # _reloadMessagesIfCurrent alone only re-renders the currently
        # open conversation's messages -- confirmed via testing that
        # this left ChatWindow's conversation tree (unread counts, new
        # conversations, reordering) stale even though sync_convos had
        # already written fresh data to the DB. _reloadConvoListIfAny
        # is a no-op in this mixin -- ChatWindow overrides it to call
        # _loadFromCache(); ConvoTabWindow has no such list to refresh.
        self._reloadMessagesIfCurrent(self._currentConvoId)
        self._reloadConvoListIfAny()
        nvdaUi.message("All chats checked.")

    def _reloadConvoListIfAny(self):
        pass

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

def _extract_join_code(text: str) -> str:
    # LOW CONFIDENCE -- real bsky.app join-link URL format unknown, so
    # this just takes the last non-empty path segment (query string
    # stripped) as a best guess. Paste back a real link if this misparses.
    text = text.strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


class JoinGroupDialog(wx.Dialog):
    """LOW CONFIDENCE end to end -- see client.request_join_group's docstring."""

    PREVIEW_DEBOUNCE_MS = 800

    def __init__(self, parent, account, on_joined=None):
        self._account = account
        self._onJoined = on_joined
        self._previewedCode = None
        self._previewConvoId = None

        super().__init__(parent, title="Join a group", size=(420, 280))

        sizer = wx.BoxSizer(wx.VERTICAL)

        codeLabel = wx.StaticText(self, label="Paste a group join link or code:")
        self.codeText = wx.TextCtrl(self)
        sizer.Add(codeLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.codeText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        previewLabel = wx.StaticText(self, label="Group info:")
        sizer.Add(previewLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        self.previewText = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(self.previewText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.joinButton = wx.Button(self, label="Join")
        closeBtn = wx.Button(self, label="&Cancel")
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
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.codeText.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        self._debounceTimer.Stop()
        gui.mainFrame.postPopup()
        self.Destroy()

    def onCodeTextChanged(self, evt):
        self.joinButton.Disable()
        self._previewedCode = None
        self._debounceTimer.Stop()
        if _extract_join_code(self.codeText.GetValue()):
            self._debounceTimer.StartOnce(self.PREVIEW_DEBOUNCE_MS)
        else:
            self.previewText.SetValue("")

    def onDebounceTimer(self, evt):
        self._loadPreview()

    def _loadPreview(self):
        code = _extract_join_code(self.codeText.GetValue())
        if not code:
            return
        self.previewText.SetValue("Loading group info...")
        nvdaUi.message("Loading group info...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                previews = client.get_join_link_previews(atprotoClient, [code])
                error = None
            except Exception as e:
                previews = []
                error = str(e)
            wx.CallAfter(self._onPreviewDone, code, previews, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onPreviewDone(self, code, previews, error):
        if code != _extract_join_code(self.codeText.GetValue()):
            return
        if error:
            text = f"Could not load preview: {error}"
            self.previewText.SetValue(text)
            nvdaUi.message(text)
            return
        preview = previews[0] if previews else None
        if not preview or "name" not in preview:
            text = "This link is disabled or invalid."
            self.previewText.SetValue(text)
            nvdaUi.message(text)
            return
        owner = preview.get("owner") or {}
        approvalText = "Requires owner approval" if preview.get("requireApproval") else "Joins immediately"
        text = (
            f'Group: {preview.get("name", "Group")}\n'
            f'Owner: @{owner.get("handle", "unknown")}\n'
            f'Members: {preview.get("memberCount", "?")}/{preview.get("memberLimit", "?")}\n'
            f'{approvalText}'
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
        nvdaUi.message("Joining...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onJoinDone(self, result, error):
        if error:
            self.joinButton.Enable()
            nvdaUi.message(f"Could not join: {error}")
            return
        status = (result or {}).get("status")
        if status == "joined":
            nvdaUi.message("Joined the group.")
            if self._onJoined and self._previewConvoId:
                self._onJoined(self._previewConvoId)
            self.Close()
            return
        # Pending approval -- keep the dialog open with a way to back
        # out, instead of closing on a request the user might want to
        # cancel (no other UI surfaces a pending outgoing request at all).
        nvdaUi.message(f"Join request sent (status: {status or 'pending'}). You can withdraw it below.")
        self.previewText.SetValue(self.previewText.GetValue() + "\nRequest pending approval.")
        self.joinButton.Destroy()
        self.withdrawButton = wx.Button(self, label="Withdraw request")
        self.GetSizer().Add(self.withdrawButton, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)
        self.withdrawButton.Bind(wx.EVT_BUTTON, self.onWithdraw)
        self.Layout()
        self.withdrawButton.SetFocus()

    def onWithdraw(self, evt):
        self.withdrawButton.Disable()
        nvdaUi.message("Withdrawing request...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.withdraw_join_request(atprotoClient, self._previewConvoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onWithdrawDone, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onWithdrawDone(self, error):
        if error:
            self.withdrawButton.Enable()
            nvdaUi.message(f"Could not withdraw request: {error}")
            return
        nvdaUi.message("Request withdrawn.")
        self.Close()


class NewChatDialog(wx.Dialog):
    """
    Start a new 1:1 conversation, or -- if more than one recipient
    ends up added -- a new GROUP chat, decided automatically from how
    many recipients are queued when "Start chat" is pressed. Same
    search+checklist pattern as ManageGroupMembersDialog (and
    feedWindow.py's ManageMembersDialog for Lists) -- both search
    results and the accumulated recipients list are check-list boxes,
    so several people can be queued or removed in one action, kept
    deliberately consistent with those dialogs' UX.

    Requires an actual first message either way:
    chat.bsky.convo.getConvoForMembers (1:1) and
    chat.bsky.group.createGroup (group, EXPERIMENTAL -- see
    client.create_group's docstring) both only resolve/create the
    conversation record, neither puts anything in it -- an empty
    conversation isn't listed anywhere (not even on bsky.app) until a
    real message is sent to it.
    """

    def __init__(self, parent, account, on_started=None):
        self._account = account
        self._userSuggestions = []
        self._recipients = []  # list of {"did", "handle", "display_name"}
        self._onStarted = on_started

        super().__init__(parent, title="Start a new chat", size=(440, 560))

        sizer = wx.BoxSizer(wx.VERTICAL)

        userLabel = wx.StaticText(self, label="Search for a user by handle or name:")
        self.userSearchText = wx.TextCtrl(self)
        sizer.Add(userLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.userSearchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.searchResultsLabel = wx.StaticText(self, label="Search results:")
        self.searchResultsList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.searchResultsLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.searchResultsList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.addRecipientButton = wx.Button(self, label="Add checked")
        sizer.Add(self.addRecipientButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.searchResultsLabel.Hide()
        self.searchResultsList.Hide()
        self.addRecipientButton.Hide()

        self.recipientsLabel = wx.StaticText(self, label="Recipients:")
        self.recipientsList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.recipientsLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.recipientsList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)
        self.removeRecipientButton = wx.Button(self, label="Remove checked")
        sizer.Add(self.removeRecipientButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        # Nothing to show/remove until at least one recipient has been
        # added -- an empty check-list box otherwise has odd, screen-
        # reader-confusing focus/read behavior of its own.
        self.recipientsLabel.Hide()
        self.recipientsList.Hide()
        self.removeRecipientButton.Hide()

        groupNameLabel = wx.StaticText(
            self, label="Group name (optional -- set this to force a group chat even with a single recipient):"
        )
        self.groupNameText = wx.TextCtrl(self)
        sizer.Add(groupNameLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        sizer.Add(self.groupNameText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        messageLabel = wx.StaticText(self, label="First message:")
        self.messageText = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 60))
        sizer.Add(messageLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        sizer.Add(self.messageText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.startButton = wx.Button(self, label="Start chat")
        self.joinGroupButton = wx.Button(self, label="Join group...")
        closeBtn = wx.Button(self, label="&Cancel")
        actionRow.Add(self.startButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.joinGroupButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.userSearchText.Bind(wx.EVT_TEXT, self.onUserSearchChanged)
        self.addRecipientButton.Bind(wx.EVT_BUTTON, self.onAddRecipient)
        self.removeRecipientButton.Bind(wx.EVT_BUTTON, self.onRemoveRecipient)
        self.startButton.Bind(wx.EVT_BUTTON, self.onStartChat)
        self.joinGroupButton.Bind(wx.EVT_BUTTON, self.onJoinGroup)
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
            self.searchResultsList.Set([])
            self._userSuggestions = []
            self.searchResultsLabel.Hide()
            self.searchResultsList.Hide()
            self.addRecipientButton.Hide()
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
            wx.CallAfter(self._onUserSearchDone, results, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onUserSearchDone(self, results, error):
        if error:
            return
        # Already-added recipients don't need to show up again.
        addedDids = {r["did"] for r in self._recipients}
        self._userSuggestions = [r for r in results if r["did"] not in addedDids]
        hasResults = bool(self._userSuggestions)
        self.searchResultsLabel.Show(hasResults)
        self.searchResultsList.Show(hasResults)
        self.addRecipientButton.Show(hasResults)
        self.Layout()
        self.searchResultsList.Set(
            [f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in self._userSuggestions]
        )
        self.searchResultsList.CheckedItems = []
        if self._userSuggestions:
            self.searchResultsList.SetSelection(0)

    def onAddRecipient(self, evt):
        indices = list(self.searchResultsList.CheckedItems)
        if not indices:
            nvdaUi.message("No search results checked.")
            return
        toAdd = [self._userSuggestions[i] for i in indices if 0 <= i < len(self._userSuggestions)]
        if not toAdd:
            return
        wasEmpty = not self._recipients
        self._recipients.extend(toAdd)
        self.recipientsList.Set(
            [f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in self._recipients]
        )
        self.recipientsList.CheckedItems = []
        if self._recipients:
            self.recipientsList.SetSelection(0)
        self.userSearchText.SetValue("")
        self.searchResultsList.Set([])
        self._userSuggestions = []
        self.searchResultsLabel.Hide()
        self.searchResultsList.Hide()
        self.addRecipientButton.Hide()
        if wasEmpty:
            self.recipientsLabel.Show()
            self.recipientsList.Show()
            self.removeRecipientButton.Show()
        self.Layout()
        nvdaUi.message(f'Added {len(toAdd)}. {len(self._recipients)} recipient(s) total.')

    def onRemoveRecipient(self, evt):
        indices = list(self.recipientsList.CheckedItems)
        if not indices:
            nvdaUi.message("No recipients checked.")
            return
        toRemove = {self._recipients[i]["did"] for i in indices if 0 <= i < len(self._recipients)}
        if not toRemove:
            return
        removedCount = len(toRemove)
        self._recipients = [r for r in self._recipients if r["did"] not in toRemove]
        self.recipientsList.Set(
            [f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in self._recipients]
        )
        self.recipientsList.CheckedItems = []
        if not self._recipients:
            self.recipientsLabel.Hide()
            self.recipientsList.Hide()
            self.removeRecipientButton.Hide()
            self.Layout()
            self.userSearchText.SetFocus()
        nvdaUi.message(f'Removed {removedCount}. {len(self._recipients)} recipient(s) left.')

    def onStartChat(self, evt):
        if not self._recipients:
            nvdaUi.message("Add at least one recipient first.")
            return
        text = self.messageText.GetValue().strip()
        if not text:
            nvdaUi.message("Type a first message before starting the chat.")
            return
        groupName = self.groupNameText.GetValue().strip() or None
        recipients = list(self._recipients)
        # A group name typed in forces a group even with a single
        # recipient (e.g. deliberately starting a 2-person group
        # instead of a plain 1:1). LOW CONFIDENCE: whether
        # chat.bsky.group.createGroup actually accepts just one
        # initial member has never been tested -- if the server
        # rejects it, paste back the error and this will need a
        # minimum-2 guard added here instead.
        isGroup = len(recipients) > 1 or bool(groupName)
        nvdaUi.message(
            "Starting group chat, please wait..." if isGroup
            else f'Starting chat with @{recipients[0]["handle"]}, please wait...'
        )

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onStartChatDone(self, convo, error):
        if error:
            log.error(f"NVSky: start new chat failed: {error}")
            nvdaUi.message(f"Could not start chat: {error}")
            return
        if not convo or not convo.get("id"):
            log.error(f"NVSky: start chat returned no usable convo: {convo!r}")
            nvdaUi.message("Could not start chat: no conversation was returned.")
            return
        gui.mainFrame.postPopup()
        self.Destroy()
        nvdaUi.message("Chat started.")
        if self._onStarted:
            self._onStarted(convo.get("id"))

    def onJoinGroup(self, evt):
        onJoined = self._onStarted
        self.Close()
        JoinGroupDialog(gui.mainFrame, self._account, on_joined=onJoined).Show()


class JoinRequestsDialog(wx.Dialog):
    """LOW CONFIDENCE -- see client.list_join_requests's docstring."""

    def __init__(self, parent, account, convo):
        self._account = account
        self._convo = convo
        self._requests = []

        super().__init__(parent, title="Join requests", size=(420, 320))

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.requestsList = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.requestsList.InsertColumn(0, "Requester", width=200)
        self.requestsList.InsertColumn(1, "Requested", width=160)
        sizer.Add(self.requestsList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.approveButton = wx.Button(self, label="Approve")
        self.rejectButton = wx.Button(self, label="Reject")
        closeBtn = wx.Button(self, label="&Close")
        actionRow.Add(self.approveButton, flag=wx.RIGHT, border=5)
        actionRow.Add(self.rejectButton, flag=wx.RIGHT, border=5)
        actionRow.Add(closeBtn)
        sizer.Add(actionRow, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.approveButton.Bind(wx.EVT_BUTTON, lambda e: self._resolveSelected(True))
        self.rejectButton.Bind(wx.EVT_BUTTON, lambda e: self._resolveSelected(False))
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self._loadRequests()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

    def _loadRequests(self):
        nvdaUi.message("Loading join requests...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onLoadDone(self, requests_, error):
        if error:
            nvdaUi.message(f"Could not load join requests: {error}")
            return
        self._requests = requests_
        self.requestsList.DeleteAllItems()
        for i, r in enumerate(requests_):
            requestedBy = r.get("requestedBy") or {}
            self.requestsList.InsertItem(i, f'@{requestedBy.get("handle", "unknown")}')
            self.requestsList.SetItem(i, 1, _format_time(r.get("requestedAt")))
        if requests_:
            self.requestsList.Focus(0)
            self.requestsList.Select(0)
        nvdaUi.message(f'{len(requests_)} join request{"s" if len(requests_) != 1 else ""}.')

    def _resolveSelected(self, approve):
        index = _focused_list_index(self.requestsList)
        if index == -1 or index >= len(self._requests):
            nvdaUi.message("No request selected.")
            return
        member = (self._requests[index].get("requestedBy") or {}).get("did")
        if not member:
            return
        self.approveButton.Disable()
        self.rejectButton.Disable()
        nvdaUi.message("Approving..." if approve else "Rejecting...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onResolveDone(self, index, approve, error):
        self.approveButton.Enable()
        self.rejectButton.Enable()
        if error:
            nvdaUi.message(f"Could not {'approve' if approve else 'reject'}: {error}")
            return
        nvdaUi.message("Approved." if approve else "Rejected.")
        if 0 <= index < len(self._requests):
            del self._requests[index]
        self.requestsList.DeleteItem(index)
        if self.requestsList.GetItemCount():
            newIndex = min(index, self.requestsList.GetItemCount() - 1)
            self.requestsList.Focus(newIndex)
            self.requestsList.Select(newIndex)


class InviteLinkDialog(wx.Dialog):
    """LOW CONFIDENCE end to end -- see client.create_join_link's docstring.
    Two sibling panels toggled via Show/Hide instead of rebuilding
    controls in place -- rebuilding broke wx's tab order chain."""

    JOIN_RULE_OPTIONS = [
        ("Anyone can join instantly", "anyone", False),
        ("Anyone can request to join", "anyone", True),
        ("People I follow can join instantly", "followedByOwner", False),
        ("People I follow can request to join", "followedByOwner", True),
    ]
    # No real shareable URL exists for group invites -- only the code
    # itself, usable by pasting into another NVSky's Join Group dialog
    # (or the equivalent in-app paste flow). Confirmed by testing.

    def __init__(self, parent, account, convo):
        self._account = account
        self._convo = convo
        self._joinLink = None

        super().__init__(parent, title="Invite link", size=(420, 320))
        outerSizer = wx.BoxSizer(wx.VERTICAL)

        self.setupPanel = wx.Panel(self)
        self._buildSetupPanel(self.setupPanel)
        outerSizer.Add(self.setupPanel, proportion=1, flag=wx.EXPAND)

        self.resultPanel = wx.Panel(self)
        self._buildResultPanel(self.resultPanel)
        outerSizer.Add(self.resultPanel, proportion=1, flag=wx.EXPAND)
        self.resultPanel.Hide()

        self.SetSizer(outerSizer)
        self.CentreOnScreen()
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.ruleRadio.SetFocus()

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

    def _buildSetupPanel(self, panel):
        sizer = wx.BoxSizer(wx.VERTICAL)
        infoLabel = wx.StaticText(
            panel,
            label="An invite link lets people join this group without being added directly. "
                  "Your name, avatar, the group name, and member count are visible to anyone with the link. "
                  "There's no way to check whether one already exists -- Save creates a new one, or updates "
                  "the existing one if it turns out there already is one.",
        )
        sizer.Add(infoLabel, flag=wx.EXPAND | wx.ALL, border=10)

        self.ruleRadio = wx.RadioBox(
            panel, label="Who can join", choices=[o[0] for o in self.JOIN_RULE_OPTIONS], style=wx.RA_SPECIFY_ROWS
        )
        sizer.Add(self.ruleRadio, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.generateButton = wx.Button(panel, label="Save")
        self.setupCloseButton = wx.Button(panel, label="&Cancel")
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

        urlLabel = wx.StaticText(panel, label="Invite code (share this text with people you want to invite):")
        sizer.Add(urlLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.urlText = wx.TextCtrl(panel, style=wx.TE_READONLY)
        sizer.Add(self.urlText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=5)

        self.statusLabel = wx.StaticText(panel, label="")
        sizer.Add(self.statusLabel, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        self.copyButton = wx.Button(panel, label="Copy")
        self.editButton = wx.Button(panel, label="Edit permissions...")
        self.toggleButton = wx.Button(panel, label="Disable link")
        self.resultCloseButton = wx.Button(panel, label="&Close")
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
        self.setupCloseButton.SetLabel("&Cancel" if self._joinLink else "&Close")
        self.resultPanel.Hide()
        self.setupPanel.Show()
        self.Layout()
        self.ruleRadio.SetFocus()

    def _showResultPanel(self):
        self.urlText.SetValue(self._joinLink.get("code", ""))
        ruleIndex = self._ruleIndexFor(self._joinLink)
        enabled = self._joinLink.get("enabledStatus") == "enabled"
        self.statusLabel.SetLabel(f'{self.JOIN_RULE_OPTIONS[ruleIndex][0]} -- {"enabled" if enabled else "disabled"}')
        self.toggleButton.SetLabel("Disable link" if enabled else "Enable link")
        self.setupPanel.Hide()
        self.resultPanel.Show()
        self.Layout()
        self.urlText.SetFocus()
        self.urlText.SelectAll()

    def _copyUrl(self):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.urlText.GetValue()))
            wx.TheClipboard.Close()
        nvdaUi.message("Invite code copied.")

    def onGenerate(self, evt):
        _, joinRule, requireApproval = self.JOIN_RULE_OPTIONS[self.ruleRadio.GetSelection()]
        self.generateButton.Disable()
        nvdaUi.message("Saving invite link...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onGenerateDone(self, joinLink, error):
        self.generateButton.Enable()
        if error:
            nvdaUi.message(f"Could not save invite link: {error}")
            return
        if not joinLink:
            nvdaUi.message("The server didn't return link details -- check debug_dumps for the raw response.")
            return
        self._joinLink = joinLink
        nvdaUi.message("Invite link ready.")
        self._showResultPanel()

    def onToggleEnabled(self, evt):
        enabling = self._joinLink.get("enabledStatus") != "enabled"
        nvdaUi.message("Enabling invite link..." if enabling else "Disabling invite link...")

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

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onToggleDone(self, joinLink, error):
        if error:
            nvdaUi.message(f"Could not update invite link: {error}")
            return
        if not joinLink:
            nvdaUi.message("The server didn't return link details -- check debug_dumps for the raw response.")
            return
        self._joinLink = joinLink
        nvdaUi.message("Invite link updated.")
        self._showResultPanel()
        

class ManageGroupMembersDialog(feedWindow.UserActionMixin, wx.Dialog):
    """
    Add/remove members for an existing group conversation. Both the
    current-members list and the search-results list are check-list
    boxes (gui.nvdaControls.CustomCheckListBox -- same control
    feedWindow.py's ManageMembersDialog already uses for Lists), so
    several members can be added or removed in one action instead of
    one at a time -- matches that dialog's UX on purpose.

    Per-member context menu reuses UserActionMixin (View profile/
    Follow/Mute/Block etc, same as Feed tab) plus Message/Remove added
    here.

    EXPERIMENTAL -- chat.bsky.group.* has never been exercised against
    a real server in this project before now. Paste back a traceback
    if either action fails.
    """

    def __init__(self, parent, account, convo, members):
        self._account = account
        self._convo = convo
        self._members = list(members)
        self._suggestions = []

        title = convo.get("group_name") or "Group"
        super().__init__(parent, title=f"Manage members - {title}", size=(460, 520))

        sizer = wx.BoxSizer(wx.VERTICAL)

        memberLabel = wx.StaticText(self, label="Current members:")
        sizer.Add(memberLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.memberList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        self._renderMembers()
        sizer.Add(self.memberList, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        self.removeButton = wx.Button(self, label="Remove checked")
        sizer.Add(self.removeButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.removeButton.Show(bool(self._members))

        addLabel = wx.StaticText(self, label="Add member (type a handle or name to search):")
        sizer.Add(addLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.searchText = wx.TextCtrl(self)
        sizer.Add(self.searchText, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        self.suggestLabel = wx.StaticText(self, label="Search results:")
        self.suggestionList = gui.nvdaControls.CustomCheckListBox(self, choices=[])
        sizer.Add(self.suggestLabel, flag=wx.LEFT | wx.TOP, border=10)
        sizer.Add(self.suggestionList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.addButton = wx.Button(self, label="Add checked")
        sizer.Add(self.addButton, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.suggestLabel.Hide()
        self.suggestionList.Hide()
        self.addButton.Hide()

        closeBtn = wx.Button(self, label="&Close")
        sizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizer(sizer)
        self.CentreOnScreen()

        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.memberList.Bind(wx.EVT_CONTEXT_MENU, self.onMemberContextMenu)
        self.searchText.Bind(wx.EVT_TEXT, self.onSearchTextChanged)
        self.addButton.Bind(wx.EVT_BUTTON, self.onAddSuggestion)
        closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.memberList.SetFocus()

    def onMemberContextMenu(self, evt):
        index = self.memberList.GetSelection()
        if index == wx.NOT_FOUND or index >= len(self._members):
            return
        member = self._members[index]
        menu = wx.Menu()
        self._populateUserActionMenu(menu, member["did"], member["handle"], member.get("display_name"))
        menu.AppendSeparator()
        self._addMenuItem(menu, "Message", lambda: self._messageMember(member))
        self._addMenuItem(menu, "Remove from group...", lambda: self._removeSingleMember(member))
        self.PopupMenu(menu)
        menu.Destroy()

    def _messageMember(self, member):
        nvdaUi.message(f'Opening chat with @{member["handle"]}, please wait...')

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                # Best-effort pre-check for a clearer error than the
                # generic send failure -- never blocks the actual
                # attempt if this fails or the response shape is wrong.
                try:
                    availability = client.get_convo_availability(atprotoClient, [member["did"]])
                    canChat = getattr(availability, "can_chat", getattr(availability, "canChat", True))
                except Exception:
                    canChat = True
                if not canChat:
                    wx.CallAfter(self._onMessageMemberDone, None, f'@{member["handle"]} isn\'t accepting messages from you.')
                    return
                convo = client.get_or_create_convo_for_member(atprotoClient, member["did"])
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                convo = None
                error = str(e)
            wx.CallAfter(self._onMessageMemberDone, convo, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onMessageMemberDone(self, convo, error):
        if error or not convo or not convo.get("id"):
            nvdaUi.message(f"Could not open chat: {error or 'no conversation was returned'}")
            return
        openConvo = getattr(self.GetParent().GetTopLevelParent(), "_openChatConvo", None)
        self.Close()
        if openConvo:
            openConvo(convo["id"])

    def _removeSingleMember(self, member):
        try:
            index = self._members.index(member)
        except ValueError:
            return
        self.memberList.CheckedItems = [index]
        self.onRemove(None)

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
            return
        evt.Skip()

    def onClose(self, evt):
        gui.mainFrame.postPopup()
        self.Destroy()

    def _renderMembers(self):
        self.memberList.Set([f'@{m["handle"]} ({m.get("display_name") or "no display name"})' for m in self._members])
        self.memberList.CheckedItems = []
        if self._members:
            self.memberList.SetSelection(0)

    def onRemove(self, evt):
        indices = list(self.memberList.CheckedItems)
        if not indices:
            nvdaUi.message("No members checked.")
            return
        toRemove = [self._members[i] for i in indices if 0 <= i < len(self._members)]
        if not toRemove:
            return

        names = ", ".join(f'@{m["handle"]}' for m in toRemove)
        confirm = wx.MessageDialog(
            self, f"Remove {names} from this group?", "Confirm remove", wx.YES_NO | wx.NO_DEFAULT
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
        nvdaUi.message(f"Removing {len(toRemove)} member(s)...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.remove_group_members(atprotoClient, self._convo["convo_id"], list(removedDids))
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onRemoveDone, toRemove, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onRemoveDone(self, removed, error):
        if error:
            # Roll back the optimistic removal.
            self._members.extend(removed)
            self._renderMembers()
            self.removeButton.Show(bool(self._members))
            self.Layout()
            nvdaUi.message(f"Could not remove member(s): {error}")
            return
        nvdaUi.message(f"Removed {len(removed)} member(s).")

    def onSearchTextChanged(self, evt):
        wx.CallLater(400, self._runSearch, self.searchText.GetValue())

    def _runSearch(self, query):
        if query != self.searchText.GetValue():
            return
        if not query.strip():
            self.suggestionList.Set([])
            self._suggestions = []
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
        self.suggestionList.Set([f'@{r["handle"]} ({r.get("display_name") or "no display name"})' for r in self._suggestions])
        self.suggestionList.CheckedItems = []
        if self._suggestions:
            self.suggestionList.SetSelection(0)

    def onAddSuggestion(self, evt):
        indices = list(self.suggestionList.CheckedItems)
        if not indices:
            nvdaUi.message("No suggestions checked.")
            return
        toAdd = [self._suggestions[i] for i in indices if 0 <= i < len(self._suggestions)]
        if not toAdd:
            return

        # Optimistic here too, for consistency with member removal.
        self._members.extend(toAdd)
        self._renderMembers()
        self.removeButton.Show(bool(self._members))
        self.searchText.SetValue("")
        self.suggestionList.Set([])
        self.suggestLabel.Hide()
        self.suggestionList.Hide()
        self.addButton.Hide()
        self.Layout()
        nvdaUi.message(f"Adding {len(toAdd)} member(s)...")

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.add_group_members(atprotoClient, self._convo["convo_id"], [u["did"] for u in toAdd])
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onAddDone, toAdd, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onAddDone(self, added, error):
        if error:
            # Roll back.
            addedDids = {u["did"] for u in added}
            self._members = [m for m in self._members if m["did"] not in addedDids]
            self._renderMembers()
            self.removeButton.Show(bool(self._members))
            self.Layout()
            nvdaUi.message(f"Could not add member(s): {error}")
            return
        nvdaUi.message(f"Added {len(added)} member(s).")


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
        self._suppressConvoSelectEvents = False

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

        # Dedicated widget for the "this group is locked" explanation --
        # previously this reused composeLabel (SetLabel()'d in place),
        # which sat as the last VISIBLE static text before statusBar
        # whenever every other compose/request/reply control was hidden,
        # and got its text changed WHILE convoTree still had real focus
        # (selecting a conversation always fires with tree focus). Fix
        # replaced dynamic composeLabel retexting with this dedicated,
        # always-static widget instead -- composeLabel now only ever
        # shows its fixed default text. Same layout pattern as
        # requestNotice, which never had this problem.
        self.lockNotice = wx.StaticText(
            self, label="This group is locked -- no new messages can be sent."
        )
        sizer.Add(self.lockNotice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.lockNotice.Hide()

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

        # Explicit accessible name -- without one, NVDA can fall back to
        # guessing a label for the status bar from the nearest visible
        # StaticText in tab order (same class of bug as requestNotice
        # borrowing composeText's label).
        self.statusBar = wx.StatusBar(self)
        self.statusBar.SetName("Chat status")
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

    def onAccountChanged(self):
        # Same purpose as FeedListMixin.onAccountChanged in
        # feedWindow.py -- ChatWindow isn't a FeedListMixin host so it
        # needs its own copy, but reuses its own existing _updateTitle/
        # _loadFromCache.
        self._account = db.get_active_account()
        self._updateTitle()
        self._loadFromCache()

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
            return
        self._reloadingConvos = True
        try:
            previousConvoId = self._currentConvoId
            self.convoTree.Freeze()
            self._suppressConvoSelectEvents = True
            try:
                self.convoTree.DeleteAllItems()
                self._convoRoot = self.convoTree.AddRoot("Conversations")
                allConvos = db.get_convos(self._account["id"]) if self._account else []
                requests = [c for c in allConvos if c.get("status") == "request"]
                accepted = [c for c in allConvos if c.get("status") != "request"]
                self._convos = requests + accepted
                self._membersByConvo = {
                    c["convo_id"]: db.get_convo_members(self._account["id"], c["convo_id"])
                    for c in self._convos
                } if self._account else {}
                restoreItem = None
                for convo in self._convos:
                    item = self.convoTree.AppendItem(self._convoRoot, self._convoLabel(convo))
                    self.convoTree.SetItemData(item, convo["convo_id"])
                    if convo["convo_id"] == previousConvoId:
                        restoreItem = item
                self._updateStatusBar()
            finally:
                self._suppressConvoSelectEvents = False
            if restoreItem is not None:
                self.convoTree.SelectItem(restoreItem)
            else:
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
        members = self._membersByConvo.get(convo["convo_id"], [])
        name = db.describe_convo_from_members(convo, members)
        # db.get_unread_message_count (kept in sync with the server via
        # reconcile_message_read_state + client.mark_message_read)
        # instead of convo["unread_count"] directly -- that field only
        # changes on a full resync or the explicit "Mark read" action.
        unread = db.get_unread_message_count(self._account["id"], convo["convo_id"]) if self._account else 0
        suffix = f", {unread} unread" if unread else ""
        if convo.get("status") == "request":
            tags = ["request"]
        else:
            tags = []
            if convo.get("is_group"):
                tags.append("group")
            if convo.get("locked"):
                tags.append("locked")
            joinRequests = convo.get("unread_join_request_count", 0) if convo.get("is_admin") else 0
            if joinRequests:
                tags.append(f"{joinRequests} join request{'s' if joinRequests != 1 else ''}")
        prefix = f"({', '.join(tags)}) " if tags else ""
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
        self._notifyConvoChanged(convoId)

    def _reloadMessagesIfCurrent(self, convoId):
        convo = self._currentConvo()
        if convo and convo["convo_id"] == convoId:
            self._showMessages(convoId)

    def _reloadConvoListIfAny(self):
        self._loadFromCache()

    # ---------------- full-text hooks (Alt+number / Show message...) ----------------

    def _messageFromLabel(self, message):
        myDid = self._account["did"] if self._account else None
        senderDid = message.get("sender_did")
        if senderDid == myDid:
            return "You"
        return self._memberLabel(self._currentConvoId, senderDid)

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
            if getattr(self, "_suppressConvoSelectEvents", False):
                return
            item = evt.GetItem()
            if item.IsOk() and item != self._convoRoot:
                self._showMessages(self.convoTree.GetItemData(item))
        except (RuntimeError, AttributeError):
            pass
        finally:
            evt.Skip()

    def _showMessages(self, convoId):
        # previousConvoId lets the lock/request announcement below fire
        # only when the SELECTED conversation actually changes, not on
        # every resync-triggered redraw of the same conversation (Check
        # for updates, background sync, etc.) -- otherwise it would
        # repeat itself on every F5 while sitting in a locked group.
        previousConvoId = self._currentConvoId
        # previousMessageId lets a reload of the SAME conversation
        # restore the message the user was actually on, instead of
        # always snapping to the newest one. This became much more
        # visible once cross-tab notifyConvoChanged started triggering
        # reloads far more often (every send/react/mark-read/lock
        # anywhere touching this convo, not just an explicit F5) --
        # reading through older messages and having focus silently
        # yanked to the newest one moments after a background resync
        # landed was the "focus keeps jumping to the last item" report.
        previousMessageId = None
        if convoId == previousConvoId:
            oldMessages = getattr(self, "_currentMessages", [])
            focusedIndex = _focused_list_index(self.messageList)
            if focusedIndex != -1 and focusedIndex < len(oldMessages):
                previousMessageId = oldMessages[focusedIndex].get("message_id")
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
            # Refresh the status bar on every conversation switch, not
            # only after a message gets marked read or a full reload --
            # otherwise it can keep showing whatever it last had when the
            # user checks it right after switching conversations.
            self._updateStatusBar()

            isRequest = convo is not None and convo.get("status") == "request"
            isLocked = convo is not None and bool(convo.get("locked")) and not isRequest
            # lockNotice/requestNotice are plain StaticText -- neither is
            # a Tab stop, so ordinary keyboard navigation never lands on
            # them (only the review cursor does, which most users don't
            # use day to day -- confirmed by testing). Speaking this
            # explicitly on an actual selection change is what makes the
            # "why" discoverable; the convoTree's own "(locked)"/
            # "(request)" tag on the item only says THAT it's locked/a
            # request, not what that means for sending a message.
            if convoId != previousConvoId:
                if isLocked:
                    nvdaUi.message(self.lockNotice.GetLabel())
                elif isRequest:
                    nvdaUi.message(self.requestNotice.GetLabel())

            if not messages and isRequest:
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
                    targetIndex = None
                    if previousMessageId is not None:
                        targetIndex = next(
                            (i for i, m in enumerate(messages) if m.get("message_id") == previousMessageId), None
                        )
                    if targetIndex is None:
                        targetIndex = 0 if newestFirst else len(messages) - 1
                    self.messageList.Focus(targetIndex)
                    self.messageList.Select(targetIndex)
                    self.messageList.EnsureVisible(targetIndex)
            finally:
                self._suppressFocusEvents = False
        finally:
            self.messageList.Thaw()

    def _updateActionArea(self, convo):
        # Exactly one of (compose+Send) / (Accept+Decline) / (lock
        # notice) is visible at a time, based on the selected
        # conversation's status -- never more than one, never none
        # (unless nothing is selected at all). composeLabel now ONLY
        # ever shows its fixed default text and is shown/hidden in
        # lockstep with the real compose controls -- it is never
        # SetLabel()'d dynamically anymore (see lockNotice's comment in
        # __init__ for why).
        isRequest = convo is not None and convo.get("status") == "request"
        isLocked = convo is not None and bool(convo.get("locked")) and not isRequest
        hasConvo = convo is not None
        canCompose = hasConvo and not isRequest and not isLocked

        self.composeLabel.Show(canCompose)
        self.composeText.Show(canCompose)
        self.emojiButton.Show(canCompose)
        self.sendButton.Show(canCompose)
        self.lockNotice.Show(isLocked)
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

    def _memberLabel(self, convoId, did):
        # Per-SENDER lookup, not per-convo -- a group has more than one
        # possible "them", so this can't just describe the whole convo
        # the way a 1:1's single member used to.
        members = self._membersByConvo.get(convoId, [])
        member = next((m for m in members if m["did"] == did), None)
        if member is None:
            return "Them"
        return member.get("display_name") or member.get("handle") or "Them"

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

        isGroup = bool(convo.get("is_group"))
        locked = bool(convo.get("locked"))
        isAdmin = bool(convo.get("is_admin"))

        menu = wx.Menu()
        markReadItem = menu.Append(wx.ID_ANY, "Mark read")
        markAllReadItem = menu.Append(wx.ID_ANY, "Mark all read")
        muteItem = menu.Append(wx.ID_ANY, "Unmute" if convo.get("muted") else "Mute")
        self.Bind(wx.EVT_MENU, lambda e: self._markConvoRead(convo), markReadItem)
        self.Bind(wx.EVT_MENU, lambda e: self._markAllConvosRead(), markAllReadItem)
        self.Bind(wx.EVT_MENU, lambda e: self._toggleMuteConvo(convo), muteItem)

        # An owner can't Leave until the group is locked (OwnerCannotLeave
        # from the server) -- previously Leave was always offered and
        # _onLeaveConvoDone turned the failure into a clear "lock first"
        # message after the fact. Now that convo["is_admin"] reliably
        # reflects the current account's own role (see
        # client._store_convo), the one case guaranteed to fail -- an
        # unlocked group's owner -- just isn't offered at all; every
        # other case (1:1, a regular member, or an already-locked
        # group's owner) still shows it exactly as before.
        if not (isGroup and isAdmin and not locked):
            leaveItem = menu.Append(wx.ID_ANY, "Leave conversation...")
            self.Bind(wx.EVT_MENU, lambda e: self._leaveConvo(convo), leaveItem)
        if isGroup and isAdmin:
            # LOCK REQUIRES OWNER -- confirmed via a real 400
            # InsufficientRole error from a regular member. Only ever
            # offered to the admin/owner now.
            lockItem = menu.Append(wx.ID_ANY, "Unlock this group" if locked else "Lock this group")
            self.Bind(wx.EVT_MENU, lambda e: self._setGroupLocked(convo, not locked), lockItem)

        openTabItem = menu.Append(wx.ID_ANY, "Open in new tab...")
        self.Bind(wx.EVT_MENU, lambda e: self._openInNewTab(convo), openTabItem)
        if isGroup:
            manageMembersItem = menu.Append(wx.ID_ANY, "Manage members...")
            self.Bind(wx.EVT_MENU, lambda e: self._manageGroupMembers(convo), manageMembersItem)
        if isGroup and isAdmin:
            # RENAME REQUIRES OWNER TOO -- same InsufficientRole error
            # confirmed for a regular member trying to rename the group.
            editNameItem = menu.Append(wx.ID_ANY, "Edit name...")
            self.Bind(wx.EVT_MENU, lambda e: self._editGroupName(convo), editNameItem)
        if isGroup and isAdmin:
            joinRequestsItem = menu.Append(wx.ID_ANY, "Join requests...")
            self.Bind(wx.EVT_MENU, lambda e: JoinRequestsDialog(self, self._account, convo).Show(), joinRequestsItem)
            inviteLinkItem = menu.Append(wx.ID_ANY, "Invite link...")
            self.Bind(wx.EVT_MENU, lambda e: InviteLinkDialog(self, self._account, convo).Show(), inviteLinkItem)

        self.PopupMenu(menu)
        menu.Destroy()

    def _manageGroupMembers(self, convo):
        members = self._membersByConvo.get(convo["convo_id"], [])
        gui.mainFrame.prePopup()
        dlg = ManageGroupMembersDialog(self, self._account, convo, members)
        dlg.Show()

    def _editGroupName(self, convo):
        dlg = wx.TextEntryDialog(self, "New group name:", "Edit group name", value=convo.get("group_name") or "")
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        name = dlg.GetValue().strip()
        dlg.Destroy()
        if not name:
            nvdaUi.message("Group name can't be empty.")
            return
        convoId = convo["convo_id"]
        previousName = convo.get("group_name")
        convo["group_name"] = name
        db.set_convo_group_name(self._account["id"], convoId, name)
        self._refreshConvoLabel(convoId)
        nvdaUi.message(f'Group renamed to "{name}".')
        self._notifyConvoChanged(convoId)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                client.edit_group(atprotoClient, convoId, name)
                client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onEditGroupNameDone, convoId, name, previousName, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onEditGroupNameDone(self, convoId, name, previousName, error):
        if not error:
            self._reloadMessagesIfCurrent(convoId)
            return
        log.error(f"NVSky: rename group failed: {error}")
        convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
        if convo is not None:
            convo["group_name"] = previousName
        db.set_convo_group_name(self._account["id"], convoId, previousName)
        self._refreshConvoLabel(convoId)
        nvdaUi.message(f"Could not rename group: {error}")
        self._notifyConvoChanged(convoId)

    def _setGroupLocked(self, convo, locked):
        # Optimistic UI, same pattern as _markConvoRead just above: we
        # already know the resulting state (we're the one requesting
        # it), so update local DB + on-screen state immediately instead
        # of announcing progress and waiting on a full foreground Chat
        # sync (onCheckForUpdates) the way this used to work -- that
        # produced a confusing "Group locked." immediately followed by
        # "Checking Chat for updates, please wait..." for something the
        # UI already knew the answer to.
        convoId = convo["convo_id"]
        db.set_convo_locked(self._account["id"], convoId, locked)
        convo["locked"] = int(locked)
        self._refreshConvoLabel(convoId)
        if self._currentConvoId == convoId:
            self._updateActionArea(convo)
        nvdaUi.message("Group locked." if locked else "Group unlocked.")
        self._notifyConvoChanged(convoId)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                if locked:
                    client.lock_convo(atprotoClient, convoId)
                else:
                    client.unlock_convo(atprotoClient, convoId)
                # Silent background reconcile -- no "checking for
                # updates" chatter, no re-announcing the result the user
                # already heard above.
                client.sync_convo_messages(atprotoClient, self._account["id"], convoId)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onSetGroupLockedDone, convoId, locked, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback
    def _onSetGroupLockedDone(self, convoId, requestedLocked, error):
        if error:
            log.error(f"NVSky: lock/unlock group failed: {error}")
            # The optimistic update above was wrong -- roll it back and
            # say so plainly, instead of leaving the UI showing a state
            # the server never actually applied.
            db.set_convo_locked(self._account["id"], convoId, not requestedLocked)
            convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
            if convo is not None:
                convo["locked"] = int(not requestedLocked)
                self._refreshConvoLabel(convoId)
                if self._currentConvoId == convoId:
                    self._updateActionArea(convo)
            nvdaUi.message(f"Could not {'lock' if requestedLocked else 'unlock'} the group: {error}")
            self._notifyConvoChanged(convoId)
            return
        # Quiet resync already landed the authoritative state in the DB
        # above -- just re-render if this conversation is still on
        # screen. No announcement -- the user already heard the result
        # immediately when they took the action.
        self._reloadMessagesIfCurrent(convoId)

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
        name = db.describe_convo_from_members(convo, self._membersByConvo.get(convo["convo_id"], []))
        confirm = wx.MessageDialog(
            self, f"Decline this message request from {name}?",
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
        self._notifyConvoChanged(convoId)

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
        name = db.describe_convo_from_members(convo, self._membersByConvo.get(convo["convo_id"], []))
        confirm = wx.MessageDialog(
            self,
            f"Leave this conversation with {name}? "
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
            if "OwnerCannotLeave" in error:
                nvdaUi.message(
                    "You're the owner of this group -- lock it first "
                    "(right-click the conversation, Lock this group), "
                    "then you'll be able to leave."
                )
            else:
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
        members = self._membersByConvo.get(convo["convo_id"], [])
        panel = ConvoTabWindow(mainWindow.notebook, dict(convo), self._account, members, origin_key="chat")
        label = db.describe_convo_from_members(convo, members)
        mainWindow.addTab(panel, f"Chat: {label}", select=True, removable=True)
        db.add_open_temp_tab(self._account["id"], {
            "type": "conversation",
            "key": convo["convo_id"],
            "convo_id": convo["convo_id"],
            "origin_key": "chat",
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
        convo = next((c for c in self._convos if c["convo_id"] == convoId), None)
        members = self._membersByConvo.get(convoId, [])
        name = db.describe_convo_from_members(convo, members) if convo else "this conversation"
        newCount = len(getattr(self, "_currentMessages", []))
        if newCount <= previousMessageCount:
            nvdaUi.message(f"No new chat for {name}.")
        else:
            nvdaUi.message(f"{name} updated.")

    def _syncForBulkCheck(self, atprotoClient):
        # Comparing summed unread_count alone missed real changes that
        # don't move that number -- a brand-new convo already read
        # elsewhere, a message landing in an already-fully-read convo,
        # or last_message_text changing with unread staying flat --
        # confirmed as the cause of "data really did update, but
        # checkAllOpenTabs neither announced nor reloaded it." Snapshot
        # every convo's (unread_count, last_message_sent_at) instead --
        # any of those changing means something real happened.
        beforeSnapshot = {
            c["convo_id"]: (c.get("unread_count") or 0, c.get("last_message_sent_at"))
            for c in self._convos
        }
        client.sync_convos(atprotoClient, self._account["id"], self._account["did"])
        afterConvos = db.get_convos(self._account["id"])
        afterSnapshot = {
            c["convo_id"]: (c.get("unread_count") or 0, c.get("last_message_sent_at"))
            for c in afterConvos
        }
        return afterSnapshot != beforeSnapshot

    def _reloadAfterBulkCheck(self, moveFocus=True):
        # moveFocus WAS ignored here on the assumption that
        # SelectItem() alone doesn't grab real OS focus -- confirmed
        # false by testing: _loadFromCache() below does a full
        # DeleteAllItems()+rebuild of convoTree (not just SelectItem),
        # and rebuilding the native tree control's items apparently
        # does pull real focus back onto convoTree, even while the
        # user actually had real focus on messageList (reading/
        # replying while background sync ticked). Explicitly capturing
        # and restoring whichever control really had focus beforehand,
        # rather than trusting which native calls do or don't touch
        # focus, is the same defensive pattern already used elsewhere
        # (see FeedListMixin._applyFocusPosition's history).
        hadMessageListFocus = self.messageList.HasFocus()
        currentConvo = self._currentConvo()
        self._loadFromCache()
        if currentConvo:
            self._selectConvoById(currentConvo["convo_id"])
        if not moveFocus and hadMessageListFocus:
            self.messageList.SetFocus()

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
        currentConvoBefore = self._currentConvo()
        self._loadFromCache()
        if currentConvoBefore:
            self._selectConvoById(currentConvoBefore["convo_id"])
        newMessageCount = len(getattr(self, "_currentMessages", []))
        if currentConvoBefore and newMessageCount <= previousMessageCount:
            members = self._membersByConvo.get(currentConvoBefore["convo_id"], [])
            name = db.describe_convo_from_members(currentConvoBefore, members) if members or currentConvoBefore.get("is_group") else "this conversation"
            nvdaUi.message(f"No new chat for {name}.")
        else:
            nvdaUi.message("Chat updated.")


class ConvoTabWindow(RemovableTabMixin, _ChatMessagePanelMixin, wx.Panel):
    """
    Pop-out single-conversation view, opened via "Open in new tab..."
    from ChatWindow's conversation context menu -- opt-in, not shown by
    default. Just a message list + its own compose box for this one
    conversation. Removable (Ctrl+W) unlike the main Chat tab.
    """

    def __init__(self, parent, convo, account, members=None, origin_key=None):
        super().__init__(parent)

        self._convo = convo
        self._account = account
        self._replyToMessageId = None
        self._jumpBackMessageId = None
        self._currentConvoId = convo["convo_id"]
        self._suppressFocusEvents = False
        self._originTabKey = origin_key
        if members is None:
            members = db.get_convo_members(account["id"], convo["convo_id"]) if account else []
        self._members = members
        self.TAB_NAME = db.describe_convo_from_members(convo, self._members)
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

        # lock-aware compose hiding, was missing entirely before
        self.lockNotice = wx.StaticText(self, label="This group is locked -- no new messages can be sent.")
        sizer.Add(self.lockNotice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.lockNotice.Hide()

        replyRow = wx.BoxSizer(wx.HORIZONTAL)
        self.replyingToLabel = wx.StaticText(self, label="")
        self.cancelReplyButton = wx.Button(self, label="Cancel reply")
        replyRow.Add(self.replyingToLabel, proportion=1, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
        replyRow.Add(self.cancelReplyButton)
        sizer.Add(replyRow, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
        self.replyingToLabel.Hide()
        self.cancelReplyButton.Hide()

        self.statusBar = wx.StatusBar(self)
        self.statusBar.SetName(f"{self.TAB_NAME} status")
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
        self._updateActionArea()
        self._loadMessages(moveFocus=False)
        # moveFocus=False here just so addTab()'s _restoreFocusPosition check finds this method
        self._restoreFocusPosition(moveFocus=False)
        
    def _restoreFocusPosition(self, moveFocus=True):
        if moveFocus:
            self.messageList.SetFocus()

    def _updateActionArea(self):
        isLocked = bool(self._convo.get("locked"))
        self.composeText.Show(not isLocked)
        self.emojiButton.Show(not isLocked)
        self.sendButton.Show(not isLocked)
        self.lockNotice.Show(isLocked)
        self.Layout()

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
        # Return to wherever this conversation was opened FROM (the
        # permanent Chat tab) instead of leaving the notebook to fall
        # back on whatever wx.Notebook auto-selects next -- same
        # pattern as FeedPreviewTabWindow.onTabRemoved.
        if self._originTabKey:
            mainWindow = self.GetTopLevelParent()
            for panel in mainWindow.getOpenTabs():
                identity = mainWindow._getTabIdentity(panel)
                if identity and identity.get("key") == self._originTabKey:
                    mainWindow.notebook.SetSelection(mainWindow.notebook.FindPage(panel))
                    break

    def onTabRenamed(self, newName):
        # MainWindow.renameCurrentTab() calls this (if present) right
        # after updating panel.TAB_NAME in memory -- persists the new
        # name into this tab's existing db.get_open_temp_tabs() entry
        # so it survives past this session.
        if self._account is not None:
            db.set_temp_tab_custom_name(self._account["id"], "conversation", self._convo["convo_id"], newName)

    def _afterMessageRead(self, convoId):
        self._updateStatusBar()
        self._notifyConvoChanged(convoId)

    def _refreshConvoLabel(self, convoId):
        if convoId != self._convo["convo_id"]:
            return
        freshConvo = db.get_convo(self._account["id"], convoId) if self._account else None
        if freshConvo is not None:
            self._convo = freshConvo
        self.TAB_NAME = db.describe_convo_from_members(self._convo, self._members)
        self._updateTitle()

    def _reloadMessagesIfCurrent(self, convoId, moveFocus=True):
        # This tab only ever shows one fixed conversation, so "current"
        # is unconditional. moveFocus=False is used by
        # MainWindow.notifyConvoChanged (cross-tab live sync) --
        # _loadMessages(moveFocus=True) calls messageList.SetFocus(),
        # which would otherwise steal real OS focus away from whatever
        # tab the user is actually in when SOME OTHER panel is the one
        # that triggered this reload.
        self._loadMessages(moveFocus=moveFocus)

    # ---------------- full-text hooks (Alt+number / Show message...) ----------------

    def _messageFromLabel(self, message):
        myDid = self._account["did"] if self._account else None
        senderDid = message.get("sender_did")
        if senderDid == myDid:
            return "You"
        member = next((m for m in self._members if m["did"] == senderDid), None)
        if member is None:
            return self.TAB_NAME
        return member.get("display_name") or member.get("handle") or self.TAB_NAME

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
        # Refresh self._convo from the DB every time -- this tab's copy
        # is a one-time dict(convo) snapshot taken when the tab was
        # opened (see ChatWindow._openInNewTab), so it never picks up a
        # lock/unlock that happened elsewhere (ChatWindow, another
        # account instance, etc.) on its own. This call already runs on
        # every cross-tab notifyConvoChanged, F5, and tab-activation, so
        # piggybacking the lock-state refresh here means it doesn't need
        # its own separate propagation path.
        freshConvo = db.get_convo(self._account["id"], self._convo["convo_id"]) if self._account else None
        if freshConvo is not None:
            self._convo = freshConvo
        self._updateActionArea()

        previousMessageId = None
        oldMessages = getattr(self, "_currentMessages", None)
        if oldMessages:
            focusedIndex = _focused_list_index(self.messageList)
            if focusedIndex != -1 and focusedIndex < len(oldMessages):
                previousMessageId = oldMessages[focusedIndex].get("message_id")

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
                targetIndex = None
                if previousMessageId is not None:
                    targetIndex = next(
                        (i for i, m in enumerate(messages) if m.get("message_id") == previousMessageId), None
                    )
                if targetIndex is None:
                    targetIndex = 0 if newestFirst else len(messages) - 1
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
                self.messageList.Focus(targetIndex)
                self.messageList.Select(targetIndex)
                self.messageList.EnsureVisible(targetIndex)
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

    def _reloadAfterBulkCheck(self, moveFocus=True):
        self._loadMessages(moveFocus=moveFocus)

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
