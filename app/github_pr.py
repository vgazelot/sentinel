"""GitHub PR details (diff with line numbers, checks, review threads) + reviews, on demand from the dashboard."""
import re

import requests

import config
import md

API = "https://api.github.com"
MAX_FILES = 50
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
    pr = _get(f"/repos/{repo}/pulls/{number}")
    files = _get(f"/repos/{repo}/pulls/{number}/files", per_page=MAX_FILES + 1)
    checks = _checks_summary(repo, pr["head"]["sha"])
    threads = _threads(repo, number)

    shown = []
    for f in files[:MAX_FILES]:
        lines = _patch_lines(f.get("patch"))
        file_threads = threads.pop(f["filename"], {})
        if lines:
            for ln in lines:
                key = (ln["side"], ln["line"])
                if ln["line"] is not None and key in file_threads:
                    ln["threads"] = file_threads.pop(key)
        shown.append({
            "filename": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "lines": lines,
            # outdated (line null) or beyond the shown diff
            "other_threads": [t for ts in file_threads.values() for t in ts],
        })
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


def _reviews_summary(repo: str, number: int) -> list[dict]:
    """Latest non-comment review per reviewer: who approved, who asked for changes."""
    latest: dict[str, dict] = {}
    for rv in _get(f"/repos/{repo}/pulls/{number}/reviews", per_page=100):
        if rv["state"] in ("APPROVED", "CHANGES_REQUESTED"):
            latest[rv["user"]["login"]] = {"user": rv["user"]["login"], "state": rv["state"].lower()}
    return list(latest.values())


def _threads(repo: str, number: int) -> dict:
    """Review comments grouped into threads, keyed path → (side, line) → [thread]."""
    comments = _get(f"/repos/{repo}/pulls/{number}/comments", per_page=100)
    roots: dict[int, dict] = {}
    for c in comments:
        entry = {
            "id": c["id"],
            "user": c["user"]["login"],
            "created_at": c["created_at"],
            "html": md.render(c.get("body") or ""),
            "url": c.get("html_url"),
        }
        parent = c.get("in_reply_to_id")
        if parent and parent in roots:
            roots[parent]["comments"].append(entry)
            continue
        roots[c["id"]] = {
            "id": c["id"],
            "path": c["path"],
            "side": c.get("side") or "RIGHT",
            "line": c.get("line"),
            "start_line": c.get("start_line"),
            "outdated": c.get("line") is None,
            "comments": [entry],
        }
    out: dict = {}
    for t in roots.values():
        out.setdefault(t["path"], {}).setdefault((t["side"], t["line"]), []).append(t)
    return out


def _patch_lines(patch: str | None) -> list[dict] | None:
    """Diff lines with old/new numbers; `side`+`line` = what the review API expects for a comment."""
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


def submit_review(repo: str, number: int, event: str, body: str = "", comments: list[dict] | None = None,
                  commit_id: str | None = None):
    """event: APPROVE | COMMENT | REQUEST_CHANGES. comments: [{path, line, side, start_line?, start_side?, body}]."""
    payload: dict = {"event": event}
    if body:
        payload["body"] = body
    if comments:
        payload["comments"] = [
            {k: c[k] for k in ("path", "line", "side", "start_line", "start_side", "body") if c.get(k) is not None}
            for c in comments
        ]
        if commit_id:
            payload["commit_id"] = commit_id
    _post(f"/repos/{repo}/pulls/{number}/reviews", payload)


def updated_at(repo: str, number: int) -> str:
    """PR updated_at, read back after a review to ack on a state that includes it."""
    return _get(f"/repos/{repo}/pulls/{number}")["updated_at"]


def reply(repo: str, number: int, comment_id: int, body: str):
    _post(f"/repos/{repo}/pulls/{number}/comments/{comment_id}/replies", {"body": body})
