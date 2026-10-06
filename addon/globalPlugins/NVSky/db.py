"""
SQLite persistence layer for NVSky. See init_db() for schema.
The database file lives under NVDA's user config directory, NOT inside the
add-on folder (the add-on folder gets overwritten on every update).

Uses sqlcipher3 (not plain sqlite3) for full database encryption. Its
compiled extension bundles SQLCipher+OpenSSL statically, so no
os.add_dll_directory() trick is needed (that trick is process-wide and
let other add-ons' sqlite3.dll collide with ours).

The DB encryption master key is a random 32-byte value, generated once and
stored -- encrypted via the same DPAPI helpers as the App Password
(crypto.py) -- in a small file next to the database. It can't live inside
the database itself (can't read the key from the db you need the key to
open).
"""

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager

from sqlcipher3 import dbapi2 as sqlite3

import globalVars
from logHandler import log

from . import crypto

DB_DIR = os.path.join(globalVars.appArgs.configPath, "NVSky")
DB_PATH = os.path.join(DB_DIR, "nvsky.db")
DB_KEY_PATH = os.path.join(DB_DIR, "db.key")


def _get_or_create_db_key() -> str:
    """64-char hex string (32-byte key), DPAPI-encrypted at rest."""
    if os.path.exists(DB_KEY_PATH):
        with open(DB_KEY_PATH, "rb") as f:
            encrypted = f.read()
        return crypto.decrypt(encrypted)

    key_hex = secrets.token_hex(32)
    os.makedirs(DB_DIR, exist_ok=True)
    with open(DB_KEY_PATH, "wb") as f:
        f.write(crypto.encrypt(key_hex))
    return key_hex


_local = threading.local()
_reset_lock = threading.Lock()


def _is_unreadable_storage_error(e) -> bool:
    text = str(e)
    return "CryptUnprotectData" in text or "not a database" in text


def _backup_unreadable_storage():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm", DB_KEY_PATH):
        if os.path.exists(path):
            try:
                os.replace(path, f"{path}.{stamp}.bak")
            except OSError as e:
                log.error(f"NVSky: could not back up {path}: {e}")


def _open_connection():
    conn = sqlite3.connect(DB_PATH)
    try:
        dbKey = _get_or_create_db_key()
        conn.execute(f"PRAGMA key = \"x'{dbKey}'\"")
        conn.execute("SELECT count(*) FROM sqlite_master")
    except Exception:
        conn.close()
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _get_connection():
    # One connection per OS thread, reused for that thread's lifetime:
    # reopening per query (SQLCipher key unlock) caused the MainWindow-open
    # freeze. No lock needed: connections are check_same_thread=True and
    # threading.local() never shares them.
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(DB_DIR, exist_ok=True)
    try:
        conn = _open_connection()
    except Exception as e:
        if not _is_unreadable_storage_error(e):
            raise
        with _reset_lock:
            log.error(f"NVSky: database/key unreadable, starting fresh: {e}")
            _backup_unreadable_storage()
            conn = _open_connection()
    _local.conn = conn
    return conn


@contextmanager
def _connect():
    # Context manager kept for `with _connect() as conn:` call sites; it
    # doesn't close the connection (see _get_connection). Writers commit explicitly.
    yield _get_connection()


