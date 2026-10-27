"""A Grafana dashboard you keep open, read back by Sentinel (Graphite panels). One source per
dashboard (`grafana:<uid>`): one that cannot be read keeps its items and lights the health light,
the others go on being read.

Each curve's last 30 min is compared with the hours before it (`quart.verdicts`); every change is
listed on the Watch page. A counter that stops, or a whole-number gauge that changes, is also an
inbox item (lane `watch`, pushed) once seen on two reads in a row. The first read of a new source
counts as seen: what is there already is the engine's baseline, recorded without pushing.

The alerts Grafana sends for these dashboards are another source (`grafana-alerts`), read every minute.
"""
import hashlib
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

import requests

import db
import quart
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.grafana")


class _Grafana(Watcher):
    def __init__(self, cfg: dict):
        self.url = (cfg.get("url") or "").rstrip("/")
        self.token = os.environ.get(cfg.get("token_env", "GRAFANA_TOKEN"), "")
        self.cert = (cfg["client_cert"], cfg["client_key"]) if cfg.get("client_cert") else None
        self.verify = cfg.get("ca_cert") or True

    def _https(self):
        """The token only ever goes to the Grafana named in `url`, over https."""
        if urlparse(self.url).scheme != "https":
            raise ValueError("grafana.url must be the https address of your Grafana")

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self.token}"
        s.cert = self.cert
        s.verify = self.verify
        return s


class GrafanaWatcher(_Grafana):
    def __init__(self, cfg: dict, link: str, order: int):
        super().__init__(cfg)
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
        self.title = self.uid or link
        self._seen: set[str] | None = None  # None: no read since the start
        self._kept: dict = {}  # panel id → confirmed items

    def _check(self):
        self._https()
        if not self.uid:
            raise ValueError("not a dashboard link (…/d/<uid>/…)")
        if self.base != self.url:
            raise ValueError(f"link outside {self.url}")

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


# a rule whose query returns nothing, or fails, fires under these names, its own in `rulename`
NO_RESULT = {"DatasourceNoData": "no data", "DatasourceError": "query failed"}
# labels every alert carries: the others tell apart the series of one rule firing on several
SHARED = {"alertname", "rulename", "grafana_folder"}


class GrafanaAlertsWatcher(_Grafana):
    """The alerts Grafana sends for the dashboards you watch, the ones that page whoever is on call: an inbox item each
    (lane `watch`, pushed) until Grafana resolves it. Read from its alertmanager, so a silenced or inhibited alert,
    which pages nobody, is left out. One source for every dashboard, read every minute: the curves take minutes to
    read, an alert does not wait for them."""
    name = "grafana-alerts"
    interval_seconds = 60

    def __init__(self, cfg: dict, links: list[str]):
        super().__init__(cfg)
        self.links: dict[str, str] = {}  # uid → the first link to it: the variables you watch it with
        for link in links:
            try:
                base, uid, _ = quart.parse_dashboard_url(link)
            except ValueError:
                continue
            if base == self.url:
                self.links.setdefault(uid, link)
        self._dashboards: dict[str, tuple[str, dict[str, str]]] = {}  # uid → title, {panel id: title}

    def fetch(self) -> list[WatchedItem]:
        self._https()
        s = self._session()
        r = s.get(f"{self.url}/api/alertmanager/grafana/api/v2/alerts", timeout=30)
        r.raise_for_status()
        rows, items = {}, []
        for a in r.json():
            notes, labels = a.get("annotations") or {}, a.get("labels") or {}
            uid = notes.get("__dashboardUid__")
            if uid not in self.links or (a.get("status") or {}).get("state") != "active":
                continue
            title, panels = self._dashboard(s, uid, notes.get("__panelId__"))
            panel = panels.get(notes.get("__panelId__"), "")
            alertname = labels.get("alertname", "")
            name = f"{labels.get('rulename', alertname)} — {NO_RESULT[alertname]}" if alertname in NO_RESULT else alertname
            series = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in SHARED and not k.startswith("__"))
            name += f" · {series}" if series else ""
            note = " — ".join(v for v in (notes.get("summary"), notes.get("description"), notes.get("message")) if v)
            since = datetime.fromisoformat(a["startsAt"].replace("Z", "+00:00")).timestamp()
            rows.setdefault(uid, []).append({"name": name, "panel": panel, "since": since, "url": self.links[uid], "item": True})
            items.append(WatchedItem(
                external_id=a["fingerprint"],
                title=name,
                url=self.links[uid],
                fingerprint="firing",
                severity=labels.get("severity") or "firing",
                payload={"source": "grafana", "name": name, "dashboard": title, "panel": panel, "since": since,
                         "why": f"{title} · {panel}" if panel else title, "note": note[:500], "rule": a.get("generatorURL", "")},
                lane="watch",
                urgency="now",
            ))
        quart.publish("alerts", rows)
        return items

    def _dashboard(self, s: requests.Session, uid: str, panel_id: str | None) -> tuple[str, dict[str, str]]:
        """Its title and panels' titles, read again when an alert names a panel it does not know. Unreadable: the
        alert is still an item, named without them."""
        known = self._dashboards.get(uid)
        if known and panel_id in known[1]:
            return known
        try:
            r = s.get(f"{self.url}/api/dashboards/uid/{uid}", timeout=30)
            r.raise_for_status()
            d = r.json()["dashboard"]
        except (requests.RequestException, ValueError, KeyError) as e:
            log.info("grafana alerts: dashboard %s unread: %s", uid, e)
            return known or (uid, {})
        self._dashboards[uid] = (d.get("title", uid), {str(p.get("id")): p.get("title", "") for p in quart.panels(d)})
        return self._dashboards[uid]
