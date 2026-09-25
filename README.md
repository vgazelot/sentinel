# Sentinel 🛰️

> A tiny self-hosted watchtower for things that need your attention — GitHub PRs waiting for your review and status page incidents — with native Web Push notifications and a built-in ack/snooze workflow.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white)

![Sentinel dashboard](docs/screenshot.png)

## What it does

- 👀 **GitHub review requests** — polls PRs where your review is requested (directly or via a team) across your orgs. Inline review panel on the dashboard: diff, CI checks, one-click **Approve** or **Comment**, and an optional **Ask Claude** button for a second opinion.
- 🚦 **Status pages** — watches any [Atlassian Statuspage](https://www.atlassian.com/software/statuspage) API (Scaleway, OVHcloud, GitHub, Cloudflare, …) with component/keyword filters, so you only hear about incidents that affect *your* infra.
- 🔔 **Web Push notifications** — native OS notifications through your browser (Chrome/Firefox), no third-party service.
- ✅ **Ack / snooze workflow** — every item nags you exactly once, then stays silent until something actually changes.
- 📝 **Minimal todo list** — replaces the paper notepad next to your keyboard.

Everything runs locally in a single Docker container with a SQLite file — no accounts, no cloud, no telemetry.

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

## Fallback notifier (macOS, optional)

Web Push only works while a subscribed browser is running. `contrib/host-notifier/` provides a launchd agent that polls the API and relays pending notifications through [`terminal-notifier`](https://formulae.brew.sh/formula/terminal-notifier):

```bash
brew install terminal-notifier
# edit the plist first: replace /path/to/sentinel with your clone path
cp contrib/host-notifier/com.example.sentinel-notifier.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-notifier.plist
```

## Ask Claude (optional)

The PR panel can ask [Claude Code](https://claude.com/claude-code) for a devil's advocate review, next to your own. Claude Code runs on the **host** (that's where your skills, repos and `gh` auth live), so `contrib/claude-bridge/` is a stdlib-only HTTP server that the container calls on `127.0.0.1:8301`. It runs `claude -p` read-only — no Edit/Write tools, Bash limited to inspection commands, GitHub-posting `gh` subcommands denied — and the verdict is stored in SQLite and rendered in the panel.

```bash
# 1. the bridge, kept alive by launchd (edit the plist: python3, claude binary, workspace dir)
cp contrib/claude-bridge/com.example.sentinel-claude-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-claude-bridge.plist
curl http://127.0.0.1:8301/healthz

# 2. the button
echo CLAUDE_BRIDGE_URL=http://host.docker.internal:8301 >> .env && docker compose up -d
```

`BRIDGE_PROMPT` (env, `{url}` placeholder) replaces the default review prompt — point it at your own skill, e.g. `/my-review-skill {url}`. `BRIDGE_CWD` is the directory Claude starts in (its `CLAUDE.md` and skills apply).

## Architecture

Flask 3 + APScheduler (in-process, hence the single gunicorn worker) + SQLite (WAL) + HTMX. A watcher is one class with a `fetch()` returning the current state; `engine.py` diffs it against the stored state and drives the ack/snooze state machine. Adding a watcher = one file + one registry line + one config block — see [CLAUDE.md](CLAUDE.md) for the full tour.

## License

[MIT](LICENSE)