def _addon_version_number() -> int:
    # "1.2.3" -> 10203; 0 if unreadable (then no stamping, harmless)
    try:
        import addonHandler
        version = addonHandler.getCodeAddon().manifest["version"]
        parts = [int(p) for p in re.findall(r"\d+", version)[:3]]
        parts += [0] * (3 - len(parts))
        return parts[0] * 10000 + parts[1] * 100 + parts[2]
    except Exception:
        return 0


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                handle TEXT NOT NULL,
                did TEXT NOT NULL UNIQUE,
                encrypted_password BLOB NOT NULL,
                is_active INTEGER DEFAULT 0,
                chat_supported INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS authors (
                did TEXT PRIMARY KEY,
                handle TEXT,
                display_name TEXT,
                avatar_url TEXT,
                viewer_following TEXT,
                viewer_muted INTEGER DEFAULT 0,
                viewer_blocking TEXT,
                viewer_activity_subscription INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS posts (
                uri TEXT PRIMARY KEY,
                cid TEXT NOT NULL,
                account_id INTEGER,
                author_did TEXT,
                text TEXT,
                created_at TEXT,
                indexed_at TEXT,
                like_count INTEGER DEFAULT 0,
                repost_count INTEGER DEFAULT 0,
                reply_count INTEGER DEFAULT 0,
                reply_parent_uri TEXT,
                reply_to_did TEXT,
                reply_to_handle TEXT,
                is_repost INTEGER DEFAULT 0,
                reposted_by_did TEXT,
                reposted_by_handle TEXT,
                reposted_by_display_name TEXT,
                embed_json TEXT,
                facets_json TEXT,
                quoted_text TEXT,
                quoted_author_handle TEXT,
                is_read INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                viewer_like_uri TEXT,
                viewer_repost_uri TEXT,
                viewer_bookmarked INTEGER DEFAULT 0,
                viewer_thread_muted INTEGER DEFAULT 0,
                labels_json TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                FOREIGN KEY (author_did) REFERENCES authors(did)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_indexed_at ON posts(indexed_at);

            CREATE TABLE IF NOT EXISTS feed_items (
                account_id INTEGER,
                feed_key TEXT,
                uri TEXT,
                indexed_at TEXT,
                PRIMARY KEY (account_id, feed_key, uri),
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                FOREIGN KEY (uri) REFERENCES posts(uri) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_feed_items_lookup ON feed_items(account_id, feed_key, indexed_at);

            CREATE TABLE IF NOT EXISTS notifications (
                account_id INTEGER,
                uri TEXT,
                cid TEXT,
                reason TEXT,
                reason_subject TEXT,
                subject_uri TEXT,
                author_did TEXT,
                indexed_at TEXT,
                is_read INTEGER DEFAULT 0,
                subject_text TEXT,
                subject_author_handle TEXT,
                PRIMARY KEY (account_id, uri)
            );

            CREATE INDEX IF NOT EXISTS idx_notifications_indexed_at ON notifications(account_id, indexed_at);

            CREATE TABLE IF NOT EXISTS convos (
                account_id INTEGER NOT NULL,
                convo_id TEXT NOT NULL,
                is_group INTEGER DEFAULT 0,
                group_name TEXT,
                locked INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                last_message_text TEXT,
                last_message_sent_at TEXT,
                unread_count INTEGER DEFAULT 0,
                muted INTEGER DEFAULT 0,
                status TEXT DEFAULT 'accepted',
                unread_join_request_count INTEGER DEFAULT 0,
                PRIMARY KEY (account_id, convo_id)
            );
            CREATE INDEX IF NOT EXISTS idx_convos_last_message ON convos(account_id, last_message_sent_at);

            CREATE TABLE IF NOT EXISTS convo_members (
                account_id INTEGER NOT NULL,
                convo_id TEXT NOT NULL,
                did TEXT NOT NULL,
                handle TEXT,
                display_name TEXT,
                PRIMARY KEY (account_id, convo_id, did)
            );

            CREATE TABLE IF NOT EXISTS messages (
                account_id INTEGER NOT NULL,
                convo_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sender_did TEXT,
                text TEXT,
                sent_at TEXT,
                reply_to_message_id TEXT,
                reply_to_text TEXT,
                is_read INTEGER DEFAULT 0,
                reactions_json TEXT,
                embed_json TEXT,
                PRIMARY KEY (account_id, convo_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(account_id, convo_id, sent_at);
            CREATE TABLE IF NOT EXISTS lists (
                account_id INTEGER NOT NULL,
                list_uri TEXT NOT NULL,
                cid TEXT,
                name TEXT,
                description TEXT,
                purpose TEXT,
                creator_did TEXT,
                creator_handle TEXT,
                muted INTEGER DEFAULT 0,
                blocked_uri TEXT,
                PRIMARY KEY (account_id, list_uri)
            );

            CREATE TABLE IF NOT EXISTS ui_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Steps are keyed by the release that introduced them (1.2.0 -> 10200)
        # and must be idempotent, e.g. "if stored < 10200: ALTER TABLE ...".
        stored = conn.execute("PRAGMA user_version").fetchone()[0]
        current = _addon_version_number()
        if current > stored:
            conn.execute(f"PRAGMA user_version = {current}")
        conn.commit()
    log.info(f"NVSky: database ready at {DB_PATH}")


# ---------------- accounts ----------------

def upsert_account(handle: str, did: str, encrypted_password: bytes) -> int:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO accounts (handle, did, encrypted_password)
               VALUES (?, ?, ?)
               ON CONFLICT(did) DO UPDATE SET
                   handle=excluded.handle,
                   encrypted_password=excluded.encrypted_password""",
            (handle, did, encrypted_password),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM accounts WHERE did = ?", (did,)).fetchone()
        return row["id"]


def set_active_account(account_id: int):
    with _connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 0")
        conn.execute("UPDATE accounts SET is_active = 1 WHERE id = ?", (account_id,))
        conn.commit()


def get_active_account():
    with _connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE is_active = 1").fetchone()
        return dict(row) if row else None


def get_all_accounts():
    with _connect() as conn:
        rows = conn.execute("SELECT id, handle, did, is_active FROM accounts").fetchall()
        return [dict(r) for r in rows]


def remove_account(account_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        # Only posts has a cascading FK to accounts; delete the rest explicitly.
        conn.execute("DELETE FROM convos WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM messages WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM convo_members WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM notifications WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM lists WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM ui_state WHERE key LIKE ?", (f"%:{account_id}",))
        conn.execute("DELETE FROM ui_state WHERE key LIKE ?", (f"user_list_cache:{account_id}:%",))
        conn.commit()
        conn.execute("VACUUM")


def set_chat_supported(account_id: int, supported: bool):
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET chat_supported = ? WHERE id = ?",
            (int(supported), account_id),
        )
        conn.commit()


def close_all_connections():
    """
    Checkpoints the WAL and closes the CURRENT thread's connection. Called
    from terminate() (main thread) and at the end of background workers.
    Other threads' connections can't be closed from here
    (check_same_thread), so a sync mid-flight at shutdown keeps its file
    handle until the process exits; disable the add-on before copying the
    NVDA profile.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except Exception as e:
        log.error(f"NVSky: WAL checkpoint on shutdown failed: {e}")
    try:
        conn.close()
    except Exception as e:
        log.error(f"NVSky: closing DB connection on shutdown failed: {e}")
    _local.conn = None


def clear_all_cache(account_id: int):
    """
    Settings > General "Clear all cache": like remove_account but keeps
    the accounts row (stays logged in).
    """
    with _connect() as conn:
        # posts cascades into feed_items via its FK.
        conn.execute("DELETE FROM posts WHERE account_id = ?", (account_id,))
        # These have no FK to accounts; delete explicitly.
        conn.execute("DELETE FROM notifications WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM convos WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM messages WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM convo_members WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM lists WHERE account_id = ?", (account_id,))
        # ui_state has no account_id column; per-account keys end with
        # ":<id>", except "user_list_cache:<id>:<key>".
        conn.execute("DELETE FROM ui_state WHERE key LIKE ?", (f"%:{account_id}",))
        conn.execute("DELETE FROM ui_state WHERE key LIKE ?", (f"user_list_cache:{account_id}:%",))
        conn.commit()
        conn.execute("VACUUM")


# ---------------- authors ----------------

def upsert_author(did: str, handle: str, display_name: str, avatar_url: str,
                   viewer_following=None, viewer_muted=False, viewer_blocking=None,
                   viewer_activity_subscription=False):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO authors (did, handle, display_name, avatar_url, viewer_following, viewer_muted, viewer_blocking, viewer_activity_subscription)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(did) DO UPDATE SET
                   handle=excluded.handle,
                   display_name=excluded.display_name,
                   avatar_url=excluded.avatar_url,
                   viewer_following=excluded.viewer_following,
                   viewer_muted=excluded.viewer_muted,
                   viewer_blocking=excluded.viewer_blocking,
                   viewer_activity_subscription=excluded.viewer_activity_subscription""",
            (did, handle, display_name, avatar_url, viewer_following, int(bool(viewer_muted)), viewer_blocking,
             int(bool(viewer_activity_subscription))),
        )
        conn.commit()


def get_author(did: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM authors WHERE did = ?", (did,)).fetchone()
        return dict(row) if row else None


def set_author_following(did: str, following_uri):
    with _connect() as conn:
        conn.execute("UPDATE authors SET viewer_following = ? WHERE did = ?", (following_uri, did))
        conn.commit()


def set_author_muted(did: str, muted: bool):
    with _connect() as conn:
        conn.execute("UPDATE authors SET viewer_muted = ? WHERE did = ?", (1 if muted else 0, did))
        conn.commit()


def set_author_blocking(did: str, blocking_uri):
    with _connect() as conn:
        conn.execute("UPDATE authors SET viewer_blocking = ? WHERE did = ?", (blocking_uri, did))
        conn.commit()


def set_author_activity_subscription(did: str, subscribed: bool):
    with _connect() as conn:
        conn.execute(
            "UPDATE authors SET viewer_activity_subscription = ? WHERE did = ?",
            (1 if subscribed else 0, did),
        )
        conn.commit()


RELATIONSHIP_LIST_KINDS = ("following", "followers", "known_followers", "muted", "blocked", "subscriptions")


def cache_relationship_authors(users: list, kind: str):
    """Stores each user's relationship state (from client._actor_dict) so
    Alt+U shows truthful labels. Skips lists whose entries carry no state."""
    if kind not in RELATIONSHIP_LIST_KINDS:
        return
    for u in users:
        old = get_author(u["did"]) or {}
        subscribed = True if kind == "subscriptions" else bool(old.get("viewer_activity_subscription"))
        upsert_author(
            did=u["did"], handle=u["handle"], display_name=u.get("display_name"),
            avatar_url=old.get("avatar_url"), viewer_following=u.get("following_uri"),
            viewer_muted=u.get("muted", False), viewer_blocking=u.get("blocking_uri"),
            viewer_activity_subscription=subscribed,
        )


# ---------------- posts ----------------

def upsert_post(post: dict):
    """is_read/is_hidden stay out of ON CONFLICT SET -- local state, never overwritten by re-sync."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO posts
               (uri, cid, account_id, author_did, text, created_at, indexed_at,
                like_count, repost_count, reply_count, reply_parent_uri,
                reply_to_did, reply_to_handle, is_repost, reposted_by_did, reposted_by_handle, reposted_by_display_name,
                embed_json, facets_json, quoted_text, quoted_author_handle, viewer_like_uri, viewer_repost_uri,
                viewer_bookmarked, viewer_thread_muted, labels_json)
               VALUES (:uri, :cid, :account_id, :author_did, :text, :created_at, :indexed_at,
                       :like_count, :repost_count, :reply_count, :reply_parent_uri,
                       :reply_to_did, :reply_to_handle, :is_repost, :reposted_by_did, :reposted_by_handle, :reposted_by_display_name,
                       :embed_json, :facets_json, :quoted_text, :quoted_author_handle, :viewer_like_uri, :viewer_repost_uri,
                       :viewer_bookmarked, :viewer_thread_muted, :labels_json)
               ON CONFLICT(uri) DO UPDATE SET
                   text=excluded.text,
                   created_at=excluded.created_at,
                   indexed_at=excluded.indexed_at,
                   like_count=excluded.like_count,
                   repost_count=excluded.repost_count,
                   reply_count=excluded.reply_count,
                   reply_parent_uri=excluded.reply_parent_uri,
                   reply_to_did=excluded.reply_to_did,
                   reply_to_handle=excluded.reply_to_handle,
                   is_repost=excluded.is_repost,
                   reposted_by_handle=excluded.reposted_by_handle,
                   reposted_by_display_name=excluded.reposted_by_display_name,
                   reposted_by_did=excluded.reposted_by_did,
                   embed_json=excluded.embed_json,
                   facets_json=excluded.facets_json,
                   quoted_text=excluded.quoted_text,
                   quoted_author_handle=excluded.quoted_author_handle,
                   viewer_like_uri=excluded.viewer_like_uri,
                   viewer_repost_uri=excluded.viewer_repost_uri,
                   viewer_bookmarked=excluded.viewer_bookmarked,
                   viewer_thread_muted=excluded.viewer_thread_muted,
                   labels_json=excluded.labels_json""",
            post,
        )
        conn.commit()


def upsert_feed_item(account_id: int, feed_key: str, uri: str, indexed_at: str):
    """Records that `uri` belongs to `feed_key` for this account, at
    position `indexed_at`. Separate from upsert_post so the same post
    can independently belong to several feeds (Home, a custom Feed, a
    List, ...) without the feeds colliding on the post's own PK."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO feed_items (account_id, feed_key, uri, indexed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account_id, feed_key, uri) DO UPDATE SET
                   indexed_at=excluded.indexed_at""",
            (account_id, feed_key, uri, indexed_at),
        )
        conn.commit()


def clear_feed_key_cache(account_id: int, feed_key: str):
    """
    Removes every feed_items row for one feed_key (Ctrl+Delete in a tab).
    Leaves the shared posts table alone; other feeds may reference those rows.
    """
    with _connect() as conn:
        conn.execute(
            "DELETE FROM feed_items WHERE account_id = ? AND feed_key = ?",
            (account_id, feed_key),
        )
        conn.commit()


def delete_feed_item(account_id: int, feed_key: str, uri: str):
    """Reverse of upsert_feed_item. Needed where items can disappear
    (e.g. Saved after unbookmarking): a sync only adds/updates rows."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM feed_items WHERE account_id = ? AND feed_key = ? AND uri = ?",
            (account_id, feed_key, uri),
        )
        conn.commit()


def get_feed_page(account_id: int, feed_key: str, before_indexed_at: str = None, limit: int = None):
    query = """SELECT p.*, f.indexed_at AS feed_indexed_at, a.handle, a.display_name, a.avatar_url FROM feed_items f
               JOIN posts p ON p.uri = f.uri
               LEFT JOIN authors a ON p.author_did = a.did
               WHERE f.account_id = ? AND f.feed_key = ? AND p.is_hidden = 0"""
    params = [account_id, feed_key]
    if before_indexed_at is not None:
        query += " AND f.indexed_at < ?"
        params.append(before_indexed_at)
    query += " ORDER BY f.indexed_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_post(uri: str):
    """Fetches one post by uri, joined with its author -- same row
    shape as get_feed_page() minus feed_indexed_at. Used by Post action
    (Alt+A) on NotificationsWindow to resolve a notification's
    actionable post from the local cache."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT p.*, a.handle, a.display_name, a.avatar_url FROM posts p
               LEFT JOIN authors a ON p.author_did = a.did
               WHERE p.uri = ?""",
            (uri,),
        ).fetchone()
        return dict(row) if row else None


def upsert_convo(convo: dict):
    # Plain overwrite of every field: client._store_convo derives them reliably each sync.
    with _connect() as conn:
        conn.execute(
            """INSERT INTO convos
               (account_id, convo_id, is_group, group_name, locked, is_admin,
                last_message_text, last_message_sent_at, unread_count, muted, status,
                unread_join_request_count)
               VALUES (:account_id, :convo_id, :is_group, :group_name, :locked, :is_admin,
                       :last_message_text, :last_message_sent_at, :unread_count, :muted, :status,
                       :unread_join_request_count)
               ON CONFLICT(account_id, convo_id) DO UPDATE SET
                   is_group=excluded.is_group,
                   group_name=excluded.group_name,
                   locked=excluded.locked,
                   is_admin=excluded.is_admin,
                   last_message_text=excluded.last_message_text,
                   last_message_sent_at=excluded.last_message_sent_at,
                   unread_count=excluded.unread_count,
                   unread_join_request_count=excluded.unread_join_request_count,
                   muted=excluded.muted,
                   status=excluded.status""",
            convo,
        )
        conn.commit()


def replace_convo_members(account_id: int, convo_id: str, members: list):
    """
    Delete-then-insert sync of a convo's OTHER members (never includes
    the active account itself) -- simpler than diffing since group
    membership lists are small. Called every time a convo is stored
    (client._store_convo, i.e. every sync_convos/sync_convo_messages).
    """
    with _connect() as conn:
        conn.execute(
            "DELETE FROM convo_members WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        )
        for m in members:
            conn.execute(
                """INSERT INTO convo_members (account_id, convo_id, did, handle, display_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (account_id, convo_id, m["did"], m.get("handle"), m.get("display_name")),
            )
        conn.commit()


def get_convo_members(account_id: int, convo_id: str):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT did, handle, display_name FROM convo_members WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        ).fetchall()
        return [dict(r) for r in rows]


def describe_convo_from_members(convo: dict, members: list) -> str:
    """
    Display name for a convo from an already-fetched member list. No DB
    access on purpose: querying per convo caused an N+1 freeze.
    """
    if convo.get("is_group"):
        if convo.get("group_name"):
            return convo["group_name"]
        names = [m.get("display_name") or m.get("handle") or "?" for m in members]
        # Translators: Fallback display name for a group conversation with no name and no members cached.
        return ", ".join(names) if names else _("Group")
    other = members[0] if members else None
    if other is None:
        # Translators: Fallback display name for a 1:1 conversation with no member cached.
        return _("Conversation")
    # Translators: Fallback display name for a 1:1 conversation whose member has neither a display name nor a handle cached.
    return other.get("display_name") or other.get("handle") or _("Conversation")


def get_convos(account_id: int):
    # NULL last_message_sent_at (e.g. a new group with no messages) sorts
    # lowest under DESC; the CASE puts those convos first instead.
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM convos WHERE account_id = ?
               ORDER BY CASE WHEN last_message_sent_at IS NULL THEN 0 ELSE 1 END,
                        last_message_sent_at DESC""",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_convo(account_id: int, convo_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM convos WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        ).fetchone()
        return dict(row) if row else None


def mark_convo_read_local(account_id: int, convo_id: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET unread_count = 0 WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        )
        conn.commit()


def set_convo_unread_count(account_id: int, convo_id: str, unread_count: int):
    """
    Keeps the cached convos.unread_count column in sync with local read
    progress pushed to the server one message at a time (see
    chatWindow.py's _pushMessageReadToServer). Without this,
    reconcile_message_read_state() on the NEXT sync re-derives every
    message's is_read from a STALE, too-high unread_count still sitting
    in this column -- confirmed as the cause of "read locally, but a
    refresh brings the unread count back."
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET unread_count = ? WHERE account_id = ? AND convo_id = ?",
            (unread_count, account_id, convo_id),
        )
        conn.commit()


def delete_convo(account_id: int, convo_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM convos WHERE account_id = ? AND convo_id = ?", (account_id, convo_id))
        conn.execute("DELETE FROM messages WHERE account_id = ? AND convo_id = ?", (account_id, convo_id))
        conn.execute("DELETE FROM convo_members WHERE account_id = ? AND convo_id = ?", (account_id, convo_id))
        conn.commit()


def set_convo_group_name(account_id: int, convo_id: str, name: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET group_name = ? WHERE account_id = ? AND convo_id = ?",
            (name, account_id, convo_id),
        )
        conn.commit()


def set_convo_locked(account_id: int, convo_id: str, locked: bool):
    # Local-first write for ChatWindow._setGroupLocked's optimistic UI
    # -- the authoritative value still comes from the server via the
    # normal convo-sync path, this just lets the UI reflect the
    # requested state immediately instead of waiting on it.
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET locked = ? WHERE account_id = ? AND convo_id = ?",
            (int(locked), account_id, convo_id),
        )
        conn.commit()


def set_convo_muted(account_id: int, convo_id: str, muted: bool):
    # Local-first write for ChatWindow._toggleMuteConvo's optimistic UI.
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET muted = ? WHERE account_id = ? AND convo_id = ?",
            (int(muted), account_id, convo_id),
        )
        conn.commit()


def set_convo_status(account_id: int, convo_id: str, status: str):
    # Local-first write for ChatWindow._acceptConvo's optimistic UI.
    with _connect() as conn:
        conn.execute(
            "UPDATE convos SET status = ? WHERE account_id = ? AND convo_id = ?",
            (status, account_id, convo_id),
        )
        conn.commit()


def upsert_list(list_row: dict):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO lists
               (account_id, list_uri, cid, name, description, purpose,
                creator_did, creator_handle, muted, blocked_uri)
               VALUES (:account_id, :list_uri, :cid, :name, :description, :purpose,
                       :creator_did, :creator_handle, :muted, :blocked_uri)
               ON CONFLICT(account_id, list_uri) DO UPDATE SET
                   cid=excluded.cid,
                   name=excluded.name,
                   description=excluded.description,
                   purpose=excluded.purpose,
                   creator_did=excluded.creator_did,
                   creator_handle=excluded.creator_handle,
                   muted=excluded.muted,
                   blocked_uri=excluded.blocked_uri""",
            list_row,
        )
        conn.commit()


def set_list_muted(account_id: int, list_uri: str, muted: bool):
    with _connect() as conn:
        conn.execute(
            "UPDATE lists SET muted = ? WHERE account_id = ? AND list_uri = ?",
            (1 if muted else 0, account_id, list_uri),
        )
        conn.commit()


def set_list_blocked_uri(account_id: int, list_uri: str, blocked_uri):
    with _connect() as conn:
        conn.execute(
            "UPDATE lists SET blocked_uri = ? WHERE account_id = ? AND list_uri = ?",
            (blocked_uri, account_id, list_uri),
        )
        conn.commit()


def get_lists(account_id: int):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lists WHERE account_id = ? ORDER BY name COLLATE NOCASE ASC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_list(account_id: int, list_uri: str):
    with _connect() as conn:
        conn.execute("DELETE FROM lists WHERE account_id = ? AND list_uri = ?", (account_id, list_uri))
        conn.execute("DELETE FROM feed_items WHERE account_id = ? AND feed_key = ?", (account_id, list_uri))
        conn.commit()


def upsert_message(message: dict):
    """is_read is never set here (new rows get DEFAULT 0; see
    reconcile_message_read_state/mark_message_read). reactions_json IS
    refreshed on every resync, since it's server-side data."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messages
               (account_id, convo_id, message_id, sender_did, text, sent_at,
                reply_to_message_id, reply_to_text, reactions_json, embed_json)
               VALUES (:account_id, :convo_id, :message_id, :sender_did, :text, :sent_at,
                       :reply_to_message_id, :reply_to_text, :reactions_json, :embed_json)
               ON CONFLICT(account_id, convo_id, message_id) DO UPDATE SET
                   text=excluded.text,
                   sent_at=excluded.sent_at,
                   reply_to_message_id=excluded.reply_to_message_id,
                   reply_to_text=excluded.reply_to_text,
                   reactions_json=excluded.reactions_json,
                   embed_json=excluded.embed_json""",
            message,
        )
        conn.commit()
def get_messages_for_convo(account_id: int, convo_id: str):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE account_id = ? AND convo_id = ? ORDER BY sent_at ASC",
            (account_id, convo_id),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_message_read(account_id: int, convo_id: str, message_id: str):
    # Scoped by the full (account_id, convo_id, message_id) key -- unlike
    # posts' mark_post_read(uri), a message_id alone isn't guaranteed
    # globally unique (see the messages table's actual PRIMARY KEY above).
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE account_id = ? AND convo_id = ? AND message_id = ?",
            (account_id, convo_id, message_id),
        )
        conn.commit()


def delete_message(account_id: int, convo_id: str, message_id: str):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE account_id = ? AND convo_id = ? AND message_id = ?",
            (account_id, convo_id, message_id),
        )
        conn.commit()


def set_message_reactions(account_id: int, convo_id: str, message_id: str, reactions_json: str):
    # Optimistic local write; the next sync overwrites it with the server's value.
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET reactions_json = ? WHERE account_id = ? AND convo_id = ? AND message_id = ?",
            (reactions_json, account_id, convo_id, message_id),
        )
        conn.commit()


