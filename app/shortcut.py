"""Shortcut epics: progress, open stories, last activity. Read-only."""
import logging
import re
from functools import lru_cache

import requests

import config

log = logging.getLogger("sentinel.shortcut")
API = "https://api.app.shortcut.com/api/v3"
DONE_TYPES = {"done"}


def _get(path: str):
    if not config.SHORTCUT_API_TOKEN:
        raise RuntimeError("SHORTCUT_API_TOKEN missing from .env")
    r = requests.get(f"{API}{path}", headers={"Shortcut-Token": config.SHORTCUT_API_TOKEN}, timeout=25)
    r.raise_for_status()
    return r.json()


@lru_cache(maxsize=1)
def _states() -> dict:
    """{workflow_state_id: type} across every workflow — Infra and Mention do not share one."""
    return {s["id"]: s["type"] for w in _get("/workflows") for s in w["states"]}


@lru_cache(maxsize=1)
def _state_names() -> dict:
    return {s["id"]: s["name"] for w in _get("/workflows") for s in w["states"]}


# Cycle labels follow `<Prefix>-<year>-C<n>` (Team-2026-C5): the highest one is the live cycle.
CYCLE_LABEL_RE = re.compile(r"^(?P<prefix>[A-Za-z]+)-(?P<year>\d{4})-C(?P<n>\d+)$")


def current_label(prefix: str = "Infra", name: str = "") -> dict | None:
    """The cycle's label: `name` if config pins one, else the highest `<prefix>-<year>-C<n>`."""
    labels = _get("/labels")
    if name:
        return next((l for l in labels if l["name"].lower() == name.lower()), None)
    best, best_key = None, ()
    for label in labels:
        m = CYCLE_LABEL_RE.match(label["name"])
        if not m or m.group("prefix").lower() != prefix.lower():
            continue
        key = (int(m.group("year")), int(m.group("n")))
        if key > best_key:
            best, best_key = label, key
    return best


def label_epics(label_id: int) -> list[dict]:
    """Just id + name: the full epic is only fetched for the ones a big rock actually claims."""
    return [{"id": e["id"], "name": e["name"]} for e in _get(f"/labels/{label_id}/epics")]


def member_id(mention_name: str) -> str:
    if not mention_name:
        return ""
    for m in _get("/members"):
        if m["profile"]["mention_name"].lower() == mention_name.lower():
            return m["id"]
    log.warning("shortcut member %r not found", mention_name)
    return ""


def epic(epic_id: int, owner_id: str = "") -> dict:
    """Epic + its stories, normalized. `owner_id` marks which stories are yours."""
    meta = _get(f"/epics/{epic_id}")
    stories = _get(f"/epics/{epic_id}/stories")
    types, names = _states(), _state_names()
    open_stories, done, mine_open = [], 0, 0
    last_activity = last_activity_mine = ""
    for s in stories:
        mine = owner_id in (s.get("owner_ids") or [])
        state_type = types.get(s["workflow_state_id"], "unstarted")
        last_activity = max(last_activity, s["updated_at"][:19] + "Z")
        if mine:
            last_activity_mine = max(last_activity_mine, s["updated_at"][:19] + "Z")
        if state_type in DONE_TYPES:
            done += 1
            continue
        mine_open += mine
        open_stories.append({
            "id": s["id"],
            "name": s["name"],
            "url": s["app_url"],
            "state": names.get(s["workflow_state_id"], "?"),
            "state_type": state_type,
            "updated_at": s["updated_at"][:19] + "Z",
            "mine": mine,
        })
    open_stories.sort(key=lambda s: (not s["mine"], s["updated_at"]), reverse=False)
    total = len(stories)
    return {
        "id": epic_id,
        "name": meta["name"],
        "url": meta["app_url"],
        "state": meta["state"],
        "deadline": (meta.get("deadline") or "")[:10],
        "total": total,
        "done": done,
        "open": len(open_stories),
        "mine_open": mine_open,
        "pct": round(done * 100 / total) if total else 0,
        "last_activity": last_activity,
        "last_activity_mine": last_activity_mine,
        "stories": open_stories,
    }
