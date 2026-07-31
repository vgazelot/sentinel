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
├── github_pr.py         # PR details (diff, checks) + approve via GitHub API, on demand
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

**Review ▾** button on PR items → `GET /api/items/<id>/pr` (GitHub pull + files + check-runs + combined status) rendered in `partials/pr_details.html`. **Approve ✓** → `POST /api/items/<id>/approve` (APPROVE review) then ack ∞ (the item disappears on the next fetch since the review request is lifted). Approve failure (403/422, e.g. your own PR) → panel re-rendered with the error via the `HX-Retarget` header. The `#pr-details-<id>` container carries `hx-preserve` to survive the 15s `#items` poll. Token: classic PAT with `repo` scope required for approve.

## Gotchas

- Subscribe to push from `http://localhost:8300` (never `127.0.0.1`) and with Chrome/Firefox (Safari is finicky)
- `vapid_claims`: pass a fresh dict to every `webpush()` call (pywebpush mutates it)
- OVH: component granularity = datacenter (GRA/RBX/...), filters in `config.yaml`