def get_unread_message_count(account_id: int, convo_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM messages WHERE account_id = ? AND convo_id = ? AND is_read = 0",
            (account_id, convo_id),
        ).fetchone()
        return row["c"] if row else 0


def reconcile_message_read_state(account_id: int, convo_id: str, unread_count: int):
    """
    Re-derives every message's local is_read flag for this conversation
    from the server's authoritative unread_count -- the newest
    `unread_count` messages (by sent_at) become unread, everything
    older becomes read. Called after every message sync for a
    conversation (see client._sync_convo_messages) so local state can't
    permanently drift from what the server actually knows, even though
    reads also get pushed back individually via client.mark_message_read
    -- this is the reconciliation half of that two-way sync. Safe to
    re-run any time; always a full recompute, never incremental.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        )
        if unread_count > 0:
            conn.execute(
                """UPDATE messages SET is_read = 0 WHERE rowid IN (
                       SELECT rowid FROM messages
                       WHERE account_id = ? AND convo_id = ?
                       ORDER BY sent_at DESC LIMIT ?
                   )""",
                (account_id, convo_id, unread_count),
            )
        conn.commit()


def mark_all_messages_read(account_id: int, convo_id: str):
    # Used by the explicit "Mark read" menu action (_markConvoRead) --
    # separate from reconcile_message_read_state above since this one
    # is an unconditional "everything in this convo is read now",
    # not a resync-driven recompute.
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE account_id = ? AND convo_id = ?",
            (account_id, convo_id),
        )
        conn.commit()


def mark_post_read(uri: str):
    with _connect() as conn:
        conn.execute("UPDATE posts SET is_read = 1 WHERE uri = ?", (uri,))
        conn.commit()


def mark_post_unread(uri: str):
    with _connect() as conn:
        conn.execute("UPDATE posts SET is_read = 0 WHERE uri = ?", (uri,))
        conn.commit()


def hide_post(uri: str):
    with _connect() as conn:
        conn.execute("UPDATE posts SET is_hidden = 1 WHERE uri = ?", (uri,))
        conn.commit()


def delete_post(uri: str):
    with _connect() as conn:
        conn.execute("DELETE FROM posts WHERE uri = ?", (uri,))
        conn.commit()


def set_post_like_uri(uri: str, like_uri):
    with _connect() as conn:
        conn.execute("UPDATE posts SET viewer_like_uri = ? WHERE uri = ?", (like_uri, uri))
        conn.commit()


def set_post_repost_uri(uri: str, repost_uri):
    with _connect() as conn:
        conn.execute("UPDATE posts SET viewer_repost_uri = ? WHERE uri = ?", (repost_uri, uri))
        conn.commit()

def set_post_bookmarked(uri: str, bookmarked: bool):
    with _connect() as conn:
        conn.execute("UPDATE posts SET viewer_bookmarked = ? WHERE uri = ?", (1 if bookmarked else 0, uri))
        conn.commit()

def set_post_thread_muted(uri: str, muted: bool):
    with _connect() as conn:
        conn.execute("UPDATE posts SET viewer_thread_muted = ? WHERE uri = ?", (1 if muted else 0, uri))
        conn.commit()

def get_unread_count(account_id: int, feed_key: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as c FROM feed_items f
               JOIN posts p ON p.uri = f.uri
               WHERE f.account_id = ? AND f.feed_key = ? AND p.is_read = 0 AND p.is_hidden = 0""",
            (account_id, feed_key),
        ).fetchone()
        return row["c"] if row else 0


