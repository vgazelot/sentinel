import unittest

import helpers  # noqa: F401 — sys.path
import deps

RENOVATE_BODY = """This PR contains the following updates:

| Package | Type | Update | Change |
|---|---|---|---|
| [scaleway](https://registry.terraform.io/providers/scaleway/scaleway) ([source](https://redirect.github.com/scaleway/terraform-provider-scaleway)) | required_provider | minor | `~> 2.60, < 2.81.0` → `< 2.87.0` |
| [scaleway](https://registry.terraform.io/providers/scaleway/scaleway) ([source](https://x)) | required_provider | major | `< 2.81` → `< 3.0` |

---
"""


class TitleTest(unittest.TestCase):
    def test_renovate_titles(self):
        cases = {
            "chore(mise): Update dependency go to v1.27.2": ("go", "v1.27.2"),
            "chore(deps): Update karpenter-crd Docker tag to v1.15.0": ("karpenter-crd", "v1.15.0"),
            "chore(deps): Update Helm release alloy to v1.2.0": ("alloy", "v1.2.0"),
            "chore(deps): Update Terraform hashicorp/terraform to v1.16.4": ("hashicorp/terraform", "v1.16.4"),
            "chore(terraform): Update github to ~> 6.13": ("github", "~> 6.13"),
            "ci(github-actions): Update jdx/mise-action action to v3": ("jdx/mise-action", "v3"),
            "terraform(deps): Update scaleway to v2.86.0": ("scaleway", "v2.86.0"),
            "Update dependency eslint to v9 (major)": ("eslint", "v9"),
            "Update dependency eslint to v9 [SECURITY]": ("eslint", "v9"),
        }
        for title, (name, to) in cases.items():
            dep = deps.parse_title(title)
            self.assertEqual((dep["name"], dep["to"]), (name, to), title)

    def test_what_is_bumped_is_part_of_the_bump(self):
        image = deps.parse_title("chore(deps): Update alloy Docker tag to v1.2")
        chart = deps.parse_title("chore(deps): Update Helm release alloy to v1.2")
        self.assertEqual((image["kind"], chart["kind"]), ("image", "Helm chart"))
        self.assertNotEqual(deps.group_key(image), deps.group_key(chart))
        self.assertTrue(deps.parse_title("Update dependency eslint to v9 [SECURITY]")["security"])
        self.assertEqual(deps.group_key(deps.parse_title("Update dependency eslint to v9 [SECURITY]")),
                         deps.group_key(deps.parse_title("Update dependency eslint to v9")))

    def test_dependabot_titles(self):
        dep = deps.parse_title("build(deps): bump actions/checkout from 3 to 4 in /tools")
        self.assertEqual((dep["name"], dep["from"], dep["to"]), ("actions/checkout", "3", "4"))

    def test_not_a_bump(self):
        self.assertIsNone(deps.parse_title("fix(runners): drop obsolete gradle jvm args"))
        self.assertFalse(deps.is_dependency_pr("Runner image version 1.2", ["infra"]))
        self.assertTrue(deps.is_dependency_pr("Lock file maintenance", ["renovate"]))


class BodyTest(unittest.TestCase):
    def test_renovate_table_takes_the_biggest_bump(self):
        body = deps.parse_body(RENOVATE_BODY)
        self.assertEqual(body["type"], "major")
        self.assertEqual((body["from"], body["to"]), ("~> 2.60, < 2.81.0", "< 2.87.0"))
        self.assertEqual(body["packages"], ["scaleway"])

    def test_dependabot_sentence(self):
        body = deps.parse_body("Bumps [lodash](https://github.com/lodash/lodash) from 4.17.15 to 4.17.21.")
        self.assertEqual((body["type"], body["from"], body["to"]), ("patch", "4.17.15", "4.17.21"))

    def test_semver(self):
        self.assertEqual(deps.semver_type("v1.2.3", "v2.0.0"), "major")
        self.assertEqual(deps.semver_type("~> 1.14", "~> 1.16"), "minor")
        self.assertEqual(deps.semver_type("1.27.1", "1.27.2"), "patch")
        self.assertEqual(deps.semver_type("latest", "v2"), "")


class GroupTest(unittest.TestCase):
    def test_same_bump_across_repos_is_one_group(self):
        def pr(repo, name, to, kind):
            return {"repo": repo, "number": 1, "title": "t", "dep": {"name": name, "to": to, "type": kind}}

        groups = deps.group([pr("a/x", "go", "v1.2", "patch"), pr("a/y", "Go", "v1.2", "patch"),
                             pr("a/z", "karpenter", "v2", "major")])
        self.assertEqual([g["key"] for g in groups], ["karpenter@v2", "go@v1.2"])
        self.assertEqual(len(groups[1]["prs"]), 2)

    def test_approvable_is_green_not_draft_not_conflicting(self):
        base = {"repo": "a/x", "number": 1, "title": "t", "dep": {"name": "go", "to": "v1", "type": "patch"},
                "ci": "success", "mergeable": "MERGEABLE"}
        prs = [base, dict(base, number=2, mergeable="CONFLICTING"), dict(base, number=3, ci="pending"),
               dict(base, number=4, draft=True)]
        self.assertEqual([p["number"] for p in deps.group(prs)[0]["approvable"]], [1])
