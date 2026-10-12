from helpers import SentinelTest


def item(ext="a", fp="1", lane="you", urgency="now", title="PR"):
    from watchers import WatchedItem

    return WatchedItem(external_id=ext, title=title, url="https://github.com/acme/x/pull/1", fingerprint=fp,
                       payload={"why": "w"}, lane=lane, urgency=urgency)


def state(ext="a"):
    import db

    return db.items("external_id = ?", ext)[0]


class TriageTest(SentinelTest):
    def test_first_pass_records_without_pushing(self):
        import engine

        engine.process("w", [item("a"), item("b", lane="team", urgency="later")])
        self.assertEqual(self.pushes, [])
        self.assertEqual(state("a")["state"], "pending")
        engine.process("w", [item("a"), item("b", lane="team", urgency="later"), item("c")])
        self.assertEqual([p[0] for p in self.pushes], ["PR"], "only c, new after the baseline")

    def test_urgencies(self):
        import engine

        self.seed("w")
        engine.process("w", [item("now"), item("later", urgency="later"), item("quiet", urgency="quiet")])
        self.assertEqual(len(self.pushes), 1)
        self.assertIsNotNone(state("now")["notified_at"])
        self.assertIsNone(state("later")["notified_at"])
        self.assertIsNone(state("quiet")["notified_at"])

    def test_grace_period_promotes_unclaimed_team_requests(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("t", lane="team", urgency="later")])
        engine.promote_due()
        self.assertEqual(self.pushes, [], "still in grace")
        with db.connect() as conn:
            conn.execute("UPDATE items SET first_seen = ? WHERE external_id = 't'", (db.ago(61),))
        engine.promote_due()
        self.assertEqual(len(self.pushes), 1)
        engine.promote_due()
        self.assertEqual(len(self.pushes), 1, "once")

    def test_claimed_within_grace_is_never_pushed(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("t", lane="team", urgency="later")])
        engine.process("w", [])  # a teammate approved: gone from the search, twice in a row
        engine.process("w", [])
        with db.connect() as conn:
            conn.execute("UPDATE items SET first_seen = ? WHERE external_id = 't'", (db.ago(120),))
        engine.promote_due()
        self.assertEqual(self.pushes, [])
        self.assertEqual(state("t")["state"], "resolved")

    def test_escalation_to_direct_pushes(self):
        import engine

        self.seed("w")
        engine.process("w", [item("t", lane="team", urgency="later")])
        engine.process("w", [item("t", lane="you", urgency="now")])
        self.assertEqual(len(self.pushes), 1)

    def test_ack_then_change(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("a", fp="1"), item("q", fp="1", urgency="quiet")])
        for ext in ("a", "q"):
            row = state(ext)
            db.set_state(row["id"], "acked", acked_fingerprint=row["fingerprint"])
        self.pushes.clear()
        engine.process("w", [item("a", fp="1"), item("q", fp="1", urgency="quiet")])
        self.assertEqual(self.pushes, [])
        engine.process("w", [item("a", fp="2"), item("q", fp="2", urgency="quiet")])
        self.assertEqual(len(self.pushes), 1, "the quiet one comes back silently")
        self.assertEqual(state("q")["state"], "pending")

    def test_first_pass_keeps_acks_across_a_fingerprint_change(self):
        import db
        import engine

        with db.connect() as conn:  # an item acked by an older version, under its old fingerprint scheme
            conn.execute("""INSERT INTO items (watcher, external_id, title, url, fingerprint, state, acked_fingerprint,
                                               first_seen, last_seen) VALUES ('w', 'a', 'PR', 'u', 'old', 'acked', 'old', ?, ?)""",
                         (db.ago(600), db.ago(600)))
        engine.process("w", [item("a", fp="team", lane="team", urgency="later")])
        self.assertEqual(state("a")["state"], "acked")
        engine.process("w", [item("a", fp="team", lane="team", urgency="later")])
        self.assertEqual(state("a")["state"], "acked")
        engine.promote_due()
        self.assertEqual(self.pushes, [])

    def test_storm_becomes_one_push(self):
        import engine

        self.seed("w")
        engine.process("w", [item(str(i)) for i in range(6)])
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("6 new", self.pushes[0][0])

    def test_reappearance_is_a_fresh_cycle(self):
        import engine

        self.seed("w")
        engine.process("w", [item("a")])
        engine.process("w", [])
        engine.process("w", [])
        engine.process("w", [item("a")])
        self.assertEqual(len(self.pushes), 2)

    def test_one_missing_poll_is_not_a_resolution(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("a")])
        db.set_state(state("a")["id"], "acked", acked_fingerprint="1")
        engine.process("w", [])  # GitHub search dropped it for one poll
        engine.process("w", [item("a")])
        self.assertEqual(len(self.pushes), 1, "no second push")
        self.assertEqual(state("a")["state"], "acked", "the ack survives")

    def test_health_item_after_a_failure_streak(self):
        import db
        import engine

        engine.check_health()  # baseline
        db.record_run("github_reviews", "boom")
        engine.check_health()
        self.assertEqual(self.pushes, [], "not failing for long enough yet")
        db.set_watcher_state("github_reviews", "error_since", db.ago(31))
        engine.check_health()
        self.assertEqual(len(self.pushes), 1)
        db.record_run("github_reviews")
        engine.check_health()
        engine.check_health()
        self.assertEqual(state("github_reviews")["state"], "resolved")

    def test_snooze_wakes_loud_items_only(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("a"), item("q", urgency="quiet")])
        self.pushes.clear()
        for ext in ("a", "q"):
            db.set_state(state(ext)["id"], "snoozed", snooze_until=db.ago(1))
        engine.wake_snoozed()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(state("q")["state"], "pending")


class MissedTest(SentinelTest):
    def test_an_item_missing_from_the_last_poll_is_neither_promoted_nor_woken(self):
        import db
        import engine

        self.seed("w")
        engine.process("w", [item("t", lane="team", urgency="later"), item("s")])
        db.set_state(state("s")["id"], "snoozed", snooze_until=db.ago(1))
        self.pushes.clear()
        with db.connect() as conn:
            conn.execute("UPDATE items SET first_seen = ?", (db.ago(61),))
        engine.process("w", [])  # a teammate approved t; both missing once
        engine.promote_due()
        engine.wake_snoozed()
        self.assertEqual(self.pushes, [])
