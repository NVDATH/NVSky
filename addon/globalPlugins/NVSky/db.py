"""
SQLite persistence layer for NVSky. See init_db() for schema.
The database file lives under NVDA's user config directory, NOT inside the
add-on folder (the add-on folder gets overwritten on every update).

Uses sqlcipher3 (not plain sqlite3) for full database encryption -- its
compiled extension bundles SQLCipher+OpenSSL statically, so unlike the old
plain-sqlite3 setup, this needs NO os.add_dll_directory() vendoring trick
at all. That trick was process-wide (Windows AddDllDirectory is global to
the whole NVDA process, not scoped per add-on), which is how it was
possible for another add-on's vendored sqlite3.dll to get picked up here
and vice versa -- confirmed by testing, not just theory. sqlcipher3 having
no external DLL to resolve removes that risk entirely, but still worth
re-confirming empirically (e.g. temporarily disable other add-ons that
vendor sqlite3 and check NVSky still opens its db fine).

The DB encryption master key is a random 32-byte value, generated once and
stored -- encrypted via the same DPAPI helpers as the App Password
(crypto.py) -- in a small file next to the database. It can't live inside
the database itself (can't read the key from the db you need the key to
open).
"""

import json
import os
import secrets
import threading
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


def _get_connection():
    # One connection per OS thread, created once and reused for that
    # thread's lifetime, instead of opening+closing (and paying the
    # SQLCipher PRAGMA-key unlock cost) on every single query. Confirmed
    # via timing that opening a fresh connection per call was the
    # dominant cost behind the MainWindow-open freeze (~200+ connects
    # in a single UI build, ~0.8s total) -- see also the time-format
    # N+1 fix in feedWindow.py/chatWindow.py, which was the other half
    # of that same freeze.
    #
    # Safe across threads without a lock: sqlite3/sqlcipher3 connections
    # default to check_same_thread=True (each connection only usable
    # from the thread that created it), which is exactly what
    # threading.local() already gives us for free -- one isolated
    # connection object per thread, never shared.

    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    dbKey = _get_or_create_db_key()
    conn.execute(f"PRAGMA key = \"x'{dbKey}'\"")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    _local.conn = conn
    return conn


@contextmanager
def _connect():
    # Kept as a context manager so every existing `with _connect() as
    # conn:` call site keeps working unchanged -- it just no longer
    # closes the connection on exit, since the connection is now meant
    # to outlive this one call. Write paths already call conn.commit()
    # explicitly where needed (see upsert_convo etc.), so nothing here
    # relied on close()-time behavior.
    yield _get_connection()


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                handle TEXT NOT NULL,
                did TEXT NOT NULL UNIQUE,
                encrypted_password BLOB NOT NULL,
                is_active INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS authors (
                did TEXT PRIMARY KEY,
                handle TEXT,
                display_name TEXT,
                avatar_url TEXT
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
                member_did TEXT,
                member_handle TEXT,
                member_display_name TEXT,
                last_message_text TEXT,
                last_message_sent_at TEXT,
                unread_count INTEGER DEFAULT 0,
                muted INTEGER DEFAULT 0,
                status TEXT DEFAULT 'accepted',
                PRIMARY KEY (account_id, convo_id)
            );
            CREATE INDEX IF NOT EXISTS idx_convos_last_message ON convos(account_id, last_message_sent_at);

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
        conn.commit()
        conn.execute("VACUUM")


