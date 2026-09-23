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
