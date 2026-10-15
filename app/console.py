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
_alive: dict = {"at": 0.0, "names": set()}


def enabled() -> bool:
    return agents.enabled()


def _ws_base() -> str:
    return config.CLAUDE_CONSOLE_URL or f"ws://localhost:{urlparse(config.CLAUDE_BRIDGE_URL).port or 8301}"


def _bridge(path: str, payload: dict) -> dict:
    r = requests.post(f"{config.CLAUDE_BRIDGE_URL}{path}", json=payload, headers=agents._auth(), timeout=20)
    data = r.json() if r.content else {}
    if r.status_code == 404:
        abort(404)
    if r.status_code >= 400:
        raise RuntimeError(data.get("error") or f"bridge HTTP {r.status_code}")
    return data


def answer(name: str, title: str, prompt: str = ""):
    """Start the session unless it runs (no prompt = reopen only), and give the page its way in."""
    if not enabled():
        return jsonify({"error": "CLAUDE_BRIDGE_URL not configured"}), 503
    try:
        data = _bridge("/console", {"name": name, "prompt": prompt})
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": f"the Claude bridge: {e}"}), 502
    _alive["at"] = 0.0
    return jsonify({"name": name, "title": title, "ws": f"{_ws_base()}/console/ws?ticket={data['ticket']}"})


def alive() -> set[str]:
    """Sessions still running, for the buttons: read at most every 5 s, none when the bridge is away."""
    if not enabled():
        return set()
    if time.monotonic() - _alive["at"] > 5:
        try:
            r = requests.get(f"{config.CLAUDE_BRIDGE_URL}/console", headers=agents._auth(), timeout=2)
            names = {c["name"] for c in r.json().get("consoles", [])} if r.ok else set()
        except (requests.RequestException, ValueError, KeyError, TypeError):
            names = set()
        _alive.update(at=time.monotonic(), names=names)
    return _alive["names"]


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
        _bridge("/console/end", {"name": name})
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": f"the Claude bridge: {e}"}), 502
    _alive["at"] = 0.0
    return jsonify({"ended": name})