def clear_cache(account_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM posts WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM ui_state WHERE key = ?", (f"lastFocus:home:{account_id}",))
        conn.commit()
        conn.execute("VACUUM")


# ---------------- authors ----------------

def upsert_author(did: str, handle: str, display_name: str, avatar_url: str):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO authors (did, handle, display_name, avatar_url)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(did) DO UPDATE SET
                   handle=excluded.handle,
                   display_name=excluded.display_name,
                   avatar_url=excluded.avatar_url""",
            (did, handle, display_name, avatar_url),
        )
        conn.commit()


def get_author(did: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM authors WHERE did = ?", (did,)).fetchone()
        return dict(row) if row else None


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
                viewer_bookmarked)
               VALUES (:uri, :cid, :account_id, :author_did, :text, :created_at, :indexed_at,
                       :like_count, :repost_count, :reply_count, :reply_parent_uri,
                       :reply_to_did, :reply_to_handle, :is_repost, :reposted_by_did, :reposted_by_handle, :reposted_by_display_name,
                       :embed_json, :facets_json, :quoted_text, :quoted_author_handle, :viewer_like_uri, :viewer_repost_uri,
                       :viewer_bookmarked)
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
                   viewer_bookmarked=excluded.viewer_bookmarked""",
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


def delete_feed_item(account_id: int, feed_key: str, uri: str):
    """Removes `uri` from `feed_key` for this account -- the reverse of
    upsert_feed_item. Needed for feeds where an item can legitimately
    disappear (e.g. Saved, when a post gets unbookmarked) -- without
    this, get_feed_page() keeps returning the stale membership row
    forever, since a sync only ever adds/updates rows, it never detects
    and removes ones no longer present remotely."""
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
    with _connect() as conn:
        conn.execute(
            """INSERT INTO convos
               (account_id, convo_id, member_did, member_handle, member_display_name,
                last_message_text, last_message_sent_at, unread_count, muted, status)
               VALUES (:account_id, :convo_id, :member_did, :member_handle, :member_display_name,
                       :last_message_text, :last_message_sent_at, :unread_count, :muted, :status)
               ON CONFLICT(account_id, convo_id) DO UPDATE SET
                   member_did=excluded.member_did,
                   member_handle=excluded.member_handle,
                   member_display_name=excluded.member_display_name,
                   last_message_text=excluded.last_message_text,
                   last_message_sent_at=excluded.last_message_sent_at,
                   unread_count=excluded.unread_count,
                   muted=excluded.muted,
                   status=excluded.status""",
            convo,
        )
        conn.commit()


def get_convos(account_id: int):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM convos WHERE account_id = ? ORDER BY last_message_sent_at DESC",
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


def delete_convo(account_id: int, convo_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM convos WHERE account_id = ? AND convo_id = ?", (account_id, convo_id))
        conn.execute("DELETE FROM messages WHERE account_id = ? AND convo_id = ?", (account_id, convo_id))
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
    """is_read is deliberately NOT set here -- a new row just gets the
    column's DEFAULT 0, and db.reconcile_message_read_state() (called
    right after a batch of these upserts, from
    client._sync_convo_messages) is what actually establishes the
    correct read/unread boundary, from the server's authoritative
    per-conversation unread_count. Never touched by ON CONFLICT either
    way -- some other local-state path (reconcile / mark_message_read /
    mark_all_messages_read) always owns it after this point.
    reactions_json IS refreshed on every resync though (in both INSERT
    and ON CONFLICT) -- unlike is_read, reactions are someone else's
    server-side data, not local state we own, so a stale cached copy
    would just be wrong rather than "the user's own read progress"."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messages
               (account_id, convo_id, message_id, sender_did, text, sent_at,
                reply_to_message_id, reply_to_text, reactions_json)
               VALUES (:account_id, :convo_id, :message_id, :sender_did, :text, :sent_at,
                       :reply_to_message_id, :reply_to_text, :reactions_json)
               ON CONFLICT(account_id, convo_id, message_id) DO UPDATE SET
                   text=excluded.text,
                   sent_at=excluded.sent_at,
                   reply_to_message_id=excluded.reply_to_message_id,
                   reply_to_text=excluded.reply_to_text,
                   reactions_json=excluded.reactions_json""",
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


def mark_message_unread(account_id: int, convo_id: str, message_id: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET is_read = 0 WHERE account_id = ? AND convo_id = ? AND message_id = ?",
            (account_id, convo_id, message_id),
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


def get_open_temp_tabs(account_id: int) -> list:
    """
    "Temp tabs" -- dynamic tabs opened on demand (Lists' Show in new
    tab today; future per-user timeline/search tabs could reuse the
    same mechanism) that get remembered and reopened automatically
    next time MainWindow is built. Stored as one JSON blob per account
    since it's small, order-sensitive, and only ever read/written as a
    whole list.
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