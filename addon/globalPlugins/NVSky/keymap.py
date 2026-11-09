"""
Configurable keyboard shortcuts for an add-on's own windows (not NVDA gestures).

Leaf module: imports only wx, so another add-on can copy it as is and change
_DEFS and the labels. Storage is injected with configure().

Every key the windows react to is listed, so the list doubles as a manual:
 - "edit" rows: the user may change them. Default "" means no shortcut yet.
 - "fixed" rows: built in, listed for reference and for conflict checks.
 - "fixed_digits" rows: a modifier plus the keys 1 to 9 (stored as modifiers only).

A shortcut is stored as text: "ctrl+shift+pageup", "f5". Only changes from the
defaults are saved; "" saved over a default means the user removed it.
"""
import json

import wx

_EDIT, _FIXED, _FIXED_DIGITS = "edit", "fixed", "fixed_digits"

# (id, default, group, scopes, mode)
# scopes: windows that receive the key. "window" overlaps tab, chat and lists_tree.
_DEFS = [
    ("check_updates", "f5", "window", ("window",), _EDIT),
    ("check_all", "ctrl+f5", "window", ("window",), _EDIT),
    ("new_item", "ctrl+n", "window", ("window",), _EDIT),
    ("settings", "ctrl+p", "window", ("window",), _EDIT),
    ("remove_tab", "ctrl+w", "window", ("window",), _EDIT),
    ("rename_tab", "ctrl+f2", "window", ("window",), _EDIT),
    ("move_tab_left", "ctrl+shift+pageup", "window", ("window",), _EDIT),
    ("move_tab_right", "ctrl+shift+pagedown", "window", ("window",), _EDIT),
    ("next_account", "", "window", ("window",), _EDIT),
    ("previous_account", "", "window", ("window",), _EDIT),
    ("close_window", "escape", "window", ("window",), _EDIT),
    ("select_tab", "ctrl", "window", ("window",), _FIXED_DIGITS),

    ("load_older", "shift+f5", "tab", ("tab",), _EDIT),
    ("clear_cache", "ctrl+delete", "tab", ("tab",), _EDIT),
    ("next_unread", "space", "tab", ("tab",), _EDIT),
    ("delete_post", "delete", "tab", ("tab",), _EDIT),
    ("jump_user_previous", "left", "tab", ("tab",), _EDIT),
    ("jump_user_next", "right", "tab", ("tab",), _EDIT),
    ("post_action", "alt+a", "tab", ("tab",), _EDIT),
    ("user_action", "alt+u", "tab", ("tab", "dialog"), _EDIT),
    ("copy_row", "ctrl+c", "tab", ("tab", "chat"), _EDIT),
    ("jump_row", "ctrl+j", "tab", ("tab", "chat"), _EDIT),
    ("reveal_warning", "ctrl+space", "tab", ("tab",), _EDIT),
    ("select_all", "ctrl+a", "tab", ("tab",), _EDIT),
    ("toggle_advanced", "alt+v", "tab", ("tab",), _EDIT),
    ("remove_list", "delete", "tab", ("lists_tree",), _EDIT),
    ("nth_item", "alt", "tab", ("tab",), _FIXED_DIGITS),
    ("enter_action", "return", "tab", ("tab",), _FIXED),

    ("chat_check_all", "shift+f5", "chat", ("chat",), _EDIT),
    ("next_unread_message", "space", "chat", ("chat",), _EDIT),
    ("jump_to_replied", "left", "chat", ("chat",), _EDIT),
    ("jump_to_reply", "right", "chat", ("chat",), _EDIT),
    ("send", "ctrl+return", "chat", ("chat", "compose"), _EDIT),
    ("nth_message", "alt", "chat", ("chat",), _FIXED_DIGITS),

    ("close_dialog", "ctrl+w", "compose", ("compose",), _EDIT),

    ("select_page", "ctrl", "dialog", ("dialog",), _FIXED_DIGITS),

    ("post_reply", "", "post", ("tab",), _EDIT),
    ("post_reply_all", "", "post", ("tab",), _EDIT),
    ("post_quote", "", "post", ("tab",), _EDIT),
    ("post_repost", "", "post", ("tab",), _EDIT),
    ("post_like", "", "post", ("tab",), _EDIT),
    ("post_save", "", "post", ("tab",), _EDIT),
    ("post_mark_read", "", "post", ("tab",), _EDIT),
    ("post_mark_unread", "", "post", ("tab",), _EDIT),
    ("post_copy_text", "", "post", ("tab",), _EDIT),
    ("post_copy_link", "", "post", ("tab",), _EDIT),
    ("post_view_thread", "", "post", ("tab",), _EDIT),
    ("post_view_likes", "", "post", ("tab",), _EDIT),
    ("post_view_reposts", "", "post", ("tab",), _EDIT),
    ("post_view_quotes", "", "post", ("tab",), _EDIT),
    ("post_share_to_chat", "", "post", ("tab",), _EDIT),
    ("post_mute_thread", "", "post", ("tab",), _EDIT),
    ("post_hide", "", "post", ("tab",), _EDIT),
    ("post_report", "", "post", ("tab",), _EDIT),
    ("post_pin", "", "post", ("tab",), _EDIT),
    ("post_edit_replies", "", "post", ("tab",), _EDIT),

    ("user_profile", "", "user", ("tab",), _EDIT),
    ("user_timeline", "", "user", ("tab",), _EDIT),
    ("user_followers", "", "user", ("tab",), _EDIT),
    ("user_following", "", "user", ("tab",), _EDIT),
    ("user_known_followers", "", "user", ("tab",), _EDIT),
    ("user_lists", "", "user", ("tab",), _EDIT),
    ("user_start_chat", "", "user", ("tab",), _EDIT),
    ("user_follow", "", "user", ("tab",), _EDIT),
    ("user_mute", "", "user", ("tab",), _EDIT),
    ("user_block", "", "user", ("tab",), _EDIT),
    ("user_subscribe", "", "user", ("tab",), _EDIT),
    ("user_add_to_list", "", "user", ("tab",), _EDIT),
    ("user_copy_url", "", "user", ("tab",), _EDIT),
    ("user_open_web", "", "user", ("tab",), _EDIT),
    ("user_report", "", "user", ("tab",), _EDIT),

    ("message_reply", "", "message", ("chat",), _EDIT),
    ("message_react", "", "message", ("chat",), _EDIT),
    ("message_copy", "", "message", ("chat",), _EDIT),
    ("message_show", "", "message", ("chat",), _EDIT),
    ("message_delete", "", "message", ("chat",), _EDIT),
]
_BY_ID = {d[0]: d for d in _DEFS}
_UNDER_WINDOW = {"window", "tab", "chat", "lists_tree"}

