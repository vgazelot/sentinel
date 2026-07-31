"""GitHub PRs waiting for your review, one search request per org."""
import requests

import config
from watchers import WatchedItem, Watcher

API = "https://api.github.com/search/issues"


class GithubReviewsWatcher(Watcher):
    name = "github_reviews"

    def __init__(self, cfg: dict):
        self.interval_seconds = cfg.get("interval_seconds", 180)
        self.login = cfg["login"]
        self.orgs = cfg.get("orgs", [])

    def fetch(self) -> list[WatchedItem]:
        if not config.GITHUB_TOKEN:
            raise RuntimeError("GITHUB_TOKEN missing from .env")
        headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        items = []
        for org in self.orgs:
            q = f"is:pr is:open review-requested:{self.login} org:{org}"
            r = requests.get(API, params={"q": q, "per_page": 50, "sort": "updated"}, headers=headers, timeout=15)
            r.raise_for_status()
            for pr in r.json().get("items", []):
                repo = pr["repository_url"].split("/repos/")[-1]
                items.append(
                    WatchedItem(
                        external_id=pr["node_id"],
                        title=f"{repo}#{pr['number']} — {pr['title']}",
                        url=pr["html_url"],
                        fingerprint=pr["updated_at"],
                        severity="draft" if pr.get("draft") else None,
                        payload={
                            "repo": repo,
                            "number": pr["number"],
                            "author": pr.get("user", {}).get("login"),
                            "draft": pr.get("draft", False),
                        },
                    )
                )
        return items
