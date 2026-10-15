"""Lookout: a tech watch that keeps a handful of finds a week out of hundreds of links.

`collect()` (every `collect_minutes`) reads the sources (lookout_sources) into the `lookout` table, one row
per thing: the same repository on Hacker News, in a newsletter and on GitHub trending is one row with three
mentions. `advance()` (every 2 min) moves rows through Claude — no tool, the material fenced:

  new → scouting → kept | dropped    `scout`: the new titles at once, at most every `scout_hours`
  kept → assessing → assessed        `assess`: a few kept rows read closely (README, page, release notes),
                                     scored 0-10, try / watch / skip, and why it matters to you

Releases of the tools you run skip the scout. A run that fails or is cut by a restart gives its rows back
(`_recover`). The feedback you give on the page (useful, noise, known, to try) goes into both prompts: the
filter learns what you read. One push a week (`digest`) with the finds since the last one; the rest waits
on the page. `Dig in` (on demand, `dig`) reads one find in depth: maturity, fit with your stack, how to
try it.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import agents
import config
import db
import lookout_sources as sources
import notify

log = logging.getLogger("sentinel.lookout")
NAME = "lookout"
SUBJECT = "lookout"
FEEDBACK = ("useful", "noise", "known", "try")
CATEGORIES = ("tool", "release", "practice", "incident", "security", "ai", "news")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_KEPT = re.compile(r"^\s*(?:[-*]\s*)?#(\d+)\b[\s—–:-]*(.*)$")
_SCORED = re.compile(r"^\s*(?:[-*]\s*)?#?(\d+)\s*\|\s*(\d{1,2})\s*(?:/\s*10)?\s*\|\s*(try|watch|skip)\s*\|\s*([\w-]+)\s*\|\s*(.+?)\s*$", re.I)


def enabled() -> bool:
    return bool(config.lookout()["enabled"]) and agents.enabled()


# --- Collect -----------------------------------------------------------------------------------------
def collect():
    """Scheduler job: read every source, keep what is new. A source that fails is noted on the page — never a
    health item: a feed down for a day costs a few links, not a push."""
    for name, read in sources.jobs(config.lookout()["sources"]):
        try:
            new = store(read())
            _source_state(name, "", new)
        except Exception as e:  # noqa: BLE001 — one source never stops the others
            log.warning("lookout source %s failed: %s", name, e)
            _source_state(name, str(e)[:300], 0)


def store(candidates: list) -> int:
    """New things in, a known one gets the new mention. Returns how many were new."""
    now, new = db.now(), 0
    with db.connect() as conn:
        for c in candidates:
            if not c.title or not sources.web_link(c.url):
                continue
            mention = {k: v for k, v in (("discussion", c.discussion if sources.web_link(c.discussion) else ""),
                                         ("points", c.points), ("comments", c.comments)) if v}
            row = conn.execute("SELECT id, sources FROM lookout WHERE key = ?", (c.key,)).fetchone()
            if row:
                seen = json.loads(row["sources"] or "{}")
                seen[c.source] = mention
                conn.execute("UPDATE lookout SET sources = ?, last_seen = ? WHERE id = ?", (json.dumps(seen), now, row["id"]))
                continue
            conn.execute(
                """INSERT INTO lookout (key, kind, title, url, summary, body, sources, published, first_seen, last_seen, stage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (c.key, c.kind, c.title[:300], c.url, c.summary, c.body, json.dumps({c.source: mention}),
                 c.published or None, now, now, "kept" if c.kind == "release" else "new"))
            new += 1
    return new


def _source_state(name: str, error: str, new: int):
    states = db.watcher_states().get(NAME, {})
    previous = json.loads(states.get(f"source:{name}") or "{}")
    since = (previous.get("since") or db.now()) if error else ""
    db.set_watcher_state(NAME, f"source:{name}", json.dumps({"at": db.now(), "error": error, "since": since, "new": new}))


