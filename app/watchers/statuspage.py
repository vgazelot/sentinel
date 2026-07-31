"""Generic Atlassian Statuspage watcher (Scaleway, OVHcloud universes, ...)."""
import re

import requests

from watchers import WatchedItem, Watcher


class StatuspageWatcher(Watcher):
    def __init__(self, instance: dict, interval_seconds: int):
        self.instance = instance["name"]
        self.name = f"statuspage:{self.instance}"
        self.interval_seconds = interval_seconds
        self.url = instance["url"]
        self.components = [c.lower() for c in instance.get("components", []) or []]
        self.keywords = [k.lower() for k in instance.get("keywords", []) or []]
        self.include_maintenances = instance.get("include_maintenances", False)

    def fetch(self) -> list[WatchedItem]:
        r = requests.get(self.url, timeout=10)
        r.raise_for_status()
        data = r.json()

        incidents = list(data.get("incidents", []))
        if self.include_maintenances:
            incidents += data.get("scheduled_maintenances", [])

        items = []
        for inc in incidents:
            if inc.get("status") in ("resolved", "completed"):
                continue  # absent from the fetch → the engine will mark it resolved
            comp_names = [c.get("name", "") for c in inc.get("components", [])]
            if not self._matches(inc, comp_names):
                continue
            updates = inc.get("incident_updates", [])
            fingerprint = str(updates[0]["id"]) if updates else str(inc.get("updated_at"))
            body = (updates[0].get("body", "") if updates else "")[:300]
            items.append(
                WatchedItem(
                    external_id=inc["id"],
                    title=f"[{self.instance}] {inc.get('name', 'incident')}",
                    url=inc.get("shortlink") or self.url,
                    fingerprint=fingerprint,
                    severity=inc.get("impact") or "maintenance",
                    payload={
                        "status": inc.get("status"),
                        "body": body,
                        "components": comp_names,
                    },
                )
            )
        return items

    def _matches(self, inc: dict, comp_names: list[str]) -> bool:
        """components = main filter; keywords = additional restriction (AND).
        Keywords match at word start (`gra` → GRA1, not `degraded`)."""
        comps = " ".join(comp_names).lower()
        if self.components and not any(c in comps for c in self.components):
            return False
        if self.keywords:
            text = f"{inc.get('name', '')} {comps}".lower()
            return any(re.search(r"\b" + re.escape(k), text) for k in self.keywords)
        return True
