"""Ask Claude Code for a PR review through the host-side bridge (contrib/claude-bridge).

The run takes minutes: it happens in a background thread, the result lands in the
`reviews` table and the panel polls it.
"""
import logging
import threading

import requests

import config
import db
import md

log = logging.getLogger("sentinel.review")
TIMEOUT = 900
# key = `mode` sent to the bridge (which owns prompt + model per mode); the rest is UI copy
MODES = {
    "deep": {
        "button": "Ask Claude — devil's advocate",
        "title": "Structural review: durability, modeling, blast radius (read-only, nothing is posted on GitHub)",
        "eta": "typically 5–10 min, up to 15",
        "verdict": "Claude's verdict",
    },
    "quick": {
        "button": "Ask Claude — quick pass",
        "title": "Form only: typos, naming, leftovers, PR text vs change (read-only, nothing is posted on GitHub)",
        "eta": "typically 1–3 min",
        "verdict": "Claude's quick pass",
    },
}


def enabled() -> bool:
    return bool(config.CLAUDE_BRIDGE_URL)


def start(item_id: int, url: str, mode: str) -> bool:
    """Returns False if this mode is already running for this item."""
    current = db.get_reviews(item_id).get(mode)
    if current and current["status"] == "running":
        return False
    db.start_review(item_id, mode)
    threading.Thread(target=_run, args=(item_id, url, mode), daemon=True).start()
    return True


def _run(item_id: int, url: str, mode: str):
    try:
        r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/review", json={"url": url, "mode": mode}, timeout=TIMEOUT)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
        db.finish_review(item_id, mode, output=data.get("output") or "(empty answer)")
        log.info("claude %s review done for item %s in %ss", mode, item_id, data.get("duration"))
    except Exception as e:  # noqa: BLE001
        log.warning("claude %s review failed for item %s: %s", mode, item_id, e)
        db.finish_review(item_id, mode, error=str(e))


def render(text: str) -> str:
    """Claude points at `path:line`: make those refs jump to the diff line in the panel."""
    return md.link_file_refs(md.render(text))
