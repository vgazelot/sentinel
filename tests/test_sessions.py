"""Sessions: the bridge lists Claude Code's sessions with what the context advisor publishes about each, Sentinel files
them under the cycle's tickets and runs new ones in consoles."""
import json
import os
import time
from unittest import mock

from helpers import SentinelTest
from test_agents_and_bridge import bridge

SID = "2b2aabb8-84b9-4d5e-bfbc-f8d2f25a3f4b"


class BridgeSessionsTest(SentinelTest):
    def setUp(self):
        super().setUp()
        self.b = bridge()
        self.b.CLAUDE_HOME = self.tmp.name
        self.b.ADVISOR = os.path.join(self.tmp.name, "context-advisor")
        os.makedirs(self.b.ADVISOR)
        os.makedirs(os.path.join(self.tmp.name, "projects", "-work"))

    def live(self, agents, consoles=None, parents=None):
        listed = mock.Mock(returncode=0, stdout=json.dumps(agents), stderr="")
        with mock.patch.object(self.b.subprocess, "run", return_value=listed) as run, \
                mock.patch.object(self.b, "_console_pids", return_value=consoles or {}), \
                mock.patch.object(self.b, "_parents", return_value=parents or {}):
            rows = {r["console"] or r["id"]: r for r in self.b.live_sessions()}
        self.assertEqual(run.call_args.args[0][1:], ["agents", "--json"], "Claude Code's own list, for scripts")
        return rows

    def test_a_session_comes_with_its_title_and_what_the_advisor_publishes(self):
        advisor = {"story": "sc-1234", "pinned": True, "tokens": 366000, "cold": True, "advise": "cold"}
        with open(os.path.join(self.b.ADVISOR, f"{SID}.json"), "w") as f:
            json.dump(advisor, f)
        with open(os.path.join(self.tmp.name, "projects", "-work", f"{SID}.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(e) for e in ({"type": "ai-title", "aiTitle": "First"}, {"type": "user"},
                                                        {"type": "ai-title", "aiTitle": "Heap"}, {"type": "assistant"})) + "\n")
        s = self.live([{"pid": 100, "sessionId": SID, "name": "dev-9a", "status": "waiting", "waitingFor": "input needed"}])[SID]
        self.assertEqual((s["name"], s["status"], s["waiting_for"], s["title"], s["console"]), ("dev-9a", "waiting", "input needed", "Heap", ""))
        self.assertEqual(s["context"], advisor, "passed as the advisor wrote it: Sentinel reads it")

    def test_a_console_is_the_tmux_pane_claude_runs_in(self):
        rows = self.live([{"pid": 200, "sessionId": SID, "status": "idle"}], {"150": "sc-1234", "300": "check-lag-ab12"}, {200: 150})
        self.assertEqual(rows["sc-1234"]["id"], SID, "the pane's shell is Claude's parent")
        self.assertEqual((rows["check-lag-ab12"]["status"], rows["check-lag-ab12"]["context"]), ("starting", {}),
                         "a console whose Claude is not listed yet")

    def test_a_session_without_the_advisor_or_a_transcript_still_lists(self):
        s = self.live([{"pid": 1, "sessionId": SID, "status": "busy"}, {"pid": 2, "sessionId": "../../etc", "status": "idle"}])
        self.assertEqual((s[SID]["title"], s[SID]["context"]), ("", {}))
        self.assertEqual(s["../../etc"]["context"], {}, "an id that is not one reads no file")

    def test_a_failing_list_is_an_error_the_page_can_say(self):
        import http.client
        import threading

        server = self.b.ThreadingHTTPServer(("127.0.0.1", 0), self.b.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(self.b, "live_sessions", side_effect=RuntimeError("claude agents exited 1: boom")):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", "/sessions", headers={"Host": "localhost"})
            resp = conn.getresponse()
            self.assertEqual((resp.status, json.loads(resp.read())), (500, {"error": "claude agents exited 1: boom"}))
            conn.close()

    def test_the_list_fails_loud_and_old_advisor_files_go(self):
        old = os.path.join(self.b.ADVISOR, "old.json")
        with open(old, "w") as f:
            f.write("{}")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        self.live([])
        self.assertFalse(os.path.exists(old), "the advisor writes, it cannot delete")
        with open(old, "w") as f:
            f.write("{}")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        with mock.patch.object(self.b.os, "remove", side_effect=FileNotFoundError):
            self.live([])  # pruned by another read meanwhile: nothing to say
        with mock.patch.object(self.b.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="boom")), \
                self.assertRaises(RuntimeError, msg="an empty page would read as no session"):
            self.b.live_sessions()

    def test_a_console_started_on_a_ticket_is_linked_to_it(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=1 if "has-session" in argv else 0, stderr="")

        with mock.patch.object(self.b.subprocess, "run", side_effect=run):
            self.b.open_console("sc-1234", "Pick up sc-1234", "sc-1234")
            self.b.open_console("x", "go", "sc-1234; rm -rf ~")
        self.assertIn("CONTEXT_STORY=sc-1234", calls[1])
        self.assertFalse(any(a.startswith("CONTEXT_STORY") for a in calls[3]), "only a story id reaches the environment")

    def test_ending_a_console_waits_for_its_claude_to_leave(self):
        answers = iter([True, True, False])
        with mock.patch.object(self.b, "_console_pids", return_value={"150": "sc-1234", "300": "other"}), \
                mock.patch.object(self.b, "_parents", return_value={200: 150, 301: 300}), \
                mock.patch.object(self.b, "_tmux") as tmux, mock.patch.object(self.b.time, "sleep"), \
                mock.patch.object(self.b, "_running", side_effect=lambda pid: next(answers)) as running:
            self.b.end_console("sc-1234")
        tmux.assert_called_once_with("kill-session", "-t", "=sc-1234")
        self.assertEqual([c.args[0] for c in running.call_args_list], [200, 200, 200], "that console's Claude only, until it is gone")


