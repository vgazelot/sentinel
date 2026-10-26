"""SQLite: schema + helpers. No ORM, stdlib sqlite3."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

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
    lane TEXT NOT NULL DEFAULT 'you',
    urgency TEXT NOT NULL DEFAULT 'now',
    notified_at TEXT,
    missed INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT,
    context TEXT NOT NULL DEFAULT '{}',
    output TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    UNIQUE(subject, mode)
);
CREATE TABLE IF NOT EXISTS watcher_state (
    watcher TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (watcher, key)
);
-- Lookout (lookout.py): one row per thing seen, whatever the source; stage new → scouting → kept/dropped →
-- assessing → assessed. sources = {source: {discussion, points, comments}}, facts = what the repository says.
CREATE TABLE IF NOT EXISTS lookout (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '{}',
    facts TEXT NOT NULL DEFAULT '{}',
    published TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'new',
    note TEXT,
    score INTEGER,
    verdict TEXT,
    category TEXT,
    why TEXT,
    assessed_at TEXT,
    feedback TEXT,
    feedback_at TEXT
);
CREATE INDEX IF NOT EXISTS lookout_stage ON lookout (stage);
-- The Second (second.py): one console per (kind, subject, fingerprint) — an alert occurrence, a ticket at a given
-- update, a day's plan; state queued → running → done | failed.
CREATE TABLE IF NOT EXISTS second_jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    console TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    item_id INTEGER,
    story TEXT,
    prompt TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    session TEXT,
    report TEXT,
    error TEXT,
    posted_at TEXT,
    ended_at TEXT,
    meta TEXT,
    UNIQUE(kind, subject, fingerprint)
);
"""
# a pending item counts as "needs you" when it is urgent, or when its grace period ran out
ATTENTION = "state = 'pending' AND (urgency = 'now' OR (urgency = 'later' AND notified_at IS NOT NULL))"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init():
    with connect() as conn:
        _migrate_items(conn)
        conn.executescript(SCHEMA)
        _add_columns(conn, "agent_runs", (("attempts", "INTEGER NOT NULL DEFAULT 1"),))
        _add_columns(conn, "second_jobs", (("meta", "TEXT"),))
        _migrate_reviews(conn)


def _columns(conn, table: str) -> list[str]:
    return [c["name"] for c in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_items(conn):
    """Databases from before the triage: add the new columns (CREATE TABLE IF NOT EXISTS won't)."""
    _add_columns(conn, "items", (("lane", "TEXT NOT NULL DEFAULT 'you'"), ("urgency", "TEXT NOT NULL DEFAULT 'now'"),
                                 ("notified_at", "TEXT"), ("missed", "INTEGER NOT NULL DEFAULT 0")))


def _add_columns(conn, table: str, columns: tuple):
    cols = _columns(conn, table)
    if not cols:
        return  # the table does not exist yet: SCHEMA creates it whole
    for name, ddl in columns:
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_reviews(conn):
    """The `reviews` table (one Ask Claude verdict per item and mode) became `agent_runs`, keyed by PR."""
    if not _columns(conn, "reviews"):
        return
    rows = conn.execute(
        "SELECT r.*, i.payload, i.url AS item_url FROM reviews r JOIN items i ON i.id = r.item_id"
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload"] or "{}")
        if not payload.get("repo"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO agent_runs (subject, mode, status, url, output, error,
                                                 created_at, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"{payload['repo']}#{payload['number']}", r["mode"], r["status"], r["item_url"],
             r["output"], r["error"], r["started_at"], r["started_at"], r["finished_at"]),
        )
    conn.execute("DROP TABLE reviews")


# --- items -----------------------------------------------------------------
def get_item(conn, item_id: int):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def items(where: str = "1", *params) -> list:
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY first_seen DESC", params
        ).fetchall()


def attention_count() -> int:
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM items WHERE {ATTENTION}").fetchone()[0]


def set_state(item_id: int, state: str, **fields):
    cols = {"state": state, **fields}
    assign = ", ".join(f"{k} = ?" for k in cols)
    with connect() as conn:
        conn.execute(f"UPDATE items SET {assign} WHERE id = ?", (*cols.values(), item_id))


# --- notifications ----------------------------------------------------------
def log_notification(conn, item_id: int, reason: str, title: str, body: str) -> int:
    cur = conn.execute(
        "INSERT INTO notifications (item_id, sent_at, reason, title, body) VALUES (?, ?, ?, ?, ?)",
        (item_id, now(), reason, title, body),
    )
    return cur.lastrowid


def notifications_after(seq: int, limit: int = 20) -> list:
    with connect() as conn:
        return conn.execute(
            """SELECT n.*, i.url, i.watcher, i.lane FROM notifications n
               JOIN items i ON i.id = n.item_id
               WHERE n.id > ? ORDER BY n.id DESC LIMIT ?""",
            (seq, limit),
        ).fetchall()[::-1]


def last_notification_id() -> int:
    with connect() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0]


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


def record_run(watcher: str, error: str = ""):
    """last_run/last_error for any polling job, plus error_since: when the current failure streak began."""
    states = watcher_states().get(watcher, {})
    set_watcher_state(watcher, "last_run", now())
    set_watcher_state(watcher, "last_error", error)
    if not error:
        set_watcher_state(watcher, "error_since", "")
    elif not states.get("error_since"):
        set_watcher_state(watcher, "error_since", now())


# --- Claude agent runs ------------------------------------------------------------
def get_run(subject: str, mode: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM agent_runs WHERE subject = ? AND mode = ?", (subject, mode)).fetchone()


def runs_for(subjects: list[str]) -> dict:
    """{subject: {mode: row}} for a set of subjects, one query."""
    if not subjects:
        return {}
    marks = ",".join("?" * len(subjects))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM agent_runs WHERE subject IN ({marks})", subjects).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["subject"], {})[r["mode"]] = r
    return out


def queue_run(subject: str, mode: str, url: str, context: dict, retry: bool = False):
    """(Re)queue a run: one row per subject and mode, the latest one wins. `retry` counts one more
    attempt (an automatic run trying again after an error); anything else starts over at 1."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_runs (subject, mode, status, url, context, created_at)
               VALUES (?, ?, 'queued', ?, ?, ?)
               ON CONFLICT(subject, mode) DO UPDATE SET status = 'queued', url = excluded.url,
                   context = excluded.context, output = NULL, error = NULL,
                   created_at = excluded.created_at, started_at = NULL, finished_at = NULL,
                   attempts = CASE WHEN ? THEN attempts + 1 ELSE 1 END""",
            (subject, mode, url, json.dumps(context), now(), int(retry)),
        )


def claim_runs(limit: int) -> list:
    """Move up to `limit` queued runs to running (oldest first) and return them."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE status = 'queued' ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE agent_runs SET status = 'running', started_at = ? WHERE id = ?", (now(), r["id"]))
    return rows


def finish_run(run_id: int, output: str | None = None, error: str | None = None):
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = ?, output = ?, error = ?, finished_at = ? WHERE id = ?",
            ("error" if error else "done", output, error, now(), run_id),
        )


def count_runs(where: str, *params) -> int:
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM agent_runs WHERE {where}", params).fetchone()[0]


def fail_stale_runs():
    """Called at startup: a restart kills the runner threads, their rows would stay 'running' forever."""
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = 'error', error = 'interrupted by a Sentinel restart — run it again', "
            "finished_at = ? WHERE status = 'running'",
            (now(),),
        )
