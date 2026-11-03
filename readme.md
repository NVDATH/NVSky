# NVSky for NVDA

NVSky is an NVDA add-on that lets you use [Bluesky](https://bsky.app) (the AT Protocol social network) without ever opening a web browser.

Everything in Bluesky is presented as lists, menus, and dialogs designed for screen reader users: your timeline, notifications, direct messages, lists, feeds, and search. Every feature can be used with the keyboard alone, and nothing depends on how the Bluesky website happens to look.

What you can do with NVSky:

- Read your timeline, notifications, saved posts, and liked posts.
- Write posts with images, video, link previews, and content labels. Reply to posts, quote them, repost them, like them, and save them.
- Send and receive direct messages, including group chats.
- Search for posts, people, starter packs, and feeds.
- Manage your lists, follows, mutes, blocks, and muted words.
- Use several Bluesky accounts and switch between them at any time.
- Hear sounds for actions and events, using sound packs you can customize.
- Have a list read to you row by row, hands-free, with auto read.

NVSky stores your posts, notifications, chat messages, and lists in an encrypted database on your computer. Your tabs open instantly. NVSky only contacts the network when you ask for updates, or when a scheduled background check is due.

NVSky does not have its own image viewer, video player, or web view. Images and videos are downloaded and opened in whatever program your system uses for them (or sent to the Be My Eyes app). Links open in your normal web browser.

## Contents

- [Requirements](#requirements)
- [Opening NVSky](#opening-nvsky)
- [Logging in](#logging-in)
- [The main window](#the-main-window)
- [Keyboard shortcuts that work in the whole window](#keyboard-shortcuts-that-work-in-the-whole-window)
- [How post lists work](#how-post-lists-work)
- [The Post action menu (Alt+A)](#the-post-action-menu-altaaa)
- [The User action menu (Alt+U)](#the-user-action-menu-altu)
- [Writing a post](#writing-a-post)
- [The tabs](#the-tabs): Home, Notifications, Explore, Saved, Likes, Chat, Lists, People
- [Tabs that open on demand](#tabs-that-open-on-demand)
- [Using more than one account](#using-more-than-one-account)
- [Real-time updates (Jetstream)](#real-time-updates-jetstream)
- [Settings](#settings)
- [Notification area icon](#notification-area-icon)
- [Reading unread counts](#reading-unread-counts)
- [Auto read](#auto-read)
- [Sound packs](#sound-packs)
- [Privacy and security](#privacy-and-security)
- [Known issues](#known-issues)
- [Additional information](#additional-information)

## Requirements

- NVDA 2026.1 or later, 64-bit.
- A Bluesky account.
- An **App Password** for that account. This is not your normal account password. See [Logging in](#logging-in).

## Opening NVSky

Press **NVDA+Shift+Y** to open the NVSky main window. If no account is logged in yet, you go straight to the login dialog instead.

If that shortcut conflicts with something else on your system, you can change it:

1. Open the NVDA menu, then Preferences, then **Input gestures...**
2. Find the **NVSky** category.
3. Choose **Open the NVSky main window** and assign the shortcut you want.

The NVSky category has three more commands. None of them has a shortcut until you assign one yourself, the same way as above:

- **Open a quick new post window** opens the compose window directly, without opening the main window first.
- **Read the unread count of every open NVSky tab** speaks your unread counts. See [Reading unread counts](#reading-unread-counts).
- **Start or stop auto read** reads the current list tab to you row by row. See [Auto read](#auto-read).

You can also open NVSky's settings from the NVDA menu: Preferences, then **NVSky Settings...**

## Logging in

NVSky signs in with a Bluesky **App Password**, not your regular password. An App Password is a separate password that you create just for one app. If you ever stop using NVSky, you can delete its App Password on Bluesky without changing your main password.

To log in:

1. In the login dialog, press **Generate App Password...**. Bluesky's App Password page opens in your browser.
2. Create a new App Password there. It looks like `abcd-efgh-ijkl-mnop`.
3. Come back to NVSky, enter your handle (or email) and paste the App Password.
4. Press **Log in**.

If what you typed doesn't look like an App Password, NVSky warns you once and lets you continue if you press Log in again.

You can add more accounts, switch between them, or remove them in **Settings > Accounts**.

The first time you log in, while no main window is open yet, NVSky does one full check of every tab so that they are filled in, and says "Sync complete." when it is done.

## The main window

The main window holds everything as tabs. Above the tabs is a toolbar that is shared by all tabs:

- **Check for updates (F5)** refreshes the tab you are on.
- **New post... (Ctrl+N)** opens the compose window. On the Chat tab this button becomes **New chat...**, and on the Lists tab it becomes **New list...**.
- **Find lists by user...** appears only on the Lists tab. It searches for someone else's public lists.
- **Settings... (Ctrl+P)** opens NVSky's settings.
- **Remove current tab (Ctrl+W)** closes the current tab, if it is a tab that can be closed. It is hidden on the permanent tabs.
- **Close** closes the NVSky window.

When you switch tabs, NVDA speaks the tab's name, and focus moves to the main control of that tab.

## Keyboard shortcuts that work in the whole window

- **F5** checks the current tab for updates.
- **Ctrl+F5** checks every open tab for updates in one go and tells you which ones have something new.
- **Ctrl+N** starts a new post, chat, or list, depending on the tab you are on.
- **Ctrl+W** closes the current tab (only for tabs that can be closed).
- **Ctrl+Shift+F2** renames the current tab (only for tabs that can be closed).
- **Ctrl+1** to **Ctrl+9** jump to the tab in that position.
- **Ctrl+Shift+Page Up** and **Ctrl+Shift+Page Down** move the current tab left or right. NVSky remembers the order.
- **Ctrl+P** opens Settings.
- **Escape** closes the NVSky window. While you have text typed into a multi-line box (for example a half-written chat message), Escape does nothing, so you can't lose your text by accident.

Most of these shortcuts can be changed, and many actions that have no shortcut yet can be given one, in [Settings > Keymap](#keymap).

NVSky also remembers which tab you were on when you closed it, and opens on that tab next time.

## How post lists work

Home, Saved, Likes, Lists (curation lists), Notifications, and every tab that shows posts (threads, user timelines, feed previews, saved searches) behave the same way.

### What a row shows

Every row has the same columns, and NVDA reads them in order:

- **Embed:** what is attached to the post, for example "Image" (or "Image (2)", followed by the alt text if there is any), "Video", "Link", "Quote post", "Quote + image", "Quote + video", "Quote + link", or "Animated GIF". A post hidden behind a content warning shows "Warning".
- **Author:** who wrote the post. For a repost it is the person who reposted it, because that is why the post is in your list. The original author is named at the start of the message.
- **Message:** the text of the post. A reply starts with "Reply to @name:", a repost with "Reposted @name:", and a quote adds "Quote from @name:" and the quoted text after the post's own text. Web addresses are shown in full, even where Bluesky's own apps shorten them. A post hidden behind a content warning shows "(Content warning: ...)" with the category names instead of its text.
- **Posted:** when it was posted, in the format you chose in Settings > Display. Relative times such as "5 minutes ago" update by themselves.

Notifications use the columns Author, Notification, and Received. Chat messages use Reactions, From, Message, Message (more), and Sent.

### Moving around

- **Up and Down arrows** move through the list. A sound plays when you reach the top or bottom.
- **Space** jumps to the next unread item. It always goes to the oldest unread item first, whichever sort order you use.
- **Left and Right arrows** jump to the previous or next post that involves the same person as the post you are on. A post involves a person if they wrote it, reposted it, were replied to in it, or were mentioned in it. This works in Home and Lists. NVSky keeps tracking the same person while you press Left or Right repeatedly, even if you land on a post written by someone else (for example a post that only mentions them).
- **Ctrl+J** asks for a row number and jumps to that row.
- **Alt+1** to **Alt+9** read out the Nth newest item (or oldest, if you sort oldest first) without moving your focus. This is useful for peeking while you are somewhere else in the tab.
- **Ctrl+A** selects every item. This is for actions that work on many posts at once. Right now that means marking as read or unread.

### Loading more and refreshing

- **F5** checks for new items.
- **Shift+F5** loads older items beyond what is already on the screen. If a stretch of older posts has nothing new to show, NVSky tells you to press Shift+F5 again to keep looking further back.
- **Ctrl+Delete** clears this tab's saved copy of its items, after asking you to confirm. This only clears NVSky's local copy and does not delete anything on Bluesky.

### Copying and searching

- **Ctrl+C** copies the focused row. The copy contains the full text of the post (not only what fits on screen) and a link back to the post. In user lists, and in the trees of the Chat and Lists tabs, it copies the text of the focused row.
- NVSky has no search box inside each list, because NVDA can already search a list for you. Press **NVDA+Ctrl+F** to search, then **NVDA+F3** and **NVDA+Shift+F3** for the next and previous match. The Explore tab is different: it searches Bluesky itself.

### Other post list keys

- **Enter** does whatever you chose in Settings > General (by default it opens the thread).
- **Ctrl+Space** reveals the real text of a post that is hidden behind a content warning, once, without changing your settings.
- **Delete** deletes the focused post if it is your own. If it is a repost that you made, Delete undoes your repost.
- **Alt+A** opens the Post action menu and **Alt+U** opens the User action menu. Both are described below.

### Checking unread counts of a tab

Each list tab has a status bar that shows how many items are unread and how many there are in total. To hear it without leaving the list, press **NVDA+End**. This is NVDA's own "report status bar" command. It works the same way in the Chat tab and in every other tab that has a status bar.

### Content warnings

Bluesky can label posts as Pornography, Sexually suggestive, Nudity, or Graphic media. How NVSky treats each label is set in Settings > Display. A post labeled **Warn** shows "Warning" in the Embed column and a content-warning note in place of its text. A post labeled **Hide** doesn't appear in your lists at all.

## The Post action menu (Alt+A)

Press **Alt+A** on a post to open a menu of everything you can do with it. The same menu opens with the Menu key or from the **Post action...** button.

- **Manage post...** appears only on your own posts. It contains Pin or unpin to profile, Edit who can reply, and Delete post.
- **Reply...** opens the compose window with the post as context.
- **Repost** or **Undo repost**.
- **Quote post...**
- **Mark as...** has Read and Unread.
- **Like** or **Unlike**.
- **Save** or **Unsave**. Saved posts appear in the Saved tab.
- **Copy...** copies the post text, or a link to the post.
- **View...** opens the thread, the list of people who liked the post, the people who reposted it, or the posts that quote it. Each opens in its own tab.
- **Embed...** appears when the post has an image, video, animated GIF, or link. You can open an image, copy it to the clipboard, send it to Be My Eyes, open a video, copy a video address, open a link, or copy a link address. For an animated GIF you can open it in your browser or copy its address.
- **More...** has Share to chat, Mute or Unmute thread, Hide post for me, and Report post.

If you select several posts (with Ctrl+A or by selecting rows), the menu only offers marking them as read or unread.

## The User action menu (Alt+U)

Press **Alt+U** on a post to act on the people connected to it. If more than one person is involved (the author, whoever reposted it, the person being replied to, or people mentioned), you first choose who. The menu offers:

- **View...** has Profile, Timeline, Followers, Following, Known followers (people you follow who also follow them), and Lists.
- **Start chat...**
- **Follow** or **Unfollow**.
- **Subscribe to posts** or **Unsubscribe from posts**. This is Bluesky's "bell" feature. You get a notification whenever that person publishes a new post, even if you don't follow them. It covers posts and quote posts, not plain reposts.
- **Mute** or **Unmute**.
- **Block** or **Unblock**.
- **Add to list...**
- **Copy...** copies the profile address, or opens the profile on bsky.app.
- **Report user...**

## Writing a post

Press **Ctrl+N** (or use the quick new post command) to open the compose window. Replying and quoting open the same window with the original post shown above your text.

- Type your post in the text box. A counter shows how much of Bluesky's 300-character limit you have used. Thai and other combined characters are counted the way Bluesky counts them.
- **Attach media...** lets you add up to four images (JPG, PNG, GIF, or WebP), or one video (MP4, MPEG, MOV, or WebM). You can't mix images and a video in the same post. Bluesky only shows the first frame of a GIF, and NVSky tells you so when you attach one.
- For each image, NVSky asks you for alt text (a description for people who can't see the image). It is optional, but strongly recommended. If an image is larger than Bluesky's 1 MB limit, NVSky offers to shrink it for you.
- For a video, NVSky first checks its format and, where it can, its size and length against Bluesky's current limits (at the moment 300 MB and 10 minutes), and asks Bluesky whether you have reached today's upload limit. Bluesky has the final say, and its limits have changed before. Then NVSky shows its progress while the video uploads and Bluesky processes it. You hear rising beeps as it moves through the steps. You can cancel at any time. When it is done, NVSky asks you for alt text for the video too.
- If your text contains a web address, NVSky offers to attach a link preview card. If there is more than one address, press **Choose link...** to pick which one gets the card. No link preview is offered when the post has images or a video, or when it quotes another post.
- Web addresses in your text become real links, and an @handle that belongs to a real account becomes a mention of that account. Text that looks like a handle but isn't one stays plain text.
- **Content labels** lets you label your own post as Pornography, Sexually suggestive, Nudity, or Graphic media.
- **Ctrl+Enter** sends the post. Once it is sent, your post appears in Home at once, and its counts and media links are filled in at the next check.
- **Ctrl+W** or the **Cancel** button closes the window without posting.
- **Escape** does nothing in this window, on purpose, so you can't lose a half-written post by accident.

## The tabs

**Home** is always shown. Seven more tabs can be turned on or off in **Settings > Display**. When NVSky is first installed, Notifications, Explore, Chat, Lists, and People are on, and Saved and Likes are off.

### Home

Your timeline. At the top is a **Feed filter** drop-down with:

- **Following**: posts from the people you follow, and your own.
- **Discover**: Bluesky's recommended posts.
- Any custom feed you added in **Settings > Feed manager** or from Explore.

NVSky remembers the filter you chose. Switching to a feed you have never opened loads it automatically; otherwise it shows what it has saved and waits for you to press F5.

Home supports all the [post list](#how-post-lists-work) keys, including Space, Left and Right, Ctrl+A, and Ctrl+N for a new post. Your own posts and reposts show up in Home too.

### Notifications

Shows likes, reposts, follows, replies, mentions, quotes, and new posts from accounts you subscribed to. It also shows when someone likes or reposts a post that is your own repost, when someone joins through your starter pack, and when your account is verified or loses its verification.

Each row shows who did it, what happened, the text of the related post if there is one, and when.

- **Alt+A** acts on the post the notification is about. For a like or repost, that is your post that was liked or reposted. For a reply, mention, or quote, it is the new post itself. A **follow** notification has no post, so use Alt+U instead.
- **Alt+U** acts on the person who triggered the notification.
- If a post isn't saved on your computer yet, NVSky asks you to press F5 to fetch it.
- Opening the tab or checking for updates also tells Bluesky you have seen your notifications, so the unread badge in the official app clears too.
- If many notifications arrive between two checks, NVSky keeps fetching until it catches up.

Which kinds of activity notify you at all is set in Settings > Display (see [Display](#display-settings)).

### Explore

Searches Bluesky. There are three parts at the top:

- **Search** is the text box. NVSky searches by itself shortly after you stop typing.
- **Result type** is a set of radio buttons: **Posts**, **People**, **Starter packs**, and **Feeds**.
- **Advanced search** (Alt+V) is for Posts only. It opens extra fields: **From handle**, **Since** and **Until** (dates as YYYY-MM-DD), and **Language**. The Alt+V key works when focus is on the panel itself, not while you are typing inside one of its fields.

What you can do with each result type:

- **Posts** use the normal post list keys and the Alt+A and Alt+U menus. **Open in new tab** pins the search as its own tab.
- **People** have the User action menu (Alt+U). **Open in new tab** pins the people search as its own tab.
- **Starter packs** have an **Action** menu with View pack details, Follow everyone in this pack, and Open on bsky.app. Following everyone skips people you already follow.
- **Feeds** have an **Action** menu with View feed (opens a preview tab), Add to my feeds, and Open on bsky.app.

Alt+1 to Alt+9 read the Nth result in any of the four result types.

### Saved

Your bookmarked posts. A post gets here when you choose **Save** in the Post action menu. Saved is a reference list, so it doesn't count unread items. If you **Unsave** a post, it disappears from the list immediately. Saved posts are private, and nobody else can see them.

### Likes

The posts you have liked, newest like first. If you unlike a post here, it disappears from the list immediately. Likes made from other tabs appear here too. Like Saved, it doesn't count unread items.

### Chat

Your direct messages. See [Chat in detail](#chat-in-detail) below.

### Lists

Your lists. See [Lists in detail](#lists-in-detail) below.

### People

One place to see and manage the accounts you have a relationship with. A radio box at the top chooses which list is shown:

- **Following**
- **Followers**
- **Muted users**
- **Blocked users**
- **Activity subscriptions** (people whose new posts you are subscribed to)

Press **Alt+U** on a person to open the User action menu. On the lists you control (Following, Muted, Blocked, and Activity subscriptions), choosing the matching action, such as Unfollow, Unmute, Unblock, or Unsubscribe, removes that person from the list right away.

**Open in new tab** opens the current list in its own tab, which is useful for keeping an eye on a list such as your followers. For someone else's followers or following, use User action > View.

How it refreshes:

- The first time you open each list in a session, NVSky fetches it from Bluesky and plays the progress sound. After that, it shows what it has saved and refreshes quietly in the background.
- Changes made elsewhere (for example following someone from a feed) show up after you press **F5** in this tab.
- Ctrl+F5 does not include the People tab.

## Chat in detail

The Chat tab shows your conversations in a tree on the left, the messages of the selected conversation on the right, and a compose box at the bottom.

### Reading messages

- Selecting a conversation is instant, because it shows what is already saved.
- The conversation name in the tree shows tags such as (group), (muted), (locked), (request), and the number of unread messages.
- A message shows its emoji reactions, who sent it, its text, and when it was sent. A reply shows the start of the message it answers.
- Messages are marked as read when you move onto them. This is also sent to Bluesky, so the unread count stays right on your other devices.
- Very long messages are spread over two columns (**Message** and **Message (more)**). To read the whole message in one piece, open the message menu and choose **Show message...**.
- A shared post appears in the message text as "Shared post from @name: ...".

### Keyboard shortcuts in Chat

- **Ctrl+Enter** sends the message you typed.
- **F5** refreshes the selected conversation.
- **Shift+F5** checks every conversation.
- **Alt+1** to **Alt+9** read the Nth newest message without moving focus.
- **Left arrow** jumps to the message that the current one is replying to.
- **Right arrow** jumps forward to a reply to the current message.
- **Space** jumps to the next unread message.
- **Ctrl+C** copies the focused message.
- **Ctrl+J** jumps to a message by row number.

### Message menu

Press the Menu key or right-click on a message to get: **Reply**, **React...** (pick an emoji; reacting again with your own emoji offers to remove it), **Copy text**, **Show message...**, and **Delete for me** (this removes the message only for you).

### Writing messages

Type in the **Message** box and press Ctrl+Enter or the **Send** button. **Emoji...** opens a list of common emoji to insert. Choose "Custom..." in it to type or paste any other emoji. A counter shows the length of your message against 1000 characters. That limit is an estimate, because Bluesky doesn't publish one. NVSky refuses a longer message and plays the warning sound.

To reply to a message, choose **Reply** in its menu. The compose box then shows "Replying to:" with the start of that message, and **Cancel reply** drops the reply. A message you send appears in the list at once and is confirmed a moment later.

### Conversation menu

Press the Menu key or right-click on a conversation in the tree for:

- **Mark read** and **Mark all read**
- **Mute** or **Unmute**
- **Leave conversation...**
- **Open in new tab...**, which opens the conversation in its own tab so you can keep several open side by side.

For group chats, the menu also has **Manage members...**. If you are the owner of the group, it also has **Lock this group** or **Unlock this group**, **Edit name...**, **Join requests...**, and **Invite link...**.

A group's owner can't leave until the group is locked. NVSky tells you this if you try.

### Group chats

Group chats are marked experimental, see [Known issues](#known-issues). In the conversation tree a group shows tags such as "(group)" and "(locked)". An owner also sees how many join requests are waiting, for example "1 join request". A group without a name is shown with the names of its members.

- **Manage members...** shows the current members as a checklist. Check people and press **Remove checked** to remove them, after you confirm. To add people, type a handle or name in the search box, check the results you want, and press **Add checked**. The change shows at once and is undone if Bluesky refuses it.
- **Edit name...** renames the group. This is for the owner.
- **Lock this group** stops everyone from sending messages. In a locked group the message box is replaced by a note saying so, and **Unlock this group** reverses it. An owner has to lock a group before leaving it.
- **Join requests...** lists the people who asked to join, with **Approve** and **Reject** buttons. This is for the owner.
- **Invite link...** is for the owner. In its dialog you choose who can join (anyone, or only people you follow, each either joining instantly or having to ask) and press **Save**. NVSky then shows the invite code, which is a piece of text and not a web address, with **Copy**, **Edit permissions...**, and **Disable link** or **Enable link**. Bluesky doesn't let NVSky check whether a link already exists, so Save either creates a link or updates the existing one.
- To join a group, press **Join group...** in the new chat window and paste an invite link or code. After a short pause NVSky shows the group's name, its owner, how many members it has, and whether joining needs the owner's approval. Press **Join**. If approval is needed, the request is sent and a **Withdraw request** button appears so you can take it back.

### Message requests

When someone who you don't have a conversation with messages you, it shows up as a **request**. Instead of a compose box you get **Accept** and **Decline** buttons. Declining asks you to confirm and removes the conversation.

### Starting chats

- Press **Ctrl+N** on the Chat tab (or choose **Start chat...** in the User action menu) to start a new conversation.
- In the new chat window you can search for people and add them as recipients. Add more than one person, or type a group name, to start a group chat instead of a one-to-one chat.
- A first message is required, because Bluesky doesn't list empty conversations.
- **Join group...** in that window lets you paste a group invite code or link to join a group.
- **Share to chat...** in the Post action menu sends a post to one of your conversations.

If your account can't use direct messages, NVSky hides the Chat tab and tells you.

## Lists in detail

The Lists tab shows every list you created or subscribed to, in a tree on the left.

There are two kinds of list:

- A **curation list** is a group of accounts whose posts you read together as one timeline. Selecting one shows its timeline on the right, with the same keys and the same Alt+A and Alt+U menus as Home.
- A **moderation list** is a group of accounts for muting or blocking in bulk. It has no timeline, so selecting one shows its members on the right. Alt+U works on each member.

Press the Menu key or right-click on a list for:

- **Remove list**, for lists you created. Pressing **Delete** on a list does the same. Either way NVSky asks you to confirm.
- **Show in new tab**, for curation lists.
- **Manage members...** lets you add or remove members. You can only change lists you created, and for other people's lists the members are read-only.
- **Mute list** or **Unmute list**, and **Block list** or **Unblock list**, for moderation lists.

Other list features:

- **Ctrl+N** (or the **New list...** button) creates a list. You choose a name, an optional description, and whether it is for browsing or for moderation.
- **Find lists by user...** (toolbar) searches for another person and shows their lists. You can open their curation lists as tabs, or subscribe to their moderation lists to mute or block everyone on them.
- **Add to list...** in the User action menu adds a person to one or more of your lists.
- The unread count shown for the Lists tab adds up your curation lists.

## Tabs that open on demand

Many actions open a new tab instead of a window. These tabs can be closed with **Ctrl+W**, renamed with **Ctrl+Shift+F2**, and moved with Ctrl+Shift+Page Up and Down. NVSky remembers them and opens them again next time. When you close one, you go back to the tab you opened it from.

The kinds of tab that can open on demand are:

- **Thread**, from Post action > View > Thread, or from Enter. It shows the whole conversation from the first post down to the replies, with replies marked by ">" signs to show how deep they are. Press F5 to reload the thread. Its Post action menu is shorter: Like, Copy, and Embed.
- **Quotes**, from Post action > View > Quotes. It lists the posts that quote a post. If the quoted post is yours, you can also choose **Detach my post from this quote...**, which removes that quote from your post's quote list without touching the other person's post.
- **Likes** and **Reposts** of a post, from Post action > View. These are lists of people.
- **Timeline**, **Followers**, **Following**, and **Known followers** of a person, from User action > View. A person's timeline shows their pinned post first, marked "Pinned:".
- **Feed preview** and **Search** tabs, from Explore. A feed preview has an **Add to my feeds** button.
- **A list**, from Lists > Show in new tab.
- **A conversation**, from Chat > Open in new tab.
- **People search**, from Explore, with the People result type.

## Using more than one account

You can log in with several Bluesky accounts in Settings > Accounts and switch between them whenever you like. One account is active at a time, and the tabs, the notification area icon, and the commands all work on it.

Each account is kept apart: it has its own saved data in its own encrypted database file, and nearly all of NVSky's settings belong to it too (General, Display, Feed manager, Sound, and so on). Your keyboard shortcuts are shared by all accounts.

To switch the active account you can:

- Choose **Set as active** in Settings > Accounts.
- Choose the account in the menu of the notification area icon. This menu lists accounts only when you have more than one.
- Use the **Switch to the next account** and **Switch to the previous account** actions. They have no shortcut until you assign one in [Settings > Keymap](#keymap), and they work while the main window has focus. With only one account, NVSky tells you so.

After a switch NVSky says which account is now active. The tabs of an open main window are rebuilt for the new account, auto read stops, and real-time updates restart for the new account.

Background checks cover every account that has **Check this account in the background** turned on, one account at a time. When something new is found for an account that is not active, the announcement starts with that account's handle, for example "@name: New post in: Home". Real-time updates, the notification area icon's counts, and the command that reads your unread counts only cover the active account.

## Real-time updates (Jetstream)

Normally NVSky checks for new content on a schedule (see Settings > General). You can also turn on **Enable real-time Home feed updates (Jetstream)** in **Settings > General**. NVSky then keeps a live connection open to Bluesky's public Jetstream stream, and new content appears within moments of being posted, with the same sound and announcement as a background check.

What it covers:

- **Home (Following feed):** new posts and reposts from people you follow, and your own.
- **Lists:** new posts from the members of your curation lists, including any list open in its own tab. Reposts are not included, the same as in Bluesky's own list timelines.

The scheduled checks for Home and Lists keep running as a backup. If you set their intervals to 0 to rely on real-time updates alone, and Jetstream can't start (see below), Home and Lists only update when you press F5. Jetstream starts by itself when NVDA starts, reconnects by itself after a network drop or after your computer sleeps, and catches up on what you missed. A burst of catch-up content is announced once, not once per post.

Things you should know:

- The people you follow and the members of your lists are read **once, when Jetstream starts**. If you follow or unfollow someone, or change list members, turn Jetstream off and on again in Settings (or restart NVDA) so it notices.
- It does **not** cover Notifications, Chat, Discover, custom feeds, or saved searches. Those keep using scheduled checks and F5.
- It keeps a connection open and handles a steady stream of events, so it uses a little extra network and processor time for as long as NVSky is running.
- Bluesky's real-time service refuses requests that name a very long list of accounts. In our tests it accepted 300 accounts and refused 1,000. When the accounts you follow plus the members of your lists add up to roughly 250 or more, real-time updates do not start. NVSky tells you so and keeps using the scheduled checks. Bluesky's older real-time service has another way to take a long list, but in our test it ignored the list and sent everything, so NVSky does not use it that way.
- Real-time updates have only been tried with real accounts that follow a few dozen people. Longer lists were tested with made-up account IDs only. If real-time updates never start or stop working, turn off Jetstream in Settings > General.

## Settings

Open Settings from the main window (Ctrl+P), or from the NVDA menu: Preferences, then **NVSky Settings...**

Settings has its own tabs, which you can switch between with Ctrl+1 to Ctrl+9 or Ctrl+Tab. Nothing is saved until you press **OK** or **Apply**. **Cancel** or **Escape** throws away your unsaved changes. If saving something to Bluesky fails, NVSky tells you what could not be saved.

### Accounts

Lists your logged-in accounts, and marks the active one. You can:

- **Add account...** to log in with another Bluesky account.
- **Set as active** to switch to the selected account.
- **Remove account** to remove the selected account. This also deletes its saved data and its stored App Password from your computer, and can't be undone.
- **Check this account in the background** decides whether the scheduled background checks include the selected account. It is on by default. An account with checks turned off is marked in the list.

These actions take effect immediately and don't wait for OK. Before you add or switch to another account, NVSky first saves the Settings changes you made for the account you are leaving. If that save fails, NVSky tells you and stays on the current account. If you remove an account, unsaved changes that belonged to it are thrown away, and NVSky tells you so. Switching accounts rebuilds the tabs in an open main window. See [Using more than one account](#using-more-than-one-account).

### General

- **Enter key action on a post:** what pressing Enter does on a post. Choices are View thread (the default), Reply, Quote post, Repost / Undo repost, Like / Unlike, and Toggle read/unread.
- **Auto read delay between rows (seconds, 1 to 10):** how long auto read waits after finishing one row before it reads the next. See [Auto read](#auto-read).
- **Auto read voice profile:** one of your NVDA configuration profiles, or "(None, keep the current voice)". Auto read speaks each row with that profile's voice settings. See [Auto read](#auto-read).
- **Speak when background sync finds new content in:** a checklist of categories. For each one you check, NVSky speaks a short announcement when a background check finds something new. All are checked by default. A sound still plays if the sound is enabled for it.
- **Background sync intervals:** how often, in minutes, each category is checked while you aren't looking at it. Setting it to 0 turns that category off. The categories are Home, Notifications, Saved / Likes, Chat, Lists, Search / feed previews, Profile / people & post lists, and Thread. Saved / Likes is off by default.
- **Enable real-time Home feed updates (Jetstream):** see [Real-time updates](#real-time-updates-jetstream). The Home and Lists scheduled checks keep running as a backup.
- **Clear all cache:** deletes every saved post, notification, chat message, and list for the active account. You stay logged in. Every tab is empty until the next check. This asks you to confirm.

### Display

- **Feed order:** Newest first or Oldest first. This applies to every post list and to chat messages.
- **Show author as:** Display name or Handle.
- **Post time format:** Relative (up to 24 hours, then the full date and time), Relative always (minutes, hours, days, months, or years ago), Full date and time, or Custom format. With a custom format you type a Python strftime pattern. The **strftime format reference...** button opens its documentation in your browser.
- **Show these tabs:** a checklist of the optional tabs (Notifications, Explore, Saved, Likes, Chat, Lists, and People). Home is always shown. Changing it rebuilds the tabs when you close Settings.
- **Notification area icon:** see [Notification area icon](#notification-area-icon).
- **Content label categories:** the first row is a master switch for **Adult content**. Below it are Pornography, Sexually suggestive, Nudity, and Graphic media. Press **Space** on a row to cycle its setting. For the adult switch it cycles Enabled and Disabled. For the others it cycles **Show**, **Warn**, and **Hide**. While adult content is off, every category except Nudity is hidden. These are saved to your Bluesky account, so they match the official app.
- **Notify me about:** which kinds of activity notify you. Press **Space** on a row to cycle Off, Everyone, and People you follow (or just Off and On for kinds with no filter). These are saved to your Bluesky account, so they also change what the official app notifies you about.

The content label and notification lists load from Bluesky the first time you open the Display tab, and you hear the progress sound while they load.

### Feed manager

Manages the custom feeds you have added. Each row shows the feed name, its creator, and whether it is pinned. The buttons are **Move up**, **Move down**, **Pin selected** (or **Unpin selected**), and **Remove selected**. Changes are sent to Bluesky when you press OK or Apply, and your Home feed filter updates to match. Add new feeds from Explore, using the Feeds result type.

### Sound

- **Sound pack:** choose a pack, or **Silent / No sound** to turn all sounds off.
- **Play a sound for:** a checklist of events (like, new message, error, tab opened, and many more). Uncheck an event to silence it, whichever pack you use. New events added in later versions are on by default.

See [Sound packs](#sound-packs) for how to add your own packs.

### Keymap

Lists the keyboard shortcuts of NVSky's own windows, so you can see every key and change them. Each row shows the action, its shortcut, and where it works: the main window, tabs with lists, Chat, the new post window, the Settings window, or one of three groups of actions that have no shortcut until you give them one (Post actions, User actions, and Chat message actions). A shortcut is shown as "(fixed)" if it is built in and can't be changed, "(changed)" if it differs from its default, and "Not assigned" if the action has none.

- **Change...** (or Enter on a row) asks you to press the new shortcut. Press Escape to cancel.
- **Remove shortcut** leaves the action without a shortcut.
- **Reset** puts the selected action back to its default. **Reset all** does the same for every action, after asking you to confirm.

NVSky does not accept Tab, Shift+Tab, Up, or Down, which are for moving around. If another action that works in the same place already uses the key, NVSky tells you which one and does not accept it. It also warns you, but still accepts the key, when a letter, digit, or symbol on its own would stop NVDA's type-to-search in lists, or when Alt with a letter may clash with a button's access key.

Giving a shortcut to a Post action, User action, or Chat message action runs it straight on the focused post, user, or message, without opening the menu. A few of them only work on your own posts. The two account switching actions also have no default; see [Using more than one account](#using-more-than-one-account). Auto read is not in this list; assign its shortcut in NVDA's Input gestures dialog.

Changes are saved when you press OK or Apply. The shortcuts shown on buttons update when you close Settings.

### Profile

Edit your Bluesky profile: **Display name**, **Bio**, and the buttons **Change avatar...** and **Change banner...**. The profile loads the first time you open this tab. A new avatar or banner is only uploaded when you press OK or Apply.

### Muted words

Add or remove muted words and tags. Press **Add** and type a word, or select one and press **Remove selected**. Changes are sent to Bluesky when you press OK or Apply. Muted users, blocked users, and activity subscriptions are managed in the **People** tab.

## Notification area icon

NVSky can show an icon in the Windows notification area (the system tray). Its tooltip is your unread count. Press Enter or click on the icon to open NVSky. Its menu has **Open NVSky** and **NVSky Settings...**. If you have more than one account, the menu also lists them, each with its unread count when it has any, and choosing one makes it the active account.

### Turning it on and choosing what it shows

In **Settings > Display**, check **Show an icon in the notification area with the unread count**. Two more settings then appear:

- **Count unread items from these tabs in the icon:** choose any of Home, Notifications, and Chat. The Chat count adds up every conversation. The Lists tab is not counted.
- **Icon text:** choose how the count is worded:
  - **Total unread count** gives one total, for example "NVSky: 5 unread".
  - **Unread count of each tab, with tab names** gives each tab, for example "NVSky: Home 3, Noti 2, Chat 1". Tabs with nothing unread are left out, and a count above 99 is shown as 99+. If the text would be too long for Windows, NVSky shows the total instead.

When nothing is unread, the text is "NVSky: no unread".

### If you can't find the icon

Windows may be hiding it. Press **Open Windows taskbar settings...** (next to the checkbox), then:

1. Press Tab until you reach the item for other system tray icons, and open it with Enter or Space.
2. Press Tab to the list of applications and find the entry for NVDA.
3. Press Space on it to switch it to On.

The wording and layout of these Windows settings differ between Windows versions.

### Things to know

The icon is removed and added again every time its count changes. This is so your screen reader reads its name and count as a single message. If you happen to be on the icon at that moment, NVSky puts your focus back on it, but you may briefly hear another icon's name.

When several programs have notification area icons, the one updated most recently is always placed first. You can move icons with Alt+Shift+Left and Alt+Shift+Right, but the result can take some trial and error when there are many icons.

## Reading unread counts

Besides the notification area icon, you can ask NVSky for your unread counts at any time with a keyboard command.

1. Open the NVDA menu, then Preferences, then **Input gestures...**
2. Find the **NVSky** category.
3. Choose **Read the unread count of every open NVSky tab** and assign a shortcut.

The command speaks the unread count of every tab you have open that has one, leaving out tabs with nothing unread, for example "NVSky unread: Home 3, Notifications 2, Chat 1". If nothing is unread, it says "NVSky: no unread". If the main window is closed, it reads Home, Notifications, and Chat only (and only the ones you have turned on).

## Auto read

Auto read reads a list tab to you, one row after another, without you pressing anything between rows. It does what pressing Space does (jump to the next unread item, oldest unread first, whatever your sort order), and then does it again and again.

To use it, assign a shortcut to **Start or stop auto read** in NVDA's Input gestures dialog, in the NVSky category. It has no default shortcut. Press it once to start and again to stop.

- Each row is read in full, whatever fits on screen. When the speech has finished, auto read waits for the delay you set in Settings > General (1 to 10 seconds) and moves on. Each row is marked as read as it is reached.
- With the main window open, it reads the current tab and moves the focus to each row as it goes. With the main window closed, it reads the tab you last used from NVSky's saved data, and the tab opens on the row you stopped at next time. This works for Home, Notifications, Saved, Likes, and list, user timeline, and search or feed preview tabs. If your last tab was another kind, NVSky tells you it can't read it.
- It is not available on the Chat and People tabs, on Explore results other than Posts, on moderation lists, or on thread and quotes tabs. NVSky says so if you try.
- Saved, Likes, and Explore posts have no unread tracking. There auto read reads the rows in order, starting after the row you are on.
- The sounds for posts with an image, video, link, quote, or content warning play as each row is reached. Posts with a content warning are read as the warning, not their text.
- If you choose a voice profile in Settings > General, each row is read with that profile's voice settings, and NVDA returns to your normal voice afterwards. If the profile was deleted, or is already active, auto read just uses the current voice.
- Spoken announcements of background checks are held back while auto read is running.

Auto read stops by itself when there is nothing more to read. It also stops as soon as you press something that makes NVDA speak, or silence speech, so you can take over at any moment. If your speech synthesizer never reports that it finished a row, auto read stops after a while and tells you.

## Sound packs

Sound packs live in NVSky's own `SoundPack` folder, with one subfolder for each pack. To make your own pack:

1. Create a new subfolder inside the `SoundPack` folder.
2. Put `.wav` files in it, named after the events, for example `like.wav`, `error.wav`, or `new_message.wav`.
3. Open Settings > Sound and choose your pack.

The file names, all ending in `.wav`, are:

- Post and chat actions: `like`, `unlike`, `repost`, `unrepost`, `save`, `unsave`, `send_post`, `delete`, `follow`, `unfollow`, `block_mute` (for mute, block, subscribe, and report), and `send_message`.
- Things that arrive: `new_message` and `notification`.
- The window and its tabs: `main_open`, `main_close`, `open_tab`, and `close_tab`.
- Feedback: `boundary` (the end of a list), `error`, `ready` (a check or load finished), and `max_length` (text over the length limit).
- A row gets the focus: `embed_image`, `embed_video`, `embed_gif`, `embed_link`, `embed_quote`, and `content_warning`.
- Auto read: `autoread_start` and `autoread_stop`.
- `progress`, the looped sound described below.

A pack doesn't need every file. Events without a file are silent. A file named `progress.wav` is played in a loop while NVSky is waiting for something; without it, NVSky plays a soft repeating beep instead. Choose "Silent / No sound" in Settings > Sound to turn everything off.

## Privacy and security

NVSky takes care to keep your Bluesky data away from other people who can use your computer.

- Your **App Password** is encrypted with Windows' DPAPI before it is saved.
- Your **whole local database** is encrypted using [SQLCipher](https://www.zetetic.net/sqlcipher/). That includes every saved post, notification, chat message, and list. Each account you have logged in has its own database file, and all of them are protected the same way. This is not limited to sensitive fields. The key that protects the database is itself protected with DPAPI.

DPAPI ties this protection to **one Windows user account on one computer**. That is a deliberate choice for security, but it has consequences:

- **NVSky won't work with a portable copy of NVDA** that was copied to another folder or a USB drive. The stored App Password and database key can't be read there, so you have to log in again.
- **It doesn't move to another computer**, or to another Windows user on the same computer, for the same reason.
- If the key or the database ever becomes unreadable (a damaged profile, or one of the situations above), NVSky notices, renames the unreadable files with a `.bak` ending instead of crashing, and starts fresh. You will need to log in again. Nothing about your Bluesky account is affected, only NVSky's local copy.

If you use NVSky on more than one computer, log in separately on each. This is expected, not a bug.

## Known issues

- The conversation list doesn't go past the first 50 conversations, so accounts with more than 50 won't see the rest in the Chat tab.
- Temporary files from opening images and videos are only cleaned up when NVDA starts, so they can stay in your Temp folder for up to about 24 hours.
- Using more than one account has only been tried with one real account. Switching between two accounts, and background checks of several accounts, have not been tested with real accounts. Switching the active account while the Settings dialog is open, and very large amounts of notifications or chat, have not been fully tested.
- Several Bluesky features used for group chats and invite links are marked inside NVSky as experimental, because not every case has been tried against a real server. If you get an error in one of these areas, please report it with as much detail as you can.
- These have not been tested with a real account and may misbehave: uploading a new avatar or banner in Settings > Profile, blocking or unblocking a whole moderation list, the "isn't accepting messages from you" check when starting a chat, hiding the Chat tab for accounts without direct messages, and **Mark all read** on accounts with a very large number of conversations.
- If Bluesky limits how fast your account can make requests, NVSky tells you roughly how long to wait instead of showing a raw error. Background checks resume by themselves afterwards.
- Occasionally, while you read through a feed, the status bar may show the wrong text instead of the unread count. Switching to another tab and back fixes it.
- NVSky has only been tested with a small number of accounts. Setting up a brand new account, and accounts with a very large number of followers, lists, or conversations, have not been verified.
- Real-time updates (Jetstream) are not available when you follow, and list, roughly 250 accounts or more. See [Real-time updates](#real-time-updates-jetstream).
- Because the notification area icon is removed and added again when its count changes, NVDA's focus may jump to another icon if you are on the NVSky icon at that moment. NVSky tries to bring focus back, but you may need to move back yourself. See [Notification area icon](#notification-area-icon).

## Additional information

NVSky is built on the [atproto](https://github.com/MarshalX/atproto) Python SDK, which talks to Bluesky's AT Protocol servers. We are grateful to its maintainers and contributors.

The overall design (a multi-tab, multi-account desktop client with locally saved feeds) was inspired by **Qwitter**, a Twitter client that many screen reader users remember fondly. Several features were adapted from ideas in **OpenTween**, most notably jumping to the next or previous post involving the same user with the Left and Right arrows.

If you want to compare NVSky with another accessible way to use Bluesky, **[FastSMRW](https://github.com/masonasons/FastSMRW)** is a separate accessible Bluesky client by a different author. It is a standalone application, not an NVDA add-on. Try both and use the one that suits how you work.

NVSky is under active development, so features and shortcuts described here may change between releases. If you run into a problem, please report it with as much detail as you can.