# ---------------- notifications ----------------

def upsert_notification(notif: dict):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO notifications
               (account_id, uri, cid, reason, reason_subject, subject_uri, author_did, indexed_at,
                subject_text, subject_author_handle)
               VALUES (:account_id, :uri, :cid, :reason, :reason_subject, :subject_uri, :author_did, :indexed_at,
                       :subject_text, :subject_author_handle)
               ON CONFLICT(account_id, uri) DO UPDATE SET
                   cid=excluded.cid,
                   reason=excluded.reason,
                   reason_subject=excluded.reason_subject,
                   subject_uri=excluded.subject_uri,
                   author_did=excluded.author_did,
                   indexed_at=excluded.indexed_at,
                   subject_text=excluded.subject_text,
                   subject_author_handle=excluded.subject_author_handle""",
            notif,
        )
        conn.commit()


def get_notification_page(account_id: int, before_indexed_at: str = None, limit: int = None):
    query = """SELECT n.*, a.handle, a.display_name, a.avatar_url FROM notifications n
               LEFT JOIN authors a ON n.author_did = a.did
               WHERE n.account_id = ?"""
    params = [account_id]
    if before_indexed_at is not None:
        query += " AND n.indexed_at < ?"
        params.append(before_indexed_at)
    query += " ORDER BY n.indexed_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_unread_notification_count(account_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM notifications WHERE account_id = ? AND is_read = 0",
            (account_id,),
        ).fetchone()
        return row["c"] if row else 0


def mark_notification_read(uri: str):
    with _connect() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE uri = ?", (uri,))
        conn.commit()


def mark_notification_unread(uri: str):
    with _connect() as conn:
        conn.execute("UPDATE notifications SET is_read = 0 WHERE uri = ?", (uri,))
        conn.commit()


def clear_notifications_cache(account_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM notifications WHERE account_id = ?", (account_id,))
        # Without this, the next sync stops at the old resume point and stores nothing.
        conn.execute("DELETE FROM ui_state WHERE key = ?", (f"notification_sync_cursor:{account_id}",))
        conn.commit()
        conn.execute("VACUUM")


# ---------------- ui_state ----------------

def set_ui_state(key: str, value: str):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ui_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        conn.commit()


def get_ui_state(key: str):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM ui_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def delete_ui_state(key: str):
    with _connect() as conn:
        conn.execute("DELETE FROM ui_state WHERE key = ?", (key,))
        conn.commit()


# ---------------- saved feeds cache (Settings > Feed manager) ----------------
# A plain ui_state-backed cache, not a real table -- lets the Feed manager
# panel render instantly from what it saw last time instead of blocking on
# a getPreferences + getFeedGenerators round trip on every Settings open.

def set_saved_feeds_cache(account_id: int, feeds: list):
    set_ui_state(f"saved_feeds_cache:{account_id}", json.dumps(feeds))


def get_saved_feeds_cache(account_id: int) -> list:
    raw = get_ui_state(f"saved_feeds_cache:{account_id}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


# ---------------- content label prefs cache (Settings > Content labels) ----------------
# Local cache so FeedListMixin's per-row visibility check never needs a
# network call -- written whenever Settings > Content labels loads or
# saves a change. Missing/empty means "no explicit pref cached yet" --
# callers fall back to client.CONTENT_LABEL_DEFAULT_VISIBILITY.

def get_content_label_prefs_cache(account_id: int) -> dict:
    raw = get_ui_state(f"content_label_prefs_cache:{account_id}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def set_content_label_prefs_cache(account_id: int, prefs: dict):
    set_ui_state(f"content_label_prefs_cache:{account_id}", json.dumps(prefs))


# ---------------- user list cache (followers/following/people-search tabs) ----------------
# Same ui_state-backed JSON-blob pattern as saved_feeds_cache above --
# UserListTabWindow (feedWindow.py) shows this instantly on open, then
# silently re-fetches in the background and only re-renders/announces
# if the result actually changed. Keyed by the tab's own TAB_TEMP_KEY
# (e.g. "followers:did:xyz", "search:some query").

def get_user_list_cache(account_id: int, list_key: str):
    # Returns None (not []) when nothing has been cached yet, so
    # callers can tell that apart from a real, confirmed-empty list
    # (e.g. an account with zero followers).
    raw = get_ui_state(f"user_list_cache:{account_id}:{list_key}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def set_user_list_cache(account_id: int, list_key: str, users: list):
    set_ui_state(f"user_list_cache:{account_id}:{list_key}", json.dumps(users))


def delete_user_list_cache(account_id: int, list_key: str):
    delete_ui_state(f"user_list_cache:{account_id}:{list_key}")


# ---------------- background sync scheduler ----------------

BG_SYNC_DEFAULT_INTERVALS = {
    "home": 5, "chat": 2, "notifications": 3, "saved": 0,
    "lists": 10, "search": 3, "profile": 15, "thread": 5,
}


def get_bg_sync_interval(category: str) -> int:
    """Minutes between background syncs for `category`, or 0 = disabled.
    Falls back to BG_SYNC_DEFAULT_INTERVALS if never explicitly set."""
    raw = get_ui_state(f"bg_sync_interval_{category}")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return BG_SYNC_DEFAULT_INTERVALS.get(category, 0)


def set_bg_sync_interval(category: str, minutes: int):
    set_ui_state(f"bg_sync_interval_{category}", str(max(0, minutes)))


def get_bg_sync_announce_categories() -> set:
    """Category keys (see BG_SYNC_DEFAULT_INTERVALS) the user wants
    spoken when background sync finds new content for them -- replaces
    the old single all-or-nothing checkbox. Defaults to every category,
    matching that checkbox's old default of True."""
    raw = get_ui_state("bg_sync_announce_categories")
    if raw is None:
        return set(BG_SYNC_DEFAULT_INTERVALS)
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set(BG_SYNC_DEFAULT_INTERVALS)


