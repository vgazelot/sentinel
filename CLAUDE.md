# Sentinel

Self-hosted officer of the watch: review requests sorted into lanes, your own PRs by what they wait for, the PRs you approved followed until they have landed (merged + post-merge checks green), provider status pages, optional Cycle objectives — local Docker, web UI on `localhost:8300`, Web Push, optional Claude agents through a host bridge and a Claude Code mod reading `/api/summary`.

**Design (12/10/2026 redesign): calm and readable.** Light only: grey page `--bg`, white cards (`.card`, 12 px radius), sentence case everywhere (uppercase only in the wordmark), Source Sans 3 for text, mono only for refs and diffs. Meaning is carried by plain words plus one colour each: `hot` = waits on you, `bad` = broke, `warn` = needs a look, `ok` = good news (`.dot`, `.pill`). One primary action per row, the rest in a `⋯` menu (`<details class="menu">`). No gradient, no emoji, no CSS framework. Tokens at the top of `static/style.css` — keep new UI inside them.

## Stack

- **Backend**: Flask 3 + Gunicorn (python:3.12-slim) — **exactly 1 worker** (`-w 1 --threads 8`): APScheduler runs in-process, more workers = duplicated jobs
- **Frontend**: Jinja2 + HTMX 2 (CDN with SRI) + Google Fonts, no build step
- **State**: SQLite `/data/sentinel.db` (WAL), `./data/` volume
- **Notifications**: Web Push (pywebpush + VAPID), service worker `sw.js` served at the root

## Structure

```
app/
├── main.py           # routes, view models (_view, _now_context, _mine_groups, _units), scheduler jobs
├── config.py         # config.yaml + env; triage(), agents(), github() with defaults
├── db.py             # schema + migrations (_migrate_items, _migrate_reviews) + helpers, no ORM
├── engine.py         # CORE: diff fetch vs state, notification policy, ack/snooze, grace, health
├── watchers/         # 1 watcher = fetch() → list[WatchedItem] (lane + urgency), nothing else
│   ├── github_reviews.py   # GraphQL: lanes you / team / deps, team+APPROVED dropped, bump bodies cached
│   └── statuspage.py       # generic Atlassian Statuspage; maintenance + `notify: quiet` → quiet
├── checks.py         # check runs / statuses read one way: rollup, outcome (cancelled = neutral), CI family
├── evidence.py       # material for the automatic agents (PR, diff, job logs, workflow history) + previous run outcome
├── deps.py           # Renovate/Dependabot parsing (title, kind, update table), grouping by name (kind)@to, prechecks
├── my_prs.py         # your open PRs + next action; actionable ones → engine items (lane mine)
├── followup.py       # PRs you approved: blocker, merge commit checks, inherited/failed, revert → items + investigation
├── agents.py         # agent_runs queue → bridge, modes deep/quick/precheck/followup, verdict parsing
├── github_pr.py      # PR panel data (numbered diff, checks, threads) + submit_review + reply
├── cycle.py · slite.py · shortcut.py   # Cycle tab; stalled big rocks → engine items (lane cycle)
├── md.py             # markdown → HTML + `path:line` → .fileref links
└── templates/partials/  now · rows (macros) · agent_macro · pr_details · my_prs · followup · cycle · watchers
contrib/claude-bridge/   bridge.py (host, launchd) — modes, prompts, toolsets
contrib/claude-code-mod/ the Claude Code mod (status line, toasts, /sentinel pane) — reads /api/summary
tests/                   unittest, run inside the image (see Tests)
```

## Triage model (engine.py)

Every `WatchedItem` has a **lane** (where it shows: `you`, `team`, `deps`, `mine`, `followup`, `incident`, `cycle`, `health`) and an **urgency**:
- `now` → pushed when it appears, again when its fingerprint changes after an ack
- `later` → pushed only if still pending after `triage.team_grace_minutes` (`promote_due()`, every minute) — team review requests; resolved meanwhile = never pushed
- `quiet` → never pushed (bumps, drafts, maintenance, reminders)

"Needs you" = `db.ATTENTION`: pending and (`now`, or `later` already notified). Badges count **decisions** (`main._units`): every item, except your own PRs which count once per cause (`_mine_groups`: action + first failing CI family). A post-merge failure whose follow-up run says `unrelated` is cleared (`_clear_by_claude`): it leaves "needs you" for the radar.

