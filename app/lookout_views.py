"""The Lookout page: this week's finds, the ones you want to try, and what the filter left out (so you can
tell it it was wrong)."""
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, render_template, request

import config
import db
import lookout

bp = Blueprint("lookout", __name__)
TABS = ("finds", "try", "filtered")
# a release says so in its category: its source would only repeat it
SHORT = {"hackernews": "HN", "lobsters": "Lobsters", "trending": "GitHub trending", "releases": ""}


def _label(source: str) -> str:
    kind, _, name = source.partition(":")
    return SHORT[source] if source in SHORT else {"reddit": f"r/{name}", "feed": name}.get(kind, source)


def _view(row) -> dict:
    v = dict(row)
    v["mentions"] = [{**m, "label": _label(m["source"])} for m in lookout.mentions(row)]
    v["points"] = max((m.get("points") or 0 for m in v["mentions"]), default=0)
    v["facts"] = json.loads(row["facts"] or "{}")
    return v


def context(tab: str) -> dict:
    cfg = config.lookout()
    slot = lookout.last_slot(cfg["digest"])
    since = lookout.iso(slot) if slot else ""
    unread = [_view(r) for r in lookout.finds("feedback IS NULL")]
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT stage, COUNT(*) FROM lookout GROUP BY stage").fetchall())
        week = db.ago(7 * 1440)
        filtered = conn.execute(
            "SELECT * FROM lookout WHERE feedback IS NULL AND first_seen >= ? AND (stage = 'dropped' OR "
            "(stage = 'assessed' AND (score IS NULL OR score < ? OR verdict = 'skip'))) ORDER BY first_seen DESC LIMIT 200",
            (week, int(cfg["threshold"]))).fetchall()
    states = lookout.source_states()
    to_try = [_view(r) for r in lookout.finds("feedback = 'try'")]
    return {
        "tab": tab,
        "fresh": [v for v in unread if (v["assessed_at"] or "") >= since],
        "earlier": [v for v in unread if (v["assessed_at"] or "") < since],
        "to_try": to_try,
        # the likeliest misses first: read closely but scored under the threshold, then the most upvoted
        "filtered": sorted((_view(r) for r in filtered), key=lambda v: (v["stage"] != "assessed", -(v["score"] or 0), -v["points"])),
        "unread": len(unread),
        "waiting": counts.get("new", 0) + counts.get("scouting", 0),
        "reading": counts.get("kept", 0) + counts.get("assessing", 0),
        "assessed": counts.get("assessed", 0),
        "runs": db.runs_for([f"lookout:{v['id']}" for v in unread + to_try] + [lookout.SUBJECT]),
        "sources": states,
        "failing": [s for s in states if s["error"]],
        "next_digest": (slot + timedelta(days=7)) if slot else None,
        "threshold": cfg["threshold"],
    }


@bp.app_context_processor
def nav() -> dict:
    if not lookout.enabled():
        return {"lookout_enabled": False}
    return {"lookout_enabled": True, "nav_lookout": len(lookout.finds("feedback IS NULL"))}


def _tab() -> str:
    tab = request.values.get("tab", "")
    return tab if tab in TABS else "finds"


@bp.get("/lookout")
def lookout_page():
    if not lookout.enabled():
        abort(404)
    return render_template("lookout.html", active="lookout", **context(_tab()))


@bp.get("/partials/lookout")
def partial_lookout():
    return render_template("partials/lookout.html", **context(_tab()))


@bp.post("/api/lookout/<int:find_id>/feedback")
def api_feedback(find_id: int):
    """useful / noise / known / try, or "" to take it back: the find leaves the list, the filter learns."""
    if not lookout.set_feedback(find_id, request.form.get("value", "")):
        return "unknown find or feedback", 400
    return partial_lookout()