_NAMED = {
    "delete": wx.WXK_DELETE, "return": wx.WXK_RETURN, "space": wx.WXK_SPACE,
    "pageup": wx.WXK_PAGEUP, "pagedown": wx.WXK_PAGEDOWN, "home": wx.WXK_HOME, "end": wx.WXK_END,
    "insert": wx.WXK_INSERT, "backspace": wx.WXK_BACK, "left": wx.WXK_LEFT, "right": wx.WXK_RIGHT,
    "up": wx.WXK_UP, "down": wx.WXK_DOWN, "tab": wx.WXK_TAB, "escape": wx.WXK_ESCAPE,
}
for _n in range(1, 13):
    _NAMED[f"f{_n}"] = getattr(wx, f"WXK_F{_n}")
_BY_CODE = {code: name for name, code in _NAMED.items()}
_DISPLAY = {
    "return": "Enter", "pageup": "PageUp", "pagedown": "PageDown", "delete": "Delete", "space": "Space",
    "home": "Home", "end": "End", "insert": "Insert", "backspace": "Backspace", "left": "Left",
    "right": "Right", "up": "Up", "down": "Down", "tab": "Tab", "escape": "Escape",
}
_MODIFIER_CODES = {wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT, wx.WXK_WINDOWS_LEFT, wx.WXK_WINDOWS_RIGHT}
_MODIFIER_CODES = {wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT, wx.WXK_WINDOWS_LEFT, wx.WXK_WINDOWS_RIGHT}

_load = None
_save = None
_bindings = None  # id -> effective text ("" = no shortcut)
_parsed = {}
_labels = None


def configure(load, save):
    """load() -> JSON text or None; save(text)."""
    global _load, _save, _bindings
    _load, _save, _bindings = load, save, None


def reload():
    global _bindings
    _bindings = None


# ---------------- parsing ----------------

def _code(evt):
    code = evt.GetKeyCode()
    return wx.WXK_RETURN if code == wx.WXK_NUMPAD_ENTER else code


def _name_to_code(part):
    if part in _NAMED:
        return _NAMED[part]
    if len(part) == 1 and 33 <= ord(part.upper()) <= 126:
        return ord(part.upper())
    return None


