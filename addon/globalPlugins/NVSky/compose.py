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

import json
import os
import threading
import wx

import gui.nvdaControls
import tones
import ui as nvdaUi
from logHandler import log
from . import db
from . import client
from . import attachments as attachmentModule
from . import uiutil
from . import soundpack

CONTENT_LABEL_DISPLAY_NAMES = {
    # Translators: Content label display name (used when self-labeling a post).
    "porn": _("Pornography"),
    # Translators: Content label display name (used when self-labeling a post).
    "sexual": _("Sexually suggestive"),
    # Translators: Content label display name (used when self-labeling a post).
    "nudity": _("Nudity"),
    # Translators: Content label display name (used when self-labeling a post).
    "graphic-media": _("Graphic media"),
}

POST_MAX_LENGTH = 300
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 1_000_000
MEDIA_WILDCARD = (
    # Translators: File-type label in the attach-media file picker (before the extension list).
    _("Media files") + " (*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.mpeg;*.mpg;*.mov;*.webm)|"
    "*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.mpeg;*.mpg;*.mov;*.webm"
)



class VideoUploadDialog(wx.Dialog):
    """
    Shown while a video attachment uploads and Bluesky processes it
    server-side. No byte-level upload progress is available from the
    SDK/API -- shows a pulsing gauge during the upload itself, then the
    job's own processing state once that's available.
    """

    def __init__(self, parent, video_path):
        # Translators: Title of the video-upload progress dialog.
        super().__init__(parent, title=_("Attaching video"))
        self._videoPath = video_path
        self._videoFilename = os.path.basename(video_path)
        self._cancelled = threading.Event()
        self.result_blob = None
        self.error = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.statusLabel = wx.StaticText(self, label="")
        sizer.Add(self.statusLabel, flag=wx.ALL | wx.EXPAND, border=10)

        # getJobStatus "progress": 0 while encoding, climbing while uploading,
        # 100 when done. Stays 0 during the initial file send (one blocking call).
        self.gauge = wx.Gauge(self, range=100, size=(320, 20))
        sizer.Add(self.gauge, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

        # Translators: Button to cancel a video upload/attach in progress.
        self.cancelButton = wx.Button(self, label=_("&Cancel"))
        sizer.Add(self.cancelButton, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.statusBar = wx.StatusBar(self)
        sizer.Add(self.statusBar, flag=wx.EXPAND)

        self.SetSizerAndFit(sizer)
        self.CentreOnScreen()

        self._currentStep = 0
        self._beepTimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._onBeepTick, self._beepTimer)
        self._beepTimer.Start(1000)  # same interval as feedWindow.py's loading beep

        self.cancelButton.Bind(wx.EVT_BUTTON, self.onCancel)
        self.Bind(wx.EVT_CLOSE, self.onCancel)
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

        self.cancelButton.SetFocus()
        # Translators: Status shown while a video file is being sent. {} is the file name.
        self._setStatus(_("Attaching {}...").format(self._videoFilename))
        self._startUpload()

    _BEEP_PITCHES = {0: 300, 1: 380, 2: 460, 3: 540, 4: 650, 5: 800, 6: 1000}

    def _onBeepTick(self, evt):
        # Beeps while waiting (long gaps between state changes); pitch rises with each phase.
        pitch = self._BEEP_PITCHES[self._currentStep]
        tones.beep(pitch, 80)

    def _setStatus(self, text):
        self.statusLabel.SetLabel(text)
        self.SetTitle(text)
        self.statusBar.SetStatusText(text)

    def onCharHook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.onCancel(evt)
            return
        evt.Skip()

    def onCancel(self, evt):
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        self._beepTimer.Stop()
        # Close at once (client-side discard; no server abort endpoint is
        # known). The worker keeps running until its blocking call returns;
        # _onDone then discards the result, even a successful blob.
        self.EndModal(wx.ID_CANCEL)

    def _startUpload(self):
        def onProgress(state, extra):
            wx.CallAfter(self._onProgress, state, extra)

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                limits = client.get_video_upload_limits(atprotoClient)
                if not limits.get("canUpload", True):
                    # Translators: Fallback message when the server doesn't explain why video uploads are blocked today.
                    message = limits.get("message") or _("You've reached today's video upload limit.")
                    wx.CallAfter(self._onDone, None, message)
                    return
                if self._cancelled.is_set():
                    wx.CallAfter(self._onDone, None, "cancelled")
                    return
                blob = client.upload_video(
                    atprotoClient, self._videoPath,
                    progress_callback=onProgress, cancel_event=self._cancelled,
                )
                error = None
            except client.VideoUploadCancelled:
                blob, error = None, "cancelled"
            except Exception as e:
                blob, error = None, str(e)
            wx.CallAfter(self._onDone, blob, error)

        threading.Thread(target=worker, daemon=True).start()

    # state name -> (status phrase, beep step). Step 0 is the dialog's own
    # initial "Attaching..." text, steps 1-5 climb with the job states, and
    # step 6 is the success beep in _onDone. Labels aim for a sense of
    # progress, not an exact mirror of the server pipeline.
    _STATE_INFO = {
        # Translators: Video upload status. {name} is the file name.
        "uploading": (_("Uploading {name}..."), 1),
        # Translators: Video upload status. {name} is the file name.
        "JOB_STATE_CREATED": (_("Queued for processing: {name}"), 2),
        # Translators: Video upload status. {name} is the file name.
        "JOB_STATE_SCANNING": (_("Scanning {name}..."), 3),
        # Translators: Video upload status. {name} is the file name.
        "JOB_STATE_ENCODING": (_("Encoding {name}..."), 4),
        # Translators: Video upload status. {name} is the file name.
        "JOB_STATE_UPLOADING": (_("Finalizing {name}..."), 5),
    }
    _FALLBACK_STEP = 4  # for any state not in _STATE_INFO -- see below

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onProgress(self, state, percent):
        if self._cancelled.is_set():
            return
        info = self._STATE_INFO.get(state)
        if info is not None:
            phrase, step = info
        else:
            # Unknown state: show a generic phrase, never the raw string; log it
            # so it can be added to _STATE_INFO.
            log.info(f"NVSky: unrecognized video job state {state!r} (percent={percent!r})")
            # Translators: Fallback video-upload status for an unrecognized server state. {name} is the file name.
            phrase, step = _("Processing {name}..."), self._FALLBACK_STEP
        self._setStatus(phrase.format(name=self._videoFilename))
        if isinstance(percent, int):
            self.gauge.SetValue(max(0, min(100, percent)))
        self._currentStep = step

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onDone(self, blob, error):
        # After a cancel the dialog is already gone: discard the result and don't touch it.
        if self._cancelled.is_set():
            return
        self._beepTimer.Stop()
        if error == "cancelled":
            self.EndModal(wx.ID_CANCEL)
        elif error:
            self.error = error
            self.EndModal(wx.ID_ABORT)
        else:
            tones.beep(self._BEEP_PITCHES[6], 150)
            self.result_blob = blob
            self.EndModal(wx.ID_OK)


class ComposeDialog(wx.Dialog):
    def __init__(self, parent, onClosed=None, reply_to=None, quote_of=None):
        # reply_to / quote_of: {"uri", "cid", "handle", "text", "is_reply"}
        # of the target post, or None. Only one of the two should be set.
        # Translators: Compose-window title for a plain new post.
        title = _("New post")
        if reply_to:
            # Translators: Compose-window title when replying. {} is the handle being replied to.
            title = _("Reply to @{}").format(reply_to["handle"])
        elif quote_of:
            # Translators: Compose-window title when quoting. {} is the handle being quoted.
            title = _("Quote @{}").format(quote_of["handle"])
        super().__init__(parent, title=title)

        account = db.get_active_account()
        # Translators: Fallback account label when no account is active.
        self._accountHandle = account["handle"] if account else _("no account")
        self._attachments = []  # list of {"path": str, "alt": str}
        self._video = None  # {"blob": dict, "path": str} once uploaded
        self.posted = False
        self._onClosed = onClosed
        self._replyTo = reply_to
        self._quoteOf = quote_of

        sizer = wx.BoxSizer(wx.VERTICAL)

        target = reply_to or quote_of
        # Translators: Label for the compose box when writing a plain new post.
        textFieldLabel = _("Post t&ext:")
        if target:
            preview = target.get("text", "")
            if len(preview) > 100:
                preview = preview[:100] + "..."
            if reply_to:
                # Translators: Context line above the compose box when replying. First {} is the handle, second {} is a preview of the original post.
                contextText = _("Replying to @{}: {}").format(target["handle"], preview)
                # Translators: Compose-box label when replying (folds the context in so Tab announces both). {} is the handle being replied to.
                textFieldLabel = _("Replying to @{}. Post t&ext:").format(target["handle"])
            else:
                # Translators: Context line above the compose box when quoting. First {} is the handle, second {} is a preview of the quoted post.
                contextText = _("Quoting @{}: {}").format(target["handle"], preview)
                # Translators: Compose-box label when quoting (folds the context in so Tab announces both). {} is the handle being quoted.
                textFieldLabel = _("Quoting @{}. Post t&ext:").format(target["handle"])
            contextLabel = wx.StaticText(self, label=contextText)
            sizer.Add(contextLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
            # contextLabel above stays too, for sighted use and NVDA+B.

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

        # Translators: Button to pick which detected link gets a preview card, when a post has more than one URL.
        self.chooseLinkButton = wx.Button(self, label=_("Choose &link..."))
        sizer.Add(self.chooseLinkButton, flag=wx.LEFT | wx.TOP, border=5)
        self.chooseLinkButton.Hide()
        self.chooseLinkButton.Bind(wx.EVT_BUTTON, self.onChooseLink)

        self.statusLabel = wx.StaticText(self, label="")
        sizer.Add(self.statusLabel, flag=wx.LEFT | wx.TOP, border=10)

        # Attach must come before Post in tab order.
        # Translators: Button to attach an image or video to a post.
        self.attachButton = wx.Button(self, label=_("Attach &media..."))
        sizer.Add(self.attachButton, flag=wx.LEFT | wx.TOP, border=10)

        # Created (and added to the sizer) BEFORE postButton/cancelButton
        # below -- tab order follows creation/sizer-add order, and this
        # needs to land right after Attach, not after Post/Cancel.
        # Translators: Label above the self-label checklist in the compose window.
        labelListLabel = wx.StaticText(self, label=_("Content &labels for this post (check any that apply):"))
        sizer.Add(labelListLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        self.labelCheckList = gui.nvdaControls.CustomCheckListBox(
            self, choices=[CONTENT_LABEL_DISPLAY_NAMES.get(k, k) for k in client.CONTENT_LABEL_KEYS]
        )
        sizer.Add(self.labelCheckList, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=10)

        # Translators: Button to submit the post. Shows the Ctrl+Enter shortcut.
        self.postButton = wx.Button(self, label=_("&Post (Ctrl+Enter)"))
        self.cancelButton = wx.Button(self, label=_("&Cancel"))
        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        buttonRow.Add(self.postButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.cancelButton)
        sizer.Add(buttonRow, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

        self.SetSizerAndFit(sizer)

        self.textCtrl.Bind(wx.EVT_TEXT, self.onTextChanged)
        self.attachButton.Bind(wx.EVT_BUTTON, self.onAttachMedia)
        self.postButton.Bind(wx.EVT_BUTTON, self.onPost)
        self.cancelButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
        self.Bind(wx.EVT_CLOSE, self.onCloseEvent)

        self._updateTitle()
        self.textCtrl.SetFocus()

    def _selectedSelfLabels(self):
        return [client.CONTENT_LABEL_KEYS[i] for i in self.labelCheckList.CheckedItems]

    def onCloseEvent(self, evt):
        if self._onClosed:
            self._onClosed(self.posted)
        self.Destroy()

    def _updateTitle(self):
        length = client.count_graphemes(self.textCtrl.GetValue())
        if self._video:
            # Translators: Window-title suffix noting a video is attached.
            suffix = _(" (1 video attachment)")
        else:
            count = len(self._attachments)
            if count == 1:
                # Translators: Window-title suffix noting exactly one picture is attached.
                suffix = _(" (1 picture attachment)")
            elif count > 1:
                # Translators: Window-title suffix noting several pictures are attached. {} is the count.
                suffix = _(" ({} picture attachments)").format(count)
            else:
                suffix = ""
        # Base title is recomputed each time so Reply/Quote titles survive typing.
        if self._replyTo:
            # Translators: Compose window title base while replying. {} is the handle being replied to.
            base = _("Reply to @{}").format(self._replyTo["handle"])
        elif self._quoteOf:
            # Translators: Compose window title base while quoting. {} is the handle being quoted.
            base = _("Quote @{}").format(self._quoteOf["handle"])
        else:
            # Translators: Compose window title base for a plain new post.
            base = _("New post")
        # Translators: Compose window title. Placeholders: base title (New post/Reply to.../Quote...), current length, max length, attachment suffix, account handle.
        self.SetTitle(_("{} {}/{}{} - ({})").format(base, length, POST_MAX_LENGTH, suffix, self._accountHandle))

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
        if self._quoteOf or self._attachments or self._video:
            if self.linkPreviewCheck.IsShown() or self.chooseLinkButton.IsShown():
                self.linkPreviewCheck.Hide()
                self.chooseLinkButton.Hide()
                self.chooseLinkButton.Disable()
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
        # Translators: Checkbox label to attach a link-preview card. {} is the URL.
        self.linkPreviewCheck.SetLabel(_("Attach link preview for {}").format(self._detectedLinkUrl))
        self.linkPreviewCheck.SetValue(True)
        self.linkPreviewCheck.Show()
        hasMultipleUrls = len(urls) > 1
        self.chooseLinkButton.Show(hasMultipleUrls)
        self.chooseLinkButton.Enable(hasMultipleUrls)
        self.Layout()

    def onChooseLink(self, evt):
        # Translators: Prompt in the choose-which-link-to-preview dialog.
        # Translators: Title of the choose-which-link-to-preview dialog.
        dlg = wx.SingleChoiceDialog(self, _("Choose which link to preview:"), _("Choose link"), self._detectedUrls)
        if dlg.ShowModal() == wx.ID_OK:
            self._detectedLinkUrl = dlg.GetStringSelection()
            # Translators: Checkbox label to attach a link-preview card. {} is the URL.
            self.linkPreviewCheck.SetLabel(_("Attach link preview for {}").format(self._detectedLinkUrl))
            self.linkPreviewCheck.SetValue(True)
            self.Layout()
        dlg.Destroy()


    def onAttachMedia(self, evt):
        if len(self._attachments) >= MAX_IMAGES:
            # Translators: Shown when trying to attach more images than the per-post limit. {} is the max count.
            self.statusLabel.SetLabel(_("You can attach up to {} images per post.").format(MAX_IMAGES))
            return

        remaining = MAX_IMAGES - len(self._attachments)
        with wx.FileDialog(
            # Translators: Title of the attach-media file picker.
            self, _("Attach image(s) or a video"), wildcard=MEDIA_WILDCARD,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as fileDlg:
            if fileDlg.ShowModal() != wx.ID_OK:
                return
            paths = fileDlg.GetPaths()

        videoPaths = [p for p in paths if os.path.splitext(p)[1].lower() in client.VIDEO_EXTENSIONS]
        imagePaths = [p for p in paths if p not in videoPaths]

        if videoPaths:
            if imagePaths or self._attachments:
                # A dialog, not a status label: this needs the user's attention.
                wx.MessageBox(
                    # Translators: Shown when trying to attach a video alongside images. Images must be removed first.
                    _(
                        "A post can't include both images and a video. "
                        "Remove the attached image(s) first if you want to attach a video instead."
                    ),
                    # Translators: Title of the can't-attach-video message box.
                    _("Can't attach video"), wx.OK | wx.ICON_INFORMATION, self,
                )
                return
            if len(videoPaths) > 1:
                wx.MessageBox(
                    # Translators: Shown when trying to attach more than one video to a post.
                    _("Only one video can be attached per post -- pick a single video file."),
                    # Translators: Title of the can't-attach-video message box.
                    _("Can't attach video"), wx.OK | wx.ICON_INFORMATION, self,
                )
                return
            self._startVideoUpload(videoPaths[0])
            return

        self._attachImages(imagePaths[:remaining])

    def _attachImages(self, paths):
        addedCount = 0
        skippedNames = []
        for path in paths:
            size = os.path.getsize(path)
            if size > MAX_IMAGE_BYTES:
                # Resizing happens at upload time (client._upload_blob_dict); just let it through.
                confirmResize = wx.MessageBox(
                    # Translators: Asks whether to auto-resize an oversized image. First {} is the file name, second {} is its size in MB.
                    _("{} is {:.1f}MB, over Bluesky's 1MB image limit. "
                      "Would you like NVSky to resize it automatically so it can be attached?").format(
                        os.path.basename(path), size / 1_000_000),
                    # Translators: Title of the image-too-large dialog.
                    _("Image too large"), wx.YES_NO | wx.ICON_QUESTION, self,
                )
                if confirmResize != wx.YES:
                    skippedNames.append(os.path.basename(path))
                    continue

            if path.lower().endswith(".gif"):
                # Translators: Announced when attaching a GIF, which Bluesky only shows as a static first frame.
                nvdaUi.message(_("Note: Bluesky only shows the first frame of GIFs, not the animation."))

            altDlg = wx.TextEntryDialog(
                self,
                # Translators: Prompt for an image's alt text. {} is the file name.
                _("Alt text for {} (optional but recommended):").format(os.path.basename(path)),
                # Translators: Title of the image alt-text dialog.
                _("Image description"),
            )
            altText = altDlg.GetValue() if altDlg.ShowModal() == wx.ID_OK else ""
            altDlg.Destroy()

            self._attachments.append({"path": path, "alt": altText})
            addedCount += 1

        self._updateTitle()
        self._updateLinkPreviewCheck(self.textCtrl.GetValue())
        if addedCount:
            # Translators: Announced after attaching one or more images. {} is the total attached so far.
            self.statusLabel.SetLabel(_("{} image(s) attached.").format(len(self._attachments)))
        if skippedNames:
            names = ", ".join(skippedNames)
            if len(skippedNames) == 1:
                # Translators: Shown when the user declines to auto-resize a single oversized image. {} is the file name.
                message = _("Skipped {} (over Bluesky's 1MB image limit).").format(names)
            else:
                # Translators: Shown when the user declines to auto-resize several oversized images. {} is a comma-separated list of file names.
                message = _("Skipped: {} (over Bluesky's 1MB image limit).").format(names)
            # Translators: Title of the image-too-large message box.
            wx.MessageBox(message, _("Image too large"), wx.OK | wx.ICON_INFORMATION, self)

    def _startVideoUpload(self, video_path):
        validation = client.validate_video_file(video_path)
        if not validation["ok"]:
            # Translators: Title of the can't-attach-video message box.
            wx.MessageBox(validation["message"], _("Can't attach video"), wx.OK | wx.ICON_WARNING, self)
            return

        # The limits check runs inside VideoUploadDialog's worker, not as a separate step.
        self.attachButton.Disable()
        dlg = VideoUploadDialog(self, video_path)
        result = dlg.ShowModal()
        blob, uploadError = dlg.result_blob, dlg.error
        dlg.Destroy()
        self.attachButton.Enable()

        if result == wx.ID_OK and blob:
            altDlg = wx.TextEntryDialog(
                self,
                # Translators: Prompt for a video's alt text. {} is the file name.
                _("Alt text for {} (optional but recommended):").format(os.path.basename(video_path)),
                # Translators: Title of the video alt-text dialog.
                _("Video description"),
            )
            altText = altDlg.GetValue() if altDlg.ShowModal() == wx.ID_OK else ""
            altDlg.Destroy()
            self._video = {"blob": blob, "path": video_path, "alt": altText}
            self._updateTitle()
            self._updateLinkPreviewCheck(self.textCtrl.GetValue())
            # Nothing more can be attached alongside a video -- disable
            # outright instead of leaving the button clickable only to
            # bounce the user with a dialog every time.
            self.attachButton.Disable()
            # Translators: Shown after a video finishes attaching. {} is the file name.
            self.statusLabel.SetLabel(_("Video attached: {}").format(os.path.basename(video_path)))
            # Translators: Announced after a video finishes attaching.
            nvdaUi.message(_("Video attached."))
        elif result == wx.ID_CANCEL:
            # Translators: Shown when the user cancels a video upload.
            self.statusLabel.SetLabel(_("Video upload cancelled."))
        else:
            # Translators: Fallback error text when a video upload fails with no specific message.
            errorText = uploadError or _("unknown error")
            # Translators: Shown when a video upload fails. {} is the error message.
            failedText = _("Video upload failed: {}").format(errorText)
            self.statusLabel.SetLabel(failedText)
            nvdaUi.message(failedText)

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
        if not text and not self._attachments and not self._video:
            # Translators: Shown when trying to post with nothing typed or attached.
            self.statusLabel.SetLabel(_("Post text can't be empty."))
            return
        length = client.count_graphemes(text)
        if length > POST_MAX_LENGTH:
            soundpack.play("max_length")
            # Translators: Shown when the post text exceeds the length limit. First {} is the current length, second {} is the max.
            self.statusLabel.SetLabel(_("Post is too long ({}/{}).").format(length, POST_MAX_LENGTH))
            return

        # Disable Cancel too: focus shifts when Post is disabled, and a stray
        # key must not cancel an in-flight post.
        self.postButton.Disable()
        self.cancelButton.Disable()
        self.attachButton.Disable()
        # Translators: Status shown while a post is being submitted.
        postingText = _("Posting...")
        self.statusLabel.SetLabel(postingText)
        nvdaUi.message(postingText)

        attachments = list(self._attachments)
        video = self._video
        replyTo = self._replyTo
        quoteOf = self._quoteOf
        selfLabels = self._selectedSelfLabels()
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

                createdPost = client.create_post(
                    atprotoClient, text, attachments=attachments, reply_ref=reply_ref,
                    quote_ref=quote_ref, link_card=link_card, facets=facets, video=video,
                    self_labels=selfLabels,
                )
                error = None
            except Exception as e:
                createdPost = None
                error = str(e)
            wx.CallAfter(self._onPostDone, error, createdPost)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onPostDone(self, error, createdPost=None):
        if error:
            self.postButton.Enable()
            self.cancelButton.Enable()
            self.attachButton.Enable(self._video is None)
            # Translators: Shown when posting fails. {} is the error message.
            failedText = _("Failed to post: {}").format(error)
            self.statusLabel.SetLabel(failedText)
            nvdaUi.message(failedText)
            soundpack.play("error")
            return
        self.posted = True
        if createdPost:
            self._insertOptimisticPost(createdPost, video=self._video)
        soundpack.play("send_post")
        # Translators: Announced after a post is successfully published.
        nvdaUi.message(_("Posted successfully."))
        # Delay Close(): it returns focus to the parent and NVDA would cut off the message above.
        wx.CallLater(1000, self.Close)

    def _insertOptimisticPost(self, createdPost, video=None):
        """
        Best-effort insert of the just-created post into the Home cache,
        then a quiet reload of an open Home tab (never moves focus).
        Counts and server-processed media URLs stay blank until the next
        sync. Failures are only logged: the post itself already succeeded.
        video is self._video; without embed_json a video post showed as empty text.
        """
        try:
            account = db.get_active_account()
            if account is None:
                return
            if db.get_author(account["did"]) is None:
                db.upsert_author(
                    did=account["did"], handle=account["handle"], display_name=None, avatar_url=None,
                )

            embedData = None
            if video:
                # Only $type and alt are known client-side; "Embed..." has no URL until a sync replaces this row.
                embedData = {"$type": "app.bsky.embed.video"}
                if video.get("alt"):
                    embedData["alt"] = video["alt"]

            db.upsert_post({
                "uri": createdPost["uri"],
                "cid": createdPost["cid"],
                "account_id": account["id"],
                "author_did": account["did"],
                "text": createdPost.get("text", ""),
                "created_at": createdPost.get("created_at"),
                "indexed_at": createdPost.get("created_at"),
                "like_count": 0,
                "repost_count": 0,
                "reply_count": 0,
                "reply_parent_uri": self._replyTo["uri"] if self._replyTo else None,
                "reply_to_did": None,
                "reply_to_handle": self._replyTo.get("handle") if self._replyTo else None,
                "is_repost": 0,
                "reposted_by_did": None,
                "reposted_by_handle": None,
                "reposted_by_display_name": None,
                "embed_json": json.dumps(embedData) if embedData else None,
                "facets_json": None,
                "quoted_text": self._quoteOf.get("text") if self._quoteOf else None,
                "quoted_author_handle": self._quoteOf.get("handle") if self._quoteOf else None,
                "viewer_like_uri": None,
                "viewer_repost_uri": None,
                "viewer_bookmarked": False,
                "viewer_thread_muted": False,
                "labels_json": json.dumps(self._selectedSelfLabels()) or None,
            })
            db.upsert_feed_item(account["id"], "home", createdPost["uri"], createdPost.get("created_at"))

            from . import get_main_window
            mainWindow = get_main_window()
            if mainWindow is None:
                return
            for panel in mainWindow.getOpenTabs():
                if getattr(panel, "TAB_KEY", None) == "home" and getattr(panel, "_feedKey", None) == "home":
                    panel._loadFromCache(reset=True)
        except Exception as e:
            log.error(f"NVSky: optimistic post insert failed (non-fatal): {e}")