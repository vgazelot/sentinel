"""SQLite: schema + helpers. No ORM, stdlib sqlite3."""
import json
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    watcher TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    severity TEXT,
    payload TEXT DEFAULT '{}',
    fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    ack_forever INTEGER NOT NULL DEFAULT 0,
    acked_fingerprint TEXT,
    snooze_until TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(watcher, external_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    sent_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    keys_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    item_id INTEGER NOT NULL REFERENCES items(id),
    mode TEXT NOT NULL DEFAULT 'deep',
    status TEXT NOT NULL,
    output TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (item_id, mode)
);
CREATE TABLE IF NOT EXISTS watcher_state (
    watcher TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (watcher, key)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    # reviews: one row per item → one row per (item, mode); SQLite cannot alter a primary key
    cols = [c["name"] for c in conn.execute("PRAGMA table_info(reviews)")]
    if "mode" not in cols:
        conn.executescript("""
            ALTER TABLE reviews RENAME TO reviews_old;
            CREATE TABLE reviews (
                item_id INTEGER NOT NULL REFERENCES items(id),
                mode TEXT NOT NULL DEFAULT 'deep',
                status TEXT NOT NULL,
                output TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY (item_id, mode)
            );
            INSERT INTO reviews (item_id, mode, status, output, error, started_at, finished_at)
                SELECT item_id, 'deep', status, output, error, started_at, finished_at FROM reviews_old;
            DROP TABLE reviews_old;
        """)


# --- items -----------------------------------------------------------------
def get_item(conn, item_id: int):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def items_by_state(state: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE state = ? ORDER BY severity DESC, last_seen DESC", (state,)
        ).fetchall()


def pending_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM items WHERE state = 'pending'").fetchone()[0]


def set_state(item_id: int, state: str, **fields):
    cols = {"state": state, **fields}
    assign = ", ".join(f"{k} = ?" for k in cols)
    with connect() as conn:
        conn.execute(f"UPDATE items SET {assign} WHERE id = ?", (*cols.values(), item_id))


# --- notifications ----------------------------------------------------------
def log_notification(conn, item_id: int, reason: str, title: str, body: str):
    conn.execute(
        "INSERT INTO notifications (item_id, sent_at, reason, title, body) VALUES (?, ?, ?, ?, ?)",
        (item_id, now(), reason, title, body),
    )


def notifications_since(since: str):
    with connect() as conn:
        return conn.execute(
            """SELECT n.*, i.url, i.watcher FROM notifications n
               JOIN items i ON i.id = n.item_id
               WHERE n.sent_at > ? AND i.state = 'pending'
               ORDER BY n.sent_at""",
            (since,),
        ).fetchall()


# --- push subscriptions ------------------------------------------------------
def add_subscription(sub: dict):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, keys_json, created_at) VALUES (?, ?, ?)",
            (sub["endpoint"], json.dumps(sub.get("keys", {})), now()),
        )


def remove_subscription(endpoint: str):
    with connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def subscriptions():
    with connect() as conn:
        return conn.execute("SELECT * FROM push_subscriptions").fetchall()


# --- watcher state ------------------------------------------------------------
def set_watcher_state(watcher: str, key: str, value: str):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watcher_state (watcher, key, value) VALUES (?, ?, ?)",
            (watcher, key, value),
        )


def watcher_states() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM watcher_state").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["watcher"], {})[r["key"]] = r["value"]
    return out


# --- Claude reviews ------------------------------------------------------------
def get_reviews(item_id: int) -> dict:
    """{mode: row} for this item."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM reviews WHERE item_id = ?", (item_id,)).fetchall()
    return {r["mode"]: r for r in rows}


def start_review(item_id: int, mode: str):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reviews (item_id, mode, status, output, error, started_at, finished_at)
               VALUES (?, ?, 'running', NULL, NULL, ?, NULL)""",
            (item_id, mode, now()),
        )


def finish_review(item_id: int, mode: str, output: str | None = None, error: str | None = None):
    with connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = ?, output = ?, error = ?, finished_at = ? WHERE item_id = ? AND mode = ?",
            ("error" if error else "done", output, error, now(), item_id, mode),
        )


def fail_stale_reviews():
    """Called at startup: a restart kills the background threads, their rows would stay 'running' forever."""
    with connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'error', error = 'interrupted by a Sentinel restart — run it again', finished_at = ? WHERE status = 'running'",
            (now(),),
        )
