## File: plan-01.md
`md
# NVSky — แผนพัฒนา

_เอกสารนี้รวบรวมทุกไอเดีย/ข้อเสนอที่คุยกันมาตลอดโปรเจกต์ แก้ไข/ตัดทอนได้ตามต้องการ_

## ภาพรวมโปรเจกต์

NVDA add-on สำหรับใช้งาน Bluesky (AT Protocol) โดย Virus พัฒนาคนเดียว อยู่ในสถานะ closed development (ยังไม่ปล่อยสู่สาธารณะ) ใช้ SDK `atproto` (Python) โครงสร้างคล้าย YoutubePlus/MessengerAccess ที่เคยทำมาก่อน

รองรับ NVDA 2026.1 ขึ้นไปเท่านั้น (64-bit / Python 3.13) ไม่รองรับ 32-bit

## โครงสร้างไฟล์ปัจจุบัน

```
NVSky/
├── manifest.ini
├── buildVars.py
└── globalPlugins/
    └── NVSky/
        ├── __init__.py       # entry point, gesture, prePopup/postPopup, i18n init
        ├── db.py             # SQLite (sqlcipher3 เข้ารหัส) layer
        ├── client.py         # atproto wrapper (lazy import, ทุก action)
        ├── crypto.py         # DPAPI encrypt/decrypt (Windows)
        ├── settings.py       # หน้าตั้งค่า (Accounts/General/Display/Sound/Profile)
        ├── feedWindow.py     # หน้าต่างหลัก (FeedWindow, ProfileDialog, UserListDialog, UserActionMixin)
        ├── compose.py        # ComposeDialog (โพสต์ใหม่)
        └── lib/              # vendored libs (atproto, sqlcipher3, pydantic ฯลฯ)
```

## สถาปัตยกรรม/การตัดสินใจสำคัญที่ทำไปแล้ว

- **Lib**: ใช้ `pip install --target lib/ --python-version 3.13 --implementation cp --abi cp313 --platform win_amd64 --only-binary=:all:` เสมอ เพื่อให้ตรงกับ Python ของ NVDA 2026.1
- **Lazy import**: `atproto`/`pydantic` ฯลฯ import เฉพาะตอนใช้จริง (ในฟังก์ชัน ไม่ใช่บนสุดไฟล์) ลด NVDA startup time
- **Ghost dependency**: `sys.modules` เป็น cache ระดับ process เดียวกันทั้ง NVDA ไม่แยกตาม add-on — ถ้า add-on อื่นเคย import ชื่อ module เดียวกันมาก่อน (เช่น `sqlite3`) จะได้ตัวที่ cache ไว้แทน ไม่ใช่ของตัวเอง เป็นเหตุผลหลักที่เปลี่ยนไปใช้ `sqlcipher3` (ชื่อไม่ชนใคร + bundle ทุกอย่างในตัวเอง ไม่มี DLL แยกให้ชนชื่ออีก)
- **atproto SDK bug**: `send_image()`/`send_images()`/typed Record models ชนบั๊ก pydantic ที่รู้จักแล้ว (MarshalX/atproto#354, MockValSer) แก้โดยใช้ low-level `repo.create_record`/`delete_record` ส่ง dict ดิบแทนแทบทุก action (post, like, follow, mute, block, report)
- **Record parsing**: `post.record` บางทีเป็น dict, บางทีเป็น DotDict (มี `.get` แต่ไม่ callable เสมอ — ต้องเช็ค `callable()` ไม่ใช่แค่ `hasattr()`), บางทีเป็น typed Record model — ดึง embed/quote จาก `post.embed` (hydrated View) แทน `record.embed` เสมอ ปลอดภัยกว่า
- **DB**: เข้ารหัสด้วย `sqlcipher3`, master key เก็บแยกไฟล์ `db.key` เข้ารหัสด้วย DPAPI (ตัวเดียวกับ App Password) — ข้อจำกัด: ผูกกับเครื่อง+user Windows เดียวกันเท่านั้น ไม่รองรับ NVDA portable ย้ายเครื่องบ่อยๆ (ยังไม่ตัดสินใจว่าจะทำทางเลือก passphrase หรือไม่)
- **UI pattern**: ทุก dialog ที่ popup ตรงจาก gesture ต้องมี `gui.mainFrame.prePopup()`/`postPopup()` คู่กัน, dialog ที่เปิดจากปุ่มในหน้าต่างที่ popup ไว้แล้วไม่ต้อง
- **ComposeDialog ต้องใช้ `Show()` ไม่ใช่ `ShowModal()`** เวลาเรียกตรงจาก NVDA gesture (ไม่งั้น NVDA crash — บั๊กเดียวกับที่เคยเจอใน YoutubePlus)
- **Focus/speech**: ปิด dialog/menu แล้ว focus เด้งกลับทำให้ NVDA พูดทับข้อความที่เพิ่งสั่งไว้ — แก้ด้วย `ui.message(text, speechPriority=speech.priorities.Spri.NOW)` (ไม่ใช้วิธีหน่วงเวลาแบบเดาแล้ว)
- **User action ระบบเดียว**: `UserActionMixin` ใช้ร่วมกันทั้ง FeedWindow (Alt+U), UserListDialog, ProfileDialog — View profile/Copy profile URL/Follow/Mute/Block ชุดเดียว โค้ดเดียว

## ฟีเจอร์ที่เสร็จแล้ว

### บัญชี/ความปลอดภัย
- Login ด้วย App Password (เก็บเข้ารหัส DPAPI), validate pattern แบบ soft-warning
- รองรับหลายบัญชี, Set active account
- Remove account (มี confirm dialog), ลบ cache/focus state ของบัญชีนั้นด้วย
- Clear cache (ปุ่มแยกใน General tab)
- DB เข้ารหัสทั้งไฟล์ด้วย sqlcipher3

### หน้า Feed (Home)
- ListCtrl 4 คอลัมน์: Author, Message, Posted, Embed
- Lazy load: โหลดทุกอย่างที่ cache ไว้ตอนเปิด, ดึงจาก network เฉพาะตอน cache หมด (page ละ 50, walk อัตโนมัติสูงสุด 5 หน้า)
- จำตำแหน่ง focus ล่าสุดต่อบัญชี, จำ scroll ไม่ reset ตอน re-render
- Mark read/unread อัตโนมัติตอน focus, unread count ใน status bar, Space = ไปโพสต์แรกสุดที่ยังไม่อ่าน
- Reply แสดง "Reply to @handle: ..."
- Quote post แสดง "... Quote from @handle: <ข้อความต้นฉบับ>"
- Repost: Author column แสดงคนที่ repost (ตาม setting name/handle), message แสดง "Reposted @เจ้าของเดิม: ..."
- Check for updates (F5) แจ้งจำนวนโพสต์ใหม่แยกจากกรณีไม่มีอะไรใหม่
- Filter Following/Discover/For You (Discover/For You ยังเป็น stub)
- Status bar: ชื่อ tab + unread + total

### Post action menu (Alt+A + ปุ่ม)
- Like/Unlike (ใช้งานได้จริง)
- Copy... submenu (Copy post text, Copy link to post)
- Delete post... (เฉพาะโพสต์ตัวเอง, มี confirm)
- More... submenu: Mute thread, Hide post for me (local only), Report post... (มี reason picker)
- Reply / Repost / Quote post / Edit who can reply / Add to saved posts / Open share menu / View embed — **ยังเป็น stub ทั้งหมด**

### User action menu (Alt+U + ปุ่ม, ระบบเดียวกันทุกที่)
- ตรวจจับ post author, คน repost, คนที่ถูก reply, คนที่ถูก mention (จาก facets) — ถ้ามีคนเดียวไม่ต้องเข้า submenu
- View profile (ดึงจาก SDK จริง, แสดงใน dialog อ่านได้ ไม่เปิดเว็บ)
- Copy profile URL, Follow, Mute, Block (ทิศทางเดียว ยังไม่มี unfollow/unmute/unblock เพราะต้องมี relationship lookup เพิ่ม)

### Profile dialog
- แสดง display name/handle/DID/bio
- ปุ่ม Followers/Following (เปิด UserListDialog แบบ ListCtrl กด Enter ดู profile ซ้อนได้ไม่จำกัดชั้น)
- ปุ่ม Actions... (Follow/Mute/Block คนที่กำลังดูอยู่)
- ปุ่ม Open profile on bsky.app (สำรอง)

### Compose
- Multiline text box, progress gauge นับตัวอักษร (300 char), title แสดง count + จำนวนรูปแนบ
- แนบรูปได้สูงสุด 4 รูป (≤1MB, JPEG/PNG/GIF/WebP), ถาม alt text ต่อรูป
- Ctrl+Enter โพสต์ได้จากทุกจุดใน dialog, Escape ปิดไม่ได้ (กันโพสต์หายไม่ตั้งใจ), Ctrl+W/Cancel ปิดได้
- Quick new post แยก (script ไม่ผูก default gesture, ให้ผู้ใช้ตั้งเอง)

### Settings
- Tab เรียง: Accounts → General → Display → Sound → Profile
- Accounts: add/remove/set active
- General: fetch interval (นาที, ยังไม่ต่อ scheduler จริง), Clear cache
- Display: author display mode, time format (4 โหมด + custom strftime), sort order (ยังไม่ต่อ query จริง)
- Sound, Profile: placeholder รอทำ

### อื่นๆ
- i18n: ใส่โครง `addonHandler.initTranslation()` แล้วใน `__init__.py` เท่านั้น (ไฟล์อื่นยังเป็น hardcoded string ภาษาอังกฤษ)

## กำลังทำอยู่

- **View embed / เปิดไฟล์แนบ**: เปิดรูปด้วยโปรแกรมเริ่มต้นของ Windows, ส่งรูปเข้า Be My Eyes (ดาวน์โหลดมาไว้ temp ก่อนแล้ว ShellExecute ด้วย AUMID), เปิดวิดีโอ/ลิงก์ในเบราว์เซอร์ — ต้องเพิ่ม URL ของรูป/วิดีโอ/ลิงก์เข้าไปใน `embed_json` ที่เก็บไว้ก่อน (ตอนนี้เก็บแค่ alt text ของรูป ยังไม่เก็บ URL จริง)

## ฟีเจอร์ที่วางแผนไว้ (ยังไม่เริ่ม)

### Post action ที่ยังเป็น stub
- Reply / Repost / Quote post — ต้องมี compose UI แบบมี context (ตอบ/quote โพสต์ไหน)
- Edit who can reply — ไม่แน่ใจว่า SDK รองรับไหม (threadgate record) ต้องตรวจสอบ
- Add to saved posts (bookmark) — endpoint ของ AT Protocol ยังไม่ชัดเจน ต้องหาข้อมูลเพิ่ม

### ระบบเสียง (Sound)
- Settings > Sound: เลือก theme (subfolder ใต้ `globalPlugins/NVSky/sounds/<theme>/`), "Silent" เป็นตัวเลือกแรกแต่ไม่ใช่ default, checklist เลือกเล่นเสียง event ไหนบ้าง (defaultติ๊กหมด, ซ่อนถ้าเลือก Silent)
- เล่นเสียงผ่าน `nvwave` (ของ NVDA เอง ไม่ต้อง vendor lib เพิ่ม)
- Event ที่ต้องมีเสียง: post success, reply success, dm success, new notification, new dm, new post, new search result, เจอโพสต์ที่มี link/attachment ระหว่างเลื่อนอ่าน

### ระบบหลายแท็บ (Multi-tab)
วางแผนคร่าวๆ ตามหน้าเว็บ bsky.app:
- **Home** — มีแล้ว
- **Explore** — search + trending topics/hashtag
- **Notifications** — ต้องมี
- **Chat** — direct message
- **Feeds** — browse/pin custom feed generator (algorithmic feed อื่นนอกจาก Following)
- **Lists** — จัดกลุ่ม user, เพิ่ม user เข้า list ได้, ดู timeline ของทั้ง list
- **Saved** — คล้าย bookmark
- **Profile** — ย้ายไปอยู่ใน Settings แทนแล้ว
- **Settings** — อยากให้เข้าถึงจากตรงนี้ได้ด้วย ไม่ใช่แค่ผ่าน NVDA settings dialog

รายละเอียดที่ต้องคิดเพิ่ม:
- Dynamic tab: ค้นหา keyword/hashtag แล้ว "Pin as tab" สร้างแท็บถาวรได้ (ต้องมีตาราง `saved_searches` เก็บไว้ข้ามเซสชัน)
- Reorder tab (move up/down) — เอาไว้ท้ายๆ หลังมีหลายแท็บจริง
- View user timeline — โครงสร้างเหมือน Home แต่กรองเฉพาะ user เดียว ต้อง add ไปแสดงถาวรในหน้าหลักได้ (คล้าย pinned tab)
- จำตำแหน่ง tab ล่าสุดที่เปิดไว้ ข้าม session

### Background fetch อัตโนมัติ
- อ่านค่า interval จาก General settings (มีอยู่แล้ว) มา schedule จริงด้วย `wx.Timer`
- แยก interval ต่อประเภท feed (main timeline, DM ฯลฯ)
- ต้องระวังเรื่อง thread safety ตอนรันพร้อม user ใช้งานอยู่

### Enter = quick action (แบบ OpenTween/YoutubePlus)
- ตั้งค่าอยู่ใน General settings
- รอ action list เต็มก่อน (ตอนนี้ครบขึ้นเยอะแล้วหลัง Post/User action menu เสร็จ) น่าจะเริ่มออกแบบได้แล้ว

### Mute words & tags
- เก็บผ่าน `app.bsky.actor.putPreferences` (มี `mutedWordsPref`) ซับซ้อนกว่าจุดอื่น ต้องมี settings UI แยกสำหรับจัดการ list คำที่ mute

### Profile editing
- แก้ไข display name/bio/avatar ผ่าน `app.bsky.actor.profile` record — ทำได้แน่นอนตาม SDK แต่รอ feed เสถียรก่อนตามที่วางแผนไว้

### I18N เต็มรูปแบบ
- ตอนนี้มีแค่ `__init__.py` ที่ wrap `_()` ไว้ ไฟล์อื่น (`feedWindow.py`, `compose.py`, `settings.py`, `client.py`) ยัง hardcode ภาษาอังกฤษทั้งหมด
- ต้องแปลง string ที่มีตัวแปร (f-string) เป็น `_("...").format(...)`, ใช้ `ngettext()` สำหรับพหูพจน์ (minute/minutes ฯลฯ)
- ตั้งใจไว้ว่าเป็นงานก้อนใหญ่ที่ควรทำรวดเดียวจบ ไม่ปนกับ fix อื่น

### Unfollow/Unmute/Unblock
- ตอนนี้ Follow/Mute/Block เป็นทิศทางเดียว ไม่รู้ relationship ปัจจุบัน ต้องดึงจาก `getProfile`'s `viewer` state (`viewer.following`, `viewer.muted` ฯลฯ) มาใช้ตัดสินว่าจะโชว์ปุ่มไหน (Follow vs Unfollow เป็นต้น)

``n
## File: plan-02.md
`md
# NVSky — แผนพัฒนา (อัปเดต)

_เอกสารนี้รวบรวมทุกไอเดีย/ข้อเสนอที่คุยกันมาตลอดโปรเจกต์ แก้ไข/ตัดทอนได้ตามต้องการ อัปเดตจากเวอร์ชันแรกหลังจบรอบ View embed + Phase 2 (Reply/Repost/Quote, Unfollow/Unmute/Unblock) และฟีเจอร์เสริมอีกหลายตัวที่เพิ่มระหว่างทาง_

## ภาพรวมโปรเจกต์

NVDA add-on สำหรับใช้งาน Bluesky (AT Protocol) โดย Virus พัฒนาคนเดียว อยู่ในสถานะ closed development (ยังไม่ปล่อยสู่สาธารณะ) ใช้ SDK `atproto` (Python) โครงสร้างคล้าย YoutubePlus/MessengerAccess ที่เคยทำมาก่อน

รองรับ NVDA 2026.1 ขึ้นไปเท่านั้น (64-bit / Python 3.13) ไม่รองรับ 32-bit

## โครงสร้างไฟล์ปัจจุบัน

```
NVSky/
├── manifest.ini
├── buildVars.py
└── globalPlugins/
    └── NVSky/
        ├── __init__.py       # entry point, gesture, prePopup/postPopup, i18n init
        ├── db.py             # SQLite (sqlcipher3 เข้ารหัส) layer
        ├── client.py         # atproto wrapper (lazy import, ทุก action)
        ├── crypto.py         # DPAPI encrypt/decrypt (Windows) — เขียนใหม่ทั้งไฟล์ (ดูด้านล่าง)
        ├── attachments.py    # ใหม่ — ดาวน์โหลด/เปิด/ส่ง Be My Eyes/copy สำหรับ embed
        ├── settings.py       # หน้าตั้งค่า (Accounts/General/Display/Sound/Profile/Muted words)
        ├── feedWindow.py     # FeedWindow, ProfileDialog, UserListDialog, UserTimelineDialog,
        │                     # ThreadDialog, UserActionMixin, EmbedViewMixin
        ├── compose.py        # ComposeDialog (โพสต์ใหม่/reply/quote)
        └── lib/              # vendored libs (atproto, sqlcipher3, pydantic, Pillow ฯลฯ)
```

## สถาปัตยกรรม/การตัดสินใจสำคัญ

_(รายการเดิมทั้งหมดยังใช้ได้อยู่ — เพิ่มรายการใหม่ต่อท้าย)_

