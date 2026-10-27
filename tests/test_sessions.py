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

    def test_files_pasted_on_new_are_read_with_the_prompt(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=1 if "has-session" in argv else 0, stderr="")

        files = self.b._files([{"name": "Capture d’écran.png", "data": "iVBORw=="}, {"name": "../../.ssh/key", "data": ""}])
        with mock.patch.object(self.b.subprocess, "run", side_effect=run), mock.patch.object(self.b.tempfile, "tempdir", self.tmp.name):
            self.b.open_console("look-1", "Regarde ça", "", None, files)
        prompt = next(a for a in calls[1] if a.startswith("SENTINEL_PROMPT=")).removeprefix("SENTINEL_PROMPT=")
        text, mentions = prompt.split("\n\n")
        paths = [m.removeprefix("@") for m in mentions.split(" ")]
        self.assertEqual(text, "Regarde ça")
        self.assertEqual([os.path.basename(p) for p in paths], ["1-Capture-d-cran.png", "2-..-..-.ssh-key"], "a name fit for a path and a mention")
        self.assertEqual({os.path.dirname(os.path.dirname(p)) for p in paths}, {self.tmp.name}, "in a directory of their own")
        with open(paths[0], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG")
        with self.assertRaises(ValueError):
            self.b._files([{"name": "x", "data": "not base64!"}])

    def test_ending_a_console_waits_for_its_claude_to_leave(self):
        answers = iter([True, True, False])
        with mock.patch.object(self.b, "_console_pids", return_value={"150": "sc-1234", "300": "other"}), \
                mock.patch.object(self.b, "_parents", return_value={200: 150, 301: 300}), \
                mock.patch.object(self.b, "_tmux") as tmux, mock.patch.object(self.b.time, "sleep"), \
                mock.patch.object(self.b, "_running", side_effect=lambda pid: next(answers)) as running:
            self.b.end_console("sc-1234")
        tmux.assert_called_once_with("kill-session", "-t", "=sc-1234")
        self.assertEqual([c.args[0] for c in running.call_args_list], [200, 200, 200], "that console's Claude only, until it is gone")

    def test_ending_a_session_stops_its_claude_only(self):
        listed = [{"pid": 300, "sessionId": "other"}, {"pid": 400, "sessionId": SID}]

        def end(comm, alive=(True, False)):
            answers = iter(alive)
            ps = mock.Mock(returncode=0, stdout=f"{comm}\n")
            with mock.patch.object(self.b, "_agents", return_value=listed), \
                    mock.patch.object(self.b.subprocess, "run", return_value=ps) as run, \
                    mock.patch.object(self.b.os, "kill") as kill, mock.patch.object(self.b.time, "sleep"), \
                    mock.patch.object(self.b, "_running", side_effect=lambda pid: next(answers)):
                self.b.end_session(SID)
            return run, kill

        run, kill = end("claude")
        self.assertEqual(run.call_args.args[0], ["ps", "-o", "comm=", "-p", "400"])
        kill.assert_called_once_with(400, self.b.signal.SIGTERM)
        with self.assertRaisesRegex(RuntimeError, "runs /usr/bin/vim, not claude"):
            end("/usr/bin/vim")  # a pid left behind is another program's by now: said, not killed, not called ended
        self.assertFalse(end("")[1].called, "it left on its own meanwhile")
        with mock.patch.object(self.b, "_agents", return_value=listed[:1]), mock.patch.object(self.b.os, "kill") as kill:
            self.b.end_session(SID)
        self.assertFalse(kill.called, "gone already: nothing to do")
        with mock.patch.object(self.b.time, "monotonic", side_effect=[0, 0, 11]), self.assertRaises(RuntimeError):
            end("claude", alive=(True, True))

    def test_ending_a_session_takes_its_id_only(self):
        import http.client
        import threading

        server = self.b.ThreadingHTTPServer(("127.0.0.1", 0), self.b.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(self.b, "TOKEN", "t"), mock.patch.object(self.b, "end_session") as end:
            for sid, code in (("1; kill -9 -1", 400), (SID, 200)):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                conn.request("POST", "/sessions/end", json.dumps({"id": sid}),
                             headers={"Host": "localhost", "Authorization": "Bearer t", "Content-Type": "application/json"})
                self.assertEqual(conn.getresponse().status, code)
                conn.close()
        end.assert_called_once_with(SID)


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

    def test_an_alert_console_goes_with_the_run(self):
        """Watch's Claude button starts `zabbix-<event>`: run work from the start, before its ticket reaches the cycle."""
        import sessions

        self.sessions = [session("z", "busy", console="zabbix-42"), session("y", "idle", {"story": "sc-1001"}, console="zabbix-43"),
                         session("x", "idle", console="check-lag-ab12")]
        groups = {g["title"]: [r["key"] for r in g["rows"]] for g in sessions.board()["groups"]}
        self.assertIn("console:zabbix-42", groups["RUN"], "no ticket yet: the run's")
        self.assertIn("console:zabbix-43", groups["Mesh"], "its ticket says where it goes")
        self.assertEqual(groups["Off cycle"], ["console:check-lag-ab12"], "any other console without a ticket stays off cycle")

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
        self.assertIn('<section class="proj has-live" id="proj-rock-Mesh" data-group="rock:Mesh">', page, "a big rock is one block, its sessions say so")
        self.assertIn('href="#s-started"', page, "a started ticket without a session is drawn half full")
        self.assertIn('<div class="board" id="board"', page, "the consoles' board, in the pane's place")
        self.assertIn('data-key="console:sc-1002" data-term="sc-1002"', page, "a console's row puts it on the board")
        self.assertNotIn("/partials/session?key=console", page, "a console never opens in the pane")
        self.assertIn('data-detail="/partials/session?key=session%3Ad"', page, "a session of another terminal does")
        self.assertEqual(page.count("<template data-key="), 1, "a head for each console, none for another terminal")
        head = page[page.index('<template data-key="console:sc-1002">'):page.index("</template>")]
        self.assertIn('data-close="console:sc-1002" data-word="starting" data-ask', head, "End on the board, asking first")
        self.assertIn("Upgrade", head, "titled after its ticket until Claude names it")
        self.assertIn('data-close="session:d" data-word="working" data-ask', page, "a session at work asks first")
        self.assertIn('data-close="session:c" data-word="idle" title=', page, "an idle one ends at once")
        polled = self.client.get("/partials/sessions").get_data(as_text=True)
        self.assertIn('<template data-key="console:sc-1002">', polled, "every poll brings the heads along")
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
        self.assertIn("Opening on the board", live, "a start lands in the pane, the board takes it from the list")
        self.assertNotIn("/attach", live)
        self.assertIn("another terminal", elsewhere)
        self.assertIn('hx-vals=\'{"key": "session:c"}\'', elsewhere, "it ends from here too")
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

    def test_files_pasted_on_new_go_with_its_prompt(self):
        import io

        import sessions

        def post():
            return self.client.post("/api/sessions/new", content_type="multipart/form-data",
                                    data={"prompt": "Regarde", "files": [(io.BytesIO(b"\x89PNG"), "shot.png")]})

        with mock.patch("console.start", return_value={"ticket": "t"}) as start:
            post()
            with mock.patch.object(sessions, "PASTED_MAX", 3):
                too_big = post().get_data(as_text=True)
        self.assertEqual(start.call_args.kwargs["files"], [{"name": "shot.png", "data": "iVBORw=="}])
        self.assertEqual(start.call_count, 1)
        self.assertIn("Files up to", too_big)

    def test_new_goes_under_the_run_unless_told_otherwise(self):
        import sessions

        form = self.client.get("/partials/session?key=new").get_data(as_text=True)
        self.assertIn('<input type="radio" name="under" value="run" data-start="" checked>RUN', form, "the run by default")
        self.assertIn('value="rock:Mesh" data-start="">Mesh', form, "every group to choose from")
        with mock.patch("console.start", return_value={"ticket": "t"}) as start:
            self.client.post("/api/sessions/new", data={"prompt": "Vérifie le lag Kafka", "under": "run"})
            run_name = start.call_args.args[0]
            self.client.post("/api/sessions/new", data={"prompt": "Lis la doc", "under": "off"})
            off_name = start.call_args.args[0]
            self.client.post("/api/sessions/new", data={"prompt": "Ailleurs", "under": "own:abcdef"})
            again = self.client.post("/api/sessions/new", data={"prompt": " ", "under": "off"}).get_data(as_text=True)
        self.assertIn('value="off" data-start="" checked', again, "the choice survives an error")
        self.sessions = [session("r", "busy", console=run_name), session("o", "busy", console=off_name)]
        groups = {g["key"]: [r["key"] for r in g["rows"]] for g in sessions.board()["groups"]}
        self.assertIn(f"console:{run_name}", groups["run"])
        self.assertEqual(groups["off"], [f"console:{off_name}"], "off cycle files nothing")
        self.assertEqual(set(sessions._layout()[1]), {f"console:{run_name}"}, "a group that does not exist is not filed")
        with mock.patch("cycle.enabled", return_value=False):
            self.assertNotIn('name="under"', self.client.get("/partials/session?key=new").get_data(as_text=True), "no cycle, no choice")

    def test_new_starts_the_task_with_its_groups_words(self):
        import yaml

        with open(self.cfg_path) as f:
            cfg = yaml.safe_load(f)
        cfg["sessions"] = {"new_prompts": {"run": "Ouvre un ticket run sur : ", "Mesh": "<b>Mesh</b> : "}}
        with open(self.cfg_path, "w") as f:
            yaml.safe_dump(cfg, f)
        form = self.client.get("/partials/session?key=new").get_data(as_text=True)
        self.assertIn(">Ouvre un ticket run sur : </textarea>", form, "the group checked starts the task")
        self.assertIn('value="rock:Mesh" data-start="&lt;b&gt;Mesh&lt;/b&gt; : "', form, "by title too, escaped")
        self.assertIn('value="off" data-start=""', form)
        again = self.client.post("/api/sessions/new", data={"prompt": " ", "under": "off"}).get_data(as_text=True)
        self.assertIn("></textarea>", again, "the group picked, not the run's words")
        cfg["sessions"] = {"new_prompts": "run"}
        with open(self.cfg_path, "w") as f:
            yaml.safe_dump(cfg, f)
        self.assertIn('value="run" data-start=""', self.client.get("/partials/session?key=new").get_data(as_text=True), "not a mapping: ignored")

    def test_a_session_goes_where_you_put_it(self):
        import sessions

        def groups():
            return {g["key"]: [r["key"] for r in g["rows"]] for g in sessions.board()["groups"]}

        made = self.client.post("/api/sessions/sections", data={"title": "  Incident\x07 kafka  "}).get_json()["key"]
        self.assertEqual(sessions._layout()[0][0]["title"], "Incident kafka")
        self.assertEqual(groups()[made], [], "a section of yours shows empty")
        self.assertIn("Drag a session here", self.client.get("/sessions").get_data(as_text=True))
        self.assertEqual(self.client.post("/api/sessions/file", data={"key": "console:sc-1002", "group": made}).status_code, 204)
        self.assertEqual(groups()[made], ["console:sc-1002"], "your move wins over its big rock's ticket")
        self.assertEqual(self.client.post("/api/sessions/file", data={"key": "console:sc-1002", "group": "own:ffffff"}).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions/file", data={"key": "console:../x", "group": "run"}).status_code, 400)
        self.assertEqual(self.client.post("/api/sessions/sections", data={"title": " "}).status_code, 400)
        self.client.post(f"/api/sessions/sections/{made[4:]}", data={"title": "Kafka"})
        self.assertIn('<span class="pn">Kafka</span>', self.client.get("/partials/sessions").get_data(as_text=True))
        self.client.post(f"/api/sessions/sections/{made[4:]}/delete")
        self.assertIn("console:sc-1002", groups()["rock:Mesh"], "its section gone, back under its ticket")
        other = self.client.post("/api/sessions/sections", data={"title": "Spare", "key": "console:sc-1002"}).get_json()["key"]
        self.assertEqual(groups()[other], ["console:sc-1002"], "a section made by a drop takes the session")
        for n in range(sessions.FILED_KEEP + 5):
            sessions._file(f"console:old-{n}", "run")
        self.assertEqual(len(sessions._layout()[1]), sessions.FILED_KEEP)
        self.assertIn(f"console:old-{sessions.FILED_KEEP + 4}", sessions._layout()[1], "the latest moves are kept")

    def test_an_ended_console_gives_its_ticket_back(self):
        def end(name):
            self.sessions = [s for s in self.sessions if s["console"] != name]

        with mock.patch("console.end", side_effect=end), mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            page = self.client.post("/api/sessions/end", data={"key": "console:sc-1002"}).get_data(as_text=True)
            self.sessions.append(session("p", "idle", console="sc-1001-ab12"))
            picked = self.client.post("/api/sessions/end", data={"key": "console:sc-1001-ab12"}).get_data(as_text=True)
        self.assertIn("Pick up", page)
        self.assertIn("Pick up", picked, "a console picked up as sc-N-xxxx gives its ticket back too")
        for key in ("console:../x", "session:../x", "story:1001", "sc-1002"):
            self.assertEqual(self.client.post("/api/sessions/end", data={"key": key}).status_code, 404, key)

    def test_a_session_in_another_terminal_ends_there(self):
        self.sessions.append(session(SID, "idle", {"story": "sc-1001"}))

        def stop(sid):
            self.sessions = [s for s in self.sessions if s["id"] != sid]

        with mock.patch("console.stop", side_effect=stop) as ended, mock.patch("shortcut.story", side_effect=RuntimeError("x")):
            page = self.client.post("/api/sessions/end", data={"key": f"session:{SID}"}).get_data(as_text=True)
        ended.assert_called_once_with(SID)
        self.assertIn("Pick up", page, "its ticket, to pick up again")
        self.sessions.append(session(SID, "idle"))
        with mock.patch("console.stop", side_effect=stop):
            quiet = self.client.post("/api/sessions/end", data={"key": f"session:{SID}", "quiet": "1"})
        self.assertEqual((quiet.status_code, quiet.get_data()), (204, b""), "a row's ×: the pane shows another one")
        with mock.patch("console.stop", side_effect=RuntimeError("Claude (pid 4) still runs 10 s after SIGTERM")):
            self.sessions.append(session(SID, "idle"))
            failed = self.client.post("/api/sessions/end", data={"key": f"session:{SID}", "quiet": "1"}).get_data(as_text=True)
        self.assertIn("still runs 10 s after SIGTERM", failed, "a failure says so, quiet or not")

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



WAITS = "11111111-2222-4333-8444-555555555555"
ANSWERED = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class SessionInboxTest(SentinelTest):
    """A session waiting on you is an inbox row, pushed after a few minutes; one that answered is only the mod's band's."""

    def setUp(self):
        super().setUp()
        import main
        import sessions

        self.client = main.app.test_client()
        self.seed(sessions.WATCH)
        self.now = time.time()
        self.listed, self.error = [], ""
        for p in (mock.patch("config.CLAUDE_BRIDGE_URL", "http://bridge"),
                  mock.patch("sessions.live", side_effect=lambda: (self.listed, self.error))):
            p.start()
            self.addCleanup(p.stop)

    def answered(self, minutes_ago, **over):
        return {"story": "sc-12345", "tokens": 90_000, "answeredAt": (self.now - minutes_ago * 60) * 1000, **over}

    def rows(self):
        import db
        import main

        return {r["external_id"]: main._view(r) for r in db.items("state != 'resolved'")}

    def waited(self, minutes, seen=0):
        import db

        with db.connect() as conn:
            conn.execute("UPDATE items SET first_seen = ?, last_seen = ? WHERE external_id = ?",
                         (db.ago(minutes), db.ago(seen), f"wait:{WAITS}"))

    def test_a_session_waiting_is_yours_after_a_few_minutes_and_pushed_once(self):
        import engine
        import sessions

        self.listed = [session(WAITS, "waiting", waiting_for="input needed", console="sc-12345")]
        sessions.watch()
        row = self.rows()[f"wait:{WAITS}"]
        self.assertFalse(row["attention"], "you may be answering it right now")
        self.assertEqual((row["headline"], row["p"]["why"], row["tone"]), (f"title {WAITS}", "waits for you: input needed", "neutral"))
        self.assertEqual(row["url"].split("/", 3)[-1], "sessions?open=sc-12345", "straight to its console")
        engine.promote_due()
        self.assertEqual(self.pushes, [])
        self.waited(4)
        engine.promote_due()
        engine.promote_due()
        self.assertEqual(self.pushes, [(f"title {WAITS}", "sc-12345 has waited for you 3 min", f"item-{row['id']}")])
        self.assertEqual(self.rows()[f"wait:{WAITS}"]["tone"], "hot")

    def test_a_wait_answered_between_two_polls_starts_over(self):
        import engine
        import sessions

        self.listed = [session(WAITS, "waiting")]
        sessions.watch()
        self.waited(2)
        self.listed = [session(WAITS, "busy")]
        sessions.watch()
        self.assertEqual(self.rows(), {}, "a local list: gone at once")
        self.listed = [session(WAITS, "waiting")]
        sessions.watch()
        self.waited(2)
        engine.promote_due()
        self.assertEqual(self.pushes, [], "a session asking permission after permission, each answered: no push")

    def test_the_bridge_away_neither_ends_a_wait_nor_pushes_it(self):
        import engine
        import sessions

        self.listed = [session(WAITS, "waiting")]
        sessions.watch()
        self.listed, self.error = [], "the Claude bridge: connection refused"
        sessions.watch()
        sessions.watch()
        self.assertIn(f"wait:{WAITS}", self.rows())
        self.waited(4, seen=2)
        engine.promote_due()
        self.assertEqual(self.pushes, [], "answered meanwhile, for all it knows")
        self.listed, self.error = [session(WAITS, "busy")], ""
        sessions.watch()
        engine.promote_due()
        self.assertEqual((self.rows(), self.pushes), ({}, []))

    def test_a_wait_seen_before_sentinel_stopped_is_not_pushed_on_start(self):
        import engine
        import sessions

        self.listed = [session(WAITS, "waiting")]
        sessions.watch()
        self.waited(10, seen=8)  # Sentinel redeployed or the Mac asleep: promote_due runs before the next watch
        engine.promote_due()
        self.assertEqual(self.pushes, [])
        sessions.watch()  # still waiting once looked at again: pushed
        engine.promote_due()
        self.assertEqual(len(self.pushes), 1)

    def test_a_session_that_answered_is_the_bands_never_a_row(self):
        import sessions

        self.listed = [session(ANSWERED, "idle", self.answered(2)), session(WAITS, "idle", self.answered(70, cold=True)),
                       session("77777777-8888-4999-8aaa-bbbbbbbbbbbb", "busy", self.answered(1))]
        sessions.watch()
        self.assertEqual(self.rows(), {})
        [a] = sessions.answered()
        self.assertEqual((a["subject"], a["ref"], a["title"]), (ANSWERED, "sc-12345", f"title {ANSWERED}"))
        self.assertEqual(a["at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 120)))
        self.listed, self.error = [], "the Claude bridge: connection refused"
        sessions.watch()
        self.assertEqual(sessions.answered(), [], "unknown: the band says nothing")

    def test_priority_the_mod_and_settings_list_the_sessions(self):
        import db
        import sessions

        self.listed = [session(WAITS, "waiting", console="sc-12345"), session(ANSWERED, "idle", self.answered(2))]
        sessions.watch()
        with db.connect() as conn:
            conn.execute("UPDATE items SET notified_at = ? WHERE external_id = ?", (db.now(), f"wait:{WAITS}"))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('<div class="grp">Sessions <span>1</span></div>', page)
        data = self.client.get("/api/summary").get_json()
        self.assertEqual([i["subject"] for i in data["items"]][:1], [WAITS], "first")
        self.assertTrue(data["items"][0]["at"])
        self.assertEqual([a["subject"] for a in data["answered"]], [ANSWERED])
        self.assertEqual(data["attention"], 1)
        detail = self.client.get(f"/partials/item/{data['items'][0]['id']}").get_data(as_text=True)
        self.assertIn("Open in Sessions", detail)
        self.assertEqual(detail.count(">sc-12345<"), 2, "the top line and the ticket, not a third time as the session")
        self.assertIn(sessions.WATCH, self.client.get("/settings").get_data(as_text=True))