def set_bg_sync_announce_categories(categories: set):
    set_ui_state("bg_sync_announce_categories", json.dumps(sorted(categories)))


def get_bg_sync_last(account_id: int, category: str):
    """ISO timestamp string of the last successful background sync for
    this category, or None if it's never run yet."""
    return get_ui_state(f"bg_sync_last:{category}:{account_id}")


def set_bg_sync_last(account_id: int, category: str, iso_timestamp: str):
    set_ui_state(f"bg_sync_last:{category}:{account_id}", iso_timestamp)


def get_chat_log_cursor(account_id: int):
    """chat.bsky.convo.getLog cursor for background delta-sync (see
    client.sync_chat_delta) -- None means never synced this way yet,
    triggers a one-time full bootstrap sync."""
    return get_ui_state(f"chat_log_cursor:{account_id}")


def set_chat_log_cursor(account_id: int, cursor: str):
    set_ui_state(f"chat_log_cursor:{account_id}", cursor)


def get_jetstream_cursor(account_id: int):
    """Jetstream time_us cursor (see jetstream.JetstreamClient) --
    None means never connected before, starts from "now" (no cursor
    param sent)."""
    raw = get_ui_state(f"jetstream_cursor:{account_id}")
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def set_jetstream_cursor(account_id: int, time_us: int):
    set_ui_state(f"jetstream_cursor:{account_id}", str(time_us))


