"""GitHub PR details + approve, on demand from the dashboard."""
import requests

import config

API = "https://api.github.com"
MAX_FILES = 20
MAX_LINES_PER_FILE = 120


def _headers() -> dict:
    if not config.GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing from .env")
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, **params) -> dict | list:
    r = requests.get(f"{API}{path}", params=params, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def details(repo: str, number: int) -> dict:
    pr = _get(f"/repos/{repo}/pulls/{number}")
    files = _get(f"/repos/{repo}/pulls/{number}/files", per_page=MAX_FILES + 1)
    checks = _checks_summary(repo, pr["head"]["sha"])

    shown = [
        {
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "lines": _patch_lines(f.get("patch")),
        }
        for f in files[:MAX_FILES]
    ]
    return {
        "repo": repo,
        "number": number,
        "title": pr["title"],
        "body": (pr.get("body") or "").strip()[:6000],
        "author": pr["user"]["login"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": pr["changed_files"],
        "mergeable_state": pr.get("mergeable_state"),
        "draft": pr.get("draft", False),
        "checks": checks,
        "files": shown,
        "files_truncated": len(files) > MAX_FILES,
    }


def _checks_summary(repo: str, sha: str) -> dict:
    """Merges check-runs (Actions) and combined status (Jenkins & friends)."""
    ok = failed = pending = 0
    runs = _get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
    for run in runs:
        if run["status"] != "completed":
            pending += 1
        elif run["conclusion"] in ("success", "neutral", "skipped"):
            ok += 1
        else:
            failed += 1
    status = _get(f"/repos/{repo}/commits/{sha}/status")
    for st in status.get("statuses", []):
        if st["state"] == "pending":
            pending += 1
        elif st["state"] == "success":
            ok += 1
        else:
            failed += 1
    return {"ok": ok, "failed": failed, "pending": pending}


def _patch_lines(patch: str | None) -> list[dict] | None:
    if not patch:
        return None
    lines = []
    for line in patch.splitlines()[:MAX_LINES_PER_FILE]:
        if line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        lines.append({"cls": cls, "text": line})
    if len(patch.splitlines()) > MAX_LINES_PER_FILE:
        lines.append({"cls": "hunk", "text": f"… truncated ({len(patch.splitlines()) - MAX_LINES_PER_FILE} more lines)"})
    return lines


def submit_review(repo: str, number: int, event: str, body: str = ""):
    """event: APPROVE | COMMENT | REQUEST_CHANGES (COMMENT and REQUEST_CHANGES require a body)."""
    payload = {"event": event}
    if body:
        payload["body"] = body
    r = requests.post(
        f"{API}/repos/{repo}/pulls/{number}/reviews",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"GitHub {r.status_code}: {msg}")
