"""
Small shared UI-safety helpers for NVSky.

Kept as its own leaf module (no NVSky-internal imports) so both
feedWindow.py and chatWindow.py can import it without any circular-
import risk.
"""
import functools

import wx
from logHandler import log

# Set True by MainWindow.onClose (feedWindow.py) right before it calls
# Destroy(), reset back to False at the start of MainWindow.__init__
# for the next time the user opens NVSky in the same NVDA session.
# safe_ui_callback checks this FIRST, before even attempting to touch
# any wx object -- catching RuntimeError("has been deleted") after the
# fact only helps when the C++ object is already fully torn down;
# during active teardown (window mid-destruction while a background
# thread's wx.CallAfter callback lands) that same touch can apparently
# reach a native crash instead of a clean Python exception (confirmed
# by testing -- see plan-09.md). Skipping the attempt entirely during
# shutdown avoids that window regardless of which failure mode a given
# race would have hit.
app_closing = False


def single_line(text: str) -> str:
    """
    Collapses embedded newlines into a plain space before handing text
    to a wx.ListCtrl column. ListCtrl (SysListView32 underneath on
    Windows) is single-line-per-cell -- confirmed via testing that a
    long multi-paragraph message displays/reads truncated in a column
    even though the full text is intact both in memory and in the DB
    (copying the raw text straight from the message/post dict comes
    out complete) -- only the ListCtrl's own copy was short. Collapsing
    the newlines is the fix being tried first since it's cheap to test;
    if a message with no embedded newlines still truncates at the same
    length, this isn't the (whole) story and a real per-cell character
    limit is the next thing to check.
    """
    return text.replace("\r\n", " ").replace("\n", " ")


def move_focus_and_check_announce(list_ctrl, index: int) -> bool:
    """
    Moves a ListCtrl's real focused-item position to `index`
    (Focus/Select/EnsureVisible) and returns True if the caller needs
    to announce that row itself.

    A ListCtrl only fires an accessible focus event (which NVDA
    announces automatically) when BOTH (a) the control already has
    real OS focus and (b) the focused item is actually moving to a
    different index -- so callers must announce explicitly whenever
    either condition fails, or the row change goes unannounced
    (control doesn't have real focus) or NVDA stays silent entirely
    (same index repeated -- no state change, no event).
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


def safe_ui_callback(func):
    """
    Decorator for methods that are the target of wx.CallAfter(...)
    from a background worker thread (the self._onXxxDone pattern used
    throughout feedWindow.py/chatWindow.py).

    By the time the main-thread event loop actually runs a queued
    CallAfter, the panel/window it targets may already have been
    destroyed -- e.g. the user opened MainWindow and closed it again
    before a background network call finished. Touching any wx
    control on a destroyed window raises RuntimeError("wrapped C/C++
    object of type X has been deleted"); this decorator catches
    exactly that specific error and turns it into a silent no-op
    instead of an unhandled exception (which previously force-
    restarted NVDA -- see the ListsWindow._onSyncListsDone crash).

    Only RuntimeErrors whose message contains "has been deleted" are
    swallowed -- any other exception (including other RuntimeErrors)
    still propagates normally, so this can't hide unrelated bugs.

    TEMPORARY: logs every time this actually catches something, so we
    can confirm in testing that this is the crash path being hit and
    how often. Safe to remove the log.info call once confirmed stable
    -- the try/except itself should stay permanently.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if app_closing:
            log.info(f"NVSky: {func.__qualname__} skipped -- app is closing")
            return None
        try:
            return func(self, *args, **kwargs)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                log.info(
                    f"NVSky: {func.__qualname__} skipped -- target "
                    f"window already destroyed ({e})"
                )
                return None
            raise
    return wrapper