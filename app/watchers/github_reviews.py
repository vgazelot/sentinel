"""PRs waiting for your review, sorted into lanes — one GraphQL search for all orgs.

  you   your review was requested by name                  → pushed right away
  team  requested through a team you belong to             → pushed only if nobody took it within
                                                             the grace period; gone once approved
  deps  a bot's dependency bump (Renovate, Dependabot)     → never pushed, grouped by bump

Drafts never push. A team or deps PR somebody else already approved no longer needs you,
so it leaves the inbox (resolved) — a direct request stays: they asked for you.
"""
import logging

import requests

import checks
import config
import deps
import github
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.github_reviews")
QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes { ... on PullRequest {
      id number title url isDraft createdAt updatedAt reviewDecision mergeable
      additions deletions changedFiles
      repository { nameWithOwner }
      author { __typename login }
      labels(first: 12) { nodes { name } }
      autoMergeRequest { enabledAt }
      reviewRequests(first: 20) { nodes { requestedReviewer {
        __typename ... on User { login } ... on Team { slug } } } }
      latestReviews(first: 10) { nodes { state author { login } } }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
    } }
  }
}
"""


class GithubReviewsWatcher(Watcher):
    name = "github_reviews"

    def __init__(self, cfg: dict):
        self.interval_seconds = cfg.get("interval_seconds", 180)
        self.login = cfg["login"]
        self.orgs = cfg.get("orgs", [])
        # Renovate's update table (major/minor/patch, from → to) lives in the PR body, which is
        # far too heavy to pull every poll: read once per PR title (a new version = a new title)
        self._bodies: dict[tuple[str, str], dict] = {}

    def fetch(self) -> list[WatchedItem]:
        q = f"is:pr is:open archived:false review-requested:{self.login} " + " ".join(f"org:{o}" for o in self.orgs)
        data = github.graphql(QUERY, {"q": q})
        items = []
        for pr in data["search"]["nodes"]:
            if pr:
                item = self._item(pr)
                if item:
                    items.append(item)
        live = {(i.external_id, i.payload["title"]) for i in items}
        self._bodies = {k: v for k, v in self._bodies.items() if k in live}
        return items

    def _item(self, pr: dict) -> WatchedItem | None:
        repo = pr["repository"]["nameWithOwner"]
        author = pr.get("author") or {}
        labels = [l["name"] for l in pr["labels"]["nodes"]]
        requested = [n["requestedReviewer"] for n in pr["reviewRequests"]["nodes"] if n.get("requestedReviewer")]
        direct = any(r.get("__typename") == "User" and (r.get("login") or "").lower() == self.login.lower()
                     for r in requested)
        teams = [r["slug"] for r in requested if r.get("__typename") == "Team"]
        is_bot = author.get("__typename") == "Bot"
        is_dep = is_bot and deps.is_dependency_pr(pr["title"], labels)
        decision = pr.get("reviewDecision")
        if not direct and decision == "APPROVED":
            return None  # somebody else already covered the review this request was about

        commit = (pr["commits"]["nodes"] or [{}])[0].get("commit") or {}
        payload = {
            "repo": repo,
            "number": pr["number"],
            "title": pr["title"],
            "author": author.get("login", ""),
            "author_bot": is_bot,
            "draft": pr["isDraft"],
            "direct": direct,
            "teams": teams,
            "review_decision": decision,
            "approvals": [r["author"]["login"] for r in pr["latestReviews"]["nodes"]
                          if r["state"] == "APPROVED" and r.get("author")],
            "ci": checks.rollup((commit.get("statusCheckRollup") or {}).get("state")),
            "mergeable": pr.get("mergeable"),
            "head_sha": commit.get("oid", ""),
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "files": pr["changedFiles"],
            "labels": labels,
            "automerge": bool(pr.get("autoMergeRequest")) or any("automerge" in l.lower() for l in labels),
            "created_at": pr["createdAt"],
            "updated_at": pr["updatedAt"],
        }
        if is_dep:
            lane, urgency = "deps", "now" if direct else "quiet"
            payload["dep"] = self._dep(pr, repo)
            fingerprint = payload["dep"].get("to") or pr["title"]
            payload["why"] = "dependency bump" + (" · asked you directly" if direct else "")
        elif direct:
            lane, urgency, fingerprint = "you", "now", pr["updatedAt"]
            payload["why"] = f"asked you directly · by {payload['author']}"
        else:
            # a team request that moves (new commits, comments) is still not yours: never re-notify
            lane, urgency, fingerprint = "team", "later", "team"
            payload["why"] = f"team {', '.join(teams) or '?'} · by {payload['author']}"
        if pr["isDraft"]:
            urgency = "quiet"
        return WatchedItem(
            external_id=pr["id"],
            title=f"{repo.split('/')[-1]}#{pr['number']} — {pr['title']}",
            url=pr["url"],
            fingerprint=fingerprint,
            severity="draft" if pr["isDraft"] else None,
            payload=payload,
            lane=lane,
            urgency=urgency,
        )

    def _dep(self, pr: dict, repo: str) -> dict:
        dep = deps.parse_title(pr["title"]) or {"name": pr["title"], "to": "", "from": ""}
        key = (pr["id"], pr["title"])
        if key not in self._bodies:
            try:
                self._bodies[key] = deps.parse_body(_body(repo, pr["number"]))
            except Exception as e:  # noqa: BLE001 — the bump is still listed, just without its size
                log.info("could not read the body of %s#%s: %s", repo, pr["number"], e)
                self._bodies[key] = {}
        body = self._bodies[key]
        return {**dep, "type": body.get("type", ""), "from": body.get("from") or dep.get("from", ""),
                "change_to": body.get("to", ""), "packages": body.get("packages", []), "key": deps.group_key(dep)}


def _body(repo: str, number: int) -> str:
    r = requests.get(f"https://api.github.com/repos/{repo}/pulls/{number}", timeout=15, headers={
        "Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json().get("body") or ""
