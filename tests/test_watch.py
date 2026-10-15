"""Watch: curves read back from dashboards, Zabbix problems, and the page that shows them."""
from unittest import mock

import requests

from helpers import SentinelTest

WINDOW = 360


def series(name, values):
    # one more point than analysed: Graphite's latest point is still filling up and is dropped
    return {"target": name, "datapoints": [[v, i] for i, v in enumerate(values + [values[-1]])]}


class DetectionTest(SentinelTest):
    def found(self, name, values):
        import quart

        return quart.verdicts([series(name, values)], WINDOW)

    def test_a_counter_that_stops(self):
        [a] = self.found("stats_counts.app.pulled.all", [50.0] * 110 + [0.0] * 9)
        self.assertEqual(a["kind"], "stop")
        self.assertIn("stopped for 30 min", a["text"])

    def test_the_last_point_still_filling_up_is_not_a_stop(self):
        values = [50.0] * 119
        s = {"target": "stats_counts.app.pulled.all", "datapoints": [[v, i] for i, v in enumerate(values + [0.0])]}
        import quart

        self.assertEqual(quart.verdicts([s], WINDOW), [])

    def test_a_whole_number_gauge_that_changes(self):
        [a] = self.found("cluster.health.status", [3.0] * 110 + [2.0] * 9)
        self.assertEqual((a["kind"], a["discrete"]), ("state", True))
        self.assertEqual(a["text"], "went from 3 to 2")

    def test_a_small_step_of_a_rate_is_not_a_change(self):
        self.assertEqual(self.found("app.ping.all.count", [42.67] * 110 + [46.55] * 9), [])

    def test_a_counter_stepping_by_a_few_is_not_a_state_change(self):
        import quart

        # seen on a real ping counter behind a …all.* target: 32 → 29 is whole numbers, but a count, not a status
        self.assertEqual(quart.verdicts([series("app.ping.all.count", [32.0] * 110 + [29.0] * 9)], WINDOW), [])
        [a] = quart.verdicts([series("app.ping.all.count", [32.0] * 110 + [12.0] * 9)], WINDOW)
        self.assertEqual((a["kind"], a["discrete"], a["volume"]), ("state", False, True))
        # a panel whose targets count things says so for curves an alias renamed
        self.assertEqual(quart.verdicts([series("ping", [32.0] * 110 + [29.0] * 9)], WINDOW, volume=True), [])

    def test_a_drift_out_of_the_usual_band(self):
        base = [100.0 + i % 5 for i in range(110)]
        [a] = self.found("app.delay.all.upper", base + [420.0] * 9)
        self.assertEqual(a["kind"], "drift")

    def test_noise_inside_the_band_is_not_a_drift(self):
        values = [100.0 + (i * 37) % 40 for i in range(119)]
        self.assertEqual(self.found("app.delay.all.upper", values), [])

    def test_statsd_aggregates_nobody_watches_are_ignored(self):
        self.assertEqual(self.found("app.delay.all.lower", [50.0] * 110 + [0.0] * 9), [])

    def test_dashboard_links_and_template_variables(self):
        import quart

        base, uid, variables = quart.parse_dashboard_url("https://grafana.example.com/d/abc12/pipeline?orgId=1&var-a=$__all&var-b=x&var-b=y")
        self.assertEqual((base, uid), ("https://grafana.example.com", "abc12"))
        self.assertEqual(variables, {"a": ["$__all"], "b": ["x", "y"]})
        self.assertEqual(quart.substitute("app.$a.[[b]].${c}.count", variables), "app.*.{x,y}.${c}.count")


def rpc(results):
    def post(url, json, **kwargs):
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        result = post.triggers.pop(0) if json["method"] == "trigger.get" else results[json["method"]]
        resp.json = lambda: {"jsonrpc": "2.0", "result": result, "id": 1}
        post.bodies.append(json)
        return resp

    post.bodies = []
    post.triggers = []
    return post


