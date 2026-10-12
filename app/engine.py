"""Core: runs watchers, diffs against stored state, ack/snooze state machine, notification policy.

Every item carries a lane (where it shows up) and an urgency (whether it may interrupt you):
  now   → pushed when it appears, and again when it changes after an ack
  later → pushed only if still there after the grace period (a team review nobody picked up)
  quiet → never pushed, it waits in its section (dependency bumps, maintenances, drafts)
"""
import json
import logging

import config
import db
import notify
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.engine")
# more notifications than this from a single pass → one summary push instead of a burst
STORM = 3
# GitHub search drops a PR from its results now and then: one absence is not a resolution, or the
# PR comes back as new — ack and snooze lost, pushed again (17 PRs re-pushed within 30 min, 25 times)
MISSES = 2


def run_watcher(watcher: Watcher):
    try:
        items = watcher.fetch()
    except Exception as e:
        log.warning("watcher %s failed: %s", watcher.name, e)
        db.record_run(watcher.name, str(e) or e.__class__.__name__)
        return
    db.record_run(watcher.name)
    process(watcher.name, items)


def process(watcher_name: str, fetched: list[WatchedItem]):
    """Fetched = the full current state of a watcher; absent items are resolved."""
    ts = db.now()
    to_notify: list[tuple[int, str, WatchedItem]] = []
    # first pass of a new source (or of a fresh database): everything already exists, nothing
    # just happened — record the state as the baseline instead of pushing all of it at once
    first_pass = not db.watcher_states().get(watcher_name, {}).get("seeded")

    with db.connect() as conn:
        existing = {
            r["external_id"]: r
            for r in conn.execute("SELECT * FROM items WHERE watcher = ?", (watcher_name,))
        }
        seen = set()

        for it in fetched:
            if it.external_id in seen:
                continue
            seen.add(it.external_id)
            row = existing.get(it.external_id)
            urgent = it.urgency == "now"

            if row is None:
                cur = conn.execute(
                    """INSERT INTO items (watcher, external_id, title, url, severity, payload, fingerprint,
                                          state, first_seen, last_seen, lane, urgency, notified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                    (watcher_name, it.external_id, it.title, it.url, it.severity, json.dumps(it.payload),
                     it.fingerprint, ts, ts, it.lane, it.urgency, ts if urgent else None),
                )
                if urgent:
                    to_notify.append((cur.lastrowid, "new", it))
                continue

            conn.execute(
                """UPDATE items SET title = ?, url = ?, severity = ?, payload = ?, fingerprint = ?,
                                    last_seen = ?, lane = ?, urgency = ?, missed = 0 WHERE id = ?""",
                (it.title, it.url, it.severity, json.dumps(it.payload), it.fingerprint, ts,
                 it.lane, it.urgency, row["id"]),
            )

            if first_pass and row["state"] != "resolved":
                # baseline over a database from an older version: what is there was already pushed when
                # it arrived, and an ack recorded under another fingerprint scheme still means "seen"
                conn.execute(
                    """UPDATE items SET notified_at = COALESCE(notified_at, first_seen),
                                        acked_fingerprint = CASE WHEN state = 'acked' THEN ? ELSE acked_fingerprint END
                       WHERE id = ?""",
                    (it.fingerprint, row["id"]),
                )
            elif row["state"] == "resolved":
                # reappearance (review re-requested, incident reopened, CI red again) → fresh cycle
                conn.execute(
                    """UPDATE items SET state = 'pending', resolved_at = NULL, ack_forever = 0,
                                        acked_fingerprint = NULL, snooze_until = NULL, first_seen = ?,
                                        notified_at = ? WHERE id = ?""",
                    (ts, ts if urgent else None, row["id"]),
                )
                if urgent:
                    to_notify.append((row["id"], "new", it))
            elif row["ack_forever"]:
                pass  # silenced forever
            elif row["state"] == "acked" and it.fingerprint != row["acked_fingerprint"]:
                conn.execute("UPDATE items SET state = 'pending', notified_at = ? WHERE id = ?",
                             (ts if urgent else None, row["id"]))
                if urgent:
                    to_notify.append((row["id"], "changed", it))
            elif row["state"] == "pending" and urgent and not row["notified_at"]:
                # escalation: quiet/later → now (a team request turned into a direct one)
                conn.execute("UPDATE items SET notified_at = ? WHERE id = ?", (ts, row["id"]))
                to_notify.append((row["id"], "new", it))
            # unchanged pending / snoozed: nothing (snooze wake-up and grace have their own jobs)

        for ext_id, row in existing.items():
            if ext_id in seen or row["state"] == "resolved":
                continue
            if row["missed"] + 1 >= MISSES:
                conn.execute("UPDATE items SET state = 'resolved', resolved_at = ?, missed = 0 WHERE id = ?",
                             (ts, row["id"]))
            else:
                conn.execute("UPDATE items SET missed = missed + 1 WHERE id = ?", (row["id"],))

        if first_pass:
            log.info("%s: first pass, %d item(s) recorded without notification", watcher_name, len(to_notify))
            to_notify = []
        for item_id, reason, it in to_notify:
            db.log_notification(conn, item_id, reason, it.title, _body(it))

    if first_pass:
        db.set_watcher_state(watcher_name, "seeded", ts)
    _push([(item_id, it.title, _body(it), it.url) for item_id, _, it in to_notify])


def promote_due():
    """Grace period over and still unclaimed: the item becomes yours (one push)."""
    grace = config.triage().get("team_grace_minutes", 60)
    # a source not baselined yet (first pass pending after an upgrade) has nothing "new" to promote
    seeded = [w for w, st in db.watcher_states().items() if st.get("seeded")]
    with db.connect() as conn:
        rows = [r for r in conn.execute(
            """SELECT * FROM items WHERE state = 'pending' AND urgency = 'later'
                                     AND notified_at IS NULL AND first_seen <= ?""",
            (db.ago(grace),),
        ).fetchall() if r["watcher"] in seeded]
        for r in rows:
            conn.execute("UPDATE items SET notified_at = ? WHERE id = ?", (db.now(), r["id"]))
            db.log_notification(conn, r["id"], "unclaimed", r["title"], _row_body(r, f"nobody took it in {grace} min"))
    _push([(r["id"], r["title"], _row_body(r, f"nobody took it in {grace} min"), r["url"]) for r in rows])


def wake_snoozed():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE state = 'snoozed' AND snooze_until <= ?", (db.now(),)
        ).fetchall()
        loud = []
        for r in rows:
            conn.execute("UPDATE items SET state = 'pending', snooze_until = NULL WHERE id = ?", (r["id"],))
            if r["urgency"] != "quiet":
                db.log_notification(conn, r["id"], "snooze_expired", r["title"], "Snooze expired")
                loud.append(r)
    _push([(r["id"], r["title"], "Snooze expired", r["url"]) for r in loud])


def check_health():
    """A polling job failing for a while is an item of its own: silent failures are the worst kind."""
    minutes = config.triage().get("health_minutes", 30)
    cutoff = db.ago(minutes)
    items = []
    for name, st in db.watcher_states().items():
        since = st.get("error_since") or ""
        if name == "health" or not since or since > cutoff:
            continue
        items.append(WatchedItem(
            external_id=name,
            title=f"Sentinel cannot reach {name}",
            url=config.BASE_URL.rstrip("/") + "/settings",
            fingerprint=since,
            severity="major",
            payload={"error": (st.get("last_error") or "")[:300], "since": since},
            lane="health",
            urgency="now",
        ))
    process("health", items)


def _push(notes: list[tuple[int, str, str, str]]):
    if len(notes) > STORM:
        titles = " · ".join(t for _, t, _, _ in notes[:3])
        notify.send_push(f"Sentinel — {len(notes)} new", f"{titles} …", config.BASE_URL, tag="storm")
        return
    for item_id, title, body, url in notes:
        notify.send_push(title, body, url, tag=f"item-{item_id}")


def _row_body(row, why: str) -> str:
    payload = json.loads(row["payload"] or "{}")
    who = payload.get("author")
    return f"{why}" + (f" · by {who}" if who else "")


def _body(it: WatchedItem) -> str:
    p = it.payload
    if it.lane == "incident":
        parts = [f"{it.severity or ''} — {p.get('status', '')}".strip(" —")]
        if p.get("components"):
            parts.append(", ".join(p["components"][:4]))
        if p.get("body"):
            parts.append(p["body"])
        return " | ".join(parts)[:400]
    if p.get("why"):
        return p["why"][:400]
    if p.get("author"):
        return f"by {p['author']}" + (" (draft)" if p.get("draft") else "")
    return ""
