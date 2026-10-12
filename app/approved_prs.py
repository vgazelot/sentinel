"""Bot PRs you approved — one GraphQL call, cached in watcher_state, like my_prs.

Approving a Renovate PR takes it out of the review inbox, but the automerge rule does not
always fire (major bump, red CI, conflicts, an approval dismissed by a new commit). This
keeps them in sight until they are merged and pushes once when one sits approved too long.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

import config
import db
import notify

log = logging.getLogger("sentinel.approved_prs")
NAME = "approved_prs"

QUERY = """
query($open: String!, $closed: String!) {
  open: search(query: $open, type: ISSUE, first: 100) { nodes { ...pr } }
  closed: search(query: $closed, type: ISSUE, first: 100) { nodes { ...pr } }
}
fragment pr on PullRequest {
  number title url isDraft createdAt updatedAt merged mergedAt closedAt
  reviewDecision mergeable headRefName
  repository { nameWithOwner }
  author { login }
  autoMergeRequest { enabledAt }
  latestReviews(first: 20) { nodes { state submittedAt author { login } } }
  labels(first: 6) { nodes { name } }
  commits(last: 1) { nodes { commit { statusCheckRollup {
    state
    contexts(first: 100) { nodes {
      __typename
      ... on CheckRun { name conclusion }
      ... on StatusContext { context state }
    } }
  } } } }
}
"""
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
# a review of mine that still counts as "I approved this": DISMISSED = a new commit wiped it,
# which is precisely a PR that will never merge on its own
MINE = {"APPROVED", "DISMISSED"}
# worst first: what to chase before what merges on its own
RANK = ["dismissed", "conflicts", "ci failing", "changes requested", "review required",
        "draft", "ci running", "ready to merge"]
SORT_KEYS = {
    "approved": lambda p: p["approved_at"],
    "updated": lambda p: p["updated_at"],
    "repo": lambda p: (p["repo"], p["number"]),
    "title": lambda p: p["title"].lower(),
    "status": lambda p: (RANK.index(p["status"]), p["approved_at"]),
}


def settings(cfg: dict) -> dict:
    """The block fills in from `watchers.github_reviews` so the login/orgs live in one place."""
    gh = config.load().get("watchers", {}).get("github_reviews", {})
    return {
        "login": cfg.get("login") or gh.get("login", ""),
        "orgs": cfg.get("orgs") or gh.get("orgs", []),
        "authors": cfg.get("authors") or ["renovate"],
        "stale_days": int(cfg.get("stale_days", 3)),
        "window_days": int(cfg.get("window_days", 7)),
    }


def enabled(cfg: dict | None = None) -> bool:
    cfg = config.load().get(NAME, {}) if cfg is None else cfg
    return bool(cfg.get("enabled") and config.GITHUB_TOKEN and settings(cfg)["login"])


def refresh(cfg: dict):
    opts = settings(cfg)
    try:
        try:
            data = fetch(**opts)
        except requests.ConnectionError:
            data = fetch(**opts)
    except Exception as e:
        log.warning("approved_prs fetch failed: %s", e)
        db.set_watcher_state(NAME, "last_error", str(e))
    else:
        db.set_watcher_state(NAME, "data", json.dumps(data))
        db.set_watcher_state(NAME, "last_error", "")
        _alert_stale(data, opts["stale_days"])
    db.set_watcher_state(NAME, "last_run", db.now())


def fetch(login: str, orgs: list[str], authors: list[str], stale_days: int, window_days: int) -> dict:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    scope = (f"is:pr reviewed-by:{login} "
             + " ".join(f"org:{o}" for o in orgs)
             + " " + " ".join(f"author:app/{a}" for a in authors))
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": QUERY, "variables": {
            "open": f"{scope} is:open",
            "closed": f"{scope} is:closed closed:>={since}",
        }},
        headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
    out = {}
    for bucket in ("open", "closed"):
        prs = [_normalize(n, login) for n in payload["data"][bucket]["nodes"] if n]
        out[bucket] = [p for p in prs if p]
    out["open"].sort(key=SORT_KEYS["status"])
    out["closed"].sort(key=lambda p: p["closed_at"] or "", reverse=True)
    out["window_days"] = window_days
    out["stale_days"] = stale_days
    return out


def _normalize(pr: dict, login: str) -> dict | None:
    mine = [r for r in pr["latestReviews"]["nodes"]
            if (r.get("author") or {}).get("login") == login and r["state"] in MINE]
    if not mine:
        return None
    review = mine[-1]
    commits = pr["commits"]["nodes"]
    rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
    failed = []
    for c in rollup.get("contexts", {}).get("nodes", []):
        if c["__typename"] == "CheckRun" and c.get("conclusion") in FAILED_CONCLUSIONS:
            failed.append(c["name"])
        elif c["__typename"] == "StatusContext" and c.get("state") in ("FAILURE", "ERROR"):
            failed.append(c["context"])
    out = {
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": (pr.get("author") or {}).get("login", ""),
        "branch": pr["headRefName"],
        "draft": pr["isDraft"],
        "created_at": pr["createdAt"],
        "updated_at": pr["updatedAt"],
        "merged": pr["merged"],
        "merged_at": pr.get("mergedAt"),
        "closed_at": pr.get("closedAt"),
        "approved_at": review["submittedAt"],
        "dismissed": review["state"] == "DISMISSED",
        "review": pr.get("reviewDecision"),
        "mergeable": pr.get("mergeable"),
        "automerge": bool(pr.get("autoMergeRequest")),
        "labels": [l["name"] for l in pr["labels"]["nodes"]],
        "ci": {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
               "PENDING": "pending", "EXPECTED": "pending"}.get(rollup.get("state")),
        "ci_failed": failed[:5],
    }
    out["status"] = "merged" if out["merged"] else ("closed" if out["closed_at"] else _blocker(out))
    out["waiting_days"] = _days_since(out["approved_at"])
    return out


def _blocker(pr: dict) -> str:
    """Why an approved PR is still open — first one wins, worst first."""
    if pr["dismissed"]:
        return "dismissed"
    if pr["mergeable"] == "CONFLICTING":
        return "conflicts"
    if pr["ci"] == "failure":
        return "ci failing"
    if pr["review"] == "CHANGES_REQUESTED":
        return "changes requested"
    if pr["review"] == "REVIEW_REQUIRED":
        return "review required"
    if pr["draft"]:
        return "draft"
    if pr["ci"] == "pending":
        return "ci running"
    return "ready to merge"


def _days_since(value: str | None) -> int:
    if not value:
        return 0
    then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def _alert_stale(data: dict, stale_days: int):
    """One push per PR left approved and unmerged past the threshold, re-armed every stale_days."""
    state = json.loads(db.watcher_states().get(NAME, {}).get("alerts") or "{}")
    live = {f"{p['repo']}#{p['number']}" for p in data["open"]}
    changed = False
    for key in [k for k in state if k not in live]:
        state.pop(key)
        changed = True
    for pr in data["open"]:
        key = f"{pr['repo']}#{pr['number']}"
        if pr["waiting_days"] < stale_days:
            continue
        previous = state.get(key)
        # a Renovate PR rebases constantly: re-notify on the clock, not on every push
        if previous and _days_since(previous.get("at")) < stale_days:
            continue
        notify.send_push(
            "Sentinel — PR approuvée non mergée",
            f"{pr['repo'].split('/')[-1]}#{pr['number']} — {pr['waiting_days']} j, {pr['status']}",
            config.BASE_URL.rstrip("/") + "/approved",
            tag=f"approved-{pr['repo']}-{pr['number']}",
        )
        state[key] = {"at": db.now()}
        changed = True
    if changed:
        db.set_watcher_state(NAME, "alerts", json.dumps(state))


def load() -> dict:
    state = db.watcher_states().get(NAME, {})
    data = json.loads(state.get("data") or "{}")
    return {
        "open_prs": data.get("open", []),
        "closed_prs": data.get("closed", []),
        "window_days": data.get("window_days", 7),
        "stale_days": data.get("stale_days", 3),
        "last_run": state.get("last_run"),
        "last_error": state.get("last_error"),
    }


def sort(prs: list[dict], key: str, desc: bool) -> list[dict]:
    fn = SORT_KEYS.get(key) or SORT_KEYS["status"]
    return sorted(prs, key=fn, reverse=desc)


def summary(data: dict) -> dict:
    open_prs = data["open_prs"]
    blocked = [p for p in open_prs if RANK.index(p["status"]) <= RANK.index("review required")]
    return {
        "total": len(open_prs),
        "blocked": len(blocked),
        "stale": sum(1 for p in open_prs if p["waiting_days"] >= data["stale_days"]),
        "ready": sum(1 for p in open_prs if p["status"] == "ready to merge"),
        "merged": sum(1 for p in data["closed_prs"] if p["merged"]),
        "dropped": sum(1 for p in data["closed_prs"] if not p["merged"]),
    }
