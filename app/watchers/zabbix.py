"""Zabbix problems still open.

Dependent problems are hidden the way the Zabbix front hides them (`skipDependent`), and so are the
ones on disabled hosts or items; acknowledged ones are only counted: somebody holds them. Every open
problem is listed on the Watch page; one at `push_severity` or above is also an inbox item (lane
`watch`, pushed). One whose name matches `quiet` is known noise: listed apart, never an item.
"""
import logging
import os
import re
import time

import requests

import quart
from watchers import WatchedItem, Watcher

log = logging.getLogger("sentinel.zabbix")
SEVERITIES = ["not classified", "information", "warning", "average", "high", "disaster"]


class ZabbixWatcher(Watcher):
    name = "zabbix"

    def __init__(self, cfg: dict):
        self.interval_seconds = cfg.get("interval_seconds", 120)
        self.url = cfg["url"]
        self.web_url = cfg.get("web_url", "").rstrip("/")
        self.token = os.environ.get(cfg.get("token_env", "ZABBIX_TOKEN"), "")
        self.cert = (cfg["client_cert"], cfg["client_key"]) if cfg.get("client_cert") else None
        self.verify = cfg.get("ca_cert") or True
        self.min_severity = cfg.get("min_severity", 2)
        self.push_severity = cfg.get("push_severity", 4)
        self.quiet = [re.compile(q, re.IGNORECASE) for q in cfg.get("quiet") or []]

    def _call(self, method: str, params: dict):
        # Zabbix 5.4 reads an API token from `auth`; the Authorization header came with 6.4
        body = {"jsonrpc": "2.0", "method": method, "params": params, "auth": self.token, "id": 1}
        r = requests.post(self.url, json=body, cert=self.cert, verify=self.verify, timeout=20)
        r.raise_for_status()
        answer = r.json()
        if "error" in answer:
            raise RuntimeError(f"{method}: {answer['error'].get('data') or answer['error'].get('message')}")
        return answer["result"]

    def fetch(self) -> list[WatchedItem]:
        problems = self._call("problem.get", {
            "output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged"],
            "recent": False,
            "suppressed": False,
            "severities": list(range(self.min_severity, 6)),
        })
        ids = sorted({p["objectid"] for p in problems})
        monitored = {t["triggerid"] for t in self._call("trigger.get", {"triggerids": ids, "monitored": True, "output": ["triggerid"]})} if ids else set()
        triggers = self._call("trigger.get", {
            "triggerids": sorted(monitored),
            "skipDependent": True,
            "monitored": True,
            "output": ["triggerid"],
            "selectHosts": ["host"],
        }) if monitored else []
        hosts = {t["triggerid"]: ", ".join(h["host"] for h in t.get("hosts", [])) for t in triggers}

        rows, items = [], []
        counts = {"acked": 0, "dependent": 0, "disabled": 0}
        for p in problems:
            if p["objectid"] not in monitored:
                counts["disabled"] += 1
                continue
            if p["objectid"] not in hosts:
                counts["dependent"] += 1
                continue
            if p["acknowledged"] == "1":
                counts["acked"] += 1
                continue
            level = int(p["severity"])
            known = any(q.search(p["name"]) for q in self.quiet)
            url = f"{self.web_url}/tr_events.php?triggerid={p['objectid']}&eventid={p['eventid']}" if self.web_url else ""
            row = {"eventid": p["eventid"], "host": hosts[p["objectid"]], "name": p["name"], "severity": SEVERITIES[level], "level": level,
                   "since": int(p["clock"]), "known": known, "url": url, "item": not known and level >= self.push_severity}
            rows.append(row)
            if not row["item"]:
                continue
            items.append(WatchedItem(
                external_id=p["eventid"],
                title=f"{row['host']} · {p['name']}",
                url=url,
                fingerprint=str(level),
                severity=row["severity"],
                payload={"source": "zabbix", "name": p["name"], "host": row["host"], "since": row["since"],
                         "why": f"{row['severity']} · {row['host']}"},
                lane="watch",
                urgency="now",
            ))
        rows.sort(key=lambda r: (r["known"], -r["level"], -r["since"]))
        quart.publish("zabbix", {"read_at": time.time(), "problems": rows, "web_url": self.web_url, **counts})
        return items
