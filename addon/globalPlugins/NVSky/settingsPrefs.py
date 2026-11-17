"""
Server-backed preference lists of Settings > Display for NVSky: content-label visibility and
notification categories. Each controller drives a list control that the page builds, keeps the
staged edits, and hands the settings dialog one Task to run on OK/Apply (see Task).
"""
import threading

import wx

import ui as nvdaUi

from . import client
from . import db
from . import soundpack
from . import uiutil
from .compose import CONTENT_LABEL_DISPLAY_NAMES


class Task:
    """One staged server-side change: run(client) on the worker thread, commit() on the UI thread after success."""

    def __init__(self, label, run, commit):
        self.label = label
        self.run = run
        self.commit = commit


CONTENT_LABEL_VISIBILITY_CHOICES = [
    # Translators: Content label visibility choice -- shown normally.
    ("show", _("Show")),
    # Translators: Content label visibility choice -- text/embed hidden behind a warning until Ctrl+Space.
    ("warn", _("Warn")),
    # Translators: Content label visibility choice -- post removed from feeds entirely.
    ("hide", _("Hide")),
]

NOTIFICATION_CATEGORY_DISPLAY_NAMES = {
    # Translators: Notification category display name.
    "follow": _("New followers"),
    # Translators: Notification category display name.
    "like": _("Likes"),
    # Translators: Notification category display name.
    "like_via_repost": _("Likes on your reposts"),
    # Translators: Notification category display name.
    "mention": _("Mentions"),
    # Translators: Notification category display name.
    "quote": _("Quotes"),
    # Translators: Notification category display name.
    "reply": _("Replies"),
    # Translators: Notification category display name.
    "repost": _("Reposts"),
    # Translators: Notification category display name.
    "repost_via_repost": _("Reposts of your reposts"),
    # Translators: Notification category display name.
    "starterpack_joined": _("Someone joins your starter pack"),
    # Translators: Notification category display name -- "subscribe" is a per-account bell icon on a profile, separate from following.
    "subscribed_post": _("New posts from accounts you've bell-subscribed to"),
    # Translators: Notification category display name -- Bluesky's identity-verification checkmark being removed from your account.
    "unverified": _("Your account's verification is removed"),
    # Translators: Notification category display name -- Bluesky's identity-verification checkmark being granted to your account.
    "verified": _("Your account gets verified"),
}

NOTIFICATION_STATE_DISPLAY_NAMES = {
    # Translators: Notification state -- this category is disabled entirely.
    "off": _("Off"),
    # Translators: Notification state -- notified for this category from anyone.
    "everyone": _("Everyone"),
    # Translators: Notification state -- notified for this category only from accounts you follow.
    "following": _("People you follow"),
    # Translators: Notification state -- this category is enabled (no filtering available).
    "on": _("On"),
}


