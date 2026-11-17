"""
Settings > Keymap page for NVSky: the shortcut list with change/remove/reset, and the dialog that
captures a new key press. Changes are staged in the page and saved by the settings dialog's
OK/Apply (see KeymapPanel.apply).
"""
import wx

import ui as nvdaUi

from . import keymap


class _KeyCaptureDialog(wx.Dialog):
    """Captures one key press as a new shortcut. Escape cancels."""

    def __init__(self, parent, actionId, bindings):
        # Translators: Title of the dialog that captures a new keyboard shortcut.
        super().__init__(parent, title=_("Change shortcut"))
        self._actionId = actionId
        self._bindings = bindings
        self.text = None
        self.warning = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        # Translators: Instruction in the change-shortcut dialog. {} is the action name.
        prompt = _("Press the new shortcut for {}. Press Escape to cancel.")
        promptLabel = wx.StaticText(self, label=prompt.format(keymap.label(actionId)))
        promptLabel.Wrap(420)
        sizer.Add(promptLabel, flag=wx.ALL, border=10)

        # Translators: Label of the field showing the shortcut being captured.
        shortcutLabel = wx.StaticText(self, label=_("&Shortcut:"))
        sizer.Add(shortcutLabel, flag=wx.LEFT | wx.RIGHT, border=10)
        self.shortcutText = wx.TextCtrl(
            self, value=keymap.display_text(bindings[actionId]), style=wx.TE_READONLY
        )
        sizer.Add(self.shortcutText, flag=wx.EXPAND | wx.ALL, border=10)

        # Translators: Button to close the change-shortcut dialog without changing anything.
        cancelBtn = wx.Button(self, wx.ID_CANCEL, label=_("&Cancel"))
        sizer.Add(cancelBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

        self.SetSizerAndFit(sizer)
        self.CentreOnScreen()
        self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
        self.shortcutText.SetFocus()

    def onCharHook(self, evt):
        keyCode = evt.GetKeyCode()
        if keyCode == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        text = keymap.event_to_text(evt)
        if text is None:
            if keyCode not in (wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT):
                # Translators: Announced when a pressed key can't be used as a shortcut.
                nvdaUi.message(_("That key can't be used."))
            return
        error, warning = keymap.check(self._actionId, text, self._bindings)
        shown = keymap.display_text(text)
        if error:
            self.shortcutText.SetValue(shown)
            nvdaUi.message(f"{shown}: {error}")
            return
        self.text = text
        self.warning = warning
        self.EndModal(wx.ID_OK)


class KeymapPanel(wx.Panel):
    """Settings > Keymap. Changes are staged in self._bindings and saved on OK/Apply."""

    def __init__(self, parent):
        super().__init__(parent)
        self._ids = keymap.actions()
        self._bindings = keymap.effective()
        self.changed = False  # True once saved, so closing Settings rebuilds the tabs' button labels

        sizer = wx.BoxSizer(wx.VERTICAL)

        # Translators: Label above the shortcut list in Settings > Keymap.
        listLabel = wx.StaticText(self, label=_("&Shortcuts:"))
        sizer.Add(listLabel, flag=wx.LEFT | wx.TOP, border=10)
        self.shortcutList = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        # Translators: Column header for the name of an action in the keymap list.
        self.shortcutList.InsertColumn(0, _("Action"), width=320)
        # Translators: Column header for an action's keyboard shortcut in the keymap list.
        self.shortcutList.InsertColumn(1, _("Shortcut"), width=170)
        # Translators: Column header for where an action's shortcut works in the keymap list.
        self.shortcutList.InsertColumn(2, _("Where"), width=140)
        sizer.Add(self.shortcutList, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

        buttonRow = wx.BoxSizer(wx.HORIZONTAL)
        # Translators: Button to change the selected shortcut.
        self.changeButton = wx.Button(self, label=_("&Change..."))
        # Translators: Button to remove the selected shortcut, leaving the action without one.
        self.removeButton = wx.Button(self, label=_("Re&move shortcut"))
        # Translators: Button to put the selected shortcut back to its default.
        self.resetButton = wx.Button(self, label=_("&Reset"))
        # Translators: Button to put every shortcut back to its default.
        self.resetAllButton = wx.Button(self, label=_("Reset &all"))
        buttonRow.Add(self.changeButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.removeButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.resetButton, flag=wx.RIGHT, border=5)
        buttonRow.Add(self.resetAllButton)
        sizer.Add(buttonRow, flag=wx.LEFT | wx.RIGHT, border=10)

        note = wx.StaticText(
            self,
            # Translators: Explanatory note below the keymap list in Settings > Keymap.
            label=_(
                "Shortcuts marked fixed are built in and can't be changed; they are listed so you can see every key. "
                "Actions marked Not assigned have no shortcut yet: choose Change to give them one. "
                "Auto read has no shortcut here: assign one in NVDA's Input gestures dialog, under NVSky. "
                "Changes are saved when you press OK or Apply; shortcuts shown on buttons update when "
                "you close Settings."
            ),
        )
        note.Wrap(500)
        sizer.Add(note, flag=wx.ALL, border=10)

        self.SetSizer(sizer)

        self.changeButton.Bind(wx.EVT_BUTTON, self.onChange)
        self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
        self.resetButton.Bind(wx.EVT_BUTTON, self.onReset)
        self.resetAllButton.Bind(wx.EVT_BUTTON, self.onResetAll)
        self.shortcutList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onChange)

        self._render(target_index=0)

    def onTabActivated(self):
        self.shortcutList.SetFocus()

    def _render(self, target_index=None):
        previouslyFocused = self.shortcutList.GetFocusedItem()
        self.shortcutList.Freeze()
        try:
            self.shortcutList.DeleteAllItems()
            for i, actionId in enumerate(self._ids):
                text = keymap.display_text(self._bindings[actionId])
                if keymap.is_fixed(actionId):
                    # Translators: Shortcut column value for a shortcut that can't be changed. {} is the shortcut.
                    text = _("{} (fixed)").format(text)
                elif not text:
                    # Translators: Shortcut column value for an action that has no shortcut.
                    text = _("Not assigned")
                elif self._bindings[actionId] != keymap.default(actionId):
                    # Translators: Shortcut column value for a shortcut changed from its default. {} is the shortcut.
                    text = _("{} (changed)").format(text)
                self.shortcutList.InsertItem(i, keymap.label(actionId))
                self.shortcutList.SetItem(i, 1, text)
                self.shortcutList.SetItem(i, 2, keymap.group_label(keymap.group(actionId)))
            if target_index is not None:
                index = target_index
            elif previouslyFocused != -1:
                index = previouslyFocused
            else:
                index = 0
            index = max(0, min(index, len(self._ids) - 1))
            self.shortcutList.Focus(index)
            self.shortcutList.Select(index)
            self.shortcutList.EnsureVisible(index)
        finally:
            self.shortcutList.Thaw()

    def _selectedIndex(self):
        index = self.shortcutList.GetFocusedItem()
        if index == -1 or index >= len(self._ids):
            # Translators: Spoken when a shortcut action is needed but none is selected.
            nvdaUi.message(_("No shortcut selected."))
            return None
        return index

    def onChange(self, evt=None):
        index = self._selectedIndex()
        if index is None:
            return
        actionId = self._ids[index]
        if self._guardFixed(actionId):
            return
        dlg = _KeyCaptureDialog(self, actionId, self._bindings)
        result = dlg.ShowModal()
        text, warning = dlg.text, dlg.warning
        dlg.Destroy()
        self.shortcutList.SetFocus()
        if result != wx.ID_OK or text is None:
            return
        self._bindings[actionId] = text
        self._render(target_index=index)
        # Translators: Announced after changing a shortcut. First {} is the action name, second {} is the new shortcut.
        message = _("{} is now {}.").format(keymap.label(actionId), keymap.display_text(text))
        if warning:
            message += " " + warning
        wx.CallLater(250, nvdaUi.message, message)

    def _guardFixed(self, actionId):
        if not keymap.is_fixed(actionId):
            return False
        # Translators: Announced when trying to change a built-in shortcut.
        nvdaUi.message(_("This shortcut is fixed and can't be changed."))
        return True

    def onRemove(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        actionId = self._ids[index]
        if self._guardFixed(actionId):
            return
        if not self._bindings[actionId]:
            # Translators: Announced when removing a shortcut from an action that has none.
            nvdaUi.message(_("No shortcut to remove."))
            return
        self._bindings[actionId] = ""
        self._render(target_index=index)
        # Translators: Announced after removing a shortcut. {} is the action name.
        nvdaUi.message(_("{} now has no shortcut.").format(keymap.label(actionId)))

    def onReset(self, evt):
        index = self._selectedIndex()
        if index is None:
            return
        actionId = self._ids[index]
        if self._guardFixed(actionId):
            return
        defaultText = keymap.default(actionId)
        if self._bindings[actionId] == defaultText:
            # Translators: Announced when resetting a shortcut that already has its default.
            nvdaUi.message(_("Already the default."))
            return
        if defaultText:
            error, _warning = keymap.check(actionId, defaultText, self._bindings)
            if error:
                nvdaUi.message(error)
                return
        self._bindings[actionId] = defaultText
        self._render(target_index=index)
        if defaultText:
            # Translators: Announced after resetting a shortcut. First {} is the action name, second {} is the default shortcut.
            nvdaUi.message(_("{} reset to {}.").format(keymap.label(actionId), keymap.display_text(defaultText)))
        else:
            # Translators: Announced after resetting an action whose default is no shortcut. {} is the action name.
            nvdaUi.message(_("{} reset: no shortcut.").format(keymap.label(actionId)))

    def onResetAll(self, evt):
        if all(self._bindings[a] == keymap.default(a) for a in self._ids):
            nvdaUi.message(_("Already the default."))
            return
        confirm = wx.MessageDialog(
            self,
            # Translators: Confirmation body for resetting every shortcut.
            _("Reset every shortcut to its default?"),
            # Translators: Title of the reset-all-shortcuts confirmation dialog.
            _("Reset all"), wx.YES_NO | wx.NO_DEFAULT,
        )
        confirmed = confirm.ShowModal() == wx.ID_YES
        confirm.Destroy()
        if not confirmed:
            return
        self._bindings = {a: keymap.default(a) for a in self._ids}
        self._render(target_index=0)
        # Translators: Announced after resetting every shortcut.
        nvdaUi.message(_("All shortcuts reset."))

    def apply(self):
        if self._bindings != keymap.effective():
            keymap.save(self._bindings)
            self.changed = True