class ZabbixTest(SentinelTest):
    def watcher(self):
        from watchers.zabbix import ZabbixWatcher

        with mock.patch.dict("os.environ", {"ZABBIX_TOKEN": "tok"}):
            return ZabbixWatcher({"url": "https://zabbix.example.com/api_jsonrpc.php", "web_url": "https://zabbix.example.com",
                                  "quiet": ["bandwidth usage"]})

    def test_problems_become_incidents(self):
        import quart

        def problem(eventid, objectid, name, severity, acknowledged="0"):
            return {"eventid": eventid, "objectid": objectid, "name": name, "severity": str(severity),
                    "clock": "1700000000", "acknowledged": acknowledged}

        post = rpc({
            "problem.get": [problem("1", "t1", "Consumer delay above 1h", 4), problem("2", "t2", "Interface bandwidth usage too high", 3),
                            problem("3", "t3", "Disk space is low", 3, acknowledged="1"), problem("4", "t4", "Consumer delay above 5m", 2),
                            problem("5", "t5", "Supervisor is not running", 3, acknowledged="1")],
        })
        # t5's host is disabled; t4 depends on t1: Zabbix (and skipDependent) leave it out
        post.triggers = [[{"triggerid": t} for t in ("t1", "t2", "t3", "t4")],
                         [{"triggerid": t, "hosts": [{"host": f"{t}.example.net"}]} for t in ("t1", "t2", "t3")]]
        with mock.patch("requests.post", side_effect=post):
            items = self.watcher().fetch()

        # known noise stays on the Watch page, out of the inbox
        self.assertEqual([(i.external_id, i.urgency, i.lane) for i in items], [("1", "now", "incident")])
        self.assertEqual(items[0].url, "https://zabbix.example.com/tr_events.php?triggerid=t1&eventid=1")
        self.assertEqual([(r["name"], r["known"]) for r in quart.snapshot()["zabbix"]["problems"]],
                         [("Consumer delay above 1h", False), ("Interface bandwidth usage too high", True)])
        self.assertEqual(post.bodies[0]["auth"], "tok")
        self.assertTrue(post.bodies[2]["params"]["skipDependent"])
        snap = quart.snapshot()["zabbix"]
        self.assertEqual((snap["acked"], snap["dependent"], snap["disabled"]), (1, 1, 1))

    def test_an_api_error_fails_the_read(self):
        def post(url, json, **kwargs):
            resp = mock.Mock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"jsonrpc": "2.0", "error": {"message": "Invalid params.", "data": "Not authorized."}, "id": 1}
            return resp

        with mock.patch("requests.post", side_effect=post), self.assertRaises(RuntimeError):
            self.watcher().fetch()


DASHBOARD = {"dashboard": {"title": "Pipeline", "templating": {"list": [{"name": "source", "current": {"value": "web"}}]}, "panels": [
    {"type": "row", "panels": [
        {"id": 1, "title": "Pulled", "type": "timeseries", "datasource": {"type": "graphite", "uid": "g1"},
         "targets": [{"target": "aliasByNode(stats_counts.app.pulled.$source, 3)"}]},
        {"id": 3, "title": "Accepted", "type": "timeseries", "datasource": {"type": "graphite", "uid": "g1"},
         "targets": [{"target": "stats_counts.app.accepted.all"}]},
    ]},
    {"id": 2, "title": "Logs", "type": "logs", "datasource": {"type": "loki", "uid": "l1"}, "targets": [{"expr": "{app=\"x\"}"}]},
]}}
QUIET = {"dashboard": {"title": "Other", "panels": []}}