State machine: `pending` → `acked` (re-opens when the fingerprint changes unless `ack_forever`) / `snoozed` (`wake_snoozed()`, quiet items wake silently) → absent from `MISSES` (2) fetches in a row = `resolved` (`items.missed`; GitHub search drops PRs for a poll now and then — 17 PRs were re-pushed within 30 min before this) → reappearance = fresh cycle (`first_seen` reset). Fingerprints encode what should re-open an ack: `updatedAt` for direct reviews, the constant `team` for team requests (a team PR moving is still not yours), the target version for bumps, the action (+ failing CI families) for your PRs, a `days // stale_days` bucket for stuck approvals.

Guards: `promote_due()` and `wake_snoozed()` skip items missing from the last poll (`missed > 0`). More than `STORM` (3) notifications from one pass → one summary push. **First pass of a source** (no `seeded` flag in `watcher_state`) records without pushing and re-baselines old rows (`notified_at = first_seen`, acks moved to the new fingerprint) — an upgrade that changes fingerprints must not resurface everything. `promote_due()` ignores sources not seeded yet. `check_health()`: `db.record_run()` keeps `error_since` per source; failing longer than `triage.health_minutes` → item (lane health).

## Follow-up (followup.py)

One GraphQL call: `reviewed-by:<login> -author:<login> author:app/<bot>…` open + closed within `window_days`, with the merge commit's check runs and its **first parent's** check runs. Post-merge checks = check runs whose workflow event is not `merge_group`/`pull_request*` + commit statuses. Stage: `waiting` (blocker, worst first: dismissed > conflicts > ci failing > changes requested > review required > draft > ci running > ready) · `checking` (pending, or merged < 30 min with nothing yet) · `failed` · `inherited` (every failing check was already failing before: on the parent commit, or — when the job did not run there, `paths` filters — in the previous run of its workflow on the base branch, `evidence.previous_outcome`, cached) · cancelled runs are neutral (`concurrency:` supersedes them) · `reverted` (a cross-referenced PR titled "Revert…") · `landed` · `no checks` · `closed`. Items: dismissed, stuck (`waiting_days >= stale_days`), failed, reverted. A `failed` PR merged less than `AUTO_INVESTIGATE_HOURS` (48) ago gets one `followup` agent run per merge commit — never on the source's first pass; older ones keep the Investigate button (no surprise push about the past). Pages read the stages through `main._followup_view`: `inherited` + failed-and-cleared-by-Claude = section "not yours", and every count (Now strip, Follow-up page, badge) comes from it or from the engine items. `followup.cleared()` is the one clearing rule: verdict `unrelated` AND no failing check that passed right before the merge AND every log read (`logs_missing` in the run context) — a verdict alone can be steered. A previous run GitHub could not return (`unknown`) keeps the last refresh's answer, else the stage waits in `checking` rather than alarming; `previous_outcome` compares same-event runs only and caches only final answers.

## Dependency bumps (deps.py)

A review request whose author is a Bot and whose title parses as a bump (or carries a renovate/dependencies label) goes to lane `deps`. The PR body is fetched once per (PR, title) for the update table: type (major > minor > patch), from → to. `pending_groups()` groups pending bump items by `name@to`; **Approve N green** (`POST /api/deps/approve {key, ids}`) reviews only the ids the page showed that are still `approvable()` (green, not draft, not conflicting) and whose head is still the sha seen (`github_pr.head_sha`), pinned with `commit_id` — the rest is reported back. `precheck()` (every 5 min) queues one `precheck` run per group (subject `deps:<name>@<to>`), PR URLs of every repo at once, and again when the set of PRs changes (`settled(urls=…)`).

## Claude agents (agents.py + contrib/claude-bridge)

`agent_runs` = one row per (subject, mode), subject = `owner/repo#n` or `deps:<key>`; `queue_run` re-queues, `tick()` (every 10 s) starts up to `agents.concurrency` runs in threads → `POST {bridge}/run {mode, url, urls, context}` with `Authorization: Bearer CLAUDE_BRIDGE_TOKEN`. Automatic modes obey `agents.precheck|followup` and `max_runs_per_day`, retry a failed run after an hour up to `MAX_ATTEMPTS` (3, `attempts` column); on-demand ones never do, and reset attempts. Their context is a callable (`deps.context`, `followup.context`): the GitHub fetches of the material only happen when a run is actually queued. `check_bridge()` (every 2 min) records `claude-bridge` in `watcher_state`: a bridge that does not answer, or an older one without this version's modes, becomes a health item. The page only names a subject: `_agent_target()` rebuilds URL and context from Sentinel's own data. Prompts start with `Verdict: <token>` (English tokens whatever `BRIDGE_LANG`): `agents.verdict()` reads the **first line only** (a verdict quoted from a log further down must not count) → (token, tone), `first_finding()` for rows, `summary_line()` for pushes. A finished `followup` run pushes its verdict (`_announce`). Every run passes `--setting-sources user`: the project/local settings of `BRIDGE_CWD` carry their own allow rules, which **add** to `--allowedTools` (verified 12/10/2026: an allowed `Bash(tee:*)` wrote a file). Toolsets: `quick`, `precheck`, `followup` = **no tool** (`--tools ""`), the material (built by `evidence.py`; `quick` = `pr_brief` with 800 diff lines) arrives in `context` and the bridge fences **all** of it between BEGIN/END MATERIAL as untrusted (a custom prompt without `{context}` gets it appended); `deep` = named read-only commands, no `gh api` (`-f` POSTs, `-X` can follow any prefix), no WebFetch, offered only on PRs whose author is not a Bot. The bridge refuses non-local Host headers, runs nothing without `BRIDGE_TOKEN`, and `/healthz` reports `api` (2) and `token`: `check_bridge()` flags an older bridge or a missing token. The daily cap counts attempts (`db.attempts_since`). `reviews` (old table) is migrated into `agent_runs` at startup.

