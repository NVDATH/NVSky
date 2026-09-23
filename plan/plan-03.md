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
