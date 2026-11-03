"""
Auto read for NVSky: does what pressing Space does (jump to the next unread
row, oldest unread first, whatever the display order), again and again. Each
row is spoken, NVDA reports when the speech reached its end, then a user-set
delay passes before the next row.

Rows come from a source (the engine below only needs this interface):
    label, tracks, first_row(), next_row(), has_more(), text(row),
    row_started(row), show_row(row) -> bool
 - PanelSource: the current tab of the open main window. The list focus really
   moves to each row; NVDA's own announcement of it is replaced by ours.
 - CacheSource: the last active tab, from the local database, while the main
   window is closed. Read state and the saved focus position are written, so
   the tab opens on the row last read.
Row text is built from the stored post/notification, never from a list cell, so
it is always the full text.

Tabs without unread tracking (Saved, Likes, Explore posts) are read in order,
starting after the focused/saved row.

Reading stops when speech NVSky didn't start, or a speech cancel, is seen (the
user pressed something that speaks, or silenced speech).

An optional NVDA configuration profile (Settings > General) gives auto read its
own voice: each row is wrapped in enter/exit commands of a private profile trigger.
"""
import json
import time

import wx

import braille
import config
import controlTypes
import speech
import ui as nvdaUi
from logHandler import log
from speech.commands import CallbackCommand, ConfigProfileTriggerCommand
from speech.extensions import pre_speech, pre_speechCanceled as speechCanceled

from . import client
from . import db
from . import soundpack
from . import uiutil
from .feedWindow import (
    FeedListMixin,
    _announce_now,
    _embed_sound_event,
    _format_post_time,
    _label_visibility,
    _visible_embed_text,
    _visible_message_text,
)
from .notificationsWindow import _describe_notification

START_AFTER_CURRENT = True  # sequential tabs only. False: start on the current row itself
GRACE_SECONDS = 0.4  # NVDA's own speech/cancel around the start is ignored
RESTART_GUARD_SECONDS = 0.35  # a toggle right after an interrupt-driven stop must not restart
FOCUS_WINDOW_SECONDS = 1.0  # after a focus move NVDA's own row speech is expected and ignored
FOCUS_FALLBACK_MS = 800  # speak the row ourselves if NVDA never reports the focus change
WATCHDOG_BASE_SECONDS = 15.0
WATCHDOG_PER_CHAR = 0.3
WATCHDOG_MAX_SECONDS = 180.0
PROFILE_TRIGGER_SPEC = "nvskyAutoRead"  # key of our private profile trigger


def _active_profile_names():
    return [getattr(p, "name", None) or "base" for p in config.conf.profiles]


def _join(parts):
    return ", ".join(p for p in parts if p)


def _row_sound(row):
    # Same rule as FeedListMixin.onItemFocused.
    if _label_visibility(row) == "warn":
        return "content_warning"
    return _embed_sound_event(row.get("embed_json"))


def _row_text(row, kind, mode):
    # FeedListMixin helpers never use self.
    author = FeedListMixin._authorLabel(None, row, mode)
    when = _format_post_time(row.get("indexed_at"))
    if kind == "notification":
        return _join([author, _describe_notification(row), when])
    return _join([_visible_embed_text(row), author, _visible_message_text(row), when])


def _resume_index(rows, positionUri):
    if not positionUri:
        return 0
    found = next((i for i, r in enumerate(rows) if r["uri"] == positionUri), -1)
    if found == -1:
        return 0
    return found + 1 if START_AFTER_CURRENT else found


def _oldest_unread(rows):
    # Same scan as Space (_focusNextUnread): oldest unread first, whatever the display order.
    newestFirst = db.get_ui_state("sort_order") != "oldest_first"
    indices = range(len(rows) - 1, -1, -1) if newestFirst else range(len(rows))
    for i in indices:
        if not rows[i].get("is_read"):
            return rows[i]
    return None


