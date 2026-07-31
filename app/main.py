"""Sentinel — Flask: pages + HTMX partials + API + APScheduler scheduler."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

import config
import db
import engine
import github_pr
import notify
from watchers import build_watchers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentinel")

app = Flask(__name__)
db.init()

WATCHERS = build_watchers(config.load())

scheduler = BackgroundScheduler(timezone="UTC")
for i, watcher in enumerate(WATCHERS.values()):
    scheduler.add_job(
        engine.run_watcher,
        "interval",
        seconds=watcher.interval_seconds,
        args=[watcher],
        id=watcher.name,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
        jitter=10,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5 + i * 3),
    )
scheduler.add_job(
    engine.wake_snoozed, "interval", seconds=60, id="wake_snoozed",
    coalesce=True, max_instances=1, misfire_grace_time=120,
)
scheduler.start()
log.info("scheduler started, watchers: %s", list(WATCHERS))


@app.template_filter("localdt")
def localdt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(config.TZ)).strftime("%d/%m %H:%M")
    except ValueError:
        return value


def _items_context():
    states = db.watcher_states()
    errors = {w: s["last_error"] for w, s in states.items() if s.get("last_error")}
    return {
        "pending": db.items_by_state("pending"),
        "snoozed": db.items_by_state("snoozed"),
        "acked": db.items_by_state("acked"),
        "errors": errors,
    }


def _watchers_context():
    states = db.watcher_states()
    return {
        "watchers": [
            {
                "name": w.name,
                "interval": w.interval_seconds,
                "last_run": states.get(w.name, {}).get("last_run"),
                "last_error": states.get(w.name, {}).get("last_error"),
            }
            for w in WATCHERS.values()
        ]
    }


# --- Pages -----------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", active="dashboard", **_items_context())


@app.get("/todo")
def todo_page():
    return render_template("todo.html", active="todo", todos=db.todos())


@app.get("/history")
def history_page():
    return render_template("history.html", active="history", notifications=db.history())


@app.get("/settings")
def settings_page():
    return render_template(
        "settings.html", active="settings",
        push_enabled=bool(config.VAPID_PRIVATE_KEY),
        subscriptions=db.subscriptions(),
        **_watchers_context(),
    )


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/sw.js")
def service_worker():
    # served at the root so the service worker scope covers the whole site
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


# --- Partials HTMX ------------------------------------------------------------
@app.get("/partials/items")
def partial_items():
    return render_template("partials/items.html", **_items_context())


@app.get("/partials/watchers")
def partial_watchers():
    return render_template("partials/watchers.html", **_watchers_context())


# --- API items -----------------------------------------------------------------
@app.post("/api/items/<int:item_id>/ack")
def api_ack(item_id: int):
    with db.connect() as conn:
        row = db.get_item(conn, item_id)
    if not row:
        return "not found", 404
    forever = request.form.get("mode") == "forever"
    db.set_state(item_id, "acked", ack_forever=int(forever),
                 acked_fingerprint=row["fingerprint"], snooze_until=None)
    return partial_items()


@app.post("/api/items/<int:item_id>/snooze")
def api_snooze(item_id: int):
    duration = request.form.get("duration", "1h")
    until = _snooze_until(duration)
    if not until:
        return "bad duration", 400
    db.set_state(item_id, "snoozed", snooze_until=until, ack_forever=0)
    return partial_items()


@app.post("/api/items/<int:item_id>/unsnooze")
def api_unsnooze(item_id: int):
    db.set_state(item_id, "pending", snooze_until=None)
    return partial_items()


def _pr_item(item_id: int):
    """PR item + decoded payload, or (None, None)."""
    with db.connect() as conn:
        row = db.get_item(conn, item_id)
    if not row or row["watcher"] != "github_reviews":
        return None, None
    payload = json.loads(row["payload"] or "{}")
    return row, payload


@app.get("/api/items/<int:item_id>/pr")
def api_pr_details(item_id: int):
    row, payload = _pr_item(item_id)
    if not row:
        return "not found", 404
    if not payload.get("repo"):
        return render_template("partials/pr_details.html", item=row,
                               error="repo/number not in payload yet — wait for the next fetch")
    try:
        pr = github_pr.details(payload["repo"], payload["number"])
    except Exception as e:
        return render_template("partials/pr_details.html", item=row, error=str(e))
    return render_template("partials/pr_details.html", item=row, pr=pr)


@app.post("/api/items/<int:item_id>/approve")
def api_approve(item_id: int):
    row, payload = _pr_item(item_id)
    if not row:
        return "not found", 404
    try:
        github_pr.approve(payload["repo"], payload["number"])
    except Exception as e:
        # re-render the panel with the error instead of replacing the list
        resp = make_response(render_template("partials/pr_details.html", item=row,
                                             error=f"Approve failed — {e}"))
        resp.headers["HX-Retarget"] = f"#pr-details-{item_id}"
        return resp
    # approved → the review request disappears on the next fetch; ack ∞ meanwhile
    db.set_state(item_id, "acked", ack_forever=1,
                 acked_fingerprint=row["fingerprint"], snooze_until=None)
    log.info("PR approved via dashboard: %s#%s", payload["repo"], payload["number"])
    return partial_items()


def _snooze_until(duration: str) -> str | None:
    tz = ZoneInfo(config.TZ)
    now_local = datetime.now(tz)
    if duration == "tomorrow":
        target = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        m = re.fullmatch(r"(\d+)([mh]?)", duration.strip())
        if not m:
            return None
        minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
        target = now_local + timedelta(minutes=minutes)
    return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- API todos ------------------------------------------------------------------
def _todos_partial():
    return render_template("partials/todos.html", todos=db.todos())


@app.post("/api/todos")
def api_add_todo():
    text = (request.form.get("text") or "").strip()
    if text:
        db.add_todo(text[:300])
    return _todos_partial()


@app.post("/api/todos/<int:todo_id>/toggle")
def api_toggle_todo(todo_id: int):
    db.toggle_todo(todo_id)
    return _todos_partial()


@app.delete("/api/todos/<int:todo_id>")
def api_delete_todo(todo_id: int):
    db.delete_todo(todo_id)
    return _todos_partial()


@app.post("/api/todos/clear-done")
def api_clear_done_todos():
    db.clear_done_todos()
    return _todos_partial()


# --- API watchers / notifications ---------------------------------------------
@app.post("/api/watchers/<path:name>/run")
def api_run_watcher(name: str):
    watcher = WATCHERS.get(name)
    if not watcher:
        return "unknown watcher", 404
    engine.run_watcher(watcher)
    return partial_watchers()


@app.get("/api/notifications/pending")
def api_notifications_pending():
    since = request.args.get("since", "1970-01-01T00:00:00Z")
    rows = db.notifications_since(since)
    return jsonify([
        {"id": r["id"], "item_id": r["item_id"], "sent_at": r["sent_at"],
         "reason": r["reason"], "title": r["title"], "body": r["body"], "url": r["url"]}
        for r in rows
    ])


# --- API push -------------------------------------------------------------------
@app.get("/api/push/key")
def api_push_key():
    return jsonify({"key": config.VAPID_PUBLIC_KEY})


@app.post("/api/push/subscribe")
def api_push_subscribe():
    sub = request.get_json(silent=True)
    if not sub or "endpoint" not in sub:
        return "bad subscription", 400
    db.add_subscription(sub)
    return jsonify({"ok": True})


@app.delete("/api/push/subscribe")
def api_push_unsubscribe():
    sub = request.get_json(silent=True) or {}
    if sub.get("endpoint"):
        db.remove_subscription(sub["endpoint"])
    return jsonify({"ok": True})


@app.post("/api/push/test")
def api_push_test():
    sent = notify.send_push("Sentinel — test", "Notifications are working 🎉",
                            config.BASE_URL, tag="test")
    return jsonify({"sent": sent})