def source_states() -> list[dict]:
    """Every source read so far, failing ones first: the line at the bottom of the page."""
    rows = [{"name": k.removeprefix("source:"), **json.loads(v)} for k, v in db.watcher_states().get(NAME, {}).items()
            if k.startswith("source:")]
    configured = {name for name, _ in sources.jobs(config.lookout()["sources"])}
    return sorted((r for r in rows if r["name"] in configured), key=lambda r: (not r["error"], r["name"]))


# --- Claude, in two passes ---------------------------------------------------------------------------------
def advance():
    """Scheduler job: rows back from a failed run, the next scout and assess, the weekly digest, old rows out."""
    cfg = config.lookout()
    _recover()
    _scout(cfg)
    _assess(cfg)
    digest(cfg)
    _forget(cfg)


def _recover():
    """Rows claimed by a run that is no longer queued or running go back: it failed, or a restart cut it
    (a finished run already moved its rows, before it read done)."""
    for mode, claimed, back in (("scout", "scouting", "new"), ("assess", "assessing", "kept")):
        run = db.get_run(SUBJECT, mode)
        if run and run["status"] in ("queued", "running"):
            continue
        with db.connect() as conn:
            conn.execute("UPDATE lookout SET stage = ? WHERE stage = ?", (back, claimed))


def _ready(run, hours: float) -> bool:
    """Free to start: never ran; or not running, not failed within the hour, and — after a success, or
    MAX_ATTEMPTS failures in a row — `hours` since the last start. A broken run must not spend the daily
    cap the other modes share."""
    if not run:
        return True
    if run["status"] in ("queued", "running") or (run["status"] == "error" and (run["finished_at"] or "") > db.ago(60)):
        return False
    if run["status"] == "error" and run["attempts"] < agents.MAX_ATTEMPTS:
        return True
    return run["created_at"] <= db.ago(hours * 60)


def _scout(cfg: dict):
    run = db.get_run(SUBJECT, "scout")
    with db.connect() as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM lookout WHERE stage = 'new'").fetchone()[0]
    backlog = waiting >= cfg["batch"] and run and run["status"] == "done"
    if waiting and _ready(run, 0 if backlog else cfg["scout_hours"]):
        _claim("scout", "new", "scouting", cfg["batch"], scout_context, cfg)


def _assess(cfg: dict):
    run = db.get_run(SUBJECT, "assess")
    if _ready(run, 0 if run and run["status"] == "done" else cfg["scout_hours"]):
        _claim("assess", "kept", "assessing", cfg["assess_batch"], assess_context, cfg)