class _Source:
    """Shared reading order. Subclasses supply rows(), _focusKey(), _positionUri(), row_started(), show_row()."""

    tracks = True  # False: no unread state, read in order

    def __init__(self, label, kind):
        self.label = label
        self._kind = kind
        self._mode = FeedListMixin._currentAuthorMode(None)
        self._cursor = 0

    def text(self, row):
        return _row_text(row, self._kind, self._mode)

    def first_row(self):
        if not self.tracks:
            self._cursor = _resume_index(self.rows(), self._positionUri())
        return self.next_row()

    def next_row(self):
        rows = self.rows()
        if self.tracks:
            return _oldest_unread(rows)
        if self._cursor >= len(rows):
            return None
        row = rows[self._cursor]
        self._cursor += 1
        return row

    def has_more(self):
        rows = self.rows()
        if self.tracks:
            return _oldest_unread(rows) is not None
        return self._cursor < len(rows)

    def _remember(self, row):
        db.set_ui_state(self._focusKey(), row["uri"])


class PanelSource(_Source):
    """Current tab of the open main window (any FeedListMixin host with a post list)."""

    def __init__(self, panel):
        kind = "notification" if getattr(panel, "TAB_KEY", None) == "notifications" else "post"
        super().__init__(panel.TAB_NAME, kind)
        self._panel = panel
        self.tracks = getattr(panel, "_tracksUnread", True) is not False

    def rows(self):
        return self._panel._posts  # live: the panel replaces this list on reload

    def _focusKey(self):
        return self._panel._focusStateKey()

    def _positionUri(self):
        panel = self._panel
        if panel.postList.HasFocus():
            focused = panel.postList.GetFocusedItem()
            if 0 <= focused < len(panel._posts):
                return panel._posts[focused]["uri"]
        return db.get_ui_state(self._focusKey())

    def row_started(self, row):
        panel = self._panel
        if not row.get("is_read"):
            panel._markItemRead(row)
            row["is_read"] = 1
            panel._updateStatusBar()
        self._remember(row)

    def show_row(self, row):
        """Moves the list focus to the row. True if the caller must speak it (NVDA won't announce it)."""
        panel = self._panel
        index = next((i for i, p in enumerate(panel._posts) if p["uri"] == row["uri"]), -1)
        if index == -1:
            return True
        panel._suppressFocusEvents = True  # row_started already marked it read and saved the position
        try:
            return uiutil.move_focus_and_check_announce(panel.postList, index)
        finally:
            panel._suppressFocusEvents = False


class CacheSource(_Source):
    """The last active tab read straight from the database (main window closed)."""

    def __init__(self, account, label, feedKey, kind):
        super().__init__(label, kind)
        self._accountId = account["id"]
        self._feedKey = feedKey
        self.tracks = feedKey not in ("saved", "likes")
        if kind == "notification":
            rows = db.get_notification_page(self._accountId)
        else:
            rows = [p for p in db.get_feed_page(self._accountId, feedKey) if _label_visibility(p) != "hide"]
        self._rows = FeedListMixin._applySortOrder(None, rows)

    def rows(self):
        return self._rows

    def _focusKey(self):
        return f"lastFocus:{self._feedKey}:{self._accountId}"

    def _positionUri(self):
        return db.get_ui_state(self._focusKey())

    def row_started(self, row):
        if not row.get("is_read"):
            if self._kind == "notification":
                db.mark_notification_read(row["uri"])
            else:
                db.mark_post_read(row["uri"])
            row["is_read"] = 1
            from . import refresh_tray
            refresh_tray()
        self._remember(row)  # the tab opens on this row next time

    def show_row(self, row):
        return True  # nothing to focus; the tabs restore the saved position when the window opens


def _panel_supported(panel):
    if not (hasattr(panel, "_posts") and hasattr(panel, "postList") and callable(getattr(panel, "_markItemRead", None))):
        return False
    if getattr(panel, "_account", None) is None:
        return False
    tabKey = getattr(panel, "TAB_KEY", None)
    if tabKey == "explore":
        return panel._currentType() == "posts"
    if tabKey == "lists":
        selected = panel._selectedList
        return bool(selected) and selected["purpose"] == client.LIST_PURPOSE_CURATE
    return True


