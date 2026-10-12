# Sentinel 🛰️

> A tiny self-hosted watchtower for things that need your attention — GitHub PRs waiting for your review, your own open PRs, and status page incidents — with native Web Push notifications and a built-in ack/snooze workflow.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white)

![Sentinel dashboard](docs/screenshot.png)

## What it does

- 👀 **GitHub review requests** — polls PRs where your review is requested (directly or via a team) across your orgs. Inline review panel on the dashboard: numbered diff, CI checks, existing review threads with reply, GitHub-style **line comments** (click a line, shift-click a range) sent as one review with **Approve**, **Comment** or **Request changes**, and optional **Ask Claude** buttons for a second opinion (devil's advocate, or a quick form/typos pass).
- 🚦 **Status pages** — watches any [Atlassian Statuspage](https://www.atlassian.com/software/statuspage) API (Scaleway, OVHcloud, GitHub, Cloudflare, …) with component/keyword filters, so you only hear about incidents that affect *your* infra.
- 🔔 **Web Push notifications** — native OS notifications through your browser (Chrome/Firefox), no third-party service.
- ✅ **Ack / snooze workflow** — every item nags you exactly once, then stays silent until something actually changes.
- 📋 **Your open PRs** — one sortable ledger of every PR you authored across your orgs: CI, review decision, conflicts, age — so nothing rots forgotten.
- 🤖 **Approved, not merged** (optional) — the bot PRs (Renovate, Dependabot) you approved that are *still* open: why each one is stuck (conflicts, red CI, an approval dismissed by a new commit), how long it has been waiting, and what merged over the last week. Approving is not merging — automerge rules do not always fire.
- 🎯 **Cycle objectives** (optional) — your big rocks read from a [Slite](https://slite.com) note, joined with the [Shortcut](https://shortcut.com) epics that carry them: progress against the share of the cycle already elapsed, and a push notification when a big rock goes quiet for too long.

Everything runs locally in a single Docker container with a SQLite file — no accounts, no cloud, no telemetry. The UI is a plain logbook: paper, ink, one signal colour, no build step.

## Quick start

```bash
git clone https://github.com/vgazelot/sentinel.git && cd sentinel

# 1. Configuration: set your GitHub login/orgs and the status pages you care about
cp config.example.yaml config.yaml

# 2. Secrets: add a GitHub token (quick bootstrap: gh auth token)
cp .env.example .env

# 3. Generate the Web Push (VAPID) keys and paste them into .env
docker compose build
docker compose run --rm sentinel python gen_vapid.py

# 4. Run
docker compose up -d
```

Open **http://localhost:8300** — then go to **Settings → Subscribe this browser** to enable notifications (use `localhost`, not `127.0.0.1`: the service worker origin depends on it).

> [!WARNING]
> Sentinel has **no authentication**. It is designed to run on your own machine, bound to `127.0.0.1` (the default in `docker-compose.yml`). Never expose it to a network.

## How ack / snooze works

| Action | Effect |
|---|---|
| **Ack** | Silent until the item *changes* (new push/comment on the PR, new incident update) |
| **Ack ∞** | Never notify again for this item |
| **Snooze** | Re-notifies when it expires (30 min / 1 h / 4 h / tomorrow 9am) |
| — | Item gone from the source (PR merged/reviewed, incident resolved) → auto-resolved |

## Configuration

`config.yaml` (mounted read-only — restart after editing: `docker compose restart`):

```yaml
watchers:
  github_reviews:
    enabled: true
    interval_seconds: 180
    login: your-github-username
    orgs: [your-org]

  statuspage:
    enabled: true
    interval_seconds: 300
    instances:
      - name: scaleway
        url: https://status.scaleway.com/api/v2/summary.json
        components: [Instances, Object Storage]   # an affected component must match (substring)
        keywords: []                               # extra AND filter, matched at word start
        include_maintenances: true
```

Any status page exposing `/api/v2/summary.json` works. `components` and `keywords` keep the noise down: `keywords: [gra]` matches `GRA1` but not `degraded`.

Environment variables (`.env`):

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Lists PRs waiting for your review. A fine-grained PAT with read-only PR access is enough; the **Approve** button needs a classic PAT with `repo` scope |
| `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` | Web Push keys — generate with `docker compose run --rm sentinel python gen_vapid.py` |
| `VAPID_CLAIM_EMAIL` | Contact email embedded in push claims (any address you own) |
| `TZ` | Timezone for local time display and "snooze until tomorrow 9am" (default `UTC`) |
| `CLAUDE_BRIDGE_URL` | Optional — URL of the host-side [Claude bridge](#ask-claude-optional) (`http://host.docker.internal:8301`). Empty = button hidden |

## Cycle tab (optional)

Hidden unless `SLITE_API_KEY` and `SHORTCUT_API_TOKEN` are in `.env` *and* `config.yaml` carries a
`cycle:` block. Objectives come from the Slite note, live progress from Shortcut. It rolls over to the
next cycle by itself: newest note under `parent_id`, dates read off its title, highest
`<prefix>-<year>-C<n>` label, and the epics carrying that label whose name contains the big rock title.
Everything commented out below is an override for when a convention is not respected.

```yaml
cycle:
  enabled: true
  interval_seconds: 1800
  owner: Your Name                # matches the `_Owner_ @Your Name` line of a note section
  shortcut_member: yourmention    # Shortcut mention name, flags your own stories
  stall_days: 7                   # no activity for that long on a big rock → one Web Push
  behind_points: 10               # outputs delivered this far under the elapsed cycle → BEHIND
  slite:
    parent_id: xxxxxxxxxxxx       # collection holding one note per cycle; the newest is picked
    # note_id: yyyyyyyyyyyy       # ... unless you pin one here
  # label: Team-2026-C5          # ... unless you pin one here
  # start: 2026-09-01             # ... unless the note title carries no months
  # end: 2026-10-31
  epics:                          # override for a big rock whose epics are named differently
    "Some big rock": [12345]
  run:                            # the day-to-day epic, shown apart from the objectives
    label: RUN
    match: RUN my-service
```

Each row gets a verdict — `STALLED` (nothing moved for `stall_days`), `BEHIND` (outputs delivered lag the
share of the cycle elapsed), `ON TRACK` — and rows are sorted worst first. The verdict is measured on the
note's outputs, not on story progress: epics carry stories closed in earlier cycles, so a percentage can
look healthy while nothing was delivered this cycle. Story % stays as a context column.

## Approved tab (optional)

Hidden unless `config.yaml` carries an `approved_prs:` block. One GraphQL search per refresh:
the PRs opened by the listed bot accounts that carry a review of yours, open ones on top
(sorted by blocker, worst first) and the recently closed ones folded underneath.

```yaml
approved_prs:
  enabled: true
  interval_seconds: 900
  authors: [renovate]  # bot login, searched as author:app/<login>
  stale_days: 3                   # approved longer ago than this and still open → one push
  window_days: 7                  # how far back the "recently closed" list reaches
```

`login` and `orgs` fall back to `watchers.github_reviews`, so they live in one place. A review of
yours in state `DISMISSED` keeps the PR in the list and flags it: a new commit wiped your approval,
the PR is back to "review required" and will never merge on its own. The push fires once per PR past
`stale_days` and re-arms every `stale_days` after that — a Renovate PR rebases constantly, so
notifying on movement would be pure noise.

## Fallback notifier (macOS, optional)

Web Push only works while a subscribed browser is running. `contrib/host-notifier/` provides a launchd agent that polls the API and relays pending notifications through [`terminal-notifier`](https://formulae.brew.sh/formula/terminal-notifier):

```bash
brew install terminal-notifier
# edit the plist first: replace /path/to/sentinel with your clone path
cp contrib/host-notifier/com.example.sentinel-notifier.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-notifier.plist
```

## Ask Claude (optional)

The PR panel can ask [Claude Code](https://claude.com/claude-code) for a second opinion next to your own: a **devil's advocate** review (structural, subagents allowed, default model) or a **quick pass** (typos, naming, leftovers, PR text vs change — no subagents, `sonnet` by default, a couple of minutes). Claude Code runs on the **host** (that's where your skills, repos and `gh` auth live), so `contrib/claude-bridge/` is a stdlib-only HTTP server that the container calls on `127.0.0.1:8301`. It runs `claude -p` read-only — no Edit/Write tools, Bash limited to inspection commands, GitHub-posting `gh` subcommands denied — and the verdict is stored in SQLite and rendered in the panel.

```bash
# 1. the bridge, kept alive by launchd (edit the plist: python3, claude binary, workspace dir)
cp contrib/claude-bridge/com.example.sentinel-claude-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-claude-bridge.plist
curl http://127.0.0.1:8301/healthz

# 2. the button
echo CLAUDE_BRIDGE_URL=http://host.docker.internal:8301 >> .env && docker compose up -d
```

`BRIDGE_PROMPT` / `BRIDGE_PROMPT_QUICK` (env, `{url}` placeholder) replace the default prompts of each mode — point them at your own skills, e.g. `/my-review-skill {url}`. `BRIDGE_MODEL` / `BRIDGE_MODEL_QUICK` pick the model per mode (empty = CLI default; quick defaults to `sonnet`). `BRIDGE_CWD` is the directory Claude starts in (its `CLAUDE.md` and skills apply).

## Architecture

Flask 3 + APScheduler (in-process, hence the single gunicorn worker) + SQLite (WAL) + HTMX. A watcher is one class with a `fetch()` returning the current state; `engine.py` diffs it against the stored state and drives the ack/snooze state machine. Adding a watcher = one file + one registry line + one config block — see [CLAUDE.md](CLAUDE.md) for the full tour.

## License

[MIT](LICENSE)
