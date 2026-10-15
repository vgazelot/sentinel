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
            """SELECT * FROM items WHERE state = 'pending' AND urgency = 'later' AND missed = 0
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
            "SELECT * FROM items WHERE state = 'snoozed' AND missed = 0 AND snooze_until <= ?", (db.now(),)
        ).fetchall()
        loud = []
        for r in rows:
            conn.execute("UPDATE items SET state = 'pending', snooze_until = NULL WHERE id = ?", (r["id"],))
            if r["urgency"] != "quiet":
                db.log_notification(conn, r["id"], "snooze_expired", r["title"], "Snooze expired")
                loud.append(r)
    _push([(r["id"], r["title"], "Snooze expired", r["url"]) for r in loud])


# sources named from the config: one per status page, one per dashboard link
CONFIGURED = ("statuspage:", "grafana:")


def retire(current: set[str]):
    """A configured source gone from the config (a link removed or edited) is never read again: its items
    are resolved and its state dropped, else they wait forever and its last failure keeps the light red."""
    with db.connect() as conn:
        names = {r["watcher"] for r in conn.execute("SELECT DISTINCT watcher FROM items WHERE state != 'resolved'")}
        names |= {r["watcher"] for r in conn.execute("SELECT DISTINCT watcher FROM watcher_state")}
        for name in sorted(n for n in names if n.startswith(CONFIGURED) and n not in current):
            log.info("%s is no longer in the config: its items are resolved", name)
            conn.execute("UPDATE items SET state = 'resolved', resolved_at = ?, missed = 0 WHERE watcher = ? AND state != 'resolved'",
                         (db.now(), name))
            conn.execute("DELETE FROM watcher_state WHERE watcher = ?", (name,))


def check_health():
    """Sources failing for a while are one item, however many: they fail together (network, machine
    asleep) — 40 pushes in two days said nothing one push does not. Silent failures stay the worst kind."""
    cutoff = db.ago(config.triage().get("health_minutes", 30))
    failing = {name: st for name, st in db.watcher_states().items()
               if name != "health" and st.get("error_since") and st["error_since"] <= cutoff}
    items = []
    if failing:
        names = sorted(failing)
        items.append(WatchedItem(
            external_id="sources",
            title="Sentinel cannot reach " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else ""),
            url=config.BASE_URL.rstrip("/") + "/settings",
            fingerprint="down",  # one more source joining the outage is the same outage
            severity="major",
            payload={"sources": names, "since": min(st["error_since"] for st in failing.values()),
                     "error": (failing[names[0]].get("last_error") or "")[:300]},
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
    if p.get("why"):
        return p["why"][:400]
    if p.get("author"):
        return f"by {p['author']}" + (" (draft)" if p.get("draft") else "")
    return ""