def _parse(text, digits=False):
    """(ctrl, alt, shift, key) or None. key is None for a modifiers-only (digits) shortcut."""
    if not isinstance(text, str):
        return None
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    mods = {"ctrl": False, "alt": False, "shift": False}
    key = None
    for i, part in enumerate(parts):
        if part in mods:
            mods[part] = True
        elif not digits and key is None and i == len(parts) - 1:
            key = _name_to_code(part)
            if key is None:
                return None
        else:
            return None
    flags = (mods["ctrl"], mods["alt"], mods["shift"])
    if digits:
        return flags + (None,) if any(flags) else None
    return flags + (key,) if key is not None else None


def _parse_any(text):
    return _parse(text) or _parse(text, True)


def _to_text(spec):
    ctrl, alt, shift, key = spec
    parts = [n for n, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift)) if on]
    if key is not None:
        parts.append(_BY_CODE.get(key) or chr(key).lower())
    return "+".join(parts)


def _key_display(key):
    name = _BY_CODE.get(key)
    if name is None:
        return chr(key).upper()
    return _DISPLAY.get(name) or name.upper()


def _ensure():
    global _bindings
    if _bindings is not None:
        return
    overrides = {}
    try:
        raw = _load() if _load else None
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            overrides = data
    except (ValueError, TypeError):
        pass
    _bindings = {}
    _parsed.clear()
    for aid, default, _group, _scopes, mode in _DEFS:
        digits = mode == _FIXED_DIGITS
        candidates = [overrides.get(aid), default] if mode == _EDIT else [default]
        chosen, spec = "", None
        for text in candidates:
            if text == "":
                break
            parsed = _parse(text, digits)
            if parsed is not None:
                chosen, spec = _to_text(parsed), parsed
                break
        _bindings[aid] = chosen
        if spec is not None:
            _parsed[aid] = spec


# ---------------- use in key handlers ----------------

def matches(evt, action_id):
    _ensure()
    spec = _parsed.get(action_id)
    if spec is None or spec[3] is None:
        return False
    return _code(evt) == spec[3] and (evt.ControlDown(), evt.AltDown(), evt.ShiftDown()) == spec[:3]


def display(action_id):
    """Shortcut as shown in labels, e.g. "Ctrl+Shift+PageUp"; "" if none."""
    _ensure()
    return display_text(_bindings.get(action_id, ""))


def display_text(text):
    spec = _parse_any(text)
    if spec is None:
        return ""
    ctrl, alt, shift, key = spec
    parts = [n for n, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift)) if on]
    parts.append("1-9" if key is None else _key_display(key))
    return "+".join(parts)


def hint(action_id):
    """" (Ctrl+N)" to append to a button label; "" if the action has no shortcut."""
    text = display(action_id)
    return f" ({text})" if text else ""


def match_group(evt, group_id):
    """Id of the first action in that group whose shortcut is evt, or None."""
    _ensure()
    for aid, _default, grp, _scopes, _mode in _DEFS:
        if grp == group_id and matches(evt, aid):
            return aid
    return None


# ---------------- settings page support ----------------

def actions():
    return [d[0] for d in _DEFS]


def group(action_id):
    return _BY_ID[action_id][2]


def is_fixed(action_id):
    return _BY_ID[action_id][4] != _EDIT


def default(action_id):
    return _BY_ID[action_id][1]


def effective():
    _ensure()
    return dict(_bindings)


def save(bindings):
    """Stores only what differs from the defaults (fixed rows are never stored)."""
    overrides = {
        aid: bindings[aid] for aid, default, _g, _s, mode in _DEFS
        if mode == _EDIT and aid in bindings and bindings[aid] != default
    }
    if _save:
        _save(json.dumps(overrides))
    reload()


def event_to_text(evt):
    """Shortcut text for a captured key press, or None if it is only a modifier or unusable."""
    code = _code(evt)
    if code in _MODIFIER_CODES:
        return None
    if code in _BY_CODE or 33 <= code <= 126:
        return _to_text((evt.ControlDown(), evt.AltDown(), evt.ShiftDown(), code))
    return None


def _overlap(a, b):
    a, b = set(a), set(b)
    return (
        bool(a & b)
        or ("window" in a and bool(b & _UNDER_WINDOW))
        or ("window" in b and bool(a & _UNDER_WINDOW))
    )


def _collide(a, b):
    if a[:3] != b[:3]:
        return False
    ka, kb = a[3], b[3]
    if ka is None and kb is None:
        return True
    if ka is None:
        return ord("1") <= kb <= ord("9")
    if kb is None:
        return ord("1") <= ka <= ord("9")
    return ka == kb


def conflicts(action_id, text, bindings=None):
    """Ids of other rows whose shortcut would clash with `text` (bindings: staged id -> text map)."""
    spec = _parse(text)
    if spec is None:
        return []
    scopes = _BY_ID[action_id][3]
    current = bindings if bindings is not None else effective()
    result = []
    for aid, _default, _group, otherScopes, _mode in _DEFS:
        if aid == action_id or not _overlap(scopes, otherScopes):
            continue
        other = _parse_any(current.get(aid))
        if other is not None and _collide(spec, other):
            result.append(aid)
    return result


