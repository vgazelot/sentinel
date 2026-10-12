"""What Claude reads in the automatic modes, fetched here: the PR and its diff, the failed job logs,
the recent history of the failing workflow on the default branch.

The automatic modes (bump precheck, post-merge investigation) get this material and no tool at all:
a release note or a CI log written to steer the model can bend its verdict, never make it read a
file or reach a host. The same fetches tell a post-merge failure that was already there before the
merge (previous run of the same workflow on the default branch) from one this merge brought.
"""
import logging
import re

import requests

import config

log = logging.getLogger("sentinel.evidence")
API = "https://api.github.com"
MAX_MATERIAL = 60_000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
_JOB_URL = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
_ERROR = re.compile(r"\berror\b|\bfailed\b|exit code [1-9]", re.I)
# (repo, job id, branch) → outcome of the same job in the previous run: the answer never changes
_previous: dict[tuple[str, str, str], str] = {}


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _get(path: str, **params):
    r = requests.get(f"{API}{path}", params=params, headers=_headers(), timeout=30)
    if r.status_code >= 500:  # listing a long workflow history 502s now and then: once more
        r = requests.get(f"{API}{path}", params=params, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def job_ids(url: str) -> tuple[str, str] | None:
    """(run id, job id) from a check run's details URL, None outside GitHub Actions."""
    m = _JOB_URL.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def pr_brief(repo: str, number: int, body_chars: int = 6000, diff_lines: int = 300) -> str:
    pr = _get(f"/repos/{repo}/pulls/{number}")
    files = _get(f"/repos/{repo}/pulls/{number}/files", per_page=100)
    out = [f"PR {repo}#{number}: {pr['title']}", f"by {pr['user']['login']} into {pr['base']['ref']}", "",
           "Description:", (pr.get("body") or "(empty)")[:body_chars], "", "Diff:"]
    budget = diff_lines
    for f in files:
        patch = (f.get("patch") or "(binary or too large)").splitlines()[:max(budget, 0)]
        out += [f"--- {f['filename']} (+{f['additions']} -{f['deletions']})", *patch]
        budget -= len(patch)
        if budget <= 0:
            out.append(f"… diff cut here: {pr['changed_files'] - files.index(f) - 1} more file(s) not shown")
            break
    if pr["changed_files"] > len(files):
        out.append(f"… {pr['changed_files'] - len(files)} more file(s) not shown")
    return "\n".join(out)


def job_log(repo: str, url: str, tail: int = 120) -> str:
    ids = job_ids(url)
    if not ids:
        return "(no log: not a GitHub Actions job)"
    r = requests.get(f"{API}/repos/{repo}/actions/jobs/{ids[1]}/logs", headers=_headers(), timeout=30)
    if r.status_code in (404, 410):
        return "(log expired or not found)"
    r.raise_for_status()
    rows = [_STAMP.sub("", _ANSI.sub("", line)) for line in r.text.splitlines()]
    errors = [line for line in rows if _ERROR.search(line)][:40]
    return "\n".join(["Lines mentioning an error:", *errors, "", f"Last {tail} lines:", *rows[-tail:]])[:9000]


def workflow_history(repo: str, url: str, branch: str, limit: int = 8) -> str:
    ids = job_ids(url)
    if not ids:
        return ""
    run = _get(f"/repos/{repo}/actions/runs/{ids[0]}")
    runs = _get(f"/repos/{repo}/actions/workflows/{run['workflow_id']}/runs", branch=branch, per_page=limit)
    out = [f"Recent runs of the workflow '{run['name']}' on {branch}, newest first:"]
    for r in runs.get("workflow_runs", []):
        mark = "   <- the failed run" if r["id"] == run["id"] else ""
        out.append(f"- {r['created_at'][:16]} {r['conclusion'] or r['status']} {r['head_sha'][:7]} "
                   f"{(r.get('display_title') or '')[:90]} {r['html_url']}{mark}")
    return "\n".join(out)


def previous_outcome(repo: str, url: str, branch: str) -> str:
    """The same job in the previous run of the same workflow on `branch`: failure / success / absent,
    or unknown when GitHub did not answer (not cached: the next refresh asks again).

    The merge commit's first parent says nothing when the job did not run there (a `paths` filter, a
    matrix leg): the previous run of the workflow is where "already broken" is decided."""
    ids = job_ids(url)
    if not ids:
        return "absent"
    key = (repo, ids[1], branch)
    if key in _previous:
        return _previous[key]
    try:
        job = _get(f"/repos/{repo}/actions/jobs/{ids[1]}")
        run = _get(f"/repos/{repo}/actions/runs/{ids[0]}")
        runs = _get(f"/repos/{repo}/actions/workflows/{run['workflow_id']}/runs", branch=branch, per_page=30,
                    created=f"<{run['created_at']}")  # anchored: on a busy workflow the 30 newest miss it
        # same trigger only: a fork's pull_request run on a branch named like the base is not "before"
        earlier = [r for r in runs.get("workflow_runs", [])
                   if r["created_at"] < run["created_at"] and r["event"] == run["event"] and r["conclusion"] != "cancelled"]
        result, settled = "absent", True
        for prev in earlier[:5]:  # the job may not have run in the last one either (paths filters)
            if prev["status"] != "completed":
                settled = False  # still running: it may decide later, so the answer is not final
                continue
            jobs = _get(f"/repos/{repo}/actions/runs/{prev['id']}/jobs", per_page=100).get("jobs", [])
            same = [j for j in jobs if _job_key(j["name"]) == _job_key(job["name"])
                    and j["conclusion"] not in (None, "skipped", "cancelled")]
            if same:
                result = "failure" if same[0]["conclusion"] in ("failure", "timed_out") else "success"
                break
    except requests.RequestException as e:
        log.info("previous run of %s not found: %s", url, e)
        return "unknown"
    if settled and result != "absent":  # "nothing found" may be a window too short: never final
        _previous[key] = result
    return result


def failing_checks(repo: str, sha: str) -> list[tuple[str, str]]:
    """(name, details URL) of the failed check runs on a commit — a PR head before approval."""
    if not sha:
        return []
    runs = _get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
    return [(r["name"], r.get("details_url") or r.get("html_url") or "") for r in runs
            if r.get("conclusion") in ("failure", "timed_out")]


def _job_key(name: str) -> str:
    """Matrix job names drift between runs ("Plan ./stacks/common" then "Plan stacks/common")."""
    return name.replace("./", "").strip().lower()


def material(*parts: str) -> str:
    """The parts, each fetched separately, joined and capped."""
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= MAX_MATERIAL else text[:MAX_MATERIAL] + "\n[material cut here: the rest was not shown]"


def safely(fetch, *args, **kwargs) -> str:
    """One fetch failing (log expired, API hiccup) leaves a note instead of losing the whole material."""
    try:
        return fetch(*args, **kwargs)
    except requests.RequestException as e:
        return f"({fetch.__name__} unavailable: {e})"


def safely_list(fetch, *args, **kwargs) -> list:
    try:
        return fetch(*args, **kwargs)
    except requests.RequestException as e:
        log.info("%s unavailable: %s", fetch.__name__, e)
        return []
