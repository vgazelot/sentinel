"""Sentinel — Flask: pages + HTMX partials + API + APScheduler scheduler."""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, make_response, redirect, render_template, request, send_from_directory

import agents
import config
import console
import cycle
import db
import deps
import engine
import followup
import github_pr
import insight
import lookout
import my_prs
import notify
import second
import sessions
from watchers import build_watchers
from console import bp as console_bp
from lookout_views import bp as lookout_bp
from sessions import bp as sessions_bp
from watch_views import bp as watch_bp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentinel")

app = Flask(__name__)
app.register_blueprint(watch_bp)
app.register_blueprint(console_bp)
app.register_blueprint(lookout_bp)
app.register_blueprint(sessions_bp)
db.init()
db.fail_stale_runs()

WATCHERS = build_watchers(config.load())
engine.retire(set(WATCHERS))
GH = config.github()
CYCLE_CFG = config.load().get(cycle.NAME, {})
FOLLOWUP_CFG = config.load().get(followup.NAME, {})


def _every(func, seconds: int, job_id: str, delay: int = 5, args=None):
    scheduler.add_job(func, "interval", seconds=seconds, id=job_id, args=args or [], coalesce=True,
                      max_instances=1, misfire_grace_time=300,
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=delay))


scheduler = BackgroundScheduler(timezone="UTC")
for i, watcher in enumerate(WATCHERS.values()):
    _every(engine.run_watcher, watcher.interval_seconds, watcher.name, delay=5 + i * 3, args=[watcher])
_every(engine.wake_snoozed, 60, "wake_snoozed")
_every(engine.promote_due, 60, "promote_due", delay=20)
_every(engine.check_health, 60, "check_health", delay=60)
if GH["login"]:
    _every(my_prs.refresh, GH["interval_seconds"], my_prs.NAME, delay=8, args=[GH["login"], GH["orgs"]])
if followup.enabled(FOLLOWUP_CFG):
    _every(followup.refresh, FOLLOWUP_CFG.get("interval_seconds", 300), followup.NAME, delay=10, args=[FOLLOWUP_CFG])
if cycle.enabled(CYCLE_CFG):
    _every(cycle.refresh, CYCLE_CFG.get("interval_seconds", 1800), cycle.NAME, delay=12, args=[CYCLE_CFG])
if sessions.enabled():
    _every(sessions.watch, 30, sessions.WATCH, delay=25)
if second.enabled():
    _every(second.tick, 30, "second", delay=40)
if agents.enabled():
    _every(agents.tick, 10, "agents", delay=15)
    _every(agents.check_bridge, 120, "claude-bridge", delay=30)
    if config.agents()["precheck"]:
        _every(deps.precheck, 300, "precheck", delay=45)
    _every(insight.refresh, 300, "insight", delay=50)
    if lookout.enabled():
        _every(lookout.collect, config.lookout()["collect_minutes"] * 60, "lookout-sources", delay=35)
        _every(lookout.advance, 120, lookout.NAME, delay=70)
if os.environ.get("SENTINEL_SCHEDULER", "1") == "1":  # 0 = tests: routes only, no background polling
    scheduler.start()
    log.info("scheduler started, watchers: %s", list(WATCHERS))


# --- No auth by design (localhost only), so no other site may drive this one --------------------
LOCAL_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal", urlparse(config.BASE_URL).hostname}


@app.before_request
def local_and_same_origin():
    """A page from another site must not use your browser to approve PRs here (cross-site form, or a
    DNS name of theirs rebound to 127.0.0.1). The Claude Code mod and curl send no Origin: allowed."""
    if request.host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
        return "unknown host", 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site") or (origin and urlparse(origin).netloc != request.host):
        return "cross-site request refused", 403
    return None



@app.after_request
def not_in_a_frame(resp):
    """No page of another site may show Sentinel in a frame: a click on Claude and the keys typed after
    it would reach a live shell (clickjacking)."""
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp


# --- Template filters ----------------------------------------------------------------
def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@app.template_filter("md")
def md(value: str | None) -> str:
    return agents.render(value or "")


@app.template_filter("age")
def age(value: str | None) -> str:
    """Compact age: 35 min, 3 h, 12 d, 4 mo."""
    if not value:
        return "—"
    secs = int((datetime.now(timezone.utc) - _parse(value)).total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 1)} min"
    if secs < 86400:
        return f"{secs // 3600} h"
    if secs < 86400 * 60:
        return f"{secs // 86400} d"
    return f"{secs // (86400 * 30)} mo"