- **Lib**: ใช้ `pip install --target lib/ --python-version 3.13 --implementation cp --abi cp313 --platform win_amd64 --only-binary=:all:` เสมอ
- **Lazy import**: `atproto`/`pydantic` ฯลฯ import เฉพาะตอนใช้จริง ลด NVDA startup time
- **Ghost dependency**: ใช้ `sqlcipher3` เพราะ `sys.modules` เป็น cache ระดับ process ไม่แยกตาม add-on
- **atproto SDK bug**: หลีกเลี่ยง typed Record models (MarshalX/atproto#354) ด้วยการส่ง dict ดิบผ่าน `com.atproto.repo.*` โดยตรงแทบทุก action — **ข้อยกเว้นเดียว**: `_save_muted_words()` (ดูหัวข้อ Mute words) ใช้ `.model_dump()` กับ object ที่ SDK parse มาให้แล้ว (ความเสี่ยงต่างจากกรณีสร้าง object ใหม่) — ยังไม่ผ่านการทดสอบจริง
- **Record parsing**: ดึง embed/quote จาก `post.embed` (hydrated View) เสมอ ไม่ใช้ `record.embed`
- **DB**: เข้ารหัสด้วย `sqlcipher3`, master key เก็บแยกไฟล์เข้ารหัส DPAPI — ผูกกับเครื่อง+user เดียวกันเท่านั้น
- **UI pattern**: ทุก dialog ที่ popup ตรงจาก gesture ต้อง bracket `gui.mainFrame.prePopup()`/`postPopup()` ด้วย `EVT_CLOSE` เสมอ (ไม่ใช่เรียก `postPopup()` ทันทีหลัง `Show()` เพราะเป็น non-modal — เคยเป็นบั๊กจริงใน `script_quickNewPost`, แก้แล้ว)
- **ComposeDialog ใช้ `Show()`** เวลาเรียกตรงจาก gesture (ไม่ใช่ `ShowModal()`)
- **Focus/speech**: ใช้ `ui.message(text, speechPriority=Spri.NOW)` + สำหรับ action-result หลัง popup menu ปิด ใช้ `core.callLater(150-200ms)` ร่วมกับ `speech.cancelSpeech()` ก่อนพูด (แก้ปัญหา focus เด้งกลับ ListCtrl พูดขัด — เจอปัญหานี้มานาน แก้ได้แล้วผ่าน Gemini)
- **Clipboard ต้องรันบน main thread**: เรียก `wx.CallAfter()` ก่อน copy รูปเข้า clipboard เสมอ ไม่งั้นเจอ COM `CoInitialize` error เพราะ Windows Clipboard/OLE ต้องรันบน thread ที่ COM-initialized แล้ว (worker thread ธรรมดาไม่ได้ init)
- **User action ระบบเดียว**: `UserActionMixin` ใช้ร่วมกันทั้ง FeedWindow (Alt+U), UserListDialog, ProfileDialog, UserTimelineDialog, ThreadDialog — เมนูชุดเดียวโค้ดเดียว ดึง viewer state (`following`/`muted`/`blocking`) สดจาก `get_profile()` ก่อนโชว์เมนูทุกครั้ง เพื่อให้ Follow/Unfollow, Mute/Unmute, Block/Unblock ถูกฝั่งเสมอ
- **Embed view ระบบเดียว**: `EmbedViewMixin` ใช้ร่วมกันทั้ง FeedWindow/UserTimelineDialog/ThreadDialog — เมนู "Embed..." (เดิมชื่อ "View embed...") + open/send-to-Be-My-Eyes/copy handlers
- **Design หลักการเมนู**: list ที่มีทั้ง post + user (เช่น timeline ปกติ) ต้องมีทั้ง Post action + User action แยกกัน, list ที่มีแค่ user ล้วนๆ (Followers/Following) มี User action จุดเดียวพอ
- **ProfileDialog** เป็น dialog แสดงข้อมูลอย่างเดียว (ไม่มี DID, มี follower/following/posts count เป็นข้อความ) ปุ่มเดียวคือ Close, กด Alt+U เพื่อเปิดเมนู user action ของคนที่กำลังดูอยู่ได้
- **UserListDialog** (Followers/Following) ปุ่มเดียวคือ "User action" (+Enter/Alt+U) ไม่มีปุ่มแยก View/Actions
- **ดาวน์โหลดไฟล์แนบ (embed)**: ต้องส่ง header `User-Agent` แบบเบราว์เซอร์ + `Referer: https://bsky.app/` เสมอ (Bluesky CDN มี hotlink protection) และเช็ค `Content-Type` จริงจากเซิร์ฟเวอร์ก่อนเซฟไฟล์ ไม่ใช่เชื่อ suffix ที่ขอไปเฉยๆ
- **รูปเป็น WebP**: CDN ของ Bluesky ส่งเป็น WebP เสมอ ซึ่ง `wx.Image` เวอร์ชันที่มากับ NVDA อ่านไม่ได้ ("Unknown image data format") ต้องแปลงเป็น PNG ด้วย Pillow (vendor เพิ่ม) ก่อนส่งให้ wx/clipboard/Be My Eyes เสมอ
- **วิดีโอเป็น HLS (.m3u8)**: ห้ามดาวน์โหลด manifest มาเซฟ local ตรงๆ เพราะ URI ของ segment ย่อยข้างในเป็น relative path (อิงตำแหน่งเซิร์ฟเวอร์เดิม) เปิดจาก local แล้วหา segment ไม่เจอ — ต้อง rewrite URI ทุกบรรทัดในไฟล์เป็น absolute (`urljoin`) ก่อนเซฟ ถึงจะเล่นได้จริง
- **Be My Eyes**: ส่งไฟล์ผ่าน `ShellExecuteW(None, "open", "shell:appsFolder\\<AUMID>", file_path, None, 1)` — เทคนิคเดียวกับ Explorer "Open with" บน UWP app
- **create_post รวมเป็นฟังก์ชันเดียว**: `client.create_post(text, attachments=None, reply_ref=None, quote_ref=None, link_card=None, facets=None)` ครอบคลุมทุก combo (ข้อความล้วน/รูป/reply/quote/quote+รูป/link card) แทนที่ `create_post`/`create_post_with_images` แบบเดิม
- **Reply ต้องมี root+parent strongRef**: parent เอาจากโพสต์ที่กด reply ตรงๆ ได้เลย, root ถ้าโพสต์นั้นเป็น top-level ก็คือตัวมันเอง แต่ถ้าเป็น reply อยู่แล้วต้องเดิน `getPostThread` หา root จริง (`get_reply_refs()`) — ยังไม่ผ่านการทดสอบจริง
- **RichText facets + link card ต้องทำเองฝั่ง client ทั้งหมด**: AT Protocol server ไม่สร้าง embed/facet อัตโนมัติจาก URL ในข้อความให้เลย แม้แต่เว็บ bsky.app เองก็ทำฝั่ง client (คนละ URL ที่ไม่โพสต์ก็ไม่มี card) — NVSky ดึง og:tags ตรงจาก URL เป้าหมายเอง (ไม่ผ่าน proxy ภายในของ Bluesky ที่ไม่มีเอกสารทางการ) ด้วย regex (ไม่ vendor HTML parser เพิ่ม)
- **Repost/Bookmark ต้อง track state ใน DB**: เพิ่มคอลัมน์ `viewer_repost_uri`, `viewer_bookmarked` ใน `posts` table (มี migration `ALTER TABLE ... ADD COLUMN` แบบ try/except สำหรับ DB เดิมที่ยังไม่มีคอลัมน์นี้)
- **Bookmark เป็นฟีเจอร์ทางการของ Bluesky** (เปิดตัวจริง ก.ย. 2025) เก็บแบบ "off-protocol" (ไม่ใช่ repo record สาธารณะเหมือน follow/mute/block) ผ่าน `app.bsky.bookmark.create_bookmark`/`delete_bookmark` — พารามิเตอร์/ชื่อ field `viewer.bookmarked` ยังไม่ได้ยืนยัน schema แน่ชัด 100% (endpoint ใหม่ เอกสารยังไม่ละเอียด)
- **Threadgate (Edit who can reply) ทำได้จริง** ผ่าน record `app.bsky.feed.threadgate` (rkey ต้องตรงกับ rkey ของโพสต์ root) — รองรับ Everyone/Following only/Mentioned only/Nobody (ยังไม่รองรับ List-based rule เพราะไม่มีระบบจัดการ List)
- **Sort order ไม่ใช่ query DB**: การเรียง newest/oldest first เป็น display-level sort (`_applySortOrder()` เรียง `self._posts` ใหม่ทุกครั้งด้วย `sorted(..., key=indexed_at)`) ไม่ใช่ ORDER BY ที่เปลี่ยนตาม setting เพราะ lazy-load pagination ผูกกับทิศทาง DESC จาก network เสมอ (API เดินคอนเนอร์เก่าได้ทางเดียว) — จุด edge-case ที่ผูกกับ sort order: lazy-load trigger edge (บน/ล่าง) และ fallback focus index (0) ต้องคำนวณตาม setting ปัจจุบันเสมอ
- **Account-scoped settings tab ต้องมี `reload()`**: แท็บไหนใน Settings ที่โหลดข้อมูลผูกกับ "บัญชี active" ตอน `__init__` (Profile, Muted words) ต้องมีเมธอด `reload()` แล้วต่อสายเข้า callback `onAccountChanged` ของ `AccountsPanel.onSetActive()` เสมอ — ไม่งั้นสลับบัญชีระหว่างเปิด Settings ค้างไว้จะเห็นข้อมูลผิดบัญชีจนกว่าจะปิดเปิดใหม่ (พบเป็นบั๊กจริงตอนทำ Muted words แล้วแก้ทั้ง Profile ไปด้วย เพราะเป็น pattern เดียวกัน)

## ฟีเจอร์ที่เสร็จและทดสอบผ่านแล้ว

### บัญชี/ความปลอดภัย
- Login ด้วย App Password (เก็บเข้ารหัส DPAPI), validate pattern แบบ soft-warning
- รองรับหลายบัญชี, Set active account, Remove account (confirm + ลบ cache), Clear cache
- DB เข้ารหัสทั้งไฟล์ด้วย sqlcipher3

### หน้า Feed (Home)
- ListCtrl 4 คอลัมน์: Author, Message, Posted, Embed
- Lazy load (cache-first, network เฉพาะ cache หมด), จำตำแหน่ง focus/scroll ต่อบัญชี
- Mark read/unread อัตโนมัติตอน focus, unread count ใน status bar, Space = ไปโพสต์แรกที่ยังไม่อ่าน
- Reply/Quote/Repost แสดงผลถูกต้องในคอลัมน์ Message/Author
- Check for updates (F5), Filter Following/Discover/For You (Discover/For You ยังเป็น stub)

### Embed (View embed → เปลี่ยนชื่อเป็น "Embed...")
- รูป: เปิดด้วยโปรแกรมเริ่มต้น / ส่ง Be My Eyes / Copy to clipboard — ทำงานถูกต้องหลังแก้ WebP→PNG (Pillow) + Referer header
- วิดีโอ: เปิดด้วยโปรแกรมเริ่มต้น (ดาวน์โหลด+rewrite URI ก่อน) + Copy video URL — ทำงานถูกต้องหลังแก้ relative-path
- ลิงก์: Open in browser + Copy link URL
- รองรับหลายรูปในโพสต์เดียว (submenu ต่อรูป, รูปเดียวไม่มี submenu) และ recordWithMedia (quote + รูป/วิดีโอ/ลิงก์แนบพร้อมกัน)

## โค้ดที่ส่งไปแล้ว รอทดสอบ

_ทั้งหมดนี้เขียนเสร็จและส่งโค้ดให้แล้วในแชท แต่ยังไม่ได้รับการทดสอบ/ยืนยันผลจริงจากผู้ใช้ — ใช้ list นี้เป็นเช็คลิสต์ไล่ทดสอบทีละอันในแชทถัดไป_

### Post action menu
- **Reply / Repost / Quote post** — เปิด `ComposeDialog` พร้อม context (แสดง preview บรรทัดบน), unified `create_post()` — จุดเสี่ยงสุด: `get_reply_refs()` เฉพาะกรณี reply-ต่อ-reply (ต้องเดิน `getPostThread` หา root)
- **Undo repost** — toggle ผ่าน `viewer_repost_uri` ที่เพิ่งเพิ่มใน DB
- **Bookmark / Remove bookmark** — จุดเสี่ยงสุด: param `app.bsky.bookmark.create_bookmark`/`delete_bookmark` ยังไม่ยืนยัน schema เป๊ะๆ
- **View thread...** — `ThreadDialog` ใหม่ (ancestors + replies แบบ depth-indent ด้วย "> ") จุดเสี่ยง: `client.get_thread()`/`getPostThread` shape
- **Edit who can reply...** — threadgate 4 โหมด เฉพาะโพสต์ตัวเอง
- **Mark as read/unread** — เดี่ยว + multi-select ("Mark N selected as..."), `Ctrl+A` select all

### User action menu
- **Unfollow/Unmute/Unblock** — ใช้ backend function ที่มีอยู่แล้วเดิม แค่เพิ่งต่อเข้า UI จริง
- **View timeline** — `UserTimelineDialog` ใหม่ (fetch-once ยังไม่ lazy-load)
- **Show followers/following** — ดึงครบทุกหน้า (ไม่ lazy-load), title โชว์เจ้าของ list ตาม display name/handle setting

### Compose
- **RichText facets** (@mention/URL คลิกได้จริง) — จุดเสี่ยง: `resolve_handle` ชื่อ method ยังไม่ยืนยัน 100%
- **Link-card auto-embed** — checkbox "Attach link preview for <url>" auto-detect จาก URL ในข้อความ ยกเลิกได้ก่อนโพสต์ — จุดเสี่ยง: regex ดึง og:tags อาจพลาดกับเว็บที่ render ด้วย JS หรือจัด attribute แปลก (ไม่ error แค่ได้การ์ดไม่สวย)
- **attachButton ถูก disable ระหว่างโพสต์** (กัน race condition)

### Navigation เพิ่มเติม (ไม่มีในแผนเดิม เพิ่มระหว่างคุยงาน)
- **Left/Right = jump ไปโพสต์ของ/mention คนที่ focus อยู่** — จำลองเป็น virtual "search: @handle" filter ในแท็บปัจจุบัน (จับคู่ handle คนโพสต์ตรง หรือ "@handle" เป็น substring ในข้อความ) ล็อก anchor handle ไว้จนกว่าจะ navigate ด้วยวิธีอื่น
- **Enter = quick action ที่ตั้งค่าได้** — Settings > General > "Enter key action on a post" (view_thread/reply/quote/repost/like/mark_read, default view_thread)

### Settings
- **Sort order (Newest/Oldest first) ใช้งานจริงแล้ว** — เดิมบันทึกค่าไว้เฉยๆ ไม่มีผล ตอนนี้กระทบทั้งการ render, lazy-load trigger edge, และ fallback focus position
- **Profile tab ใช้งานได้จริง** — แก้ display name/bio (ปุ่ม Save แยก) + เปลี่ยน avatar/banner (อัปโหลดทันทีตอนเลือกไฟล์)
- **Muted words tab ใหม่** — เพิ่ม/ลบคำ/แท็กที่ mute ผ่าน `putPreferences`, จุดเสี่ยงสุด: `_save_muted_words()` ใช้ `.model_dump()` (ข้อยกเว้นเดียวในโปรเจกต์ที่ไม่ใช้ raw dict ล้วน)
- **Account-switch reload bug แก้แล้ว** — สลับ active account ระหว่างเปิด Settings ค้างไว้ ตอนนี้ Profile/Muted words tab reload ข้อมูลให้อัตโนมัติ

### แก้ไขจาก Gemini (ผู้ใช้ merge เอง + ผมช่วยรีวิว/ปรับเพิ่ม)
- `crypto.py` — เขียนใหม่ทั้งไฟล์ ใส่ `argtypes`/`restype` ให้ทุก `ctypes.windll` call (กัน pointer truncation บน 64-bit) + flag `CRYPTPROTECT_UI_FORBIDDEN`
- `__init__.py` — `script_quickNewPost` ผูก `postPopup()` กับ `EVT_CLOSE` แทนเรียกทันทีหลัง `Show()`, `terminate()` เช็คก่อน `.remove()` + ปิด `feedWindow` ที่เปิดค้าง
- `attachments.py` — ลบ import ซ้ำในฟังก์ชัน, ลบไฟล์ temp ทิ้งถ้า Pillow แปลง WebP พัง
- `settings.py` — คืน focus ให้ `accountList` หลังลบบัญชี, ใส่ accessible name ให้ `SpinCtrl` ทั้งสองตัว

## ฟีเจอร์ที่วางแผนไว้ (ยังไม่เริ่ม)

### ระบบเสียง (Sound)
- รอผู้ใช้ตัดสินใจก่อนว่าต้องมีไฟล์เสียงอะไรสำหรับ event ไหนบ้าง (ยังไม่ตัดสินใจ ณ ตอนนี้)
- แนวทางเดิม: Settings > Sound เลือก theme (subfolder), checklist เลือกเล่นเสียง event ไหนบ้าง, เล่นผ่าน `nvwave`

### ระบบหลายแท็บ (Multi-tab) — เฟสใหญ่ถัดไป
วางแผนคร่าวๆ ตามหน้าเว็บ bsky.app: Home (มีแล้ว) → Notifications → Explore (search+trending) → Chat (DM) → Feeds → Lists → Saved
- Dynamic/pinnable tab (search/hashtag), reorder tab, จำ tab ล่าสุดข้าม session
- **งานแรกของเฟสนี้**: สกัด lazy-load/status-bar/check-for-update ของ `FeedWindow` ออกเป็น mixin กลาง ให้ `UserTimelineDialog`/`ThreadDialog`/แท็บใหม่ๆ ใช้ร่วมกัน (ตอนนี้ทั้งสอง dialog นี้เป็น fetch-once ไม่มี lazy-load เลย เป็น known limitation ที่ตั้งใจเลื่อนมาทำพร้อมเฟสนี้)

### Background fetch อัตโนมัติ
- อ่านค่า interval จาก General settings มา schedule จริงด้วย `wx.Timer` — รอ Multi-tab เสร็จก่อน (ต้องรู้จำนวนแท็บ/interval ต่อแท็บ)

### I18N เต็มรูปแบบ
- มีแค่ `__init__.py` ที่ wrap `_()` ไว้ ไฟล์อื่นยัง hardcode ภาษาอังกฤษทั้งหมด (ตอนนี้เยอะขึ้นมากหลัง Phase 2)
- ตั้งใจไว้เป็นงานก้อนใหญ่ทำรวดเดียวจบท้ายๆ ก่อนใกล้ปล่อยจริง ไม่ปนกับ fix อื่น

### List management
- ยังไม่มีเลย — เป็นเงื่อนไขที่บล็อกอยู่ 2 จุด: List-based threadgate rule, List-based mute (list mute ทั้งกลุ่ม)

## หมายเหตุสำหรับแชทถัดไป

ลำดับที่แนะนำ: ไล่ทดสอบตาม "โค้ดที่ส่งไปแล้ว รอทดสอบ" ด้านบนทีละหัวข้อ โดยเฉพาะจุดที่ทำเครื่องหมาย "จุดเสี่ยงสุด" ไว้ — ถ้า error ให้ paste traceback มาตรงๆ จะวิเคราะห์ได้เร็วสุด จากประสบการณ์รอบ View embed พบว่าเดา schema/behavior ของ SDK ล่วงหน้าไม่แม่นเท่า debug จาก error จริง โดยเฉพาะ endpoint ที่เพิ่งออกใหม่ (Bookmark) หรือ SDK method ที่ไม่เคยใช้มาก่อนในโปรเจกต์ (threadgate, muted words, resolve_handle, get_post_thread)

``n
## File: plan-03.md
`md
# NVSky — แผนพัฒนา (อัปเดต v3)

_อัปเดตจาก plan-02.md หลังจบรอบทดสอบ+แก้บั๊ก Phase 2 ทั้งหมดจนนิ่งแล้ว พร้อมเข้าเฟส Multi-tab เป็นเฟสถัดไป_

## ภาพรวมโปรเจกต์

NVDA add-on สำหรับใช้งาน Bluesky (AT Protocol) โดย Virus พัฒนาคนเดียว อยู่ในสถานะ closed development (ยังไม่ปล่อยสู่สาธารณะ) ใช้ SDK `atproto` (Python) โครงสร้างคล้าย YoutubePlus/MessengerAccess ที่เคยทำมาก่อน

รองรับ NVDA 2026.1 ขึ้นไปเท่านั้น (64-bit / Python 3.13) ไม่รองรับ 32-bit

## โครงสร้างไฟล์ปัจจุบัน

```
NVSky/
├── manifest.ini
├── buildVars.py
└── globalPlugins/
    └── NVSky/
        ├── __init__.py       # entry point, gesture, prePopup/postPopup, i18n init
        ├── db.py             # SQLite (sqlcipher3 เข้ารหัส) layer
        ├── client.py         # atproto wrapper (lazy import, ทุก action)
        ├── crypto.py         # DPAPI encrypt/decrypt (Windows)
        ├── attachments.py    # ดาวน์โหลด/เปิด/ส่ง Be My Eyes/copy สำหรับ embed
        ├── settings.py       # หน้าตั้งค่า (Accounts/General/Display/Sound/Profile/Muted words)
        ├── feedWindow.py     # FeedWindow, ProfileDialog, UserListDialog, UserTimelineDialog,
        │                     # ThreadDialog, UserActionMixin, EmbedViewMixin
        ├── compose.py        # ComposeDialog (โพสต์ใหม่/reply/quote)
        └── lib/              # vendored libs (atproto, sqlcipher3, pydantic, Pillow ฯลฯ)
```

ทุกไฟล์ในนี้ (ยกเว้น `manifest.ini`/`buildVars.py`) แก้ไขไปมากตลอด Phase 2 — **อย่าอ้างอิงโค้ดจาก plan.md เก่าหรือความจำเดิม ให้ยึดไฟล์ `nvsky.md`/ซอร์สจริงที่แนบมาในแชทนี้เป็นความจริงเท่านั้น**

## สถาปัตยกรรม/การตัดสินใจสำคัญ

- **Lib**: ใช้ `pip install --target lib/ --python-version 3.13 --implementation cp --abi cp313 --platform win_amd64 --only-binary=:all:` เสมอ
- **Lazy import**: `atproto`/`pydantic` ฯลฯ import เฉพาะตอนใช้จริง ลด NVDA startup time
- **Ghost dependency**: ใช้ `sqlcipher3` เพราะ `sys.modules` เป็น cache ระดับ process ไม่แยกตาม add-on
- **atproto SDK bug (เขียน/create)**: หลีกเลี่ยง typed Record models (MarshalX/atproto#354) ด้วยการส่ง dict ดิบผ่าน `com.atproto.repo.*` โดยตรงแทบทุก action
- **atproto SDK bug (อ่าน/response แบบ recursive union)**: `getPostThread` มี pydantic bug คนละตัวกับข้างบน — response เป็น discriminated union ที่อ้างอิงตัวเองซ้อนกัน (`ThreadViewPost` มี parent/replies เป็น `ThreadViewPost` อีกที) parse ผ่าน SDK ไม่ได้เลย (`Unable to extract tag using discriminator 'py_type' | 'pyType'`) **แก้โดยดึง JSON ดิบตรงจาก public AppView (`public.api.bsky.app`, ไม่ต้อง auth) แล้วแปลงเป็น `SimpleNamespace` เอง** (`_dict_to_ns`) — ต้องแปลง key จาก camelCase → snake_case ระหว่างทางด้วย (`_camel_to_snake`) เพราะ SDK จริงแปลงให้อัตโนมัติแต่ JSON ดิบไม่ทำให้ — ยืนยันแล้วว่าใช้ได้จริง (`get_thread`, `get_reply_refs`) ข้อจำกัด: thread จาก public endpoint ไม่มี `viewer` field (like/repost/bookmark ของเราเอง) เพราะไม่ auth
- **Record parsing**: ดึง embed/quote จาก `post.embed` (hydrated View) เสมอ ไม่ใช้ `record.embed`
- **DB**: เข้ารหัสด้วย `sqlcipher3`, master key เก็บแยกไฟล์เข้ารหัส DPAPI — ผูกกับเครื่อง+user เดียวกันเท่านั้น
- **UI pattern**: ทุก dialog ที่ popup ตรงจาก gesture ต้อง bracket `gui.mainFrame.prePopup()`/`postPopup()` ด้วย `EVT_CLOSE` เสมอ
- **Dialog ที่โหลดข้อมูล async ต้องโหลดให้เสร็จก่อนค่อยสร้าง**: ห้ามสร้าง dialog เปล่าๆ ขึ้นมาก่อนแล้วค่อยเติมข้อมูลทีหลัง (ทำให้เสียโฟกัส/title ว่างตอนเปิด) — ตอนนี้ `ThreadDialog`/`UserTimelineDialog` ทั้งคู่แก้เป็น fetch data ก่อน แล้วสร้าง dialog พร้อมข้อมูลครบรอบเดียว (fetch อยู่ในเมธอดของฝั่งที่เปิด เช่น `FeedWindow._openThread`/`UserActionMixin.showTimeline` ไม่ใช่ใน `__init__` ของ dialog เอง)
- **ComposeDialog ใช้ `Show()`** เวลาเรียกตรงจาก gesture (ไม่ใช่ `ShowModal()`)
- **Focus/speech — `_announce_now()` เป็นจุดรวมเดียว**: ใช้ `core.callLater(~100-200ms)` ร่วมกับ `speech.cancelSpeech()` ก่อนพูดเสมอ ไม่ใช่แค่ `speechPriority=Spri.NOW` เฉยๆ (ไม่พอ เพราะข้อความมักถูกพูดก่อนที่ NVDA จะ queue เสียงอ่าน ListCtrl ที่โฟกัสกลับมา ทำให้โดนบังทีหลังอยู่ดี) — ทุกจุดที่พูด "please wait"/ผลลัพธ์ action หลังปิดเมนู/dialog ต้องผ่านฟังก์ชันนี้ ไม่เรียก `nvdaUi.message()` ตรงๆ
- **Clipboard ต้องรันบน main thread**: เรียก `wx.CallAfter()` ก่อน copy รูปเข้า clipboard เสมอ ไม่งั้นเจอ COM `CoInitialize` error
- **User action ระบบเดียว**: `UserActionMixin` ใช้ร่วมกันทั้ง FeedWindow (Alt+U), UserListDialog, ProfileDialog, UserTimelineDialog, ThreadDialog — เมนูเปิด**ทันที** (Follow/Unfollow ฯลฯ เป็นข้อความเดียว ไม่ต้องรอ fetch ก่อนโชว์เมนู) พอกด Follow/Mute/Block ค่อย fetch สถานะจริงแล้วถาม **ยืนยันด้วย Yes/No dialog ที่ข้อความตรงทิศทางจริง** (เช่น "Unfollow @x?" ถ้าตามอยู่แล้ว) ก่อนทำจริงเสมอ (`_toggleRelation`)
- **Embed view ระบบเดียว**: `EmbedViewMixin` ใช้ร่วมกันทั้ง FeedWindow/UserTimelineDialog/ThreadDialog — เมนู "Embed..." + open/send-to-Be-My-Eyes/copy handlers, รูปเดียวไม่มี submenu (รูปหลายรูปมี submenu ต่อรูป), วิดีโอ/ลิงก์มี Copy URL แยกจาก Open เสมอ
- **Design หลักการเมนู**: list ที่มีทั้ง post + user ต้องมีทั้ง Post action + User action แยกกัน, list ที่มีแค่ user ล้วนๆ มี User action จุดเดียวพอ, list ที่มีแค่ post หลายคน (Thread) มี Author column แยกจาก Message เสมอ (ต่างจาก Timeline ที่คนโพสต์คนเดียวไม่ต้องมี column นี้)
- **UI list ทุกจุดใช้ `wx.ListCtrl`** ไม่ใช้ `wx.ListBox`/`wx.ListBox`-derivative เพราะ `ListBox` มีปัญหาโฟกัสตอน list ว่างเปล่า ต้องเขียน workaround เพิ่ม (เจอใน `AccountsPanel`/`MutedWordsPanel` มาก่อน แก้เป็น `ListCtrl` หมดแล้ว)
- **ProfileDialog** เป็น dialog แสดงข้อมูลอย่างเดียว ปุ่มเดียวคือ Close, กด Alt+U เพื่อเปิดเมนู user action ได้
- **UserListDialog** (Followers/Following) ปุ่มเดียวคือ "User action" (+Enter/Alt+U)
- **ดาวน์โหลดไฟล์แนบ (embed)**: ต้องส่ง `User-Agent` แบบเบราว์เซอร์ + `Referer: https://bsky.app/` เสมอ + เช็ค `Content-Type` จริงก่อนเซฟไฟล์
- **รูปเป็น WebP**: แปลงเป็น PNG ด้วย Pillow (vendor เพิ่ม) ก่อนส่งให้ wx/clipboard/Be My Eyes เสมอ
- **วิดีโอเป็น HLS (.m3u8)**: rewrite URI ทุกบรรทัดในไฟล์เป็น absolute (`urljoin`) ก่อนเซฟ local เสมอ (relative path จะหา segment ไม่เจอ)
- **Be My Eyes**: `ShellExecuteW(None, "open", "shell:appsFolder\\<AUMID>", file_path, None, 1)`
- **create_post รวมเป็นฟังก์ชันเดียว**: `client.create_post(text, attachments=None, reply_ref=None, quote_ref=None, link_card=None, facets=None)` ครอบคลุมทุก combo — ทดสอบผ่านแล้วทุกกรณี รวม reply-ต่อ-reply
- **RichText facets + link card ทำเองฝั่ง client ทั้งหมด**: ดึง og:tags ตรงจาก URL เป้าหมายเอง (ไม่ผ่าน proxy ภายในของ Bluesky) ด้วย regex — ถ้ามีมากกว่า 1 URL ในข้อความ ต้องให้ผู้ใช้เลือกว่าจะ preview อันไหน (embed ใส่ได้แค่ 1 ลิงก์ต่อโพสต์จริง)
- **Repost/Bookmark track state ใน DB**: คอลัมน์ `viewer_repost_uri`, `viewer_bookmarked` ใน `posts` table (มี migration `ALTER TABLE ... ADD COLUMN` แบบ try/except)
- **Bookmark เป็นฟีเจอร์ทางการของ Bluesky** (ก.ย. 2025) ผ่าน `app.bsky.bookmark.create_bookmark`/`delete_bookmark` — **ทดสอบแล้ว sync ขึ้นเว็บจริง** UI ใช้คำว่า Save/Unsave (ตัวแปร/ฟังก์ชันฝั่ง backend ยังชื่อ bookmark เหมือนเดิม)
- **Threadgate (Edit who can reply)**: ผ่าน record `app.bsky.feed.threadgate` — ดึงค่าปัจจุบันมา pre-select ใน dialog ด้วย `get_threadgate_mode()` (อ่านผ่าน `com.atproto.repo.get_record` ธรรมดา ไม่ติด union bug เพราะเป็น record เดี่ยวไม่ recursive) — `RecordNotFound` ตอนอ่านเป็นเรื่องปกติ (ยังไม่เคยตั้งค่า) ไม่ log เป็น error
- **Sort order เป็น display-level sort**: `_applySortOrder()` เรียง `self._posts` ใหม่ทุกครั้งด้วย `sorted(..., key=indexed_at)` ไม่ใช่ ORDER BY — lazy-load trigger edge และ fallback focus index ต้องคำนวณตาม setting ปัจจุบันเสมอ
- **Lazy-load "No more posts" ต้องแยก 2 กรณี**: cursor จาก server หมดจริง (จบ feed จริง) กับแค่เดิน `MAX_NETWORK_PAGE_WALK` หน้าไม่ครบรอบ (เช่นเจอโพสต์ mute/hidden ติดกันเป็นพืด) — กรณีหลังต้องรีเซ็ต `_loadTriggeredAtCurrentLength = False` ด้วย ไม่งั้นต้องปิดเปิดหน้าต่างใหม่ถึงจะลองโหลดต่อได้ (เคยเป็นบั๊กจริง แก้แล้ว)
- **Account-scoped settings tab ต้องมี `reload()`**: แท็บที่โหลดข้อมูลผูกกับ "บัญชี active" (Profile, Muted words) ต้องมีเมธอด `reload()` ต่อสายเข้า callback `onAccountChanged` ของ `AccountsPanel` — **ต้องเช็คด้วยว่า `NVSkySettingsPanel.makeSettings()` ส่ง `onAccountChanged=onAccountChanged` เข้า `AccountsPanel(...)` จริงๆ** (เคยลืมส่ง ทำให้ทั้งระบบไม่ทำงานทั้งที่โค้ดทุกจุดถูกหมดแล้ว)

## ฟีเจอร์ที่เสร็จและทดสอบผ่านแล้วทั้งหมด

### บัญชี/ความปลอดภัย
- Login ด้วย App Password (เก็บเข้ารหัส DPAPI), validate pattern แบบ soft-warning
- รองรับหลายบัญชี, Set active account (+ reload แท็บที่ผูกบัญชีอัตโนมัติ), Remove account (confirm + ลบ cache), Clear cache
- DB เข้ารหัสทั้งไฟล์ด้วย sqlcipher3

### หน้า Feed (Home)
- ListCtrl 4 คอลัมน์: Author, Message, Posted, Embed — รองรับ multi-select (`Ctrl+A`/`Shift+ลูกศร`)
- Lazy load (cache-first, network เฉพาะ cache หมด), จำตำแหน่ง focus/scroll ต่อบัญชี, sort order (newest/oldest first) มีผลจริงทุกจุด
- Mark read/unread อัตโนมัติตอน focus + เมนู "Mark as..." (เดี่ยว/multi-select), unread count ใน status bar, Space = ไปโพสต์แรกที่ยังไม่อ่าน
- Left/Right = jump ไปโพสต์ของ/reply-ถึง/mention คนที่ focus อยู่ (DID-based, แม่นกว่า text-matching)
- Enter = quick action ตั้งค่าได้ (Settings > General)
- Check for updates (F5), Filter Following/Discover/For You (Discover/For You ยังเป็น stub)

### Post action menu (ครบทุกรายการ ทดสอบผ่านหมด)
- Reply / Repost (+ Undo) / Quote post — รวม reply-ต่อ-reply (แก้ด้วย public AppView fix)
- View thread... — `ThreadDialog` มี Author column, title จาก first post, ไม่มี "Reply:" ก่อนข้อความ (รู้ว่าเป็น reply แต่ไม่รู้ว่า reply ใคร เพราะ AT Proto record ไม่แนบ handle ผู้ถูกตอบมาด้วย)
- Edit who can reply... — 4 โหมด, ดึงค่าปัจจุบันมา pre-select ถูกต้อง
- Save / Unsave (bookmark)
- Like/Unlike, Copy (text/link), Embed submenu, Mute thread/Hide/Report

### User action menu (ครบทุกรายการ)
- Follow/Unfollow, Mute/Unmute, Block/Unblock — เมนูเปิดทันที, confirm dialog ก่อนทำจริงเสมอ
- View timeline, Show followers/following (ดึงครบทุกหน้า), View user info, Copy profile URL

### Compose
- RichText facets (@mention/URL), Link-card auto-embed (เลือกได้ถ้ามีหลาย URL), Reply/Quote พร้อม context label ในทั้ง title และ label ของช่องพิมพ์
- attachButton disable ระหว่างโพสต์

### Settings
- Accounts (ListCtrl), General (interval + Enter action), Display (sort order), Sound (placeholder), Profile (แก้ display name/bio/avatar/banner), Muted words (Add ผ่าน dialog, ListCtrl)

## ฟีเจอร์ที่วางแผนไว้ (ยังไม่เริ่ม) — เรียงตามลำดับ

### 1. ระบบหลายแท็บ (Multi-tab) ← **เฟสถัดไปที่จะทำ**
วางแผนคร่าวๆ ตามหน้าเว็บ bsky.app: Home (มีแล้ว) → Notifications → Explore (search+trending) → Chat (DM) → Feeds → Lists → Saved
- Dynamic/pinnable tab (search/hashtag), reorder tab, จำ tab ล่าสุดข้าม session
- **งานแรกของเฟสนี้**: สกัด lazy-load/status-bar/check-for-update ของ `FeedWindow` ออกเป็น mixin กลาง ให้ `UserTimelineDialog`/`ThreadDialog`/แท็บใหม่ๆ ใช้ร่วมกัน (ตอนนี้ทั้งสอง dialog นี้เป็น fetch-once ครั้งเดียวจบ ไม่มี lazy-load เลย — เป็น known limitation ที่ตั้งใจเลื่อนมาทำพร้อมเฟสนี้)

### 2. Background fetch อัตโนมัติ
- อ่านค่า interval จาก General settings มา schedule จริงด้วย `wx.Timer` — รอ Multi-tab เสร็จก่อน (ต้องรู้จำนวนแท็บ/interval ต่อแท็บ)

### 3. ระบบเสียง (Sound)
- รอผู้ใช้ตัดสินใจก่อนว่าต้องมีไฟล์เสียงอะไรสำหรับ event ไหนบ้าง
- แนวทางเดิม: Settings > Sound เลือก theme (subfolder), checklist เลือกเล่นเสียง event ไหนบ้าง, เล่นผ่าน `nvwave`

### 4. I18N เต็มรูปแบบ
- มีแค่ `__init__.py` ที่ wrap `_()` ไว้ ไฟล์อื่นยัง hardcode ภาษาอังกฤษทั้งหมด (เยอะขึ้นมากหลัง Phase 2)
- ตั้งใจไว้เป็นงานก้อนใหญ่ทำรวดเดียวจบท้ายๆ ก่อนใกล้ปล่อยจริง ไม่ปนกับ fix อื่น

### List management (ยังไม่มีเลย)
- บล็อกอยู่ 2 จุด: List-based threadgate rule, List-based mute

## หมายเหตุสำหรับแชทถัดไป

Phase 2 ปิดจบสมบูรณ์แล้ว ไม่มีอะไรค้างทดสอบ — เริ่มตรงนี้ได้เลยที่ **Multi-tab เฟสที่ 1: สกัด lazy-load/status-bar/check-for-update ของ FeedWindow เป็น mixin กลาง** แนะนำให้เริ่มจากออกแบบ interface ของ mixin ก่อน (เมธอด/state ที่ FeedWindow มีตอนนี้ที่ต้อง generalize ให้ใช้ได้ทั้ง Home/Timeline/Thread/แท็บใหม่ในอนาคต) แล้วค่อย refactor FeedWindow ให้ใช้ mixin ตัวเองก่อน เพื่อพิสูจน์ว่า mixin ถูกต้องโดยไม่กระทบ UX เดิม จากนั้นค่อยย้าย Timeline/Thread มาใช้ทีหลัง

``n
## File: plan-04.md
`md
# NVSky Development Plan (plan-04)

Carried over from the previous chat. Multi-tab architecture was mis-scoped
once already this session (see "Lesson" below) — read this whole doc
before writing structural code.

## Response format & workflow conventions

These are already saved in Claude's memory, restated here for certainty:

- Code changes go as `old_str:`/`new_str:` blocks (not git diff), applied
  via a Notepad++ script the user built with Gemini's help.
  - `old_str` needs 2-3 unique context lines before AND after the changed
    code. Never a bare short/generic line like `try:` alone as the whole
    anchor.
  - Must match the real file's exact whitespace/indentation.
  - A single contiguous change stays ONE block, not split into tiny ones.
  - State the target filename clearly right before each block.
  - When one file has multiple separate edit locations in the same
    message, number them as sub-headings under that file's heading —
    e.g. file heading, then `1.1`, `1.2`, ... before each respective
    block — so it's easy to confirm all edits were applied.
- Always read the actual current source file before proposing code for
  it. Don't rely on a plan doc (including this one) as source of truth —
  the user may have already applied or adjusted patches independently of
  what's described here.
- Chat replies in Thai. Code — including comments and UI strings — in
  English only.
- For multi-part fixes in one reply, use `###` headers to separate each
  fix point.
- `log.info()` used sparingly — only for output genuinely needed for
  debugging (e.g. a value worth copy/pasting back), not routine noise.
- Anything touching a lexicon/endpoint NVSky hasn't exercised before
  (this session: threadgate, postgate, listNotifications, getPosts) gets
  flagged EXPERIMENTAL, with a note to paste back the traceback if it
  errors.
- For anything touching overall window/UI **structure** (not just one
  function or one dialog's internals), restate the intended shape back
  to the user in plain terms before writing code — see lesson below.

## Lesson from this session (important)

Claude built `NotificationsWindow` as a fully separate `wx.Dialog` opened
via its own gesture (NVDA+Alt+N) — but the actual goal was a **single
multi-tab window**, Ctrl+Tab between Home/Notifications/etc inside one
window, opened with one command. This wasn't caught until after the user
had already patched the wrong-shaped code in and gone looking for the
tab that didn't exist. Root cause: Claude inferred the shape from
"extract a shared mixin" language instead of confirming it directly.
Don't repeat this — confirm structural shape explicitly before coding it.

## Confirmed multi-tab architecture

- **One top-level window** (`MainWindow`) containing a `wx.aui.AuiNotebook`
  (part of core wx, no new dependency — also gives tab drag-reorder for
  free, which was already planned separately).
- Opened with a single command (NVDA+Alt+B, replacing the old
  Home-only gesture). No more per-tab-type gestures.
- **Each tab is a `wx.Panel`**, not a `wx.Dialog`, embedded as a notebook
  page. `FeedWindow` and the wrongly-separate `NotificationsWindow` both
  need to be converted from `wx.Dialog` to panel form.
- **Title + status bar are per-tab** — each panel shows its own
  title/status info (not a single shared frame-level status bar). Needs
  a concrete design for how a `wx.Panel` shows "title" (frame title bar
  doesn't apply per-tab) and where each tab's status text renders —
  open implementation detail for the new chat.
- **Home is a permanent tab** — cannot be closed (Ctrl+W is a no-op on
  it, or reassign focus without removing it), but CAN be reordered like
  any other tab.
- **Closable tabs** (can be opened, left open, and removed with Ctrl+W):
  Notifications, and all future feed-like tabs (Explore, Feeds, Lists,
  Saved), AND things that used to be separate list-based dialogs —
  View Thread, User Timeline, Show Followers/Following-list, and any
  future tab of this kind. These are all `wx.ListCtrl`-driven and the
  user should be able to leave them open and refresh them, same as
  Home/Notifications.
- **Single-item info dialogs stay as `wx.Dialog`** — correct as before,
  no change needed. This covers Profile info (`ProfileDialog`) and the
  new Post Info dialog (see below). Rule of thumb: if it's a list the
  user might want to leave open and refresh, it's a tab; if it's a
  one-shot "look and close" detail view, it's a dialog.
- **`FeedListMixin` stays as the shared base** for every tab's
  lazy-load/status-bar/check-for-update/focus-restore scaffolding —
  this part of the design was correct and tested working (Home +
  Notifications data layer both already function against it). Only the
  container (Dialog → Panel) needs to change, not the mixin's logic.

## New tab-level features to add alongside the panel conversion

- **Unified action menu** — rename/merge into a single mixin (e.g.
  `ItemActionMixin`) that any tab can call, adapting which actions are
  offered based on what kind of item is focused: a real post (Like/
  Repost/Quote/Reply/Bookmark/etc. all apply), a notification wrapping a
  real post (same actions on the underlying post), or a follow
  notification with no post (only User action — follow/mute/block —
  applies). Needs `onPostAction`'s current full body (not yet seen in
  full) to design against real code, not guesses.
- **Ctrl+F5 = check every open tab for updates.** Needs a dynamic
  registry in `MainWindow` (list of currently-open tab panels, not a
  fixed set) since tabs can be opened/closed at runtime. Loop calls
  each open tab's own `onCheckForUpdates(None)`.
- **Ctrl+W = close current tab** (except the permanent Home tab).
- **Ctrl+Delete = clear current tab's cache** ("clear timeline") — wipe
  that tab's cached rows (its `feed_key` in `feed_items`, or the whole
  `notifications` table for that tab) and reload empty. Needs a new
  `db.clear_feed_cache(account_id, feed_key)` plus a notifications
  equivalent.
- **Check-for-updates / Fetch-previous-posts (Shift+F5) messages must
  use each tab's own name** — already works via `self.TAB_NAME`, just
  needs to keep working after the Dialog→Panel conversion.
- **Post Info** (new) — dialog showing: author, full post text, time,
  reply count, repost count, like count — mirroring what the feed
  screen already shows at a glance, laid out for a one-shot read. Add
  as a new item in the unified action menu.

## Before writing any of this code

Ask for **current full `feedWindow.py` + `__init__.py`** again at the
start of the next chat — the user has already hand-applied several
rounds of patches (including working around the Dialog/Panel mixup by
patching what was sent), so Claude's last-known copy of these files is
stale relative to what's actually on disk. Read fresh before proposing
any structural changes, especially `onPostAction`'s full body.

## Already shipped and working (don't redo)

- Home timeline: lazy-load, status bar, check-for-updates, mark-read,
  jump-to-user, select-all — all tested working, including the repost
  feed-ordering fix (feed_items.indexed_at uses the repost's own
  timestamp for reposts, not the original post's indexed_at) and the
  Shift+F5 manual "fetch previous posts" replacement for the old
  auto-lazy-load-on-scroll (which had UX confusion issues).
- `feed_items` mapping table (account_id, feed_key, uri, indexed_at) —
  decouples "post content cache" (`posts`, deduped by uri) from
  "which feed(s) contain this uri and in what order" — this is what
  makes multiple post-based tabs (Explore/Feeds/Lists/Saved) safe to
  add later without collisions.
- Edit-who-can-reply redesign (threadgate + postgate) — tested working.
  One small polish item still open: initial dialog focus should land on
  the radio box, not the OK button (may already be fixed — verify).
- Notifications data layer (db table, `sync_notifications`,
  `resolve_posts` for always showing the original liked/reposted/
  replied/mentioned/quoted message text) — tested working data-wise.
  ONLY the UI container is wrong (separate Dialog instead of a tab) —
  the db.py and client.py pieces should NOT need to be rebuilt, just
  re-wired into a panel instead of a dialog.
- Version numbering restarted at 1.01 as of the start of the multi-tab
  phase.

## Still separately open (not part of this multi-tab push, don't lose track)

- Discover/For You feed filter (stub)
- Sound system, Background fetch scheduler, i18n pass, General
  fetch-interval/Display sort-order wiring — none started
- Enter-key quick-action — coded, may need revisiting once the full
  action inventory (including the new unified ItemActionMixin) is
  finalized
- Mute words & tags, Profile editing — coded, believed working
- Video attachment support for compose (or confirming nothing else is
  missing there) — open question
- Chat/DM tab — deliberately last, needs its own `chat.bsky.*` lexicon
  research from scratch

``n
## File: plan-05.md
`md
# NVSky Development Plan (plan-05)

Handoff doc from a very long multi-tab + Chat build session. Paste this
into the new chat along with fresh copies of `feedWindow.py`,
`mainWindow.py`, `chatWindow.py`, `client.py`, `db.py`, and `__init__.py`
before starting structural work — several rounds of hand-applied patches
mean any prior chat's last-known copies are stale. Read the real files
first.

## Response format & workflow conventions (unchanged, still apply)

- Code changes go as `old_str:`/`new_str:` blocks (not git diff), applied
  via a Notepad++ Python Script plugin the user built (Python 2.7.18).
  `old_str` needs 2-3 unique context lines before AND after the changed
  code — never a bare short/generic line as the whole anchor. Must match
  the real file's exact whitespace/indentation. A single contiguous
  change stays ONE block. State the target filename clearly right before
  each block. Multiple edit locations in the same file in one message →
  number them as sub-headings (1.1, 1.2, ...) under that file's heading.
- **The user does NOT run PowerShell directly.** PowerShell-styled
  outputs seen in past sessions came from a Python script inside
  Notepad++, not a real PowerShell session — never ask them to run
  PowerShell/shell commands. Ask for a Notepad++ search-and-paste
  instead, or ask them to attach/upload the file directly.
- Their patch script does **not** check for duplicate `old_str` matches
  — if the same code pattern appears more than once in a file (e.g. the
  same key-binding tail across multiple `onCharHook` methods, or across
  Dialog classes that share a mixin), the script silently patches the
  FIRST match in the file, which is often the wrong one. Always make
  `old_str` anchors long enough / specific enough (include a unique
  nearby comment) to guarantee a single match, especially anywhere
  `UserActionMixin`/`ItemActionMixin` methods are shared across many
  classes.
- **No DB migrations during this phase.** The add-on is solo-dev beta —
  user is the only tester and can always delete the local db.sqlite and
  rebuild fresh. `db.py` must NOT use `ALTER TABLE` migration blocks for
  schema changes right now — add new columns directly into the
  `CREATE TABLE IF NOT EXISTS` statements instead. (A past ALTER-TABLE
  migration was placed in the wrong order — after the table's own
  `CREATE TABLE` — and silently no-opped on fresh installs, costing a
  long multi-turn debugging detour before being caught. Don't reintroduce
  migrations until much closer to a real public release with other
  users' data worth preserving.)
- Always read the actual current source file before proposing code for
  it. Don't rely on this plan doc as source of truth.
- Chat replies in Thai. Code — including comments and UI strings — in
  English only.
- For multi-part fixes in one reply, use `###` headers to separate each
  fix point.
- `log.info()` used sparingly — only for output genuinely needed for
  debugging.
- Flag EXPERIMENTAL for anything touching a lexicon/endpoint NVSky
  hasn't exercised before, with a note to paste back the traceback (or
  ideally the FULL traceback via `log.error(traceback.format_exc())`,
  not just `str(e)` — a bare exception message cost a very long detour
  this session before a full traceback finally pinpointed the real
  failing line) if it errors.
- For anything touching overall window/UI **structure**, restate the
  intended shape back to the user in plain terms before writing code.
  This project has burned real time twice on structural mismatches
  (NotificationsWindow built as a separate Dialog when the goal was a
  tab; Chat first built as tab-per-conversation when the goal was one
  tree+list tab) — always confirm shape explicitly, don't infer it.
- When genuinely stuck on a bug that doesn't reproduce locally (e.g. an
  SDK/pydantic quirk), the installed `atproto` package can be
  pip-inspected directly (`pip install atproto --break-system-packages`,
  then `python3 -c "from atproto_client... import inspect..."`) to check
  real model field names, aliases, and method signatures rather than
  guessing from docs pages that don't render their schemas. This found
  several real bugs this session (bookmark field names, chat.bsky.convo.*
  field names, a couple of genuine SDK serialization bugs — see below).

