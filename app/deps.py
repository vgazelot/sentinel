"""Dependency-update PRs (Renovate, Dependabot): what is bumped, from what to what, and how big.

The same bump usually lands as one PR per repository; grouping them by `name@to` turns
N reviews into one decision.
"""
import json
import re

import agents
import db
import evidence

_PREFIX = r"^(?:[\w-]+(?:\([^)]*\))?!?:\s*)?"
TITLE_PATTERNS = [
    # Renovate: "Update dependency go to v1.27.2", "Update karpenter-crd Docker tag to v1.15.0",
    # "Update Helm release alloy to v1.2", "Update Terraform hashicorp/terraform to v1.16.4", "Update github to ~> 6.13"
    re.compile(_PREFIX + r"update (?P<kind>dependency |module |helm release |terraform )?(?P<name>.+?)"
               r"(?P<suffix> docker tag| action| digest| orb)? to (?P<to>\S.*?)\s*$", re.I),
    # Dependabot: "Bump lodash from 4.17.15 to 4.17.21", "build(deps): bump actions/checkout from 3 to 4 in /x"
    re.compile(_PREFIX + r"bump (?P<name>\S+) from (?P<from>\S+) to (?P<to>\S+)(?: in \S+)?\s*$", re.I),
]
DEP_LABELS = {"renovate", "dependencies", "dependabot"}
# what is bumped matters for grouping: the alloy image and the alloy chart are two decisions
KINDS = {"helm release": "Helm chart", "docker tag": "image", "action": "action", "digest": "digest", "orb": "orb",
         "module": "module", "terraform": "Terraform"}
_NOTE = re.compile(r"\s*(\[security\]|\((?:major|minor|patch)\))\s*$", re.I)
# biggest first: a PR bumping several packages is as risky as its biggest bump
TYPE_RANK = ["major", "minor", "patch", "digest", "pin", "pindigest", "lockfilemaintenance"]
_TICKS = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def parse_title(title: str) -> dict | None:
    for pattern in TITLE_PATTERNS:
        m = pattern.match(title.strip())
        if m:
            groups = m.groupdict()
            kind = KINDS.get(((groups.get("kind") or "") + (groups.get("suffix") or "")).strip().lower(), "")
            to, security = m.group("to").strip(), False
            while _NOTE.search(to):  # "v9 [SECURITY]", "v9 (major)": the same version as "v9"
                security = security or "security" in _NOTE.search(to).group(1).lower()
                to = _NOTE.sub("", to)
            return {"name": m.group("name").strip(), "kind": kind, "to": to,
                    "from": (groups.get("from") or "").strip(), "security": security}
    return None


def is_dependency_pr(title: str, labels: list[str]) -> bool:
    return bool(parse_title(title)) or bool(DEP_LABELS & {l.lower() for l in labels})


