"""
Image and video upload for NVSky: blob upload with size shrinking and the video upload job.
Re-exported by client.py.
"""
import json
import os
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from logHandler import log




MAX_BLOB_BYTES = 1_000_000  # Bluesky's hard limit on any single blob


def _compress_image_for_blob(image_path: str, max_bytes: int = MAX_BLOB_BYTES) -> str:
    """Bluesky rejects any blob over max_bytes -- hit this for an
    auto-fetched link-preview thumbnail (1024x576 PNG, 1,032,450 bytes,
    just over the 1,000,000 limit) but the same cap applies to avatar/
    banner/attached images too, so this is called from the one shared
    upload path (upload_blob_dict) rather than patched per call site.
    Returns image_path unchanged if it's already small enough or if
    Pillow/compression isn't available; otherwise re-encodes as JPEG,
    lowering quality and then dimensions until it fits, and returns a
    NEW temp file path (the original at image_path is left untouched)."""
    try:
        if os.path.getsize(image_path) <= max_bytes:
            return image_path
    except OSError:
        return image_path

    try:
        from PIL import Image
    except ImportError:
        log.error("NVSky: Pillow unavailable, cannot shrink oversized image for upload")
        return image_path

    try:
        img = Image.open(image_path)
        img.load()
    except Exception as e:
        log.error(f"NVSky: could not open oversized image to shrink it: {e}")
        return image_path

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    fd, out_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)

    quality = 85
    scale = 1.0
    while True:
        working = img if scale >= 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS,
        )
        working.save(out_path, "JPEG", quality=quality, optimize=True)
        if os.path.getsize(out_path) <= max_bytes or scale < 0.1:
            break
        if quality > 40:
            quality -= 15
        else:
            scale *= 0.8

    return out_path


def upload_blob_dict(client, image_path: str) -> dict:
    originalPath = image_path
    image_path = _compress_image_for_blob(image_path)
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    if image_path != originalPath:
        try:
            os.remove(image_path)
        except OSError:
            pass
    upload = client.com.atproto.repo.upload_blob(image_bytes)
    blob = upload.blob
    return {
        "$type": "blob",
        "ref": {"$link": blob.ref.link},
        "mimeType": blob.mime_type,
        "size": blob.size,
    }


VIDEO_EXTENSIONS = (".mp4", ".mpeg", ".mpg", ".mov", ".webm")


# Current Bluesky video limits (10 minutes / 300MB since 2026-08-26); they have changed before. The
# server is the final authority: this is only a cheap pre-check.
VIDEO_MAX_DURATION_SECONDS = 600


VIDEO_MAX_BYTES = 300 * 1_000_000


_VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mov": "video/quicktime", ".webm": "video/webm",
}


class VideoUploadCancelled(Exception):
    pass


def _find_isobmff_box(f, target_type: bytes):
    """Scans top-level boxes of an ISOBMFF (MP4/MOV) file for
    `target_type` (e.g. b"moov"), returns its content bytes or None.
    Reads only box headers via seeks, not the whole file -- a 300MB
    video shouldn't be fully loaded just to check its duration."""
    f.seek(0, 2)
    fileSize = f.tell()
    pos = 0
    while pos + 8 <= fileSize:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[0:4])[0]
        boxType = header[4:8]
        headerSize = 8
        if size == 1:
            extSize = f.read(8)
            if len(extSize) < 8:
                break
            size = struct.unpack(">Q", extSize)[0]
            headerSize = 16
        elif size == 0:
            size = fileSize - pos
        if size < headerSize:
            break
        if boxType == target_type:
            f.seek(pos + headerSize)
            return f.read(size - headerSize)
        pos += size
    return None