## Confirmed multi-tab architecture (built and working)

- **`MainWindow(wx.Frame)`** holds a plain **`wx.Notebook`** (NOT
  `wx.aui.AuiNotebook` — the AUI notebook has broken per-tab
  accessibility, NVDA read tab names concatenated together; plain
  `wx.Notebook` reads cleanly, "Home tab selected" etc.). Everything
  (toolbar + notebook) is wrapped in one `wx.Panel` parented to the
  Frame — `wx.Frame` does NOT apply dialog-style Tab-key traversal
  across its direct children the way `wx.Panel`/`wx.Dialog` do, so
  without this wrapper Tab could never reach the toolbar.
- **Persistent toolbar** (in `MainWindow`, shared across every tab):
  Check for updates (F5 fallback), New post (Ctrl+N fallback), Settings
  (opens NVDA Settings to NVSky's category via
  `gui.mainFrame.popupSettingsDialog`), Remove current tab (Ctrl+W —
  hides itself via `_updateRemoveTabButton()` when the current tab isn't
  removable), Close (Escape/Alt+F4 — closes the whole MainWindow).
- **Close vs Remove terminology, final and consistent everywhere:**
  "Close" = closing the whole `MainWindow` (Escape/Alt+F4/toolbar Close
  button). "Remove" = Ctrl+W taking the current TAB out of the notebook
  — no-op on permanent tabs, never called "close" anywhere in code/UI to
  avoid the confusion that cost a full round of back-and-forth earlier.