@app.template_filter("elapsed")
def elapsed(value: str) -> str:
    secs = int((datetime.now(timezone.utc) - _parse(value)).total_seconds())
    return f"{secs // 60} min {secs % 60:02d} s" if secs >= 60 else f"{secs} s"


@app.template_filter("localdt")
def localdt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return _parse(value).astimezone(ZoneInfo(config.TZ)).strftime("%d/%m %H:%M")
    except ValueError:
        return value


@app.template_filter("domid")
def domid(value: str) -> str:
    return re.sub(r"[^\w-]", "-", value)


@app.template_filter("verdict")
def verdict(output: str | None) -> tuple[str, str]:
    return agents.verdict(output)


@app.template_filter("headline")
def headline(output: str | None) -> str:
    return agents.first_finding(output)


_CONVENTIONAL = re.compile(r"^(?:(?P<kind>[a-z][a-z-]*(?:\([^)]*\))?!?):\s*)?(?:\[(?P<ticket>sc-?\d+)\]\s*)?(?P<text>.+)$")


@app.template_filter("subject")
def subject(title: str | None) -> dict:
    """`chore(ci): [sc-12345] expire stale tags` → kind, ticket and the sentence, capitalised: in a narrow
    list the prefix ate the words that say what the PR does."""
    title = (title or "").strip()
    m = _CONVENTIONAL.match(title)
    text = m.group("text") if m else title
    return {"kind": (m and m.group("kind")) or "", "ticket": (m and m.group("ticket")) or "", "text": text[:1].upper() + text[1:]}


