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
