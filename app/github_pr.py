"""GitHub PR details (diff with line numbers, checks, reviews) + submitting a review or a comment, on demand from the dashboard."""
import re
import time

import requests

import config
import md

API = "https://api.github.com"
MAX_FILES = 50
CACHE_SECONDS = 60  # going back to a PR in the list is instant; a review sent drops its entry
_cache: dict[tuple[str, int], tuple[float, dict]] = {}
MAX_LINES_PER_FILE = 600
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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


def _post(path: str, payload: dict):
    r = requests.post(f"{API}{path}", headers=_headers(), json=payload, timeout=15)
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("message", r.text)
            for e in body.get("errors", []):
                msg += f" — {e.get('message') or e}"
        except ValueError:
            msg = r.text
        raise RuntimeError(f"GitHub {r.status_code}: {msg}")
    return r.json()


def details(repo: str, number: int) -> dict:
    hit = _cache.get((repo, number))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = _details(repo, number)
    now = time.monotonic()
    for key in [k for k, (at, _) in _cache.items() if now - at >= CACHE_SECONDS]:
        _cache.pop(key, None)
    _cache[(repo, number)] = (now, result)
    return result


def _details(repo: str, number: int) -> dict:
    pr = _get(f"/repos/{repo}/pulls/{number}")
    files = _get(f"/repos/{repo}/pulls/{number}/files", per_page=MAX_FILES + 1)
    checks = _checks_summary(repo, pr["head"]["sha"])

    shown = [{"filename": f["filename"], "status": f["status"], "additions": f["additions"],
              "deletions": f["deletions"], "lines": _patch_lines(f.get("patch"))} for f in files[:MAX_FILES]]
    return {
        "repo": repo,
        "number": number,
        "title": pr["title"],
        "body": (pr.get("body") or "").strip()[:6000],
        "author": pr["user"]["login"],
        "head_sha": pr["head"]["sha"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": pr["changed_files"],
        "mergeable_state": pr.get("mergeable_state"),
        "draft": pr.get("draft", False),
        "checks": checks,
        "reviews": _reviews_summary(repo, number),
        "files": shown,
        "files_truncated": len(files) > MAX_FILES,
    }


def _checks_summary(repo: str, sha: str) -> dict:
    """Merges check-runs (Actions) and combined status (Jenkins & friends); the failing ones by name."""
    ok = pending = 0
    failing = []
    runs = _get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
    for run in runs:
        if run["status"] != "completed":
            pending += 1
        elif run["conclusion"] in ("success", "neutral", "skipped"):
            ok += 1
        else:
            failing.append({"name": run["name"], "url": run.get("html_url") or ""})
    status = _get(f"/repos/{repo}/commits/{sha}/status")
    for st in status.get("statuses", []):
        if st["state"] == "pending":
            pending += 1
        elif st["state"] == "success":
            ok += 1
        else:
            failing.append({"name": st["context"], "url": st.get("target_url") or ""})
    return {"ok": ok, "failed": len(failing), "pending": pending, "failing": failing}


def _reviews_summary(repo: str, number: int) -> list[dict]:
    """Latest non-comment review per reviewer: who approved, who asked for changes and what they wrote
    (untrusted markdown: through md.render like everything shown from GitHub)."""
    latest: dict[str, dict] = {}
    for rv in _get(f"/repos/{repo}/pulls/{number}/reviews", per_page=100):
        if rv["state"] in ("APPROVED", "CHANGES_REQUESTED"):
            body = (rv.get("body") or "").strip()
            latest[rv["user"]["login"]] = {"user": rv["user"]["login"], "state": rv["state"].lower(),
                                           "html": md.link_file_refs(md.render(body[:4000])) if body else ""}
    return list(latest.values())


def _patch_lines(patch: str | None) -> list[dict] | None:
    """Diff lines with old/new numbers; `side`+`line` locate a line (Claude's `file:line` links jump to it)."""
    if not patch:
        return None
    lines, old, new = [], 0, 0
    raw = patch.splitlines()
    for text in raw[:MAX_LINES_PER_FILE]:
        m = HUNK.match(text)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            lines.append({"cls": "hunk", "text": text, "old": None, "new": None, "side": None, "line": None})
            continue
        if text.startswith("\\"):
            lines.append({"cls": "ctx", "text": text, "old": None, "new": None, "side": None, "line": None})
            continue
        if text.startswith("+"):
            lines.append({"cls": "add", "text": text, "old": None, "new": new, "side": "RIGHT", "line": new})
            new += 1
        elif text.startswith("-"):
            lines.append({"cls": "del", "text": text, "old": old, "new": None, "side": "LEFT", "line": old})
            old += 1
        else:
            lines.append({"cls": "ctx", "text": text, "old": old, "new": new, "side": "RIGHT", "line": new})
            old += 1
            new += 1
    if len(raw) > MAX_LINES_PER_FILE:
        lines.append({"cls": "hunk", "text": f"… truncated ({len(raw) - MAX_LINES_PER_FILE} more lines)",
                      "old": None, "new": None, "side": None, "line": None})
    return lines


def submit_review(repo: str, number: int, event: str, body: str = "", commit_id: str | None = None):
    """event: APPROVE | COMMENT | REQUEST_CHANGES."""
    payload: dict = {"event": event}
    if body:
        payload["body"] = body
    if commit_id:
        # the review lands on the commit that was looked at (and whose CI was green), not on
        # whatever a bot pushed in between
        payload["commit_id"] = commit_id
    _post(f"/repos/{repo}/pulls/{number}/reviews", payload)
    _cache.pop((repo, number), None)


def comment(repo: str, number: int, body: str, reply_to: int | None = None) -> dict:
    """In the review thread of comment `reply_to` (GitHub answers a thread on its first comment), else on the PR's
    conversation. GitHub's comment back (html_url)."""
    if reply_to is None:
        out = _post(f"/repos/{repo}/issues/{number}/comments", {"body": body})
    else:
        first = _get(f"/repos/{repo}/pulls/comments/{reply_to}")
        if str(first.get("pull_request_url", "")).lower() != f"{API}/repos/{repo}/pulls/{number}".lower():
            raise RuntimeError("that thread belongs to another pull request")
        out = _post(f"/repos/{repo}/pulls/{number}/comments/{first.get('in_reply_to_id') or reply_to}/replies", {"body": body})
    _cache.pop((repo, number), None)
    return out


def head_sha(repo: str, number: int) -> str:
    """The PR head right now: approving checks it is still the commit whose CI was green."""
    return _get(f"/repos/{repo}/pulls/{number}")["head"]["sha"]


def updated_at(repo: str, number: int) -> str:
    """PR updated_at, read back after a review to ack on a state that includes it."""
    return _get(f"/repos/{repo}/pulls/{number}")["updated_at"]

