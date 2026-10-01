"""Your own open PRs across the configured orgs — one GraphQL search, cached in watcher_state."""
import json
import logging

import requests

import config
import db

log = logging.getLogger("sentinel.my_prs")
NAME = "my_prs"
QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    issueCount
    nodes { ... on PullRequest {
      number title url isDraft createdAt updatedAt reviewDecision mergeable additions deletions headRefName
      repository { nameWithOwner }
      labels(first: 6) { nodes { name } }
      reviewRequests { totalCount }
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
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
SORT_KEYS = {
    "updated": lambda p: p["updated_at"],
    "created": lambda p: p["created_at"],
    "repo": lambda p: (p["repo"], p["number"]),
    "title": lambda p: p["title"].lower(),
    "ci": lambda p: {"failure": 0, "pending": 1, None: 2, "success": 3}[p["ci"]],
    "review": lambda p: {"CHANGES_REQUESTED": 0, "REVIEW_REQUIRED": 1, None: 2, "APPROVED": 3}[p["review"]],
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
        db.set_watcher_state(NAME, "last_error", str(e))
    else:
        db.set_watcher_state(NAME, "data", json.dumps(prs))
        db.set_watcher_state(NAME, "last_error", "")
    db.set_watcher_state(NAME, "last_run", db.now())


def fetch(login: str, orgs: list[str]) -> list[dict]:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    q = f"is:pr is:open author:{login} " + " ".join(f"org:{o}" for o in orgs)
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
    return [_normalize(n) for n in data["data"]["search"]["nodes"] if n]


def _normalize(pr: dict) -> dict:
    commits = pr["commits"]["nodes"]
    rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
    failed = []
    for c in rollup.get("contexts", {}).get("nodes", []):
        if c["__typename"] == "CheckRun" and c.get("conclusion") in FAILED_CONCLUSIONS:
            failed.append(c["name"])
        elif c["__typename"] == "StatusContext" and c.get("state") in ("FAILURE", "ERROR"):
            failed.append(c["context"])
    state = rollup.get("state")
    ci = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
          "PENDING": "pending", "EXPECTED": "pending"}.get(state)
    return {
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
        "review_requests": pr["reviewRequests"]["totalCount"],
        "comments": pr["comments"]["totalCount"],
        "ci": ci,
        "ci_failed": failed[:5],
    }


def load() -> dict:
    state = db.watcher_states().get(NAME, {})
    return {
        "prs": json.loads(state.get("data") or "[]"),
        "last_run": state.get("last_run"),
        "last_error": state.get("last_error"),
    }


def sort(prs: list[dict], key: str, desc: bool) -> list[dict]:
    fn = SORT_KEYS.get(key) or SORT_KEYS["updated"]
    return sorted(prs, key=fn, reverse=desc)


def summary(prs: list[dict]) -> dict:
    return {
        "total": len(prs),
        "awaiting": sum(1 for p in prs if not p["draft"] and p["review"] == "REVIEW_REQUIRED"),
        "changes": sum(1 for p in prs if p["review"] == "CHANGES_REQUESTED"),
        "approved": sum(1 for p in prs if p["review"] == "APPROVED"),
        "ci_failing": sum(1 for p in prs if p["ci"] == "failure"),
        "conflicting": sum(1 for p in prs if p["mergeable"] == "CONFLICTING"),
        "drafts": sum(1 for p in prs if p["draft"]),
    }
