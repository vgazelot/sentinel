from helpers import SentinelTest


def node(merged=True, my_state="APPROVED", checks=(), before=(), revert=None, merged_ago_min=600, decision="APPROVED"):
    """A GraphQL PullRequest node as followup.QUERY returns it."""
    import db

    def run(name, status, conclusion, event="push"):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
                "detailsUrl": f"https://github.com/acme/x/actions/runs/1/job/{name}", "completedAt": db.ago(1),
                "checkSuite": {"workflowRun": {"event": event, "databaseId": 1, "workflow": {"name": "Release"}}}}

    return {
        "id": "PR_1", "number": 7, "title": "chore(deps): Update go to v1.2", "url": "https://github.com/acme/x/pull/7",
        "isDraft": False, "createdAt": db.ago(9000), "updatedAt": db.ago(60), "merged": merged,
        "mergedAt": db.ago(merged_ago_min) if merged else None, "closedAt": db.ago(merged_ago_min) if merged else None,
        "reviewDecision": decision, "mergeable": "MERGEABLE", "headRefName": "renovate/go",
        "repository": {"nameWithOwner": "acme/x"}, "author": {"login": "renovate"}, "autoMergeRequest": None,
        "latestReviews": {"nodes": [{"state": my_state, "submittedAt": db.ago(5000), "author": {"login": "me"}}]},
        "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": []}}}}]},
        "mergeCommit": {
            "oid": "abc",
            "statusCheckRollup": {"contexts": {"nodes": [run(*c) for c in checks]}},
            "parents": {"nodes": [{"statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "CheckRun", "name": n, "status": "COMPLETED", "conclusion": c} for n, c in before]}}}]},
        } if merged else None,
        "timelineItems": {"nodes": [{"source": revert}] if revert else []},
    }


class StageTest(SentinelTest):
    def norm(self, **kw):
        import followup

        return followup._normalize(node(**kw), "me", 3)

    def test_landed(self):
        self.assertEqual(self.norm(checks=[("apply", "COMPLETED", "SUCCESS")])["stage"], "landed")

    def test_pre_merge_runs_are_not_post_merge(self):
        pr = self.norm(checks=[("plan", "COMPLETED", "FAILURE", "merge_group")])
        self.assertEqual(pr["post_checks"], [])
        self.assertEqual(pr["stage"], "no checks")

    def test_failed_vs_inherited(self):
        new = self.norm(checks=[("apply", "COMPLETED", "FAILURE")], before=[("apply", "SUCCESS")])
        self.assertEqual(new["stage"], "failed")
        old = self.norm(checks=[("apply", "COMPLETED", "FAILURE")], before=[("apply", "FAILURE")])
        self.assertEqual(old["stage"], "inherited")
        unknown = self.norm(checks=[("apply", "COMPLETED", "FAILURE")])
        self.assertEqual(unknown["stage"], "failed", "did not run before: assume it is this merge")

    def test_a_job_the_parent_did_not_run_is_compared_with_the_workflow_previous_run(self):
        from unittest import mock

        import followup

        pr = self.norm(checks=[("apply", "COMPLETED", "FAILURE")])
        with mock.patch("evidence.previous_outcome", return_value="failure") as previous:
            followup._settle_before(pr)
        self.assertEqual(pr["stage"], "inherited")
        previous.assert_called_once()

    def test_cancelled_runs_are_not_failures(self):
        self.assertEqual(self.norm(checks=[("apply", "COMPLETED", "CANCELLED"), ("plan", "COMPLETED", "SUCCESS")])["stage"],
                         "landed")

    def test_checking_and_fresh_merge(self):
        self.assertEqual(self.norm(checks=[("apply", "IN_PROGRESS", None)])["stage"], "checking")
        self.assertEqual(self.norm(merged_ago_min=5)["stage"], "checking", "pipelines not started yet")

    def test_reverted(self):
        pr = self.norm(checks=[("apply", "COMPLETED", "SUCCESS")],
                       revert={"number": 9, "title": 'Revert "chore(deps): Update go"', "url": "https://github.com/acme/x/pull/9"})
        self.assertEqual(pr["stage"], "reverted")

    def test_not_mine(self):
        import followup

        self.assertIsNone(followup._normalize(node(my_state="COMMENTED"), "me", 3))

    def test_waiting_blockers(self):
        self.assertEqual(self.norm(merged=False, my_state="DISMISSED")["blocker"], "dismissed")
        self.assertEqual(self.norm(merged=False, decision="REVIEW_REQUIRED")["blocker"], "review required")


class ItemsTest(SentinelTest):
    def test_only_what_needs_you_becomes_an_item(self):
        import followup

        prs = [followup._normalize(n, "me", 3) for n in (
            node(checks=[("apply", "COMPLETED", "FAILURE")], before=[("apply", "SUCCESS")]),
            node(checks=[("apply", "COMPLETED", "FAILURE")], before=[("apply", "FAILURE")]),
            node(checks=[("apply", "COMPLETED", "SUCCESS")]),
            node(merged=False, my_state="DISMISSED"),
            node(merged=False),  # approved 3.5 days ago, still open → stuck
        )]
        kinds = sorted(i.payload["kind"] for i in followup.items({"prs": prs, "stale_days": 3}))
        self.assertEqual(kinds, ["dismissed", "failed", "stuck"])

    def test_fresh_failure_queues_an_investigation_once(self):
        from unittest import mock

        import db
        import followup

        pr = followup._normalize(node(checks=[("apply", "COMPLETED", "FAILURE")], merged_ago_min=30), "me", 3)
        old = followup._normalize(node(checks=[("apply", "COMPLETED", "FAILURE")], merged_ago_min=4000), "me", 3)
        with mock.patch("config.CLAUDE_BRIDGE_URL", "http://bridge"), \
                mock.patch("followup.context", return_value={"merge_sha": "abc", "material": "logs"}) as fetched:
            followup._investigate({"prs": [old]})
            fetched.assert_not_called()  # a week-old failure: no automatic run, no fetch
            followup._investigate({"prs": [pr]})
            followup._investigate({"prs": [pr]})
            self.assertEqual(db.count_runs("mode = 'followup'"), 1)
            db.finish_run(db.get_run("acme/x#7", "followup")["id"], output="Verdict: unrelated")
            followup._investigate({"prs": [pr]})
            self.assertEqual(db.count_runs("mode = 'followup'"), 1, "same merge commit: not again")
        with mock.patch("evidence.pr_brief", return_value="PR"), mock.patch("evidence.job_log", return_value="LOG"), \
                mock.patch("evidence.workflow_history", return_value="HISTORY"):
            ctx = followup.context(old)
        self.assertEqual(ctx["material"], "PR\n\nFailed check: apply (no earlier run found) "
                         "https://github.com/acme/x/actions/runs/1/job/apply\nLOG\n\nHISTORY")


class EvidenceTest(SentinelTest):
    def test_matrix_job_names_are_compared_without_their_drift(self):
        import evidence

        self.assertEqual(evidence._job_key("Apply / Plan ./50-org/common"), evidence._job_key("Apply / Plan 50-org/common"))
        self.assertEqual(evidence.job_ids("https://github.com/a/b/actions/runs/12/job/34"), ("12", "34"))
        self.assertIsNone(evidence.job_ids("https://jenkins.example/job/x/1"))
