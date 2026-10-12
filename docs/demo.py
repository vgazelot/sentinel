"""Fills a database with made-up data for the README screenshot — never screenshot a real instance.

    docker run --rm -v "$PWD/app:/app" -v "$PWD/docs:/docs" -v /tmp/demo:/demo -w /app sentinel-dev python /docs/demo.py /demo
    docker run -d --name sentinel-demo -p 127.0.0.1:8320:8000 -e SENTINEL_SCHEDULER=0 -e CLAUDE_BRIDGE_URL=http://bridge \
        -e SENTINEL_CONFIG=/demo/config.yaml -v /tmp/demo:/demo -e SENTINEL_DB=/demo/sentinel.db sentinel-dev
"""
import json
import os
import sys

out = sys.argv[1]
os.environ["SENTINEL_DB"] = f"{out}/sentinel.db"
os.environ["SENTINEL_CONFIG"] = f"{out}/config.yaml"
with open(os.environ["SENTINEL_CONFIG"], "w") as f:
    f.write("watchers:\n  github_reviews: {enabled: false, login: you, orgs: [acme]}\n"
            "followup: {enabled: true}\ntriage: {team_grace_minutes: 60}\n")
sys.path.insert(0, "/app")

import db  # noqa: E402
import engine  # noqa: E402
from watchers import WatchedItem  # noqa: E402

db.init()
for source in ("github_reviews", "my_prs", "followup", "statuspage:cloud"):
    db.set_watcher_state(source, "seeded", db.now())


def pr(repo, number, title, lane, urgency="now", **payload):
    return WatchedItem(external_id=f"{repo}#{number}", title=f"{repo}#{number} — {title}",
                       url=f"https://github.com/acme/{repo}/pull/{number}", fingerprint="1", lane=lane, urgency=urgency,
                       payload={"repo": f"acme/{repo}", "number": number, "title": title, "ci": "success", **payload})


def bump(repo, number, name, to, kind, ci="success"):
    return pr(repo, number, f"chore(deps): Update {name} to {to}", "deps", "quiet", ci=ci, author="renovate",
              mergeable="MERGEABLE", automerge=True,
              dep={"name": name, "to": to, "type": kind, "key": f"{name}@{to}", "from": "", "change_to": to})


engine.process("github_reviews", [
    pr("api", 412, "feat(api): paginate the audit log endpoint", "you", why="asked you directly · by alice"),
    pr("ingest", 88, "fix(ingest): retry on 429 from the vendor API", "team", "later", why="team platform · by bob"),
    pr("billing", 51, "refactor(billing): split the invoice renderer", "team", "later", why="team platform · by dana"),
    bump("infra", 310, "karpenter", "v1.15.0", "minor"), bump("cluster", 92, "karpenter", "v1.15.0", "minor"),
    bump("tools", 44, "karpenter", "v1.15.0", "minor"),
    bump("infra", 311, "hashicorp/google", "v7.0.0", "major", ci="failure"),
])
engine.process("my_prs", [
    pr("infra", 301, "chore(dns): move the status page to the new zone", "mine", action="changes",
       why="changes requested · carol", updated_at=db.ago(300)),
    *[pr("config", n, t, "mine", action="ci", families=["Molecule"], why="CI failing · Molecule", updated_at=db.ago(900))
      for n, t in [(140, "fix(nginx): drop the legacy TLS ciphers"), (141, "feat(backup): verify restores weekly"),
                   (142, "fix(consul): throttle the restart handler")]],
    pr("deploy", 77, "feat(deploy): canary the worker pool first", "mine", action="merge",
       why="approved by erin · CI green — merge it", updated_at=db.ago(60)),
    pr("runbooks", 12, "docs: the backup restore drill, step by step", "mine", "quiet", action="no-reviewer",
       why="nobody was asked to review", updated_at=db.ago(4000)),
])
failed = {"name": "Apply prod / Plan/Apply network", "state": "failure", "before": "success",
          "url": "https://github.com/acme/infra/actions/runs/1"}
engine.process("followup", [
    WatchedItem(external_id="acme/infra#305:failed", title="infra#305 — broke after merge?", fingerprint="f",
                url="https://github.com/acme/infra/pull/305", lane="followup",
                payload={"repo": "acme/infra", "number": 305, "title": "chore(deps): Update terraform hashicorp/aws to v6.2.0",
                         "kind": "failed", "checks": [failed], "subject": "acme/infra#305",
                         "why": "a check that ran after the merge failed"}),
    WatchedItem(external_id="acme/api#398:dismissed", title="api#398 — approval dismissed", fingerprint="dismissed",
                url="https://github.com/acme/api/pull/398", lane="followup",
                payload={"repo": "acme/api", "number": 398, "title": "chore(deps): Update dependency express to v5.1.0",
                         "kind": "dismissed", "subject": "acme/api#398",
                         "why": "a new commit dismissed your approval — it will not merge on its own"}),
])
engine.process("statuspage:cloud", [WatchedItem(
    external_id="inc1", title="[cloud] Object Storage — elevated error rates", url="https://status.example.com",
    fingerprint="1", severity="minor", lane="incident",
    payload={"source": "cloud", "name": "Object Storage — elevated error rates in eu-west", "status": "investigating",
             "components": ["Object Storage"], "body": "We are investigating elevated 5xx rates on PUT requests."})])

landed = [{"repo": "acme/infra", "number": 300 + i, "title": f"chore(deps): bump {n}", "url": "https://github.com/acme/infra/pull/1",
           "stage": "landed", "merged": True, "merged_at": db.ago(600), "approved_at": db.ago(900), "post_checks": [],
           "landed_at": db.ago(590), "blocker": "", "automerge": True, "dismissed": False, "waiting_days": 0}
          for i, n in enumerate(["helm", "go", "node", "terraform", "trivy", "alloy", "mise"])]
checking = dict(landed[0], number=309, title="chore(deps): Update Helm release ingress-nginx to v4.13.0", stage="checking",
                merged_at=db.ago(4), landed_at=None)
db.set_watcher_state("followup", "data", json.dumps({"prs": landed + [checking], "window_days": 7, "stale_days": 3}))
db.set_watcher_state("my_prs", "data", "[]")

for subject, mode, output in [
    ("acme/infra#305", "followup", "Verdict: caused-by-this-pr\n- the provider v6 renamed `aws_eip.vpc` to `domain`: "
     "`network/eip.tf:12` fails the apply (run 1, step Apply)\n- still failing on the next run\n- revert #305, or pin `< 6.0` in network"),
    ("deps:karpenter@v1.15.0", "precheck", "Verdict: safe\n- release notes: no breaking change for the NodePool v1 API "
     "used in infra, cluster and tools\n- CI green on the 3 PRs"),
    ("deps:hashicorp/google@v7.0.0", "precheck", "Verdict: risky\n- v7 removes `google_container_cluster.enable_binary_authorization`, "
     "still set in `infra/gke/main.tf:41` — the plan fails on it"),
    ("acme/api#412", "quick", "Verdict: clean"),
]:
    db.queue_run(subject, mode, "https://github.com/acme/x/pull/1", {})
    db.finish_run(db.get_run(subject, mode)["id"], output=output)
print("demo database ready in", out)