def parse_body(body: str) -> dict:
    """Renovate's update table → {type, from, to, packages}; Dependabot's "Bumps x from a to b" as a fallback."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if not (line.startswith("|") and "package" in line.lower() and "change" in line.lower()):
            continue
        headers = [h.strip().lower() for h in line.strip().strip("|").split("|")]
        rows = []
        for row in lines[i + 2:]:
            if not row.startswith("|"):
                break
            rows.append(dict(zip(headers, (c.strip() for c in row.strip().strip("|").split("|")))))
        if not rows:
            break
        types = [r.get("update", "").replace(" ", "").lower() for r in rows if r.get("update")]
        change = _TICKS.findall(rows[0].get("change", ""))
        return {
            "type": _biggest(types) or semver_type(*(change[:2] if len(change) >= 2 else ("", ""))),
            "from": change[0] if change else "",
            "to": change[1] if len(change) > 1 else "",
            "packages": sorted({_LINK.sub(r"\1", r.get("package", "")).split(" (")[0] for r in rows}),
            "rows": len(rows),
        }
    m = re.search(r"Bumps \[?([^\]\s]+)\]?(?:\([^)]*\))? from ([\w.\-+]*\w) to ([\w.\-+]*\w)", body or "")
    if m:
        return {"type": semver_type(m.group(2), m.group(3)), "from": m.group(2), "to": m.group(3),
                "packages": [m.group(1)], "rows": 1}
    return {}


def _biggest(types: list[str]) -> str:
    known = [t for t in types if t in TYPE_RANK]
    return min(known, key=TYPE_RANK.index) if known else (types[0] if types else "")


def semver_type(old: str, new: str) -> str:
    """major/minor/patch from two version strings ("v1.2.3", "~> 1.14", ">= 1.5.0"); "" when unclear."""
    a, b = _numbers(old), _numbers(new)
    if not a or not b:
        return ""
    for kind, x, y in zip(("major", "minor", "patch"), a, b):
        if x != y:
            return kind
    return "patch" if len(b) > len(a) else ""


def _numbers(v: str) -> list[int]:
    m = re.search(r"(\d+(?:\.\d+)*)", v or "")
    return [int(x) for x in m.group(1).split(".")][:3] if m else []


def group_key(dep: dict) -> str:
    kind = f" ({dep['kind']})" if dep.get("kind") else ""
    return f"{dep['name'].lower()}{kind.lower()}@{dep['to']}"


def approvable(pr: dict) -> bool:
    """What "Approve the green ones" approves — the page and the endpoint ask this one function."""
    return pr.get("ci") == "success" and not pr.get("draft") and pr.get("mergeable") != "CONFLICTING"


def group(prs: list[dict]) -> list[dict]:
    """PR dicts carrying a `dep` → [{key, name, to, type, prs}], biggest bump and largest group first."""
    groups: dict[str, dict] = {}
    for pr in prs:
        dep = pr.get("dep") or {}
        key = group_key(dep) if dep.get("name") else f"pr:{pr['repo']}#{pr['number']}"
        g = groups.setdefault(key, {"key": key, "name": dep.get("name") or pr["title"], "kind": dep.get("kind", ""),
                                    "to": dep.get("to", ""), "type": dep.get("type", ""), "security": False,
                                    "prs": [], "approvable": []})
        if dep.get("type") and (not g["type"] or _rank(dep["type"]) < _rank(g["type"])):
            g["type"] = dep["type"]
        g["security"] = g["security"] or bool(dep.get("security"))
        g["prs"].append(pr)
        if approvable(pr):
            g["approvable"].append(pr)
    return sorted(groups.values(), key=lambda g: (_rank(g["type"]), -len(g["prs"]), g["name"].lower()))


def _rank(kind: str) -> int:
    return TYPE_RANK.index(kind) if kind in TYPE_RANK else len(TYPE_RANK)


# --- the dependency lane of the inbox -----------------------------------------------------
def pending_groups() -> list[dict]:
    """Bumps waiting for a review, grouped — the Dependencies section and the prechecks."""
    rows = db.items("watcher = 'github_reviews' AND lane = 'deps' AND state IN ('pending', 'snoozed')")
    prs = [{**json.loads(r["payload"] or "{}"), "item_id": r["id"], "url": r["url"], "state": r["state"],
            "first_seen": r["first_seen"]} for r in rows]
    return group([p for p in prs if p.get("repo")])


def precheck():
    """Scheduler job: one Claude bump check per pending bump, with the PRs of every repository at once."""
    for g in pending_groups():
        subject = f"deps:{g['key']}"
        if agents.settled(db.get_run(subject, "precheck"), urls=urls(g)):
            continue  # once per bump and set of PRs; the button re-runs it
        agents.request(subject, "precheck", g["prs"][0]["url"], lambda g=g: context(g), auto=True)


def urls(g: dict) -> list[str]:
    return sorted(p["url"] for p in g["prs"])


def context(g: dict) -> dict:
    """The precheck's material, fetched here (see evidence.py): Claude gets no tool in this mode."""
    parts = []
    for pr in sorted(g["prs"], key=lambda p: p["url"])[:6]:
        parts.append(evidence.safely(evidence.pr_brief, pr["repo"], pr["number"], body_chars=5000, diff_lines=150))
        for name, url in evidence.safely_list(evidence.failing_checks, pr["repo"], pr.get("head_sha", ""))[:2]:
            parts.append(f"Failing check on {pr['repo']}#{pr['number']}: {name} {url}\n"
                         + evidence.safely(evidence.job_log, pr["repo"], url, tail=60))
    kind = f" ({g['kind']})" if g.get("kind") else ""
    return {"urls": urls(g),
            "bump": f"{g['name']}{kind} → {g['to'] or '?'} ({g['type'] or 'size unknown'})",
            "material": evidence.material(*parts)}
