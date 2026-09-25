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
# host-side claude-bridge (contrib/claude-bridge); empty = "Ask Claude" button hidden
CLAUDE_BRIDGE_URL = os.environ.get("CLAUDE_BRIDGE_URL", "").rstrip("/")


def load() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}
