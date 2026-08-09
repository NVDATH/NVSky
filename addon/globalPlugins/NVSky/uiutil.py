"""
Small shared UI-safety helpers for NVSky.

Kept as its own leaf module (no NVSky-internal imports) so both
feedWindow.py and chatWindow.py can import it without any circular-
import risk.
"""
import functools

from logHandler import log


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