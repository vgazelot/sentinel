"""Every page and partial renders on a populated database — template errors only show up at render time."""
import json
from unittest import mock

from helpers import SentinelTest


class WebTest(SentinelTest):
    def setUp(self):
        super().setUp()
        import db
        import engine
        import main
        from watchers import WatchedItem

        self.client = main.app.test_client()
        self.seed("github_reviews", "my_prs", "followup", "statuspage:sp")

        def pr_item(ext, lane, urgency, **payload):
            return WatchedItem(external_id=ext, title=f"x#{ext} — t", url=f"https://github.com/acme/x/pull/{ext}",
                               fingerprint="1", lane=lane, urgency=urgency,
                               payload={"repo": "acme/x", "number": int(ext), "title": f"title {ext}", "why": "w",
                                        "author": "alice", "ci": "success", **payload})

        dep = {"name": "go", "to": "v1.2", "type": "patch", "key": "go@v1.2", "from": "v1.1", "change_to": "v1.2"}
        engine.process("github_reviews", [pr_item("1", "you", "now"), pr_item("2", "team", "later"),
                                          pr_item("3", "deps", "quiet", dep=dep, mergeable="MERGEABLE")])
        engine.process("my_prs", [pr_item("4", "mine", "now", action="ci", families=["Molecule"], updated_at=db.now()),
                                  pr_item("5", "mine", "now", action="ci", families=["Molecule"], updated_at=db.now()),
                                  pr_item("6", "mine", "quiet", action="nudge", updated_at=db.now())])
        engine.process("statuspage:sp", [WatchedItem(external_id="i1", title="[sp] down", url="https://s", fingerprint="1",
                                                     severity="major", lane="incident",
                                                     payload={"source": "sp", "name": "down", "status": "investigating"})])
        failed = {"name": "apply", "state": "failure", "url": "https://github.com/acme/x/actions/runs/1", "before": "success"}
        engine.process("followup", [WatchedItem(external_id="acme/x#7:failed", title="x#7 — broke", url="https://github.com/acme/x/pull/7",
                                                fingerprint="f", lane="followup",
                                                payload={"repo": "acme/x", "number": 7, "title": "bump", "kind": "failed",
                                                         "checks": [failed], "why": "w", "subject": "acme/x#7"})])
        landed = {"repo": "acme/x", "number": 8, "title": "bump", "url": "https://github.com/acme/x/pull/8", "stage": "landed",
                  "merged": True, "merged_at": db.now(), "approved_at": db.now(), "post_checks": [dict(failed, state="success")],
                  "landed_at": db.now(), "blocker": "", "automerge": False}
        db.set_watcher_state("followup", "data", json.dumps({"prs": [landed, dict(landed, number=7, stage="failed",
                                                                                   post_checks=[failed])],
                                                              "window_days": 7, "stale_days": 3}))
        db.set_watcher_state("my_prs", "data", json.dumps([]))
        db.queue_run("acme/x#7", "followup", "https://github.com/acme/x/pull/7", {})
        db.finish_run(db.get_run("acme/x#7", "followup")["id"], output="Verdict: caused-by-this-pr\n- **the bump** broke the apply")

    def test_pages_render(self):
        with mock.patch("config.CLAUDE_BRIDGE_URL", "http://bridge"):
            for path in ("/", "/partials/now", "/my-prs", "/partials/my-prs", "/follow-up", "/partials/follow-up",
                         "/settings", "/partials/watchers", "/partials/agent?subject=acme/x%237&modes=followup,deep"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200, path)
            now = self.client.get("/partials/now").get_data(as_text=True)
        self.assertIn("Reviews for you", now)
        self.assertIn("CI failing — Molecule", now, "two PRs red on the same job = one row")
        self.assertIn("Claude · caused-by-this-pr", now)
        self.assertIn("the bump broke the apply", now, "first finding, markdown stripped")
        self.assertIn("go", now)

    def test_summary_counts_decisions(self):
        data = self.client.get("/api/summary?since=0").get_json()
        # direct review + incident + follow-up failure + one group for the two red PRs
        self.assertEqual(data["attention"], 4)
        self.assertEqual(data["counts"]["deps"], 1)
        self.assertEqual(data["counts"]["grace"], 1)

    def test_claude_clears_a_failure_it_traced_elsewhere(self):
        import db

        db.finish_run(db.get_run("acme/x#7", "followup")["id"], output="Verdict: unrelated\n- already broken since May")
        self.assertEqual(self.client.get("/api/summary").get_json()["attention"], 3)
        self.assertIn("not this PR", self.client.get("/partials/now").get_data(as_text=True))

    def test_bulk_hide(self):
        import db

        ids = ",".join(str(r["id"]) for r in db.items("watcher = 'my_prs' AND urgency = 'now'"))
        resp = self.client.post("/api/items/bulk", data={"ids": ids, "action": "ack"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/summary").get_json()["attention"], 3)

    def test_bump_approval_skips_what_is_not_green(self):
        import db

        with db.connect() as conn:
            conn.execute("UPDATE items SET payload = json_set(payload, '$.ci', 'failure') WHERE external_id = '3'")
        with mock.patch("github_pr.submit_review") as submit:
            resp = self.client.post("/api/deps/approve", data={"key": "go@v1.2"}, headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        submit.assert_not_called()
        self.assertIn("Left for you", resp.get_data(as_text=True))

    def test_approve_from_the_panel_acks_on_the_post_review_state(self):
        import db

        item = db.items("external_id = '1'")[0]
        with mock.patch("github_pr.submit_review") as submit, mock.patch("github_pr.updated_at", return_value="after"):
            resp = self.client.post(f"/api/items/{item['id']}/approve", data={"body": "lgtm", "head_sha": "abc"},
                                    headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        submit.assert_called_once_with("acme/x", 1, "APPROVE", "lgtm", [], commit_id="abc")
        self.assertIn("reviewSubmitted", resp.headers.get("HX-Trigger", ""))
        row = db.items("external_id = '1'")[0]
        self.assertEqual((row["state"], row["acked_fingerprint"]), ("acked", "after"))
