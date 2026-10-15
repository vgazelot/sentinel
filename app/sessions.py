"""Sessions: the cycle's tickets by big rock, the Claude Code session working on each, and that session — the real
CLI — in the detail pane.

The bridge lists the sessions running on the host (`GET /sessions`); what each works on and how heavy it got comes
from the context advisor (contrib/claude-code-context), shown as it says it; the Cycle tab's data says which ticket
belongs to which big rock. A session started here runs in a console shown in the pane; one started in another
terminal is listed, read-only. A ticket without a session is picked up by a fresh one that reads the ticket first:
the ticket is the memory, a session a worker you replace when its context grows.
"""
import json
import math
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone

import requests
from flask import Blueprint, abort, make_response, render_template, request

import agents
import config
import console
import cycle
import shortcut

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


def _story_row(st: dict, group: str) -> dict:
    return {"key": f"story:{st['id']}", "title": st["name"], "story": f"sc-{st['id']}", "url": st["url"],
            "group": group, "state": "none", "icon": "i-dot", "tone": "neutral", "word": st["state"],
            "at": st["updated_at"], "where": "", "advice": "", "tokens": 0, "fresh": "", "session": None}


def board() -> dict:
    """Big rocks, then the run, then what runs off the cycle; in each, the sessions — waiting on you first —, then
    your started tickets without one; your tickets not started yet fold under them."""
    sessions, error = live()
    now = time.time()
    data = cycle.load() if cycle.enabled() else {}
    sources = [(r["title"], [s for e in r["epics"] for s in e["stories"]]) for r in data.get("big_rocks") or []]
    if data.get("run"):
        sources.append((data["run"]["label"], [s for e in data["run"]["epics"] for s in e["stories"]]))
    home = {}
    for title, stories in sources:
        for st in stories:
            home.setdefault(f"sc-{st['id']}", (title, st))
    groups = {title: {"title": title, "rows": [], "later": []} for title, _ in sources}
    off = {"title": "Off cycle" if sources else "Running", "rows": [], "later": []}
    for s in sessions:
        row = _session_row(s, now, home)
        (groups[row["group"]] if row["group"] else off)["rows"].append(row)
    taken = {r["story"] for g in [*groups.values(), off] for r in g["rows"]}
    for title, stories in sources:
        for st in stories:
            if st["mine"] and f"sc-{st['id']}" not in taken:
                groups[title]["rows" if st["state_type"] == "started" else "later"].append(_story_row(st, title))
    for g in [*groups.values(), off]:
        for rows in (g["rows"], g["later"]):
            rows.sort(key=lambda r: r["at"], reverse=True)
            rows.sort(key=lambda r: ORDER.index(r["state"]) if r["state"] in ORDER else len(ORDER))
    return {"groups": [g for g in groups.values() if g["rows"] or g["later"]] + ([off] if off["rows"] else []),
            "waiting": sum(1 for s in sessions if s.get("status") == "waiting"), "error": error}


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


def _start(name: str, prompt: str, fallback, story: str = ""):
    try:
        console.start(name, prompt, story)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return fallback(f"the Claude bridge: {e}")
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
        return _detail("partials/session_new.html")
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


@bp.post("/api/sessions/new")
def api_new():
    prompt = (request.form.get("prompt") or "").strip()
    if not prompt:
        return _detail("partials/session_new.html", error="Say what Claude should work on first.")
    return _start(f"{_slug(prompt)}-{secrets.token_hex(2)}", prompt,
                  lambda error: _detail("partials/session_new.html", prompt=prompt, error=error))


@bp.post("/api/sessions/end")
def api_end():
    name = request.form.get("name", "")
    if not console.NAME.fullmatch(name):
        abort(404)
    try:
        console.end(name)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return _session(_find(f"console:{name}"), f"the Claude bridge: {e}")
    _live["at"] = 0.0
    row = _find(f"console:{name}")
    return _session(row) if row else _detail("partials/detail_gone.html", gone="Session ended",
                                             why="Its transcript stays in Claude Code's history.")
