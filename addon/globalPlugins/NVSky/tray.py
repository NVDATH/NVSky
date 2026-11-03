"""
Notification area icon for NVSky: the tooltip shows the unread counts.
Enter or a click opens the main window; the menu has Open and Settings.

Updating the icon is RemoveIcon + SetIcon. When our button disappears, Explorer moves its own
focus to a neighbouring tray button and UIA reports that to NVDA as a real focus change, which
makes NVDA cancel speech before any plugin sees it. StrayTrayFocusGuard (an overlay on the
taskbar buttons) drops such an event before it enters NVDA's queue: it is stray when the
button's window is a taskbar window and the foreground window is not (a real visit, Win+B,
always has the taskbar in the foreground). If the reader is on our icon when it is replaced,
Windows throws the focus to an unrelated icon: _restoreFocus puts it back.
Our icon is recognised by the tooltip prefix.
"""
import api
import controlTypes
import speech
import winUser
import wx
import wx.adv
from logHandler import log
from NVDAObjects.UIA import UIA

APP_NAME = "NVSky"
PREFIX = APP_NAME + ":"
TRAY_CLASS = "Windows.UI.Input.InputSite.WindowClass"  # window class of every notification area button
TRAY_FOREGROUNDS = ("Shell_TrayWnd", "TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")


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
    """counts: list of (short name, unread) with unread > 0. Must start with PREFIX:
    that is how our icon is told apart from the others."""
    if not counts:
        # Translators: Tooltip of the notification area icon when nothing is unread. {name} is the add-on name and must stay at the start.
        return _("{name}: no unread").format(name=APP_NAME)
    total = sum(n for _name, n in counts)
    if not per_tab:
        # Translators: Tooltip of the notification area icon. {name} is the add-on name and must stay at the start, {count} is the total unread count.
        return _("{name}: {count} unread").format(name=APP_NAME, count=total)
    parts = [f"{name} {'99+' if n > 99 else n}" for name, n in counts]
    # Translators: Tooltip of the notification area icon. {name} is the add-on name and must stay at the start, {list} is such as "Home 3, Noti 2".
    text = _("{name}: {list}").format(name=APP_NAME, list=", ".join(parts))
    if len(text) > 127:  # Windows tooltip limit; fall back to the total
        text = _("{name}: {count} unread").format(name=APP_NAME, count=total)
    return text


def _focus_on_our_icon():
    try:
        return (api.getFocusObject().name or "").startswith(PREFIX)
    except Exception:
        return False


def _stray_tray_focus(hwnd):
    """True for a focus event of a taskbar button while the reader is not in the taskbar."""
    try:
        root = winUser.getClassName(winUser.getAncestor(hwnd, winUser.GA_ROOT))
        foreground = winUser.getClassName(winUser.getForegroundWindow())
        return root in TRAY_FOREGROUNDS and foreground not in TRAY_FOREGROUNDS
    except Exception:
        return False


class StrayTrayFocusGuard(UIA):
    """Overlay for the taskbar buttons: drops a focus event Explorer raised by itself."""

    def _get_shouldAllowUIAFocusEvent(self):
        if _stray_tray_focus(self.windowHandle):
            log.debug("NVSky: dropped a focus event that Explorer raised by itself")
            return False
        return super()._get_shouldAllowUIAFocusEvent()


def choose_overlay(obj, clsList):
    """Called for every object NVDA creates, so the first checks are cheap."""
    if getattr(obj, "windowClassName", "") != TRAY_CLASS or not isinstance(obj, UIA):
        return
    if obj.role == controlTypes.Role.BUTTON:  # the Alt+Tab list shares the class but has another role
        clsList.insert(0, StrayTrayFocusGuard)


class TrayIcon(wx.adv.TaskBarIcon):
    RESTORE_INTERVAL_MS = 20
    RESTORE_MAX_TRIES = 15

    def __init__(self, on_open, on_settings, counts, per_tab=False, accounts=None, on_account=None):
        super().__init__()
        self._onOpen = on_open
        self._onSettings = on_settings
        self._accounts = accounts  # () -> [(account id, menu text)]
        self._onAccount = on_account  # (account id) -> None
        self._icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
        self._text = None
        self._closed = False
        self._restoreTries = 0
        self.set_counts(counts, per_tab)  # the first tooltip is already the real count
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self._clicked)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self._clicked)

    def set_counts(self, counts, per_tab=False):
        text = tooltip_text(counts, per_tab)
        if text == self._text:  # touch the icon only when the text changes
            return
        hadFocus = False
        if self._text is not None:
            hadFocus = _focus_on_our_icon()
            # The first SetIcon text becomes the button's name and later ones only its
            # tooltip, so NVDA reads both. Re-adding sets the name again.
            self.RemoveIcon()
        self._text = text
        self.SetIcon(self._icon, text)
        if hadFocus:
            self._restoreTries = 0
            wx.CallLater(self.RESTORE_INTERVAL_MS, self._restoreFocus)

    def _restoreFocus(self):
        if self._closed:
            return
        self._restoreTries += 1
        try:
            focus = api.getFocusObject()
            if (focus.name or "").startswith(PREFIX) and self._restoreTries > 1:
                return  # focus is back on our icon
            parent = focus.parent
            for child in (parent.children if parent else []):
                if (child.name or "").startswith(PREFIX):
                    speech.cancelSpeech()  # drop the announcement of the icon Windows jumped to
                    child.setFocus()
                    break
        except Exception as e:
            log.error(f"NVSky: restoring tray icon focus failed: {e}")
            return
        if self._restoreTries < self.RESTORE_MAX_TRIES:
            wx.CallLater(self.RESTORE_INTERVAL_MS, self._restoreFocus)

    def _clicked(self, event):
        wx.CallAfter(self._onOpen)  # not inside the tray's own click handling

    def CreatePopupMenu(self):
        menu = wx.Menu()
        # Translators: Item of the notification area icon's menu.
        item = menu.Append(wx.ID_ANY, _("&Open NVSky"))
        self.Bind(wx.EVT_MENU, lambda e: self._onOpen(), item)
        # Translators: Item of the notification area icon's menu.
        item = menu.Append(wx.ID_ANY, _("NVSky &Settings..."))
        self.Bind(wx.EVT_MENU, lambda e: self._onSettings(), item)
        choices = []
        if self._accounts is not None and self._onAccount is not None:
            try:
                choices = self._accounts()
            except Exception as e:
                log.error(f"NVSky: listing accounts for the tray menu failed: {e}")
        if len(choices) > 1:
            menu.AppendSeparator()
            for accountId, text in choices:
                item = menu.Append(wx.ID_ANY, text)
                # After the menu has closed: switching redraws the icon.
                self.Bind(wx.EVT_MENU, lambda e, a=accountId: wx.CallAfter(self._onAccount, a), item)
        return menu

    def cleanup(self):
        self._closed = True
        try:
            self.RemoveIcon()
        finally:
            self.Destroy()
