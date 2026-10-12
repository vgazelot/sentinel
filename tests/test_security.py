from helpers import SentinelTest


class SecurityTest(SentinelTest):
    def setUp(self):
        super().setUp()
        import main

        self.client = main.app.test_client()

    def test_markdown_drops_html_and_script_links(self):
        import agents

        html = agents.render("Verdict: safe\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(2)) `path/file.tf:12`")
        self.assertNotIn("onerror", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("javascript:", html)
        self.assertIn('class="fileref"', html, "file refs are linked after the cleaning")

    def test_other_sites_cannot_post_through_the_browser(self):
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"}):
            resp = self.client.post("/api/items/bulk", data={"ids": "1", "action": "ack"}, headers=headers)
            self.assertEqual(resp.status_code, 403, headers)
        ok = self.client.post("/api/items/bulk", data={"ids": "1", "action": "ack"},
                              headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.post("/api/items/bulk", data={"ids": "1", "action": "ack"}).status_code, 200,
                         "no Origin: the Claude Code mod, curl")

    def test_rebound_dns_names_are_refused(self):
        self.assertEqual(self.client.get("/api/summary", headers={"Host": "evil.example:8300"}).status_code, 403)
        self.assertEqual(self.client.get("/api/summary", headers={"Host": "localhost:8300"}).status_code, 200)

    def test_failed_automatic_runs_get_one_more_try_an_hour_later(self):
        import agents
        import db

        db.queue_run("deps:go@v1", "precheck", "u", {"merge_sha": "a"})
        run = db.get_run("deps:go@v1", "precheck")
        self.assertTrue(agents.settled(run), "queued")
        db.finish_run(run["id"], error="bridge down")
        self.assertTrue(agents.settled(db.get_run("deps:go@v1", "precheck")), "failed just now: wait")
        with db.connect() as conn:
            conn.execute("UPDATE agent_runs SET finished_at = ?", (db.ago(61),))
        self.assertFalse(agents.settled(db.get_run("deps:go@v1", "precheck")), "an hour later: retry")
        self.assertFalse(agents.settled(db.get_run("deps:go@v1", "precheck"), merge_sha="b"), "another merge commit")