def _read_mp4_duration_seconds(video_path: str):
    """Best-effort duration for MP4/MOV via the mvhd box -- no library.
    Returns None (not an error) for anything this simple parser can't
    read (WebM, unusual atom layout); callers should skip the duration
    check rather than block the upload in that case."""
    try:
        with open(video_path, "rb") as f:
            moov = _find_isobmff_box(f, b"moov")
            if moov is None:
                return None
            pos = 0
            while pos + 8 <= len(moov):
                size = struct.unpack(">I", moov[pos:pos + 4])[0]
                boxType = moov[pos + 4:pos + 8]
                if size < 8 or pos + size > len(moov):
                    break
                content = moov[pos + 8:pos + size]
                if boxType == b"mvhd":
                    version = content[0]
                    if version == 1:
                        timescale = struct.unpack(">I", content[20:24])[0]
                        duration = struct.unpack(">Q", content[24:32])[0]
                    else:
                        timescale = struct.unpack(">I", content[12:16])[0]
                        duration = struct.unpack(">I", content[16:20])[0]
                    return duration / timescale if timescale else None
                pos += size
    except Exception:
        return None
    return None


def validate_video_file(video_path: str) -> dict:
    """Best-effort pre-upload check -- {"ok": True} or {"ok": False,
    "message": ...}. Server has the final say; this just catches the
    cheap, obvious rejects before spending time on an upload that
    would fail anyway."""
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        # Translators: Shown when a video file's format isn't supported. First {} is the extension (or "no extension"), second {} is the accepted list.
        return {
            "ok": False,
            "message": _("Unsupported video format ({}). Bluesky accepts: {}.").format(
                ext or _("no extension"), ", ".join(VIDEO_EXTENSIONS)
            ),
        }

    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        # Translators: Shown when a video file can't be read. {} is the error message.
        return {"ok": False, "message": _("Could not read file: {}").format(e)}
    if size > VIDEO_MAX_BYTES:
        # Translators: Shown when a video is too large. First {} is its size in MB, second {} is the limit in MB.
        return {
            "ok": False,
            "message": _("Video is {}MB, over Bluesky's current {}MB limit.").format(
                f"{size / 1_000_000:.0f}", f"{VIDEO_MAX_BYTES / 1_000_000:.0f}"
            ),
        }

    if ext in (".mp4", ".mov"):
        duration = _read_mp4_duration_seconds(video_path)
        if duration is not None and duration > VIDEO_MAX_DURATION_SECONDS:
            # Translators: Shown when a video is too long. First {} is its length in seconds, second {} is the limit in seconds.
            return {
                "ok": False,
                "message": _("Video is {}s long, over Bluesky's current {}s limit.").format(
                    f"{duration:.0f}", VIDEO_MAX_DURATION_SECONDS
                ),
            }

    return {"ok": True}


def _get_pds_service_did(client) -> str:
    """
    The user's own PDS service DID (did:web:<pds-domain>), used as
    getServiceAuth's `aud` for uploadVideo -- NOT video.bsky.app's
    (that gives a 401). Read from the session's pds_endpoint.
    """
    session = getattr(client, "_session", None)
    pdsEndpoint = getattr(session, "pds_endpoint", None) if session is not None else None
    if not pdsEndpoint:
        pdsEndpoint = "https://bsky.social"  # SDK's own documented default
    pdsHost = urllib.parse.urlparse(pdsEndpoint).netloc
    return f"did:web:{pdsHost}"


def _get_upload_video_auth(client) -> str:
    """
    Service auth for app.bsky.video.uploadVideo only: aud is the user's own PDS (not video.bsky.app)
    and lxm is always "com.atproto.repo.uploadBlob". getUploadLimits/getJobStatus need a different
    aud/lxm, see _get_video_query_auth.
    """
    audDid = _get_pds_service_did(client)
    resp = client.com.atproto.server.get_service_auth(
        params={"aud": audDid, "lxm": "com.atproto.repo.uploadBlob"}
    )
    return resp.token


def _get_video_query_auth(client, lxm: str) -> str:
    """
    Service auth for app.bsky.video.getUploadLimits / getJobStatus: aud is video.bsky.app itself
    (not the PDS, unlike uploadVideo) and lxm is the endpoint being called.
    """
    resp = client.com.atproto.server.get_service_auth(
        params={"aud": "did:web:video.bsky.app", "lxm": lxm}
    )
    return resp.token


