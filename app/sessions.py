"""Sessions: the cycle's tickets by big rock, the Claude Code session working on each, and those sessions — the real
CLI — side by side on the board.

The bridge lists the sessions running on the host (`GET /sessions`); what each works on and how heavy it got comes
from the context advisor (contrib/claude-code-context), shown as it says it; the Cycle tab's data says which ticket
belongs to which big rock. A session started here runs in a console the board shows (up to four, placed by the page);
one started in another terminal is listed, read-only. A ticket without a session is picked up by a fresh one that reads the ticket first:
the ticket is the memory, a session a worker you replace when its context grows.
"""
import json
import math
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Blueprint, abort, make_response, render_template, request

import agents
import config
import console
import cycle
import db
import engine
import shortcut
from watchers import WatchedItem

bp = Blueprint("sessions", __name__)
RESUME_PROMPT = ('Pick up {story}: read it and its latest "Handoff" comment if any, check its branch and pull requests, '
                 "sum up in two lines where it stands, then carry on from the next step.")
# what a row says: icon, tone, word — waiting on you first, then what moves, then what sleeps
STATES = {
    "waiting": ("i-chat", "hot", "waits for you"),
    "working": ("i-arc", "run", "working"),
    "starting": ("i-arc", "run", "starting"),
    "done": ("i-check", "ok", "answered"),
    "idle": ("i-clock", "neutral", "idle"),
}
ORDER = ("waiting", "working", "starting", "done", "idle")
ALERT = re.compile(r"zabbix-\d+")  # Watch's consoles (watch_views.api_zabbix_console): run work, a ticket or not
_layout_lock = threading.Lock()
FILED_KEEP = 200  # moves kept, the latest: a console picked up again (sc-N) goes back where you put it
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_live: dict = {"at": 0.0, "rows": [], "error": ""}


def enabled() -> bool:
    return console.enabled()


def live() -> tuple[list[dict], str]:
    """The sessions running on the host, asked of the bridge at most every 3 s."""
    if time.monotonic() - _live["at"] > 3:
        try:
            r = requests.get(f"{config.CLAUDE_BRIDGE_URL}/sessions", headers=agents._auth(), timeout=3)
            if r.status_code == 404:
                raise RuntimeError("this bridge does not list sessions — restart it (launchctl bootout, then bootstrap)")
            if not r.ok:
                raise RuntimeError((r.json() if r.content else {}).get("error") or f"HTTP {r.status_code}")
            rows, error = r.json()["sessions"], ""
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as e:
            rows, error = [], f"the Claude bridge: {e}"
        _live.update(at=time.monotonic(), rows=rows, error=error)
    return _live["rows"], _live["error"]


