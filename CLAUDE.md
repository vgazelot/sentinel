# Sentinel

Self-hosted watchtower (PRs waiting for your review + Atlassian Statuspage incidents) + minimalist todo list — local Docker, web UI on `localhost:8300`, Web Push notifications. Design: custom CSS in `static/style.css` (dark theme, `:root` variables), Bootstrap only for grid/dropdowns.

## Stack

- **Backend**: Flask 3 + Gunicorn (python:3.12-slim) — **exactly 1 worker required** (`-w 1 --threads 8`): APScheduler runs in-process, multiple workers = duplicated jobs
- **Frontend**: Jinja2 + HTMX 2 + Bootstrap 5 (CDN with SRI, no build step)
- **State**: SQLite `/data/sentinel.db` (WAL), `./data/` volume
- **Notifications**: Web Push (pywebpush + VAPID), service worker `sw.js` served at the root

## Structure

```
app/
├── main.py              # Flask routes + scheduler startup
├── config.py            # config.yaml + env vars
├── db.py                # schema + sqlite3 helpers (no ORM)
├── engine.py            # CORE: diff fetch vs state, ack/snooze state machine
├── notify.py            # Web Push, prunes 404/410 subscriptions
├── github_pr.py         # PR details (diff, checks) + submit_review (APPROVE/COMMENT) via GitHub API
├── claude_review.py     # "Ask Claude": background thread → contrib/claude-bridge → `reviews` table, markdown rendering
├── gen_vapid.py         # one-off VAPID key generation
└── watchers/            # 1 watcher = fetch() → list[WatchedItem], nothing else
    ├── github_reviews.py
    └── statuspage.py    # generic Atlassian Statuspage (Scaleway, OVHcloud, ...)
```

## State machine (engine.py)

`pending` → notification sent, waiting for action. `acked` → re-notifies if the fingerprint changes (unless `ack_forever`). `snoozed` → re-notifies at `snooze_until` (60s job). Absent from the fetch → `resolved`; reappearance → fresh cycle (`new`).

## Adding a watcher

1 file in `watchers/` (class with `name`, `interval_seconds`, `fetch()`), register it in `build_watchers()`, 1 block in `config.yaml`. Diff/state/notifications are already handled.

## Conventions

- Commits: Conventional Commits, no Claude/Co-Authored-By signature
- DB timestamps: UTC ISO `%Y-%m-%dT%H:%M:%SZ`, local display via the `localdt` Jinja filter
- No application auth: bind `127.0.0.1:8300` only

## Inline PR review (dashboard)

**Review ▾** button on PR items → `GET /api/items/<id>/pr` (GitHub pull + files + check-runs + combined status) rendered in `partials/pr_details.html`. The panel header has a single **▴ Hide panel** button (collapse only, never touches the PR). Below: one textarea + **Approve PR ✓** (`POST .../approve`, APPROVE review with the optional message, then ack ∞ — the item disappears on the next fetch since the review request is lifted) and **Comment 💬** (`POST .../comment`, COMMENT review, message required, item stays pending, panel re-rendered with a notice). Every error/notice re-renders the whole panel (diff included) via `_pr_panel()` + `HX-Retarget`. The `#pr-details-<id>` container carries `hx-preserve` to survive the 15s `#items` poll. Token: classic PAT with `repo` scope required for approve/comment.

**Ask Claude** (`partials/claude_review.html`, shown only if `CLAUDE_BRIDGE_URL` is set): `POST .../review/run` starts a daemon thread that calls the host bridge (`contrib/claude-bridge/bridge.py`, `claude -p` read-only, 15 min timeout) and stores the result in `reviews` (one row per item, statuses running/done/error). While running, the partial polls `GET .../review` every 5s (`hx-swap="outerHTML"` — the final render has no trigger, so polling stops). Output rendered with the `md` Jinja filter (python-markdown). The bridge owns the prompt (`BRIDGE_PROMPT`, `{url}`), the app only sends the PR URL.

## Gotchas

- Subscribe to push from `http://localhost:8300` (never `127.0.0.1`) and with Chrome/Firefox (Safari is finicky)
- `vapid_claims`: pass a fresh dict to every `webpush()` call (pywebpush mutates it)
- OVH: component granularity = datacenter (GRA/RBX/...), filters in `config.yaml`
- Never open `data/sentinel.db` from the host (`sqlite3` CLI) while the container runs: WAL locking across the bind mount → `sqlite3.OperationalError: disk I/O error` in the app (observed 25/09/2026 while polling every 5s). Use the API or `docker compose exec` instead
