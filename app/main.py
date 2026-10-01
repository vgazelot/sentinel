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
import claude_review
import github_pr
import my_prs
import notify
from watchers import build_watchers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentinel")

app = Flask(__name__)
db.init()
db.fail_stale_reviews()

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
GH_CFG = config.load().get("watchers", {}).get("github_reviews", {})
if GH_CFG.get("login"):
    scheduler.add_job(
        my_prs.refresh, "interval", seconds=GH_CFG.get("interval_seconds", 180), id=my_prs.NAME,
        args=[GH_CFG["login"], GH_CFG.get("orgs", [])], coalesce=True, max_instances=1,
        misfire_grace_time=300, next_run_time=datetime.now(timezone.utc) + timedelta(seconds=8),
    )
scheduler.start()
log.info("scheduler started, watchers: %s", list(WATCHERS))


@app.template_filter("md")
def md(value: str | None) -> str:
    return claude_review.render(value or "")


@app.template_filter("age")
def age(value: str | None) -> str:
    """Compact age for ledger columns: 35 min, 3 h, 12 d, 4 mo."""
    if not value:
        return "—"
    then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - then).total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 1)} min"
    if secs < 86400:
        return f"{secs // 3600} h"
    if secs < 86400 * 60:
        return f"{secs // 86400} d"
    return f"{secs // (86400 * 30)} mo"


@app.context_processor
def inject_counts():
    return {"nav_pending": db.pending_count(), "nav_my_prs": len(my_prs.load()["prs"])}


@app.template_filter("elapsed")
def elapsed(value: str) -> str:
    started = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - started).total_seconds())
    return f"{secs // 60} min {secs % 60:02d} s" if secs >= 60 else f"{secs} s"


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
    # inbox banner = watchers only; the My PRs tab reports its own fetch errors
    errors = {w: s["last_error"] for w, s in states.items() if s.get("last_error") and w in WATCHERS}
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
    return render_template("index.html", active="inbox", **_items_context())


@app.get("/my-prs")
def my_prs_page():
    return render_template("my_prs.html", active="my_prs", **_my_prs_context())


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


def _my_prs_context():
    key = request.args.get("sort", "updated")
    desc = request.args.get("dir", "asc") == "desc"
    data = my_prs.load()
    prs = my_prs.sort(data["prs"], key, desc)
    return {"prs": prs, "summary": my_prs.summary(prs), "sort": key, "desc": desc,
            "last_run": data["last_run"], "last_error": data["last_error"]}


@app.get("/partials/my-prs")
def partial_my_prs():
    return render_template("partials/my_prs.html", **_my_prs_context())


@app.post("/api/my-prs/refresh")
def api_my_prs_refresh():
    if GH_CFG.get("login"):
        my_prs.refresh(GH_CFG["login"], GH_CFG.get("orgs", []))
    return partial_my_prs()


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


def _pr_panel(item_id: int, row, payload: dict, **ctx):
    """Panel rendered in place (HX-Retarget): diff + actions, or the error alone if GitHub is unreachable."""
    if "pr" not in ctx and payload.get("repo"):
        try:
            ctx["pr"] = github_pr.details(payload["repo"], payload["number"])
        except Exception as e:
            ctx.setdefault("error", str(e))
    elif not payload.get("repo"):
        ctx.setdefault("error", "repo/number not in payload yet — wait for the next fetch")
    resp = make_response(render_template("partials/pr_details.html", item=row,
                                         reviews=db.get_reviews(item_id), modes=claude_review.MODES,
                                         claude_enabled=claude_review.enabled(), **ctx))
    resp.headers["HX-Retarget"] = f"#pr-details-{item_id}"
    return resp


@app.get("/api/items/<int:item_id>/pr")
def api_pr_details(item_id: int):
    row, payload = _pr_item(item_id)
    if not row:
        return "not found", 404
    return _pr_panel(item_id, row, payload)


def _pending_comments() -> list[dict]:
    """Line comments queued in the panel, sent as a JSON list in the `comments` form field."""
    raw = request.form.get("comments") or "[]"
    try:
        comments = json.loads(raw)
    except ValueError:
        return []
    return [c for c in comments if isinstance(c, dict) and c.get("path") and c.get("line") and c.get("body")]


def _submit_review(item_id: int, event: str):
    row, payload = _pr_item(item_id)
    if not row:
        return "not found", 404
    body = (request.form.get("body") or "").strip()
    comments = _pending_comments()
    label = {"APPROVE": "Approve", "COMMENT": "Comment", "REQUEST_CHANGES": "Request changes"}[event]
    if event != "APPROVE" and not body and not comments:
        return _pr_panel(item_id, row, payload, error=f"{label} failed — write a message or add a line comment first")
    try:
        github_pr.submit_review(payload["repo"], payload["number"], event, body, comments,
                                commit_id=request.form.get("head_sha") or None)
    except Exception as e:
        return _pr_panel(item_id, row, payload, error=f"{label} failed — {e}")
    log.info("PR review %s via dashboard: %s#%s (%d line comments)", event, payload["repo"], payload["number"], len(comments))
    # reviewed = out of the inbox now, back only when the PR moves again: ack on the state that
    # already includes this review (APPROVE/REQUEST_CHANGES also drop the request → resolved next fetch)
    try:
        fingerprint = github_pr.updated_at(payload["repo"], payload["number"])
    except Exception:
        fingerprint = row["fingerprint"]
    db.set_state(item_id, "acked", ack_forever=0, acked_fingerprint=fingerprint, snooze_until=None)
    resp = make_response(partial_items())
    # tells the page to drop the queued line comments (an error keeps them)
    resp.headers["HX-Trigger"] = json.dumps({"reviewSubmitted": {"id": item_id}})
    return resp


@app.post("/api/items/<int:item_id>/approve")
def api_approve(item_id: int):
    return _submit_review(item_id, "APPROVE")


@app.post("/api/items/<int:item_id>/comment")
def api_comment(item_id: int):
    return _submit_review(item_id, "COMMENT")


@app.post("/api/items/<int:item_id>/request-changes")
def api_request_changes(item_id: int):
    return _submit_review(item_id, "REQUEST_CHANGES")


@app.post("/api/items/<int:item_id>/reply")
def api_reply(item_id: int):
    row, payload = _pr_item(item_id)
    if not row:
        return "not found", 404
    body = (request.form.get("body") or "").strip()
    if not body:
        return _pr_panel(item_id, row, payload, error="Reply failed — write something first")
    try:
        github_pr.reply(payload["repo"], payload["number"], int(request.form["comment_id"]), body)
    except Exception as e:
        return _pr_panel(item_id, row, payload, error=f"Reply failed — {e}")
    return _pr_panel(item_id, row, payload, notice="Reply posted ✔")


# --- API Claude review -----------------------------------------------------------
def _review_partial(item_id: int, row):
    return render_template("partials/claude_review.html", item=row,
                           reviews=db.get_reviews(item_id), modes=claude_review.MODES)


@app.get("/api/items/<int:item_id>/review")
def api_review(item_id: int):
    row, _ = _pr_item(item_id)
    if not row:
        return "not found", 404
    return _review_partial(item_id, row)


@app.post("/api/items/<int:item_id>/review/run")
def api_review_run(item_id: int):
    row, _ = _pr_item(item_id)
    if not row:
        return "not found", 404
    if not claude_review.enabled():
        return "CLAUDE_BRIDGE_URL not configured", 503
    mode = request.form.get("mode", "deep")
    if mode not in claude_review.MODES:
        return "unknown review mode", 400
    claude_review.start(item_id, row["url"], mode)
    return _review_partial(item_id, row)


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
