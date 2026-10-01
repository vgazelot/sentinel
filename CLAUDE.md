# Sentinel

Self-hosted watchtower (PRs waiting for your review + your own open PRs + Atlassian Statuspage incidents) — local Docker, web UI on `localhost:8300`, Web Push notifications.

**Design (25/09/2026 redesign): a logbook, not a dashboard.** Light only: cool paper `--paper`, ink, one signal colour `--signal` (urgent/failed), semantic ok/warn. Type: Barlow Semi Condensed (wordmark, tabs, labels, buttons — uppercase + letter-spacing), Source Sans 3 (body), system mono (`.ref` ids, ages, diffs). Layout = ledger rows (`.row` grid: kind · body · age · actions) separated by rules, square corners, no cards, no gradients, no emoji, no Bootstrap (HTMX only; snooze = `<details class="menu">`). State is encoded by `.mark` chips (ok/fail/pend/none). Keep new UI inside this system — tokens at the top of `static/style.css`.

## Stack

- **Backend**: Flask 3 + Gunicorn (python:3.12-slim) — **exactly 1 worker required** (`-w 1 --threads 8`): APScheduler runs in-process, multiple workers = duplicated jobs
- **Frontend**: Jinja2 + HTMX 2 (CDN with SRI) + Google Fonts, no build step, no CSS framework
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
├── github_pr.py         # PR details (numbered diff, checks, review threads) + submit_review (APPROVE/COMMENT/REQUEST_CHANGES + line comments) + reply
├── md.py                # markdown → HTML (python-markdown + blank line before lists, GitHub-style) + `path:line` → .fileref links
├── claude_review.py     # "Ask Claude": background thread → contrib/claude-bridge → `reviews` table, markdown rendering
├── my_prs.py            # "My PRs" tab: one GraphQL search (author:login org:…), cached in watcher_state, sort/summary helpers
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

**Review** button on PR items → `GET /api/items/<id>/pr` (GitHub pull + files + check-runs + combined status + review comments + reviews) rendered in `partials/pr_details.html`. Header: facts, `approved · user` / `changes · user` marks, single **Hide panel ▴** (collapse only). Below: one summary textarea + **Approve PR** / **Comment** / **Request changes** (`POST .../approve|comment|request-changes`, one review each, all through `_submit_review()`), then the diff.

**Line comments (GitHub style)**: `github_pr._patch_lines()` numbers every diff line from the hunk headers (`old`/`new`, plus `side` LEFT/RIGHT + `line` = what the reviews API expects). Each line renders as `.dl` with a gutter `+` button; `app.js` opens an inline form (click = one line, shift-click = range on the same file/side), queues comments in memory (`pending[itemId]`, shown as `.thread.pending` boxes) and sends them as a JSON `comments` field via `hx-vals="js:{comments: pendingJson(id)}"` with any of the three buttons → one GitHub review (`commit_id` = head sha). Success answers with `HX-Trigger: reviewSubmitted` (queue dropped); an error re-renders the panel and `htmx:afterSwap` puts the queue back on the fresh DOM. Existing review threads (`GET /pulls/n/comments`, grouped by `in_reply_to_id`) render under their line, outdated ones (`line: null`) under the file as "Comments on lines not shown"; each thread has a Reply form (`POST .../reply`). Limits: 50 files, 600 lines/file. Any of the three submitted reviews acks the item on the PR's post-review `updated_at` (read back from the API): the row leaves the inbox at once and comes back only if the PR moves again (Approve/Request changes also drop the review request, so the next fetch resolves it instead). Every error/notice re-renders the whole panel via `_pr_panel()` + `HX-Retarget`; `#pr-details-<id>` carries `hx-preserve` to survive the 15s poll. Token: classic PAT with `repo` scope required for reviews.

**Ask Claude** (`partials/claude_review.html`, shown only if `CLAUDE_BRIDGE_URL` is set): two buttons = two modes, `deep` (devil's advocate, subagents, default model) and `quick` (form/typos pass, no subagents, faster model), listed in `claude_review.MODES` (UI copy only). `POST .../review/run` (`mode` form field) starts a daemon thread that calls the host bridge (`contrib/claude-bridge/bridge.py`, `claude -p` read-only, 15 min timeout) and stores the result in `reviews` (one row per `(item, mode)`, statuses running/done/error; `db._migrate()` converts the old one-row-per-item table). While any mode is running, the partial polls `GET .../review` every 5s (`hx-swap="outerHTML"` — the final render has no trigger, so polling stops). Output rendered with the `md` Jinja filter (python-markdown). The bridge owns prompt + model per mode (`BRIDGE_PROMPT`/`BRIDGE_PROMPT_QUICK`, `BRIDGE_MODEL`/`BRIDGE_MODEL_QUICK`, `{url}`), the app only sends the PR URL and the mode. `claude_review.render()` adds `md.link_file_refs()` on top of the markdown: every `path/file.ext:42` (or `:42-50`) in the verdict becomes an `<a class="fileref">`, and `app.js` resolves it against the diff below (exact `data-path`, else suffix, else basename; RIGHT side preferred), scrolls both the page and the `.pr-diff` box to it and flashes `.dl.is-target`. Not in the rendered diff (50 files / 600 lines limits, a line outside a hunk, a file Claude read but the PR didn't touch) → opens `data-blob` on the panel (`<pr url minus /pull/n>/blob/<head sha>/<path>#L42`) in a new tab. The HTML is walked tag by tag so markup and existing `<a>` are left alone; the ref pattern needs a dotted filename with an alphabetic extension, which keeps `12:30`, `localhost:8080` and URLs out.

## My PRs tab

`my_prs.refresh()` runs on the scheduler (same login/orgs/interval as `github_reviews` in `config.yaml`) and stores the normalized list as JSON in `watcher_state` (`my_prs`/`data`, `last_run`, `last_error`). `/my-prs` renders it sorted server-side (`?sort=updated|created|repo|title|ci|review&dir=asc|desc`, default: oldest update first so forgotten PRs surface), `#my-prs` polls the partial every 60s keeping the sort, **Refresh** runs the fetch now. No notifications by design.

## Gotchas

- Subscribe to push from `http://localhost:8300` (never `127.0.0.1`) and with Chrome/Firefox (Safari is finicky)
- `vapid_claims`: pass a fresh dict to every `webpush()` call (pywebpush mutates it)
- OVH: component granularity = datacenter (GRA/RBX/...), filters in `config.yaml`
- Polled regions (`#items`, `#my-prs`, `#watchers`): `app.js` skips the swap when the server HTML is byte-identical to the previous poll, and otherwise saves/restores window scroll, focus + caret and the scroll boxes of open panels around the swap. Without it, hx-preserve moving the panel out and back resets its inner scroll and the re-focused textarea scrolls the page to the top (reproduced 25/09/2026)
- Never open `data/sentinel.db` from the host (`sqlite3` CLI) while the container runs: WAL locking across the bind mount → `sqlite3.OperationalError: disk I/O error` in the app (observed 25/09/2026 while polling every 5s). Use the API or `docker compose exec` instead
