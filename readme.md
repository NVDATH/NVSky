# NVSky for NVDA

> NVSky is an add-on that lets you use [Bluesky](https://bsky.app) (the AT Protocol social network) directly from NVDA, without ever needing a browser.
> Every part of Bluesky — your timeline, notifications, direct messages, lists, feeds, and search — is presented through report-mode lists and dialogs built specifically for screen reader use, with full keyboard access and no reliance on how the Bluesky website happens to render for sighted users.
> You can run multiple Bluesky accounts side by side and switch between them at any time.
> Posts, notifications, chat messages, and lists are all cached locally in an encrypted database, so your feeds open instantly and only reach out to the network when you ask for updates or when background sync is due.
> Direct messages (including group chats) are fully supported: sending, replying, reacting with emoji, message requests, and group management (members, invite links, join requests, locking).
> Content label handling (adult content categories, Show/Warn/Hide) and muted words/users/lists all mirror what Bluesky's own official app offers, so your moderation preferences carry over.
> NVSky does not use its own player, video previewer, or web view for anything — images and videos are downloaded and handed off to your system's default viewer/player (or to the Be My Eyes app), and links open in your normal browser.

## Requirements

- NVDA 2026.1 or later, 64-bit.
- A Bluesky account and an **App Password** (not your regular account password — see [Logging in](#logging-in) below).

## Opening NVSky

Press **NVDA+Shift+Y** to open NVSky's main window. If no account is logged in yet, you'll be taken straight to the login dialog instead.

If NVDA+Shift+Y conflicts with another add-on or command on your system, you can change it via `NVDA -> Preferences -> Input Gestures...` under the "NVSky" category. The same category also has an unbound command, **Open a quick new post window**, which opens the compose dialog directly without opening the main window first. To use it, assign it a gesture yourself from `NVDA -> Preferences -> Input Gestures... -> NVSky -> Open a quick new post window` — it has no default shortcut out of the box.

### Logging in

NVSky signs in using a Bluesky **App Password**, not your normal account password. From the login dialog, press **Generate App Password...** to open Bluesky's app-password page in your browser, create one there, then paste it back (format looks like `abcd-efgh-ijkl-mnop`). This keeps your main password out of any third-party app, including this one.

You can add, remove, and switch between several accounts from **Settings > Accounts** at any time.

## The main window

NVSky's main window holds every part of the add-on as tabs in a single window, with a toolbar shared across all of them:

- **Check for updates (F5)** — refreshes whichever tab is currently open.
- **New post... (Ctrl+N)** — opens the compose window. This button (and Ctrl+N) automatically relabels itself to **New chat...** while a Chat tab is active, or **New list...** while the Lists tab is active.
- **Find lists by user...** — only shown while a Lists tab is active; searches for another account's public lists to browse or subscribe to.
- **Settings... (Ctrl+P)** — opens NVSky's settings dialog.
- **Remove current tab (Ctrl+W)** — closes whichever pop-out tab (a thread, a user's followers, a conversation, etc.) is currently open. Does nothing on a permanent tab.
- **Close** — closes the NVSky window (same as Escape).

### Tabs

**Home** is always shown. Six more tabs are optional and can be turned on or off from **Settings > Display**:

- **Notifications** — likes, reposts, follows, replies, mentions, and quotes.
- **Explore** — search posts, people, starter packs, or feeds.
- **Saved** — your bookmarked posts.
- **Likes** — posts you've liked, newest first.
- **Chat** — direct messages, including group chats.
- **Lists** — lists you've created or subscribed to.

Home's own feed filter (a dropdown at the top of the tab) switches between **Following**, **Discover**, and any custom feed you've added via **Settings > Feed manager**.

Selecting a post, notification, conversation, or user elsewhere in the add-on can open further tabs on demand — a thread, a user's timeline, a list of followers, a search result pinned for later, and so on. These are removable with Ctrl+W and are remembered and reopened automatically the next time you start NVSky.

### Window-level keyboard shortcuts

These work anywhere in the main window:

- **F5** — check the current tab for updates.
- **Ctrl+F5** — check every currently open tab for updates in one pass.
- **Ctrl+N** — open a new post/chat/list, depending on context (see above).
- **Ctrl+W** — remove the current (removable) tab.
- **Ctrl+Shift+F2** — rename the current tab (removable tabs only).
- **Ctrl+1** through **Ctrl+9** — jump straight to that tab by position.
- **Ctrl+Shift+Page Up / Page Down** — move the current tab left/right in the tab order. The new order is remembered.
- **Ctrl+P** — open Settings.
- **Escape** — close the NVSky window. Disabled while you have unsent text in a multi-line field (e.g. mid-message in Chat's compose box), so you can't lose what you were typing by accident.

## Feeds, lists, and posts

Home, Saved, Likes, Lists (curation lists), Notifications, and any pop-out post list (a thread, a user's timeline, a feed preview, a pinned search) all share the same list behavior and shortcuts.

### List navigation shortcuts

- **Up / Down arrows** — move through the list; a sound plays at the top/bottom.
- **Space** — jump to the next unread item (Home, Saved, Likes, Lists, Notifications).
- **Ctrl+A** — select every item, for menu actions that support multiple posts at once (currently: mark as read/unread).
- **Left / Right arrows** — jump to the previous/next post involving the same user as the one currently focused — whoever posted it, reposted it, replied to it, or was mentioned in it (Home and Lists only).
- **Alt+1** through **Alt+9** — announce the Nth newest (or oldest, depending on your sort order) item without moving focus.
- **Ctrl+C** — copy the full text of the focused row, including a working link back to the post — not just what's visible on screen.
- **Ctrl+J** — jump straight to a row by number.
- **Shift+F5** — fetch older items past what's currently loaded.
- **Ctrl+Delete** — clear this tab's cached items (asks for confirmation).
- **Ctrl+Space** — reveal the real text/embed of a content-warned post, once, without changing your content label settings.
- **Delete** — quick-delete the focused post, if it's your own (or undo your own repost of someone else's post).

### Checking unread counts

Every list tab keeps its own unread count, shown in that tab's status bar as "X unread, Y total" — this is separate from what's spoken as you arrow through items. To hear it at any time without moving focus off the list, press **NVDA+End** (NVDA's standard "report status bar" command, not something specific to NVSky). This works the same way on the Chat tab (unread messages and conversation count) and everywhere else in the add-on that has a status bar.

### Post action menu (Alt+A)

Opens a menu of everything you can do with the focused post:

- **Manage post...** (your own posts only) — Pin/unpin to profile, Edit who can reply, Delete post
- **Reply...**
- **Repost** / **Undo repost**
- **Quote post...**
- **Mark as...** — Read / Unread
- **Like** / **Unlike**
- **Save** / **Unsave**
- **Copy...** — post text, or a link to the post
- **View...** — Thread, Likes, Reposts, Quotes (each opens its own pop-out tab)
- **Embed...** — open, copy, or send an attached image/video to Be My Eyes; open or copy a link preview's URL
- **More...** — Share to chat, Mute/Unmute thread, Hide post for me, Report post

Selecting several posts (Ctrl+A, or checking multiple rows) narrows this down to a **Mark as...** bulk action instead.

### User action menu (Alt+U)

Opens a menu of actions for a user connected to the focused post (the author, or — via a submenu — whoever reposted it, replied to it, or was mentioned):

- **View...** — Profile, Timeline, Followers, Following, Known followers, Lists they're on
- **Start chat...**
- **Follow** / **Unfollow**
- **Mute** / **Unmute**
- **Block** / **Unblock**
- **Add to list...**
- **Copy...** — profile URL, or open the profile on bsky.app
- **Report user...**

### Composing a post

The compose window (Ctrl+N, or the standalone quick-new-post command) supports:

- Up to 4 image attachments (with alt text prompts) or a single video attachment, with live upload progress.
- Automatic link-preview card detection, with a picker if your post text has more than one URL.
- Content self-labels (Pornography, Sexually suggestive, Nudity, Graphic media) via a checklist.
- A live character counter matching Bluesky's 300-grapheme limit.
- Ctrl+Enter to post, Ctrl+W to close without posting.

Replying and quoting open the same window pre-filled with the original post's context.

## Chat

The Chat tab shows a tree of your conversations on the left and the selected conversation's messages on the right, plus a compose box at the bottom. Opening a conversation from anywhere else in the add-on (a user's profile, "Start chat...", sharing a post) can pop it out into its own removable tab instead, so you can keep several conversations open side by side.

- **Ctrl+Enter** — send the composed message.
- **F5** — refresh just the selected conversation.
- **Shift+F5** — check every conversation for updates.
- **Alt+1** through **Alt+9** — announce the Nth newest message.
- **Left arrow** — jump to the message a reply refers to.
- **Right arrow** — jump forward to whatever replied to the currently focused message.
- **Space** — jump to the next unread message.
- **Ctrl+C** — copy the focused message's full text (including sender and time).
- **Ctrl+J** — jump to a message by row number.

Right-click (or the Menu key) on a message opens Reply, React..., Copy text, Show message... (for anything too long to read from the list alone), and Delete for me.

Right-click on a conversation in the tree gives you Mark read, Mark all read, Mute/Unmute, Leave conversation, and — for groups you own — Lock/Unlock, Manage members, Edit name, Join requests, and Invite link management. A message request (someone messaging you for the first time) shows dedicated Accept/Decline buttons instead of a compose box until you accept it.

Starting a new chat (Ctrl+N while a Chat tab is active) lets you search for one or more recipients; adding more than one, or setting a group name, starts a group chat instead of a 1:1 conversation.

## Lists

The Lists tab shows every list you've created or subscribed to. Selecting a **curation list** shows its timeline on the right, using the exact same post list and Alt+A/Alt+U behavior as Home. Selecting a **moderation list** shows its member roster instead.

- Right-click a list for Remove, Show in new tab, Manage members, and (moderation lists) Mute/Unmute or Block/Unblock.
- **Find lists by user...** (toolbar) searches for someone else's public lists, so you can open one of their curation lists as a tab, or subscribe to one of their moderation lists.
- Ctrl+N (or the toolbar's relabeled New post button) creates a new list.

## Explore

Search **Posts**, **People**, **Starter packs**, or **Feeds** from one search box, switching result type with the radio buttons. An **Advanced search (Alt+V)** panel adds From-handle, Since, Until, and Language filters for post searches. Results carry the same Alt+A/Alt+U actions as everywhere else, plus type-specific actions (following everyone in a starter pack, adding a feed to your saved feeds, and so on).

## Settings

Open Settings from the main window's toolbar (Ctrl+P), or via `NVDA -> Preferences -> NVSky Settings...`. Nothing is saved until you press **OK** or **Apply** — Cancel (or Escape) discards any unsaved changes.

- **Accounts** — add, remove, or switch the active Bluesky account.
- **General** — what Enter does on a focused post (view thread, reply, quote, repost, like, or toggle read); which categories speak up when background sync finds something new; how often each category (Home, Notifications, Saved/Likes, Chat, Lists, Search, Profile, Thread) syncs in the background, or 0 to disable it; and a **Clear all cache** button for the active account.
- **Display** — feed sort order (newest/oldest first), whether authors show as display name or handle, the post time format (relative, full date/time, or a custom pattern), which optional tabs are shown, per-category **content label** visibility (Show/Warn/Hide, cycled with Space — plus the master Adult content switch), and per-category **notification** settings (Off/Everyone/People you follow, also cycled with Space) — both of these apply server-side, the same as changing them in Bluesky's own app.
- **Feed manager** — reorder, pin, or remove your subscribed custom feeds.
- **Sound** — pick a sound pack, and choose which events play a sound at all.
- **Profile** — edit your display name, bio, avatar, and banner.
- **Muted words** — add or remove muted words/tags.
- **Muted users** / **Blocked users** — separate tabs; check users and press Undo to stage unmuting/unblocking them, applied on OK/Apply.

### Sound packs

Sound packs live in NVSky's own `SoundPack` folder, one subfolder per pack. Add your own by creating a new subfolder there and dropping in `.wav` files named after each event (e.g. `like.wav`, `error.wav`, `new_message.wav`) — a pack doesn't need every file, missing ones just stay silent. Pick "Silent / No sound" in Settings > Sound to disable all of it.

## A note on privacy and security

NVSky takes real care to keep your Bluesky data off-limits to anyone else with access to your computer:

- Your **App Password** is encrypted with Windows' own DPAPI before it's written to disk.
- Your **entire local database** — every cached post, notification, chat message, and list, for every account you've logged into — is encrypted as a whole, using [SQLCipher](https://www.zetetic.net/sqlcipher/), not just the sensitive fields. The encryption key itself is also DPAPI-protected.

Both of these are tied to DPAPI, which Windows scopes to **the specific Windows user account, on the specific computer, that created them**. This is a deliberate security choice, but it comes with a real limitation you should know about before relying on NVSky:

- **It will not work with a portable copy of NVDA.** If you copy your NVDA user profile (including NVSky's data) to a USB drive or another folder and run it as a portable copy, NVSky's stored App Password and database key can't be decrypted there — you'll need to log in again from scratch.
- **It does not carry over to another computer**, or to a different Windows user account on the same computer, for the same reason.
- If the DPAPI key or the database itself ever becomes unreadable (a corrupted profile, or exactly the portability situations above), NVSky detects this automatically, backs up the unreadable files with a `.bak` suffix instead of crashing, and starts fresh — you'll simply need to log back in. Nothing about your Bluesky account itself is at risk; only NVSky's own local cache is affected.

If you install NVSky on more than one computer, treat each one as needing its own separate login — this is expected behavior, not a bug.

## Known issues

- Conversation lists (`list_convos`) don't paginate past the first 50 conversations yet — accounts with more than 50 active conversations won't see the rest in the Chat tab.
- Downloaded temporary files (images/videos opened via "View embed") are only swept on NVDA startup, so they can sit in your Temp folder for up to about 24 hours before being cleaned up.
- Switching the active account while the Settings dialog is open, and very large notification/chat volumes, haven't been directly tested yet and may behave unexpectedly.
- Several Bluesky endpoints used for group chats, join links, and activity subscriptions are still marked internally as low-confidence/experimental, since they haven't all been exercised against a real server in every scenario. If you hit an error in one of these areas, please report it with as much detail as you can.

## Additional information

NVSky is built on the [atproto](https://github.com/MarshalX/atproto) Python SDK for talking to Bluesky's AT Protocol servers. We're grateful to its maintainers and contributors for making a project like this possible.

The overall usage pattern (a multi-tab, multi-account desktop client with cached local feeds) took early inspiration from the legendary **Qwitter**, a Twitter client many screen reader users remember fondly, and several individual features were adapted from ideas found in **OpenTween** — most notably the "jump to the next/previous post involving this user" navigation (Left/Right arrows in feeds).

If you'd like to compare NVSky against another accessible way to use Bluesky, **[FastSMRW](https://github.com/masonasons/FastSMRW)** is a separate accessible Bluesky client — not an NVDA add-on, but a standalone application — built by a different author. Feel free to try both and use whichever one fits how you work.

This add-on is under active, closed development and has not yet had a public release. Feature set and shortcuts described here reflect the current development build and may still change before release.