def _claim(mode: str, stage: str, claimed: str, limit: int, context, cfg: dict):
    """The oldest rows of `stage` go to the run; refused (daily cap, bridge off) = they stay where they were."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM lookout WHERE stage = ? ORDER BY first_seen, id LIMIT ?", (stage, limit)).fetchall()
        conn.executemany("UPDATE lookout SET stage = ? WHERE id = ?", [(claimed, r["id"]) for r in rows])
    if rows and not agents.request(SUBJECT, mode, "", lambda: context(rows, cfg), auto=True):
        with db.connect() as conn:
            conn.executemany("UPDATE lookout SET stage = ? WHERE id = ? AND stage = ?", [(stage, r["id"], claimed) for r in rows])


def mentions(row) -> list[dict]:
    """Where a find was seen, with its points: what the page and the prompts show."""
    return [{"source": name, **m} for name, m in json.loads(row["sources"] or "{}").items()]


def _seen_on(row) -> str:
    return " · ".join(" ".join([m["source"]] + [f"{m[k]} {k}" for k in ("points", "comments") if m.get(k)])
                      for m in mentions(row))


def _feedback_lines(limit: int = 40) -> str:
    with db.connect() as conn:
        rows = conn.execute("SELECT title, feedback FROM lookout WHERE feedback IS NOT NULL ORDER BY feedback_at DESC LIMIT ?",
                            (limit,)).fetchall()
    if not rows:
        return ""
    return ("\n\nMy feedback on earlier finds, newest first (useful = glad I read it, noise = not for me, known = relevant "
            "but I knew it, try = I want to try it):\n" + "\n".join(f"- {r['feedback']}: {r['title']}" for r in rows))


def scout_context(rows, cfg: dict) -> dict:
    lines = ["Candidates, one per line: #id · where it was seen · title · site — summary"]
    for r in rows:
        summary = " ".join((r["summary"] or "").split())[:200]
        lines.append(f"#{r['id']} · {_seen_on(r)} · {r['title']} · {urlparse(r['url']).hostname or ''}"
                     + (f" — {summary}" if summary else ""))
    return {"profile": cfg["profile"], "material": "\n".join(lines) + _feedback_lines()}


def _material(row, readme: int = 2500, page: int = 3000) -> tuple[str, dict]:
    head = [f"#{row['id']} [{row['kind']}] {row['title']}", f"link: {row['url']}", f"seen on: {_seen_on(row)}"]
    if row["published"]:
        head.append(f"published: {row['published'][:10]}")
    facts: dict = {}
    try:
        repo = sources.github_repo(row["url"])
        if row["kind"] == "repo" and repo:
            text, facts = sources.repo_material(repo, readme_chars=readme, releases_n=8 if readme > 2500 else 3)
        elif row["body"]:
            text = row["body"][:page]
        else:
            # a page that reads as a few words (an app shell, a consent wall) says less than the summary
            read = sources.page_text(row["url"], page)
            text = read if len(read) >= 200 else (row["summary"] or read)
    except Exception as e:  # noqa: BLE001 — a find that cannot be read is judged on its title
        text = f"(could not read it: {str(e)[:200]})"
    return "\n".join(head + [text or "(nothing readable)"]), facts


def assess_context(rows, cfg: dict) -> dict:
    with ThreadPoolExecutor(6) as pool:
        read = list(pool.map(_material, rows))
    with db.connect() as conn:
        conn.executemany("UPDATE lookout SET facts = ? WHERE id = ?",
                         [(json.dumps(facts), r["id"]) for r, (_, facts) in zip(rows, read) if facts])
    return {"profile": cfg["profile"], "material": "\n\n---\n\n".join(text for text, _ in read) + _feedback_lines()}


def apply_scout(run, output: str):
    """Kept rows carry the scout's reason; the other rows of the batch are dropped. Without the verdict line
    nothing moves: the run fails and the rows go back to the next scout."""
    if agents.verdict(output)[0] != "scouted":
        raise ValueError("no `Verdict: scouted` line: the candidates wait for the next scout")
    kept = {int(m.group(1)): m.group(2).strip()[:200] for m in map(_KEPT.match, output.splitlines()) if m}
    with db.connect() as conn:
        for r in conn.execute("SELECT id FROM lookout WHERE stage = 'scouting'").fetchall():
            if r["id"] in kept:
                conn.execute("UPDATE lookout SET stage = 'kept', note = ? WHERE id = ?", (kept[r["id"]], r["id"]))
            else:
                conn.execute("UPDATE lookout SET stage = 'dropped' WHERE id = ?", (r["id"],))


def apply_assess(run, output: str):
    """One scored line per row; a row Claude left out is assessed without a score (never shown)."""
    if agents.verdict(output)[0] != "assessed":
        raise ValueError("no `Verdict: assessed` line: the finds wait for the next assessment")
    scored = {}
    for m in filter(None, map(_SCORED.match, output.splitlines())):
        category = m.group(4).lower()
        scored[int(m.group(1))] = (min(int(m.group(2)), 10), m.group(3).lower(),
                                   category if category in CATEGORIES else "news", m.group(5).strip()[:400])
    now = db.now()
    with db.connect() as conn:
        for r in conn.execute("SELECT id FROM lookout WHERE stage = 'assessing'").fetchall():
            score, verdict, category, why = scored.get(r["id"], (None, None, None, None))
            conn.execute("UPDATE lookout SET stage = 'assessed', score = ?, verdict = ?, category = ?, why = ?, assessed_at = ? "
                         "WHERE id = ?", (score, verdict, category, why, now, r["id"]))


# hooked before the run reads done: its rows are still claimed while they are written
agents.ON_FINISH.update(scout=apply_scout, assess=apply_assess)


# --- Dig in (on demand) -------------------------------------------------------------------------------
def get(find_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM lookout WHERE id = ?", (find_id,)).fetchone()


def target(subject: str, mode: str):
    """`lookout:<id>` for the agents block: the find's link and, when the run is queued, its material."""
    m = re.fullmatch(r"lookout:(\d+)", subject)
    row = get(int(m.group(1))) if m and mode == "dig" else None
    return (row["url"], lambda: dig_context(row)) if row else None


