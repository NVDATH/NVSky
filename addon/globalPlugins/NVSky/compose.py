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

import tones
import ui as nvdaUi
from logHandler import log
from . import db
from . import client
from . import attachments as attachmentModule
from . import uiutil
from . import soundpack

POST_MAX_LENGTH = 300
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 1_000_000
# Translators: File-type label in the attach-media file picker (before the extension list).
IMAGE_WILDCARD = _("Image files") + " (*.jpg;*.jpeg;*.png;*.gif;*.webp)|*.jpg;*.jpeg;*.png;*.gif;*.webp"
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

        # Real percent IS available from getJobStatus's own "progress"
        # field (confirmed via testing: goes 0 during
        # JOB_STATE_ENCODING, then a real climbing number during
        # JOB_STATE_UPLOADING, then 100 at JOB_STATE_COMPLETED) -- no
        # pulsing/indeterminate mode needed. Starts at 0 and stays
        # there during the plain "uploading" phase (before any poll
        # has happened yet -- there's genuinely no percent for that
        # part, the file send itself is one blocking call).
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
        # Continuous beeping while waiting, same pattern as
        # feedWindow.py's _startLoadingBeep/_onLoadingBeepTick -- a
        # single one-shot beep at each state change doesn't tell the
        # user anything is still happening during the long stretches
        # between state changes, which is most of the wait. 6 distinct
        # pitch levels (0-5) so each processing phase sounds clearly
        # different from the last, giving a sense of forward progress
        # even though the underlying percentages aren't reliable.
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
        # Close IMMEDIATELY -- previously waited for the background
        # worker to notice cancellation (up to a full 3s poll
        # interval), which felt like Cancel didn't respond at once.
        # Confirmed no server-side "abort this video job" endpoint
        # exists (LOW CONFIDENCE, no such lexicon method found) -- this
        # is a client-side discard only. The worker thread keeps
        # running in the background (a blocking network call can't be
        # interrupted mid-flight) and will eventually call _onDone via
        # wx.CallAfter, but _onDone checks self._cancelled first and
        # simply discards whatever it got, even a fully successful
        # blob -- the user asked to cancel, so nothing gets attached
        # regardless of how far the upload/processing had gotten.
        # No spoken "Cancelling..." needed either -- the user pressed
        # Cancel themselves; focus returning to ComposeDialog already
        # tells them it worked.
        self.EndModal(wx.ID_CANCEL)
        # Dialog closes once the worker notices and reports back (see
        # _onDone) -- the upload POST can't be interrupted mid-flight,
        # only the polling phase actually stops.

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

    # state name -> (status phrase, beep step). Step climbs 0..4 across
    # the known processing states; step 5 (highest pitch) is reserved
    # for the final "attached" announcement in _onVideoLimitsChecked-
    # adjacent code, not fired from here. Exact server semantics behind
    # each state name aren't confirmed -- these labels are chosen to
    # feel like clear forward progress to the user rather than to
    # precisely mirror the backend's own processing pipeline.
    # Step 0 is reserved for the dialog's OWN initial "Attaching..."
    # text (set in __init__, before _startUpload even calls
    # client.upload_video) -- the "uploading" state below, fired once
    # the actual network send begins, is a genuinely later phase and
    # needs its own step. Confirmed by testing: sharing step 0 between
    # both made "Attaching..." and "Uploading..." sound identical even
    # though the displayed text visibly changed between them.
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
            # An unrecognized state (server added a new one, or a
            # naming variant not seen during testing) -- never show the
            # raw state string to the user, just a generic "still
            # working on it" phrase with a mid-range beep, and log the
            # real value so this can be added to _STATE_INFO later.
            log.info(f"NVSky: unrecognized video job state {state!r} (percent={percent!r})")
            # Translators: Fallback video-upload status for an unrecognized server state. {name} is the file name.
            phrase, step = _("Processing {name}..."), self._FALLBACK_STEP
        self._setStatus(phrase.format(name=self._videoFilename))
        if isinstance(percent, int):
            self.gauge.SetValue(max(0, min(100, percent)))
        self._currentStep = step

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onDone(self, blob, error):
        # The dialog may already be gone by the time this fires (see
        # onCancel -- it closes immediately, doesn't wait for this).
        # If the user already cancelled, discard whatever came back --
        # including a fully successful blob -- and don't touch the
        # (possibly already-destroyed) dialog again.
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
        # Translators: Button to submit the post. Shows the Ctrl+Enter shortcut.
        self.postButton = wx.Button(self, label=_("&Post (Ctrl+Enter)"))
        self.cancelButton = wx.Button(self, label=_("&Cancel"))
        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        buttonRow.Add(self.attachButton, flag=wx.RIGHT, border=5)
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
        # BUG FIX: this used to always rebuild the title as "New post
        # ...", overwriting the Reply/Quote title set in __init__ the
        # moment the user typed a single character -- base is now
        # recomputed from self._replyTo/self._quoteOf every time, same
        # as __init__'s own initial title logic.
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
        self.chooseLinkButton.Show(len(urls) > 1)
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
                # A dialog, not just a status label -- this is a real
                # decision point (which images to drop?), not a passive
                # status update, and speech for a plain label can be
                # gone before the user's caught up to what happened.
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
        oversizedNames = []
        for path in paths:
            size = os.path.getsize(path)
            if size > MAX_IMAGE_BYTES:
                oversizedNames.append(os.path.basename(path))
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
        if oversizedNames:
            names = ", ".join(oversizedNames)
            if len(oversizedNames) == 1:
                # Translators: Shown when a single attached image exceeds Bluesky's size limit. {} is the file name.
                message = _("{} is over Bluesky's 1MB image limit -- compress and try again.").format(names)
            else:
                # Translators: Shown when several attached images exceed Bluesky's size limit. {} is a comma-separated list of file names.
                message = _("{} are over Bluesky's 1MB image limit -- compress and try again.").format(names)
            # Translators: Title of the image-too-large message box.
            wx.MessageBox(message, _("Image too large"), wx.OK | wx.ICON_WARNING, self)

    def _startVideoUpload(self, video_path):
        validation = client.validate_video_file(video_path)
        if not validation["ok"]:
            # Translators: Title of the can't-attach-video message box.
            wx.MessageBox(validation["message"], _("Can't attach video"), wx.OK | wx.ICON_WARNING, self)
            return

        # Straight into the progress dialog -- no separate "checking
        # limits" status text first. The limits check still happens
        # (inside VideoUploadDialog's own worker, before the real
        # upload starts) but the user doesn't need to see that as a
        # distinct step; they just want pass/fail.
        self.attachButton.Disable()
        dlg = VideoUploadDialog(self, video_path)
        result = dlg.ShowModal()
        blob, uploadError = dlg.result_blob, dlg.error
        dlg.Destroy()
        self.attachButton.Enable()

        log.info(f"NVSky DEBUG video upload dialog result: result={result!r} wx.ID_OK={wx.ID_OK!r} blob_is_none={blob is None!r} uploadError={uploadError!r}")

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

        # Disable both buttons, not just Post -- disabling the focused
        # button shifts focus to the next control, and an accidental extra
        # keypress right after submitting shouldn't be able to hit Cancel
        # while the post is in flight.
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
                )
                error = None
            except Exception as e:
                createdPost = None
                error = str(e)
            wx.CallAfter(self._onPostDone, error, createdPost)

        threading.Thread(target=worker, daemon=True).start()


    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onPostDone(self, error, createdPost=None):
        if error:
            self.postButton.Enable()
            self.cancelButton.Enable()
            self.attachButton.Enable()
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
        # Delay the actual close instead of delaying whatever happens
        # after -- Close() is what jumps focus back to the parent window
        # and triggers NVDA to announce it, cutting off the message above.
        # Buttons are already disabled, so staying open silently for a
        # moment longer doesn't let anything unwanted happen.
        wx.CallLater(1000, self.Close)

    def _insertOptimisticPost(self, createdPost, video=None):
        """
        Best-effort local insert of the just-created post into the Home
        (Following) feed cache, then a quiet in-place re-render of an
        already-open Home tab IF it's currently showing that feed --
        never moves real focus. Fields the SDK's create_record response
        doesn't give us client-side (like/repost counts, server-
        processed image thumbnail URLs, video playlist URL) are left
        blank/zero here; the next real sync (manual or background)
        fills them in properly. Any failure here is silently swallowed
        -- the post itself already succeeded server-side regardless of
        whether this cosmetic step works.

        video, if given, is self._video ({"blob", "path"}) -- confirmed
        by testing that omitting embed_json entirely for a video post
        left the row showing as empty text with no embed at all until
        the next real sync/F5 corrected it.
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
                # No playlist/thumbnail URL available client-side yet
                # (that's only known once the server finishes its own
                # processing) -- $type alone is enough for
                # _describe_embed() to correctly show "Video" instead
                # of blank; "View embed" won't have a URL to open until
                # a real sync replaces this row. alt text IS known
                # immediately though (the user just typed it) -- was
                # missing here even though self._video already carried
                # it, a leftover gap from adding the alt-text prompt in
                # a separate later patch.
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