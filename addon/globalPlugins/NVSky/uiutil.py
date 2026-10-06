"""
Small shared UI-safety helpers for NVSky.

Kept as its own leaf module (no NVSky-internal imports) so both
feedWindow.py and chatWindow.py can import it without any circular-
import risk.
"""
import functools

import threading
import time

import controlTypes
import queueHandler
import speech
import wx
import ui as nvdaUi
from logHandler import log

# Set True by MainWindow.onClose just before Destroy(), reset in
# MainWindow.__init__. safe_ui_callback checks it first: touching a window
# mid-teardown can crash natively instead of raising RuntimeError.
app_closing = False


def start_worker(worker, progress=True):
    """Runs worker in a daemon thread; the progress sound plays until it finishes."""
    from . import soundpack

    def run():
        try:
            worker()
        finally:
            if progress:
                soundpack.stop_progress()

    if progress:
        soundpack.start_progress()
    threading.Thread(target=run, daemon=True).start()


def copy_text_to_clipboard(text: str) -> bool:
    if not text:
        return False
    if not wx.TheClipboard.Open():
        return False
    try:
        wx.TheClipboard.SetData(wx.TextDataObject(text))
    finally:
        wx.TheClipboard.Close()
    # Translators: Announced after Ctrl+C copies the focused row's text to the clipboard.
    nvdaUi.message(_("Copied."))
    return True


def focus_check_updates_button(window):
    """Fallback focus for a list that just lost its last row: a ListCtrl
    that keeps focus with no rows makes NVDA report "unknown"."""
    button = getattr(window.GetTopLevelParent(), "checkUpdatesButton", None)
    if button is not None:
        button.SetFocus()


_jump_title = ""
_jump_suppress_until = 0.0


def _suppress_jump_title(title):
    # The main window's title is re-announced when the dialog closes; skip that one announcement.
    global _jump_title, _jump_suppress_until
    _jump_title = title
    _jump_suppress_until = time.time() + 1.5


def jump_title_suppressed(name):
    return time.time() < _jump_suppress_until and name == _jump_title


_jump_row_text = ""
_jump_row_until = 0.0


def _set_jump_row(text, active=True):
    global _jump_row_text, _jump_row_until
    _jump_row_text = text
    _jump_row_until = time.time() + 0.6 if active else 0.0


def jump_row_text_for(obj):
    """Row text to speak instead of NVDA's own announcement right after Ctrl+J."""
    if time.time() < _jump_row_until and obj.role == controlTypes.Role.LISTITEM:
        return _jump_row_text
    return None


