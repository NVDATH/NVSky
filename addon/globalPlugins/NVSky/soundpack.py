"""
SoundPack playback engine for NVSky.

Scans globalPlugins/NVSky/SoundPack/<PackName>/ for named .wav files
matching known event keys, and plays them via nvwave.playWaveFile().

Two playback modes:
 - discrete events (EVENT_KEYS): one-shot sound for a specific action
   (like, follow, error, etc). Missing file -- silent, no beep
   fallback, since silence is a deliberate valid choice here.
 - progress indicator (PROGRESS_KEY): looped sound for a background
   wait (fetching, checking for updates), same pattern as YoutubePlus's
   own progress indicator -- falls back to tones.beep() if the pack
   has no progress sound, since silence during a genuine wait reads
   as a freeze.
"""
import os
import threading

import tones
import nvwave
from logHandler import log

from . import db

SOUND_PACK_DIR = os.path.join(os.path.dirname(__file__), "SoundPack")

# Every discrete (one-shot) event this add-on can play a sound for.
# Filenames inside a pack folder must match these exactly: "like.wav",
# "error.wav", etc. A pack doesn't need to provide all of them --
# missing files just stay silent.
EVENT_KEYS = [
    "like", "unlike", "repost", "unrepost", "save", "unsave",
    "send_post", "delete",
    "follow", "unfollow", "block_mute",
    "send_message", "new_message",
    "notification",
    "open_tab", "close_tab", "boundary", "error", "ready",
    "embed_image", "embed_video", "embed_link", "embed_quote",
    "max_length",
]

# The looped progress-indicator sound -- played on repeat while
# something loads, distinct from the one-shot EVENT_KEYS above.
PROGRESS_KEY = "progress"

SILENT_PACK = ""  # sentinel for "Silent / No sound" in the picker


def list_packs() -> list:
    """Every subfolder of SoundPack/ -- each one a selectable pack.
    Does NOT include the "Silent" option itself; callers add that."""
    if not os.path.isdir(SOUND_PACK_DIR):
        return []
    try:
        return sorted(
            name for name in os.listdir(SOUND_PACK_DIR)
            if os.path.isdir(os.path.join(SOUND_PACK_DIR, name))
        )
    except OSError as e:
        log.error(f"NVSky: failed to list sound packs: {e}")
        return []


def _pack_dir(pack_name: str) -> str:
    return os.path.join(SOUND_PACK_DIR, pack_name)


def _sound_path(pack_name: str, event_key: str):
    if not pack_name or pack_name == SILENT_PACK:
        return None
    path = os.path.join(_pack_dir(pack_name), f"{event_key}.wav")
    return path if os.path.isfile(path) else None


class SoundEngine:
    """
    One instance, lives for the add-on's whole session (see module-level
    get_engine() below). Caches which event keys actually have a sound
    file in the currently selected pack, so play() doesn't hit the
    filesystem on every call -- rebuilt via reload() whenever the
    selected pack or enabled-events list changes in Settings.
    """

    def __init__(self):
        self._enabledEvents = set()
        self._availablePaths = {}  # event_key -> path, only for files that exist
        self._progressPath = None
        self._progressStopEvent = threading.Event()
        self._progressThread = None
        self._debounceTimer = None
        self.reload()

    def reload(self):
        packName = db.get_soundpack_selected()
        disabled = db.get_soundpack_disabled_events()
        self._enabledEvents = set(EVENT_KEYS) - disabled

        self._availablePaths = {}
        if packName and packName != SILENT_PACK:
            for key in EVENT_KEYS:
                path = _sound_path(packName, key)
                if path:
                    self._availablePaths[key] = path
            self._progressPath = _sound_path(packName, PROGRESS_KEY)
        else:
            self._progressPath = None

    def play(self, event_key: str):
        """One-shot sound for `event_key`. Silent (no beep) if the
        pack is Silent, the event is disabled, or the pack simply has
        no file for this event."""
        if event_key not in self._enabledEvents:
            return
        path = self._availablePaths.get(event_key)
        if not path:
            return
        try:
            nvwave.playWaveFile(path)
        except Exception as e:
            log.error(f"NVSky: failed to play sound {path}: {e}")

    def play_debounced(self, event_key: str, delay_ms: int = 150):
        """
        Same as play(), but delayed and cancellable -- used for
        focus-driven sounds (embed type on arrow-key navigation) where
        firing on every single row during fast scrolling would produce
        overlapping/garbled sound spam. Each call cancels any pending
        call from a PREVIOUS play_debounced() (any event_key, not just
        the same one) -- only the row the user actually settles on
        should ever produce a sound. Fires on a background timer
        thread, not wx's main thread, so it never blocks NVDA's own
        speech for the row.
        """
        if event_key not in self._enabledEvents:
            return
        path = self._availablePaths.get(event_key)
        if not path:
            return
        if self._debounceTimer is not None:
            self._debounceTimer.cancel()
        self._debounceTimer = threading.Timer(delay_ms / 1000.0, self._play_debounced_fire, args=(path,))
        self._debounceTimer.daemon = True
        self._debounceTimer.start()

    def _play_debounced_fire(self, path):
        try:
            nvwave.playWaveFile(path)
        except Exception as e:
            log.error(f"NVSky: failed to play sound {path}: {e}")

    # ---------------- progress indicator (looped) ----------------

    def start_progress(self):
        """Starts the looped 'still working' indicator. No-op if
        already running -- callers don't need to track state themselves."""
        if self._progressThread is not None and self._progressThread.is_alive():
            return
        self._progressStopEvent.clear()
        progressPath = self._progressPath
        self._progressThread = threading.Thread(
            target=self._progress_worker, args=(progressPath,), daemon=True
        )
        self._progressThread.start()

    def stop_progress(self):
        self._progressStopEvent.set()

    def _progress_worker(self, progress_path):
        useBeepFallback = not progress_path
        while not self._progressStopEvent.is_set():
            if useBeepFallback:
                tones.beep(500, 50)
                self._progressStopEvent.wait(1.0)
            else:
                try:
                    nvwave.playWaveFile(progress_path)
                except Exception as e:
                    log.error(f"NVSky: failed to play progress sound {progress_path}: {e}")
                    useBeepFallback = True
                    continue
                self._progressStopEvent.wait(1.0)


_engine = None


def get_engine() -> SoundEngine:
    global _engine
    if _engine is None:
        _engine = SoundEngine()
    return _engine


def play(event_key: str):
    """Module-level convenience -- most call sites just want
    soundpack.play("like") without holding a reference to the engine."""
    get_engine().play(event_key)


def play_debounced(event_key: str, delay_ms: int = 150):
    get_engine().play_debounced(event_key, delay_ms)


def start_progress():
    get_engine().start_progress()


def stop_progress():
    get_engine().stop_progress()


def reload():
    get_engine().reload()