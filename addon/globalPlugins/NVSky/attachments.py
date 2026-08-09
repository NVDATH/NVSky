# attachments.py for NVSky NVDA add-on

"""
Attachment-opening helpers for NVSky's "View embed" action.

Downloads happen over plain HTTP(S) via urllib -- Bluesky's CDN
(cdn.bsky.app / video.bsky.app) serves media publicly, no auth needed,
so this deliberately does NOT go through the atproto client.

Sending a file to Be My Eyes uses the same ShellExecute trick Explorer
uses for "Open with" on a UWP app: targeting shell:appsFolder\\<AUMID>
with the file path as the parameter. This is EXPERIMENTAL -- paste
back the traceback if it doesn't behave as expected on your machine.
"""

import ctypes
import os
import tempfile
import urllib.parse
import urllib.request

import wx
from logHandler import log

BEMYEYES_AUMID = "BeMyEyes.BeMyEyes_7yeb8xxw19svt!App"

def download_to_temp(url: str, suffix: str = "") -> str:
    """
    Downloads `url` to a new temp file and returns its path, or raises.
    Sends a browser-like User-Agent and a Referer pointing at bsky.app --
    Bluesky's CDN appears to hotlink-protect and can return an HTML/JSON
    error page instead of the real file for requests without a
    same-site-looking Referer. Content-Type is checked against `suffix`
    so that kind of error gets caught here with a clear message, instead
    of failing downstream as a confusing "unknown image data format".

    Logs status/content-type/size/first-bytes for every download --
    TEMPORARY while chasing the image-download bug; safe to remove the
    log.info line once it's confirmed fixed.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*",
            "Referer": "https://bsky.app/",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
        data = response.read()

    log.info(
        f"NVSky: download_to_temp {url} -> status={status} "
        f"content-type={content_type!r} bytes={len(data)} "
        f"first-bytes={data[:16].hex()}"
    )

    if status >= 400:
        raise OSError(f"Download failed: HTTP {status} ({len(data)} bytes)")

    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp") and not content_type.startswith("image/"):
        snippet = data[:200].decode("utf-8", errors="replace")
        raise OSError(f"Expected an image, got Content-Type '{content_type}': {snippet}")

    # บังคับใช้ suffix เป็น .webp ถ้าเซิร์ฟเวอร์ส่ง WebP มา (แม้ว่าตอนเรียกฟังก์ชันจะขอ .jpg ก็ตาม)
    if content_type.startswith("image/webp"):
        suffix = ".webp"

    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    
    with open(path, "wb") as f:
        f.write(data)

    # [ส่วนที่เพิ่มใหม่] แปลง WebP เป็น PNG ด้วย Pillow ที่ติดมากับ NVDA
    if suffix == ".webp" or content_type.startswith("image/webp"):
        try:
            from PIL import Image
            
            new_path = path + ".png"
            with Image.open(path) as img:
                img.save(new_path, "PNG")
            
            # ลบไฟล์ WebP ต้นฉบับทิ้งเพื่อไม่ให้รก Temp
            try:
                os.remove(path)
            except OSError:
                pass
                
            return new_path
            
        except Exception as e:
            log.error(f"NVSky: Failed to convert WebP to PNG using Pillow: {e}")
            try:
                os.remove(path)
            except OSError:
                pass
            raise OSError(f"Could not convert WebP image for GUI display: {e}")

    return path    

def download_video_playlist_to_temp(url: str) -> str:
    """
    Downloads an HLS (.m3u8) playlist and rewrites any relative segment/
    variant-playlist URIs to absolute ones before saving locally. A raw
    m3u8 saved as-is breaks when opened from disk: its URI lines are
    relative to the ORIGINAL remote location, and a media player
    resolves them relative to the local temp folder instead once the
    manifest is on disk -- the app launches fine (proving the open call
    itself works) but can't find any of the actual segments.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 200)
        text = response.read().decode("utf-8", errors="replace")

    if status >= 400:
        raise OSError(f"Download failed: HTTP {status}")

    rewritten_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("http"):
            line = urllib.parse.urljoin(url, stripped)
        rewritten_lines.append(line)

    fd, path = tempfile.mkstemp(suffix=".m3u8")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(rewritten_lines))
    return path
    

def open_with_default_app(file_path: str):
    os.startfile(file_path)


def copy_image_to_clipboard(file_path: str) -> bool:
    image = wx.Image(file_path)
    if not image.IsOk():
        return False
    bitmap = wx.Bitmap(image)
    if wx.TheClipboard.Open():
        wx.TheClipboard.SetData(wx.BitmapDataObject(bitmap))
        wx.TheClipboard.Close()
        return True
    return False


def send_to_bemyeyes(file_path: str) -> bool:
    """
    Launches Be My Eyes with `file_path` via ShellExecute against
    shell:appsFolder\\<AUMID>, passing the file path as the parameter --
    this is the same mechanism Explorer uses for "Open with" on a UWP
    app. Returns False if the app isn't installed or launch failed
    (ShellExecute returns a value > 32 on success, an error code
    otherwise).
    """
    target = f"shell:appsFolder\\{BEMYEYES_AUMID}"
    result = ctypes.windll.shell32.ShellExecuteW(None, "open", target, file_path, None, 1)
    return result > 32