## API for the Claude Code mod

`GET /api/summary?since=<notification id>` → `attention`, per-section `counts`, `landing` (follow-up stages), up to 25 items needing you (id, lane, ref, title, url, why, claude headline), notifications after `since`, `seq`. Item actions answer JSON unless the request comes from HTMX (`_done()`): `POST /api/items/<id>/ack|unack|snooze|unsnooze`, `POST /api/items/bulk {ids, action: ack|forever|snooze, duration}`.

## Adding a watcher

1 file in `watchers/` (class with `name`, `interval_seconds`, `fetch()` returning `WatchedItem`s with lane + urgency + a fingerprint that changes only when an ack should re-open), register it in `build_watchers()`, 1 block in `config.yaml`. Diff, state, grace, notifications are already handled. A source computing its own state (like my_prs/followup) calls `engine.process(name, items)` after `db.record_run(name)`.

## Tests

```bash
docker build -t sentinel-dev ./app
docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -v "$PWD/contrib:/contrib" -w /app sentinel-dev python -m unittest discover -s /tests
```
`tests/helpers.SentinelTest` gives each test its own database and config and captures pushes; `SENTINEL_SCHEDULER=0` lets tests import `main` without background jobs. `test_web.py` renders every page on a populated database — template errors only show at render time.

A second instance on a copy of the database (UI work without touching the live one): copy with `sqlite3.backup` from inside the running container, run the image on another port with `SENTINEL_PUSH_DISABLED=1`, `SENTINEL_BASE_URL`, and the copy mounted on `/data`.

## Conventions

- Commits: Conventional Commits, no Claude/Co-Authored-By signature
- DB timestamps: UTC ISO `%Y-%m-%dT%H:%M:%SZ`, local display via `localdt`, ages via `age`
- No application auth: bind `127.0.0.1:8300` only
- Public repo: no org names, logins or internal project names in code, docs, examples or screenshots (config.yaml/.env/data are gitignored; screenshots only from fake data)

## Gotchas

- Subscribe to push from `http://localhost:8300` (never `127.0.0.1`) and with Chrome/Firefox (Safari is finicky)
- `vapid_claims`: pass a fresh dict to every `webpush()` call (pywebpush mutates it)
- Never call a `db.*` helper that opens its own connection while holding a write transaction (`with db.connect() as conn:` after a write): the second connection waits on the lock for `busy_timeout` then fails — read `watcher_state` before the transaction, write it after
- Polled regions (`#now`, `#my-prs`, `#follow-up`, `#watchers`, `#cycle`): `app.js` skips the swap when the HTML is byte-identical, and otherwise restores window scroll, focus + caret, inner scroll boxes and every open `<details id>` around the swap. Give any new `<details>` an id or it closes on the next poll. PR panels survive through `hx-preserve`
- `app/` is COPYed into the image, not bind-mounted: a template or Python change needs `docker compose up -d --build`
- Never open `data/sentinel.db` from the host while the container runs (WAL across the bind mount → `disk I/O error` in the app): `docker compose exec` or the API
- Jinja: a dict key named like a dict method (`items`, `keys`) resolves to the method — use another key (sections use `rows`)
- Bridge plist edits: `launchctl bootout` then `bootstrap` (`kickstart -k` keeps the old EnvironmentVariables). The bridge is a separate process: an upgrade of the app needs a bridge restart too, `check_bridge()` flags an old one
- Rendered markdown (Claude verdicts, GitHub comments) goes through `md.render` → nh3 allowlist: never mark untrusted HTML `|safe` any other way