def dig_context(row) -> dict:
    text, facts = _material(row, readme=10_000, page=20_000)
    repo = sources.github_repo(row["url"])
    if row["kind"] == "release" and repo:
        try:
            text += "\n\n" + sources.repo_material(repo, readme_chars=3000)[0]
        except Exception as e:  # noqa: BLE001
            log.info("no repository material for %s: %s", repo, e)
    if facts:
        with db.connect() as conn:
            conn.execute("UPDATE lookout SET facts = ? WHERE id = ?", (json.dumps(facts), row["id"]))
    return {"profile": config.lookout()["profile"], "material": text}


# --- Feedback, digest, forgetting -----------------------------------------------------------------------
def set_feedback(find_id: int, value: str) -> bool:
    if value and value not in FEEDBACK:
        return False
    with db.connect() as conn:
        done = conn.execute("UPDATE lookout SET feedback = ?, feedback_at = ? WHERE id = ?",
                            (value or None, db.now() if value else None, find_id)).rowcount
    return bool(done)


def last_slot(spec: str, now: datetime | None = None) -> datetime | None:
    """The latest `mon 09:00` (local time) at or before now; None when the digest is off."""
    m = re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun)\w*\s+(\d{1,2}):(\d{2})", (spec or "").strip().lower())
    if not m:
        return None
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.TZ))
    slot = local.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
    slot -= timedelta(days=(local.weekday() - _DAYS.index(m.group(1))) % 7)
    return slot - timedelta(days=7) if slot > local else slot


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def finds(where: str = "1", *params) -> list:
    """Assessed rows at or above the threshold that Claude would not skip: what the page calls a find."""
    with db.connect() as conn:
        return conn.execute(
            f"SELECT * FROM lookout WHERE stage = 'assessed' AND score >= ? AND verdict != 'skip' AND ({where}) "
            "ORDER BY score DESC, assessed_at DESC", (int(config.lookout()["threshold"]), *params)).fetchall()


def digest(cfg: dict, now: datetime | None = None):
    """Once a week, at or after the slot: one push with the finds of the week before it, none without any."""
    slot = last_slot(cfg["digest"], now)
    if not slot:
        return
    week = slot.strftime("%Y-%m-%d")
    if db.watcher_states().get(NAME, {}).get("digest") == week:
        return
    rows = finds("feedback IS NULL AND assessed_at >= ? AND assessed_at < ?", iso(slot - timedelta(days=7)), iso(slot))
    db.set_watcher_state(NAME, "digest", week)
    if rows:
        notify.send_push(f"Lookout — {len(rows)} find{'s' if len(rows) > 1 else ''} this week",
                         " · ".join(r["title"] for r in rows[:3]), config.BASE_URL.rstrip("/") + "/lookout", tag="lookout")


def _forget(cfg: dict):
    """Rows nobody gave feedback on leave after `keep_days`; feedback stays, it is what the filter learns from."""
    with db.connect() as conn:
        conn.execute("DELETE FROM lookout WHERE feedback IS NULL AND first_seen < ? AND stage NOT IN ('scouting', 'assessing')",
                     (db.ago(cfg["keep_days"] * 1440),))
