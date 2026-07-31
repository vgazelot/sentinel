"""Watcher contract + registry built from the config."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class WatchedItem:
    external_id: str
    title: str
    url: str
    fingerprint: str
    severity: str | None = None
    payload: dict = field(default_factory=dict)


class Watcher(ABC):
    name: str
    interval_seconds: int

    @abstractmethod
    def fetch(self) -> list[WatchedItem]:
        """Returns the full current state. Diff/state/notif is handled by engine.py."""


def build_watchers(cfg: dict) -> dict[str, Watcher]:
    from watchers.github_reviews import GithubReviewsWatcher
    from watchers.statuspage import StatuspageWatcher

    watchers: dict[str, Watcher] = {}
    wcfg = cfg.get("watchers", {})

    gh = wcfg.get("github_reviews", {})
    if gh.get("enabled"):
        w = GithubReviewsWatcher(gh)
        watchers[w.name] = w

    sp = wcfg.get("statuspage", {})
    if sp.get("enabled"):
        for instance in sp.get("instances", []):
            w = StatuspageWatcher(instance, sp.get("interval_seconds", 300))
            watchers[w.name] = w

    return watchers
