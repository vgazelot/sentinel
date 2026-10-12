#!/usr/bin/env python3
"""Host-side bridge: lets the Sentinel container run Claude Code on a PR.

Claude Code (skills, repos, gh auth) lives on the host, not in the container, so this
tiny stdlib-only HTTP server runs `claude -p` on demand. Bound to 127.0.0.1 only —
the container reaches it through host.docker.internal.

    POST /run  {"mode": "...", "url": "https://github.com/org/repo/pull/123",
                "urls": [...more PR URLs...], "context": {"key": "value or list"}}
    → {"output": "<markdown>", "duration": 142.3}

Modes — prompt, model and tools are owned here, the app only sends URLs and context:
  deep      on demand: devil's advocate review (subagents, default model)
  quick     on demand: form pass (typos, naming, leftovers), faster model
  precheck  automatic: a dependency bump read before approval (release notes, removed pins)
  followup  automatic: a PR approved then merged broke a post-merge check — cause and next step

Every run loads the user settings only (`--setting-sources user`): the allow rules of
BRIDGE_CWD's project would otherwise widen the lists below. The automatic modes read
third-party text (release notes, CI logs) with nobody watching, so they get the strictest
toolset: a fixed list of read-only gh subcommands, no file tools, no network beyond gh.
`POST /review {"url", "mode"}` is kept for older Sentinel versions.
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
LANG = os.environ.get("BRIDGE_LANG", "English")
MAX_CONTEXT = 4000

# {url} = the PR URL, {urls} = all PR URLs one per line, {context} = the app's context as text,
# {lang} = BRIDGE_LANG. Override any of them per mode with BRIDGE_PROMPT_<MODE> (deep: BRIDGE_PROMPT).
DEFAULT_PROMPTS = {
    "deep": "Review this pull request as a senior engineer who discovers it without context. "
            "Read-only: no edits, no GitHub comments. Answer in {lang} with a one-sentence verdict, then "
            "numbered findings (most structural first, each with file:line evidence), then a firm "
            "recommendation. PR: {url}",
    "quick": "Quick pass on this pull request: form, not design. Read the diff once and report only "
             "what you actually see: typos and wording in code, comments, docs, commit messages and the "
             "PR description; naming or style inconsistent with the surrounding code; leftover debug, "
             "TODO or dead code; copy-paste slips; mismatch between the PR title/description and the "
             "change. Do not dig into architecture, performance or blast radius. Read-only: no edits, "
             "no GitHub comments. Answer in {lang}. No preamble, no narration, never list what is fine. "
             "First line: `Verdict: clean` or `Verdict: tidy-up` (keep these English tokens). Then one "
             "bullet per real finding, `file:line — issue`, ten words max each. PR: {url}",
    "precheck": "A bot opened these dependency-update pull requests (the same bump, one per repository) "
                "and a human is about to approve them. Check them before that happens.\n{urls}\n{context}\n"
                "With gh (read-only: gh pr view --json body,files, gh pr diff, gh pr checks, gh release view, "
                "gh issue view, gh search code to see how a repository uses the dependency), look for: breaking or behaviour "
                "changes in the release notes that matter for how these repositories use the dependency; a "
                "version constraint loosened or a deliberate pin removed (read the comment next to it and say "
                "whether its reason still holds); a failing check the bump explains; anything a careful "
                "reviewer would stop on. Never post anything. Answer in {lang}. First line exactly "
                "`Verdict: safe`, `Verdict: check` or `Verdict: risky` (keep these English tokens). Then at "
                "most five bullets, most important first, each naming the repository and the evidence "
                "(file:line or release note). No preamble.",
    "followup": "A pull request I approved has been merged, and a check that ran after the merge failed. "
                "Find out whether this PR caused it.\nPR: {url}\n{context}\n"
                "With gh (read-only): gh pr view / gh pr diff for the change; gh run view <run-id> "
                "--log-failed for the failure; gh run list --workflow <name> --branch <default branch> to see "
                "whether it already failed before this merge and whether a later run passed. Never post, "
                "re-run or change anything. Answer in {lang}, eight lines max, no preamble. First line exactly "
                "`Verdict: caused-by-this-pr`, `Verdict: unrelated` or `Verdict: unclear` (keep these English "
                "tokens). Then: the root cause seen in the logs (quote the key error line); the current state "
                "(still failing, or fixed by a later run — with its link); what to do (revert, fix, re-run, "
                "ignore) and who should.",
}
PROMPTS = {
    mode: os.environ.get("BRIDGE_PROMPT" if mode == "deep" else f"BRIDGE_PROMPT_{mode.upper()}", default)
    for mode, default in DEFAULT_PROMPTS.items()
}
# empty = the CLI default model
MODELS = {
    "deep": os.environ.get("BRIDGE_MODEL", ""),
    "quick": os.environ.get("BRIDGE_MODEL_QUICK", "sonnet"),
    "precheck": os.environ.get("BRIDGE_MODEL_PRECHECK", "sonnet"),
    "followup": os.environ.get("BRIDGE_MODEL_FOLLOWUP", ""),
}

# on-demand reviews: the toolset your own skills expect (e.g. a /review skill reading the repo)
REVIEW_TOOLS = "Bash,Read,Grep,Glob,WebFetch"
REVIEW_ALLOWED = [
    "Bash(gh:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git fetch:*)",
    "Bash(git ls-files:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(cat:*)",
    "Bash(ls:*)", "Bash(head:*)", "Bash(sed -n:*)", "Bash(wc:*)", "WebFetch", "Agent",
]
# gh writes that a prefix rule can name; `gh api` with -f/-F defaults to POST and cannot be told
# apart by prefix, which is why the automatic modes do not get `gh api` at all
REVIEW_DENIED = [
    "Bash(gh pr review:*)", "Bash(gh pr comment:*)", "Bash(gh pr merge:*)", "Bash(gh pr close:*)",
    "Bash(gh pr edit:*)", "Bash(gh pr create:*)", "Bash(gh pr ready:*)", "Bash(gh pr reopen:*)",
    "Bash(gh issue comment:*)", "Bash(gh issue create:*)", "Bash(gh issue edit:*)", "Bash(gh issue close:*)",
    "Bash(gh api -X:*)", "Bash(gh api --method:*)", "Bash(gh workflow run:*)", "Bash(gh run rerun:*)",
    "Bash(gh run cancel:*)", "Bash(gh run delete:*)", "Bash(gh release create:*)", "Bash(gh release delete:*)",
    "Bash(gh release edit:*)", "Bash(gh repo:*)", "Bash(gh secret:*)", "Bash(gh variable:*)",
    "Bash(gh label:*)", "Bash(gh gist:*)", "Bash(gh cache delete:*)",
]
AUTO_ALLOWED = [
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh run view:*)",
    "Bash(gh run list:*)", "Bash(gh release view:*)", "Bash(gh issue view:*)", "Bash(gh search code:*)",
    "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(jq:*)", "Bash(wc:*)",
]
AUTO_DENIED = ["Bash(gh api:*)", "Bash(curl:*)", "Bash(wget:*)"]
TOOLSETS = {
    "deep": (REVIEW_TOOLS + ",Agent", REVIEW_ALLOWED, REVIEW_DENIED),
    "quick": (REVIEW_TOOLS, REVIEW_ALLOWED, REVIEW_DENIED),
    "precheck": ("Bash", AUTO_ALLOWED, AUTO_DENIED),
    "followup": ("Bash", AUTO_ALLOWED, AUTO_DENIED),
}


def _context_text(context: dict) -> str:
    lines = []
    for key, value in (context or {}).items():
        label = str(key).replace("_", " ")
        if isinstance(value, list):
            lines.append(f"- {label}:")
            lines += [f"  - {str(v)[:400]}" for v in value[:12]]
        else:
            lines.append(f"- {label}: {str(value)[:400]}")
    text = "\n".join(lines)
    return ("Context:\n" + text)[:MAX_CONTEXT] if text else ""


def run(mode: str, url: str, urls: list[str], context: dict) -> dict:
    prompt = PROMPTS[mode].format(url=url, urls="\n".join(urls or [url]), context=_context_text(context), lang=LANG)
    tools, allowed, denied = TOOLSETS[mode]
    cmd = [
        CLAUDE, "-p", prompt,
        "--output-format", "text",
        # user settings only: allow rules from the project and local settings of BRIDGE_CWD would
        # otherwise add to the lists below (an allowed `tee` wrote a file in a run meant read-only)
        "--setting-sources", "user",
        # no MCP servers: gh is enough, and each configured server costs startup time
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", tools,
        "--allowedTools", *allowed,
        "--disallowedTools", *denied,
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
            return self._json(200, {"ok": True, "claude": CLAUDE, "cwd": CWD, "modes": sorted(PROMPTS), "lang": LANG})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/run", "/review"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        mode = str(data.get("mode") or "deep")
        url = str(data.get("url") or "")
        urls = [str(u) for u in (data.get("urls") or [])][:8]
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        if mode not in PROMPTS:
            return self._json(400, {"error": f"mode must be one of {sorted(PROMPTS)}"})
        if not all(u.startswith("https://github.com/") for u in [url, *urls]):
            return self._json(400, {"error": "urls must be github.com PR URLs"})
        self.log_message("%s %s%s", mode, url, f" (+{len(urls) - 1})" if len(urls) > 1 else "")
        try:
            self._json(200, run(mode, url, urls, context))
        except subprocess.TimeoutExpired:
            self._json(504, {"error": f"claude timed out after {TIMEOUT}s"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"claude bridge on http://{HOST}:{PORT} (claude={CLAUDE}, cwd={CWD}, lang={LANG})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