def get_jetstream_enabled() -> bool:
    return get_ui_state("jetstream_enabled") == "1"


def set_jetstream_enabled(enabled: bool):
    set_ui_state("jetstream_enabled", "1" if enabled else "0")


def get_tray_enabled() -> bool:
    # On unless the user turned it off.
    return get_ui_state("tray_enabled") != "0"


def set_tray_enabled(enabled: bool):
    set_ui_state("tray_enabled", "1" if enabled else "0")


TRAY_CATEGORIES = ("home", "notifications", "chat")


def get_tray_categories() -> set:
    """Tabs whose unread counts the notification area icon shows (all by default)."""
    raw = get_ui_state("tray_categories")
    if raw is None:
        return set(TRAY_CATEGORIES)
    try:
        return set(json.loads(raw)) & set(TRAY_CATEGORIES)
    except (ValueError, TypeError):
        return set(TRAY_CATEGORIES)


def set_tray_categories(categories) -> None:
    set_ui_state("tray_categories", json.dumps(sorted(categories)))


def get_tray_per_tab() -> bool:
    return get_ui_state("tray_per_tab") == "1"


def set_tray_per_tab(per_tab: bool) -> None:
    set_ui_state("tray_per_tab", "1" if per_tab else "0")


def get_feed_item_indexed_at(account_id: int, feed_key: str, uri: str):
    """Existing sort-position timestamp for one feed_items row, or
    None if not cached yet -- used by client._store_feed_item to keep
    a self-repost's position from sinking when a subsequent sync
    doesn't frame it as a repost (see that function's own comment)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT indexed_at FROM feed_items WHERE account_id = ? AND feed_key = ? AND uri = ?",
            (account_id, feed_key, uri),
        ).fetchone()
        return row["indexed_at"] if row else None


def mark_post_reposted(uri: str, reposted_by_did, reposted_by_handle, reposted_by_display_name):
    """Local-only write for jetstream.py's real-time repost path --
    sets is_repost + reposted_by_* on an already-cached post row.
    Deliberately outside upsert_post's own ON CONFLICT SET clause since
    a normal sync legitimately overwrites these back to the server's
    real state on its own next pass."""
    with _connect() as conn:
        conn.execute(
            """UPDATE posts SET is_repost = 1, reposted_by_did = ?,
               reposted_by_handle = ?, reposted_by_display_name = ? WHERE uri = ?""",
            (reposted_by_did, reposted_by_handle, reposted_by_display_name, uri),
        )
        conn.commit()


def get_notification_sync_cursor(account_id: int):
    """listNotifications cursor from the last successful sync -- lets
    sync_notifications resume from where it left off instead of always
    re-fetching only the newest 50, which silently dropped anything
    past that if more than 50 notifications arrived between syncs
    (e.g. the add-on left closed for a while, or a popular account)."""
    return get_ui_state(f"notification_sync_cursor:{account_id}")


def set_notification_sync_cursor(account_id: int, cursor: str):
    set_ui_state(f"notification_sync_cursor:{account_id}", cursor)


def get_home_active_filter(account_id: int) -> str:
    """The feed_key FeedWindow's filter dropdown was last set to (e.g.
    "home", "discover", or a custom feed uri) -- persisted on every
    FeedWindow.onFilterChanged so background sync (which may run while
    MainWindow is closed) knows which feed the user actually cares
    about right now, instead of always assuming plain "home"."""
    return get_ui_state(f"home_active_filter:{account_id}") or "home"


def set_home_active_filter(account_id: int, feed_key: str):
    set_ui_state(f"home_active_filter:{account_id}", feed_key)


DEFAULT_ENABLED_TABS = {"notifications", "explore", "chat", "lists", "people"}


def get_enabled_tabs() -> set:
    """Permanent tabs shown besides Home (Home is always shown)."""
    raw = get_ui_state("enabled_tabs")
    if raw is None:
        return set(DEFAULT_ENABLED_TABS)
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set(DEFAULT_ENABLED_TABS)


def set_enabled_tabs(tabs) -> None:
    set_ui_state("enabled_tabs", json.dumps(sorted(tabs)))


# ---------------- sound pack ----------------

def get_soundpack_selected() -> str:
    """
    Empty string means "Silent / No sound". Never set: the "default" pack,
    else the first pack found, else Silent if no pack folder exists.
    """
    raw = get_ui_state("soundpack_selected")
    if raw is not None:
        return raw
    from . import soundpack
    packs = soundpack.list_packs()
    if soundpack.DEFAULT_PACK_NAME in packs:
        return soundpack.DEFAULT_PACK_NAME
    return packs[0] if packs else ""


def set_soundpack_selected(pack_name: str):
    set_ui_state("soundpack_selected", pack_name or "")


def get_soundpack_disabled_events() -> set:
    """
    Event keys the user has UNCHECKED. Storing the disabled set means
    event keys added to soundpack.EVENT_KEYS later are enabled by default.
    """
    raw = get_ui_state("soundpack_disabled_events")
    if raw is None:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set()


def set_soundpack_disabled_events(events) -> None:
    set_ui_state("soundpack_disabled_events", json.dumps(sorted(events)))


def get_open_temp_tabs(account_id: int) -> list:
    """
    "Temp tabs": removable tabs opened on demand (list, thread, user
    timeline, conversation, ...), restored on the next MainWindow build.
    One JSON list per account.
    """
    raw = get_ui_state(f"open_temp_tabs:{account_id}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def add_open_temp_tab(account_id: int, entry: dict):
    tabs = get_open_temp_tabs(account_id)
    tabs = [t for t in tabs if not (t.get("type") == entry.get("type") and t.get("key") == entry.get("key"))]
    tabs.append(entry)
    set_ui_state(f"open_temp_tabs:{account_id}", json.dumps(tabs))


def remove_open_temp_tab(account_id: int, tab_type: str, key: str):
    tabs = get_open_temp_tabs(account_id)
    tabs = [t for t in tabs if not (t.get("type") == tab_type and t.get("key") == key)]
    set_ui_state(f"open_temp_tabs:{account_id}", json.dumps(tabs))


def set_temp_tab_custom_name(account_id: int, tab_type: str, key: str, name: str):
    """
    Persists a user-chosen rename for a temp tab (see MainWindow.
    renameCurrentTab -> panel.onTabRenamed) directly into its existing
    open_temp_tabs entry, so it survives past this session instead of
    being lost on close. No-ops silently if the entry isn't found
    (shouldn't normally happen -- the entry is written when the tab is
    first opened, before it could ever be renamed).
    """
    tabs = get_open_temp_tabs(account_id)
    for t in tabs:
        if t.get("type") == tab_type and t.get("key") == key:
            t["custom_name"] = name
            break
    set_ui_state(f"open_temp_tabs:{account_id}", json.dumps(tabs))


def get_tab_order(account_id: int) -> list:
    """
    One interleaved order for EVERY tab (permanent and temp) as
    [kind, key] pairs; empty list if never saved.
    """
    raw = get_ui_state(f"tab_order:{account_id}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def set_tab_order(account_id: int, order: list):
    set_ui_state(f"tab_order:{account_id}", json.dumps(order))
