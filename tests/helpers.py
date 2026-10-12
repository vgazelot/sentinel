"""Test scaffolding: a throwaway database and config per test, pushes captured instead of sent.

Run inside the image, app mounted (see README → Tests):
    docker run --rm -v "$PWD/app:/app" -v "$PWD/tests:/tests" -v "$PWD/contrib:/contrib" -w /app sentinel-dev python -m unittest discover -s /tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "/app")
os.environ.setdefault("SENTINEL_SCHEDULER", "0")
os.environ.setdefault("GITHUB_TOKEN", "test-token")

CONFIG = """
watchers:
  github_reviews: {enabled: true, interval_seconds: 180, login: me, orgs: [acme]}
followup: {enabled: true, interval_seconds: 300, authors: [renovate], stale_days: 3, window_days: 7}
triage: {team_grace_minutes: 60}
"""


class SentinelTest(unittest.TestCase):
    def setUp(self):
        import config
        import db

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.yaml")
        with open(self.cfg_path, "w") as f:
            f.write(CONFIG)
        self._patches = [
            mock.patch.object(config, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(config, "CONFIG_PATH", self.cfg_path),
        ]
        for p in self._patches:
            p.start()
        db.init()
        self.pushes = []
        push = mock.patch("notify.send_push", side_effect=lambda title, body, url, tag=None: self.pushes.append((title, body, tag)) or 1)
        push.start()
        self._patches.append(push)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def seed(self, *watchers: str):
        """Mark sources as already baselined, so their next pass notifies like in steady state."""
        import db

        for w in watchers:
            db.set_watcher_state(w, "seeded", db.now())
