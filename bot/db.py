import os
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    real_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    text TEXT NOT NULL,
    hash TEXT NOT NULL,
    scope_type TEXT NOT NULL,      -- forest | group | place
    scope_value TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | replaced | deleted
    replaced_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scope ON documents(scope_type, scope_value, status);
""")

_SELECT = ("SELECT d.*, u.real_name AS author FROM documents d "
           "LEFT JOIN users u ON u.tg_id = d.author_id ")


def get_user(tg_id):
    return conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def set_user(tg_id, name):
    conn.execute("INSERT OR REPLACE INTO users VALUES(?,?)", (tg_id, name))
    conn.commit()


def get_doc(doc_id):
    return conn.execute(_SELECT + "WHERE d.id=?", (doc_id,)).fetchone()


def find_active_by_hash(h):
    return conn.execute(_SELECT + "WHERE d.hash=? AND d.status='active'", (h,)).fetchone()


def find_active_by_hash_scope(h, scope_type, scope_value):
    return conn.execute(
        _SELECT + "WHERE d.hash=? AND d.scope_type=? AND d.scope_value=? AND d.status='active'",
        (h, scope_type, scope_value)).fetchone()


def active_docs(scope_type, scope_value):
    return conn.execute(
        _SELECT + "WHERE d.scope_type=? AND d.scope_value=? AND d.status='active' "
        "ORDER BY d.uploaded_at DESC", (scope_type, scope_value)).fetchall()


def user_docs(author_id):
    return conn.execute(
        _SELECT + "WHERE d.author_id=? AND d.status='active' ORDER BY d.uploaded_at DESC",
        (author_id,)).fetchall()


def add_doc(author_id, filename, text, h, scope_type, scope_value, replaces=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO documents(author_id, filename, text, hash, scope_type, scope_value, uploaded_at)"
        " VALUES(?,?,?,?,?,?,?)", (author_id, filename, text, h, scope_type, scope_value, now))
    new_id = cur.lastrowid
    if replaces:
        # Стара версія не видаляється, лише перестає бути актуальною.
        conn.execute("UPDATE documents SET status='replaced', replaced_by=? "
                     "WHERE id=? AND status='active'", (new_id, replaces))
    conn.commit()
    return new_id


def delete_doc(doc_id, author_id):
    cur = conn.execute("UPDATE documents SET status='deleted' "
                       "WHERE id=? AND author_id=? AND status='active'", (doc_id, author_id))
    conn.commit()
    return cur.rowcount > 0