def check(action_id, text, bindings=None):
    """(error, warning): translated strings or None. An error means the shortcut must not be saved."""
    spec = _parse(text)
    if spec is None:
        # Translators: Keymap error: the pressed key can't be a shortcut.
        return _("That key can't be used."), None
    if spec in (
        (False, False, False, wx.WXK_TAB), (False, False, True, wx.WXK_TAB),
        (False, False, False, wx.WXK_UP), (False, False, False, wx.WXK_DOWN),
    ):
        # Translators: Keymap error: Tab, Shift+Tab, Up and Down are for moving around and can't be shortcuts.
        return _("Tab, Shift+Tab, Up and Down are for moving around and can't be assigned."), None
    clashes = conflicts(action_id, text, bindings)
    if clashes:
        # Translators: Keymap error. {} is a comma-separated list of action names that already use the shortcut.
        return _("Already used by: {}.").format(", ".join(label(c) for c in clashes)), None
    ctrl, alt, _shift, key = spec
    printable = key not in _BY_CODE
    warnings = []
    if printable and not (ctrl or alt):
        # Translators: Keymap warning: a bare character key is also used to type and to search lists by typing.
        warnings.append(_("A letter, digit or symbol alone stops type-to-search in lists."))
    if alt and not ctrl and printable and chr(key).isalpha():
        # Translators: Keymap warning: button access keys also use Alt plus a letter.
        warnings.append(_("Alt with a letter may clash with a button access key."))
    return None, " ".join(warnings) or None


