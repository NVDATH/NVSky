"""
Shared "search for users, tick results, act on the ticked ones" behavior for NVSky dialogs: the
new-chat dialog and the group and list member dialogs all show a search box, a check list of
search results and an Add button, and manage another check list of people next to it.
"""
import wx

import ui as nvdaUi

from . import client
from . import uiutil

SEARCH_DEBOUNCE_MS = 400


def take_checked(checkList, items, button, emptyMessage):
    """
    The items of `items` that are ticked in `checkList`. None when the (possibly hidden) button
    that triggered this isn't shown: a hidden button still fires its mnemonic. Announces
    `emptyMessage` and returns [] when nothing is ticked.
    """
    if not button.IsShown():
        return None
    checked = list(checkList.CheckedItems)
    if not checked:
        nvdaUi.message(emptyMessage)
        return []
    return [items[i] for i in checked if 0 <= i < len(items)]


class UserSearchMixin:
    """
    For a dialog with a user search box, a check list of results and an Add button. The dialog
    builds the widgets, calls _bindUserSearch() once, and implements _excludedDids() so people
    already in its own list aren't offered again. Results are in self._userSuggestions.
    """

    def _bindUserSearch(self, searchText, resultsLabel, resultsList, addButton):
        self._usText = searchText
        self._usLabel = resultsLabel
        self._usList = resultsList
        self._usAdd = addButton
        self._userSuggestions = []
        searchText.Bind(wx.EVT_TEXT, self._userSearchTextChanged)

    def _excludedDids(self):
        return set()

    def _userSearchTextChanged(self, evt):
        wx.CallLater(SEARCH_DEBOUNCE_MS, self._userSearchRun, self._usText.GetValue())

    def _userSearchRun(self, query):
        try:
            if query != self._usText.GetValue():
                return  # a newer keystroke already superseded this debounce
        except RuntimeError:
            return  # the dialog was closed meanwhile
        if not query.strip():
            self._showUserSuggestions([])
            return

        def worker():
            try:
                atprotoClient = client.get_client_for_active_account()
                results = client.search_actors_typeahead(atprotoClient, query)
                error = None
            except Exception as e:
                results = []
                error = str(e)
            wx.CallAfter(self._userSearchDone, query, results, error)

        uiutil.start_worker(worker, progress=False)

    @uiutil.safe_ui_callback
    def _userSearchDone(self, query, results, error):
        if error or query != self._usText.GetValue():
            return
        excluded = self._excludedDids()
        self._showUserSuggestions([r for r in results if r["did"] not in excluded])

    def _showUserSuggestions(self, users):
        self._userSuggestions = users
        hasResults = bool(users)
        self._usLabel.Show(hasResults)
        self._usList.Show(hasResults)
        self._usAdd.Show(hasResults)
        self.Layout()
        self._usList.Set([uiutil.user_choice_label(u) for u in users])
        self._usList.CheckedItems = []
        if users:
            self._usList.SetSelection(0)

    def _clearUserSearch(self):
        self._usText.ChangeValue("")
        self._showUserSuggestions([])

    def _takeCheckedSuggestions(self, emptyMessage):
        return take_checked(self._usList, self._userSuggestions, self._usAdd, emptyMessage)
