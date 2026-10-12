"""Your own open PRs: one GraphQL search, cached in watcher_state, plus the next move on each.

The list itself is the My PRs page. The PRs waiting on *you* (conflicts, changes requested,
red CI, approved and green, open threads) also become engine items, so a transition pushes
once — "#1608 changes requested", "#1517 ready to merge" — instead of a tab you must remember.
"""
import json
import logging
from datetime import datetime, timezone

import requests

import checks
import config
import db
import engine
from watchers import WatchedItem

log = logging.getLogger("sentinel.my_prs")
NAME = "my_prs"
QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes { ... on PullRequest {
      id number title url isDraft createdAt updatedAt reviewDecision mergeable additions deletions headRefName
      repository { nameWithOwner }
      labels(first: 6) { nodes { name } }
      reviewRequests(first: 10) { nodes { requestedReviewer {
        __typename ... on User { login } ... on Team { slug } } } }
      latestReviews(first: 10) { nodes { state author { login } } }
      reviewThreads(first: 50) { nodes { isResolved } }
      comments { totalCount }
      commits(last: 1) { nodes { commit { statusCheckRollup {
        state
        contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name conclusion status }
          ... on StatusContext { context state }
        } }
      } } } }
    } }
  }
}
"""
# what the PR is waiting for, most urgent first; the first five are on you and push once
ACTIONS = {
    "conflicts": "Conflicts to resolve",
    "changes": "Changes requested",
    "ci": "CI failing",
    "merge": "Approved and green — merge it",
    "threads": "Open review threads",
    "no-reviewer": "Nobody was asked to review",
    "nudge": "No movement for a while — nudge a reviewer",
    "running": "CI running",
    "waiting": "Waiting for a review",
    "draft": "Drafts",
}
PUSHED = ("conflicts", "changes", "ci", "merge", "threads")
LISTED = ("no-reviewer", "nudge")  # shown on Now as a reminder, never pushed
SORT_KEYS = {
    "updated": lambda p: p["updated_at"],
    "created": lambda p: p["created_at"],
    "repo": lambda p: (p["repo"], p["number"]),
    "title": lambda p: p["title"].lower(),
    "action": lambda p: (list(ACTIONS).index(p["action"]), p["updated_at"]),
}


def refresh(login: str, orgs: list[str]):
    try:
        try:
            prs = fetch(login, orgs)
        except requests.ConnectionError:
            # GitHub drops idle keep-alive connections now and then: one retry on a fresh connection
            prs = fetch(login, orgs)
    except Exception as e:
        log.warning("my_prs fetch failed: %s", e)
        db.record_run(NAME, str(e) or e.__class__.__name__)
        return
    db.set_watcher_state(NAME, "data", json.dumps(prs))
    db.record_run(NAME)
    engine.process(NAME, items(prs))


def fetch(login: str, orgs: list[str]) -> list[dict]:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    q = f"is:pr is:open archived:false author:{login} " + " ".join(f"org:{o}" for o in orgs)
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": QUERY, "variables": {"q": q}},
        headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
    stale = config.triage()["stale_review_days"]
    return [_normalize(n, stale) for n in data["data"]["search"]["nodes"] if n]


def _normalize(pr: dict, stale_days: int) -> dict:
    commits = pr["commits"]["nodes"]
    rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
    failed = checks.failed_names(rollup.get("contexts", {}).get("nodes", []))
    requested = [n["requestedReviewer"] for n in pr["reviewRequests"]["nodes"] if n.get("requestedReviewer")]
    out = {
        "id": pr["id"],
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "branch": pr["headRefName"],
        "draft": pr["isDraft"],
        "created_at": pr["createdAt"],
        "updated_at": pr["updatedAt"],
        "review": pr.get("reviewDecision"),
        "mergeable": pr.get("mergeable"),
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "labels": [l["name"] for l in pr["labels"]["nodes"]],
        "reviewers": [r.get("login") or f"@{r.get('slug')}" for r in requested],
        "approved_by": [r["author"]["login"] for r in pr["latestReviews"]["nodes"]
                        if r["state"] == "APPROVED" and r.get("author")],
        "changes_by": [r["author"]["login"] for r in pr["latestReviews"]["nodes"]
                       if r["state"] == "CHANGES_REQUESTED" and r.get("author")],
        "unresolved": sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"]),
        "comments": pr["comments"]["totalCount"],
        "ci": checks.rollup(rollup.get("state")),
        "ci_failed": failed[:8],
        "ci_family": sorted({checks.family(n) for n in failed}),
    }
    out["action"] = next_action(out, stale_days)
    return out


def next_action(pr: dict, stale_days: int) -> str:
    if pr["draft"]:
        return "draft"
    if pr["mergeable"] == "CONFLICTING":
        return "conflicts"
    if pr["review"] == "CHANGES_REQUESTED":
        return "changes"
    if pr["ci"] == "failure":
        return "ci"
    if pr["review"] == "APPROVED" and pr["ci"] in ("success", None):
        return "merge"
    if pr["unresolved"] and pr["review"] != "APPROVED":
        return "threads"
    if pr["ci"] == "pending":
        return "running"
    if pr["review"] == "REVIEW_REQUIRED" and not pr["reviewers"] and not pr["approved_by"]:
        return "no-reviewer"
    if _days_since(pr["updated_at"]) >= stale_days:
        return "nudge"
    return "waiting"


def items(prs: list[dict]) -> list[WatchedItem]:
    out = []
    for p in prs:
        if p["action"] not in PUSHED + LISTED:
            continue
        why = ACTIONS[p["action"]].lower()
        if p["action"] == "ci" and p["ci_family"]:
            why += f" · {', '.join(p['ci_family'][:2])}"
        elif p["action"] == "changes" and p["changes_by"]:
            why += f" · {', '.join(p['changes_by'])}"
        elif p["action"] == "merge" and p["approved_by"]:
            why = f"approved by {', '.join(p['approved_by'])} · CI green — merge it"
        elif p["action"] == "threads":
            why = f"{p['unresolved']} open review thread{'s' if p['unresolved'] > 1 else ''}"
        elif p["action"] == "nudge":
            why = f"no movement for {_days_since(p['updated_at'])} d · {', '.join(p['reviewers'][:3])}"
        out.append(WatchedItem(
            external_id=p["id"],
            title=f"{p['repo'].split('/')[-1]}#{p['number']} — {p['title']}",
            url=p["url"],
            # re-notify after an ack only when the PR waits on something else
            fingerprint=p["action"] + (":" + ",".join(p["ci_family"]) if p["action"] == "ci" else ""),
            payload={"repo": p["repo"], "number": p["number"], "title": p["title"], "action": p["action"],
                     "why": why, "ci": p["ci"], "branch": p["branch"], "families": p["ci_family"],
                     "updated_at": p["updated_at"]},
            lane="mine",
            urgency="now" if p["action"] in PUSHED else "quiet",
        ))
    return out


def _days_since(value: str | None) -> int:
    if not value:
        return 0
    then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def load() -> dict:
    state = db.watcher_states().get(NAME, {})
    return {
        # a cache written by an older version (no `action`) waits for the next refresh instead of
        # breaking every page in the seconds after an upgrade
        "prs": [p for p in json.loads(state.get("data") or "[]") if "action" in p],
        "last_run": state.get("last_run"),
        "last_error": state.get("last_error"),
    }


def sort(prs: list[dict], key: str, desc: bool) -> list[dict]:
    fn = SORT_KEYS.get(key) or SORT_KEYS["action"]
    return sorted(prs, key=fn, reverse=desc)


def groups(prs: list[dict]) -> list[dict]:
    """Sections of the My PRs page, in ACTIONS order; red CI split by failing check family."""
    out = []
    for action, label in ACTIONS.items():
        members = sorted((p for p in prs if p["action"] == action), key=lambda p: p["updated_at"])
        if not members:
            continue
        if action == "ci":
            fams: dict[str, int] = {}
            for p in members:
                for f in p["ci_family"] or ["?"]:
                    fams[f] = fams.get(f, 0) + 1
            label = f"{label} — " + " · ".join(f"{f} ×{n}" for f, n in sorted(fams.items(), key=lambda x: -x[1]))
        out.append({"action": action, "label": label, "prs": members, "yours": action in PUSHED + LISTED})
    return out