def _resolve_cached_tab(account):
    """(label, feed key, kind) of the last active tab, or None if it can't be read from the database."""
    accountId = account["id"]
    identity = {"kind": "permanent", "key": "home"}
    raw = db.get_ui_state(f"last_active_tab:{accountId}")
    if raw:
        try:
            loaded = json.loads(raw)
            identity = loaded if isinstance(loaded, dict) else {"kind": "permanent", "key": raw}
        except (ValueError, TypeError):
            identity = {"kind": "permanent", "key": raw}
    kind, key = identity.get("kind"), identity.get("key")
    if kind == "permanent":
        if key == "home":
            # Translators: Tab name used by auto read for the Home tab.
            return _("Home"), db.get_home_active_filter(accountId), "post"
        if key == "notifications":
            # Translators: Tab name used by auto read for the Notifications tab.
            return _("Notifications"), "notifications", "notification"
        if key == "saved":
            # Translators: Tab name used by auto read for the Saved tab.
            return _("Saved"), "saved", "post"
        if key == "likes":
            # Translators: Tab name used by auto read for the Likes tab.
            return _("Likes"), "likes", "post"
        return None
    entry = next(
        (e for e in db.get_open_temp_tabs(accountId) if e.get("type") == kind and e.get("key") == key), None
    )
    if entry is None:
        return None
    name = entry.get("custom_name")
    if kind == "list":
        # Translators: Fallback tab name used by auto read for a list tab with no name cached.
        return name or entry.get("list_name") or _("List"), key, "post"
    if kind == "user_timeline":
        # Translators: Fallback owner label for a user-timeline tab with no name cached.
        owner = entry.get("owner_label") or _("user")
        # Translators: Tab name used by auto read for a user's timeline. {} is the account owner.
        return name or _("Timeline of {}").format(owner), f"user_timeline:{key}", "post"
    if kind == "search_preview":
        # Translators: Fallback tab name used by auto read for a search/feed preview tab.
        return name or entry.get("name") or _("Search"), key, "post"
    return None


def build_source():
    """Returns (source, None) or (None, message to speak)."""
    account = db.get_active_account()
    if account is None:
        # Translators: Announced when auto read is started with no active account.
        return None, _("No active account.")
    from . import get_main_window
    mainWindow = get_main_window()
    if mainWindow is not None:
        index = mainWindow.notebook.GetSelection()
        panel = mainWindow.notebook.GetPage(index) if index != wx.NOT_FOUND else None
        if panel is None or not _panel_supported(panel):
            # Translators: Announced when auto read is started on a tab it can't read.
            return None, _("Auto read isn't available on this tab.")
        return PanelSource(panel), None
    resolved = _resolve_cached_tab(account)
    if resolved is None:
        # Translators: Announced when auto read is started with the main window closed and the last tab can't be read from the saved data.
        return None, _("Auto read can't read your last tab while the main window is closed.")
    label, feedKey, kind = resolved
    return CacheSource(account, label, feedKey, kind), None


