"""The Grafana dashboards you keep open, read back by Sentinel (Graphite panels).

Each curve's last 30 min is compared with the hours before it (`quart.verdicts`). A counter that
stops, or a whole-number gauge that changes, is pushed once seen on two reads in a row; anything
else that changed is listed without pushing. A dashboard that cannot be read keeps what was known.
"""
import logging
import os
import time

import requests

import quart
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.grafana")


class GrafanaWatcher(Watcher):
    name = "grafana"

    def __init__(self, cfg: dict):
        self.interval_seconds = cfg.get("interval_seconds", 300)
        self.window_minutes = int(cfg.get("window_hours", 6) * 60)
        self.points = cfg.get("points", 120)
        self.dashboards = cfg.get("dashboards") or []
        self.token = os.environ.get(cfg.get("token_env", "GRAFANA_TOKEN"), "")
        self.cert = (cfg["client_cert"], cfg["client_key"]) if cfg.get("client_cert") else None
        self.verify = cfg.get("ca_cert") or True
        self._seen: set[str] = set()
        self._kept: dict[str, list[WatchedItem]] = {}

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self.token}"
        s.cert = self.cert
        s.verify = self.verify
        return s

    def fetch(self) -> list[WatchedItem]:
        s = self._session()
        cards, items, seen, failed = [], [], set(), 0
        for link in self.dashboards:
            base, uid, variables = quart.parse_dashboard_url(link)
            try:
                card = self._read(s, base, uid, variables, link)
            except (requests.RequestException, KeyError, ValueError) as e:
                failed += 1
                log.warning("grafana %s: %s", uid, e)
                cards.append({"uid": uid, "title": uid, "link": link, "error": str(e)[:200], "panels": 0, "series": 0, "anomalies": []})
                items.extend(self._kept.get(uid, []))
                continue
            kept = []
            for a in card["anomalies"]:
                key = f"{uid}:{a['panel_id']}:{a['series']}:{a['kind']}"
                seen.add(key)
                a["confirmed"] = key in self._seen
                if a["confirmed"]:
                    kept.append(self._item(key, card, a))
            self._kept[uid] = kept
            items.extend(kept)
            cards.append(card)
        if self.dashboards and failed == len(self.dashboards):
            raise RuntimeError("no dashboard could be read")
        self._seen = seen
        quart.publish("grafana", {"read_at": time.time(), "window_minutes": self.window_minutes, "dashboards": cards})
        return items

    def _read(self, s: requests.Session, base: str, uid: str, variables: dict, link: str) -> dict:
        r = s.get(f"{base}/api/dashboards/uid/{uid}", timeout=30)
        r.raise_for_status()
        dashboard = r.json()["dashboard"]
        card = {"uid": uid, "title": dashboard.get("title", uid), "link": link, "error": None, "panels": 0, "series": 0, "anomalies": []}
        for panel in quart.panels(dashboard):
            graphite = quart.graphite_targets(panel, variables)
            if not graphite:
                continue
            ds, targets = graphite
            params = [("target", t) for t in targets] + [
                ("from", f"-{self.window_minutes}min"), ("until", "now"), ("format", "json"), ("maxDataPoints", self.points)]
            r = s.get(f"{base}/api/datasources/proxy/uid/{ds}/render", params=params, timeout=60)
            r.raise_for_status()
            series = r.json()
            card["panels"] += 1
            card["series"] += len(series)
            volume = bool(quart.VOLUME.search(" ".join(targets)))
            for a in quart.verdicts(series, self.window_minutes):
                spark = quart.sparkline(a.pop("values"))
                card["anomalies"].append({**a, "panel": panel.get("title", ""), "panel_id": panel.get("id"), "volume": volume, "spark": spark})
        return card

    def _item(self, key: str, card: dict, a: dict) -> WatchedItem:
        urgent = (a["kind"] == "stop" and a["volume"]) or (a["kind"] == "state" and a["discrete"])
        return WatchedItem(
            external_id=key,
            title=f"{card['title']} · {a['panel']} · {a['series']}: {a['text']}"[:300],
            url=card["link"],
            fingerprint=a["kind"],
            severity=a["kind"],
            payload={"source": "grafana", "name": f"{a['series']}: {a['text']}", "dashboard": card["title"], "panel": a["panel"]},
            lane="incident",
            urgency="now" if urgent else "quiet",
        )