def get_video_upload_limits(client) -> dict:
    """Raw HTTP to video.bsky.app. Returns {"canUpload", "message",
    "remainingDailyBytes", "remainingDailyVideos"}."""
    token = _get_video_query_auth(client, "app.bsky.video.getUploadLimits")
    request = urllib.request.Request(
        "https://video.bsky.app/xrpc/app.bsky.video.getUploadLimits",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def upload_video(client, video_path: str, progress_callback=None, cancel_event=None) -> dict:
    """
    Uploads a video and polls until Bluesky's processing job finishes,
    returning the blob dict (same shape upload_blob_dict returns for
    images -- usable directly in a post embed).

    Raw HTTP (uploadVideo + getJobStatus), not the typed SDK.

    progress_callback(state, extra), if given, is called with
    "uploading" once the file is sent, then the job's own state string
    (e.g. "JOB_STATE_ENCODING") on every poll -- no byte-level upload
    progress is available. cancel_event (threading.Event), if given,
    is checked between polls; the initial upload POST itself can't be
    interrupted mid-flight since it's one blocking call, only the
    polling phase can actually stop.
    """
    ext = os.path.splitext(video_path)[1].lower()
    mimeType = _VIDEO_MIME_TYPES.get(ext, "video/mp4")

    token = _get_upload_video_auth(client)
    with open(video_path, "rb") as f:
        videoBytes = f.read()

    if progress_callback:
        progress_callback("uploading", None)

    params = urllib.parse.urlencode({"did": client.me.did, "name": os.path.basename(video_path)})
    request = urllib.request.Request(
        f"https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?{params}",
        data=videoBytes, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": mimeType,
            "Content-Length": str(len(videoBytes)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            jobStatus = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Uploading the same video bytes again isn't a failure: the server answers 409 with a
            # JOB_STATE_COMPLETED job status (and a usable blob) in the error body. HTTPError is
            # readable as a response here.
            jobStatus = json.loads(e.read().decode("utf-8"))
        else:
            raise

    if cancel_event is not None and cancel_event.is_set():
        raise VideoUploadCancelled()

    blob = jobStatus.get("blob")
    if blob:
        return blob
    jobId = jobStatus.get("jobId")
    if not jobId:
        # Translators: Shown when the server's video upload response has no blob and no job ID.
        raise RuntimeError(_("Video upload returned neither a blob nor a job ID."))

    pollToken = _get_video_query_auth(client, "app.bsky.video.getJobStatus")
    pollParams = urllib.parse.urlencode({"jobId": jobId})
    pollUrl = f"https://video.bsky.app/xrpc/app.bsky.video.getJobStatus?{pollParams}"

    for _attempt in range(200):  # ~10 min at 3s intervals
        if cancel_event is not None and cancel_event.is_set():
            raise VideoUploadCancelled()
        time.sleep(3)
        pollRequest = urllib.request.Request(pollUrl, headers={"Authorization": f"Bearer {pollToken}"})
        with urllib.request.urlopen(pollRequest, timeout=15) as response:
            status = json.loads(response.read().decode("utf-8"))
        jobStatus = status.get("jobStatus", status)
        state = jobStatus.get("state", "")
        if progress_callback:
            progress_callback(state, jobStatus.get("progress"))
        if state == "JOB_STATE_COMPLETED":
            blob = jobStatus.get("blob")
            if blob:
                return blob
            # Translators: Shown when video processing finished but the server returned no video.
            raise RuntimeError(_("Video processing completed but returned no blob."))
        if state == "JOB_STATE_FAILED":
            # Translators: Fallback shown when video processing fails with no server message.
            raise RuntimeError(jobStatus.get("error") or jobStatus.get("message") or _("Video processing failed."))

    # Translators: Shown when video processing takes too long.
    raise RuntimeError(_("Video processing timed out."))