@app.template_filter("minutes_left")
def minutes_left(first_seen: str) -> int:
    grace = config.triage()["team_grace_minutes"]
    return max(int(grace - (datetime.now(timezone.utc) - _parse(first_seen)).total_seconds() // 60), 0)


@app.context_processor
def inject_nav():
    off_track = (sum(1 for r in cycle.load()["big_rocks"] if r["status"] in ("stalled", "behind"))
                 if cycle.enabled(CYCLE_CFG) else 0)
    pending = [_view(r) for r in db.items("state = 'pending'")]
    _clear_by_claude(pending, db.runs_for([v["subject"] for v in pending if v["lane"] == "followup"]))
    return {"nav_attention": _units(pending),
            "nav_cycle": off_track, "cycle_enabled": cycle.enabled(CYCLE_CFG),
            "claude_enabled": agents.enabled(), "agent_modes": agents.MODES, "status": _status()}


def _status() -> str:
    """The sidebar light: lit only while a source fails, instead of an inbox row per failing source."""
    down = db.items("lane = 'health' AND state != 'resolved'")
    return down[0]["title"] if down else ""


# --- View models -------------------------------------------------------------------------
def _view(row) -> dict:
    """An item row + its decoded payload, plus what every template needs to render it."""
    p = json.loads(row["payload"] or "{}")
    v = dict(row)
    v["p"] = p
    v["subject"] = f"{p['repo']}#{p['number']}" if p.get("repo") else p.get("subject", "")
    v["ref"] = (f"{p['repo'].split('/')[-1]}#{p['number']}" if p.get("repo")
                else p.get("source") or ("Sentinel" if row["lane"] == "health" else ""))
    v["headline"] = p.get("title") or p.get("name") or row["title"]
    v["attention"] = row["state"] == "pending" and (
        row["urgency"] == "now" or (row["urgency"] == "later" and bool(row["notified_at"])))
    v["tone"] = _tone(row, p, v["attention"])
    return v


def _tone(row, p: dict, attention: bool) -> str:
    """Status colour: hot = waiting on you, bad = something broke, warn = needs a look, ok = good news."""
    lane = row["lane"]
    if lane == "health":
        return "bad"
    if lane == "followup":
        return "bad" if p.get("kind") in ("failed", "reverted") else "warn"
    if lane == "mine":
        return {"merge": "ok", "threads": "warn", "no-reviewer": "warn", "nudge": "neutral"}.get(p.get("action"), "bad")
    if lane == "incident":
        return {"critical": "bad", "major": "bad", "minor": "warn"}.get(row["severity"], "neutral")
    if lane == "watch":
        return "bad" if row["severity"] in ("stop", "high", "disaster") else "warn"
    if lane == "cycle":
        return "warn"
    if lane == "fix":
        return "hot" if attention else {"pushed": "ok", "refused": "warn", "unread": "warn"}.get(p.get("step"), "neutral")
    return "hot" if attention else "neutral"


def _mine_groups(views: list[dict]) -> list[dict]:
    """Your PRs, one row per cause: ten PRs red on the same CI job are one problem, not ten."""
    groups: dict[str, dict] = {}
    for v in views:
        action = v["p"].get("action", "")
        family = (v["p"].get("families") or [""])[0] if action == "ci" else ""
        key = f"{action}:{family}"
        label = my_prs.ACTIONS.get(action, action) + (f" — {family}" if family else "")
        g = groups.setdefault(key, {"key": key, "action": action, "label": label, "tone": v["tone"], "rows": [],
                                    "family": family})
        g["rows"].append(v)
    return sorted(groups.values(), key=lambda g: (MINE_ORDER.index(g["action"]) if g["action"] in MINE_ORDER else 99,
                                                  -len(g["rows"])))


# what unblocks the most first: a red CI shared by three PRs before one alone, merging last (it waits on nobody)
MINE_ORDER = ("conflicts", "changes", "ci", "threads", "no-reviewer", "nudge", "merge")


def _clear_by_claude(views: list[dict], runs: dict):
    """A post-merge failure Claude traced to something else stops asking for you: it answered the
    question "did I break it?". It moves to Later, verdict attached."""
    for v in views:
        run = (runs.get(v["subject"]) or {}).get("followup")
        if v["lane"] == "followup" and v["p"].get("kind") == "failed" and followup.cleared(v["p"].get("checks") or [], run):
            v["attention"], v["tone"], v["cleared"] = False, "ok", True


def _followup_view(prs: list[dict]) -> tuple[list[dict], dict]:
    """Follow-up PRs with `section` set — a failure already red before the merge or traced elsewhere
    by Claude is "not yours" — and the counts every view shows, computed once here."""
    runs = db.runs_for([f"{p['repo']}#{p['number']}" for p in prs if p["stage"] == "failed"])
    for p in prs:
        run = (runs.get(f"{p['repo']}#{p['number']}") or {}).get("followup")
        p["cleared"] = followup.cleared(p.get("post_checks") or [], run)
        p["section"] = "not yours" if p["stage"] == "inherited" or (p["stage"] == "failed" and p["cleared"]) else p["stage"]
    count = {s: sum(1 for p in prs if p["section"] == s) for s in followup.STAGES + ["not yours"]}
    count["blocked"] = sum(1 for p in prs if p["stage"] == "waiting"
                           and followup.RANK.index(p["blocker"]) <= followup.RANK.index("review required"))
    return prs, count


# the inbox: the day's plan, your sessions and the monitoring alerts on top, the health light in the sidebar, cycle in
# its tab, status pages wait in Later
FOR_YOU = ("you", "team", "followup")
INBOX = ("plan", "session", "watch", "fix") + FOR_YOU + ("mine",)
TABS = ("priority", "later", "done")


def _units(views: list[dict]) -> int:
    """What the badges count: the rows of Priority — each item waiting on you, your PRs once per cause
    (approved and green included: never pushed, still yours to merge)."""
    return (sum(1 for v in views if v["attention"] and v["lane"] in INBOX and v["lane"] != "mine")
            + len(_mine_groups([v for v in views if v["state"] == "pending" and v["lane"] == "mine"])))


def _inbox(notice: str = "", error: str = "") -> dict:
    views = [_view(r) for r in db.items("state != 'resolved'")]
    groups = deps.pending_groups()
    runs = db.runs_for([v["subject"] for v in views if v["subject"]] + [f"deps:{g['key']}" for g in groups]
                       + [f"why:{f}" for v in views for f in (v["p"].get("families") or [])[:1]]
                       + [f"asks:{v['subject']}" for v in views if v["lane"] == "mine"])
    _clear_by_claude(views, runs)
    attention = [v for v in views if v["attention"]]
    pending = [v for v in views if v["state"] == "pending"]
    fu = followup.load() if followup.enabled(FOLLOWUP_CFG) else {"prs": [], "stale_days": 3}
    fu_prs, landing = _followup_view(fu["prs"])

    def newest(vs):
        return sorted(vs, key=lambda v: v["first_seen"], reverse=True)

    later = {
        "snoozed": sorted((v for v in views if v["state"] == "snoozed"), key=lambda v: v["snooze_until"] or ""),
        "grace": sorted((v for v in pending if v["lane"] == "team" and not v["attention"]), key=lambda v: v["first_seen"]),
        "dep_groups": groups,
        "incidents": newest(v for v in pending if v["lane"] == "incident"),
        "radar": newest(v for v in pending if v.get("cleared") or (
            v["urgency"] == "quiet" and v["lane"] not in ("deps", "mine", "incident", "fix"))),
        "others": sorted((p for p in my_prs.load()["prs"] if p["action"] not in my_prs.PUSHED + my_prs.LISTED),
                         key=lambda p: p["updated_at"], reverse=True),
        # what is moving and fine — a dismissed or stuck one is already in the inbox
        "landing_live": [p for p in fu_prs if p["stage"] == "checking" or (
            p["stage"] == "waiting" and not p["dismissed"] and p["waiting_days"] < fu.get("stale_days", 3))],
    }
    acked = [v for v in views if v["state"] == "acked"][:40]
    # broken after a merge first, then the reviews people have waited on longest
    for_you = sorted((v for v in attention if v["lane"] in FOR_YOU),
                     key=lambda v: (0 if v["p"].get("kind") in ("failed", "reverted") else 1 if v["lane"] == "you"
                                    else 2 if v["lane"] == "team" else 3, v["first_seen"]))
    mine = _mine_groups([v for v in pending if v["lane"] == "mine"])
    return {
        **later,
        "today": [v for v in attention if v["lane"] == "plan"],
        "sessions": sorted((v for v in attention if v["lane"] == "session"), key=lambda v: v["first_seen"]),
        "watch": newest(v for v in attention if v["lane"] == "watch"),
        # what the Second does on each alert
        "seconds": second.for_items([v["id"] for v in attention if v["lane"] == "watch"]),
        # the Second's fixes: the one waiting for your push first, then what it pushed or left, newest first
        "fixes": sorted(newest(v for v in pending if v["lane"] == "fix"), key=lambda v: not v["attention"]),
        "for_you": for_you,
        "mine": mine,
        "acked": acked,
        "attention": _units(views),
        "attention_rows": attention,
        "mine_prs": sum(len(g["rows"]) for g in mine),
        # what may need you later; your PRs waiting on others and what is landing are only for reading
        "counts": {"later": sum(len(later[k]) for k in ("snoozed", "grace", "dep_groups", "incidents", "radar"))},
        "dep_count": sum(len(g["prs"]) for g in groups),
        "landing": landing,
        "runs": runs,
        "notice": notice,
        "error": error,
    }


def _tab() -> str:
    """The inbox tab: from the query string, or — for an action posted from the detail pane — from the
    page the action came from."""
    tab = request.args.get("tab") or parse_qs(urlparse(request.headers.get("HX-Current-URL", "")).query).get("tab", [""])[0]
    return tab if tab in TABS else "priority"


# --- Pages -----------------------------------------------------------------------------
@app.get("/")
def inbox_page():
    return render_template("inbox.html", active="inbox", tab=_tab(), **_inbox())


@app.get("/my-prs")
@app.get("/follow-up")
@app.get("/approved")
def old_pages():
    # My PRs and Follow-up became parts of the inbox; old pushes and bookmarks still land somewhere useful
    return redirect("/" if request.path == "/my-prs" else "/?tab=later", code=301)


@app.get("/cycle")
def cycle_page():
    return render_template("cycle.html", active="cycle", **cycle.load())


@app.get("/settings")
def settings_page():
    return render_template(
        "settings.html", active="settings",
        push_enabled=bool(config.VAPID_PRIVATE_KEY) and not config.PUSH_DISABLED,
        subscriptions=db.subscriptions(), triage=config.triage(), agent_cfg=config.agents(),
        bridge=config.CLAUDE_BRIDGE_URL, consoles=[{**c, "started": datetime.fromtimestamp(c["created"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                  for c in console.sessions()],
        **_watchers_context(), **_agents_context(),
    )


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.get("/sw.js")
def service_worker():
    # served at the root so the service worker scope covers the whole site
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


# --- Partials --------------------------------------------------------------------------------
@app.get("/partials/inbox")
def partial_inbox():
    return render_template("partials/inbox_list.html", tab=_tab(), **_inbox())


@app.get("/partials/cycle")
def partial_cycle():
    return render_template("partials/cycle.html", **cycle.load())


@app.post("/api/cycle/refresh")
def api_cycle_refresh():
    if cycle.enabled(CYCLE_CFG):
        cycle.refresh(CYCLE_CFG)
    return partial_cycle()


def _watchers_context() -> dict:
    states = db.watcher_states()
    jobs = [(w.name, w.interval_seconds) for w in WATCHERS.values()]
    if GH["login"]:
        jobs.append((my_prs.NAME, GH["interval_seconds"]))
    if followup.enabled(FOLLOWUP_CFG):
        jobs.append((followup.NAME, FOLLOWUP_CFG.get("interval_seconds", 300)))
    if cycle.enabled(CYCLE_CFG):
        jobs.append((cycle.NAME, CYCLE_CFG.get("interval_seconds", 1800)))
    if sessions.enabled():
        jobs.append((sessions.WATCH, 30))
    return {"watchers": [{"name": name, "interval": interval, "runnable": name in WATCHERS,
                          **{k: states.get(name, {}).get(k) for k in ("last_run", "last_error", "error_since")}}
                         for name, interval in jobs]}


def _agents_context() -> dict:
    with db.connect() as conn:
        recent = conn.execute("SELECT * FROM agent_runs ORDER BY COALESCE(started_at, created_at) DESC LIMIT 12").fetchall()
    return {"recent_runs": recent, "runs_today": agents.used_today()}


@app.get("/partials/watchers")
def partial_watchers():
    return render_template("partials/watchers.html", **_watchers_context())


# --- Detail pane -------------------------------------------------------------------------------
def _detail(template: str, **ctx):
    resp = make_response(render_template(template, **ctx))
    resp.headers["HX-Retarget"] = "#detail"
    resp.headers["HX-Reswap"] = "innerHTML"
    return resp


def _gone():
    return _detail("partials/detail_gone.html")


@app.get("/partials/item/<int:item_id>")
def partial_item(item_id: int):
    with db.connect() as conn:
        row = db.get_item(conn, item_id)
    if not row:
        return _gone()
    if row["watcher"] == "github_reviews":
        return _review(row)
    v = _view(row)
    if v["lane"] == "mine":
        return _mine_detail(v)
    if v["lane"] == "fix":
        return _fix_detail(v)
    runs = db.runs_for([v["subject"]])
    _clear_by_claude([v], runs)
    job = second.job(v["p"]["job"]) if v["lane"] == "plan" and v["p"].get("job") else second.for_items([v["id"]]).get(v["id"])
    return _detail("partials/detail_item.html", v=v, runs=runs, job=job)


def _fix_detail(v: dict, notice: str = "", error: str = ""):
    job = second.job(v["p"].get("job") or 0)
    if job:
        job["meta_dict"] = json.loads(job.get("meta") or "{}")
    result = (job or {}).get("meta_dict", {}).get("result") or {}
    ready = bool(job and result.get("moved") and not job["meta_dict"].get("pushed") and not job["meta_dict"].get("removed")
                 and not result.get("busy") and not result.get("dirty"))
    return _detail("partials/detail_fix.html", v=v, job=job, ready=ready, notice=notice, error=error,
                   console_alive=bool(job and job["console"] in console.alive()))


@app.post("/api/second/<int:job_id>/push")
def api_second_push(job_id: int):
    """Your click on a fix ready under review: Sentinel checks GitHub again (still open, yours, at the head the fix started
    from), then the bridge pushes, leased on that head."""
    try:
        said, failed = second.push(job_id, clicked=True), ""
    except (ValueError, RuntimeError, LookupError, requests.RequestException) as e:
        said, failed = "", f"Not pushed: {e}"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE watcher = ? AND external_id = ?",
                           (second.FIX, f"fix:{(second.job(job_id) or {}).get('subject', '')}")).fetchone()
    if not row:
        return _gone()
    return _fix_detail(_view(row), notice="Pushed: GitHub runs its checks now" if said == "pushed" else said, error=failed)


@app.post("/api/second/<int:job_id>/post")
def api_second_post(job_id: int):
    """Your click on a real alert's diagnosis: what it saw goes on its Run ticket, its commands stay here."""
    second.post_alert(job_id)
    return partial_item((second.job(job_id) or {}).get("item_id") or 0)


@app.get("/partials/group")
def partial_group():
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()]
    rows = [_view(r) for r in db.items(f"id IN ({','.join('?' * len(ids))})", *ids)] if ids else []
    if not rows:
        return _gone()
    if len(rows) == 1:
        return _mine_detail(rows[0])
    g = _mine_groups(rows)[0]
    why = f"why:{g['family']}"
    return _detail("partials/detail_group.html", g=g, why=why, runs=db.runs_for([why]))


@app.get("/partials/dep")
def partial_dep():
    group = next((g for g in deps.pending_groups() if g["key"] == request.args.get("key")), None)
    if not group:
        return _gone()
    subject = f"deps:{group['key']}"
    return _detail("partials/detail_dep.html", g=group, subject=subject, runs=db.runs_for([subject]))


def _pr_details(payload: dict, ctx: dict):
    """The PR as GitHub has it now, or the error in its place: the pane still shows what Sentinel knows."""
    if payload.get("repo"):
        try:
            ctx["pr"] = github_pr.details(payload["repo"], payload["number"])
        except Exception as e:  # noqa: BLE001
            ctx.setdefault("error", str(e))


def _mine_detail(v: dict):
    """One of your PRs: what it waits for, the failing checks, what reviewers asked — and Claude's reading."""
    ctx: dict = {}
    _pr_details(v["p"], ctx)
    why = f"why:{(v['p'].get('families') or [''])[0]}"
    asks = f"asks:{v['subject']}"
    return _detail("partials/detail_mine.html", v=v, why=why, asks=asks, runs=db.runs_for([why, asks]), **ctx)


def _review(row, **ctx):
    """A review request: the PR from GitHub (diff, checks, reviews) and the three review actions."""
    payload = json.loads(row["payload"] or "{}")
    if not payload.get("repo"):
        ctx.setdefault("error", "repo/number not in payload yet — wait for the next fetch")
    elif "pr" not in ctx:
        _pr_details(payload, ctx)
    subject = f"{payload.get('repo')}#{payload.get('number')}"
    # the devil's advocate explores with tools: not on a bot's PR, whose text (release notes) nobody vouches for
    modes = ["brief"] if payload.get("author_bot") else ["brief", "deep"]
    return _detail("partials/detail_review.html", v=_view(row), subject=subject, modes=modes,
                   runs=db.runs_for([subject]).get(subject, {}), **ctx)


# --- Item actions --------------------------------------------------------------------------
def _done(notice: str = "", error: str = ""):
    """HTMX gets the refreshed list (the page then selects the next row), anything else (the Claude
    Code mod) a JSON ack."""
    if not request.headers.get("HX-Request"):
        return jsonify({"ok": not error, "notice": notice, "error": error}), (400 if error else 200)
    resp = make_response(render_template("partials/inbox_list.html", tab=_tab(), **_inbox()))
    resp.headers["HX-Retarget"] = "#list"
    resp.headers["HX-Reswap"] = "innerHTML"
    resp.headers["HX-Trigger-After-Settle"] = json.dumps({"acted": {"notice": notice, "error": error}})
    return resp


def _ack(row):
    db.set_state(row["id"], "acked", ack_forever=0, acked_fingerprint=row["fingerprint"], snooze_until=None)


@app.post("/api/items/<int:item_id>/ack")
def api_ack(item_id: int):
    with db.connect() as conn:
        row = db.get_item(conn, item_id)
    if not row:
        return "not found", 404
    _ack(row)
    return _done()


@app.post("/api/items/bulk")
def api_bulk():
    """Done or Later on several items at once — a group row of your PRs."""
    ids = [int(x) for x in request.form.get("ids", "").split(",") if x.strip().isdigit()]
    action = request.form.get("action", "")
    until = _snooze_until(request.form.get("duration", "tomorrow")) if action == "snooze" else None
    if action not in ("ack", "snooze") or (action == "snooze" and not until):
        return "bad action", 400
    with db.connect() as conn:
        rows = [r for r in (db.get_item(conn, i) for i in ids) if r]
    for row in rows:
        if action == "snooze":
            db.set_state(row["id"], "snoozed", snooze_until=until, ack_forever=0)
        else:
            _ack(row)
    return _done()


@app.post("/api/items/<int:item_id>/unack")
def api_unack(item_id: int):
    db.set_state(item_id, "pending", ack_forever=0, acked_fingerprint=None, snooze_until=None)
    return _done()


@app.post("/api/items/<int:item_id>/snooze")
def api_snooze(item_id: int):
    until = _snooze_until(request.form.get("duration", "tomorrow"))
    if not until:
        return "bad duration", 400
    db.set_state(item_id, "snoozed", snooze_until=until, ack_forever=0)
    return _done()


@app.post("/api/items/<int:item_id>/unsnooze")
def api_unsnooze(item_id: int):
    db.set_state(item_id, "pending", snooze_until=None)
    return _done()


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


# --- Reviews --------------------------------------------------------------------------------
def _approve(row, payload: dict, body: str = "", event: str = "APPROVE", head_sha: str | None = None):
    """One review on GitHub, then ack the item on the PR state that already includes it: out of the
    inbox now, back only if the PR moves (APPROVE/REQUEST_CHANGES also drop the request → resolved)."""
    github_pr.submit_review(payload["repo"], payload["number"], event, body, commit_id=head_sha)
    fingerprint = row["fingerprint"]
    if row["lane"] == "you":  # the only lane whose fingerprint follows updated_at
        try:
            fingerprint = github_pr.updated_at(payload["repo"], payload["number"])
        except Exception:  # noqa: BLE001
            pass
    db.set_state(row["id"], "acked", ack_forever=0, acked_fingerprint=fingerprint, snooze_until=None)


def _submit_review(item_id: int, event: str):
    with db.connect() as conn:
        row = db.get_item(conn, item_id)
    if not row or row["watcher"] != "github_reviews":
        return "not found", 404
    payload = json.loads(row["payload"] or "{}")
    body = (request.form.get("body") or "").strip()
    label = {"APPROVE": "Approve", "COMMENT": "Comment", "REQUEST_CHANGES": "Request changes"}[event]
    if event != "APPROVE" and not body:
        return _review(row, error=f"{label} failed — write a message first")
    try:
        _approve(row, payload, body, event, head_sha=request.form.get("head_sha") or None)
    except Exception as e:  # noqa: BLE001
        return _review(row, error=f"{label} failed — {e}")
    log.info("PR review %s via dashboard: %s#%s", event, payload["repo"], payload["number"])
    return _done(notice=f"{label} sent on {payload['repo'].split('/')[-1]}#{payload['number']}")


@app.post("/api/items/<int:item_id>/approve")
def api_approve(item_id: int):
    return _submit_review(item_id, "APPROVE")


@app.post("/api/items/<int:item_id>/comment")
def api_comment(item_id: int):
    return _submit_review(item_id, "COMMENT")


@app.post("/api/items/<int:item_id>/request-changes")
def api_request_changes(item_id: int):
    return _submit_review(item_id, "REQUEST_CHANGES")


# --- Dependency bumps -----------------------------------------------------------------------
@app.post("/api/deps/approve")
def api_deps_approve():
    """Approve the green PRs of one bump the page showed you — those still green on the very commit you
    saw; one a bot pushed to since, one added to the group since, or a red one is left to you."""
    group = next((g for g in deps.pending_groups() if g["key"] == request.form.get("key")), None)
    if not group:
        return _done(error="That bump is no longer waiting for review")
    # "id:sha" pairs: the commit the page showed as green, not whatever the last poll saw
    shown = dict(x.split(":", 1) for x in request.form.get("ids", "").split(",") if ":" in x)
    done, failed = [], []
    targets = []
    for p in group["approvable"]:
        try:
            seen = shown.get(str(p["item_id"]))
            if seen and seen == p.get("head_sha") and github_pr.head_sha(p["repo"], p["number"]) == seen:
                targets.append(p)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{p['repo'].split('/')[-1]}#{p['number']} ({e})")
    skipped = [f"{p['repo'].split('/')[-1]}#{p['number']}" for p in group["prs"]
               if p not in targets and not any(f.startswith(f"{p['repo'].split('/')[-1]}#{p['number']} (") for f in failed)]
    with db.connect() as conn:
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM items WHERE id IN ({','.join('?' * len(group['prs']))})",
            [p["item_id"] for p in group["prs"]])}
    for p in targets:
        ref = f"{p['repo'].split('/')[-1]}#{p['number']}"
        try:
            _approve(rows[p["item_id"]], p, head_sha=p["head_sha"])
            done.append(ref)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{ref} ({e})")
    log.info("bump %s approved on %s, skipped %s, failed %s", group["key"], done, skipped, failed)
    notice = f"Approved {', '.join(done)}." if done else ""
    if skipped:
        notice += f" Left for you (not green, moved since you looked, or new): {', '.join(skipped)}."
    return _done(notice=notice.strip(), error=f"Failed: {'; '.join(failed)}" if failed else "")


# --- Claude ------------------------------------------------------------------------------------
def _agent_target(subject: str, mode: str) -> tuple[str, dict] | None:
    """What a run on this subject needs, rebuilt from Sentinel's own data — the page only names it."""
    if mode in insight.MODES:
        return insight.target(subject, mode)
    if subject.startswith("lookout:"):
        return lookout.target(subject, mode)
    if subject.startswith("deps:"):
        group = next((g for g in deps.pending_groups() if f"deps:{g['key']}" == subject), None)
        return (group["prs"][0]["url"], deps.context(group)) if group and mode == "precheck" else None
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", subject)
    if not m:
        return None
    url = f"https://github.com/{m.group(1)}/pull/{m.group(2)}"
    if mode == "followup":
        pr = next((p for p in followup.load()["prs"] if f"{p['repo']}#{p['number']}" == subject), None)
        return (url, followup.context(pr)) if pr and pr["stage"] == "failed" else None
    return (url, {}) if mode in agents.ON_DEMAND else None


def _agent_block(subject: str, modes: list[str]):
    return render_template("partials/agent.html", subject=subject, modes=modes,
                           runs=db.runs_for([subject]).get(subject, {}))


@app.get("/partials/agent")
def partial_agent():
    modes = [m for m in request.args.get("modes", "").split(",") if m in agents.MODES]
    return _agent_block(request.args.get("subject", ""), modes)


@app.post("/api/agent")
def api_agent():
    subject, mode = request.form.get("subject", ""), request.form.get("mode", "")
    modes = [m for m in request.form.get("modes", mode).split(",") if m in agents.MODES]
    if not agents.enabled():
        return "CLAUDE_BRIDGE_URL not configured", 503
    target = _agent_target(subject, mode)
    if not target:
        return "nothing to run for that", 404
    agents.request(subject, mode, *target)
    return _agent_block(subject, modes)


# --- API: watchers, summary for the Claude Code mod, notifications ------------------------------
@app.post("/api/watchers/<path:name>/run")
def api_run_watcher(name: str):
    watcher = WATCHERS.get(name)
    if not watcher:
        return "unknown watcher", 404
    engine.run_watcher(watcher)
    return partial_watchers()


@app.get("/api/summary")
def api_summary():
    """Everything the Claude Code mod shows: counts, what needs you, notifications after `since`."""
    ctx = _inbox()
    since = request.args.get("since", type=int)
    runs = ctx["runs"]
    attention = ctx["attention_rows"]

    def brief(v: dict) -> dict:
        run = next((r for r in runs.get(v["subject"], {}).values() if r["status"] == "done"), None)
        return {"id": v["id"], "lane": v["lane"], "ref": v["ref"], "title": v["headline"], "url": v["url"],
                "why": v["p"].get("why", ""), "since": v["first_seen"], "subject": v["subject"],
                # when it began to need you: pushed, else listed
                "at": v["notified_at"] or v["first_seen"],
                "claude": agents.summary_line(run["output"]) if run else ""}

    def lanes(*names):
        return sum(1 for v in attention if v["lane"] in names)

    notes = db.notifications_after(since if since is not None else db.last_notification_id())
    return jsonify({
        "attention": ctx["attention"],
        "counts": {"health": lanes("health"), "reviews": lanes("you", "team"), "mine": lanes("mine"),
                   "followup": lanes("followup"), "incidents": len(ctx["incidents"]), "watch": lanes("watch"), "cycle": lanes("cycle"),
                   "deps": ctx["dep_count"], "grace": len(ctx["grace"]), "radar": len(ctx["radar"]),
                   "broken": sum(1 for v in attention if v["lane"] == "followup" and v["p"].get("kind") in ("failed", "reverted"))},
        "landing": ctx["landing"],
        # sessions first, alerts after the reviews and PRs: a storm must not push them out of the 25
        "items": [brief(v) for v in sorted((v for v in attention if v["lane"] in INBOX),
                                           key=lambda v: (v["lane"] != "session", v["lane"] == "watch"))][:25],
        "notifications": [{"id": n["id"], "title": n["title"], "body": n["body"], "url": n["url"],
                           "reason": n["reason"], "lane": n["lane"]} for n in notes],
        # the sessions that answered, cache warm: the band's, never a row of the inbox
        "answered": sessions.answered(),
        "seq": db.last_notification_id(),
        "agents": {"running": db.count_runs("status = 'running'"), "queued": db.count_runs("status = 'queued'")},
        "base_url": config.BASE_URL,
    })


@app.get("/api/notifications/pending")
def api_notifications_pending():
    """contrib/host-notifier: notifications sent after `since` whose item still waits for you."""
    since = request.args.get("since", "1970-01-01T00:00:00Z")
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT n.*, i.url FROM notifications n JOIN items i ON i.id = n.item_id
               WHERE n.sent_at > ? AND i.state = 'pending' ORDER BY n.sent_at""", (since,)).fetchall()
    return jsonify([{"id": r["id"], "item_id": r["item_id"], "sent_at": r["sent_at"], "reason": r["reason"],
                     "title": r["title"], "body": r["body"], "url": r["url"]} for r in rows])


# --- API push ------------------------------------------------------------------------------------
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
    sent = notify.send_push("Sentinel — test", "Notifications are working", config.BASE_URL, tag="test")
    return jsonify({"sent": sent})