- **Ctrl+1-9** jumps to tab N by position (`MainWindow.onCharHook`).
- Opened via NVDA+Alt+B (`script_openFeed` in `__init__.py`), which
  creates `MainWindow` and adds each permanent tab in order via
  `addTab(panel, label, select=..., removable=False)`.
- **Permanent tabs** (Ctrl+W no-ops): Home, Notifications, Saved, Chat.
  Future primary sections (Explore, Feeds, **Lists — next up**) are also
  permanent tabs, not closable.
- **Removable tabs** (opened from an item's action menu, Ctrl+W takes
  them out): View Thread, User Timeline, Followers/Following list — NOT
  YET converted from their old `wx.Dialog` form to tab-panel form (this
  conversion never actually got done during the Home/Notifications push
  — see backlog below). Chat's "Open in new tab..." pop-out
  (`ConvoTabWindow`) is the one removable tab that IS done.
- **Per-tab focus/title mechanics** (`feedWindow.py`'s `FeedListMixin`):
  - `_restoreFocusPosition(moveFocus=True)` — restores saved
    list-position/selection always; only grabs REAL OS/screen-reader
    focus when `moveFocus=True`. Tab panels call it with
    `moveFocus=False` in their own `__init__` (a panel may be
    constructed before it's even the tab meant to be visible — grabbing
    real focus there caused two tabs' content to get announced
    back-to-back on open). `MainWindow.addTab()`'s `wx.CallAfter` grants
    real focus once, correctly, after notebook layout settles; a tab's
    own `onTabActivated()` (called by `MainWindow.onPageChanged`) also
    calls it with default `moveFocus=True` when regaining focus via
    tab-switch, AND explicitly speaks `"{TAB_NAME} tab"` first —
    `wx.Notebook`'s own tab-selected speech was losing the race against
    the focus change.
  - `_updateTitle()` — sets the notebook tab label to just `TAB_NAME`
    (short), and pushes the fuller `"{TAB_NAME} - NVSky - {handle}"`
    onto the **shared MainWindow title bar** only while that tab is the
    currently active one. Uses `notebook.FindPage(self)` (wx.Notebook;
    NOT `GetPageIndex`, that's AuiNotebook-only).
  - `_updateActionButtons()` — hides Post/User action buttons entirely
    when the tab's list is empty (`self._posts` is empty), rather than
    leaving a visible button that just replies "No post selected."
  - `_getSelectedPosts()` lives in `FeedListMixin` (shared) — was
    originally FeedWindow-only, moved during this session after
    NotificationsWindow's bulk actions hit an AttributeError.
- **`ItemActionMixin`** (Post action menu, Alt+A, shared by FeedWindow /
  NotificationsWindow / SavedWindow): host class supplies
  `_getActionablePost()` — FeedWindow/SavedWindow return the focused
  item directly; NotificationsWindow resolves via a `subject_uri` column
  (see Notifications below). `_showBulkPostActionMenu` differs per host
  where bulk semantics differ (Notifications' bulk mark read/unread
  marks the NOTIFICATIONS themselves via a dedicated
  `_markSelectedNotificationsRead`, not the underlying posts).

## Tab-by-tab status

### Home — DONE
Following (`get_timeline`) and Discover (Bluesky's real "whats-hot" feed
generator, `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`
via `app.bsky.feed.getFeed`) both wired and working. "For You" was
**deliberately dropped** — confirmed (both via API research and the
user's own live test) that it's not a Bluesky system feed; it was a
specific pinned third-party custom feed that Bluesky has since sunset
("This feed is no longer online"). `filterRadio` now has only 2 options.

### Notifications — DONE
Full parity with Home: Post action (Alt+A) works on notification items
via `subject_uri` (added to the `notifications` table — for
reply/mention/quote it's the notification's own post; for like/repost
it's `reasonSubject`; follow has none). `resolve_posts`/`sync_notifications`
fully hydrate every notification's actionable post into the local
`posts` cache (not just display text) so Post action has real data
(cid, viewer like/repost/bookmark state). `app.bsky.notification.updateSeen`
is called after every sync so the unread badge in the official app/other
clients clears too, not just NVSky's local `is_read`. Bulk select-all →
mark read/unread works via the dedicated
`_markSelectedNotificationsRead` (marks the notifications, not their
underlying posts — bulk-liking/replying to N notifications' posts at
once was never coherent, so Post action's bulk menu stays single-item
only, same as before).

### Saved — DONE
Backed by `app.bsky.bookmark.getBookmarks` (response field is `.item`
for the full hydrated post — NOT `.subject`, which is just a bare
strongRef with no author; confirmed from a real error log). No filter,
no unread tracking (`_tracksUnread=False` — a saved-posts list is a
personal reference list, not a stream to catch up on).
Unsaving a post (from ANY tab's Post action menu, via the shared
`_toggleBookmark`) removes the row from Saved's list immediately if
currently viewing Saved (`_onBookmarkChanged` hook, default no-op for
Home/Notifications) AND deletes the local `feed_items` row
(`db.delete_feed_item`) so it doesn't come back on next sync — the
original bug was `feed_items` rows never getting cleaned up locally even
though the server-side unbookmark worked correctly.

### Chat (DM) — DONE (v1), several rounds of real bugs found and fixed
**Structure** (confirmed after two revisions — do not redesign again
without explicit re-confirmation): ONE permanent "Chat" tab —
`wx.TreeCtrl` (flat list of conversations, requests always sorted to the
top with a "(request) " label prefix) + `wx.ListCtrl` (messages of
whichever conversation is selected in the tree) side by side, like
YoutubePlus's category-list pattern. F5 syncs the conversation list AND
every conversation's full message history in one pass — expanding/
selecting a conversation must be instant from local cache, never a
per-select network round-trip. Compose box + Send button at the bottom
(Enter or Ctrl+Enter) send to whichever conversation is selected.
"Open in new tab..." on a conversation's context menu pops it out into
its own removable `ConvoTabWindow` tab (title = the other person's
name) — opt-in only, not default. Message requests (status="request")
get dedicated **visible** Accept/Decline buttons (not menu items — the
user wants primary actions like this to always have a visible button,
context-menu/Application-key access alone isn't enough); accepted
conversations get a visible "Message options..." button alongside the
right-click/Menu-key menu (same principle — this is likely to apply to
other tabs' primary action too, not yet decided which).

**Reply** works: Post action-style "Reply" on a message sets a
"Replying to: ..." indicator + Cancel button, includes
`reply_to_message_id` in `send_message`. Response-side `MessageView.reply_to`
embeds the **entire original message** (id + text directly), NOT just a
bare ID like the request side (`MessageInput.reply_to` = `{messageId}`
only) — different shape each direction, both fields (`reply_to_message_id`,
`reply_to_text`) are stored locally so the reply preview always has text
without needing a lookup. Left/Right on a message list jump to/from the
replied-to message; Right also does a **forward search** (find any
message whose `reply_to_message_id` equals the currently focused
message) when there's no pending "jump back" target, so starting from an
original message can navigate forward to whatever replied to it, not
just backtrack a prior Left jump.

**Real SDK bugs found and worked around this session (important for any
future chat.bsky.* work):**
1. `dm.get_messages()`'s typed response parsing throws
   `PydanticUserError: union_tag_not_found` on some real responses (a
   discriminated-union resolution bug on `chat.bsky.convo.defs#messageView`).
   Fixed by calling `dm._client.invoke_query(...)` directly and parsing
   `response.content` as a raw dict instead of the typed Response model
   — same principle `_fetch_thread_json` already used elsewhere in
   `client.py` for a near-identical bug on `getPostThread`, just adapted
   for an authenticated (not public) endpoint.
2. `dm.send_message()`'s RESPONSE parsing hits the same MessageView bug
   (its return type is also MessageView) — fixed the same way, call
   `invoke_procedure` directly and ignore the echoed-back message (the
   caller already re-syncs after a successful send anyway).
3. Separately, the REQUEST body for `send_message` threw
   `PydanticSerializationError: Unable to serialize unknown type: FieldInfo`
   inside `model_dump_json()` — did NOT reproduce against a freshly
   pip-installed copy of the exact same `atproto`==0.0.69 / `pydantic`==2.13.4
   versions, so this looked like an environment-specific issue. **Root
   cause confirmed by the user**: a "dependency ghost" — another add-on
   (Gemini-related) bundles its OWN older `pydantic` under its own
   `lib/`, loads AFTER NVSky at NVDA startup, and was shadowing NVSky's
   bundled pydantic via `sys.path` order, creating a mismatched
   pydantic/pydantic_core pair at runtime despite `pydantic.VERSION`
   itself reading identically. User fixed it on their end (removed the
   other add-on's own pydantic, forcing everything onto NVSky's copy).
   **The workaround is kept anyway** — send the request body as a
   `DotDict` (from `atproto_client.models.dot_dict`) instead of a typed
   `Data` model; `DotDict` serializes through `get_model_as_dict()` +
   `to_json()`, a completely different code path that never touches
   `pydantic_core`'s `model_dump_json()` at all. This protects any OTHER
   user who might hit the same cross-add-on dependency conflict, costs
   nothing, and should not be reverted.
4. **Packaging implication for later**: `requirements.txt` should pin
   `atproto` to an EXACT version (`atproto==0.0.69`, or whatever's
   current when this is set up) — not a range — specifically because of
   #3 above; reproducible builds matter a lot for an accessibility tool
   where an unexpected dependency-version drift could silently break
   things for blind users. No need to enumerate sub-dependencies
   individually — pip resolves those from `atproto`'s own declared
   requirements.

**Still open / explicitly deferred, in the order the user wants them
picked up:**
1. **Chat settings → fold into the main Settings dialog**, NOT a
   button/dialog inside the Chat tab itself. New idea from this
   session's end: add a "Chat" category/tab to the existing NVDA
   Settings panel (`settings.py`) alongside Accounts/General/Display/
   Sound/Profile. Confirmed-available via SDK (**must fetch current
   real values every time the dialog opens, exactly like the existing
   Edit-who-can-reply dialog does — re-verify that pattern in
   `_editReplyPermissions`/`_onReplyPermissionsLoaded` in `feedWindow.py`
   before building this, per the user's explicit reminder that settings
   shown locally must always reflect the server's real current state,
   not a stale local cache):
   - "Allow direct messages from" (Everyone/Following/No one) +
     "Allow group chat invites from" (same 3 choices) — these are
     fields `allowIncoming`/`allowGroupInvites` on the
     `chat.bsky.actor.declaration` **record** (read/write via
     `com.atproto.repo.getRecord`/`putRecord`, NOT a dedicated
     procedure — confirmed via `Record.model_fields` on
     `atproto_client.models.chat.bsky.actor.declaration`).
   - "Mark all chats as read" / "Mark all requests as read" —
     `chat.bsky.convo.updateAllRead(status="accepted"|"request")`
     (confirmed field: `Data.model_fields == ['status']`).
   - Notification settings for new messages / new message requests —
     `chat.bsky.notification.putPreferences(chat=ChatPreference(include=,
     push=), chat_request=ChatPreference(...))` — `ChatPreference` has
     `include: 'all'|'follows'` and `push: bool`.
   - Export chat data — `chat.bsky.actor.exportAccountData` exists;
     response shape (file? stream?) NOT YET confirmed, lowest priority.
2. **Emoji message reactions** — deliberately deferred as a nice-to-have,
   not core. Confirmed feasible if wanted later:
   `chat.bsky.convo.addReaction`/`removeReaction`, Data =
   `{convo_id, message_id, value}` where `value` is a plain string
   (1-64 chars, presumably a single emoji).
3. `ConvoTabWindow` (the pop-out tab) doesn't have the message-options
   button/reply-jump parity that `ChatWindow` got in the same session —
   it DOES have Reply/Copy/Delete via context menu and Ctrl+Enter, just
   not the newer visible "Message options" button or Left/Right jump.
   Low priority since it's an opt-in secondary view.
4. Message-level own read-tracking + focus-first-unread — floated by the
   user as an idea (chat apps' default of always focusing the newest
   message might not be ideal for a busy conversation), explicitly
   deferred, not decided.
5. Clear-cache (Ctrl+Delete equivalent) — Settings > General already has
   a "clear cache" feature but it was found to only clear the Home feed,
   not Notifications/Saved/Chat. Needs the actual `settings.py` source
   read (never inspected yet this whole session) before fixing.

### Explore, Feeds — NOT STARTED
Purpose/scope still undecided by the user (unclear what these would even
show that Home's Following/Discover doesn't already cover). Do not start
without asking first.

### Lists — NOT STARTED, but confirmed feasible, **user said the next chat should build this**
SDK research already done this session, ready to build directly:
- `app.bsky.graph.list` — the list record itself (name, description,
  `purpose`: modlist vs curatelist).
- `app.bsky.graph.listitem` — a separate record per member, references
  the list record.
- Create/delete both via plain `com.atproto.repo.createRecord`/
  `deleteRecord` (same pattern as posts/likes/follows elsewhere in this
  codebase) — no dedicated procedure needed.
- `app.bsky.graph.getLists` — read all of an actor's lists, filterable
  by `purpose`.
- `app.bsky.graph.getList` — read one list's full details + hydrated
  member list.
- Also exists: `muteActorList`/`unmuteActorList` for moderation-purpose
  lists, and (if `purpose=curatelist`) a curation list can be browsed as
  a feed via `app.bsky.feed.getListFeed` (name unconfirmed — verify via
  pip-inspection before use, wasn't double-checked this session).

**Proposed UI** (from user's own description, not yet built or
re-confirmed — restate and confirm shape before coding, per the
standing rule above): list-of-lists you can expand, same
category-tree-then-detail-list pattern as Chat (`wx.TreeCtrl` for the
list-of-lists, `wx.ListCtrl` for a selected list's members), plus
Add/Remove buttons for managing membership. Given Chat's tree+list
pattern is now proven and working well, Lists should very likely reuse
the same structural approach rather than reinvent one — but confirm
with the user before assuming.

### Saved-feeds / pinned custom feeds — NOT STARTED, NOT in the original tab inventory
Came up implicitly during the Discover/For-You research (a "For You"
custom feed some users pin in the official app) — no tab planned for
this yet, but noting it exists as a concept (`savedFeedsPref`/
`savedFeedsPrefV2` in `app.bsky.actor.getPreferences`) in case it comes
up again.

### Profile — folded into Settings already, no separate tab planned.

### General Settings — dialog already exists (`settings.py`, never
directly inspected by Claude this session — do that before touching it).
Confirmed extra things the SDK can pull that the current dialog might
not yet expose: saved/pinned feeds, thread-view sort-order default,
per-labeler content-label preferences, adult-content toggle (all via
`app.bsky.actor.getPreferences`/`putPreferences`), plus the Chat
settings listed above.

## Remaining backlog carried over from earlier phases (still not started)
- View Thread / User Timeline / Followers-Following list — still
  separate `wx.Dialog`s, never actually converted to removable tabs
  despite being planned for the multi-tab push from the very start.
- Ctrl+F5 (check every open tab), Ctrl+Delete (clear current tab's
  cache) — `MainWindow.checkAllOpenTabs()` exists and works; per-tab
  cache-clear does not exist anywhere yet.
- Sound system, background fetch scheduler, i18n pass — not started.
- Video attachment support in Compose — open question, not confirmed
  either way.

``n
## File: plan-06.md
`md
# NVSky — plan-06.md

Handoff doc from a chat that ran too long (context got too big to track
file state reliably — several patches failed because Claude guessed at
file contents instead of reading them fresh). Starting a new chat.

## Process reminder for the new chat (agreed with the user)

- **Read actual current source files fresh at the start of the new
  chat** (paste in or upload) before proposing any code. Never guess
  file contents from memory/earlier context, even within the same
  conversation, once it's gotten long — re-read before editing.
- Diff format: `old_str:` / `new_str:` blocks, applied via the user's
  Notepad++ script — this is the fast/preferred path for the user when
  it's precise.
- For anything sizable (heavily-edited method, brand new method), send
  the **whole method** instead of a diff — less risky than a long
  old_str that has to match exactly.
- Whichever of the two costs Claude fewer tokens to produce accurately
  is fine — full-method paste is often actually cheaper/safer than a
  fragile long diff, so default to that when a change touches more
  than a few contiguous lines.
- When multiple edit locations exist, number sub-headings clearly
  (`##### 1.1`, `##### 1.2`, ...) under a file heading — the user
  navigates by these to track what's applied.
- If unsure whether a specific method/file state matches what's being
  patched, **ask for just that method pasted back** (not the whole
  file) rather than guessing — cheaper than a failed patch loop, and
  much cheaper than a whole-file re-upload.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. Structure mirrors YoutubePlus/MessengerAccess (same
developer's other add-ons). Multi-tab `MainWindow` (`wx.Notebook`)
holding Home, Notifications, Saved, Chat, and (new this session) Lists
as permanent tabs, plus dynamically-opened removable tabs (per-list
timelines, per-conversation chat pop-outs).

## Completed this session

- **Lists tab** (`globalPlugins/NVSky/feedWindow.py`): `ListsWindow`
  (tree of the account's lists + curation-list timeline / moderation-
  list member view depending on which list is selected),
  `ListTabWindow` (a single curation list popped into its own
  removable tab, persisted across restarts via `db.get_open_temp_tabs`/
  `add_open_temp_tab`/`remove_open_temp_tab`), `AddListDialog`,
  `ManageMembersDialog` (add/remove members, typeahead user search via
  a `CustomCheckListBox`), `SubscribeListDialog` ("Find lists by
  user" — search a user, browse their public lists, open curation
  lists as tabs directly or mute/block moderation lists). New
  `client.py` functions: `get_lists`, `get_list`, `sync_list_feed`,
  `create_list`, `delete_list`, `add_list_member`, `remove_list_member`,
  `mute_actor_list`/`unmute_actor_list`, `block_actor_list`/
  `unblock_actor_list`, `resolve_list_uri`, `search_actors_typeahead`.
  New `db.py` table `lists` + `upsert_list`/`get_lists`/`delete_list`,
  plus `get_open_temp_tabs`/`add_open_temp_tab`/`remove_open_temp_tab`
  (JSON blob per account in `ui_state`, key `open_temp_tabs:<account_id>`).
  `mainWindow.py`'s `removeCurrentTab` now calls `panel.onTabRemoved()`
  if present before `DeletePage()`.
- **Timezone + time-format bug** (`timeutils.py`, new shared module):
  fixed absolute/custom time display showing UTC instead of local time
  across every tab; centralized `format_timestamp`/
  `current_mode_and_pattern` so Chat and the feed tabs use the same
  logic instead of Chat having its own broken one-off formatter.
- **Grapheme counting bug** (`client.py`: `count_graphemes`): Python's
  `len()` overcounts Thai text substantially (confirmed against
  bsky.app side-by-side: 1704 real graphemes vs `len()`'s 1887 on the
  same message) because Thai combining vowels/tone marks are separate
  codepoints that count as ONE grapheme with their base consonant.
  Fixed in both post compose (`compose.py`) and Chat's char-limit
  display/validation.
- **Alt+1 through Alt+9 shortcut** — reads the Nth-newest item's full
  row (all columns, via `GetItemText`) aloud without moving focus.
  Works from anywhere including while typing in Chat's compose box.
  Wired into every `FeedListMixin` host (`FeedWindow`,
  `NotificationsWindow`, `SavedWindow`, `ListsWindow`'s curation-list
  view, `ListTabWindow`) via a shared `_announceNthNewestPost`, and
  separately into `ChatWindow`/`ConvoTabWindow` via
  `_announceNthNewestMessage`. Respects the `sort_order` setting
  (newest/oldest) automatically for the feed tabs (they already sort
  `self._posts` before this reads it); Chat needed its own explicit
  fix, see below.
- **Periodic time-column refresh** — a `wx.Timer` (30s interval, no
  network/DB call, just re-formats the already-in-memory timestamp)
  keeps relative time labels ("5 minutes ago") from going stale while
  a tab is left open. Wired into `FeedListMixin._initFeedListState`
  (covers every feed tab automatically) and separately into
  `ChatWindow`/`ConvoTabWindow`. **Bug fixed along the way**: the timer
  must bind to `self` (the panel), not `self.postList` — `FeedWindow`
  calls `_initFeedListState()` before `postList` exists yet, so binding
  to the widget crashed with `AttributeError` on open.
- **Chat optimistic send** — `onSend` in both `ChatWindow` and
  `ConvoTabWindow` now inserts the message into the visible list AND
  `self._currentMessages` (the cache Alt+number reads) synchronously,
  *then* speaks "Message sent" after a short `wx.CallLater(250, ...)`
  delay (was speaking instantly, before the row even rendered, and
  before this fix Alt+number right after sending read stale data
  because only the widget was updated, not the cache). The real send
  still happens on a background thread; `send_message`'s own echoed-
  back response can't be trusted (SDK bug, see the note already in
  `client.py` on `send_message` itself), so a background resync
  (`sync_convo_messages`) silently swaps the placeholder row for the
  real one once it lands. Confirmed working by the user.
- **Chat message sort order (`sort_order` setting)** — `ChatWindow.
  _showMessages` and `ConvoTabWindow._loadMessages` now reverse the
  (always oldest-first-from-DB) message list when the setting isn't
  `"oldest_first"`, store the result in `self._currentMessages` /
  `self._messagesNewestFirst`, and every other method that used to
  re-query the DB and index directly (`onMessageContextMenu`,
  `_jumpToRepliedMessage`, `_jumpBackToReply` in both classes) was
  switched to read `self._currentMessages` instead, so row index always
  matches what's actually displayed regardless of sort direction.
  **Given to the user in the last message but not yet confirmed
  applied/tested** — check this first in the new chat.
- **Empty-state UI hiding + focus-restore-on-close + remove
  confirmations** for the Lists tab's dialogs (`ManageMembersDialog`,
  `SubscribeListDialog`) and `ListsWindow.onRemoveList` — confirmed
  working by the user.

## Outstanding / not yet done

1. **Chat message reactions (emoji reacts) — not displayed at all.**
   Confirmed via web search that the AT Protocol chat lexicon really
   does support this: `chat.bsky.convo.addReaction`/`removeReaction`
   procedures exist, and `chat.bsky.convo.defs` has `#reactionView`/
   `#reactionViewSender`/`#messageAndReactionView` — so a message view
   should carry a `reactions` field. Exact field names NOT yet
   confirmed against the installed SDK (pip-inspect before trusting).
   Already drafted (given to the user, not yet applied):
   - `_describe_reactions(reactions_json)` formatter function for
     `chatWindow.py` (dedupes by emoji, shows `emoji×N` for repeats).
   - A 4th "Reactions" column added to both `ChatWindow` and
     `ConvoTabWindow`'s `messageList` (`InsertColumn` — note this line
     is IDENTICAL text in both classes, a diff must apply to both
     occurrences).
   - Both `_showMessages` and `_loadMessages` (already rewritten this
     session for sort-order, see above) call
     `self.messageList.SetItem(i, 3, _describe_reactions(message.get("reactions_json")))`.
   **Still needed, blocked on seeing real source:**
   - `db.py`: add a `reactions_json TEXT` column to the `messages`
     table's `CREATE TABLE` (remember: no `ALTER TABLE` migrations
     during solo-dev beta — add directly to the `CREATE TABLE`
     statement per the project's standing rule).
   - `client.py`: find the actual message-sync function (likely named
     `sync_convo_messages`, stores each message via something like
     `db.upsert_message`) and extend it to pull reaction data off each
     message view into `reactions_json` before storing. **Never seen
     this function's real content in this chat — read it fresh before
     touching it.**
   - Decide/build the add/remove-reaction UI itself (a menu item on
     the message context menu, presumably) — not designed yet, only
     the read/display side has been drafted.

2. **MainWindow speaks the tab name immediately when a tab is
   *created*, not just when the user actually switches to it.**
   Diagnosis (not yet confirmed against real code, needs a fresh read
   of `mainWindow.py`): `addTab()` likely calls `onTabActivated()` (or
   equivalent) unconditionally right after adding the page, instead of
   only in response to `EVT_NOTEBOOK_PAGE_CHANGED` firing from an
   actual user-driven selection change. Needs a proper fix distinguishing
   "tab was just constructed" from "user switched to this tab" —
   **read `mainWindow.py`'s current `addTab`/`_focusPanel` and whatever
   binds `EVT_NOTEBOOK_PAGE_CHANGED` fresh before patching.**

3. **Chat: message read-status handling.** Reported as "still shows
   unread even after scrolling through and reading it" plus "Space to
   jump to next unread doesn't work." **Needs clarification from the
   user first** (was asked, not yet answered): does this mean —
   - (a) conversation-level unread (the indicator in the conversation
     tree not clearing after opening/reading a convo), or
   - (b) message-level read-tracking + Space-jump-to-next-unread, the
     same feature `FeedListMixin` already has for Home/Notifications/
     Saved (`db.mark_post_read`, Space-jump navigation) — which Chat
     may not have an equivalent of at all yet?
   These are very different sizes of work — resolve which one before
   estimating/building.

## Immediate next steps for the new chat

1. Paste in current `chatWindow.py` and confirm the sort-order rewrite
   (item above, "given but not yet confirmed applied") actually landed
   and works, including Alt+number now reading the correct side.
2. Get the user's answer on the unread-status question (#3 above).
3. Paste in current `mainWindow.py` to fix the tab-announce-on-create
   bug (#2 above).
4. Paste in current `client.py`'s message-sync function + `db.py`'s
   `messages` table schema to finish reactions (#1 above).

``n
## File: plan-07.md
`md
# NVSky — plan-07.md

Handoff doc for a new chat. Same process rules as plan-06.md, still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code — but per the standing rule
  adopted mid-way through the last session: once a round of patches is
  confirmed applied and tested, treat that as reflected in the user's
  real source and keep working from that state — don't ask for a full
  re-upload every round. Only ask for a fresh full-file paste, or just
  the specific method/section, if a patch is reported as failing or
  landing in the wrong place.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) — applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines before/after
  the change, must match exact whitespace, stays as one block for one
  contiguous change (don't split a single change into multiple tiny
  blocks).
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff — a full-file rewrite was already done once
  for chatWindow.py's refactor and worked well.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated (this bit us twice already — a misplaced `_reactToMessage`
  and having to hand-fix method placement once). When in doubt about
  which of two near-identical blocks matched, or a patch is reported as
  landing wrong, ask for that one method pasted back rather than
  guessing again.
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them — this has caused
  multiple real SyntaxError/NameError crashes (including two NVDA
  force-restarts) this session. Check line-join points carefully before
  sending.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle (explicitly generalized by the user):
  the optimistic-UI-then-background-network pattern (update UI/local
  state immediately, do the real network call on a background thread,
  reconcile silently after) should be applied to every activity across
  the whole add-on, not just chat send.
- When using an SDK method never exercised against a real server before
  in this project, flag it plainly as LOW CONFIDENCE and ask the user
  to paste back a traceback or raw response if it doesn't work, rather
  than presenting a guess as certain.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`) holds Home, Notifications,
Saved, Chat, and Lists as permanent tabs, plus dynamically-opened
removable tabs (per-list timelines, per-conversation chat pop-outs).

## Completed since plan-06.md (this past session — very large)

**Tab-identity / persistence system** (`mainWindow.py`, `db.py`,
`__init__.py`): generalized "last active tab" and "remember this tab on
restart" to work for temp tabs too (`TAB_TEMP_TYPE`/`TAB_TEMP_KEY` on
`ConvoTabWindow`/`ListTabWindow`, matching `db.get_open_temp_tabs()`'s
`type`/`key` fields), and made tab rename actually persist across
sessions (`db.set_temp_tab_custom_name`, `onTabRenamed` hook called from
`MainWindow.renameCurrentTab`) — it had only ever been an in-memory
`TAB_NAME` assignment before, no db write at all.

**Chat message-level read tracking**: first attempt (a local-only
`is_read` column guessed at insert time) was wrong — drifted from the
server's real state, defaulted every pre-existing historical message to
unread. Corrected design: `db.reconcile_message_read_state(account_id,
convo_id, unread_count)` re-derives the local `is_read` boundary from
the server's authoritative per-conversation `unread_count` on every
sync (newest N messages = unread), and `client.mark_message_read`
pushes individual reads back to the server in the background via
`chat.bsky.convo.updateRead`'s `messageId` param (LOW CONFIDENCE, never
confirmed against a real server response). Space jumps to next unread,
Alt+number also marks read (extended to ALL `FeedListMixin` tabs too,
not just chat — Home/Notifications/Saved/Lists/ListTab).

**Chat message reactions**: display (`reactions_json` column + `Reactions`
column, moved to be the FIRST column — same fix applied to the `Embed`
column across every `FeedListMixin` host), react/unreact via a message
context menu item (validates exactly-1-grapheme before calling the API,
after hitting a real `InvalidRequest` from the server), and a real emoji
picker (`_pick_emoji`/`_COMMON_EMOJI`, a `wx.SingleChoiceDialog` list of
common emoji + "Custom..." fallback — NOT a bare text box, which was
flagged as useless). `client.add_reaction`/`remove_reaction` are LOW
CONFIDENCE (first-ever use).

**New chat flow**: `NewChatDialog` (typeahead user search, same pattern
as `SubscribeListDialog`) + a required first-message field — discovered
the hard way that `getConvoForMembers` alone doesn't create anything
visible until an actual message is sent to the resolved convo id.
`client.get_or_create_convo_for_member` had to be rewritten to bypass
the typed SDK response (same class of pydantic union-discriminator bug
hit elsewhere in this project) after it silently returned nothing with
no exception at all.

**A real crash** (NVDA force-restarted twice): `RuntimeError: wrapped
C/C++ object of type TreeCtrl has been deleted` in
`ChatWindow.onConvoSelected`. Root cause never fully pinned down —
`MainWindow._openChatConvo`'s double tree-reload race was fixed (now
suppresses `onTabActivated` during its own programmatic tab switch,
using the existing `_activationSuppressed` flag) but crashes continued
after that alone, so a defensive 3-layer guard was added directly in
`onConvoSelected` too (attribute check + `try/except
RuntimeError/AttributeError` + `finally: evt.Skip()`) on a Gemini
suggestion. This turns a crash into a silent no-op — **the actual root
cause is still not confirmed**, only papered over safely. Watch for
"selected a conversation, nothing happened" (no crash, just silently
swallowed) as a sign the underlying issue is still live.

**chatWindow.py full refactor** (delivered as a whole-file rewrite, not
a diff): `_ChatMessagePanelMixin` now owns everything both `ChatWindow`
and `ConvoTabWindow` need for "the currently displayed conversation's
message list" — reactions, Alt+number, Space-jump, reply/copy/delete,
the emoji picker, sending (optimistic UI), all keyboard shortcuts.
Fixed `_deleteMessageForSelf`/`onMessageContextMenu` to a single shared
signature (root-caused the earlier `_reactToMessage` argument-count
crash). Side effect: `ConvoTabWindow` gained Left/Right reply-jump (it
never had it before) and its Alt+number now reads all 4 columns like
`ChatWindow` (previously just "From: text").

**F5 / Shift+F5 / Ctrl+F5 scheme, finalized** (chat-specific, after a
couple of wrong turns):
- F5 = refresh whatever's the narrowest current context (selected
  conversation in Chat; a feed tab's own page in feedWindow.py hosts)
- Shift+F5 = full refresh of the current tab's whole scope (all
  conversations in Chat via `onCheckAllConvos`; "fetch older posts" in
  feed tabs — inapplicable/no-op consideration was WRONG for chat,
  corrected to mean "all chats")
- Ctrl+F5 = every open tab, app-wide, via `MainWindow.checkAllOpenTabs`

`checkAllOpenTabs` was rewritten from "loop calling each panel's full
`onCheckForUpdates` concurrently" (caused real `re-login failed` errors
— concurrent calls into `client.get_client_for_active_account()` racing
on session/token refresh — and spoke each tab's own "no new X" message
back-to-back, plus jerked focus around background tabs) to: one shared
background thread calling a new `_syncForBulkCheck(atprotoClient) ->
bool` hook on each panel sequentially (pure network+DB, no wx calls),
one final spoken summary, and only the currently-VISIBLE tab actually
re-renders (`_reloadAfterBulkCheck()`) — every other tab's local cache
is fresh and picks it up naturally next time it's activated. This hook
pair now needs to exist on every checkable panel: `FeedListMixin`
(covers Home/Saved/ListTab/Notifications), `ListsWindow` (its own
override, curate vs mod list), `ChatWindow`, `ConvoTabWindow`.

Also fixed while touching this: every `feedWindow.py` host's
`onCharHook` intercepted Ctrl+F5 too (no `ControlDown()` exclusion),
silently eating it before it could bubble up to `checkAllOpenTabs` —
patched in Home/Saved/Lists/ListTab/Notifications (5 near-identical
`onCharHook` methods, sent as full-method replacements since the
duplication made precise diffs risky).

**Alt+number now moves real ListCtrl position** (`Focus`/`Select`/
`EnsureVisible`) so arrowing afterward continues from that item — but
deliberately does NOT call `SetFocus()`, so a chat compose box (or
wherever the user's real keyboard focus was) never gets stolen. Two
wrong iterations before landing here: first attempt moved real focus
too (broke "keep typing while checking a message" — the whole reason
this shortcut exists); the fix-of-the-fix over-corrected and reverted
Chat's version entirely.

**NVDA reading a ListCtrl item twice on Ctrl+Tab/Ctrl+number tab
switch** (confirmed universal — every ListCtrl-based tab, not just
chat; TreeCtrl-based tabs never doubled; switching via the tab strip
itself never doubled either). First attempted fix (reordering
`SetFocus()` vs `Focus()/Select()` inside `ConvoTabWindow._loadMessages`)
had zero effect, meaning the real cause is outside chatWindow.py
entirely. Current fix: `MainWindow` binds `EVT_NOTEBOOK_PAGE_CHANGING`
(fires before the swap, unlike `PAGE_CHANGED`) to move focus onto the
notebook itself first, theorized to prevent Windows' native
"focused-control-about-to-be-hidden" auto-reassignment from firing
alongside `onTabActivated()`'s own explicit focus call. **Not yet
re-confirmed fixed after the mainWindow.py patch landed** — last
status was "cleared" per the user but worth a specific re-check.

**Lists tab UI reorganized** to match Chat's pattern: `Remove
list`/`Show in new tab`/`Manage members...` moved into the list tree's
context menu (`onListContextMenu`, new `EVT_TREE_ITEM_MENU` binding);
`Add list...` moved to the shared toolbar's New-post button (becomes
"New list... (Ctrl+N)" while a Lists tab is active, dispatched via
`MainWindow.onPageChanged`/`onNewPost`'s existing tab-identity check,
same mechanism as Chat's "New chat..." swap) — the external
`addListButton`/`removeListButton`/`showInNewTabButton`/
`manageMembersButton` widgets are kept constructed but `.Hide()`'d
rather than removed, so their existing `.Bind()` calls don't need
touching. `Find lists by user...` moved into `MainWindow`'s own main
toolbar (`findListsButton`, shown only while Lists is active) rather
than staying in `ListsWindow`'s own row.

## Outstanding

1. **New structural bug — performance + security, not yet detailed.**
   The user has this queued for the NEW chat specifically (didn't want
   to describe it twice across two chats). **First thing to do in the
   new chat: ask for the actual details/repro before proposing
   anything.** Framed as "big, structural" by the user — treat as
   higher priority than any of the polish items below once described.
2. Re-confirm the Ctrl+Tab/Ctrl+number double-announcement fix
   (`EVT_NOTEBOOK_PAGE_CHANGING` in mainWindow.py) is actually holding
   up — last report was "cleared" but wasn't re-verified explicitly
   after the final round of fixes landed alongside other changes.
3. `feedWindow.py`'s `onCharHook` is still duplicated 5x (Home/Saved/
   Lists/ListTab/Notifications) — same class of risk that caused the
   chatWindow.py duplication bugs. Not urgent, but flagged as a
   worthwhile follow-up refactor once the structural bug (#1) is
   handled, mirroring the mixin extraction already done for
   `_insertRow`/`_buildFeedListColumns` and for chatWindow.py.
4. `onConvoSelected`'s TreeCtrl crash — the defensive guard stops NVDA
   from crashing, but the actual root cause of the tree being destroyed
   was never confirmed. Keep an eye out for "selected a conversation,
   nothing happened silently" as a sign it's still there underneath.
5. Nice-to-have, no urgency: `chat.bsky.convo.getLog` for incremental
   sync (pure performance, not user-visible); a dedicated Chat options
   settings page (no concrete need identified yet — push notifications
   were investigated and confirmed infeasible for a desktop add-on,
   `registerPush` only forwards to a self-hosted relay service); group
   chat (`chat.bsky.group.*`) is a wholly separate, unstarted namespace,
   skip unless actually wanted.

``n
## File: plan-08.md
`md
# NVSky — plan-08.md

Handoff doc for a new chat. Same process rules as plan-06.md/plan-07.md,
still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code — but per the standing rule adopted
  a few sessions back: once a round of patches is confirmed applied and
  tested, treat that as reflected in the user's real source and keep
  working from that state — don't ask for a full re-upload every round.
  Only ask for a fresh full-file paste, or just the specific
  method/section, if a patch is reported as failing or landing in the
  wrong place.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) — applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines before/after
  the change, must match exact whitespace, stays as one block for one
  contiguous change (don't split a single change into multiple tiny
  blocks). Always state the target filename clearly right before each
  block.
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated — when in doubt about which of two near-identical blocks
  matched, or a patch is reported as landing wrong, ask for that one
  method pasted back rather than guessing again. Same-name methods in
  the same file need extra disambiguating context in old_str (a
  preceding unique comment/line), not just the def line — this has come
  up before (e.g. `_onCheckForUpdatesDone` existing in both `ChatWindow`
  and `ConvoTabWindow` with identical signatures).
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them — has caused real
  SyntaxError/NameError crashes before, including NVDA force-restarts.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle: the optimistic-UI-then-background-
  network pattern (update UI/local state immediately, do the real
  network call on a background thread, reconcile silently after) should
  be applied to every activity across the whole add-on.
- When using an SDK method never exercised against a real server before
  in this project, flag it plainly as LOW CONFIDENCE and ask the user to
  paste back a traceback or raw response if it doesn't work, rather than
  presenting a guess as certain. Same standard applies to any wx/NVDA
  API mechanism being used for the first time in this project (e.g. the
  `wx.LC_VIRTUAL` work below) — flag confidence level honestly, verify
  with real testing rather than trusting theory alone (this project has
  been bitten by screen-reader-specific surprises that looked solid on
  paper before — see the Alt+number bug in "Completed" below).
- If a temporary debug patch (timing log, diagnostic log.info, etc.) is
  applied on the user's side, track that it's still present — a later
  whole-method patch to that same method needs to account for it or the
  old_str won't match. Ask "is the temporary log still in there?" before
  patching a method known to have one.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`) holds Home, Notifications,
Saved, Chat, and Lists as permanent tabs, plus dynamically-opened
removable tabs (per-list timelines, per-conversation chat pop-outs).

## Completed since plan-07.md

**Structural bug hardening pass (plan-07.md's whole focus) — fully
closed out and confirmed working:**

- **Crash** (`RuntimeError: wrapped C/C++ object of type TreeCtrl has
  been deleted`, force-restarted NVDA): root cause was `ListsWindow`
  unconditionally auto-syncing from the server on every `__init__`
  (unintentional — never meant to behave that way), racing against
  `MainWindow.onClose`'s immediate `self.Destroy()`. Fixed with a new
  shared `uiutil.py` module (`uiutil.safe_ui_callback` decorator —
  catches `RuntimeError` containing "has been deleted" from a destroyed
  wx object, turns it into a silent no-op, logs via a still-present
  temporary `log.info` for now) applied to all 43 `wx.CallAfter`-target
  completion-handler methods across `chatWindow.py`, `compose.py`,
  `feedWindow.py`, `mainWindow.py`, `settings.py`.
- **~700-800ms freeze on every MainWindow open** (confirmed via NVDA's
  own "Recovered from freeze" watchdog log): root cause was `db.py`'s
  `_connect()` opening/closing a brand-new SQLCipher connection on
  *every single query* (214 connects in one MainWindow open, ~0.82s
  total, confirmed via timing logs), compounded by an N+1 pattern where
  `_format_post_time()`/`_format_time()` independently re-queried the
  `time_format_mode` UI setting once per rendered row. Fixed with (a)
  `db.py` — one persistent connection reused per thread via
  `threading.local()`, `_connect()`'s call shape unchanged so nothing
  else in `db.py` needed touching; (b) `timeutils.py` — module-level
  in-memory cache for `(mode, pattern)`, invalidated only when
  `settings.py`'s DisplayPanel writes a new value. Freeze fully gone,
  confirmed by the user; Stage 4 (lazy tab construction) from the
  original plan is no longer needed and was dropped.

**New bug found and fixed after the above closed out:** Alt+number
(read Nth-newest post/message) started double-reading the row — root
cause was that moving the ListCtrl's real focused-item position
(`Focus()`/`Select()`) can trigger NVDA's own automatic announcement of
that row IF the control already had real OS focus, racing against the
add-on's own explicit `nvdaUi.message()` call. A first attempt using
delay + `speech.cancelSpeech()` (mirroring an existing `_announce_now`
pattern already used ~20 places in `feedWindow.py`) was rejected by the
user as fragile ms-tuning that still let a stray syllable through. The
user's own alternative was adopted instead and is confirmed fully
working: check `list.HasFocus()` and compare `GetFocusedItem()` before
vs after moving focus — only skip the add-on's own announcement when
the list already had real focus AND the index is actually changing (a
same-index repeat, e.g. pressing Alt+N again on the same row, is a
no-op state change that fires no accessible event, so NVDA would
otherwise stay silent). Refactored per a Gemini suggestion into a
single shared `uiutil.move_focus_and_check_announce(list_ctrl, index)`
helper (returns bool, lets callers lazily skip building the announced
string when not needed) used by both `ChatWindow._announceNthNewestMessage`
and the shared `FeedListMixin._announceNthNewestPost` (covers
Home/Saved/Lists/ListTab/Notifications in one place).

**Re-confirmed:** the Ctrl+Tab/Ctrl+number double-announcement fix
(`EVT_NOTEBOOK_PAGE_CHANGING` in `mainWindow.py`, from plan-07.md) is
holding up — user explicitly re-tested and confirmed. No longer an open
item.

## Current task — message/post text truncated at ~511 characters

**This is what the new chat should pick up first.**

- User noticed the Message column in Chat doesn't show the full text of
  long messages (Bluesky DMs allow up to 1000 chars). Confirmed via
  testing: copying the message via the existing `_copyMessageText()`
  action (which reads straight from the message dict, bypassing the
  ListCtrl entirely) comes out **complete** — the full text is intact
  both in the DB and in memory. The **ListCtrl column display itself**
  cuts off at **exactly 511 characters** every time, regardless of
  content.
- Ruled out: not a data-layer slice anywhere in the pipeline (checked
  `db.py`'s `messages` table schema and `upsert_message` — plain `TEXT`
  column, no truncation; checked `chatWindow.py`/`feedWindow.py`'s
  formatting code — no `[:n]` slicing on the main message/post text
  anywhere). Not a newline-handling issue either — added a shared
  `uiutil.single_line()` helper that replaces `\n`/`\r\n` with a plain
  space before `SetItem()`, applied to both `chatWindow.py`'s message
  text and `feedWindow.py`'s `_message_text()` (which had the identical
  latent bug for posts, just never surfaced since Bluesky posts cap at
  300 chars, under the 511 cutoff) — made **zero difference** to the
  511 cutoff, confirmed by the user. The number matches the classic
  signature of a fixed-size buffer (`WCHAR buffer[512]` → 511 usable
  characters + null terminator) somewhere in the Win32
  ListView/accessibility stack — exact layer unconfirmed, but the
  practical implication is clear: **the control cannot be relied on to
  store/return more than ~511 characters in a single cell.**
- User explicitly rejected a "truncate + press Enter for full text"
  workaround (compared unfavorably to apps like Unigram that display
  long pasted text completely) — wants a real fix that displays the
  full text, understands it's more invasive, and wants to pilot it on
  the **Chat tab only** first before deciding whether to extend to
  `feedWindow.py`'s `postList`.

### Agreed plan: convert `messageList` to `wx.LC_VIRTUAL`

In virtual-list mode, the control never stores per-cell text internally
— it calls back into Python's `OnGetItemText(item, column)` on demand
whenever it needs to know a cell's text (painting, accessibility
queries, etc.), so the apparent fixed-buffer limit should never come
into play at all.

**CRITICAL implementation gotcha (from a Gemini review, not yet
independently verified firsthand):** `OnGetItemText` **must be a method
on an actual `wx.ListCtrl` subclass** — if it's defined on the mixin
(`_ChatMessagePanelMixin`) or on `ChatWindow`/`ConvoTabWindow` directly
instead, wx will silently never call it (blank/empty-looking list, no
error raised). Correct approach: a small subclass —

```python
class VirtualMessageList(wx.ListCtrl):
    def OnGetItemText(self, item, column):
        return self.GetParent().get_virtual_item_text(item, column)
```

— construct `messageList` as an instance of this subclass instead of a
bare `wx.ListCtrl(self, style=wx.LC_REPORT)`, with the actual
per-column text logic living in a `get_virtual_item_text` method on the
parent panel (`ChatWindow`/`ConvoTabWindow`, via the mixin) so both
still share one implementation.

**On NVDA/screen-reader compatibility:** a Gemini review asserted that
NVDA and JAWS fully support Win32 virtual ListViews (`LVS_OWNERDATA`)
already — Explorer and Task Manager use the same underlying pattern,
and screen readers read via `LVN_GETDISPINFO` → `OnGetItemText`
dynamically, so no accessibility regression is expected. **Treat this
as reassuring but not a substitute for real testing** — this project
has hit genuine screen-reader-specific surprises before that looked
fine in theory (see the Alt+number bug above). Test thoroughly with
real NVDA once implemented, not just "does the list render."

### Concrete call sites in chatWindow.py needing conversion

(All confirmed via grep against the current source — scoped to
`_ChatMessagePanelMixin`/`ChatWindow`/`ConvoTabWindow`'s `messageList`
only; `feedWindow.py`'s `postList` deliberately not touched yet.)

1. `ChatWindow._showMessages` and `ConvoTabWindow._loadMessages` —
   currently `DeleteAllItems()` + a loop of `InsertItem`/`SetItem` per
   row. Needs to become: store `self._currentMessages = messages`, then
   `self.messageList.SetItemCount(len(messages))` +
   `self.messageList.RefreshItems(...)`. The per-column logic currently
   inline in these loops (Reactions/From/Message/Sent columns) should
   move into the new `get_virtual_item_text(item, column)` method as
   the single source of truth.
2. `_onTimeRefreshTick` — currently `SetItem(i, 3, ...)` per row on a
   60s timer. Becomes `self.messageList.RefreshItem(i)` (re-invokes
   `OnGetItemText` for that row on demand instead of pushing text in
   directly).
3. The optimistic "Sending..." temp-row insert in `onSend` (the
   `tempIndex = ...; InsertItem/SetItem` block) — needs to insert a
   synthetic dict into `self._currentMessages` first, then
   `SetItemCount`/`RefreshItems`, instead of inserting directly into
   the control.
4. `_announceNthNewestMessage`'s `GetItemText` call — should need **no
   code change**; wx's `GetItemText` on a virtual list already proxies
   to `OnGetItemText` per docs, and should incidentally start returning
   the full (untruncated) text once the conversion lands.
5. **Not yet fully verified** — whether the reaction-update or
   mark-as-read paths ever patch a single `messageList` cell directly
   (vs. always re-rendering the whole list via `_showMessages`/
   `_loadMessages`). Grep so far found no single-cell `SetItem` calls
   for reactions specifically, but this needs a final confirmation pass
   (check `_onReactionDone`, `_onDeleteMessageDone`, and the
   mark-read/unread paths in chatWindow.py) before writing the full
   patch — this was the one open item mid-check when this chat's
   context ran out. **Recommended first step in the new chat.**
6. `Focus()`/`Select()`/`EnsureVisible()`/`HasFocus()`/
   `GetFocusedItem()` (used throughout, including the just-fixed
   Alt+number code in `uiutil.move_focus_and_check_announce`) — should
   keep working unchanged in virtual mode since they're index-based
   with no dependency on per-cell storage; expected to need no code
   change, just confirm in testing.

## Other outstanding items (lower priority, carried from plan-07.md)

1. `onConvoSelected`'s TreeCtrl crash — a defensive try/except guard is
   in place and stops the crash, but the actual root cause (why the
   tree gets destroyed) was never confirmed. Distinct mechanism from
   the Stage-0 `wx.CallAfter` fix above (this is a direct event-handler
   race, not a background-thread completion callback). Watch for
   "selected a conversation, nothing happened" (silent, no crash) as a
   sign it's still live.
2. `feedWindow.py`'s `onCharHook` still duplicated 5x (Home/Saved/
   Lists/ListTab/Notifications) — worthwhile refactor, not urgent, same
   class of risk that caused chatWindow.py's duplication bugs before.
3. Whether to remove the temporary `log.info` logging in
   `uiutil.safe_ui_callback` (added during the original crash
   investigation) — extensive testing across many sessions since then
   has caught nothing else. User hasn't been asked to decide yet.
4. `chat.bsky.convo.getLog` for incremental sync (pure performance,
   nice-to-have, not user-visible); a dedicated Chat options settings
   page (no concrete need identified yet); group chat
   (`chat.bsky.group.*`) is a wholly separate, unstarted namespace —
   skip unless actually wanted. (Carried forward unchanged from
   plan-07.md, never revisited this session.)

``n
## File: plan-09.md
`md
# NVSky — plan-09.md

Handoff doc for a new chat. Same process rules as plan-06/07/08.md,
still in effect:

## Process reminders for the new chat

- Read actual current source files fresh at the start (paste in or
  upload) before proposing any code -- once a round of patches is
  confirmed applied and tested, treat that as reflected in the user's
  real source and keep working from that state -- don't ask for a
  full re-upload every round. Only ask for a fresh full-file paste, or
  just the specific method/section, if a patch is reported as failing
  or landing in the wrong place.
- **Always double-check the target FILENAME against the actual `##
  File:` marker in the uploaded source before writing a patch header.**
  This session had repeated mistakes labeling `MainWindow` patches as
  `feedWindow.py` when `MainWindow` actually lives in `mainWindow.py`
  (file boundary confirmed via the `## File:` markers in the uploaded
  .md files) -- caused real confusion for the user, who patches by
  hand per-file. Don't guess the file from memory of where a class
  "should" be; check the marker.
- Diff format: `old_str:` / `new_str:` **inside ONE fenced code block
  together** (not two separate fences) -- applied via the user's
  Notepad++ script. old_str needs 2-3 unique context lines
  before/after the change, must match exact whitespace, stays as one
  block for one contiguous change. Always state the target filename
  clearly right before each block.
- For anything sizable (heavily-edited method, brand new method, or a
  change touching many lines), send the **whole method or whole file**
  instead of a fragile diff.
- When multiple edit locations exist in one file, number sub-headings
  clearly (`##### 1.1`, `##### 1.2`, ...).
- Multiple classes in the same file are often near-identically
  duplicated -- when in doubt about which of two near-identical blocks
  matched, ask for that one method pasted back rather than guessing.
  Same-name/near-identical methods across classes need extra
  disambiguating context in old_str (a preceding unique comment/line
  or a wider span reaching back to the class's own docstring/first
  unique line), not just the def line -- came up again this session
  (feedWindow.py's onCharHook refactor needed this for 3+ classes with
  literally identical neighboring methods).
- Always double-check generated diffs for two statements accidentally
  glued onto one line with no newline between them.
- Communicate with the user in Thai. Code (all identifiers, comments,
  strings) stays English always.
- Standing architecture principle: optimistic-UI-then-background-
  network pattern (update UI/local state immediately, background
  thread does the real network call, reconcile silently after) applies
  to every activity across the whole add-on.
- When using an SDK method, wx/NVDA API mechanism, or general technique
  never exercised in this project before, flag it plainly as LOW
  CONFIDENCE and ask the user to test/paste back a traceback rather
  than presenting a guess as certain -- this project has repeatedly
  been bitten by things that looked solid in theory (LC_VIRTUAL not
  fixing the 511-char cutoff; three separate wrong guesses in a row
  for the "quick close" crash this session before finding the real
  cause). **When a hypothesis-based fix comes back from testing as
  having NO effect, say so plainly and move to the next hypothesis --
  don't defend the original theory.**
- The user now sometimes brings a second opinion from Gemini
  (analysis/ideas only, never code applied directly -- the user
  applies only Claude's diffs). Treat this as a genuinely useful
  second-opinion channel, not noise: engage with it seriously,
  fact-check specific technical claims against the actual source
  rather than accepting or dismissing wholesale, and adopt the parts
  that hold up. This session, Gemini's general diagnosis category
  (background thread racing a destroyed window) was directionally
  right and worth taking seriously, but several of its specific
  causal claims (e.g. "the worker thread in this trace is what
  crashed it") didn't hold up against a closer read of the actual
  code and were worth pushing back on with specifics rather than
  accepting at face value.
- If a temporary debug patch (timing log, diagnostic log.info, etc.)
  is applied on the user's side, track that it's still present -- a
  later whole-method patch to that same method needs to account for
  it or the old_str won't match.

## What NVSky is

Solo-dev NVDA screen-reader add-on for Bluesky (AT Protocol), closed
development. `MainWindow` (`wx.Notebook`, defined in mainWindow.py)
holds Home, Notifications, Saved, Chat, and Lists as permanent tabs,
plus dynamically-opened removable tabs (per-list timelines, per-
conversation chat pop-outs).

## Completed since plan-08.md

**Chat message 511-character truncation (plan-08.md's whole focus) --
resolved, but NOT via the originally-planned route:**

- `wx.LC_VIRTUAL`/`OnGetItemText` conversion was tried and **confirmed
  by testing to NOT fix the cutoff** -- the ~511-char limit persisted
  identically in virtual mode, including via `GetItemText()` and even
  via a Column Review add-on cell-inspection dialog independent of
  virtual/non-virtual mode. This was fully reverted (user restored
  from their own backup) -- messageList is back to a plain
  `wx.ListCtrl(style=wx.LC_REPORT)`, not virtual.
- Real fix landed in 2 parts:
  1. **Alt+number / "Show message..." full-text reads**: fixed by
     reading straight from `self._currentMessages[index]` in Python
     instead of any ListCtrl text-retrieval API (`GetItemText` etc,
     which is itself capped at ~511 regardless of virtual mode -- root
     mechanism never fully pinned down, Win32-level buffer suspected
     but not proven). New `_show_message_dialog` helper (chatWindow.py)
     -- a read-only `wx.TextCtrl(style=TE_MULTILINE|TE_READONLY)`
     dialog, same idea as Column Review -- wired to a new "Show
     message..." context-menu item (not bound to a key yet, user
     hasn't decided which key -- Enter is reserved for a planned
     quick-action feature, maybe Space, undecided).
  2. **Arrow-key/normal navigation reads** (harder problem -- NVDA's
     own automatic focus announcement reads the ListCtrl's native
     text, not app code, so Python-level fixes can't touch it):
     solved via the user's own idea, confirmed elegant and working --
     split any message over `MESSAGE_COLUMN_SPLIT_THRESHOLD` (450)
     chars across TWO columns ("Message" / "Message (more)"), since
     NVDA already reads all visible columns of a focused row back-to-
     back automatically. New `client.split_at_grapheme_boundary(text,
     max_chars)`: grapheme-cluster-safe (won't sever a Thai combining
     mark or a ZWJ emoji sequence), targets the MIDPOINT of the text
     (not a fixed max) so neither column risks exceeding the native
     cap even near Bluesky's ~1000-char message limit, and prefers to
     land the split at a whitespace/punctuation boundary within a
     100-char lookback window (covers CJK full-width punctuation too)
     to avoid mid-word breaks -- confirmed by testing to still li mid-
     word for long unbroken runs of Thai with no punctuation nearby at
     all (inherent limit, no dictionary-based segmenter in scope);
     "Show message..." remains the guaranteed-complete fallback for
     that case. `_ChatMessagePanelMixin` gained shared
     `_messageFromLabel`/`_messageDisplayText` hooks (`ChatWindow` +
     `ConvoTabWindow` both implement them) so column-population,
     Alt+number, and "Show message..." all pull from one source of
     truth instead of duplicating the reply-preview-prefix logic.
- Feature deliberately scoped to Chat's `messageList` only, same as
  plan-08.md's original pilot decision -- `feedWindow.py`'s `postList`
  (posts cap at 300 chars, under the cutoff) still untouched.

**Bonus bug found during this work and fixed:** `SavedWindow`,
`ListsWindow`, `ListTabWindow`'s `_insertRow` still populated columns
in the OLD order (Author/Message/Posted/Embed) after a past refactor
moved the shared header to Embed-first (Embed/Author/Message/Posted)
-- `FeedWindow` was fine (uses `FeedListMixin`'s shared row-insert),
the other three had their own duplicated `_insertRow` that never got
updated. Fixed in all three; confirmed working by the user.

**`onCharHook` consolidation (was on plan-08.md's lower-priority
list) -- done:** 5 near-identical copies (Home/Saved/Lists/ListTab/
Notifications) collapsed into one shared `FeedListMixin.onCharHook`,
gated per-class by `SUPPORTS_NEW_POST`/`SUPPORTS_FOCUS_NEXT_UNREAD`/
`SUPPORTS_SELECT_ALL`/`SUPPORTS_JUMP_TO_USER` class attributes (kept
each class's exact prior behavior, nothing silently added/removed).
Found and fixed a real bug in the process: `NotificationsWindow`'s old
onCharHook was missing the "Ctrl+F5 bubbles up to MainWindow" guard
the other 4 had, so Ctrl+F5 there did a single-tab sync instead of
`MainWindow.checkAllOpenTabs`'s full sweep -- fixed as a side effect
of the consolidation. `_jumpToUserPost`/`_postInvolvesUser`/
`onNewPost` (previously FeedWindow-only) moved up into `FeedListMixin`
so `ListsWindow` could opt into full Home-equivalent shortcuts per the
user's explicit design call (Home and Lists should behave identically
since they're the same kind of content; Notifications gets mark-read
only, no Left/Right cross-tab jump; Saved gets nothing extra).

**Multi-select "leftover selection" bug found and fixed:** Alt+number
(both chat and every FeedListMixin post-list tab) left the PREVIOUSLY
focused row still selected after moving to a new one, because
`uiutil.move_focus_and_check_announce`'s `Select(index)` call only
ADDS to a multi-select ListCtrl's selection, never replaces it --
`feedWindow.py`'s `_jumpToUserPost` had already solved this exact
problem locally (explicit deselect-others loop) but the fix was never
applied to the shared `move_focus_and_check_announce` helper. Fixed by
mirroring that same idiom in the one shared spot -- confirmed working
across all tabs by the user.

**A real, if minor, pre-existing bug found (unrelated to any of the
above) and fixed:** `FeedListMixin._onTimeRefreshTick` hardcoded
column index 3 for the "Posted"/time column, correct for the standard
4-column Embed/Author/Message/Posted layout but wrong for
`NotificationsWindow` (only 3 columns, Author/Notification/Received,
time column at index 2) -- would throw an unhandled `wxAssertionError`
(not caught by `safe_ui_callback`, which only catches "has been
deleted" RuntimeErrors) whenever the 60s timer ticked while
Notifications was the visible tab under a relative-time Display
setting. Fixed via a new `TIME_COLUMN_INDEX` class attribute
(default 3, `NotificationsWindow` overrides to 2). Confirmed by the
user this specific crash mode is gone, though it turned out NOT to be
the cause of the "quick close" crash reports below (different bug,
found and fixed along the way during the same testing round).

## The "quick close" crash saga -- resolved, worth reading in full for the eventual real cause

Two related-but-distinct crash reports arrived this session, both now
believed fixed (user confirmed the ORIGINAL untouched-window one is
gone; the refresh-in-progress one is also confirmed gone after the
final fix below):

1. **Original report**: open MainWindow (NVDA+Alt+B), close it again
   immediately, having touched nothing. Intermittent, sometimes took
   ~30s to manifest.
2. **Second report** (found while chasing #1): "Home tab > F5 refresh
   > close immediately" -- much more reliably/instantly reproducible
   once found.

**Root cause of #1, confirmed fixed**: `ListsWindow.__init__` (via
`_loadListsFromCache()`) was STILL unconditionally kicking off a
background `_syncListsFromServer()` network call on every single
MainWindow open, regardless of whether the user ever touched the
Lists tab -- this was the ORIGINAL Stage-0 crash from plan-07.md;
Stage 0 (the `safe_ui_callback` decorator) made the resulting
destroyed-window race SAFE at the Python level (caught RuntimeError
instead of crashing), but the previously-agreed "Stage 2" (remove the
auto-sync entirely, matching the other 4 permanent tabs which only
ever load from local cache at construction) was never actually done.
Every "quick close, untouched window" crash log showed
`ListsWindow._onSyncListsDone skipped -- already destroyed` as the
immediately-preceding event, every time. Removing the auto-sync call
(now Lists behaves exactly like Home/Saved/Notifications on open --
cache only, sync only on explicit F5/Shift+F5/user action) fully
fixed this per the user's confirmation. **User's stated intent**: some
form of background auto-update across all tabs IS wanted eventually,
just not now -- deliberately deferred to later, this was an accidental
early/partial version of it, not an intentional design.

**Root cause of #2, confirmed fixed, took 3 wrong guesses first**
(worth reading so the next session doesn't repeat them):
- Wrong guess 1: `ChatWindow._loadFromCache` reentrancy (a `SelectItem`
  synchronously firing `onConvoSelected` while still unwinding from an
  earlier call). Added a reentrancy guard -- harmless, kept, but did
  NOT fix this crash (chat wasn't even involved in the repro).
- Wrong guess 2: no timer cleanup in `MainWindow.onClose` at all
  (`wx.Timer` isn't part of the parent-child destroy cascade). Added a
  stop-loop for `_timeRefreshTimer` specifically -- directionally
  right but incomplete, see below.
- **Real cause**: `FeedListMixin._startLoadingBeep()`, called every
  time F5/`onCheckForUpdates` runs, creates a SEPARATE
  `self._loadingTimer` (1-second interval, much faster than the 60s
  `_timeRefreshTimer`) that is ONLY ever stopped from inside
  `_onCheckForUpdatesDone`. An `app_closing` module-level flag
  (`uiutil.app_closing`, checked first thing inside
  `safe_ui_callback`, set True at the very start of
  `MainWindow.onClose` and reset False at the start of
  `MainWindow.__init__`) was added for defense-in-depth on ALL
  CallAfter-dispatched completions -- but this ALONE made things
  WORSE for this specific bug, since it caused
  `_onCheckForUpdatesDone` to skip entirely when closing, meaning
  `_loadingTimer` NEVER got stopped at all, left ticking into a
  destroyed window every second. Real fix: `MainWindow.onClose` (and
  `ListTabWindow.onTabRemoved`, which had the identical gap for its
  own Ctrl+W removal path) now scan `vars(panel).values()` for every
  `wx.Timer` INSTANCE and `.Stop()` each one, rather than checking for
  specific attribute names -- avoids repeating this exact class of
  miss if another timer gets added later without this method being
  remembered/updated. The `app_closing` flag is still kept (harmless,
  useful defense-in-depth for the OTHER ~39 CallAfter-dispatched
  completions that aren't timer-related), just wasn't sufficient
  alone.

**Follow-up hardening audit done at the user's request** ("ไล่เช็กโค้ด
ให้แข็งแรงก่อนขยับไปฟีเจอร์ใหม่"), systematic not reactive:
- Cross-checked all ~39 unique `wx.CallAfter(self._onXxx, ...)` target
  methods against having `@uiutil.safe_ui_callback` -- found ONE real
  gap: `chatWindow.py`'s `_onSendComplete` (the chat-send background
  thread's completion handler) had NO decorator at all, meaning
  sending a message then immediately closing/removing that chat's tab
  before the post-send resync finished was an unguarded version of
  the exact same crash class. Fixed. Also added the decorator to
  `mainWindow.py`'s `_focusPanel` for consistency (much lower risk --
  same-thread deferred call, not a background-thread completion).
- Scanned every class's `__init__` for the same "unconditional
  background thread/sync call at construction" bug class as the
  ListsWindow one -- found nothing else, confirmed clean.
- Checked the Ctrl+W tab-removal path (`removeCurrentTab` ->
  `onTabRemoved`) for the same timer-cleanup completeness as
  `MainWindow.onClose` -- found `ListTabWindow.onTabRemoved` had the
  identical `_loadingTimer` gap, fixed with the same generic
  Timer-instance scan.

## Outstanding items carried forward (all previously lower-priority, none newly urgent)

1. Whether to remove the temporary `log.info` logging in
   `uiutil.safe_ui_callback` (added during the very first crash
   investigation, several sessions ago) -- user's explicit call this
   session: leave it in for now, this is still active dev/testing,
   clean it up in one pass at the actual end of the dev phase, not
   piecemeal.
2. `chat.bsky.convo.getLog` for incremental sync (pure perf,
   nice-to-have), a dedicated Chat options settings page (no concrete
   need identified), group chat (`chat.bsky.group.*`, wholly separate
   unstarted namespace) -- all explicitly "skip unless actually
   wanted", never revisited this session, not asked about again.
3. Whether `_stopTimeRefreshTimer()` (the old by-name method, now
   superseded by the generic `vars()`-scan pattern in both
   `MainWindow.onClose` and `ListTabWindow.onTabRemoved`) still has
   any other caller worth checking before deleting it as dead code --
   flagged but not checked this session, small cleanup item for
   whenever the log.info cleanup above happens.
4. "Show message..." context-menu item still has no keyboard shortcut
   bound -- user hasn't decided which key (Enter is reserved for a
   planned quick-action feature; Space was floated but not confirmed).
   Low priority, no urgency expressed.

## Next planned direction (per the user, start of next chat)

Hardening/audit pass is considered done for now (no further known
issues) -- next chat moves on to a NEW feature/tab rather than more
bug-hunting. Nothing has been decided yet about which one -- candidates
mentioned in earlier sessions but never scoped: Explore and Feeds tabs
(purpose/scope never decided). Start the next chat by asking the user
what they want to tackle, don't assume Explore/Feeds without asking.

``n
## File: plan-10.md
`md
# plan-10.md — NVSky handoff

Session goal: complete Chat tab group-chat support before moving to any other tab.
Core group chat (create/display/manage/lock/leave) is DONE and tested working.
This doc covers what's still open going into the next session.

## Confirmed working (tested by user this session)

- Group creation via New chat dialog (multi-recipient check-list, auto-detects
  group vs 1:1 from recipient count or an explicit group name)
- Group display: name, member list, `(group)`/`(locked)`/`(request)` tags in the
  conversation tree
- Manage members dialog: check-list add/remove, optimistic UI (removes/adds
  render immediately, roll back on failure)
- Lock / Unlock (`chat.bsky.convo.lockConvo`/`unlockConvo`) — confirmed working,
  reads `locked` correctly from DB (`convo.kind.lock_status`)
- Leave conversation — confirmed working for both 1:1 and group, including as
  the group's own owner (works once locked; `OwnerCannotLeave` from the server
  is caught and turned into a clear message telling the user to lock first)
- Context menu on the conversation tree (Leave/Lock/Manage members) — confirmed
  correct as designed: shown unconditionally for every group rather than gated
  on admin/owner status, since owner-vs-member makes no practical difference to
  which menu items are USABLE (leave still works either way, just requires lock
  first for the owner)
- `is_admin` — role now readable via `member.kind.role` (confirmed via a real
  debug_dump: `"owner"`) but not currently used for any UI decision. Available
  in `convos.is_admin` if a future feature needs it.
- Account switching / fresh re-login now correctly refreshes an already-open
  MainWindow (`GlobalPlugin.rebuild_main_window_tabs()`, wired from
  `AccountsPanel.onAdd`/`onSetActive`) — was previously silently stuck because
  `_onAccountChanged()` was never called after `onAdd`, and even when called,
  nothing downstream ever reached MainWindow's tabs
- `chat_supported` capability probe added at login (`client.check_chat_supported`
  — LOW CONFIDENCE, no documented capability endpoint exists, this just tries
  `listConvos` and treats any failure as unsupported). Chat tab is skipped
  entirely when false. **Not yet tested against a real unsupported account.**
- `remove_account` now also cleans up `convos`/`messages`/`convo_members` (were
  never covered by a FOREIGN KEY CASCADE). **Not yet tested** (blocked on the
  login bug being fixed first, per user).
- DB connection cleanup on `GlobalPlugin.terminate()` (WAL checkpoint + close
  current-thread connection) — partial fix for the "can't copy config to
  portable NVDA" issue Gemini flagged; does NOT close background-thread
  connections (can't, safely — see `db.close_all_connections`'s docstring).
  Disabling the add-on first remains the fallback for that specific case.

## Fixed this session, not yet re-tested by user

- `_onCheckAllConvosDone` (Shift+F5 in Chat tab) now also calls
  `_reloadConvoListIfAny()` → `ChatWindow._loadFromCache()` — previously synced
  data to DB correctly but never refreshed the conversation tree itself
- `ChatWindow._loadFromCache()` now restores the PREVIOUSLY selected
  conversation instead of always jumping back to the first one on every reload
  — suspected (not confirmed) contributor to both the garbled chat status-bar
  text and Ctrl+F5's "only says the first/last tab name" report
- New chat dialog now announces "Chat started." on success (was silent before)
- `onSend` returns focus to the compose box after sending (guess at the
  "status bar becomes 'Send'" report — turned out to be the wrong theory, see
  below; this focus-return is still a reasonable UX fix regardless)

## Known bugs, blocked on source not yet provided

Explicitly not guessing further at these — asked for source, waiting on it:

- **Chat tab status bar shows garbled, STUCK text** (not a speech race — the
  displayed value itself is wrong, same garbled text regardless of which
  conversation is selected). Reported text: "Message: locked -- no new
  messages [un]read 5 conversations can be sent." The leading "Message:" is
  suspicious — that's `composeLabel`'s original default text, which shouldn't
  have anything to do with the status bar at all unless something is
  conflating the two widgets. Needs: `ChatWindow.__init__`'s statusBar/
  composeLabel construction, and the current live `_updateActionArea`.
- **Mark-as-read on focus + Space key not working in Chat tab.** Reading into
  the newest (already-focused-by-default) message on open doesn't count as
  read; Space does nothing. Needs: Chat tab's `WXK_SPACE` handler and whatever
  "mark read on focus" hook exists (`_afterMessageRead` or similar).
- **Space doesn't move unread count in Lists tab either** (separate from the
  Chat tab report above, same symptom). Needs: `ListsWindow`/`FeedListMixin`'s
  Space handler.

## Deliberately deferred (backlog, not blocking)

- **Join links** (paste a bsky.app group invite URL, validate, show group
  name, join) — Stage 5, explicitly parked all session. Real
  `chat.bsky.group.*` behavior has repeatedly differed from what the lexicon
  docs implied (nested `kind` objects, no docs for member role, etc.) — start
  this in its own session with debug_dump available from the start rather than
  guessing through several rounds like this session did for group name/kind/role.
- **Join-request approval UI** (admin side: someone requests to join a group)
  — same reason, tied to join links, never tested since no join-link flow
  exists yet to generate a request.
- **Background update system** — Lock/Unlock currently does a foreground
  refresh right after the action instead of updating quietly in the
  background. Flagged to fix once a real background-sync system exists,
  rather than one-off per action.
- **React optimistic UI** — message reactions still wait on the server
  round-trip; every other group/member action was made optimistic this
  session, reactions weren't gotten to.
- **Cross-tab live sync** — sending a message from ChatWindow doesn't
  immediately show up in an already-open ConvoTabWindow for the same
  conversation (and vice versa) — each only refreshes on its own trigger
  (F5/Ctrl+F5/reopen). No pub-sub between panels showing the same convoId
  exists yet. Real architecture item, not a quick patch.
- **What happens when a group is down to one member / the last member leaves**
  — unknown, never tested, nothing in the lexicon docs found so far. User may
  test this directly (reversible — worst case the conversation just
  disappears) and report back with real behavior if curious.
- **Edit group name** — no confirmed endpoint exists in the lexicon research
  done this session (createGroup/addMembers/removeMembers/join-link endpoints
  were found; no updateGroup/rename equivalent). Not implemented.
- **Delete group entirely** — no such endpoint found either; lock + leave
  appears to be the closest functional equivalent by design.

## Debug tooling added this session

`client.debug_dump(obj, label)` — dumps any SDK object (pydantic models,
nested objects, dicts, lists) to a JSON file under
`globalPlugins/NVSky/debug_dumps/`, instead of guessing field names one
`log.info` round at a time. This directly resolved the group-name/`kind`/role
confusion this session after several wrong guesses — **use this first** for
any new unverified SDK surface (join links, background sync, etc.) rather
than repeating the log.info-guessing pattern.

## Suggested order for next session

1. Fix the 3 blocked-on-source bugs above once source is provided (status bar,
   Space/mark-as-read in Chat, Space/mark-as-read in Lists)
2. User-test: `chat_supported` false case, `remove_account` cleanup, last-member-leaves-group behavior
3. Then: either finish remaining Chat backlog (react optimistic, background
   lock refresh, cross-tab live sync) or move straight to join links as its
   own focused session — user's call
4. Other tabs (Explore/Feeds, or whatever's next) only after the above is
   actually closed out — explicit priority this session

``n
## File: plan-11.md
`md
# plan-11.md — NVSky handoff (Explore/Feeds session)

Session goal: build Explore tab (search: Posts/People/Starter packs/Feeds) and
lay groundwork for Feeds-in-Settings. Explore is structurally complete but has
one confirmed-broken bug going into next session; Settings/Feeds work never
started.

## Confirmed working (tested by user this session)

- Explore tab exists as permanent tab, position 3 (Home, Notifications,
  **Explore**, Saved, Chat, Lists), reorderable via Ctrl+Shift+PageUp/PageDown
  with order persisted (`db.get_ui_state("permanent_tab_order")`) — confirmed
  no double-announcement after the RemovePage/InsertPage suppress-flag fix.
- Search box + Result type radio (Posts/People/Starter packs/Feeds), debounced
  auto-search (~800ms idle), initial focus lands on search box correctly (both
  on first MainWindow open and on switching back to the tab), and no longer
  gets yanked back to the search box on F5/refresh (fixed via a one-time
  `_didInitialFocus` guard in `_restoreFocusPosition`, plus never stealing
  focus while the user is actively typing).
- Advanced search (From handle/Since/Until/Language) — collapsible via a
  `wx.CheckBox` (NOT `wx.ToggleButton`, which isn't used anywhere else in this
  codebase and apparently didn't announce state correctly), hidden panel with
  labelled fields, auto-hidden when result type isn't Posts. LOW CONFIDENCE:
  since/until/author/lang param names on `search_posts` recalled from lexicon
  knowledge, never independently verified against a real response — user
  confirmed filtering "works correctly" but this wasn't independently
  double-checked field-by-field.
- Posts results: real DB-cached, paginated feed (via `_dbGetPage`/`_syncPage`
  → `client.sync_search_page`, feed_key = `_search_feed_key(query, filters)`),
  NOT a one-shot fetch — matches Home tab's architecture. Fetch-older
  (scroll-to-bottom) confirmed working. `SUPPORTS_FOCUS_NEXT_UNREAD = True` is
  now set (was missing, so jump-to-unread never worked despite the status bar
  showing an unread count) — **not yet re-tested since being added**.
- People/Starter packs/Feeds results: separate dedicated ListCtrls swapped via
  Show/Hide. People reuses `UserActionMixin` (`_populateUserActionMenu`) both
  via right-click and a dedicated "User action" button. Starter packs: "View
  pack details..." (member count shown, full member/feed list, Follow/Open
  buttons built directly into the details dialog — not just the outer context
  menu) + "Follow everyone in this pack" (via `graph.follow` per profile,
  skips already-followed) + "Open on bsky.app". Feeds: "View feed..." (opens a
  real paginated tab, see below), "Open on bsky.app". Action buttons correctly
  hidden when the current result type has zero results.
- "View feed" (from a Feeds search result) and "Open in new tab" (from Posts
  results) both open a `FeedPreviewTabWindow` — a real cached/paginated tab
  (same `_dbGetPage`/`_syncPage` contract as Home, `sync_feed_generator_page`/
  `sync_search_page` in client.py), NOT a one-shot preview. Confirmed:
  fetch-older works, persists across closing/reopening NVSky (`TAB_TEMP_TYPE
  = "search_preview"`, reconstructed from `db.get_open_temp_tab`s at startup;
  stale pre-this-session entries missing "source_key" are dropped instead of
  crashing on reconstruction). Confirmed: closing a `FeedPreviewTabWindow`
  that was opened from Explore jumps focus back to the Explore tab (via
  `origin_key` tracked through `TAB_TEMP_TYPE`/temp-tab entries) — **user
  explicitly wants this "remember where I came from on close" behavior
  audited across every OTHER pop-out tab type in the app (View Thread,
  followers/following lists, chat's "Open in new tab", etc.) next session —
  Explore is the only place it's been done so far.**
- `FeedPreviewTabWindow` now also has an "Add to my feeds" button when opened
  from a feed generator (not from a pinned search) — untested this session
  (added in the same round as the `_markSelectedRead` crash below, never
  reached testing).

## Known bugs, blocked on source not yet provided

- **`ExploreWindow` crashes on open with `AttributeError:
  'ExploreWindow' object has no attribute '_markSelectedRead'`** — this
  method lives in `ItemActionMixin`, which `ExploreWindow` explicitly
  inherits, so by MRO it should exist. Strong suspicion (same failure mode hit
  multiple times this session): a stray `class` declaration got left in the
  middle of `ExploreWindow`'s own method list at some point across the many
  edits this session (exact same root cause as the earlier
  `StarterPackDetailsDialog`-in-the-middle-of-`ExploreWindow` bug, fixed once
  already this session but apparently recurred or a different instance of it
  was never caught). **Needs: paste the full current `ExploreWindow` class
  (and ideally a plain grep of `^class ` across feedWindow.py) before touching
  it again** — guessing further without seeing the real file risks the same
  whack-a-mole seen earlier this session (fixing one missing method only to
  discover the next one two patches later). Still open going into next
  session -- blocks testing "Add to my feeds" and `SUPPORTS_FOCUS_NEXT_UNREAD`
  (jump-to-unread) in `FeedPreviewTabWindow`, neither of which has been
  reachable since this crash appeared.

## Resolved this session (verified against a real server)

- **`add_feed_to_saved` (client.py)** — RESOLVED. Three of Claude's own fix
  attempts all hit the identical `Unable to serialize unknown type:
  <class 'pydantic.fields.FieldInfo'>` error (root cause: `atproto` v0.0.69's
  generated `ContentLabelPref` model has a broken `py_type` field that holds
  the raw, unresolved `FieldInfo` declaration instead of its string value,
  and no form of `model_dump()` on that object could serialize it). The user
  brought in outside help (Gemini, then ChatGPT) and landed on a working fix:
  the real problem wasn't the dump step at all -- `client.app.bsky.actor.
  put_preferences()` itself calls `get_or_create()` internally, which
  **rehydrates the already-sanitized plain-dict payload back into a fresh
  Pydantic model** before calling `model_dump_json()`, hitting the exact same
  `FieldInfo` bug a second time regardless of how clean the input dict was.
  Passing raw JSON bytes into `invoke_procedure()` doesn't avoid this either,
  since the low-level client still calls `get_model_as_json(data)`, which
  expects `data` to expose `model_dump_json()`. The working pattern: build a
  clean raw payload and drive the **low-level `app.bsky.actor.putPreferences`
  procedure path directly**, in a way that never rehydrates the payload back
  into a Pydantic model before serialization. Confirmed working against the
  real server -- the added feed shows up on Bluesky Web. Do not touch
  `NVSky\lib`'s vendored `atproto`/`pydantic` versions to "fix" this at the
  library level; the payload-level workaround is what's in place now.
  **Follow-up, not urgent:** the resulting function grew from ~2K to ~10K+
  characters because of how involved the workaround is -- Claude should read
  the actual current source next session and see whether it can be
  simplified/shortened without breaking the fix, per the user's request.

## Deliberately deferred (backlog, not blocking)

- **Embed-type filtering in Explore search** (image/video/link, like
  Twitter's advanced search) — confirmed no such param exists in
  `searchPosts`; would need to be a client-side post-hoc filter on already-
  fetched results. User explicitly said skip it, keyword search (e.g. typing
  domain names or obvious media-related terms) is good enough.
- **Hashtag search** — no dedicated UI needed; typing `#tag` directly into
  the existing Posts search box already works via the normal search API.

## Suggested order for next session

1. Fix `ExploreWindow`'s `_markSelectedRead` crash — get the real current
   class source first, don't guess.
2. While looking at `add_feed_to_saved`'s now-working ChatGPT-provided fix,
   see if it can be shortened/simplified (grew from ~2K to 10K+ characters)
   without breaking it — user's explicit ask, not urgent.
3. Test `FeedPreviewTabWindow`'s "Add to my feeds" button and
   `SUPPORTS_FOCUS_NEXT_UNREAD` (jump-to-unread) — both added but never
   reached testing before the `ExploreWindow` crash blocked further use of
   the tab.
4. Audit "remember where I came from on close" (`origin_key` /
   `TAB_TEMP_TYPE` jump-back-on-close) across every other pop-out tab in the
   app, not just Explore's — per user's explicit request last session.
5. **Main goal this next session: Settings > Feed manager.** List/reorder/
   remove subscribed feeds (read+write via `savedFeedsPrefV2` in
   `actor.get_preferences`/`put_preferences` -- reuse whatever payload
   pattern `add_feed_to_saved` ends up using once simplified), pin/unpin
   feeds per whatever the SDK actually exposes for that, browse+add new
   feeds by reusing Explore's Feeds search. Then wire the result into a real
   Home tab filter dropdown so a saved/pinned feed can actually be viewed
   from Home, not just added to the list -- this is the part that makes the
   whole feed-manager feature actually usable end to end, not just a list
   you can edit but never see reflected anywhere.

``n
