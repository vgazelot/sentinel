"""The Watch page: Zabbix and the dashboards you keep open, read in one place."""
import time

from flask import Blueprint, render_template

import config
import quart

bp = Blueprint("watch", __name__)


def duration(seconds: float) -> str:
    m = int(max(seconds, 0) // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h < 48 else f"{h // 24}d {h % 24}h"


def _read(now: float, at: float) -> str:
    return "read just now" if now - at < 60 else f"read {duration(now - at)} ago"


def _enabled() -> bool:
    w = config.load().get("watchers", {})
    return bool(w.get("zabbix", {}).get("enabled") or w.get("grafana", {}).get("enabled"))


def context() -> dict:
    snap, now = quart.snapshot(), time.time()
    zabbix = grafana = None
    if z := snap["zabbix"]:
        rows = [{**r, "age": duration(now - r["since"])} for r in z["problems"]]
        newest = max((r["since"] for r in z["problems"]), default=None)
        hidden = [f"{n} {label}" for n, label in ((z["acked"], "acknowledged"), (z["dependent"], "hidden behind the problem they depend on"),
                                                   (z["disabled"], "on disabled hosts or items")) if n]
        zabbix = {
            "look": [r for r in rows if not r["known"]],
            "known": [r for r in rows if r["known"]],
            "hidden": " · ".join(hidden),
            "quiet_for": duration(now - newest) if newest else None,
            "read": _read(now, z["read_at"]),
            "web_url": z["web_url"],
        }
    if g := snap["grafana"]:
        cards = [g[k] for k in sorted(g)]
        grafana = {
            "cards": cards,
            "series": sum(c["series"] for c in cards),
            "hours": cards[0]["window_minutes"] // 60,
            # the oldest read: every dashboard was read at least this recently
            "read": _read(now, min(c["read_at"] for c in cards)),
        }
    # the Watch badge counts what only this page shows: what pushes is already in the inbox
    entries = (zabbix["look"] if zabbix else []) + [a for c in (grafana["cards"] if grafana else []) for a in c["anomalies"]]
    to_look = sum(1 for e in entries if not e.get("item"))
    return {"zabbix": zabbix, "grafana": grafana, "to_look": to_look, "in_inbox": len(entries) - to_look}


@bp.app_context_processor
def nav() -> dict:
    if not _enabled():
        return {"watch_enabled": False}
    return {"watch_enabled": True, "nav_watch": context()["to_look"]}


@bp.get("/watch")
def watch_page():
    return render_template("watch.html", active="watch", **context())


@bp.get("/partials/watch")
def partial_watch():
    return render_template("partials/watch.html", **context())