class ContentLabelsController:
    """The content-label list: a master adult-content row, then one row per label category."""

    def __init__(self, listCtrl, statusLabel):
        self.list = listCtrl
        self.statusLabel = statusLabel
        self.prefs = {}
        self.baseline = {}
        self.ready = False
        self.loadedOnce = False
        listCtrl.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def _adultEnabled(self):
        return self.prefs.get(client.ADULT_CONTENT_PREF_KEY, True)

    def _render(self, target_index=None):
        previouslyFocused = self.list.GetFocusedItem()
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            adultOn = self._adultEnabled()
            # Translators: First row of the content label list, the master adult-content switch.
            self.list.InsertItem(0, _("Adult content"))
            # Translators: State of the adult-content switch.
            self.list.SetItem(0, 1, _("Enabled") if adultOn else _("Disabled"))
            for i, key in enumerate(client.CONTENT_LABEL_KEYS, start=1):
                self.list.InsertItem(i, CONTENT_LABEL_DISPLAY_NAMES.get(key, key))
                if key in client.CONTENT_LABEL_ADULT_ONLY and not adultOn:
                    # Translators: Visibility shown for a content label while adult content is off.
                    visLabel = _("Hidden (adult content is off)")
                else:
                    visibility = client.label_setting(self.prefs, key)
                    visLabel = dict(CONTENT_LABEL_VISIBILITY_CHOICES).get(visibility, visibility)
                self.list.SetItem(i, 1, visLabel)

            if target_index is not None:
                index = target_index
            elif previouslyFocused != -1:
                index = previouslyFocused
            else:
                index = 0
            index = max(0, min(index, len(client.CONTENT_LABEL_KEYS)))
            self.list.Focus(index)
            self.list.Select(index)
        finally:
            self.list.Thaw()

    def activate(self):
        # Loaded on the first visit of the page only.
        if not self.loadedOnce:
            self.loadedOnce = True
            self._load()

    def _load(self):
        # Translators: Status text while content label settings load.
        self.statusLabel.SetLabel(_("Loading content label settings..."))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                prefs = client.get_content_label_prefs(atprotoClient)
                error = None
            except Exception as e:
                prefs = None
                error = str(e)
            wx.CallAfter(self._onLoaded, prefs, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoaded(self, prefs, error):
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading content label settings fails. {} is the error message.
            self.statusLabel.SetLabel(_("Could not load content label settings: {}").format(error))
            return
        self.prefs = dict(prefs or {})
        self.baseline = dict(self.prefs)
        self.ready = True
        account = db.get_active_account()
        if account is not None:
            db.set_content_label_prefs_cache(account["id"], self.prefs)
        self._render(target_index=0)
        # Translators: Status text after content label settings finish loading.
        self.statusLabel.SetLabel(_("Content label settings loaded."))

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        self.loadedOnce = True
        self.ready = False
        self.prefs = {}
        self.baseline = {}
        self._load()

    def onCharHook(self, evt):
        # EVT_CHAR_HOOK, not EVT_LIST_KEY_DOWN: a plain ListCtrl consumes Space natively first.
        if evt.GetKeyCode() != wx.WXK_SPACE or evt.HasAnyModifiers():
            evt.Skip()
            return
        if wx.Window.FindFocus() is not self.list:
            evt.Skip()
            return
        index = self.list.GetFocusedItem()
        if index == -1 or index > len(client.CONTENT_LABEL_KEYS):
            return
        if not self.ready:
            # Translators: Announced when a settings list is changed before its data finished loading.
            nvdaUi.message(_("Still loading, please wait."))
            return
        if index == 0:
            self.prefs[client.ADULT_CONTENT_PREF_KEY] = not self._adultEnabled()
            self._render(target_index=index)
            return
        labelKey = client.CONTENT_LABEL_KEYS[index - 1]
        if labelKey in client.CONTENT_LABEL_ADULT_ONLY and not self._adultEnabled():
            # Translators: Announced when changing a content label while adult content is off.
            nvdaUi.message(_("Turn adult content on first."))
            return
        previousVisibility = client.label_setting(self.prefs, labelKey)
        order = [k for k, _n in CONTENT_LABEL_VISIBILITY_CHOICES]
        currentPos = order.index(previousVisibility) if previousVisibility in order else 0
        self.prefs[labelKey] = order[(currentPos + 1) % len(order)]
        self._render(target_index=index)

    def tasks(self):
        if not self.ready:
            return []
        changes = {}
        adultNow = self._adultEnabled()
        if adultNow != self.baseline.get(client.ADULT_CONTENT_PREF_KEY, True):
            changes[client.ADULT_CONTENT_PREF_KEY] = adultNow
        for key in client.CONTENT_LABEL_KEYS:
            now = client.label_setting(self.prefs, key)
            if now != client.label_setting(self.baseline, key):
                changes[key] = now
        if not changes:
            return []
        snapshot = dict(self.prefs)

        def run(atprotoClient):
            for key, value in changes.items():
                if key == client.ADULT_CONTENT_PREF_KEY:
                    client.set_adult_content_enabled(atprotoClient, value)
                else:
                    client.set_content_label_pref(atprotoClient, key, value)

        def commit():
            self.baseline = dict(snapshot)
            account = db.get_active_account()
            if account is not None:
                db.set_content_label_prefs_cache(account["id"], snapshot)

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [Task(_("content labels"), run, commit)]


class NotificationPrefsController:
    """The notification category list: Space cycles Off / Everyone / People you follow (or Off / On)."""

    def __init__(self, listCtrl, statusLabel):
        self.list = listCtrl
        self.statusLabel = statusLabel
        self.prefs = {}
        self.baseline = {}
        self.ready = False
        self.loadedOnce = False
        listCtrl.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)

    def _stateFor(self, category, pref):
        if not pref.get("list"):
            return "off"
        if category in client.NOTIFICATION_FILTERABLE_CATEGORIES:
            return "following" if pref.get("include") == "follows" else "everyone"
        return "on"

    def _render(self, target_index=None):
        previouslyFocused = self.list.GetFocusedItem()
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            for i, category in enumerate(client.NOTIFICATION_ALL_CATEGORIES):
                self.list.InsertItem(i, NOTIFICATION_CATEGORY_DISPLAY_NAMES.get(category, category))
                pref = self.prefs.get(category, {})
                state = self._stateFor(category, pref)
                self.list.SetItem(i, 1, NOTIFICATION_STATE_DISPLAY_NAMES.get(state, state))

            if target_index is not None:
                index = target_index
            elif previouslyFocused != -1:
                index = previouslyFocused
            else:
                index = 0
            index = max(0, min(index, len(client.NOTIFICATION_ALL_CATEGORIES) - 1))
            self.list.Focus(index)
            self.list.Select(index)
        finally:
            self.list.Thaw()

    def activate(self):
        # Loaded on the first visit of the page only.
        if not self.loadedOnce:
            self.loadedOnce = True
            self._load()

    def _load(self):
        # Translators: Status text while notification settings load.
        self.statusLabel.SetLabel(_("Loading notification settings..."))
        soundpack.start_progress()

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                prefs = client.get_notification_prefs(atprotoClient)
                error = None
            except Exception as e:
                prefs = None
                error = str(e)
            wx.CallAfter(self._onLoaded, prefs, error)

        threading.Thread(target=worker, daemon=True).start()

    @uiutil.safe_ui_callback(check_app_closing=False)
    def _onLoaded(self, prefs, error):
        soundpack.stop_progress()
        if error:
            # Translators: Status text when loading notification settings fails. {} is the error message.
            self.statusLabel.SetLabel(_("Could not load notification settings: {}").format(error))
            return
        self.prefs = {k: dict(v) for k, v in (prefs or {}).items()}
        self.baseline = {k: dict(v) for k, v in self.prefs.items()}
        self.ready = True
        self._render(target_index=0)
        # Translators: Status text after notification settings finish loading.
        self.statusLabel.SetLabel(_("Notification settings loaded."))

    def reload(self):
        # Account switched: pending edits belong to the old account, drop them.
        self.loadedOnce = True
        self.ready = False
        self.prefs = {}
        self.baseline = {}
        self._load()

    def onCharHook(self, evt):
        if evt.GetKeyCode() != wx.WXK_SPACE or evt.HasAnyModifiers():
            evt.Skip()
            return
        if wx.Window.FindFocus() is not self.list:
            evt.Skip()
            return
        index = self.list.GetFocusedItem()
        if index == -1 or index >= len(client.NOTIFICATION_ALL_CATEGORIES):
            return
        if not self.ready:
            # Translators: Announced when a settings list is changed before its data finished loading.
            nvdaUi.message(_("Still loading, please wait."))
            return
        category = client.NOTIFICATION_ALL_CATEGORIES[index]
        current = self.prefs.get(category, {})
        isFilterable = category in client.NOTIFICATION_FILTERABLE_CATEGORIES
        order = ["off", "everyone", "following"] if isFilterable else ["off", "on"]
        currentState = self._stateFor(category, current)
        currentPos = order.index(currentState) if currentState in order else 0
        newState = order[(currentPos + 1) % len(order)]

        if newState == "off":
            newListEnabled, newInclude = False, current.get("include")
        elif newState == "on":
            newListEnabled, newInclude = True, None
        elif newState == "everyone":
            newListEnabled, newInclude = True, "all"
        else:  # "following"
            newListEnabled, newInclude = True, "follows"

        self.prefs[category] = {
            "list": newListEnabled, "include": newInclude, "push": current.get("push", False),
        }
        self._render(target_index=index)

    def tasks(self):
        if not self.ready:
            return []
        changed = any(
            self._stateFor(c, self.prefs.get(c, {})) != self._stateFor(c, self.baseline.get(c, {}))
            for c in client.NOTIFICATION_ALL_CATEGORIES
        )
        if not changed:
            return []
        snapshot = {k: dict(v) for k, v in self.prefs.items()}

        def run(atprotoClient):
            # set_notification_category writes every category from snapshot; its other arguments are unused.
            client.set_notification_category(atprotoClient, snapshot, "follow", False, False)

        def commit():
            self.baseline = {k: dict(v) for k, v in snapshot.items()}

        # Translators: Name of a settings group, used in "Could not save: {}" messages.
        return [Task(_("notification settings"), run, commit)]