class AutoReader:
    def __init__(self):
        self._source = None
        self._firstRow = None
        self._lastRow = None
        self._token = 0  # bumped on start/stop so stale timers and callbacks do nothing
        self._serial = 0  # one per spoken utterance; a replaced utterance's callback is ignored
        self._current = None  # (text, then) of the row being read
        self._spokeForRow = False
        self._speakingOwn = False
        self._cancelingOwn = False
        self._ignoreUntil = 0.0
        self._focusWindowUntil = 0.0
        self._stoppedAt = 0.0
        self._hooked = False
        self._timer = None
        self._watchdog = None
        self._focusFallback = None
        self._trigger = None
        self._profileName = None
        self._previousSpec = None

    @property
    def running(self):
        return self._source is not None

    def toggle(self):
        if self.running:
            self.stop("user")
        elif time.monotonic() - self._stoppedAt >= RESTART_GUARD_SECONDS:
            self.start()

    def terminate(self):
        self.stop("quiet")

    # ---------------- start/stop ----------------

    def start(self):
        try:
            source, message = build_source()
            first = source.first_row() if source is not None else None
        except Exception as e:
            log.error(f"NVSky: auto read could not start: {e}")
            # Translators: Announced when auto read fails to start.
            nvdaUi.message(_("Auto read could not start."))
            return
        if source is None:
            nvdaUi.message(message)
            return
        if first is None:
            soundpack.play("boundary")
            # Translators: Announced when auto read has nothing left to read. {} is the tab name.
            nvdaUi.message(_("Nothing more to read in {}.").format(source.label))
            return
        self._source = source
        self._firstRow = first
        self._lastRow = None
        self._token += 1
        self._ignoreUntil = time.monotonic() + GRACE_SECONDS
        self._focusWindowUntil = 0.0
        self._armProfile()
        self._hook()
        soundpack.play("autoread_start")
        self._next()

    def stop(self, reason="user"):
        if not self.running:
            return
        self._source = None
        self._firstRow = None
        self._token += 1
        self._focusWindowUntil = 0.0
        self._cancelTimers()
        self._unhook()
        self._stoppedAt = time.monotonic()
        if reason == "user":
            speech.cancelSpeech()
            soundpack.play("autoread_stop")
        elif reason == "end":
            soundpack.play("boundary")
            soundpack.play("autoread_stop")
        elif reason == "stalled":
            # Translators: Announced when auto read stops because speech never reported it had finished.
            _announce_now(_("Auto read stopped: speech did not report that it finished."))
        elif reason == "error":
            # Translators: Announced when auto read stops because of an error.
            _announce_now(_("Auto read stopped because of an error."))
        self._releaseProfile()

    # ---------------- voice profile ----------------

    def _armProfile(self):
        self._trigger = None
        name = db.get_autoread_profile()
        if not name:
            return
        try:
            if name not in [n for n in config.conf.listProfiles() if n] or name in _active_profile_names():
                return  # deleted since, or its voice already applies
            mapping = config.conf.triggersToProfiles

            class AutoReadTrigger(config.ProfileTrigger):
                pass

            AutoReadTrigger.spec = PROFILE_TRIGGER_SPEC
            trigger = AutoReadTrigger()
            self._previousSpec = mapping.get(PROFILE_TRIGGER_SPEC)
            mapping[PROFILE_TRIGGER_SPEC] = name
            self._profileName = name
            self._trigger = trigger
        except Exception as e:
            log.error(f"NVSky: auto read voice profile unavailable: {e}")
            self._trigger = None

    def _releaseProfile(self):
        trigger, self._trigger = self._trigger, None
        if trigger is None:
            return
        try:
            if self._profileName in _active_profile_names():
                trigger.exit()  # speech was cut before its exit command ran
        except Exception as e:
            log.error(f"NVSky: leaving the auto read voice profile failed: {e}")
        try:
            mapping = config.conf.triggersToProfiles
            if self._previousSpec is None:
                mapping.pop(PROFILE_TRIGGER_SPEC, None)
            else:
                mapping[PROFILE_TRIGGER_SPEC] = self._previousSpec
        except Exception as e:
            log.error(f"NVSky: restoring profile triggers failed: {e}")

    # ---------------- reading ----------------

    def _next(self):
        if not self.running:
            return
        source = self._source
        row = None
        try:
            row = self._firstRow or source.next_row()
            self._firstRow = None
            if row is not None:
                text = source.text(row)
                source.row_started(row)
        except RuntimeError:
            self.stop("quiet")  # the tab or window was destroyed
            return
        except Exception as e:
            log.error(f"NVSky: auto read failed on a row: {e}")
            self.stop("error")
            return
        if row is None:
            self.stop("end")
            return
        self._lastRow = row
        self._spokeForRow = False
        if not text:
            self._rowDone()
            return
        self._current = (text, self._rowDone)
        sound = _row_sound(row)
        if sound:
            soundpack.play_debounced(sound)  # plays to the end first, so it lands before the speech
        try:
            ownSpeech = source.show_row(row)
        except RuntimeError:
            self.stop("quiet")
            return
        if ownSpeech:
            self._speakCurrent()
            return
        # NVDA is about to announce the focused row itself. Let it, then replace that with our speech
        # (see onGainFocus); if the focus event never comes, speak after a short wait.
        self._focusWindowUntil = time.monotonic() + FOCUS_WINDOW_SECONDS
        self._cancelFocusFallback()
        self._focusFallback = wx.CallLater(FOCUS_FALLBACK_MS, self._focusFallbackFired, self._token)

    def _rowDone(self):
        if not self.running:
            return
        try:
            more = self._source.has_more()
        except RuntimeError:
            self.stop("quiet")
            return
        if not more:
            self.stop("end")
            return
        self._timer = wx.CallLater(db.get_autoread_delay() * 1000, self._timerNext, self._token)

    def _timerNext(self, token):
        if token == self._token:
            self._next()

    def _speakCurrent(self):
        text, then = self._current
        token = self._token
        self._serial += 1
        serial = self._serial
        self._spokeForRow = True

        def reached():
            # Runs where the synth calls back from; hop to the main thread.
            wx.CallAfter(self._reached, token, serial, then)

        trigger = self._trigger
        if trigger is None:
            sequence = [text]
        else:
            sequence = [
                ConfigProfileTriggerCommand(trigger, True),
                text,
                ConfigProfileTriggerCommand(trigger, False),
            ]
        sequence.append(CallbackCommand(reached))
        self._speakingOwn = True
        try:
            speech.speak(sequence)
        finally:
            self._speakingOwn = False
        try:
            braille.handler.message(text)
        except Exception:
            pass
        seconds = min(WATCHDOG_MAX_SECONDS, WATCHDOG_BASE_SECONDS + len(text) * WATCHDOG_PER_CHAR)
        self._cancelWatchdog()
        self._watchdog = wx.CallLater(int(seconds * 1000), self._stalled, token)

    def _reached(self, token, serial, then):
        if token != self._token or serial != self._serial or not self.running:
            return
        self._cancelWatchdog()
        then()

    def _stalled(self, token):
        if token == self._token and self.running:
            self.stop("stalled")

    def _focusFallbackFired(self, token):
        if token == self._token and self.running and not self._spokeForRow:
            self._speakCurrent()

    def onGainFocus(self, obj):
        """Called by GlobalPlugin.event_gainFocus after NVDA handled a focus event."""
        if not self.running or time.monotonic() > self._focusWindowUntil or self._current is None:
            return
        try:
            if obj.role != controlTypes.Role.LISTITEM:
                return
        except Exception:
            return
        # NVDA just announced the row itself: cancel that and speak our own text instead.
        self._cancelFocusFallback()
        self._cancelingOwn = True
        try:
            speech.cancelSpeech()
        finally:
            self._cancelingOwn = False
        self._speakCurrent()

    # ---------------- interruption ----------------

    def _hook(self):
        pre_speech.register(self._onPreSpeech)
        speechCanceled.register(self._onSpeechCanceled)
        self._hooked = True

    def _unhook(self):
        if not self._hooked:
            return
        self._hooked = False
        for point, handler in ((pre_speech, self._onPreSpeech), (speechCanceled, self._onSpeechCanceled)):
            try:
                point.unregister(handler)
            except Exception:
                pass

    def _ignoring(self):
        now = time.monotonic()
        return self._speakingOwn or self._cancelingOwn or now < self._ignoreUntil or now < self._focusWindowUntil

    def _onPreSpeech(self, **kwargs):
        if not self._ignoring():
            wx.CallAfter(self._interrupted)

    def _onSpeechCanceled(self, **kwargs):
        if not self._ignoring():
            wx.CallAfter(self._interrupted)

    def _interrupted(self):
        if self.running:
            self.stop("interrupt")

    # ---------------- timers ----------------

    def _cancelWatchdog(self):
        if self._watchdog is not None:
            self._watchdog.Stop()
            self._watchdog = None

    def _cancelFocusFallback(self):
        if self._focusFallback is not None:
            self._focusFallback.Stop()
            self._focusFallback = None

    def _cancelTimers(self):
        self._cancelWatchdog()
        self._cancelFocusFallback()
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None
