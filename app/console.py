"""Claude Code consoles: live sessions the bridge runs on the host (tmux), shown in the page's window.

Sentinel names a session and writes its first prompt; the bridge starts it and hands out a ticket — once,
for a minute — the page attaches with over a WebSocket. Closing the window detaches: the session goes on,
a later click on the same alert reopens it.
"""
import re
import time
from urllib.parse import urlparse

import requests
from flask import Blueprint, abort, jsonify

import agents
import config

bp = Blueprint("console", __name__)
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_alive: dict = {"at": 0.0, "rows": []}


def enabled() -> bool:
    return agents.enabled()


def _ws_base() -> str:
    return config.CLAUDE_CONSOLE_URL or f"ws://localhost:{urlparse(config.CLAUDE_BRIDGE_URL).port or 8301}"


def _bridge(path: str, payload: dict) -> dict:
    r = requests.post(f"{config.CLAUDE_BRIDGE_URL}{path}", json=payload, headers=agents._auth(), timeout=20)
    data = r.json() if r.content else {}
    if r.status_code == 404 and str(data.get("error", "")).startswith("no console"):
        abort(404)
    if r.status_code == 404:
        raise RuntimeError("this bridge is older than Sentinel — restart it (launchctl bootout, then bootstrap)")
    if r.status_code >= 400:
        raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
    return data


def start(name: str, prompt: str = "", story: str = "", second: dict | None = None) -> dict:
    """Start the session unless it runs (no prompt = reopen only), linked to `story` if given: the bridge's answer, a
    ticket in it. `second` ({ssh_hosts, curl_hosts}): an unattended job's console, read-only (second.py)."""
    data = _bridge("/console", {"name": name, "prompt": prompt, **({"story": story} if story else {}),
                                **({"second": second} if second is not None else {})})
    _alive["at"] = 0.0
    return data


def fix_prepare(name: str, fix: dict) -> dict:
    """A worktree of the PR's branch at `fix["head"]` for the console `name` (a blobless clone, fetched first: a minute
    for a large repo). A branch that moved on is a ValueError."""
    r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/second/fix", json={"name": name, **fix}, headers=agents._auth(), timeout=900)
    return _fix_answer(r)


def fix_state(name: str) -> dict:
    return _fix_answer(requests.get(f"{config.CLAUDE_BRIDGE_URL}/second/fix", params={"name": name}, headers=agents._auth(), timeout=60))


def fix_push(name: str, expect: str, force: bool) -> dict:
    r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/second/fix/push", json={"name": name, "expect": expect, "force": force},
                      headers=agents._auth(), timeout=240)
    return _fix_answer(r)


def fix_remove(name: str) -> bool:
    r = requests.post(f"{config.CLAUDE_BRIDGE_URL}/second/fix/remove", json={"name": name}, headers=agents._auth(), timeout=60)
    return bool(_fix_answer(r).get("removed"))


def _fix_answer(r: requests.Response) -> dict:
    """409: the bridge refused (a branch moved on, nothing to push, a rebase without force) — ValueError; any other
    failure is a RuntimeError, a worktree it does not know a LookupError."""
    data = r.json() if r.content else {}
    if r.status_code == 409:
        raise ValueError(data.get("error") or "refused by the bridge")
    if r.status_code == 404:
        raise LookupError(data.get("error") or "this bridge has no fix worktrees — restart it")
    if r.status_code >= 400:
        raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
    return data


def end(name: str):
    _bridge("/console/end", {"name": name})
    _alive["at"] = 0.0


def stop(session_id: str):
    """A session in another terminal stops there: its terminal stays, its transcript too."""
    _bridge("/sessions/end", {"id": session_id})


def last_answer(session_id: str) -> str:
    """What the session wrote last, after its last tool call ('' while a prompt waits for its answer)."""
    r = requests.get(f"{config.CLAUDE_BRIDGE_URL}/sessions/answer", params={"id": session_id}, headers=agents._auth(), timeout=10)
    r.raise_for_status()
    return str(r.json().get("answer") or "")


def answer(name: str, title: str, prompt: str = ""):
    """Start the session unless it runs, and give the page its way in."""
    if not enabled():
        return jsonify({"error": "CLAUDE_BRIDGE_URL not configured"}), 503
    try:
        data = start(name, prompt)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": f"the Claude bridge: {e}"}), 502
    return jsonify({"name": name, "title": title, "ws": f"{_ws_base()}/console/ws?ticket={data['ticket']}"})


def sessions() -> list[dict]:
    """Consoles still running ({name, created, attached}): read at most every 5 s, none when the bridge is away."""
    if not enabled():
        return []
    if time.monotonic() - _alive["at"] > 5:
        try:
            r = requests.get(f"{config.CLAUDE_BRIDGE_URL}/console", headers=agents._auth(), timeout=2)
            rows = r.json().get("consoles", []) if r.ok else []
        except (requests.RequestException, ValueError, AttributeError):
            rows = []
        _alive.update(at=time.monotonic(), rows=rows)
    return _alive["rows"]


def alive() -> set[str]:
    return {c.get("name") for c in sessions()}


@bp.post("/api/console/<name>/attach")
def api_attach(name: str):
    if not NAME.fullmatch(name):
        abort(404)
    return answer(name, name)


@bp.post("/api/console/<name>/end")
def api_end(name: str):
    if not NAME.fullmatch(name):
        abort(404)
    try:
        end(name)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": f"the Claude bridge: {e}"}), 502
    return jsonify({"ended": name})
