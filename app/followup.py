"""Follow-up: the PRs you approved, until they have landed — merged *and* verified.

Approving takes a PR out of your inbox, but approving is not merging (automerge stalls on a
dismissed approval, red CI, conflicts) and merging is not landing: the post-merge pipelines
(terraform apply, deploy, release) run where nobody looks. Each PR you approved goes through:

  waiting    approved, still open — what blocks it, for how long
  checking   merged — checks on the merge commit still running
  landed     every post-merge check passed
  failed     a post-merge check failed → item + Claude investigates whether this PR caused it
  inherited  the failing checks were already red on the commit before the merge → listed, not alerted
  reverted   somebody reverted it → item
  no checks  merged, nothing ran after the merge (nothing to verify)

Only what needs you becomes an item (a dismissed approval, a PR stuck for stale_days, a
failure, a revert). One GraphQL call per refresh, cached in watcher_state like my_prs.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

import agents
import config
import db
import engine
from watchers import WatchedItem

log = logging.getLogger("sentinel.followup")
NAME = "followup"

QUERY = """
query($open: String!, $closed: String!) {
  open: search(query: $open, type: ISSUE, first: 100) { nodes { ...pr } }
  closed: search(query: $closed, type: ISSUE, first: 100) { nodes { ...pr ...landing } }
}
fragment pr on PullRequest {
  id number title url isDraft createdAt updatedAt merged mergedAt closedAt
  reviewDecision mergeable headRefName
  repository { nameWithOwner }
  author { login }
  autoMergeRequest { enabledAt }
  latestReviews(first: 20) { nodes { state submittedAt author { login } } }
  labels(first: 8) { nodes { name } }
  commits(last: 1) { nodes { commit { statusCheckRollup {
    state
    contexts(first: 100) { nodes {
      __typename
      ... on CheckRun { name conclusion }
      ... on StatusContext { context state }
    } }
  } } } }
}
fragment landing on PullRequest {
  mergeCommit { oid
    statusCheckRollup { contexts(first: 100) { nodes {
      __typename
      ... on CheckRun { name status conclusion detailsUrl completedAt
                        checkSuite { workflowRun { event databaseId workflow { name } } } }
      ... on StatusContext { context state targetUrl createdAt }
    } } }
    parents(first: 1) { nodes { statusCheckRollup { contexts(first: 100) { nodes {
      __typename
      ... on CheckRun { name status conclusion }
      ... on StatusContext { context state }
    } } } } }
  }
  timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 10) { nodes {
    ... on CrossReferencedEvent { source { ... on PullRequest { number title url createdAt } } }
  } }
}
"""
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
# runs that belong to the PR or the merge queue, not to what happens once merged
PRE_MERGE_EVENTS = {"merge_group", "pull_request", "pull_request_target", "pull_request_review"}
# a review of mine that still counts as "I approved this": DISMISSED = a new commit wiped it,
# which is precisely a PR that will never merge on its own
MINE = {"APPROVED", "DISMISSED"}
# worst first: what to chase before what merges on its own
RANK = ["dismissed", "conflicts", "ci failing", "changes requested", "review required",
        "draft", "ci running", "ready to merge"]
# pipelines need a moment to start after the merge: "no checks" only after this
CHECKS_START_MINUTES = 30
AUTO_INVESTIGATE_HOURS = 48
STAGES = ["failed", "reverted", "waiting", "checking", "inherited", "landed", "no checks", "closed"]


def settings(cfg: dict) -> dict:
    gh = config.github()
    authors = cfg.get("authors") or ["renovate", "dependabot"]
    return {
        "login": gh["login"],
        "orgs": gh["orgs"],
        # `all` follows every PR you approved; otherwise the bots whose PRs merge without their author watching
        "authors": [] if authors == "all" or authors == ["all"] else list(authors),
        "stale_days": int(cfg.get("stale_days", 3)),
        "window_days": int(cfg.get("window_days", 7)),
    }


def enabled(cfg: dict | None = None) -> bool:
    cfg = config.load().get(NAME, {}) if cfg is None else cfg
    return bool(cfg.get("enabled") and config.GITHUB_TOKEN and config.github()["login"])


def refresh(cfg: dict):
    opts = settings(cfg)
    try:
        try:
            data = fetch(**opts)
        except requests.ConnectionError:
            data = fetch(**opts)
    except Exception as e:
        log.warning("followup fetch failed: %s", e)
        db.record_run(NAME, str(e) or e.__class__.__name__)
        return
    db.set_watcher_state(NAME, "data", json.dumps(data))
    db.record_run(NAME)
    engine.process(NAME, items(data))
    _investigate(data)


def fetch(login: str, orgs: list[str], authors: list[str], stale_days: int, window_days: int) -> dict:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    scope = (f"is:pr archived:false reviewed-by:{login} -author:{login} "
             + " ".join(f"org:{o}" for o in orgs)
             + "".join(f" author:app/{a}" for a in authors))
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": QUERY, "variables": {
            "open": f"{scope} is:open",
            "closed": f"{scope} is:closed closed:>={since}",
        }},
        headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
    prs = []
    for bucket in ("open", "closed"):
        for node in payload["data"][bucket]["nodes"]:
            pr = _normalize(node, login, stale_days) if node else None
            if pr:
                prs.append(pr)
    prs.sort(key=lambda p: (STAGES.index(p["stage"]), p["merged_at"] or p["approved_at"]))
    return {"prs": prs, "window_days": window_days, "stale_days": stale_days}


def _normalize(pr: dict, login: str, stale_days: int) -> dict | None:
    mine = [r for r in pr["latestReviews"]["nodes"]
            if (r.get("author") or {}).get("login") == login and r["state"] in MINE]
    if not mine:
        return None
    review = mine[-1]
    rollup = _rollup(pr)
    out = {
        "id": pr["id"],
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": (pr.get("author") or {}).get("login", ""),
        "branch": pr["headRefName"],
        "draft": pr["isDraft"],
        "updated_at": pr["updatedAt"],
        "merged": pr["merged"],
        "merged_at": pr.get("mergedAt"),
        "closed_at": pr.get("closedAt"),
        "approved_at": review["submittedAt"],
        "dismissed": review["state"] == "DISMISSED",
        "review": pr.get("reviewDecision"),
        "mergeable": pr.get("mergeable"),
        "automerge": bool(pr.get("autoMergeRequest")) or any("automerge" in l["name"].lower()
                                                              for l in pr["labels"]["nodes"]),
        **rollup,
        "waiting_days": _days_since(review["submittedAt"]),
    }
    out["blocker"] = _blocker(out) if not (out["merged"] or out["closed_at"]) else ""
    if out["merged"]:
        out.update(_landing(pr))
    out["stage"] = stage(out)
    return out


def _rollup(pr: dict) -> dict:
    commits = pr["commits"]["nodes"]
    rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
    failed = []
    for c in rollup.get("contexts", {}).get("nodes", []):
        if c["__typename"] == "CheckRun" and c.get("conclusion") in FAILED_CONCLUSIONS:
            failed.append(c["name"])
        elif c["__typename"] == "StatusContext" and c.get("state") in ("FAILURE", "ERROR"):
            failed.append(c["context"])
    return {"ci": {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
                   "PENDING": "pending", "EXPECTED": "pending"}.get(rollup.get("state")),
            "ci_failed": failed[:5]}


def _landing(pr: dict) -> dict:
    """What ran on the merge commit after the merge, and whether somebody reverted the PR."""
    commit = pr.get("mergeCommit") or {}
    contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes", [])
    checks = []
    for c in contexts:
        if c["__typename"] == "CheckRun":
            run = (c.get("checkSuite") or {}).get("workflowRun") or {}
            if run.get("event") in PRE_MERGE_EVENTS:
                continue
            if c.get("status") != "COMPLETED":
                state = "pending"
            elif c.get("conclusion") in FAILED_CONCLUSIONS:
                state = "failure"
            else:
                state = "success"  # SUCCESS, NEUTRAL, SKIPPED
            checks.append({"name": c["name"], "state": state, "url": c.get("detailsUrl") or "",
                           "workflow": (run.get("workflow") or {}).get("name", ""), "run_id": run.get("databaseId"),
                           "completed_at": c.get("completedAt")})
        else:
            state = {"SUCCESS": "success", "PENDING": "pending", "EXPECTED": "pending"}.get(c.get("state"), "failure")
            checks.append({"name": c["context"], "state": state, "url": c.get("targetUrl") or "",
                           "workflow": "", "run_id": None, "completed_at": c.get("createdAt")})
    # the same check on the commit just before the merge: already red there = not this PR's doing
    parents = (commit.get("parents") or {}).get("nodes") or [{}]
    before = {}
    for c in (((parents[0] or {}).get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes", []):
        if c["__typename"] == "CheckRun":
            before[c["name"]] = ("pending" if c.get("status") != "COMPLETED"
                                 else "failure" if c.get("conclusion") in FAILED_CONCLUSIONS else "success")
        else:
            before[c["context"]] = {"SUCCESS": "success", "PENDING": "pending", "EXPECTED": "pending"}.get(c.get("state"), "failure")
    for c in checks:
        c["before"] = before.get(c["name"], "absent")
    revert = None
    for node in (pr.get("timelineItems") or {}).get("nodes", []):
        src = (node or {}).get("source") or {}
        if src.get("title", "").lower().startswith("revert") and src.get("url") != pr["url"]:
            revert = {"number": src["number"], "url": src["url"], "title": src["title"]}
    done = [c["completed_at"] for c in checks if c["completed_at"]]
    return {"merge_sha": commit.get("oid", ""), "post_checks": checks, "revert": revert,
            "landed_at": max(done) if done and all(c["state"] != "pending" for c in checks) else None}


def stage(pr: dict) -> str:
    if not pr["merged"]:
        return "closed" if pr["closed_at"] else "waiting"
    if pr.get("revert"):
        return "reverted"
    checks = pr.get("post_checks") or []
    failed = [c for c in checks if c["state"] == "failure"]
    if failed:
        return "inherited" if all(c.get("before") == "failure" for c in failed) else "failed"
    if any(c["state"] == "pending" for c in checks):
        return "checking"
    if checks:
        return "landed"
    return "checking" if _minutes_since(pr["merged_at"]) < CHECKS_START_MINUTES else "no checks"


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


def items(data: dict) -> list[WatchedItem]:
    """The follow-ups that need you; everything else lives on the Follow-up page."""
    out = []
    stale_days = data["stale_days"]
    for p in data["prs"]:
        ref = f"{p['repo']}#{p['number']}"
        base = {"repo": p["repo"], "number": p["number"], "title": p["title"], "subject": ref}
        short = f"{p['repo'].split('/')[-1]}#{p['number']}"
        if p["stage"] == "waiting" and p["dismissed"]:
            out.append(_item(f"{ref}:dismissed", f"{short} — approval dismissed", p, "dismissed",
                             {**base, "kind": "dismissed",
                              "why": "a new commit dismissed your approval — it will not merge on its own"}))
        elif p["stage"] == "waiting" and p["waiting_days"] >= stale_days:
            out.append(_item(f"{ref}:stuck", f"{short} — approved {p['waiting_days']} d ago, still open", p,
                             # the bucket re-arms the push every stale_days once acked
                             f"stuck:{p['waiting_days'] // stale_days}",
                             {**base, "kind": "stuck", "why": f"approved {p['waiting_days']} d ago · {p['blocker']}"}))
        elif p["stage"] == "failed":
            failed = [c for c in p["post_checks"] if c["state"] == "failure"]
            out.append(_item(f"{ref}:failed", f"{short} — broke after merge?", p,
                             "failed:" + ",".join(sorted(c["name"] for c in failed)),
                             {**base, "kind": "failed", "checks": failed,
                              "why": "a check that ran after the merge failed"}))
        elif p["stage"] == "reverted":
            out.append(_item(f"{ref}:reverted", f"{short} — reverted", p, "reverted",
                             {**base, "kind": "reverted", "revert": p["revert"],
                              "why": f"reverted by #{p['revert']['number']}"}))
    return out


def _item(external_id: str, title: str, pr: dict, fingerprint: str, payload: dict) -> WatchedItem:
    return WatchedItem(external_id=external_id, title=f"{title} — {pr['title']}", url=pr["url"],
                       fingerprint=fingerprint, payload=payload, lane="followup", urgency="now")


def _investigate(data: dict):
    """A fresh post-merge failure gets a Claude investigation, once per merge commit. Older ones
    (first run of Sentinel, a failure from last week) keep the button: no surprise push about the past."""
    for p in data["prs"]:
        if p["stage"] != "failed" or _minutes_since(p["merged_at"]) > AUTO_INVESTIGATE_HOURS * 60:
            continue
        subject = f"{p['repo']}#{p['number']}"
        current = db.get_run(subject, "followup")
        if current and json.loads(current["context"] or "{}").get("merge_sha") == p["merge_sha"]:
            continue
        agents.request(subject, "followup", p["url"], context(p), auto=True)


def context(pr: dict) -> dict:
    """What the bridge's followup prompt gets besides the PR URL."""
    failed = [c for c in pr["post_checks"] if c["state"] == "failure"]
    before = {"failure": "already failing", "success": "passing", "pending": "running", "absent": "did not run"}
    return {"merge_sha": pr["merge_sha"], "merged_at": pr["merged_at"],
            "failed_checks": [f"{c['name']} — {c['url']} (on the commit before the merge: {before[c['before']]})"
                              for c in failed[:6]]}


def _days_since(value: str | None) -> int:
    return int(_minutes_since(value) // 1440)


def _minutes_since(value: str | None) -> float:
    if not value:
        return 0
    then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def load() -> dict:
    state = db.watcher_states().get(NAME, {})
    data = json.loads(state.get("data") or "{}")
    return {
        "prs": data.get("prs", []),
        "window_days": data.get("window_days", 7),
        "stale_days": data.get("stale_days", 3),
        "last_run": state.get("last_run"),
        "last_error": state.get("last_error"),
    }


def summary(prs: list[dict]) -> dict:
    count = {s: sum(1 for p in prs if p["stage"] == s) for s in STAGES}
    count["blocked"] = sum(1 for p in prs if p["stage"] == "waiting"
                           and RANK.index(p["blocker"]) <= RANK.index("review required"))
    return count
