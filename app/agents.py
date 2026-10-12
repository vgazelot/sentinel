"""Claude agents, run by the host-side bridge (contrib/claude-bridge): Claude Code, your skills and
your gh auth live on the host, not in the container.

  deep      on demand — devil's advocate review of a PR
  quick     on demand — form pass (typos, naming, leftovers)
  precheck  automatic — reads a dependency bump (all its PRs at once) before you approve it
  followup  automatic — a PR you approved failed after merge: did it cause it, what to do

Runs are queued in agent_runs and started by `tick()` (scheduler job), at most `concurrency`
at a time; automatic ones are capped per day. The bridge owns prompts, models and tools per
mode: this side only sends the PR URL(s) and a small context.
"""
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone

import requests

import config
import db
import md
import notify

log = logging.getLogger("sentinel.agents")
TIMEOUT = 960
MODES = {
    "deep": {
        "button": "Devil's advocate",
        "title": "Structural review: durability, modeling, blast radius. Read-only, nothing is posted on GitHub.",
        "eta": "5–10 min",
        "verdict": "Devil's advocate",
    },
    "quick": {
        "button": "Quick pass",
        "title": "Form only: typos, naming, leftovers, PR text vs change. Read-only, nothing is posted on GitHub.",
        "eta": "1–3 min",
        "verdict": "Quick pass",
    },
    "precheck": {
        "button": "Check this bump",
        "title": "Claude reads the release notes and the diff of every PR of this bump before you approve.",
        "eta": "1–3 min",
        "verdict": "Bump check",
    },
    "followup": {
        "button": "Investigate",
        "title": "Claude reads the failed post-merge run: did this PR cause it, what to do.",
        "eta": "2–6 min",
        "verdict": "Post-merge investigation",
    },
}
ON_DEMAND = ("deep", "quick")
AUTOMATIC = ("precheck", "followup")
# the first line the bridge prompts ask for — only that line counts: a verdict quoted further down
# (a log, a release note) must not stand for Claude's own
VERDICT = re.compile(r"verdict\s*:\s*\**\s*([\w-]+)", re.I)
MAX_ATTEMPTS = 3
TONE = {"safe": "ok", "clean": "ok", "unrelated": "ok", "check": "warn", "unclear": "warn",
        "tidy-up": "warn", "risky": "bad", "caused-by-this-pr": "bad"}


def enabled() -> bool:
    return bool(config.CLAUDE_BRIDGE_URL)


def request(subject: str, mode: str, url: str, context: dict | Callable[[], dict] | None = None,
            auto: bool = False) -> bool:
    """Queue a run unless one is already queued/running; automatic runs obey the config and the daily cap.
    `context` may be a callable: its fetches only happen when the run is actually queued."""
    if not enabled():
        return False
    current = db.get_run(subject, mode)
    if current and current["status"] in ("queued", "running"):
        return False
    if auto:
        cfg = config.agents()
        if not cfg.get(mode, True):
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        used = db.count_runs("created_at >= ? AND mode IN ('precheck', 'followup')", today)
        if used >= int(cfg["max_runs_per_day"]):
            log.info("daily cap reached, %s for %s not queued", mode, subject)
            return False
    payload = context() if callable(context) else (context or {})
    db.queue_run(subject, mode, url, payload, retry=auto and bool(current) and current["status"] == "error")
    return True


def settled(run, **context) -> bool:
    """An automatic run already covers this: done, under way, failed less than an hour ago, or failed
    MAX_ATTEMPTS times (a bridge that was down gets two more tries, a broken one stops there)."""
    if not run or any(json.loads(run["context"] or "{}").get(k) != v for k, v in context.items()):
        return False
    if run["status"] != "error":
        return True
    return run["attempts"] >= MAX_ATTEMPTS or (run["finished_at"] or "") > db.ago(60)


def tick():
    """Scheduler job: start queued runs while there is room."""
    if not enabled():
        return
    room = int(config.agents()["concurrency"]) - db.count_runs("status = 'running'")
    if room <= 0:
        return
    for run in db.claim_runs(room):
        threading.Thread(target=_execute, args=(run,), daemon=True, name=f"agent-{run['id']}").start()


def _execute(run):
    try:
        context = json.loads(run["context"] or "{}")
        r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/run", timeout=TIMEOUT, headers=_auth(),
                          json={"mode": run["mode"], "url": run["url"], "urls": context.pop("urls", []),
                                "context": context})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
        output = (data.get("output") or "").strip() or "(empty answer)"
        db.finish_run(run["id"], output=output)
        log.info("claude %s for %s done in %ss", run["mode"], run["subject"], data.get("duration"))
        if run["mode"] == "followup":
            _announce(run, output)
    except Exception as e:  # noqa: BLE001
        log.warning("claude %s for %s failed: %s", run["mode"], run["subject"], e)
        db.finish_run(run["id"], error=str(e))


def _announce(run, output: str):
    """A post-merge investigation answers "did I break something": that answer is worth a push."""
    line = summary_line(output)
    with db.connect() as conn:
        item = conn.execute("SELECT id FROM items WHERE watcher = 'followup' AND external_id = ?",
                            (f"{run['subject']}:failed",)).fetchone()
        if item:
            db.log_notification(conn, item["id"], "claude", f"Claude — {run['subject']}", line)
    notify.send_push(f"Claude — {run['subject']}", line, run["url"], tag=f"followup-{run['subject']}")


def verdict(output: str | None) -> tuple[str, str]:
    """(token, tone) from the first line, `Verdict: x`, tone in ok/warn/bad; ("", "") otherwise."""
    first = next((line.strip(" *#>`") for line in (output or "").splitlines() if line.strip()), "")
    m = VERDICT.match(first)
    if not m:
        return "", ""
    token = m.group(1).lower()
    return token, TONE.get(token, "warn")


def _auth() -> dict:
    return {"Authorization": f"Bearer {config.CLAUDE_BRIDGE_TOKEN}"} if config.CLAUDE_BRIDGE_TOKEN else {}


def check_bridge():
    """Scheduler job: the bridge answers, with this version's modes — an old bridge left running after
    an upgrade 404s every automatic run in silence. Failing long enough becomes a health item."""
    try:
        r = requests.get(f"{config.CLAUDE_BRIDGE_URL}/healthz", headers=_auth(), timeout=10)
        r.raise_for_status()
        missing = set(MODES) - set(r.json().get("modes", []))
        error = f"the bridge is an older version (no {', '.join(sorted(missing))}): restart it" if missing else ""
    except (requests.RequestException, ValueError) as e:
        error = f"the Claude bridge does not answer: {e}"
    db.record_run("claude-bridge", error)


_EMPHASIS = re.compile(r"\*\*|__|`")


def first_finding(output: str | None) -> str:
    """The first line after the verdict, as plain text: what a collapsed row can show."""
    lines = [_EMPHASIS.sub("", l).strip(" -*#>") for l in (output or "").splitlines() if l.strip()]
    rest = [l for l in lines if l and not VERDICT.match(l)]
    return (rest[0] if rest else (lines[0] if lines else ""))[:220]


def summary_line(output: str | None) -> str:
    """Verdict + first finding: a notification or the Claude Code status pane."""
    head = verdict(output)[0]
    return (f"{head} — " if head else "") + first_finding(output)


def render(text: str) -> str:
    """Claude points at `path:line`: make those refs jump to the diff line in the panel."""
    return md.link_file_refs(md.render(text))