def story(sid, name, state_type, mine=True):
    return {"id": sid, "name": name, "url": f"https://app.shortcut.com/acme/story/{sid}", "state_type": state_type,
            "state": "In Development" if state_type == "started" else "Ready", "updated_at": "2026-10-15T08:00:00Z", "mine": mine}


CYCLE = {"big_rocks": [{"title": "Mesh", "status": "on track", "epics": [{"stories": [story(1001, "Runbook", "started"), story(1002, "Upgrade", "unstarted"),
                                                               story(1003, "Theirs", "started", mine=False)]}]}],
         "run": {"label": "RUN", "epics": [{"stories": [story(2001, "Alert", "started"), story(2002, "Cert", "unstarted")]}]}}


def session(sid, status, context=None, **extra):
    return {"id": sid, "name": f"dev-{sid}", "status": status, "waiting_for": "", "console": "", "title": f"title {sid}",
            "context": context or {}, **extra}


COLD = {"story": "sc-9999", "tokens": 400_000, "cold": True, "advise": "cold", "answeredAt": 1_000, "nextUsd": 3.2,
        "freshUsd": 0.58, "handoff": "/handoff"}


class SessionsTest(SentinelTest):
    def setUp(self):
        super().setUp()
        import db
        import main

        db.set_watcher_state("cycle", "data", json.dumps(CYCLE))
        self.client = main.app.test_client()
        self.sessions = [
            session("a", "waiting", {"story": "sc-2001"}),
            session("", "starting", name="", console="sc-1002", title=""),
            session("c", "idle", COLD),
            session("d", "busy"),
        ]
        for p in (mock.patch("config.CLAUDE_BRIDGE_URL", "http://bridge"), mock.patch("cycle.enabled", return_value=True),
                  mock.patch("sessions.live", side_effect=lambda: (self.sessions, ""))):
            p.start()
            self.addCleanup(p.stop)

    def test_sessions_file_under_their_tickets_waiting_first(self):
        import sessions

        b = sessions.board()
        groups = {g["title"]: g for g in b["groups"]}
        self.assertEqual(list(groups), ["Mesh", "RUN", "Off cycle"])
        self.assertEqual([r["key"] for r in groups["Mesh"]["rows"]], ["console:sc-1002", "story:1001"],
                         "a ticket picked up shows once, with its session; another's ticket only with a session on it")
        self.assertEqual(groups["Mesh"]["rows"][0]["title"], "Upgrade", "the ticket names a console until Claude titles it")
        self.assertEqual([r["key"] for r in groups["RUN"]["rows"]], ["session:a"])
        self.assertEqual([r["key"] for r in groups["RUN"]["later"]], ["story:2002"], "not started yet: folded")
        self.assertEqual([r["state"] for r in groups["Off cycle"]["rows"]], ["working", "idle"])
        self.assertEqual(b["waiting"], 1)
        idle = groups["Off cycle"]["rows"][1]
        self.assertIn("400k of context, cache cold", idle["advice"])
        self.assertIn("≈ $3.20 just to read it again, a fresh session ≈ $0.58", idle["advice"], "the advisor's figures, as it gives them")
        self.assertEqual((idle["story"], idle["where"], idle["fresh"]), ("sc-9999", "dev-c", "/handoff"))

    def test_the_advisor_file_reads_whole(self):
        """contrib/claude-code-context/tests/published.ts is what the advisor writes — its own test says so —: every field
        Sentinel reads comes through as written, and a renamed one fails here."""
        import sessions

        with open("/contrib/claude-code-context/tests/published.ts") as f:
            text = f.read()
        written = json.loads(text[text.index("{"):text.rindex("}") + 1])
        ctx = sessions._context(written)
        self.assertEqual(ctx, {key: written[key] for key in ctx})

    def test_without_the_advisor_nothing_is_guessed(self):
        import sessions

        def row(console="", context=None):
            return sessions._session_row(session("e", "idle", context, console=console), 0, {})

        self.assertEqual(row()["state"], "idle", "no word on the cache: not called fresh")
        self.assertEqual(row(context={"tokens": 90_000, "cold": False, "answeredAt": None})["state"], "idle",
                         "nothing answered since a /clear: not called answered")
        self.assertEqual(row("sc-1002-ab12")["story"], "sc-1002", "a console picked up from a ticket, before the advisor speaks")
        self.assertEqual(row("sc-1002", {"story": "sc-7777"})["story"], "sc-7777", "the advisor's word wins")
        self.assertEqual(row("sc-1002", {"tokens": 5})["story"], "", "the advisor spoke: no story is its answer")

    def test_the_advisor_file_is_read_strictly(self):
        import sessions

        ctx = sessions._context({"story": "sc-12; rm", "tokens": "400000", "cold": "yes", "advise": "cold",
                                 "handoff": "/handoff\r", "answeredAt": float("inf"), "nextUsd": True, "project": "mesh"})
        self.assertEqual(ctx, {"story": "", "project": "mesh", "tokens": 0, "answeredAt": 0, "cold": False, "advise": "cold",
                               "nextUsd": 0, "freshUsd": 0, "handoff": ""}, "a command typed into a live session, never a \\r")

    def test_tickets_show_their_state_as_a_shape(self):
        import sessions

        def glyph(state_type, state):
            return sessions._glyph({"state_type": state_type, "state": state})

        self.assertEqual(glyph("backlog", "Backlog"), ("s-backlog", "neutral"))
        self.assertEqual(glyph("unstarted", "Ready for Development"), ("s-todo", "neutral"))
        self.assertEqual(glyph("started", "In Development"), ("s-started", "dim"))
        self.assertEqual(glyph("started", "Ready for Review"), ("s-review", "dim"))
        self.assertEqual(glyph("started", "Blocked"), ("s-blocked", "warn"), "a workflow's own word for blocked says it")

    def test_pages_render(self):
        page = self.client.get("/sessions").get_data(as_text=True)
        self.assertIn("Upgrade", page)
        self.assertRegex(page, r'id="nav-sessions"\s*>1<')
        self.assertIn('<section class="proj has-live" id="proj-Mesh">', page, "a big rock is one block, its sessions say so")
        self.assertIn('href="#s-started"', page, "a started ticket without a session is drawn half full")
        tabs = page[page.index('<nav class="tabs" id="tabs"'):page.index("</nav>", page.index('<nav class="tabs"'))]
        self.assertNotIn("hx-swap-oob", tabs, "the page draws the tabs in place")
        self.assertEqual(tabs.count('class="tab"'), 4, "every live session has its tab, no ticket does")
        polled = self.client.get("/partials/sessions").get_data(as_text=True)
        self.assertIn('<nav class="tabs" id="tabs" aria-label="Live sessions" hx-swap-oob="true">', polled,
                      "the polled list brings the tabs along")
        facts = {"id": 1001, "name": "Runbook", "url": "https://app.shortcut.com/acme/story/1001", "state": "In Development",
                 "prs": [{"url": "https://github.com/acme/infra/pull/7", "ref": "infra#7", "title": "feat: [sc-1001] runbook", "draft": True}],
                 "note": {"text": "## Handoff\n- **Left**: the alerts", "at": "2026-10-15T08:00:00Z", "handoff": True}}
        with mock.patch("shortcut.story", return_value=facts):
            ticket = self.client.get("/partials/session?key=story:1001").get_data(as_text=True)
            live = self.client.get("/partials/session?key=console:sc-1002").get_data(as_text=True)
            elsewhere = self.client.get("/partials/session?key=session:c").get_data(as_text=True)
        self.assertIn("Pick up", ticket)
        self.assertIn("Last handoff", ticket)
        self.assertIn("<strong>Left</strong>", ticket)
        self.assertIn("infra#7", ticket)
        self.assertIn('data-attach="/api/console/sc-1002/attach"', live)
        self.assertIn("another terminal", elsewhere)
        self.assertIn("/handoff", elsewhere)
        self.assertIn("This one is gone", self.client.get("/partials/session?key=session:zz").get_data(as_text=True))
        self.assertIn("New session", self.client.get("/partials/session?key=new").get_data(as_text=True))

    def test_a_ticket_is_picked_up_by_a_console_named_after_it(self):
        import yaml

        with mock.patch("console.start", return_value={"ticket": "t"}) as start, mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            resp = self.client.post("/api/sessions/start", data={"story": "1001"})
        name, prompt, story = start.call_args.args
        self.assertEqual((name, story), ("sc-1001", "sc-1001"), "the session starts linked to its ticket")
        self.assertIn("Pick up sc-1001", prompt)
        self.assertEqual(json.loads(resp.headers["HX-Trigger-After-Settle"]), {"select-key": {"key": "console:sc-1001"}})
        with open(self.cfg_path) as f:
            cfg = yaml.safe_load(f)
        cfg["sessions"] = {"resume_prompt": "Reprends {story}."}
        with open(self.cfg_path, "w") as f:
            yaml.safe_dump(cfg, f)
        with mock.patch("console.start", return_value={"ticket": "t"}) as start, mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            self.client.post("/api/sessions/start", data={"story": "1001"})
        self.assertEqual(start.call_args.args[1], "Reprends sc-1001.")
        self.assertEqual(self.client.post("/api/sessions/start", data={"story": "1001; rm"}).status_code, 400)
        self.sessions.append(session("m", "idle", {"story": "sc-7777"}, console="sc-1001"))
        with mock.patch("console.start", return_value={"ticket": "t"}) as start, mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            self.client.post("/api/sessions/start", data={"story": "1001"})
        self.assertRegex(start.call_args.args[0], r"^sc-1001-[0-9a-f]{4}$", "its console moved on to another ticket: a new one")

    def test_a_new_session_is_named_after_its_task(self):
        with mock.patch("console.start", return_value={"ticket": "t"}) as start:
            self.client.post("/api/sessions/new", data={"prompt": "Vérifie le lag Kafka sur node-3, puis dis-moi"})
            self.assertIn("Say what Claude should work on", self.client.post("/api/sessions/new", data={"prompt": " "}).get_data(as_text=True))
        name, prompt, story = start.call_args.args
        self.assertRegex(name, r"^verifie-le-lag-kafka-[0-9a-f]{4}$")
        self.assertEqual(story, "")
        self.assertEqual(prompt, "Vérifie le lag Kafka sur node-3, puis dis-moi")
        self.assertEqual(start.call_count, 1)

    def test_an_ended_console_gives_its_ticket_back(self):
        def end(name):
            self.sessions = [s for s in self.sessions if s["console"] != name]

        with mock.patch("console.end", side_effect=end), mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            page = self.client.post("/api/sessions/end", data={"name": "sc-1002"}).get_data(as_text=True)
            self.sessions.append(session("p", "idle", console="sc-1001-ab12"))
            picked = self.client.post("/api/sessions/end", data={"name": "sc-1001-ab12"}).get_data(as_text=True)
        self.assertIn("Pick up", page)
        self.assertIn("Pick up", picked, "a console picked up as sc-N-xxxx gives its ticket back too")
        self.assertEqual(self.client.post("/api/sessions/end", data={"name": "../x"}).status_code, 404)

    def test_a_bridge_away_says_so(self):
        self.sessions = []
        with mock.patch("sessions.live", return_value=([], "the Claude bridge: refused")):
            page = self.client.get("/partials/sessions").get_data(as_text=True)
        self.assertIn("the Claude bridge: refused", page)
        self.assertIn("Runbook", page, "the tickets are still there")

    def test_no_bridge_no_page(self):
        with mock.patch("config.CLAUDE_BRIDGE_URL", ""):
            self.assertEqual(self.client.get("/sessions").status_code, 404)
            self.assertNotIn('href="/sessions"', self.client.get("/").get_data(as_text=True))