def _iso(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch else ""
    except (OverflowError, ValueError, OSError):  # a date no clock reads
        return ""


def _ago(seconds: float) -> str:
    return f"{int(seconds // 3600)} h" if seconds >= 3600 else f"{max(int(seconds // 60), 1)} min"


def _context(raw) -> dict:
    """The advisor's file read strictly: a field it did not write the way it writes it reads as absent — a command
    typed into a live session, a story filed under a ticket."""
    raw = raw if isinstance(raw, dict) else {}

    def num(key: str) -> float:
        value = raw.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else 0

    def text(key: str, pattern: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) and re.fullmatch(pattern, value) else ""

    return {"story": text("story", r"sc-\d{3,7}"), "project": text("project", r"[\w.-]{1,64}"), "tokens": int(num("tokens")),
            "answeredAt": num("answeredAt"), "cold": raw.get("cold") is True, "advise": text("advise", "cold|handoff"),
            "nextUsd": num("nextUsd"), "freshUsd": num("freshUsd"), "handoff": text("handoff", r"/[a-z][\w:-]{0,40}")}


def _advice(ctx: dict, now: float) -> str:
    """The context advisor's advice, in its own figures: when starting fresh beats going on."""
    tokens = ctx["tokens"] // 1000
    if ctx["advise"] == "cold":
        said = (f"{tokens}k of context, cache cold for {_ago(now - ctx['answeredAt'] / 1000)}: the next message "
                f"≈ ${ctx['nextUsd']:.2f} just to read it again, a fresh session ≈ ${ctx['freshUsd']:.2f}.")
    elif ctx["advise"] == "handoff":
        said = f"{tokens}k of context: every call reads it all again (≈ ${ctx['nextUsd']:.2f} each)."
    else:
        return ""
    return said if ctx["story"] or ctx["project"] else said + " Nothing linked to start fresh from: /ticket sc-12345 links one."


def _session_row(s: dict, now: float, home: dict) -> dict:
    ctx, spoke = _context(s.get("context")), bool(s.get("context"))
    # the advisor's story; until it speaks (or without it), a console started on a ticket is that ticket's
    named = re.fullmatch(r"(sc-\d+)(?:-[0-9a-f]{4})?", s.get("console") or "")
    story = ctx["story"] or (named.group(1) if named and not spoke else "")
    group, st = home.get(story, ("", {}))
    status = s.get("status") or ""
    # "answered" only when the advisor saw an answer (none yet after a /clear) and says the cache is still warm
    state = {"waiting": "waiting", "busy": "working", "starting": "starting"}.get(status) or (
        ("done" if ctx["answeredAt"] and not ctx["cold"] else "idle") if status == "idle" else status)
    icon, tone, word = STATES.get(state, ("i-pulse", "neutral", state))
    advice = _advice(ctx, now) if state in ("done", "idle") else ""
    return {
        "key": f"console:{s['console']}" if s.get("console") else f"session:{s.get('id')}",
        "title": s.get("title") or st.get("name") or s.get("console") or s.get("name") or "New session",
        "story": story, "url": st.get("url", ""), "group": group,
        "state": state, "icon": icon, "tone": tone, "word": word,
        "at": _iso(ctx["answeredAt"] / 1000),
        "where": "" if s.get("console") else s.get("name", ""),
        "advice": advice,
        "tokens": ctx["tokens"],
        # Start fresh: the advisor's handoff command, when it holds one and has somewhere to hand off to
        "fresh": ctx["handoff"] if advice and (ctx["story"] or ctx["project"]) else "",
        "session": s,
    }


def _glyph(st: dict) -> tuple[str, str]:
    """A ticket's state as a shape (backlog dashed, to do empty, started half full, in review three quarters) — the
    workflow's own names only for review and blocked, its state types for the rest."""
    name = st["state"].lower()
    if "block" in name:
        return "s-blocked", "warn"
    if st["state_type"] == "started":
        return ("s-review" if "review" in name else "s-started"), "dim"
    return ("s-backlog" if st["state_type"] == "backlog" else "s-todo"), "neutral"


def _story_row(st: dict, group: str) -> dict:
    icon, tone = _glyph(st)
    return {"key": f"story:{st['id']}", "title": st["name"], "story": f"sc-{st['id']}", "url": st["url"],
            "group": group, "state": "none", "icon": icon, "tone": tone, "word": st["state"],
            "at": st["updated_at"], "where": "", "advice": "", "tokens": 0, "fresh": "", "session": None}


# --- Your sections, and where you put each session ----------------------------------------------
def _layout() -> tuple[list[dict], dict]:
    """Your own sections ([{id, title}], in order) and the sessions you moved ({session key: group key})."""
    st = db.watcher_states().get("sessions", {})
    try:
        sections, filed = json.loads(st.get("sections") or "[]"), json.loads(st.get("filing") or "{}")
    except ValueError:
        return [], {}
    return (sections, filed) if isinstance(sections, list) and isinstance(filed, dict) else ([], {})


def _save(sections: list[dict] | None = None, filed: dict | None = None):
    if sections is not None:
        db.set_watcher_state("sessions", "sections", json.dumps(sections))
    if filed is not None:
        db.set_watcher_state("sessions", "filing", json.dumps(filed))


def _sources(data: dict) -> list[tuple[str, str, list]]:
    """The cycle's groups (key, title, stories): each big rock, then the run."""
    out = [(f"rock:{r['title']}", r["title"], [s for e in r["epics"] for s in e["stories"]]) for r in data.get("big_rocks") or []]
    if data.get("run"):
        out.append(("run", data["run"]["label"], [s for e in data["run"]["epics"] for s in e["stories"]]))
    return out


def _home(data: dict) -> dict:
    """The cycle's stories by key (`sc-N`): the title of the first group listing each, and the story."""
    home = {}
    for _, title, stories in _sources(data):
        for st in stories:
            home.setdefault(f"sc-{st['id']}", (title, st))
    return home


def _groups(data: dict, sections: list[dict]) -> dict:
    """Every group, in the list's order: big rocks, the run, your sections, then what runs off the cycle."""
    sources = _sources(data)
    groups = {key: {"key": key, "title": title, "own": False, "rows": [], "later": []} for key, title, _ in sources}
    for sec in sections:
        groups[f"own:{sec['id']}"] = {"key": f"own:{sec['id']}", "title": sec["title"], "own": True, "rows": [], "later": []}
    groups["off"] = {"key": "off", "title": "Off cycle" if sources else "Running", "own": False, "rows": [], "later": []}
    return groups


def board() -> dict:
    """Big rocks, then the run, your sections, then what runs off the cycle; in each, the sessions — waiting on you
    first —, then your started tickets without one; your tickets not started yet fold under them. A session goes where
    you put it, else under its ticket's group; an alert's goes with the run until its ticket says otherwise."""
    sessions, error = live()
    now = time.time()
    data = cycle.load() if cycle.enabled() else {}
    sections, filed = _layout()
    groups = _groups(data, sections)
    home, by_title = _home(data), {title: key for key, title, _ in _sources(data)}
    rows = [_session_row(s, now, home) for s in sessions]
    for row in rows:
        key = filed.get(row["key"])
        if key not in groups:  # not moved, or its section is gone
            alert = "run" in groups and ALERT.fullmatch((row["session"] or {}).get("console") or "")
            key = by_title.get(row["group"]) or ("run" if alert else "off")
        row["group"] = "" if key == "off" else groups[key]["title"]
        groups[key]["rows"].append(row)
    taken = {r["story"] for r in rows}
    for key, title, stories in _sources(data):
        for st in stories:
            if st["mine"] and f"sc-{st['id']}" not in taken:
                groups[key]["rows" if st["state_type"] == "started" else "later"].append(_story_row(st, title))
    for g in groups.values():
        for rs in (g["rows"], g["later"]):
            rs.sort(key=lambda r: r["at"], reverse=True)
            rs.sort(key=lambda r: ORDER.index(r["state"]) if r["state"] in ORDER else len(ORDER))
    return {"groups": [g for g in groups.values() if g["rows"] or g["later"] or g["own"]],
            "waiting": sum(1 for s in sessions if s.get("status") == "waiting"), "error": error}


# --- The inbox: a session that waits on you ------------------------------------------------------
WATCH = "claude_sessions"  # the inbox's source; "sessions" in watcher_state holds your sections
_answered: list[dict] = []  # the sessions that answered, cache warm, as the last watch() saw them


def watch():
    """Scheduler job: a session waiting on you is an inbox row, pushed once it has waited `session_wait_minutes`. One
    that answered is a fact of the moment, not a row to clear: kept for the mod's band (`answered()`)."""
    global _answered
    sessions, error = live()
    if error:  # the bridge away is not every session ended: the rows stay, the band says nothing
        _answered = []
        db.record_run(WATCH, error)
        return
    db.record_run(WATCH)
    now, home = time.time(), _home(cycle.load() if cycle.enabled() else {})
    rows = [(s, _session_row(s, now, home)) for s in sessions if SESSION_ID.fullmatch(s.get("id") or "")]
    _answered = [{"subject": s["id"], "ref": row["story"] or _name(s), "title": row["title"], "url": _link(s),
                  "at": _iso(_context(s.get("context"))["answeredAt"] / 1000)}
                 for s, row in rows if row["state"] == "done"]
    # a local list, it does not drop rows now and then like GitHub search: a wait answered is gone at once, and the
    # next one starts its own grace
    engine.process(WATCH, [_waiting(s, row) for s, row in rows if row["state"] == "waiting"], misses=1)


def answered() -> list[dict]:
    return _answered


def _name(s: dict) -> str:
    return s.get("console") or s.get("name") or ""


def _link(s: dict) -> str:
    return config.BASE_URL.rstrip("/") + "/sessions" + (f"?open={quote(s['console'])}" if s.get("console") else "")


def _waiting(s: dict, row: dict) -> WatchedItem:
    return WatchedItem(
        external_id=f"wait:{s['id']}", title=row["title"], url=_link(s), fingerprint="waiting", lane="session",
        urgency="later",
        payload={"subject": s["id"], "source": row["story"] or _name(s), "story": row["story"], "session": _name(s),
                 "why": "waits for you" + (f": {s['waiting_for']}" if s.get("waiting_for") else "")})


def _find(key: str) -> dict | None:
    """The row behind a key — or, for a ticket picked up or a console ended since the page listed it, the row of
    the same ticket now."""
    rows = [r for g in board()["groups"] for r in g["rows"] + g["later"]]
    row = next((r for r in rows if r["key"] == key), None)
    story = re.fullmatch(r"(?:story:|console:sc-)(\d+)(?:-[0-9a-f]{4})?", key)
    return row or (next((r for r in rows if r["story"] == f"sc-{story.group(1)}"), None) if story else None)


def _detail(template: str, **ctx):
    resp = make_response(render_template(template, **ctx))
    resp.headers["HX-Retarget"] = "#detail"
    resp.headers["HX-Reswap"] = "innerHTML"
    return resp


def _session(row: dict | None, error: str = ""):
    if not row:
        return _detail("partials/detail_gone.html")
    facts, facts_error = None, ""
    # a console's pane is its terminal: the ticket's state, PRs and handoff are for the rows without one
    if row["story"] and not (row["session"] or {}).get("console"):
        try:
            facts = shortcut.story(int(row["story"][3:]))
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
            facts_error = str(e)
    return _detail("partials/session_detail.html", row=row, facts=facts, facts_error=facts_error, error=error)


def _slug(text: str) -> str:
    """A console's name from the first words of its task: `check-kafka-lag-3f2a` reads better than a number."""
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())
    return "-".join(words[:4])[:24].strip("-") or "session"


