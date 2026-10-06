"""
Notification area icon for NVSky: the tooltip shows the Home unread count.
Enter or a click opens the main window; the menu has Open and Settings.
"""
import wx
import wx.adv

import api
import speech
from logHandler import log


def category_labels():
    # Short names: they share one tooltip.
    return {
        # Translators: Short tab name in the notification area icon's tooltip (Home feed).
        "home": _("Home"),
        # Translators: Short tab name in the notification area icon's tooltip (Notifications).
        "notifications": _("Noti"),
        # Translators: Short tab name in the notification area icon's tooltip (Chat).
        "chat": _("Chat"),
    }


def tooltip_text(counts, per_tab=False):
    """counts: list of (short name, unread) with unread > 0. The add-on name is
    part of each string (never prefixed elsewhere), so it is spoken once."""
    if not counts:
        # Translators: Tooltip of the notification area icon when nothing is unread.
        return _("NVSky: no unread")
    if not per_tab:
        # Translators: Tooltip of the notification area icon. {} is the total unread count.
        return _("NVSky: {} unread").format(sum(n for _name, n in counts))
    parts = [f"{name} {'99+' if n > 99 else n}" for name, n in counts]
    # Translators: Tooltip of the notification area icon. {} is a list such as "Home 3, Noti 2".
    text = _("NVSky: {}").format(", ".join(parts))
    if len(text) > 127:  # Windows tooltip limit; fall back to the total
        # Translators: Tooltip of the notification area icon when the list is too long. {} is the total unread count.
        text = _("NVSky: {} unread").format(sum(n for _name, n in counts))
    return text


def _focus_on_our_icon():
    try:
        return (api.getFocusObject().name or "").startswith("NVSky:")
    except Exception:
        return False


class TrayIcon(wx.adv.TaskBarIcon):
    RESTORE_INTERVAL_MS = 20
    RESTORE_MAX_TRIES = 15

    def __init__(self, on_open, on_settings, counts, per_tab=False):
        super().__init__()
        self._perTab = per_tab
        self._onOpen = on_open
        self._onSettings = on_settings
        self._icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
        self._text = None
        self._closed = False
        self._restoreTries = 0
        self.set_counts(counts, per_tab)  # the first tooltip is already the real count
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self._clicked)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self._clicked)

    def set_counts(self, counts, per_tab=False):
        self._perTab = per_tab
        text = tooltip_text(counts, per_tab)
        if text == self._text:  # touch the icon only when the text changes
            return
        hadFocus = False
        if self._text is not None:
            hadFocus = _focus_on_our_icon()
            # The first SetIcon text becomes the button's name and later ones
            # only its tooltip, so NVDA reads both. Re-adding sets the name again.
            self.RemoveIcon()
        self._text = text
        self.SetIcon(self._icon, text)
        if hadFocus:
            # Removing the focused icon makes Windows move focus to an unrelated icon.
            self._restoreTries = 0
            wx.CallLater(self.RESTORE_INTERVAL_MS, self._restoreFocus)

    def _restoreFocus(self):
        if self._closed:
            return
        self._restoreTries += 1
        try:
            focus = api.getFocusObject()
            if (focus.name or "").startswith("NVSky:") and self._restoreTries > 1:
                return  # focus is back on our icon
            parent = focus.parent
            for child in (parent.children if parent else []):
                if (child.name or "").startswith("NVSky:"):
                    speech.cancelSpeech()  # drop the announcement of the icon Windows jumped to
                    child.setFocus()
                    break
        except Exception as e:
            log.error(f"NVSky: restoring tray icon focus failed: {e}")
            return
        if self._restoreTries < self.RESTORE_MAX_TRIES:
            wx.CallLater(self.RESTORE_INTERVAL_MS, self._restoreFocus)

    def _clicked(self, event):
        self._onOpen()

    def CreatePopupMenu(self):
        menu = wx.Menu()
        # Translators: Item of the notification area icon's menu.
        item = menu.Append(wx.ID_ANY, _("&Open NVSky"))
        self.Bind(wx.EVT_MENU, lambda e: self._onOpen(), item)
        # Translators: Item of the notification area icon's menu.
        item = menu.Append(wx.ID_ANY, _("NVSky &Settings..."))
        self.Bind(wx.EVT_MENU, lambda e: self._onSettings(), item)
        return menu

    def cleanup(self):
        self._closed = True
        try:
            self.RemoveIcon()
        finally:
            self.Destroy()