"""YAML config + environment variables loading."""
import os

import yaml

CONFIG_PATH = os.environ.get("SENTINEL_CONFIG", "/app/config.yaml")
DB_PATH = os.environ.get("SENTINEL_DB", "/data/sentinel.db")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "sentinel@localhost")
TZ = os.environ.get("TZ", "UTC")
BASE_URL = os.environ.get("SENTINEL_BASE_URL", "http://localhost:8300/")
# host-side claude-bridge (contrib/claude-bridge); empty = no Claude features at all
CLAUDE_BRIDGE_URL = os.environ.get("CLAUDE_BRIDGE_URL", "").rstrip("/")
# shared with the bridge (BRIDGE_TOKEN there): only this app may make it run Claude
CLAUDE_BRIDGE_TOKEN = os.environ.get("CLAUDE_BRIDGE_TOKEN", "")
# the console window attaches to the bridge from the browser: the host side of CLAUDE_BRIDGE_URL, by default
CLAUDE_CONSOLE_URL = os.environ.get("CLAUDE_CONSOLE_URL", "").rstrip("/")
# Cycle tab: Big Rocks note on Slite + epic progress on Shortcut; either missing = tab hidden
SLITE_API_KEY = os.environ.get("SLITE_API_KEY", "")
SHORTCUT_API_TOKEN = os.environ.get("SHORTCUT_API_TOKEN", "")
# a second instance on a copy of the database (UI work) must not ping your phone or post anything
PUSH_DISABLED = os.environ.get("SENTINEL_PUSH_DISABLED", "") == "1"

TRIAGE_DEFAULTS = {
    "team_grace_minutes": 60,   # team review requests: pushed only if still unclaimed after this
    "health_minutes": 30,       # a polling job failing for this long becomes an item
    "stale_review_days": 3,     # your PR waiting for a first review this long → "nudge someone"
}
AGENT_DEFAULTS = {
    "concurrency": 2,           # bridge runs in parallel
    "max_runs_per_day": 30,     # automatic runs only (prechecks, follow-ups); on-demand ones are never capped
    "precheck": True,           # Claude reads each dependency bump before you approve it
    "followup": True,           # Claude investigates when a PR you approved breaks something after merge
    "brief": True,              # Claude reads each review asked of you: what it changes, how carefully to read it
    "whyred": True,             # Claude reads the failed job logs when one of your PRs goes red
    "asks": True,               # Claude turns the reviewers' comments on your PR into a to-do
}


def load() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def triage() -> dict:
    return {**TRIAGE_DEFAULTS, **(load().get("triage") or {})}


def agents() -> dict:
    return {**AGENT_DEFAULTS, **(load().get("agents") or {})}


def github() -> dict:
    """login/orgs live under watchers.github_reviews; every GitHub feature reads them from there."""
    gh = load().get("watchers", {}).get("github_reviews", {}) or {}
    return {"login": gh.get("login", ""), "orgs": gh.get("orgs", []) or [],
            "interval_seconds": gh.get("interval_seconds", 180)}
