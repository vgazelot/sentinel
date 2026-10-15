"""Claude reading ahead: once per state of what it reads, never for drafts, one diagnosis per red CI job."""
from unittest import mock

from helpers import SentinelTest


class InsightTest(SentinelTest):
    def setUp(self):
        super().setUp()
        import engine
        from watchers import WatchedItem

        self.seed("github_reviews", "my_prs")

        def item(ext, watcher_lane, **payload):
            return WatchedItem(external_id=ext, title=ext, url=f"https://github.com/acme/x/pull/{ext}", fingerprint="1",
                               lane=watcher_lane, urgency="now",
                               payload={"repo": "acme/x", "number": int(ext), "title": f"t{ext}", **payload})

        engine.process("github_reviews", [item("1", "you", head_sha="a1"), item("2", "team", head_sha="b1", draft=True)])
        engine.process("my_prs", [item("3", "mine", action="ci", families=["Molecule"], head_sha="c1"),
                                  item("4", "mine", action="ci", families=["Molecule"], head_sha="d1"),
                                  item("5", "mine", action="threads", unresolved=2, changes_by=[]),
                                  item("6", "mine", action="merge"),
                                  item("7", "mine", action="ci", families=["Jenkins"], head_sha="e1")])

    def queued(self):
        import db

        with db.connect() as conn:
            return sorted((r["subject"], r["mode"]) for r in conn.execute("SELECT * FROM agent_runs"))

    def refresh(self):
        import insight

        with mock.patch("config.CLAUDE_BRIDGE_URL", "http://bridge"), \
                mock.patch("insight.brief_context", side_effect=lambda p: {"head": p["head_sha"]}), \
                mock.patch("insight.whyred_context", side_effect=lambda f, prs: {"heads": insight.heads(prs)}), \
                mock.patch("insight.asks_context", side_effect=lambda p: {"asks": insight.asks_key(p)}), \
                mock.patch("insight.has_logs", side_effect=lambda f, p: f != "Jenkins"):
            insight.refresh()

    def test_each_reading_is_queued_once(self):
        self.refresh()
        self.assertEqual(self.queued(), [("acme/x#1", "brief"), ("asks:acme/x#5", "asks"), ("why:Molecule", "whyred")],
                         "no brief on a draft, one diagnosis for the two PRs red on Molecule, nothing to read on a "
                         "merge, no log to read on a commit status")
        self.refresh()
        self.assertEqual(len(self.queued()), 3, "nothing changed: nothing queued again")

    def test_a_new_commit_is_read_again(self):
        import db

        self.refresh()
        for run in (db.get_run("acme/x#1", "brief"), db.get_run("why:Molecule", "whyred")):
            db.finish_run(run["id"], output="Verdict: routine")
        with db.connect() as conn:
            conn.execute("UPDATE items SET payload = json_set(payload, '$.head_sha', 'c2') WHERE external_id = '3'")
        self.refresh()
        self.assertEqual(db.get_run("why:Molecule", "whyred")["status"], "queued", "a red PR moved: diagnose again")
        self.assertEqual(db.get_run("acme/x#1", "brief")["status"], "done", "the review did not move")
