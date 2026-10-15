from helpers import SentinelTest


def review_node(requested=(("User", "me"),), author=("User", "alice"), decision="REVIEW_REQUIRED", draft=False,
                title="fix(x): something", labels=()):
    import db

    return {
        "id": "PR_9", "number": 9, "title": title, "url": "https://github.com/acme/x/pull/9", "isDraft": draft,
        "createdAt": db.ago(30), "updatedAt": db.ago(10), "reviewDecision": decision, "mergeable": "MERGEABLE",
        "additions": 3, "deletions": 1, "changedFiles": 1, "repository": {"nameWithOwner": "acme/x"},
        "author": {"__typename": author[0], "login": author[1]}, "labels": {"nodes": [{"name": l} for l in labels]},
        "autoMergeRequest": None,
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": t, "login": n} if t == "User"
                                      else {"__typename": t, "slug": n}} for t, n in requested]},
        "latestReviews": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {"state": "SUCCESS"}}}]},
    }


class LaneTest(SentinelTest):
    def watcher(self):
        from unittest import mock

        from watchers.github_reviews import GithubReviewsWatcher

        w = GithubReviewsWatcher({"login": "me", "orgs": ["acme"]})
        mock.patch("watchers.github_reviews._body", return_value="").start()
        self.addCleanup(mock.patch.stopall)
        return w

    def test_direct(self):
        it = self.watcher()._item(review_node())
        self.assertEqual((it.lane, it.urgency), ("you", "now"))

    def test_team_waits_for_the_grace_period_and_leaves_once_approved(self):
        w = self.watcher()
        it = w._item(review_node(requested=(("Team", "infra"),)))
        self.assertEqual((it.lane, it.urgency, it.fingerprint), ("team", "later", "team"))
        self.assertIsNone(w._item(review_node(requested=(("Team", "infra"),), decision="APPROVED")))
        self.assertIsNotNone(w._item(review_node(decision="APPROVED")), "asked by name: stays")

    def test_bot_bump_is_quiet_and_keyed_by_version(self):
        it = self.watcher()._item(review_node(requested=(("Team", "infra"),), author=("Bot", "renovate"),
                                              title="chore(deps): Update dependency go to v1.27.2"))
        self.assertEqual((it.lane, it.urgency, it.fingerprint), ("deps", "quiet", "v1.27.2"))
        self.assertEqual(it.payload["dep"]["key"], "go@v1.27.2")

    def test_bot_that_is_not_a_bump_is_a_normal_request(self):
        it = self.watcher()._item(review_node(requested=(("Team", "infra"),), author=("Bot", "releasebot"),
                                              title="Runner image version 1.4"))
        self.assertEqual(it.lane, "team")

    def test_drafts_never_push(self):
        self.assertEqual(self.watcher()._item(review_node(draft=True)).urgency, "quiet")


def mine(**kw):
    pr = {"draft": False, "mergeable": "MERGEABLE", "review": "REVIEW_REQUIRED", "ci": "success", "unresolved": 0,
          "reviewers": ["alice"], "approved_by": [], "updated_at": "2099-01-01T00:00:00Z"}
    pr.update(kw)
    return pr


class MyPrsTest(SentinelTest):
    def test_next_action(self):
        from my_prs import next_action

        self.assertEqual(next_action(mine(draft=True, ci="failure"), 3), "draft")
        self.assertEqual(next_action(mine(mergeable="CONFLICTING"), 3), "conflicts")
        self.assertEqual(next_action(mine(review="CHANGES_REQUESTED", ci="failure"), 3), "changes")
        self.assertEqual(next_action(mine(ci="failure", review="APPROVED"), 3), "ci")
        self.assertEqual(next_action(mine(review="APPROVED"), 3), "merge")
        self.assertEqual(next_action(mine(unresolved=2), 3), "threads")
        self.assertEqual(next_action(mine(ci="pending"), 3), "running")
        self.assertEqual(next_action(mine(reviewers=[]), 3), "no-reviewer")
        self.assertEqual(next_action(mine(updated_at="2020-01-01T00:00:00Z"), 3), "nudge")
        self.assertEqual(next_action(mine(), 3), "waiting")

    def test_ci_family(self):
        from checks import family

        self.assertEqual(family("Molecule Test (EC2) - rsyslog / default"), "Molecule Test (EC2)")
        self.assertEqual(family("plan / Check folders"), "plan")

    def test_only_actions_on_you_become_items(self):
        from my_prs import items

        prs = [dict(mine(**kw), id=f"PR_{i}", repo="acme/x", number=i, title="t", url="u", branch="b",
                    ci_family=["Molecule Test (EC2)"], changes_by=[], action=action)
               for i, (action, kw) in enumerate([("merge", {}), ("ci", {"ci": "failure"}), ("waiting", {}),
                                                 ("draft", {}), ("nudge", {})])]
        got = {i.payload["action"]: i.urgency for i in items(prs)}
        self.assertEqual(got, {"merge": "quiet", "ci": "now", "nudge": "quiet"}, "merging is yours, never pushed")
