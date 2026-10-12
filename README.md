# Sentinel

> A small self-hosted officer of the watch for engineers. It tells you what waits on **you**: review requests, your own pull requests, the ones you approved until they have actually landed, and the provider incidents that touch your infra. Claude reads ahead so you do not have to.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white)

![Sentinel — the Now page](docs/screenshot.png)

## Why

A review inbox that pushes every request is mostly noise: on a team, most review requests reach you through a team and a teammate takes them within the hour; half of them are dependency bumps; and the PRs you *do* approve vanish from sight — automerge stalls on a dismissed approval, or the merge fails the `terraform apply` that runs afterwards and nobody notices. Sentinel sorts all of this into lanes, interrupts you only for what waits on you, and follows what you approved until it has landed.

## What it does

- **Triage, not a firehose.** Each signal gets a lane and an urgency. A review asked of you by name pushes right away; one asked of your team pushes only if nobody picked it up within a grace period, and disappears once someone approved; dependency bumps never push. More than three at once become a single notification.
- **Reviews.** Inline panel: numbered diff, CI checks, existing threads with reply, GitHub-style **line comments** (click a line, shift-click a range) sent as one review with **Approve**, **Comment** or **Request changes**.
- **Dependency bumps, one decision per bump.** Renovate and Dependabot PRs are grouped by `package@version` across repositories, sized from the update table (major / minor / patch), and **Approve the green ones** approves every PR of the bump with a passing CI in one click. With Claude enabled, each bump is read before you approve it: release notes, a deliberate version pin quietly removed, a failing plan that the bump explains.
- **Your pull requests, by what they wait for.** Conflicts, changes requested, red CI (grouped by failing job: ten PRs red on the same CI job are one problem), approved and green, open threads, nobody asked to review, no movement for days. A PR that starts waiting on you pushes once.
- **Follow-up after you approve.** Every PR you approved (bots by default) is followed until it has *landed*: merged, and every check that ran on the merge commit green — `terraform apply`, deploy, release. A dismissed approval, a PR stuck for days, a post-merge failure or a revert become items. A failing check that was already red on the commit before the merge is told apart (*merged into a red pipeline*), and with Claude enabled a fresh failure is investigated — *caused by this PR*, *unrelated*, or *unclear* — with the root cause from the logs and what to do.
- **Status pages.** Any [Atlassian Statuspage](https://www.atlassian.com/software/statuspage) (Scaleway, OVHcloud, GitHub, Cloudflare…) with component and keyword filters. Planned maintenance is listed, never pushed; a noisy page can be set to `notify: quiet`.
- **Health.** A source failing for half an hour (expired token, API change) becomes an item instead of a silent gap.
- **Cycle objectives** (optional). Your big rocks from a [Slite](https://slite.com) note joined with their [Shortcut](https://shortcut.com) epics, and a push when one goes quiet for too long.

Everything runs locally in one Docker container with a SQLite file — no account, no cloud, no telemetry. Notifications are native Web Push through your browser; an optional [Claude Code](https://claude.com/claude-code) mod puts the same inbox in your terminal's status line.

## Quick start

```bash
git clone https://github.com/vgazelot/sentinel.git && cd sentinel

# 1. Configuration: your GitHub login/orgs and the status pages you care about
cp config.example.yaml config.yaml

# 2. Secrets: a GitHub token (quick bootstrap: gh auth token)
cp .env.example .env

# 3. Web Push (VAPID) keys, pasted into .env
docker compose build
docker compose run --rm sentinel python gen_vapid.py

# 4. Run
docker compose up -d
```

Open **http://localhost:8300**, then **Settings → Subscribe this browser** (use `localhost`, not `127.0.0.1`: the service worker origin depends on it).

> [!WARNING]
> Sentinel has **no authentication**. It is designed to run on your own machine, bound to `127.0.0.1` (the default in `docker-compose.yml`). Never expose it to a network.

## When Sentinel interrupts you

| Signal | Lane | Pushed |
|---|---|---|
| Review asked of you by name | Reviews for you | right away |
| Review asked of a team you belong to | Team requests → Reviews for you | only if still unclaimed after `team_grace_minutes`; gone once somebody approved |
| Dependency bump (bot PR) | Dependency bumps | never — grouped by bump |
| Your PR starts waiting on you | Your pull requests | once per transition (conflicts, changes requested, red CI, approved and green, open threads) |
| A PR you approved: approval dismissed, stuck, broken after merge, reverted | After you approved | once |
| Provider incident on your components | Incidents | right away (`notify: quiet` and maintenance: never) |
| A source failing | Sentinel needs a hand | after `health_minutes` |

Every item can be marked **done** (silent until it changes), **hidden for good**, or **snoozed** (1 h, 4 h, tomorrow 9:00) — one by one or a whole group at once. An item that leaves its source (PR merged, incident resolved) resolves itself. The first pass of a new source records what is already there without pushing it.

## Configuration

`config.yaml` (mounted read-only — `docker compose restart` after editing). See [config.example.yaml](config.example.yaml) for every option.

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
        include_maintenances: true                 # listed, never pushed

followup:                         # the PRs you approved, until they have landed
  enabled: true
  authors: [renovate, dependabot] # bots whose PRs merge without their author watching; `all` = everything you approve
  stale_days: 3
  window_days: 7

triage:                           # defaults shown
  team_grace_minutes: 60
  health_minutes: 30
  stale_review_days: 3

agents:                           # Claude, see below; defaults shown
  precheck: true
  followup: true
  max_runs_per_day: 30
  concurrency: 2
```

Environment variables (`.env`):

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Reads PRs, reviews and checks. **Approve** needs a token allowed to review (classic PAT with `repo`, or fine-grained with Pull requests: write) |
| `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` | Web Push keys — `docker compose run --rm sentinel python gen_vapid.py` |
| `VAPID_CLAIM_EMAIL` | Contact address embedded in push claims |
| `TZ` | Timezone for display and "snooze until tomorrow 9:00" (default `UTC`) |
| `CLAUDE_BRIDGE_URL` | Optional — the host-side [Claude bridge](#claude-optional), e.g. `http://host.docker.internal:8301` |
| `SLITE_API_KEY` / `SHORTCUT_API_TOKEN` | Optional — the Cycle tab |
| `SENTINEL_PUSH_DISABLED` | `1` = never push (a second instance on a copy of the database) |

## Claude (optional)

Claude Code runs on the **host** — that is where your skills, repositories and `gh` login live — so `contrib/claude-bridge/` is a stdlib-only HTTP server the container calls on `127.0.0.1:8301`. It runs `claude -p` in one of four modes; the bridge owns prompts, models and tools, the app only sends PR URLs and a little context.

| Mode | When | What |
|---|---|---|
| `quick` | on demand, review panel | typos, naming, leftovers, PR text vs change (`sonnet`) |
| `deep` | on demand, review panel | devil's advocate review (subagents, default model) |
| `precheck` | automatic, each pending dependency bump | release notes, removed pins, failing checks the bump explains → `safe` / `check` / `risky` (`sonnet`) |
| `followup` | automatic, a fresh post-merge failure of a PR you approved | the failed run's logs, the history of the same workflow → `caused-by-this-pr` / `unrelated` / `unclear` and what to do |

A follow-up investigation answers with a push; a failure Claude traced to something else stops asking for you. Automatic runs are capped per day (`agents.max_runs_per_day`) and run at most `agents.concurrency` at a time.

**Read-only by construction.** Every run loads your *user* settings only (`--setting-sources user`): the allow rules of the project the bridge starts in would otherwise widen its lists — an allowed `tee` there wrote a file in a run meant to be read-only. No Edit/Write tools anywhere. The on-demand modes get inspection commands and `gh` minus the subcommands that write. The automatic modes read third-party text (release notes, CI logs) with nobody watching, so they get a fixed allowlist of read-only `gh` subcommands (`pr view/diff/checks`, `run view/list`, `release view`, `issue view`, `search code`) plus text filters — no `gh api`, no file tools, no WebFetch, no MCP servers.

```bash
# 1. the bridge, kept alive by launchd (edit the plist: python3, claude binary, workspace dir)
cp contrib/claude-bridge/com.example.sentinel-claude-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-claude-bridge.plist
curl http://127.0.0.1:8301/healthz

# 2. the app
echo CLAUDE_BRIDGE_URL=http://host.docker.internal:8301 >> .env && docker compose up -d
```

Bridge environment: `BRIDGE_LANG` (answer language, default English), `BRIDGE_PROMPT` / `BRIDGE_PROMPT_<MODE>` (replace a mode's prompt — placeholders `{url}`, `{urls}`, `{context}`, `{lang}`; point them at your own skills, e.g. `/my-review {url}`), `BRIDGE_MODEL` / `BRIDGE_MODEL_<MODE>`, `BRIDGE_CWD` (where Claude starts: its `CLAUDE.md` and skills apply). After editing the plist, `launchctl bootout` then `bootstrap` it — `kickstart -k` keeps the old environment.

## Claude Code status line (optional)

`contrib/claude-code-mod/` is a [Claude Code](https://claude.com/claude-code) mod that reads `GET /api/summary`: the status line shows what needs you (`● 3 for you · 2 bumps · landing 1 failed`), new notifications appear as toasts, and `/sentinel` opens the inbox in a pane — open, done, snooze, and a quick pass or devil's advocate review run right in your session. It only reads Sentinel's API: one source of truth, no second poller.

## Cycle tab (optional)

Hidden unless `SLITE_API_KEY` and `SHORTCUT_API_TOKEN` are in `.env` *and* `config.yaml` has a `cycle:` block. Objectives come from the Slite note, live progress from Shortcut, and the tab rolls over to the next cycle by itself: newest note under `parent_id`, dates read off its title, highest `<prefix>-<year>-C<n>` label, epics carrying that label whose name contains the big rock title. Each big rock gets a verdict — stalled, behind, on track — measured on the note's outputs, not on story progress (epics carry stories closed in earlier cycles). See the `cycle:` block of [config.example.yaml](config.example.yaml).

## Fallback notifier (macOS, optional)

Web Push only works while a subscribed browser runs. `contrib/host-notifier/` is a launchd agent that polls the API and relays notifications through [`terminal-notifier`](https://formulae.brew.sh/formula/terminal-notifier):

```bash
brew install terminal-notifier
# edit the plist first: replace /path/to/sentinel with your clone path
cp contrib/host-notifier/com.example.sentinel-notifier.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.sentinel-notifier.plist
```

## Tests

```bash
docker build -t sentinel-dev ./app
docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -w /app sentinel-dev python -m unittest discover -s /tests
```

## Architecture

Flask 3 + APScheduler (in-process, hence the single gunicorn worker) + SQLite (WAL) + HTMX, no build step. A watcher is one class whose `fetch()` returns the current state as items with a lane and an urgency; `engine.py` diffs it against the stored state and owns the notification policy (urgency, grace period, bursts, ack/snooze). My PRs, Follow-up and Cycle compute their own state and feed the same engine. See [CLAUDE.md](CLAUDE.md) for the full tour.

## License

[MIT](LICENSE)