def _start(name: str, prompt: str, fallback, story: str = "", group: str = ""):
    try:
        console.start(name, prompt, story)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return fallback(f"the Claude bridge: {e}")
    if group:
        _file(f"console:{name}", group)
    _live["at"] = 0.0
    resp = _session(_find(f"console:{name}"))
    # the list learns about it on its next poll: select it there
    resp.headers["HX-Trigger-After-Settle"] = json.dumps({"select-key": {"key": f"console:{name}"}})
    return resp


# --- Pages and actions ---------------------------------------------------------------------------
@bp.app_context_processor
def nav() -> dict:
    # full pages only draw the sidebar: a partial polled every few seconds must not list the sessions for nothing
    if not enabled() or request.headers.get("HX-Request"):
        return {"sessions_enabled": enabled()}
    return {"sessions_enabled": True, "nav_sessions": sum(1 for s in live()[0] if s.get("status") == "waiting")}


@bp.get("/sessions")
def sessions_page():
    if not enabled():
        abort(404)
    return render_template("sessions.html", active="sessions", **board())


@bp.get("/partials/sessions")
def partial_sessions():
    return render_template("partials/sessions_list.html", **board())


@bp.get("/partials/session")
def partial_session():
    key = request.args.get("key", "")
    if key == "new":
        return _new()
    return _session(_find(key))


