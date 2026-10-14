"""Watch: what changed in the last hours on the dashboards you keep open and in Zabbix, without opening them.

Each curve of a dashboard is read back over a window (6 h by default) and its last 30 minutes are
compared with the hours before, in the same archive and at the same resolution:
  stop    active on 90 % of the points before, zero or empty for 30 min
  state   constant before, a different value on the last two points (any step for a whole-number
          gauge, at least 20 % otherwise)
  drift   the last 30 min outside the usual band (p5-p95) and the median ×3 or ÷3
Graphite's latest point is still filling up and is left out. Comparing with the day before was
tried and dropped: older points come from another archive, and the traffic of yesterday is not today's.
"""
import re
import statistics
import threading
from urllib.parse import parse_qs, urlparse

VAR = re.compile(r"\$\{(\w+)\}|\[\[(\w+)\]\]|\$(\w+)")
# statsd timer aggregates nobody watches: their swings are noise
IGNORE = re.compile(r"\.(lower|std|sum|sum_90|sum_squares|sum_squares_90|mean_90|count_ps)$")
# a panel that counts things: a stop there is an outage, not a quiet hour
VOLUME = re.compile(r"(^|[(,\s])stats_counts\.|\.count\b")
RECENT_MINUTES = 30

_lock = threading.Lock()
_snapshot: dict = {"grafana": None, "zabbix": None}


def publish(source: str, data: dict):
    with _lock:
        _snapshot[source] = data


def snapshot() -> dict:
    with _lock:
        return dict(_snapshot)


def parse_dashboard_url(url: str) -> tuple[str, str, dict[str, list[str]]]:
    """A dashboard link as you open it (…/d/<uid>/<slug>?var-x=a&var-x=b) → (base, uid, variables)."""
    u = urlparse(url)
    parts = u.path.split("/")
    uid = parts[parts.index("d") + 1]
    variables = {k[4:]: v for k, v in parse_qs(u.query).items() if k.startswith("var-")}
    return f"{u.scheme}://{u.netloc}", uid, variables


def substitute(target: str, variables: dict[str, list[str]]) -> str:
    """Grafana template variables in a Graphite target: All → *, several values → {a,b}."""
    def repl(m):
        values = variables.get(m.group(1) or m.group(2) or m.group(3))
        if not values:
            return m.group(0)
        values = ["*" if v == "$__all" else v for v in values]
        return values[0] if len(values) == 1 else "{" + ",".join(values) + "}"

    return VAR.sub(repl, target)


def panels(dashboard: dict) -> list[dict]:
    out = []
    for p in dashboard.get("panels", []):
        out.extend(p.get("panels", []) if p.get("type") == "row" else [p])
    return out


def graphite_targets(panel: dict, variables: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    """(datasource uid, targets) of a Graphite panel, or None for any other panel."""
    ds = panel.get("datasource")
    if not isinstance(ds, dict) or ds.get("type") != "graphite":
        return None
    targets = [substitute(t["target"], variables) for t in panel.get("targets", []) if t.get("target") and not t.get("hide")]
    return (ds["uid"], targets) if targets else None


def verdicts(series: list[dict], window_minutes: int) -> list[dict]:
    """Graphite render output → one dict per curve that changed: kind, series, text, values, discrete."""
    out = []
    for s in series:
        name = s["target"]
        if IGNORE.search(name):
            continue
        values = [v for v, _ in s["datapoints"]][:-1]
        n_recent = max(3, len(values) * RECENT_MINUTES // window_minutes)
        recent, before = values[-n_recent:], values[:-n_recent]
        known = [v for v in before if v is not None]
        if len(before) < 10:
            continue
        found = _stop(known, before, recent) or _state(known, before, recent) or _drift(known, before, recent, n_recent)
        if found:
            out.append({"series": name, "values": values, **found})
    return out


def _stop(known, before, recent):
    if sum(1 for v in known if v != 0) >= 0.9 * len(before) and all(v in (None, 0) for v in recent):
        return {"kind": "stop", "text": f"stopped for {RECENT_MINUTES} min (≈ {_num(statistics.median(known))} before)", "discrete": False}
    return None


def _state(known, before, recent):
    tail = [v for v in recent if v is not None][-2:]
    if len(known) < 0.8 * len(before) or len(set(known)) != 1 or len(tail) != 2:
        return None
    v0 = known[0]
    discrete = float(v0).is_integer() and all(float(v).is_integer() for v in tail)
    moved = all(v != v0 for v in tail) if discrete else all(abs(v - v0) >= 0.2 * max(abs(v0), 1e-9) for v in tail)
    if moved:
        return {"kind": "state", "text": f"went from {_num(v0)} to {_num(tail[-1])}", "discrete": discrete}
    return None


def _drift(known, before, recent, n_recent):
    now = [v for v in recent if v is not None]
    if len(now) < 0.7 * n_recent or len(known) < 0.7 * len(before) or len(set(known)) < 2:
        return None
    q = statistics.quantiles(known, n=20)
    was, is_ = statistics.median(known), statistics.median(now)
    outside = sum(1 for v in now if v > q[-1] or v < q[0]) / len(now)
    if outside >= 0.8 and was > 0 and is_ > 0 and (is_ / was >= 3 or was / is_ >= 3):
        return {"kind": "drift", "text": f"median {_num(is_)} vs {_num(was)} before (×{is_ / was:.2g})", "discrete": False}
    return None


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.3g}"


def sparkline(values: list, width: int = 220, height: int = 36) -> dict:
    """SVG path of a curve (gaps kept) and the band its earlier part stayed in, in a width × height box."""
    known = [v for v in values if v is not None]
    if not known:
        return {"path": "", "band": None, "width": width, "height": height}
    lo, hi = min(known), max(known)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)

    def y(v):
        return round(height - 2 - (v - lo) / span * (height - 4), 1)

    path, pen_up = [], True
    for i, v in enumerate(values):
        if v is None:
            pen_up = True
            continue
        path.append(f"{'M' if pen_up else 'L'}{round(i / n * width, 1)},{y(v)}")
        pen_up = False
    n_recent = max(3, len(values) * RECENT_MINUTES // 360)
    earlier = [v for v in values[:-n_recent] if v is not None]
    band = None
    if len(earlier) >= 2:
        q = statistics.quantiles(earlier, n=20)
        band = {"y": y(q[-1]), "h": max(round(y(q[0]) - y(q[-1]), 1), 1)}
    return {"path": " ".join(path), "band": band, "width": width, "height": height}
