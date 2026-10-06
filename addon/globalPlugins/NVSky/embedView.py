"""
EmbedViewMixin for NVSky: the shared "Embed..." submenu and its
open/send/copy handlers for images, video, and links.

feedWindow is imported as a module (not by name) because it imports this
file; its helpers are only touched inside method bodies.
"""
import json
import webbrowser

import wx
import core
import speech

import ui as nvdaUi
from logHandler import log

from . import db
from . import soundpack
from . import uiutil
from . import attachments
from . import feedWindow


class EmbedViewMixin:
    """
    Shared "Embed..." submenu + open/send/copy handlers, mixed into every
    tab that shows posts.
    """

    def _buildViewEmbedMenu(self, post):
        embed_json = post.get("embed_json")
        if not embed_json:
            return None
        try:
            embed = json.loads(embed_json)
        except (ValueError, TypeError):
            return None

        menu = wx.Menu()
        added = False

        images = [img for img in (embed.get("images") or []) if img.get("fullsize_url")]
        if len(images) == 1:
            url = images[0]["fullsize_url"]
            # Translators: Context menu item to open an attached image in the default viewer.
            self._addMenuItem(menu, _("&Open image"), lambda: self._openEmbedImage(url))
            # Translators: Context menu item to send an attached image to the Be My Eyes app.
            self._addMenuItem(menu, _("&Send image to Be My Eyes"), lambda: self._sendEmbedImageToBeMyEyes(url))
            # Translators: Context menu item to copy an attached image to the clipboard.
            self._addMenuItem(menu, _("&Copy image to clipboard"), lambda: self._copyEmbedImageToClipboard(url))
            added = True
        elif len(images) > 1:
            for i, img in enumerate(images):
                url = img["fullsize_url"]
                # "&" forces the digit itself as the access key -- the
                # default would use the first letter ("I") for every
                # item, since they'd all start with "Image".
                # Translators: Submenu label for one of several attached images. {} is the image's 1-based index.
                label = _("Image &{}").format(i + 1)
                imageMenu = wx.Menu()
                # Translators: Context menu item to open an attached image in the default viewer.
                self._addMenuItem(imageMenu, _("&Open"), lambda url=url: self._openEmbedImage(url))
                # Translators: Context menu item to send an attached image to the Be My Eyes app.
                self._addMenuItem(imageMenu, _("&Send to Be My Eyes"), lambda url=url: self._sendEmbedImageToBeMyEyes(url))
                # Translators: Context menu item to copy an attached image to the clipboard.
                self._addMenuItem(imageMenu, _("&Copy to clipboard"), lambda url=url: self._copyEmbedImageToClipboard(url))
                menu.AppendSubMenu(imageMenu, label)
                added = True

        videoUrl = embed.get("video_url")
        if videoUrl:
            # Translators: Context menu item to open an attached video in the default player.
            self._addMenuItem(menu, _("Open &video"), lambda: self._openEmbedVideo(videoUrl))
            # Translators: Context menu item to copy an attached video's URL.
            self._addMenuItem(menu, _("Copy video &URL"), lambda: self._copyEmbedUrl(videoUrl, _("Video URL")))
            added = True
        elif embed.get("$type") == "app.bsky.embed.video":
            # compose.py's optimistic insert only knows the embed type, not
            # the playlist URL (that arrives with the next sync).
            self._addMenuItem(
                menu,
                # Translators: Disabled-looking menu item shown when a just-posted video hasn't finished processing on the server yet.
                _("Video not ready yet (Check for updates first)"),
                # Translators: Announced when trying to view a video embed that hasn't finished processing yet.
                lambda: nvdaUi.message(_("This video was just posted -- press F5 to refresh, then try again.")),
            )
            added = True

        linkUrl = embed.get("link_url")
        if linkUrl:
            title = embed.get("link_title") or linkUrl
            # Translators: Context menu item to open a post's link preview in a browser. {} is the link's title or URL.
            self._addMenuItem(menu, _("Open &link: {}").format(title), lambda: self._openWebLink(linkUrl))
            # Translators: Context menu item to copy a post's link-preview URL.
            self._addMenuItem(menu, _("Copy link &URL"), lambda: self._copyEmbedUrl(linkUrl, _("Link URL")))
            added = True

        if not added:
            menu.Destroy()
            return None
        return menu

    def _openWebLink(self, url):
        # Web links only -- never hand an arbitrary URI scheme to the shell.
        if not str(url).lower().startswith(("http://", "https://")):
            # Translators: Announced when a post's link uses a scheme other than http/https and is refused.
            feedWindow._announce_now(_("Only http and https links can be opened."))
            return
        webbrowser.open(url)

    def _copyEmbedUrl(self, url, label):
        self._copyToClipboard(url)
        # Translators: Announced after copying an embed's URL to the clipboard. {} is already-translated (e.g. "Video URL").
        feedWindow._announce_now(_("{} copied to clipboard.").format(label))

    def _openEmbedImage(self, url):
        # Translators: Announced while downloading an image to open it.
        feedWindow._announce_now(_("Downloading image, please wait..."))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after an image finishes opening.
            wx.CallAfter(self._onActionDone, _("Image opened.") if not error else None, error)

        uiutil.start_worker(worker)

    def _sendEmbedImageToBeMyEyes(self, url):
        # Translators: Announced while downloading an image to send it to Be My Eyes.
        feedWindow._announce_now(_("Downloading image, please wait..."))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                ok = attachments.send_to_bemyeyes(path)
                # Translators: Error shown when Be My Eyes can't be launched.
                error = None if ok else _("Could not launch Be My Eyes. Is it installed?")
            except Exception as e:
                error = str(e)
            # Translators: Announced after an image is sent to Be My Eyes.
            wx.CallAfter(self._onActionDone, _("Sent to Be My Eyes.") if not error else None, error)

        uiutil.start_worker(worker)

    def _copyEmbedImageToClipboard(self, url):
        def announce_start():
            speech.cancelSpeech()
            # Translators: Announced while downloading an image to copy it to the clipboard.
            nvdaUi.message(_("Downloading image, please wait..."))

        core.callLater(150, announce_start)

        def copy_and_notify(path):
            try:
                ok = attachments.copy_image_to_clipboard(path)
                # Translators: Error shown when the downloaded image file can't be read for clipboard copy.
                error = None if ok else _("Could not read the downloaded image.")
                # Translators: Announced after an image is copied to the clipboard.
                self._onActionDone(_("Image copied to clipboard.") if not error else None, error)
            except Exception as e:
                self._onActionDone(None, str(e))

        def worker():
            try:
                path = attachments.download_to_temp(url, suffix=".jpg")
                wx.CallAfter(copy_and_notify, path)
            except Exception as e:
                wx.CallAfter(self._onActionDone, None, str(e))

        uiutil.start_worker(worker)

    def _openEmbedVideo(self, url):
        # Translators: Announced while downloading a video to open it.
        feedWindow._announce_now(_("Downloading video, please wait..."))

        def worker():
            try:
                path = attachments.download_video_playlist_to_temp(url)
                attachments.open_with_default_app(path)
                error = None
            except Exception as e:
                error = str(e)
            # Translators: Announced after a video finishes opening.
            wx.CallAfter(self._onActionDone, _("Video opened.") if not error else None, error)

        uiutil.start_worker(worker)

    @uiutil.safe_ui_callback
    def _onLikeSynced(self, post, message, error):
        self._onActionDone(message, error)
        if error:
            return
        soundpack.play("like" if post.get("viewer_like_uri") else "unlike")
        account = db.get_active_account()
        if account is not None:
            feedWindow.sync_like_state(post, account["id"])

    @uiutil.safe_ui_callback
    def _onActionDone(self, message, error):
        def announce_immediately(text):
            speech.cancelSpeech()  # cuts off the ListCtrl's own focus announcement
            nvdaUi.message(text)   # speak ours instead

        if error:
            log.error(f"NVSky: action failed: {error}")
            # Translators: Announced when an embed action (open/copy/send) fails. {} is the error message.
            core.callLater(200, announce_immediately, _("Action failed: {}").format(error))
            return

        if message:
            core.callLater(200, announce_immediately, message)