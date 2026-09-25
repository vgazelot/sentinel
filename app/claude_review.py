"""Ask Claude Code for a PR review through the host-side bridge (contrib/claude-bridge).

The run takes minutes: it happens in a background thread, the result lands in the
`reviews` table and the panel polls it.
"""
import logging
import threading

import markdown
import requests

import config
import db

log = logging.getLogger("sentinel.review")
TIMEOUT = 900


def enabled() -> bool:
    return bool(config.CLAUDE_BRIDGE_URL)


def start(item_id: int, url: str) -> bool:
    """Returns False if a review is already running for this item."""
    current = db.get_review(item_id)
    if current and current["status"] == "running":
        return False
    db.start_review(item_id)
    threading.Thread(target=_run, args=(item_id, url), daemon=True).start()
    return True


def _run(item_id: int, url: str):
    try:
        r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/review", json={"url": url}, timeout=TIMEOUT)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
        db.finish_review(item_id, output=data.get("output") or "(empty answer)")
        log.info("claude review done for item %s in %ss", item_id, data.get("duration"))
    except Exception as e:  # noqa: BLE001
        log.warning("claude review failed for item %s: %s", item_id, e)
        db.finish_review(item_id, error=str(e))


def render(text: str) -> str:
    return markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