class GrafanaTest(SentinelTest):
    def setUp(self):
        super().setUp()
        from watchers.grafana import GrafanaWatcher

        with mock.patch.dict("os.environ", {"GRAFANA_TOKEN": "tok"}):
            self.w = GrafanaWatcher({"dashboards": ["https://grafana.example.com/d/abc/pipeline?var-source=$__all",
                                                    "https://grafana.example.com/d/other/x"]})
        self.renders = []
        self.down = False
        self.panel_down = False
        self.accepted_stops = False

    def get(self, url, params=None, timeout=None):
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        if url.endswith("/api/dashboards/uid/abc"):
            if self.down:
                raise requests.ConnectionError("unreachable")
            resp.json = lambda: DASHBOARD
        elif url.endswith("/api/dashboards/uid/other"):
            resp.json = lambda: QUIET
        elif ("target", "stats_counts.app.accepted.all") in params:
            if self.panel_down:
                raise requests.HTTPError("500 Server Error")
            resp.json = lambda: [series("accepted", [50.0] * 110 + ([0.0] if self.accepted_stops else [50.0]) * 9)]
        else:
            self.renders.append(params)
            resp.json = lambda: [series("total", [50.0] * 110 + [0.0] * 9)]
        return resp

    def fetch(self):
        with mock.patch("requests.Session.get", side_effect=self.get):
            return self.w.fetch()

    def test_a_stop_is_pushed_on_the_second_read_only(self):
        import quart

        self.assertEqual(self.fetch(), [], "one read alone pushes nothing")
        card = quart.snapshot()["grafana"]["dashboards"][0]
        self.assertEqual((card["panels"], card["series"], card["anomalies"][0]["confirmed"]), (2, 2, False))
        self.assertIn(("target", "aliasByNode(stats_counts.app.pulled.*, 3)"), self.renders[0], "the link's value wins over the saved one")

        [item] = self.fetch()
        self.assertEqual((item.urgency, item.lane, item.fingerprint), ("now", "incident", "stop"))
        self.assertEqual(item.payload["dashboard"], "Pipeline")

    def test_an_unreadable_dashboard_keeps_what_was_known(self):
        import quart

        self.fetch()
        [kept] = self.fetch()
        self.down = True
        self.assertEqual(self.fetch(), [kept])
        self.assertIn("unreachable", quart.snapshot()["grafana"]["dashboards"][0]["error"])

    def test_one_unreadable_panel_keeps_its_items_and_the_rest_is_read(self):
        import quart

        self.accepted_stops = True
        self.fetch()
        self.assertEqual(len(self.fetch()), 2)
        self.panel_down = True
        items = self.fetch()
        self.assertEqual(sorted(i.payload["panel"] for i in items), ["Accepted", "Pulled"])
        self.assertEqual(quart.snapshot()["grafana"]["dashboards"][0]["unread"], 1)

    def test_saved_variable_values_fill_a_link_that_names_none(self):
        self.w.dashboards = ["https://grafana.example.com/d/abc/pipeline"]
        self.fetch()
        self.assertIn(("target", "aliasByNode(stats_counts.app.pulled.web, 3)"), self.renders[0])

    def test_no_dashboard_readable_fails_the_read(self):
        self.w.dashboards = self.w.dashboards[:1]
        self.down = True
        with self.assertRaises(RuntimeError):
            self.fetch()


class WatchPageTest(SentinelTest):
    def setUp(self):
        super().setUp()
        import config

        cfg = config.load()
        cfg["watchers"]["zabbix"] = {"enabled": True, "url": "https://zabbix.example.com/api_jsonrpc.php"}
        self._patches.append(mock.patch.object(config, "load", return_value=cfg))
        self._patches[-1].start()

    def test_the_page_shows_problems_and_dashboards(self):
        import main
        import quart

        now = __import__("time").time()
        quart.publish("zabbix", {"read_at": now, "acked": 1, "dependent": 2, "disabled": 0, "web_url": "https://zabbix.example.com", "problems": [
            {"host": "kafka-1.example.net", "name": "Consumer delay above 1h", "severity": "high", "level": 4, "since": now - 3600,
             "known": False, "url": "https://zabbix.example.com/tr_events.php?triggerid=1&eventid=2"},
            {"host": "kafka-2.example.net", "name": "Interface bandwidth usage too high", "severity": "average", "level": 3,
             "since": now - 7200, "known": True, "url": "https://zabbix.example.com/tr_events.php?triggerid=3&eventid=4"},
        ]})
        spark = quart.sparkline([50.0] * 110 + [0.0] * 9)
        quart.publish("grafana", {"read_at": now, "window_minutes": 360, "dashboards": [
            {"uid": "abc", "title": "Pipeline", "link": "https://grafana.example.com/d/abc/pipeline", "error": None, "panels": 3, "series": 40,
             "anomalies": [{"kind": "stop", "series": "total", "text": "stopped for 30 min (≈ 50 before)", "panel": "Pulled",
                            "panel_id": 1, "volume": True, "discrete": False, "confirmed": True, "spark": spark}]},
            {"uid": "other", "title": "Other", "link": "https://grafana.example.com/d/other/x", "error": None, "panels": 2, "series": 5, "anomalies": []},
        ]})
        client = main.app.test_client()
        page = client.get("/watch").get_data(as_text=True)
        for text in ("Consumer delay above 1h", "1 known, listed without pushing", "1 acknowledged · 2 hidden behind the problem they depend on",
                     "Zabbix read just now",
                     "Pipeline", "stopped for 30 min", "nothing changed", 'href="/watch"', "2 to look at"):
            self.assertIn(text, page)
        self.assertEqual(client.get("/partials/watch").status_code, 200)

    def test_first_read_in_progress(self):
        import main
        import quart

        quart.publish("zabbix", None)
        quart.publish("grafana", None)
        self.assertIn("First read in progress", main.app.test_client().get("/watch").get_data(as_text=True))
