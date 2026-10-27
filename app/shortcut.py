"""Shortcut epics: progress, open stories, last activity. Writes only on your stories: a comment, a Run ticket for an alert."""
import logging
import re
from functools import lru_cache

import requests

import config

log = logging.getLogger("sentinel.shortcut")
API = "https://api.app.shortcut.com/api/v3"
DONE_TYPES = {"done"}
HANDOFF = re.compile(r"\bhandoff\b", re.I)


def _get(path: str):
    if not config.SHORTCUT_API_TOKEN:
        raise RuntimeError("SHORTCUT_API_TOKEN missing from .env")
    r = requests.get(f"{API}{path}", headers={"Shortcut-Token": config.SHORTCUT_API_TOKEN}, timeout=25)
    r.raise_for_status()
    return r.json()


@lru_cache(maxsize=1)
def _states() -> dict:
    """{workflow_state_id: type} across every workflow — teams do not always share one."""
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


def comment(story_id: int, text: str, owner_id: str) -> str:
    """A comment on a story of yours — refused on anyone else's: a ticket belongs to its owner. Returns the story's
    updated_at once the comment is in."""
    if not owner_id or owner_id not in (_get(f"/stories/{story_id}").get("owner_ids") or []):
        raise PermissionError(f"sc-{story_id} is not yours")
    r = requests.post(f"{API}/stories/{story_id}/comments", json={"text": text},
                      headers={"Shortcut-Token": config.SHORTCUT_API_TOKEN}, timeout=25)
    r.raise_for_status()
    return _get(f"/stories/{story_id}")["updated_at"][:19] + "Z"


def open_story(epic_id: int, name: str, description: str, owner_id: str, label: str = "") -> dict:
    """Your open story of that name in the epic, else a new one, started and yours, in its team's workflow: an alert
    that comes back lands on the ticket it already has. {id, url}"""
    if not owner_id:
        raise PermissionError("no Shortcut member to own the ticket")
    name = name[:512]
    types = _states()
    for s in _get(f"/epics/{epic_id}/stories"):
        if (s["name"] == name and owner_id in (s.get("owner_ids") or []) and not s.get("archived")
                and types.get(s["workflow_state_id"]) not in DONE_TYPES):
            return {"id": s["id"], "url": s["app_url"]}
    team = _get(f"/epics/{epic_id}")["group_id"]
    flow = next(w for w in _get("/workflows") if w["id"] == _get(f"/groups/{team}")["default_workflow_id"])
    started = min((s for s in flow["states"] if s["type"] == "started"), key=lambda s: s["position"])
    r = requests.post(f"{API}/stories", headers={"Shortcut-Token": config.SHORTCUT_API_TOKEN}, timeout=25, json={
        "name": name, "description": description, "epic_id": epic_id, "group_id": team, "owner_ids": [owner_id],
        "workflow_state_id": started["id"], "labels": [{"name": label}] if label else []})
    r.raise_for_status()
    return {"id": r.json()["id"], "url": r.json()["app_url"]}


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
    # an archived story keeps its last state (often In Development): it is neither open nor done
    stories = [s for s in _get(f"/epics/{epic_id}/stories") if not s.get("archived")]
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


def story(story_id: int) -> dict:
    """A story as the Sessions page shows it before a session picks it up: its state, its open pull requests and
    the last handoff a session wrote in it (else its last comment)."""
    s = _get(f"/stories/{story_id}")
    comments = sorted((c for c in s.get("comments") or [] if (c.get("text") or "").strip() and not c.get("deleted")),
                      key=lambda c: c["created_at"])
    handoffs = [c for c in comments if HANDOFF.search(c["text"].strip().splitlines()[0])]
    note = (handoffs or comments or [None])[-1]
    return {
        "id": s["id"],
        "name": s["name"],
        "url": s["app_url"],
        "state": _state_names().get(s["workflow_state_id"], "?"),
        "prs": [{"url": p["url"], "ref": f"{p['url'].split('/')[-3]}#{p['number']}", "title": p.get("title") or "",
                 "draft": bool(p.get("draft"))}
                for p in s.get("pull_requests") or [] if not p.get("closed") and not p.get("merged")],
        "note": {"text": note["text"], "at": note["created_at"][:19] + "Z", "handoff": note in handoffs} if note else None,
    }