@bp.post("/api/sessions/start")
def api_start():
    """A fresh session on a ticket: the prompt is written here (`sessions.resume_prompt`), the page only names it."""
    story = request.form.get("story", "")
    if not re.fullmatch(r"\d{3,7}", story):
        abort(400)
    prompt = ((config.load().get("sessions") or {}).get("resume_prompt") or RESUME_PROMPT).strip().format(story=f"sc-{story}")
    name = f"sc-{story}"
    console_row = _find(f"console:{name}")
    if console_row and console_row["key"] == f"console:{name}" and console_row["story"] != name:
        name = f"{name}-{secrets.token_hex(2)}"  # that console has moved on to another ticket: a new one, not it
    return _start(name, prompt, lambda error: _session(_find(f"story:{story}"), error), f"sc-{story}")


def _new(**ctx):
    """New: the task, and where it goes — the run by default when the cycle has one."""
    groups = _groups(cycle.load() if cycle.enabled() else {}, _layout()[0])
    under = ctx.pop("under", "")
    if under not in groups:
        under = "run" if "run" in groups else "off"
    return _detail("partials/session_new.html", groups=list(groups.values()), under=under, **ctx)


@bp.post("/api/sessions/new")
def api_new():
    """Off cycle files nothing: the session goes where its ticket says, if it gets one."""
    prompt = (request.form.get("prompt") or "").strip()
    under = request.form.get("under", "")
    if not prompt:
        return _new(error="Say what Claude should work on first.", under=under)
    groups = _groups(cycle.load() if cycle.enabled() else {}, _layout()[0])
    return _start(f"{_slug(prompt)}-{secrets.token_hex(2)}", prompt, lambda error: _new(prompt=prompt, under=under, error=error),
                  group=under if under in groups and under != "off" else "")


