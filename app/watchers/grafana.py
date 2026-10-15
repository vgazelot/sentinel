"""A Grafana dashboard you keep open, read back by Sentinel (Graphite panels). One source per
dashboard (`grafana:<uid>`): one that cannot be read keeps its items and lights the health light,
the others go on being read.

Each curve's last 30 min is compared with the hours before it (`quart.verdicts`); every change is
listed on the Watch page. A counter that stops, or a whole-number gauge that changes, is also an
inbox item (lane `watch`, pushed) once seen on two reads in a row. The first read of a new source
counts as seen: what is there already is the engine's baseline, recorded without pushing.
"""
import hashlib
import logging
import os
from urllib.parse import urlparse

import requests

import db
import quart
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.grafana")


class GrafanaWatcher(Watcher):
    def __init__(self, cfg: dict, link: str, order: int):
        self.link = link
        self.order = order
        try:
            self.base, self.uid, self.variables = quart.parse_dashboard_url(link)
        except ValueError:
            self.base, self.uid, self.variables = "", "", {}
        # the same dashboard may be watched twice, with other variables: each link is its own source
        tag = hashlib.sha1(repr(sorted(self.variables.items())).encode()).hexdigest()[:6] if self.variables else ""
        self.name = f"grafana:{self.uid or order}" + (f"-{tag}" if tag else "")
        self.interval_seconds = cfg.get("interval_seconds", 300)
        self.window_minutes = int(cfg.get("window_hours", 6) * 60)
        self.points = cfg.get("points", 120)
        self.url = (cfg.get("url") or "").rstrip("/")
        self.token = os.environ.get(cfg.get("token_env", "GRAFANA_TOKEN"), "")
        self.cert = (cfg["client_cert"], cfg["client_key"]) if cfg.get("client_cert") else None
        self.verify = cfg.get("ca_cert") or True
        self.title = self.uid or link
        self._seen: set[str] | None = None  # None: no read since the start
        self._kept: dict = {}  # panel id → confirmed items

    def _check(self):
        """The token only ever goes to the Grafana named in `url`, over https."""
        if urlparse(self.url).scheme != "https":
            raise ValueError("grafana.url must be the https address of your Grafana")
        if not self.uid:
            raise ValueError("not a dashboard link (…/d/<uid>/…)")
        if self.base != self.url:
            raise ValueError(f"link outside {self.url}")

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self.token}"
        s.cert = self.cert
        s.verify = self.verify
        return s

    def fetch(self) -> list[WatchedItem]:
        try:
            self._check()
            card = self._read(self._session())
        except Exception as e:
            quart.publish_dashboard(self.order, {"uid": self.uid, "title": self.title, "link": self.link, "error": str(e)[:200],
                                                 "panels": 0, "series": 0, "unread": 0, "anomalies": [],
                                                 "window_minutes": self.window_minutes})
            raise
        # a new source: what is there is the baseline; after a restart, a change is read twice like any other
        baseline = self._seen is None and not db.watcher_states().get(self.name, {}).get("seeded")
        seen = set()
        kept = {pid: self._kept.get(pid, []) for pid in card.pop("unread_ids")}
        for a in card["anomalies"]:
            key = f"{self.uid}:{a['panel_id']}:{a['series']}:{a['kind']}"
            seen.add(key)
            a["confirmed"] = baseline or key in (self._seen or ())
            a["item"] = a["confirmed"] and quart.pushes(a)
            if a["item"]:
                kept.setdefault(a["panel_id"], []).append(self._item(key, card, a))
        self._seen, self._kept = seen, kept
        quart.publish_dashboard(self.order, card)
        return [i for items in kept.values() for i in items]

    def _read(self, s: requests.Session) -> dict:
        r = s.get(f"{self.base}/api/dashboards/uid/{self.uid}", timeout=30)
        r.raise_for_status()
        dashboard = r.json()["dashboard"]
        self.title = dashboard.get("title", self.uid)
        variables = {**quart.defaults(dashboard), **self.variables}
        card = {"uid": self.uid, "title": self.title, "link": self.link, "error": None, "panels": 0, "series": 0,
                "unread": 0, "unread_ids": [], "anomalies": [], "window_minutes": self.window_minutes}
        error = None
        for panel in quart.panels(dashboard):
            graphite = quart.graphite_targets(panel, variables)
            if not graphite:
                continue
            ds, targets = graphite
            card["panels"] += 1
            params = [("target", t) for t in targets] + [
                ("from", f"-{self.window_minutes}min"), ("until", "now"), ("format", "json"), ("maxDataPoints", self.points)]
            try:
                r = s.get(f"{self.base}/api/datasources/proxy/uid/{ds}/render", params=params, timeout=60)
                r.raise_for_status()
                series = r.json()
            except (requests.RequestException, ValueError) as e:
                # one panel that cannot be read keeps what was known of it, the others are still read
                error = e
                card["unread"] += 1
                card["unread_ids"].append(panel.get("id"))
                continue
            card["series"] += len(series)
            volume = bool(quart.VOLUME.search(" ".join(targets)))
            for a in quart.verdicts(series, self.window_minutes, volume):
                spark = quart.sparkline(a.pop("values"))
                card["anomalies"].append({**a, "panel": panel.get("title", ""), "panel_id": panel.get("id"), "spark": spark})
        if error is not None and card["unread"] == card["panels"]:
            raise error
        return card

    def _item(self, key: str, card: dict, a: dict) -> WatchedItem:
        name = f"{a['series']} — {a['text']}"
        return WatchedItem(
            external_id=key,
            title=name[:300],
            url=card["link"],
            fingerprint=a["kind"],
            severity=a["kind"],
            payload={"source": "grafana", "name": name, "dashboard": card["title"], "panel": a["panel"],
                     "why": f"{card['title']} · {a['panel']}"},
            lane="watch",
            urgency="now",
        )
