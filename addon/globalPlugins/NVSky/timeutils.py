"""
Shared timestamp formatting for NVSky.

Server timestamps (a post's `indexedAt`, a message's `sentAt`, etc.) are
always UTC ISO 8601 (suffix `Z`). Any column that shows an absolute
point in time must convert that UTC value to the user's local system
timezone before displaying it -- relative text like "5 minutes ago"
does NOT need this, since it's a time difference and is timezone
independent either way.

This also reads the user's Settings > Display time-format choice
(relative_24h / relative_always / absolute / custom) so posts,
notifications, and chat messages all honor it. Used by feedWindow.py and
chatWindow.py; don't duplicate it there.
"""
import datetime

# In-memory cache of the Settings > Display time-format choice: reading it
# from the DB per rendered row was the biggest cost of the MainWindow-open
# freeze. Invalidated on save (DisplayPanel.apply).
_cache = None


def current_mode_and_pattern(db_module):
    """Settings > Display time-format choice as (mode, custom_pattern); `db_module` is the caller's db module. Cached (see _cache)."""
    global _cache
    if _cache is None:
        mode = db_module.get_ui_state("time_format_mode") or "relative_24h"
        pattern = db_module.get_ui_state("time_format_custom_pattern") if mode == "custom" else None
        _cache = (mode, pattern)
    return _cache


def invalidate_time_format_cache():
    """Call after writing time_format_mode/time_format_custom_pattern (DisplayPanel.apply)."""
    global _cache
    _cache = None


def format_timestamp(iso_timestamp: str, mode: str = "relative_24h", custom_pattern: str = None) -> str:
    if not iso_timestamp:
        return ""
    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return iso_timestamp

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    now = datetime.datetime.now(datetime.timezone.utc)
    seconds = max((now - dt).total_seconds(), 0)

    # Absolute times are shown in the OS's local timezone (astimezone() needs no extra dependency).
    dtLocal = dt.astimezone()

    if mode == "absolute":
        return dtLocal.strftime("%Y-%m-%d %H:%M:%S")
    if mode == "custom":
        pattern = custom_pattern or "%Y-%m-%d %H:%M:%S"
        try:
            return dtLocal.strftime(pattern)
        except ValueError:
            return dtLocal.strftime("%Y-%m-%d %H:%M:%S")

    capAt24h = mode != "relative_always"
    return _relative_string(seconds, dtLocal, capAt24h)


def _relative_string(seconds: float, dtLocal: datetime.datetime, capAt24h: bool) -> str:
    if seconds < 60:
        # Translators: Relative timestamp for under a minute old.
        return _("just now")
    if seconds < 3600:
        minutes = int(seconds // 60)
        if minutes == 1:
            # Translators: Relative timestamp, exactly one minute old.
            return _("1 minute ago")
        # Translators: Relative timestamp, several minutes old. {} is the count.
        return _("{} minutes ago").format(minutes)
    if seconds < 86400:
        hours = int(seconds // 3600)
        if hours == 1:
            # Translators: Relative timestamp, exactly one hour old.
            return _("1 hour ago")
        # Translators: Relative timestamp, several hours old. {} is the count.
        return _("{} hours ago").format(hours)
    if capAt24h:
        return dtLocal.strftime("%Y-%m-%d %H:%M:%S")

    days = int(seconds // 86400)
    if days < 30:
        if days == 1:
            # Translators: Relative timestamp, exactly one day old.
            return _("1 day ago")
        # Translators: Relative timestamp, several days old. {} is the count.
        return _("{} days ago").format(days)
    months = int(days // 30)
    if months < 12:
        if months == 1:
            # Translators: Relative timestamp, exactly one month old.
            return _("1 month ago")
        # Translators: Relative timestamp, several months old. {} is the count.
        return _("{} months ago").format(months)
    years = int(days // 365)
    if years == 1:
        # Translators: Relative timestamp, exactly one year old.
        return _("1 year ago")
    # Translators: Relative timestamp, several years old. {} is the count.
    return _("{} years ago").format(years)
