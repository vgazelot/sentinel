#!/usr/bin/env python3
"""Host-side bridge: lets the Sentinel container ask Claude Code for a PR review.

Claude Code (skills, repos, gh auth) lives on the host, not in the container, so this
tiny stdlib-only HTTP server runs `claude -p` on demand. Bound to 127.0.0.1 only —
the container reaches it through host.docker.internal.

    POST /review  {"url": "https://github.com/org/repo/pull/123"}
    → {"output": "<markdown>", "duration": 142.3}

Read-only by construction: no Edit/Write tools, Bash limited to inspection commands,
and the gh subcommands that post to GitHub are denied.
"""
import json
import os
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8301"))
CWD = os.path.expanduser(os.environ.get("BRIDGE_CWD", "~"))
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "900"))
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
# {url} is replaced by the PR URL. Point it at your own review skill, e.g. "/avocat {url} ..."
PROMPT = os.environ.get(
    "BRIDGE_PROMPT",
    "Review this pull request as a senior engineer who discovers it without context. "
    "Read-only: no edits, no GitHub comments. Answer with a one-sentence verdict, then "
    "numbered findings (most structural first, each with file:line evidence), then a firm "
    "recommendation. PR: {url}",
)

ALLOWED_TOOLS = [
    "Bash(gh:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git fetch:*)",
    "Bash(git ls-files:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(cat:*)",
    "Bash(ls:*)", "Bash(head:*)", "Bash(sed -n:*)", "Bash(wc:*)", "WebFetch", "Agent",
]
DENIED_TOOLS = [
    "Bash(gh pr review:*)", "Bash(gh pr comment:*)", "Bash(gh pr merge:*)", "Bash(gh pr close:*)",
    "Bash(gh pr edit:*)", "Bash(gh issue comment:*)", "Bash(gh api -X:*)", "Bash(gh api --method:*)",
]


def run_review(url: str) -> dict:
    cmd = [
        CLAUDE, "-p", PROMPT.format(url=url),
        "--output-format", "text",
        "--tools", "Bash,Read,Grep,Glob,WebFetch,Agent",
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DENIED_TOOLS,
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True, timeout=TIMEOUT)
    duration = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-2000:]}")
    return {"output": proc.stdout.strip(), "duration": duration}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "claude": CLAUDE, "cwd": CWD})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/review":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        url = str(data.get("url") or "")
        if not url.startswith("https://github.com/"):
            return self._json(400, {"error": "url must be a github.com PR URL"})
        self.log_message("review %s", url)
        try:
            self._json(200, run_review(url))
        except subprocess.TimeoutExpired:
            self._json(504, {"error": f"claude timed out after {TIMEOUT}s"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"claude bridge on http://{HOST}:{PORT} (claude={CLAUDE}, cwd={CWD})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
