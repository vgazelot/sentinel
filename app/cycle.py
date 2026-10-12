"""Cycle tab: your Big Rocks (Slite) joined with their epics (Shortcut), plus the RUN line.

Slite owns the objectives, Shortcut owns the live progress, config.yaml owns the link
between the two (it exists nowhere else). Cached as JSON in watcher_state like my_prs.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import config
import db
import notify
import shortcut
import slite

log = logging.getLogger("sentinel.cycle")
NAME = "cycle"


def enabled(cfg: dict | None = None) -> bool:
    """`cfg` is the `cycle:` block of config.yaml; both API keys are required."""
    cfg = config.load().get(NAME, {}) if cfg is None else cfg
    return bool(cfg.get("enabled") and config.SLITE_API_KEY and config.SHORTCUT_API_TOKEN)


def refresh(cfg: dict):
    try:
        data = build(cfg)
    except Exception as e:
        log.warning("cycle refresh failed: %s", e)
        db.set_watcher_state(NAME, "last_error", str(e))
    else:
        db.set_watcher_state(NAME, "data", json.dumps(data))
        db.set_watcher_state(NAME, "last_error", "")
        _alert_stalled(data, cfg)
    db.set_watcher_state(NAME, "last_run", db.now())


def build(cfg: dict) -> dict:
    note = slite.current_note(**(cfg.get("slite") or {}))
    rocks = slite.parse(note.get("content") or "")
    owner = (cfg.get("owner") or "").strip().lower()
    mine = [r for r in rocks if not owner or r["owner"].strip().lower() == owner]
    owner_id = shortcut.member_id(cfg.get("shortcut_member", ""))
    mapping = cfg.get("epics") or {}
    stall_days = int(cfg.get("stall_days", 7))

    label = shortcut.current_label(cfg.get("label_prefix", "Infra"), cfg.get("label", ""))
    labelled = shortcut.label_epics(label["id"]) if label else []

    calendar = _calendar(cfg, note.get("title", ""))
    behind_points = int(cfg.get("behind_points", 10))
    big_rocks = []
    for rock in mine:
        epics = [shortcut.epic(eid, owner_id) for eid in _epics_for(rock["title"], mapping, labelled)]
        rock = {**rock, "epics": epics, **_progress(epics, stall_days)}
        big_rocks.append({**rock, **_verdict(rock, calendar.get("pct"), behind_points)})
    big_rocks.sort(key=lambda r: (RANK.get(r["status"], 9), r["delta"] if r["delta"] is not None else 0))

    run_cfg = cfg.get("run") or {}
    run_ids = run_cfg.get("epics") or _by_name(run_cfg.get("match", ""), labelled)
    run_epics = [shortcut.epic(eid, owner_id) for eid in run_ids]
    run = {"label": run_cfg.get("label", "RUN"), "epics": run_epics, **_progress(run_epics, stall_days)} if run_epics else None

    return {
        "note": {"id": note["id"], "title": note["title"].strip(), "url": note["url"],
                 "last_edited": (note.get("lastEditedAt") or "")[:19] + "Z"},
        "label": label["name"] if label else "",
        "cycle": calendar,
        "big_rocks": big_rocks,
        "run": run,
    }


RANK = {"stalled": 0, "behind": 1, "unlinked": 2, "on track": 3}


def _verdict(rock: dict, cycle_pct: int | None, behind_points: int) -> dict:
    """The answer the tab exists for: is this objective keeping up with the calendar?

    Measured on the note's outputs, not on story progress: the epics carry stories closed in
    earlier cycles, so their percentage flatters a big rock that has delivered nothing this one.
    """
    done = sum(1 for o in rock["outputs"] if o["status"] == "done")
    total = len(rock["outputs"])
    delta = None if (cycle_pct is None or not total) else round(done * 100 / total) - cycle_pct
    if not rock["linked"]:
        status = "unlinked"
    elif rock["stalled"]:
        status = "stalled"
    elif delta is not None and delta < -behind_points:
        status = "behind"
    else:
        status = "on track"
    return {
        "status": status,
        "delta": delta,
        "outputs_done": done,
        "outputs_total": total,
        "todo": [_short(o["label"]) for o in rock["outputs"] if o["status"] != "done"],
    }


def _short(label: str) -> str:
    """"Output 2: ADR + PoC" → "ADR + PoC" — the numbering is noise in a one-line summary."""
    return re.sub(r"^output\s*\d*\s*:?\s*", "", label, flags=re.I).strip() or label


def _epics_for(title: str, mapping: dict, labelled: list[dict]) -> list[int]:
    """Epics carrying the cycle label whose name contains the big rock title.

    Infra epics repeat the big rock verbatim ("Infra 2026 09-10 - <big rock>"), so the label plus
    the naming convention does the join on its own. `cycle.epics` in config.yaml stays an explicit
    override for the odd big rock whose epics are named differently or carry no label.
    """
    want = _norm(title)
    for key, ids in mapping.items():
        key_n = _norm(key)
        if key_n and (key_n == want or key_n in want or want in key_n):
            return list(ids)
    return _by_name(title, labelled)


def _by_name(title: str, labelled: list[dict]) -> list[int]:
    want = _norm(title)
    return [e["id"] for e in labelled if want and want in _norm(e["name"])] if labelled else []


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _progress(epics: list[dict], stall_days: int) -> dict:
    total = sum(e["total"] for e in epics)
    done = sum(e["done"] for e in epics)
    activity = max([e["last_activity_mine"] or e["last_activity"] for e in epics], default="")
    stalled_days = _days_since(activity)
    return {
        "stories_total": total,
        "stories_done": done,
        "stories_open": sum(e["open"] for e in epics),
        "mine_open": sum(e["mine_open"] for e in epics),
        "pct": round(done * 100 / total) if total else 0,
        "last_activity": activity,
        "stalled_days": stalled_days,
        "stalled": stalled_days is not None and stalled_days >= stall_days,
        "linked": bool(epics),
    }


def _days_since(stamp: str) -> int | None:
    if not stamp:
        return None
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


MONTHS = {"jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "avr": 4, "may": 5, "mai": 5, "jun": 6,
          "jul": 7, "jui": 7, "aug": 8, "aou": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
TITLE_RE = re.compile(r"(\d{4})\s+([^\W\d_]+)\s*/\s*([^\W\d_]+)", re.UNICODE)


def _calendar(cfg: dict, note_title: str = "") -> dict:
    """Cycle bounds read off the note title ("2026 September/October …"), config as a fallback."""
    bounds = _bounds_from_title(note_title)
    if not bounds and cfg.get("start") and cfg.get("end"):
        bounds = (_date(cfg["start"]), _date(cfg["end"]))
    if not bounds:
        return {}
    start, end = bounds
    total = (end - start).days + 1
    elapsed = min(max((date.today() - start).days + 1, 0), total)
    return {"start": start.isoformat(), "end": end.isoformat(), "days_total": total,
            "days_elapsed": elapsed, "days_left": total - elapsed,
            "pct": round(elapsed * 100 / total) if total else 0}


def _bounds_from_title(title: str) -> tuple[date, date] | None:
    """"2026 September/October Infra Big Rocks" → 2026-09-01 … 2026-10-31."""
    m = TITLE_RE.search(title or "")
    if not m:
        return None
    year = int(m.group(1))
    first = MONTHS.get(m.group(2)[:3].lower())
    last = MONTHS.get(m.group(3)[:3].lower())
    if not (first and last):
        return None
    end_year = year + 1 if last < first else year
    next_month = date(end_year + (last == 12), (last % 12) + 1, 1)
    return date(year, first, 1), next_month - timedelta(days=1)


def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _alert_stalled(data: dict, cfg: dict):
    """One push per big rock that went quiet, re-armed when it moves again or after stall_days."""
    stall_days = int(cfg.get("stall_days", 7))
    state = json.loads(db.watcher_states().get(NAME, {}).get("alerts") or "{}")
    changed = False
    for rock in data["big_rocks"]:
        key, activity = rock["title"], rock["last_activity"]
        if not (rock["stalled"] and rock["linked"]):
            if state.pop(key, None) is not None:
                changed = True
            continue
        previous = state.get(key)
        if previous and previous.get("activity") == activity and _days_since(previous.get("at", "")) < stall_days:
            continue
        todo = sum(1 for o in rock["outputs"] if o["status"] != "done")
        notify.send_push(
            "Sentinel — big rock figé",
            f"{rock['title']} — rien depuis {rock['stalled_days']} j"
            + (f", {todo} output{'s' if todo > 1 else ''} à faire" if todo else ""),
            config.BASE_URL.rstrip("/") + "/cycle",
            tag=f"cycle-{_norm(key).replace(' ', '-')}",
        )
        state[key] = {"at": db.now(), "activity": activity}
        changed = True
    if changed:
        db.set_watcher_state(NAME, "alerts", json.dumps(state))


def load() -> dict:
    state = db.watcher_states().get(NAME, {})
    data = json.loads(state.get("data") or "{}")
    return {
        "note": data.get("note"),
        "label": data.get("label"),
        "calendar": data.get("cycle") or {},
        "big_rocks": data.get("big_rocks") or [],
        "run": data.get("run"),
        "last_run": state.get("last_run"),
        "last_error": state.get("last_error"),
    }
