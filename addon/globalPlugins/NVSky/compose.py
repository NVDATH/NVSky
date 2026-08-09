"""
Shared "new post" dialog for NVSky.

Used both from FeedWindow's "New post" button/Ctrl+N, and from the
standalone quick-new-post GlobalPlugin script -- one dialog, two entry
points, so ESC-disabled/Ctrl+Enter/attach behavior is consistent everywhere
it's opened from.

IMPORTANT: this dialog is shown non-modally (Show(), not ShowModal()).
Calling ShowModal() on a dialog opened directly from an NVDA global script
(no other GUI event loop already running) can crash NVDA outright -- this
is the same issue previously hit and fixed in YoutubePlus. Completion is
reported via the onClosed(posted: bool) callback instead of a ShowModal()
return value.

Escape is intentionally NOT bound to close this dialog (to avoid losing
a half-typed post by accident) -- only Ctrl+W, Alt+F4 (handled by Windows
itself), or the Cancel button close it.
"""

import os
import threading
import wx

import ui as nvdaUi
from logHandler import log
from . import db
from . import client
from . import attachments as attachmentModule
from . import uiutil

POST_MAX_LENGTH = 300
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 1_000_000
IMAGE_WILDCARD = "Image files (*.jpg;*.jpeg;*.png;*.gif;*.webp)|*.jpg;*.jpeg;*.png;*.gif;*.webp"



