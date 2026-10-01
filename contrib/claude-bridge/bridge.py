#!/usr/bin/env python3
"""Host-side bridge: lets the Sentinel container ask Claude Code for a PR review.

Claude Code (skills, repos, gh auth) lives on the host, not in the container, so this
tiny stdlib-only HTTP server runs `claude -p` on demand. Bound to 127.0.0.1 only —
the container reaches it through host.docker.internal.

    POST /review  {"url": "https://github.com/org/repo/pull/123", "mode": "deep"|"quick"}
    → {"output": "<markdown>", "duration": 142.3}

Two modes: `deep` (devil's advocate, subagents allowed, default model) and `quick`
(form/typos pass, no subagents, a faster model) — prompt and model per mode via env.

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
# {url} is replaced by the PR URL. Point them at your own skills, e.g. "/avocat {url} ..."
PROMPTS = {
    "deep": os.environ.get(
        "BRIDGE_PROMPT",
        "Review this pull request as a senior engineer who discovers it without context. "
        "Read-only: no edits, no GitHub comments. Answer with a one-sentence verdict, then "
        "numbered findings (most structural first, each with file:line evidence), then a firm "
        "recommendation. PR: {url}",
    ),
    "quick": os.environ.get(
        "BRIDGE_PROMPT_QUICK",
        "Quick pass on this pull request: form, not design. Read the diff once and report only "
        "what you actually see: typos and wording in code, comments, docs, commit messages and the "
        "PR description; naming or style inconsistent with the surrounding code; leftover debug, "
        "TODO or dead code; copy-paste slips; mismatch between the PR title/description and the "
        "change. Do not dig into architecture, performance or blast radius. Read-only: no edits, "
        "no GitHub comments. Output rules: no preamble, no narration of what you did or read, never "
        "list what is fine. First line: `Verdict: clean` or `Verdict: tidy-up needed`. Then one "
        "bullet per real finding, `file:line — issue`, ten words max each. Clean = the verdict "
        "line alone, nothing else. PR: {url}",
    ),
}
# empty = the CLI default model; quick defaults to a faster one
MODELS = {"deep": os.environ.get("BRIDGE_MODEL", ""), "quick": os.environ.get("BRIDGE_MODEL_QUICK", "sonnet")}
TOOLS = {"deep": "Bash,Read,Grep,Glob,WebFetch,Agent", "quick": "Bash,Read,Grep,Glob,WebFetch"}

ALLOWED_TOOLS = [
    "Bash(gh:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git fetch:*)",
    "Bash(git ls-files:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(cat:*)",
    "Bash(ls:*)", "Bash(head:*)", "Bash(sed -n:*)", "Bash(wc:*)", "WebFetch", "Agent",
]
DENIED_TOOLS = [
    "Bash(gh pr review:*)", "Bash(gh pr comment:*)", "Bash(gh pr merge:*)", "Bash(gh pr close:*)",
    "Bash(gh pr edit:*)", "Bash(gh issue comment:*)", "Bash(gh api -X:*)", "Bash(gh api --method:*)",
]


def run_review(url: str, mode: str) -> dict:
    cmd = [
        CLAUDE, "-p", PROMPTS[mode].format(url=url),
        "--output-format", "text",
        # no MCP servers: the review only needs gh/git, and each configured server costs startup time
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", TOOLS[mode],
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DENIED_TOOLS,
    ]
    if MODELS[mode]:
        cmd += ["--model", MODELS[mode]]
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
        mode = str(data.get("mode") or "deep")
        if not url.startswith("https://github.com/"):
            return self._json(400, {"error": "url must be a github.com PR URL"})
        if mode not in PROMPTS:
            return self._json(400, {"error": f"mode must be one of {sorted(PROMPTS)}"})
        self.log_message("review %s %s", mode, url)
        try:
            self._json(200, run_review(url, mode))
        except subprocess.TimeoutExpired:
            self._json(504, {"error": f"claude timed out after {TIMEOUT}s"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"claude bridge on http://{HOST}:{PORT} (claude={CLAUDE}, cwd={CWD})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