class _RowNumberDialog(wx.Dialog):
    def __init__(self, parent, current, total):
        # Translators: Title of the jump-to-row dialog.
        super().__init__(parent, title=_("Jump to row"))
        self._total = total
        self.number = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        # Translators: Label of the jump-to-row field. {} is the highest row number.
        label = wx.StaticText(self, label=_("&Row number (1 to {}):").format(total))
        sizer.Add(label, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
        startValue = str(current + 1) if current >= 0 else "1"
        self.rowText = wx.TextCtrl(self, value=startValue, style=wx.TE_PROCESS_ENTER)
        sizer.Add(self.rowText, flag=wx.EXPAND | wx.ALL, border=10)

        buttons = wx.StdDialogButtonSizer()
        okBtn = wx.Button(self, wx.ID_OK)
        cancelBtn = wx.Button(self, wx.ID_CANCEL)
        okBtn.SetDefault()
        buttons.AddButton(okBtn)
        buttons.AddButton(cancelBtn)
        buttons.Realize()
        sizer.Add(buttons, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizerAndFit(sizer)
        self.CentreOnScreen()

        self.rowText.Bind(wx.EVT_TEXT, self.onText)
        self.rowText.Bind(wx.EVT_TEXT_ENTER, self.onOk)
        okBtn.Bind(wx.EVT_BUTTON, self.onOk)

        self.rowText.SetFocus()
        self.rowText.SelectAll()

    def onText(self, evt):
        raw = self.rowText.GetValue()
        digits = "".join(c for c in raw if c in "0123456789").lstrip("0")
        if digits and int(digits) > self._total:
            digits = str(self._total)
            from . import soundpack
            soundpack.play("max_length")
        if digits != raw:
            self.rowText.ChangeValue(digits)
            self.rowText.SetInsertionPointEnd()
        evt.Skip()

    def onOk(self, evt):
        value = self.rowText.GetValue()
        if not value:
            # Translators: Announced when confirming the jump-to-row dialog with an empty field.
            nvdaUi.message(_("Enter a row number."))
            return
        self.number = int(value)
        _suppress_jump_title(self.GetParent().GetTopLevelParent().GetTitle())
        self.EndModal(wx.ID_OK)


def jump_to_row(parent, list_ctrl, row_text=None) -> bool:
    """Ctrl+J: asks for a row number (capped to the row count) and moves there.
    row_text(index) optionally supplies the spoken text (chat uses the full message)."""
    total = list_ctrl.GetItemCount()
    if total == 0:
        # Translators: Announced when Ctrl+J is pressed on an empty list.
        nvdaUi.message(_("Nothing to jump to."))
        return False
    dlg = _RowNumberDialog(parent, list_ctrl.GetFocusedItem(), total)
    result = dlg.ShowModal()
    number = dlg.number
    dlg.Destroy()
    if result != wx.ID_OK or number is None:
        return False
    index = max(0, min(number - 1, total - 1))

    def apply():
        try:
            if index >= list_ctrl.GetItemCount():
                return
            if row_text is not None:
                text = row_text(index)
            else:
                parts = [list_ctrl.GetItemText(index, col) for col in range(list_ctrl.GetColumnCount())]
                text = ", ".join(p for p in parts if p)
            speech.cancelSpeech()
            _set_jump_row(text)
            if move_focus_and_check_announce(list_ctrl, index):
                # No focus event will follow, so announce here.
                _set_jump_row("", active=False)
                if text:
                    nvdaUi.message(text)
        except RuntimeError:
            pass

    list_ctrl.SetFocus()
    # Runs after events NVDA already queued (focus change from the closed dialog).
    wx.CallAfter(queueHandler.queueFunction, queueHandler.eventQueue, apply)
    return True


def copy_focused_row(ctrl) -> bool:
    """Copies the focused row (all columns) of a ListCtrl, or the selected TreeCtrl item."""
    if isinstance(ctrl, wx.TreeCtrl):
        item = ctrl.GetSelection()
        if not item.IsOk():
            return False
        return copy_text_to_clipboard(ctrl.GetItemText(item))
    if isinstance(ctrl, wx.ListCtrl):
        index = ctrl.GetFocusedItem()
        if index == -1:
            return False
        parts = [ctrl.GetItemText(index, col) for col in range(ctrl.GetColumnCount())]
        return copy_text_to_clipboard(", ".join(p for p in parts if p))
    return False


def single_line(text: str) -> str:
    """
    Collapses newlines to spaces: ListCtrl cells are single-line. (A
    separate ~511-char cell limit exists; see chatWindow's column split.)
    """
    return text.replace("\r\n", " ").replace("\n", " ")


def move_focus_and_check_announce(list_ctrl, index: int) -> bool:
    """
    Moves focus to row `index` (Focus/Select/EnsureVisible); returns True
    if the caller must announce the row itself. NVDA only announces
    automatically when the control has real focus AND the index changes.
    """
    hadRealFocus = list_ctrl.HasFocus()
    previousIndex = list_ctrl.GetFocusedItem()

    # Select() on a multi-select ListCtrl (postList has LC_SINGLE_SEL
    # off for Ctrl+A/bulk actions) only ADDS index to the selection --
    # it doesn't clear whatever was already selected. Confirmed by
    # testing (see plan-09.md): without this, Alt+number left both the
    # old and new row selected, so Post action's single-vs-multi-select
    # detection wrongly showed the bulk "mark read/unread" menu, and
    # User action reported "needs a single post selected." Same idiom
    # already used in feedWindow.py's _jumpToUserPost for the identical
    # problem on Left/Right jump -- just missing here until now.
    for j in range(list_ctrl.GetItemCount()):
        if j != index and list_ctrl.GetItemState(j, wx.LIST_STATE_SELECTED):
            list_ctrl.SetItemState(j, 0, wx.LIST_STATE_SELECTED)

    list_ctrl.Focus(index)
    list_ctrl.Select(index)
    list_ctrl.EnsureVisible(index)

    return not hadRealFocus or previousIndex == index


def safe_ui_callback(func=None, *, check_app_closing=True):
    """
    Decorator for methods used as wx.CallAfter targets from worker threads
    (the self._onXxxDone pattern). By the time the callback runs, the target
    window may be destroyed: this skips the call when app_closing is set and
    turns RuntimeError("... has been deleted") into a logged no-op. Any other
    exception propagates.

    check_app_closing=False opts out of the app_closing check. Use it for
    anything not parented into MainWindow (LoginDialog, Settings panels):
    app_closing stays True after MainWindow closes until it reopens, which
    would silently drop their callbacks.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(self, *args, **kwargs):
            if check_app_closing and app_closing:
                return None
            try:
                return f(self, *args, **kwargs)
            except RuntimeError as e:
                if "has been deleted" in str(e):
                    log.info(
                        f"NVSky: {f.__qualname__} skipped -- target "
                        f"window already destroyed ({e})"
                    )
                    return None
                raise
        return wrapper

    if func is not None:
        # Bare @safe_ui_callback usage (no parentheses).
        return decorator(func)
    return decorator