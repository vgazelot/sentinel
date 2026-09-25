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
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    status TEXT NOT NULL,
    output TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
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


def history(limit: int = 200):
    with connect() as conn:
        return conn.execute(
            """SELECT n.*, i.url, i.watcher, i.state FROM notifications n
               JOIN items i ON i.id = n.item_id
               ORDER BY n.sent_at DESC, n.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


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


# --- todos --------------------------------------------------------------------
def todos():
    with connect() as conn:
        return conn.execute("SELECT * FROM todos ORDER BY done, id DESC").fetchall()


def add_todo(text: str):
    with connect() as conn:
        conn.execute("INSERT INTO todos (text, created_at) VALUES (?, ?)", (text, now()))


def toggle_todo(todo_id: int):
    with connect() as conn:
        conn.execute(
            "UPDATE todos SET done = 1 - done, done_at = CASE done WHEN 0 THEN ? ELSE NULL END WHERE id = ?",
            (now(), todo_id),
        )


def delete_todo(todo_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))


def clear_done_todos():
    with connect() as conn:
        conn.execute("DELETE FROM todos WHERE done = 1")


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
def get_review(item_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM reviews WHERE item_id = ?", (item_id,)).fetchone()


def start_review(item_id: int):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reviews (item_id, status, output, error, started_at, finished_at)
               VALUES (?, 'running', NULL, NULL, ?, NULL)""",
            (item_id, now()),
        )


def finish_review(item_id: int, output: str | None = None, error: str | None = None):
    with connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = ?, output = ?, error = ?, finished_at = ? WHERE item_id = ?",
            ("error" if error else "done", output, error, now(), item_id),
        )
