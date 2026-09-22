# -*- coding: UTF-8 -*-
# buildVars.py - variables used by SCons when building the addon.

def _(x):
    return x

# Add-on information variables
addon_info = {
    "addon_name": "NVSky",
    "addon_version": "2026.10.1",
    # Translators: Summary for this add-on
    "addon_summary": _("NVSky"),
    # Translators: Long description to be shown for this add-on on add-on information from add-ons manager
    "addon_description": _("""NVSky lets you use Bluesky (the AT Protocol social network) directly from NVDA, without ever needing a browser.
Every part of Bluesky is presented through accessible, shortcut-driven lists and dialogs built specifically for screen reader use — not a browser view of the Bluesky website.

Features:
• Log in with a Bluesky App Password (your regular password never leaves Bluesky's own site), and run several accounts side by side.
• Home, Notifications, Explore, Saved, Likes, Chat, and Lists — turn any of the optional tabs on or off to suit how you use Bluesky.
• Posts, notifications, chat messages, and lists are cached locally in an encrypted database, so your feeds open instantly.
• Full post actions: reply, repost, quote, like, save, mute a thread, edit who can reply, and more, all from one Post action menu.
• Full user actions: follow, mute, block, view someone's profile/timeline/followers/following, add them to a list, or start a chat.
• Direct messages, including group chats: message requests, reactions, replies, invite links, join requests, and member management.
• Content label visibility (Show/Warn/Hide) and muted words/users/lists, matching the same settings as Bluesky's own official app.
• A configurable background sync scheduler keeps every tab up to date on its own, with optional sounds and spoken announcements."""),
    "addon_author": "NVDA_TH <nvdainth@gmail.com>, assisted by A.I.",
    "addon_url": "https://github.com/NVDATH/NVSky",
    "addon_docFileName": "readme.html",
    "addon_minimumNVDAVersion": "2026.1",
    "addon_lastTestedNVDAVersion": "2026.2",
    "addon_updateChannel": "stable",
    # Translators: What's new text for this add-on version, shown in NVDA's Add-on Store.
    # NOTE: content between the CHANGELOG markers below is auto-replaced by
    # scripts/sync_changelog.py using the latest "## " section of changelog.md.
    # Do not edit by hand; edit changelog.md instead and re-run the sync script.
    "addon_changelog": _(
        # CHANGELOG-START
        """- (not synced yet — run scripts/sync_changelog.py)"""
        # CHANGELOG-END
    ),
}

pythonSources = [
    "addon/globalPlugins",
]

i18nSources = [
    "buildVars.py",
    "addon/globalPlugins/NVSky/*.py",
]

docFiles = ["readme.html"]

tests = []
excludedFiles = []
baseLanguage = "en"
markdownExtensions = []
