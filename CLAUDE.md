# Sentinel

Self-hosted watchtower (PRs waiting for your review + your own open PRs + the bot PRs you approved + Atlassian Statuspage incidents + the cycle's big rocks) — local Docker, web UI on `localhost:8300`, Web Push notifications.

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
├── approved_prs.py      # "Approved" tab: bot PRs you approved and that are still open, blocker + stale push
├── my_prs.py            # "My PRs" tab: one GraphQL search (author:login org:…), cached in watcher_state, sort/summary helpers
├── cycle.py             # "Cycle" tab: joins the Slite big rocks with their Shortcut epics, stall alerts
├── slite.py             # Big Rocks note: pick the cycle's note, parse headings → big rocks + outputs
├── shortcut.py          # Shortcut epics (read-only): progress, open stories, last activity
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

## Cycle tab

The objectives live on Slite, the progress lives on Shortcut, and the tab follows the cycle on its own —
no config edit between cycles as long as the conventions hold. `cycle.refresh()` runs on the scheduler
(`cycle.interval_seconds`, default 1800s) and caches the whole join as JSON in `watcher_state`
(`cycle`/`data`), exactly like My PRs.

Four things are discovered, not configured — each with a config override for when it breaks:
`slite.parent_id` → newest child note matching "big rocks" (`slite.note_id` pins one) · cycle dates →
parsed off the note title, "2026 September/October …" → 01/09–31/10, French month names and a Dec/Jan
rollover included (`start`/`end` are the fallback) · cycle label → the highest `<prefix>-<year>-C<n>`,
e.g. Team-2026-C5 (`label`, `label_prefix`) · epics → those carrying that label whose name contains the
big rock title, since infra epics repeat it verbatim ("Infra 2026 09-10 - &lt;big rock&gt;") (`epics:` maps a
title to ids for the odd one out, `run.match` picks the RUN epic by name).

- **Slite** (`slite.py`): `current_note()` takes `note_id` if config pins one, otherwise the newest child
  of `slite.parent_id` whose title contains "big rocks" — so a new cycle needs no config change for the note.
  `parse()` splits on headings and reads the note's own legend: a line carrying ✅/🚧/⛔️/📥 is an output,
  📍🏢🤝⚡️ in a heading are tags, `_Owner_ @Name` is the owner (first one wins — a section can name a
  second owner further down). The note is hand-written and Slite escapes its markdown (`\_`, `\*`), so
  `_clean()` unescapes before anything else and each section keeps its `raw` markdown as a fallback.
  Plain bullets following an output attach to it as `notes` ("Crawlers:" → feeds, forums).
- **Shortcut** (`shortcut.py`): one `/epics/{id}` + one `/epics/{id}/stories` per epic, workflow state
  types resolved once from `/workflows` (Infra and Mention do not share a workflow). `owner_id` comes from
  `/members` via the `shortcut_member` mention name and flags which open stories are yours.
- **Join** (`cycle.py`): `_epics_for()` tries the `epics:` override first (normalized substring, so the
  config key can be shorter than the note heading), then falls back to name-matching the big rock title
  against the labelled epics. A big rock neither mapped nor matched renders as `unlinked` / "no epic
  mapped" rather than silently showing nothing. Epics carrying the label that match no big rock (other
  people's) are simply ignored.
- **Verdict** (`_verdict()`): `stalled` (no movement for `stall_days`) > `behind` (delivered outputs, in %,
  more than `behind_points` under the share of the cycle elapsed) > `on track`; `unlinked` when no epic is
  mapped. Rows are sorted worst-first — the tab answers "which one is slipping", not "what did I do".
  The verdict is measured on **outputs, not stories**: the epics carry stories closed in earlier cycles,
  so story progress flatters a big rock that has delivered nothing this cycle (one big rock sat at 82%
  stories / 0 outputs). Story % stays as a context column, never as the signal.
- **Stall alert**: per big rock, `last_activity` = newest `updated_at` among *your* stories (all stories if
  none are yours). Past `stall_days` it marks the row and sends one Web Push. It re-arms when the big
  rock moves again or after another `stall_days`; state in `watcher_state` (`cycle`/`alerts`). This is the
  one notification of the tab — progress itself never notifies.

Layout is the ledger table of My PRs, not sections: one row per big rock (status · name · outputs ·
stories · last move), one sub-row with the outputs left and the epics as collapsed `<details>`. The nav
badge counts the big rocks that are stalled or behind.

## Approved tab

`approved_prs.refresh()` runs on the scheduler (`approved_prs.interval_seconds`, default 900s) and caches
both buckets as JSON in `watcher_state`, exactly like My PRs — `login`/`orgs` fall back to
`watchers.github_reviews`. One GraphQL call carries two searches: `is:open` and `is:closed closed:>=<window>`,
both scoped to `reviewed-by:<login>` + `author:app/<bot>`.

- **What counts as mine**: `latestReviews` filtered on your login, state `APPROVED` **or** `DISMISSED`.
  Dismissed is kept on purpose — a new Renovate commit wipes the approval, the PR falls back to
  `REVIEW_REQUIRED` and sits there forever (one had been waiting 32 days).
- **`_blocker()`**: first match wins, worst first — dismissed > conflicts > ci failing > changes requested >
  review required > draft > ci running > ready to merge. `RANK` drives both the default sort and the
  "blocked" count.
- **Stale push**: one per PR open more than `stale_days` after your approval, re-armed every `stale_days`
  (state in `watcher_state` `approved_prs`/`alerts`, entries dropped when the PR leaves the open list).
  Deliberately not re-armed on movement: these PRs rebase several times a day.

Layout is the My PRs ledger plus a `<details class="closed-prs">` recap of the window's merged/dropped PRs
— the answer to "did the ones I approved actually land".

## Gotchas

- Subscribe to push from `http://localhost:8300` (never `127.0.0.1`) and with Chrome/Firefox (Safari is finicky)
- `vapid_claims`: pass a fresh dict to every `webpush()` call (pywebpush mutates it)
- OVH: component granularity = datacenter (GRA/RBX/...), filters in `config.yaml`
- Polled regions (`#items`, `#my-prs`, `#watchers`): `app.js` skips the swap when the server HTML is byte-identical to the previous poll, and otherwise saves/restores window scroll, focus + caret and the scroll boxes of open panels around the swap. Without it, hx-preserve moving the panel out and back resets its inner scroll and the re-focused textarea scrolls the page to the top (reproduced 25/09/2026)
- `app/` is COPYed into the image, not bind-mounted: a template or Python change needs `docker compose up -d --build`, a `restart` keeps serving the old code
- Never open `data/sentinel.db` from the host (`sqlite3` CLI) while the container runs: WAL locking across the bind mount → `sqlite3.OperationalError: disk I/O error` in the app (observed 25/09/2026 while polling every 5s). Use the API or `docker compose exec` instead