def label(action_id):
    global _labels
    if _labels is None:
        _labels = {
            # Translators: Keymap action name.
            "check_updates": _("Check for updates"),
            # Translators: Keymap action name.
            "check_all": _("Check all open tabs for updates"),
            # Translators: Keymap action name.
            "new_item": _("New post, chat or list"),
            # Translators: Keymap action name.
            "settings": _("Open settings"),
            # Translators: Keymap action name.
            "remove_tab": _("Remove current tab"),
            # Translators: Keymap action name.
            "rename_tab": _("Rename current tab"),
            # Translators: Keymap action name.
            "move_tab_left": _("Move tab left"),
            # Translators: Keymap action name.
            "move_tab_right": _("Move tab right"),
            # Translators: Keymap action name.
            "next_account": _("Switch to the next account"),
            # Translators: Keymap action name.
            "previous_account": _("Switch to the previous account"),
            # Translators: Keymap action name.
            "close_window": _("Close the main window"),
            # Translators: Keymap action name.
            "select_tab": _("Go to tab number"),
            # Translators: Keymap action name.
            "load_older": _("Load older posts"),
            # Translators: Keymap action name.
            "clear_cache": _("Clear this tab's cache"),
            # Translators: Keymap action name.
            "next_unread": _("Go to the next unread post"),
            # Translators: Keymap action name.
            "delete_post": _("Delete your post, or undo your repost"),
            # Translators: Keymap action name.
            "jump_user_previous": _("Jump to the previous post by or mentioning the same user"),
            # Translators: Keymap action name.
            "jump_user_next": _("Jump to the next post by or mentioning the same user"),
            # Translators: Keymap action name.
            "post_action": _("Post action menu"),
            # Translators: Keymap action name.
            "user_action": _("User action menu"),
            # Translators: Keymap action name.
            "copy_row": _("Copy the focused row"),
            # Translators: Keymap action name.
            "jump_row": _("Jump to a row number"),
            # Translators: Keymap action name.
            "reveal_warning": _("Show the full post (also reveals a content warning)"),
            # Translators: Keymap action name.
            "select_all": _("Select all posts"),
            # Translators: Keymap action name.
            "toggle_advanced": _("Show or hide advanced search in Explore"),
            # Translators: Keymap action name.
            "remove_list": _("Remove the selected list"),
            # Translators: Keymap action name.
            "nth_item": _("Read the Nth newest item"),
            # Translators: Keymap action name.
            "enter_action": _("Do the Enter key action chosen in Settings > General"),
            # Translators: Keymap action name.
            "chat_check_all": _("Check all chats for updates"),
            # Translators: Keymap action name.
            "next_unread_message": _("Go to the next unread message"),
            # Translators: Keymap action name.
            "jump_to_replied": _("Jump to the message this one replies to"),
            # Translators: Keymap action name.
            "jump_to_reply": _("Jump to the reply to this message"),
            # Translators: Keymap action name.
            "send": _("Send message or post"),
            # Translators: Keymap action name.
            "nth_message": _("Read the Nth newest message"),
            # Translators: Keymap action name.
            "close_dialog": _("Close the new post window"),
            # Translators: Keymap action name.
            "select_page": _("Go to settings page number"),
            # Translators: Keymap action name.
            "post_reply": _("Reply to the post"),
            # Translators: Keymap action name.
            "post_reply_all": _("Reply to the post, mentioning everyone else in it"),
            # Translators: Keymap action name.
            "post_quote": _("Quote the post"),
            # Translators: Keymap action name.
            "post_repost": _("Repost, or undo the repost"),
            # Translators: Keymap action name.
            "post_like": _("Like or unlike the post"),
            # Translators: Keymap action name.
            "post_save": _("Save or unsave the post"),
            # Translators: Keymap action name.
            "post_mark_read": _("Mark the post as read"),
            # Translators: Keymap action name.
            "post_mark_unread": _("Mark the post as unread"),
            # Translators: Keymap action name.
            "post_copy_text": _("Copy the post text"),
            # Translators: Keymap action name.
            "post_copy_link": _("Copy the link to the post"),
            # Translators: Keymap action name.
            "post_view_thread": _("View the thread"),
            # Translators: Keymap action name.
            "post_view_likes": _("View who liked the post"),
            # Translators: Keymap action name.
            "post_view_reposts": _("View who reposted the post"),
            # Translators: Keymap action name.
            "post_view_quotes": _("View the post's quotes"),
            # Translators: Keymap action name.
            "post_share_to_chat": _("Share the post to a chat"),
            # Translators: Keymap action name.
            "post_mute_thread": _("Mute or unmute the thread"),
            # Translators: Keymap action name.
            "post_hide": _("Hide the post for me"),
            # Translators: Keymap action name.
            "post_report": _("Report the post"),
            # Translators: Keymap action name.
            "post_pin": _("Pin or unpin your post on your profile"),
            # Translators: Keymap action name.
            "post_edit_replies": _("Edit who can reply to your post"),
            # Translators: Keymap action name.
            "user_profile": _("View the user's profile"),
            # Translators: Keymap action name.
            "user_timeline": _("View the user's timeline"),
            # Translators: Keymap action name.
            "user_followers": _("View the user's followers"),
            # Translators: Keymap action name.
            "user_following": _("View who the user follows"),
            # Translators: Keymap action name.
            "user_known_followers": _("View known followers of the user"),
            # Translators: Keymap action name.
            "user_lists": _("View the user's lists"),
            # Translators: Keymap action name.
            "user_start_chat": _("Start a chat with the user"),
            # Translators: Keymap action name.
            "user_follow": _("Follow or unfollow the user"),
            # Translators: Keymap action name.
            "user_mute": _("Mute or unmute the user"),
            # Translators: Keymap action name.
            "user_block": _("Block or unblock the user"),
            # Translators: Keymap action name.
            "user_subscribe": _("Subscribe or unsubscribe to the user's posts"),
            # Translators: Keymap action name.
            "user_add_to_list": _("Add the user to a list"),
            # Translators: Keymap action name.
            "user_copy_url": _("Copy the user's profile link"),
            # Translators: Keymap action name.
            "user_open_web": _("Open the user's profile on bsky.app"),
            # Translators: Keymap action name.
            "user_report": _("Report the user"),
            # Translators: Keymap action name.
            "message_reply": _("Reply to the message"),
            # Translators: Keymap action name.
            "message_react": _("React to the message"),
            # Translators: Keymap action name.
            "message_copy": _("Copy the message text"),
            # Translators: Keymap action name.
            "message_show": _("Show the full message"),
            # Translators: Keymap action name.
            "message_delete": _("Delete the message for me"),
        }
    return _labels.get(action_id, action_id)


def group_label(group_id):
    return {
        # Translators: Keymap group name.
        "window": _("Main window"),
        # Translators: Keymap group name.
        "tab": _("Tabs with lists"),
        # Translators: Keymap group name.
        "chat": _("Chat"),
        # Translators: Keymap group name.
        "compose": _("New post window"),
        # Translators: Keymap group name.
        "dialog": _("Settings window"),
        # Translators: Keymap group name: post actions that have no default shortcut and are yours to assign.
        "post": _("Post actions"),
        # Translators: Keymap group name: user actions that have no default shortcut and are yours to assign.
        "user": _("User actions"),
        # Translators: Keymap group name: chat message actions that have no default shortcut and are yours to assign.
        "message": _("Chat message actions"),
    }.get(group_id, group_id)
