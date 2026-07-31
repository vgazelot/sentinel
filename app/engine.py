"""Core: runs watchers, diffs against stored state, ack/snooze state machine."""
import json
import logging

import db
import notify
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.engine")


def run_watcher(watcher: Watcher):
    try:
        items = watcher.fetch()
    except Exception as e:
        log.warning("watcher %s failed: %s", watcher.name, e)
        db.set_watcher_state(watcher.name, "last_error", str(e))
        db.set_watcher_state(watcher.name, "last_run", db.now())
        return
    db.set_watcher_state(watcher.name, "last_error", "")
    db.set_watcher_state(watcher.name, "last_run", db.now())
    process(watcher.name, items)


def process(watcher_name: str, fetched: list[WatchedItem]):
    ts = db.now()
    to_notify: list[tuple[int, str, WatchedItem]] = []

    with db.connect() as conn:
        existing = {
            r["external_id"]: r
            for r in conn.execute("SELECT * FROM items WHERE watcher = ?", (watcher_name,))
        }
        seen = set()

        for it in fetched:
            seen.add(it.external_id)
            row = existing.get(it.external_id)

            if row is None:
                cur = conn.execute(
                    """INSERT INTO items (watcher, external_id, title, url, severity, payload,
                                          fingerprint, state, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (watcher_name, it.external_id, it.title, it.url, it.severity,
                     json.dumps(it.payload), it.fingerprint, ts, ts),
                )
                to_notify.append((cur.lastrowid, "new", it))
                continue

            conn.execute(
                """UPDATE items SET title = ?, url = ?, severity = ?, payload = ?,
                                    fingerprint = ?, last_seen = ? WHERE id = ?""",
                (it.title, it.url, it.severity, json.dumps(it.payload),
                 it.fingerprint, ts, row["id"]),
            )

            if row["state"] == "resolved":
                # reappearance (e.g. review re-requested, incident reopened) → fresh cycle
                conn.execute(
                    """UPDATE items SET state = 'pending', resolved_at = NULL,
                                        ack_forever = 0, acked_fingerprint = NULL, snooze_until = NULL
                       WHERE id = ?""",
                    (row["id"],),
                )
                to_notify.append((row["id"], "new", it))
            elif row["ack_forever"]:
                pass  # silenced forever
            elif row["state"] == "acked" and it.fingerprint != row["acked_fingerprint"]:
                conn.execute("UPDATE items SET state = 'pending' WHERE id = ?", (row["id"],))
                to_notify.append((row["id"], "changed", it))
            # unchanged pending / snoozed: nothing (snooze wake-up has its own job)

        for ext_id, row in existing.items():
            if ext_id not in seen and row["state"] != "resolved":
                conn.execute(
                    "UPDATE items SET state = 'resolved', resolved_at = ? WHERE id = ?",
                    (ts, row["id"]),
                )

        for item_id, reason, it in to_notify:
            db.log_notification(conn, item_id, reason, it.title, _body(it))

    for item_id, reason, it in to_notify:
        notify.send_push(it.title, _body(it), it.url, tag=f"item-{item_id}")


def wake_snoozed():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE state = 'snoozed' AND snooze_until <= ?", (db.now(),)
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE items SET state = 'pending', snooze_until = NULL WHERE id = ?", (r["id"],)
            )
            db.log_notification(conn, r["id"], "snooze_expired", r["title"], "Snooze expired")
    for r in rows:
        notify.send_push(r["title"], "Snooze expired", r["url"], tag=f"item-{r['id']}")


def _body(it: WatchedItem) -> str:
    p = it.payload
    if "status" in p:  # statuspage
        parts = [f"{it.severity or ''} — {p['status']}".strip(" —")]
        if p.get("components"):
            parts.append(", ".join(p["components"][:4]))
        if p.get("body"):
            parts.append(p["body"])
        return " | ".join(parts)[:400]
    if "author" in p:  # PR
        return f"by {p.get('author')}" + (" (draft)" if p.get("draft") else "")
    return ""
