"""A fix's worktree on the bridge, against real repositories: needs git, so it runs on the host (the image has none):
    python3 -m unittest tests/test_bridge_fix.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = next(p for p in ("/contrib/claude-bridge/bridge.py", os.path.join(HERE, "..", "contrib", "claude-bridge", "bridge.py"))
              if os.path.exists(p))
WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.net", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.net"}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **WHO}).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is on the host, not in the image")
class FixWorktreeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.origin = os.path.join(self.tmp, "acme", "infra.git")
        git("init", "-q", "--bare", "-b", "main", self.origin, cwd=self.tmp)
        self.dev = os.path.join(self.tmp, "dev")
        git("clone", "-q", self.origin, self.dev, cwd=self.tmp)
        self.write("main.tf", "a = 1\n")
        git("add", ".", cwd=self.dev)
        git("commit", "-qm", "init", cwd=self.dev)
        git("push", "-q", "origin", "main", cwd=self.dev)
        git("checkout", "-qb", "feature/sc-1-x", cwd=self.dev)
        self.write("feature.tf", "b = 2\n")
        git("add", ".", cwd=self.dev)
        git("commit", "-qm", "feat: b", cwd=self.dev)
        git("push", "-q", "origin", "feature/sc-1-x", cwd=self.dev)
        self.head = git("rev-parse", "HEAD", cwd=self.dev)
        self.claude_json = os.path.join(self.tmp, ".claude.json")
        with open(self.claude_json, "w") as f:
            json.dump({"numStartups": 7, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}, f)
        # never your own ~/.claude.json
        with mock.patch.dict("os.environ", {"BRIDGE_SECOND_ROOT": os.path.join(self.tmp, "root"), "CLAUDE_CONFIG_DIR": self.tmp,
                                            "BRIDGE_SECOND_GIT_URL": os.path.join(self.tmp, "{repo}.git")}):
            spec = importlib.util.spec_from_file_location("bridge_fix", BRIDGE)
            self.b = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.b)

    def write(self, name, text, where=None):
        with open(os.path.join(where or self.dev, name), "w") as f:
            f.write(text)

    def prepare(self, name="second-infra-1", head=None):
        return self.b.prepare_fix(name, {"repo": "acme/infra", "branch": "feature/sc-1-x", "head": head or self.head, "base": "main"})

    def remote(self):
        return git("rev-parse", "refs/heads/feature/sc-1-x", cwd=self.origin)

    def test_a_fix_starts_from_the_head_sentinel_saw_and_pushes_on_top(self):
        with self.assertRaisesRegex(ValueError, "moved on"):
            self.prepare(head="0" * 40)
        path = self.prepare()["path"]
        self.assertTrue(path.startswith(os.path.join(self.tmp, "root", "work")))
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.head)
        self.assertEqual(self.b.fix_state("second-infra-1")["moved"], False)
        with self.assertRaisesRegex(ValueError, "no commit"):
            self.b.push_fix("second-infra-1", self.head, False)
        self.write("feature.tf", "b = 3\n", path)
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            self.b.push_fix("second-infra-1", self.head, False)
        git("commit", "-qam", "fix: b", cwd=path)
        state = self.b.fix_state("second-infra-1")
        self.assertEqual((state["ff"], state["moved"], len(state["commits"]), state["links_out"]), (True, True, 1, []))
        self.assertIn("+b = 3", state["diff"])
        self.assertEqual(state["messages"].strip(), "fix: b")
        with self.assertRaisesRegex(ValueError, "not the one"):
            self.b.push_fix("second-infra-1", "1" * 40, False)
        out = self.b.push_fix("second-infra-1", self.head, False)
        self.assertEqual((self.remote(), out["forced"]), (state["head"], False))
        self.assertEqual(self.b.fix_state("second-infra-1")["pushed"], state["head"])

    def test_its_repository_is_trusted_as_the_dialog_would_record_it(self):
        self.prepare()
        with open(self.claude_json) as f:
            config = json.load(f)
        mirror = os.path.realpath(os.path.join(self.tmp, "root", "repos", "acme", "infra.git"))
        self.assertEqual(config["projects"][mirror], {"hasTrustDialogAccepted": True})
        self.assertEqual((config["numStartups"], config["projects"]["/elsewhere"]), (7, {"hasTrustDialogAccepted": True}), "the rest kept")
        self.assertEqual(oct(os.stat(self.claude_json).st_mode & 0o777), "0o600")
        os.utime(self.claude_json, (0, 0))
        self.prepare()
        self.assertEqual(os.stat(self.claude_json).st_mtime, 0, "written once")

    def test_a_push_of_yours_meanwhile_and_nothing_goes(self):
        path = self.prepare()["path"]
        self.write("feature.tf", "b = 4\n")
        git("commit", "-qam", "feat: yours", cwd=self.dev)
        git("push", "-q", "origin", "feature/sc-1-x", cwd=self.dev)
        yours = self.remote()
        self.write("other.tf", "c = 1\n", path)
        git("add", ".", cwd=path)
        git("commit", "-qm", "fix: c", cwd=path)
        with self.assertRaises(RuntimeError):
            self.b.push_fix("second-infra-1", self.head, True)  # leased on the head it started from, forced or not
        self.assertEqual(self.remote(), yours)

    def test_a_rebase_is_pushed_only_when_forcing_is_allowed(self):
        git("checkout", "-q", "main", cwd=self.dev)
        self.write("main.tf", "a = 2\n")
        git("commit", "-qam", "chore: a", cwd=self.dev)
        git("push", "-q", "origin", "main", cwd=self.dev)
        path = self.prepare()["path"]
        git("fetch", "-q", "origin", cwd=path)
        git("rebase", "-q", "origin/main", cwd=path)
        state = self.b.fix_state("second-infra-1")
        self.assertEqual((state["ff"], state["commits"][0].split(" ", 1)[1]), (False, "feat: b"))
        with self.assertRaisesRegex(ValueError, "forced push"):
            self.b.push_fix("second-infra-1", self.head, False)
        self.assertEqual(self.b.push_fix("second-infra-1", self.head, True)["forced"], True)
        self.assertEqual(self.remote(), state["head"])

    def test_a_worktree_goes_with_its_branch(self):
        path = self.prepare()["path"]
        self.prepare()  # again: the old one replaced
        self.assertTrue(self.b.remove_fix("second-infra-1"))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.b.remove_fix("second-infra-1"))
        self.assertIsNone(self.b.fix_meta("second-infra-1"))
        with self.assertRaises(LookupError):
            self.b.fix_state("second-infra-1")

    def test_a_link_leading_outside_the_repository_and_no_fix_runs(self):
        os.symlink("docs", os.path.join(self.dev, "latest"))  # inside: fine
        os.symlink(os.path.expanduser("~/.aws"), os.path.join(self.dev, "aws"))
        git("add", ".", cwd=self.dev)
        git("commit", "-qm", "chore: links", cwd=self.dev)
        git("push", "-q", "origin", "feature/sc-1-x", cwd=self.dev)
        with self.assertRaisesRegex(ValueError, "aws link outside"):
            self.prepare(head=git("rev-parse", "HEAD", cwd=self.dev))
        self.assertIsNone(self.b.fix_meta("second-infra-1"), "its worktree removed")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "root", "work", "second-infra-1")))

    def test_names_and_shas_are_checked(self):
        for bad in ({"repo": "../x"}, {"repo": "acme/infra; rm"}, {"branch": "-x"}, {"base": "a..b"}, {"head": "abc"}):
            with self.assertRaises(ValueError):
                self.b.prepare_fix("second-infra-1", {"repo": "acme/infra", "branch": "feature/sc-1-x", "head": self.head, "base": "main", **bad})

    def test_a_fix_console_runs_in_its_worktree_with_the_file_tools(self):
        path = self.prepare()["path"]
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=1 if "has-session" in argv else 0, stderr="")

        with mock.patch.object(self.b, "_tmux", side_effect=lambda *a: run(list(a))):
            self.b.open_console("second-infra-1", "fix it", "sc-1", {"ssh_hosts": [], "curl_hosts": [], "fix": True})
            with self.assertRaises(LookupError):
                self.b.open_console("second-other-2", "fix it", "", {"fix": True})
        argv = calls[1]
        self.assertEqual(argv[argv.index("-c") + 1], path)
        for env in (f"SECOND_WORKTREE={path}", "GIT_EDITOR=true", "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1"):
            self.assertIn(env, argv)
        self.assertEqual(argv[-1], f'{self.b.SECOND_FIX_COMMAND} "$SENTINEL_PROMPT"')
        self.assertIn("Edit,Write", self.b.SECOND_FIX_COMMAND)
        self.assertNotIn("Edit", self.b.SECOND_COMMAND)


if __name__ == "__main__":
    unittest.main()
