"""Claude reads ahead, so the inbox says why before you open GitHub: how carefully a review needs
reading (`brief`), why your PRs are red (`whyred`), what the reviewers ask on your PR (`asks`).

Same rules as the other automatic modes (agents.py): no tool, the material fetched here, the daily
cap, and one run per state of what it reads — a new commit, a new set of red PRs, a new comment —
through `agents.settled()`. The detail pane offers the same runs on demand.
"""
import json

import agents
import checks
import db
import evidence

MODES = ("brief", "whyred", "asks")


def refresh():
    """Scheduler job: queue what changed since the last look."""
    if not agents.enabled():
        return
    for row in db.items("state = 'pending' AND watcher = 'github_reviews' AND lane IN ('you', 'team')"):
        p = json.loads(row["payload"] or "{}")
        subject = f"{p.get('repo')}#{p.get('number')}"
        if p.get("repo") and not p.get("draft") and not agents.settled(db.get_run(subject, "brief"), head=p.get("head_sha", "")):
            agents.request(subject, "brief", row["url"], lambda p=p: brief_context(p), auto=True)
    mine = [json.loads(r["payload"] or "{}") | {"url": r["url"]}
            for r in db.items("state = 'pending' AND watcher = 'my_prs'")]
    for family, prs in red_families(mine).items():
        subject = f"why:{family}"
        if not agents.settled(db.get_run(subject, "whyred"), heads=heads(prs)) and has_logs(family, prs[0]):
            agents.request(subject, "whyred", prs[0]["url"], lambda f=family, prs=prs: whyred_context(f, prs), auto=True)
    for p in mine:
        if p.get("action") not in ("changes", "threads"):
            continue
        subject = f"asks:{p['repo']}#{p['number']}"
        if not agents.settled(db.get_run(subject, "asks"), asks=asks_key(p)):
            agents.request(subject, "asks", p["url"], lambda p=p: asks_context(p), auto=True)


def red_families(prs: list[dict]) -> dict[str, list[dict]]:
    """Your red PRs by first failing CI family — the grouping of the inbox rows."""
    out: dict[str, list[dict]] = {}
    for p in prs:
        if p.get("action") == "ci" and p.get("families"):
            out.setdefault(p["families"][0], []).append(p)
    return out


def has_logs(family: str, p: dict) -> bool:
    """A red commit status (Jenkins & co) has no log Sentinel can read: an automatic run would only say
    "unclear". The detail pane still offers it."""
    return any(checks.family(name) == family
               for name, _ in evidence.safely_list(evidence.failing_checks, p["repo"], p.get("head_sha", "")))


def heads(prs: list[dict]) -> list[str]:
    return sorted(f"{p['repo']}#{p['number']}@{p.get('head_sha', '')}" for p in prs)


def asks_key(p: dict) -> str:
    return f"{','.join(sorted(p.get('changes_by') or []))}:{p.get('unresolved', 0)}"


def brief_context(p: dict) -> dict:
    return {"head": p.get("head_sha", ""), "material": evidence.material(evidence.safely(
        evidence.pr_brief, p["repo"], p["number"], body_chars=4000, diff_lines=500))}


def whyred_context(family: str, prs: list[dict]) -> dict:
    parts = []
    for p in sorted(prs, key=lambda p: p["url"])[:3]:
        parts.append(f"PR {p['repo']}#{p['number']}: {p.get('title', '')}")
        failing = [(n, u) for n, u in evidence.safely_list(evidence.failing_checks, p["repo"], p.get("head_sha", ""))
                   if checks.family(n) == family]
        for name, url in failing[:2]:
            parts.append(f"Failed check {name} {url}\n" + evidence.safely(evidence.job_log, p["repo"], url, tail=80))
        if not failing:
            parts.append(f"(no failed GitHub Actions job named {family} on the head commit: a commit status, or a re-run)")
    return {"urls": sorted(p["url"] for p in prs), "heads": heads(prs), "family": family,
            "material": evidence.material(*parts)}


def asks_context(p: dict) -> dict:
    return {"asks": asks_key(p), "material": evidence.material(
        evidence.review_asks(p["repo"], p["number"]),
        evidence.safely(evidence.pr_brief, p["repo"], p["number"], body_chars=1500, diff_lines=250))}


def target(subject: str, mode: str) -> tuple[str, dict] | None:
    """URL and context of an on-demand run, rebuilt from Sentinel's own data — the page only names it."""
    if mode == "brief":
        for row in db.items("watcher = 'github_reviews' AND state != 'resolved'"):
            p = json.loads(row["payload"] or "{}")
            if f"{p.get('repo')}#{p.get('number')}" == subject:
                return row["url"], brief_context(p)
        return None
    mine = [json.loads(r["payload"] or "{}") | {"url": r["url"]}
            for r in db.items("state != 'resolved' AND watcher = 'my_prs'")]
    if mode == "whyred" and subject.startswith("why:"):
        prs = red_families(mine).get(subject[4:])
        return (prs[0]["url"], whyred_context(subject[4:], prs)) if prs else None
    if mode == "asks" and subject.startswith("asks:"):
        p = next((p for p in mine if f"asks:{p['repo']}#{p['number']}" == subject), None)
        return (p["url"], asks_context(p)) if p else None
    return None