def _title(raw: str) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", raw or "").split())[:60]


def _file(key: str, group: str):
    with _layout_lock:
        sections, filed = _layout()
        filed.pop(key, None)
        filed[key] = group
        _save(filed=dict(list(filed.items())[-FILED_KEEP:]))


@bp.post("/api/sessions/file")
def api_file():
    """A session moved to a group of the list (yours, a big rock, the run, off cycle): it stays there until moved
    again, whatever its ticket says."""
    key, group = request.form.get("key", ""), request.form.get("group", "")
    kind, _, ref = key.partition(":")
    if not (kind == "console" and console.NAME.fullmatch(ref) or kind == "session" and SESSION_ID.fullmatch(ref)):
        abort(400)
    if group not in _groups(cycle.load() if cycle.enabled() else {}, _layout()[0]):
        return "That section is gone: the list is drawn again.", 409
    _file(key, group)
    return "", 204


@bp.post("/api/sessions/sections")
def api_section_add():
    """A section of yours, last before off cycle; with `key`, that session goes in it."""
    title = _title(request.form.get("title", ""))
    key = request.form.get("key", "")
    if not title:
        return "Name it first.", 400
    with _layout_lock:
        sections, filed = _layout()
        sid = secrets.token_hex(3)
        _save(sections=[*sections, {"id": sid, "title": title}])
    if key:
        kind, _, ref = key.partition(":")
        if kind == "console" and console.NAME.fullmatch(ref) or kind == "session" and SESSION_ID.fullmatch(ref):
            _file(key, f"own:{sid}")
    return {"key": f"own:{sid}"}


@bp.post("/api/sessions/sections/<sid>")
def api_section_rename(sid: str):
    title = _title(request.form.get("title", ""))
    if not title:
        return "Name it first.", 400
    with _layout_lock:
        sections, filed = _layout()
        if not any(s["id"] == sid for s in sections):
            abort(404)
        _save(sections=[{**s, "title": title} if s["id"] == sid else s for s in sections])
    return "", 204


@bp.post("/api/sessions/sections/<sid>/delete")
def api_section_delete(sid: str):
    """Its sessions go back where their tickets put them."""
    with _layout_lock:
        sections, filed = _layout()
        _save(sections=[s for s in sections if s["id"] != sid], filed={k: v for k, v in filed.items() if v != f"own:{sid}"})
    return "", 204


@bp.post("/api/sessions/end")
def api_end():
    """A console closes, a session in another terminal stops there; `quiet` (a console's ×, End on the board, the pane
    showing another one) answers nothing, the list says it on its next poll."""
    key = request.form.get("key", "")
    kind, _, ref = key.partition(":")
    if not (kind == "console" and console.NAME.fullmatch(ref) or kind == "session" and SESSION_ID.fullmatch(ref)):
        abort(404)
    before = _find(key)
    try:
        if kind == "console":
            console.end(ref)
        else:
            console.stop(ref)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return _session(before, f"the Claude bridge: {e}")
    _live["at"] = 0.0
    if request.form.get("quiet"):
        return "", 204
    # its ticket, to pick up again
    story = (before or {}).get("story", "")
    row = _find(key) or (_find(f"story:{story[3:]}") if story else None)
    return _session(row) if row else _detail("partials/detail_gone.html", gone="Session ended",
                                             why="Its transcript stays in Claude Code's history.")