class ComposeDialog(wx.Dialog):
    def __init__(self, parent, onClosed=None, reply_to=None, quote_of=None):
        # reply_to / quote_of: {"uri", "cid", "handle", "text", "is_reply"}
        # of the target post, or None. Only one of the two should be set.
        title = "New post"
        if reply_to:
            title = f'Reply to @{reply_to["handle"]}'
        elif quote_of:
            title = f'Quote @{quote_of["handle"]}'
        super().__init__(parent, title=title)

        account = db.get_active_account()
        self._accountHandle = account["handle"] if account else "no account"
        self._attachments = []  # list of {"path": str, "alt": str}
        self.posted = False
        self._onClosed = onClosed
        self._replyTo = reply_to
        self._quoteOf = quote_of

        sizer = wx.BoxSizer(wx.VERTICAL)

        target = reply_to or quote_of
        textFieldLabel = "Post text:"
        if target:
            kind = "Replying to" if reply_to else "Quoting"
            preview = target.get("text", "")
            if len(preview) > 100:
                preview = preview[:100] + "..."
            contextLabel = wx.StaticText(self, label=f'{kind} @{target["handle"]}: {preview}')
            sizer.Add(contextLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
            # Folded into the SAME label already sitting next to
            # textCtrl (not a new accessibility association) so Tab
            # announces it without needing NVDA+B to read the whole
            # dialog -- the separate contextLabel line above stays too,
            # for sighted use and NVDA+B users.
            textFieldLabel = f'{kind} @{target["handle"]}. Post text:'

        label = wx.StaticText(self, label=textFieldLabel)
        sizer.Add(label, flag=wx.LEFT | wx.TOP, border=10)

        self.textCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(400, 150))
        sizer.Add(self.textCtrl, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

        # Only shown once there's text -- an empty/static gauge sitting
        # there permanently isn't useful and is just extra clutter to
        # navigate past.
        self.charGauge = wx.Gauge(self, range=POST_MAX_LENGTH, size=(400, 20))
        sizer.Add(self.charGauge, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.charGauge.Hide()

        self.charCountLabel = wx.StaticText(self, label="")
        sizer.Add(self.charCountLabel, flag=wx.LEFT | wx.TOP, border=5)

        self._detectedLinkUrl = None
        self._detectedUrls = []
        self.linkPreviewCheck = wx.CheckBox(self, label="")
        sizer.Add(self.linkPreviewCheck, flag=wx.LEFT | wx.TOP, border=5)
        self.linkPreviewCheck.Hide()

        self.chooseLinkButton = wx.Button(self, label="Choose link...")
        sizer.Add(self.chooseLinkButton, flag=wx.LEFT | wx.TOP, border=5)
        self.chooseLinkButton.Hide()
        self.chooseLinkButton.Bind(wx.EVT_BUTTON, self.onChooseLink)

        self.statusLabel = wx.StaticText(self, label="")
        sizer.Add(self.statusLabel, flag=wx.LEFT | wx.TOP, border=10)

        # Attach must come before Post in tab order.
        self.attachButton = wx.Button(self, label="Attach image...")
        self.postButton = wx.Button(self, label="Post (Ctrl+Enter)")
        self.cancelButton = wx.Button(self, label="Cancel")
        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        buttonRow.Add(self.attachButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.postButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.cancelButton)
        sizer.Add(buttonRow, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

        self.SetSizerAndFit(sizer)

        self.textCtrl.Bind(wx.EVT_TEXT, self.onTextChanged)
        self.attachButton.Bind(wx.EVT_BUTTON, self.onAttach)
        self.postButton.Bind(wx.EVT_BUTTON, self.onPost)
        self.cancelButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
        self.Bind(wx.EVT_CLOSE, self.onCloseEvent)

        self._updateTitle()
        self.textCtrl.SetFocus()

    def onCloseEvent(self, evt):
        if self._onClosed:
            self._onClosed(self.posted)
        self.Destroy()

    def _updateTitle(self):
        length = client.count_graphemes(self.textCtrl.GetValue())
        count = len(self._attachments)
        if count == 1:
            suffix = " (1 picture attachment)"
        elif count > 1:
            suffix = f" ({count} picture attachments)"
        else:
            suffix = ""
        self.SetTitle(f"New post {length}/{POST_MAX_LENGTH}{suffix} - ({self._accountHandle})")

    def onTextChanged(self, evt):
        text = self.textCtrl.GetValue()
        length = client.count_graphemes(text)
        hasText = length > 0
        self.charGauge.Show(hasText)
        if hasText:
            self.charGauge.SetValue(min(length, POST_MAX_LENGTH))
            self.charCountLabel.SetLabel(f"{length} / {POST_MAX_LENGTH}")
        else:
            self.charCountLabel.SetLabel("")
        self._updateLinkPreviewCheck(text)
        self._updateTitle()
        self.Layout()
        evt.Skip()


    def _updateLinkPreviewCheck(self, text):
        # A post can't have both an external-link card and a quote/image
        # embed, so don't even offer it once either of those is present --
        # same logic create_post() itself uses to decide priority.
        if self._quoteOf or self._attachments:
            if self.linkPreviewCheck.IsShown() or self.chooseLinkButton.IsShown():
                self.linkPreviewCheck.Hide()
                self.chooseLinkButton.Hide()
                self.Layout()
            self._detectedLinkUrl = None
            self._detectedUrls = []
            return

        urls = client.find_all_urls(text)
        if urls == self._detectedUrls:
            return
        self._detectedUrls = urls

        if not urls:
            self._detectedLinkUrl = None
            self.linkPreviewCheck.Hide()
            self.chooseLinkButton.Hide()
            self.Layout()
            return

        # A post can only carry ONE external-link embed -- default to
        # the first URL found, offer a picker when there's more than one.
        self._detectedLinkUrl = urls[0]
        self.linkPreviewCheck.SetLabel(f"Attach link preview for {self._detectedLinkUrl}")
        self.linkPreviewCheck.SetValue(True)
        self.linkPreviewCheck.Show()
        self.chooseLinkButton.Show(len(urls) > 1)
        self.Layout()

    def onChooseLink(self, evt):
        dlg = wx.SingleChoiceDialog(self, "Choose which link to preview:", "Choose link", self._detectedUrls)
        if dlg.ShowModal() == wx.ID_OK:
            self._detectedLinkUrl = dlg.GetStringSelection()
            self.linkPreviewCheck.SetLabel(f"Attach link preview for {self._detectedLinkUrl}")
            self.linkPreviewCheck.SetValue(True)
            self.Layout()
        dlg.Destroy()


    def onAttach(self, evt):
        if len(self._attachments) >= MAX_IMAGES:
            self.statusLabel.SetLabel(f"You can attach up to {MAX_IMAGES} images per post.")
            return

        remaining = MAX_IMAGES - len(self._attachments)
        with wx.FileDialog(
            self, "Attach image(s)", wildcard=IMAGE_WILDCARD,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as fileDlg:
            if fileDlg.ShowModal() != wx.ID_OK:
                return
            paths = fileDlg.GetPaths()[:remaining]

        addedCount = 0
        for path in paths:
            size = os.path.getsize(path)
            if size > MAX_IMAGE_BYTES:
                self.statusLabel.SetLabel(
                    f"{os.path.basename(path)} is over Bluesky's 1MB image limit -- compress it and try again."
                )
                continue
            if path.lower().endswith(".gif"):
                nvdaUi.message("Note: Bluesky only shows the first frame of GIFs, not the animation.")

            altDlg = wx.TextEntryDialog(
                self, f"Alt text for {os.path.basename(path)} (optional but recommended):", "Image description"
            )
            altText = altDlg.GetValue() if altDlg.ShowModal() == wx.ID_OK else ""
            altDlg.Destroy()

            self._attachments.append({"path": path, "alt": altText})
            addedCount += 1

        self._updateTitle()
        self._updateLinkPreviewCheck(self.textCtrl.GetValue())
        if addedCount:
            self.statusLabel.SetLabel(f"{len(self._attachments)} image(s) attached.")

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()

        # Deliberately swallowed, not Skip()-ed -- disables the default
        # Escape-closes-dialog behavior so a half-typed post can't be
        # dismissed by accident.
        if keyCode == wx.WXK_ESCAPE:
            return

        if evt.ControlDown() and keyCode == ord("W"):
            self.Close()
            return

        # Works regardless of which control currently has focus (including
        # the multiline text box, where a plain key-down binding on the
        # control itself isn't reliable for Enter).
        if evt.ControlDown() and keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.onPost(evt)
            return

        evt.Skip()

    def onPost(self, evt):
        text = self.textCtrl.GetValue().strip()
        if not text and not self._attachments:
            self.statusLabel.SetLabel("Post text can't be empty.")
            return
        length = client.count_graphemes(text)
        if length > POST_MAX_LENGTH:
            self.statusLabel.SetLabel(f"Post is too long ({length}/{POST_MAX_LENGTH}).")
            return

        # Disable both buttons, not just Post -- disabling the focused
        # button shifts focus to the next control, and an accidental extra
        # keypress right after submitting shouldn't be able to hit Cancel
        # while the post is in flight.
        self.postButton.Disable()
        self.cancelButton.Disable()
        self.attachButton.Disable()
        self.statusLabel.SetLabel("Posting...")
        nvdaUi.message("Posting...")

        attachments = list(self._attachments)
        replyTo = self._replyTo
        quoteOf = self._quoteOf
        linkUrl = self._detectedLinkUrl if (self.linkPreviewCheck.IsShown() and self.linkPreviewCheck.GetValue()) else None

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                facets = client.build_facets(atprotoClient, text)

                reply_ref = None
                if replyTo:
                    reply_ref = client.get_reply_refs(
                        atprotoClient, replyTo["uri"], replyTo["cid"], bool(replyTo.get("is_reply"))
                    )
                quote_ref = {"uri": quoteOf["uri"], "cid": quoteOf["cid"]} if quoteOf else None

                link_card = None
                if linkUrl:
                    try:
                        link_card = client.fetch_link_card(linkUrl)
                        if link_card.get("image_url"):
                            thumbPath = attachmentModule.download_to_temp(link_card["image_url"], suffix=".jpg")
                            link_card["thumb_blob"] = client._upload_blob_dict(atprotoClient, thumbPath)
                    except Exception as e:
                        log.info(f"NVSky: link card fetch failed, posting without preview: {e}")
                        link_card = None

                client.create_post(
                    atprotoClient, text, attachments=attachments, reply_ref=reply_ref,
                    quote_ref=quote_ref, link_card=link_card, facets=facets,
                )
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onPostDone, error)

        threading.Thread(target=worker, daemon=True).start()


    @uiutil.safe_ui_callback
    def _onPostDone(self, error):
        if error:
            self.postButton.Enable()
            self.cancelButton.Enable()
            self.attachButton.Enable()
            self.statusLabel.SetLabel(f"Failed to post: {error}")
            nvdaUi.message(f"Failed to post: {error}")
            return
        self.posted = True
        nvdaUi.message("Posted successfully.")
        # Delay the actual close instead of delaying whatever happens
        # after -- Close() is what jumps focus back to the parent window
        # and triggers NVDA to announce it, cutting off the message above.
        # Buttons are already disabled, so staying open silently for a
        # moment longer doesn't let anything unwanted happen.
        wx.CallLater(1000, self.Close)