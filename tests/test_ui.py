"""The browser interface's local server: security checks and every API call."""

import csv
import http.client
import importlib.util
import json
import threading
from pathlib import Path

from hbkit.ui import server as ui
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample


class ServerTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.src = self.data / "finances.xhb"
        self.src.write_bytes(sample().data)
        self.out = self.tmp / "out"
        self.state = ui.UiState(out_root=self.out, data_dir=self.data)
        self.server = ui.make_server(self.state, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opened = []
        self._opener = ui.open_in_file_manager
        ui.open_in_file_manager = self.opened.append

    def tearDown(self):
        ui.open_in_file_manager = self._opener
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, token=True, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if token:
            hdrs["X-Hbkit-Token"] = self.server.token if token is True else token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data.decode("utf-8", "replace")
        return res.status, parsed, res

    def api(self, path, body=None):
        status, data, _ = self.request("POST" if body is not None else "GET", path, body)
        self.assertEqual(status, 200, data)
        return data


class SecurityTests(ServerTestCase):
    def test_listens_on_this_computer_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertIn("127.0.0.1", self.server.url)
        self.assertIn("#token=", self.server.url)  # key in the fragment, never sent to other sites

    def test_key_required(self):
        self.assertEqual(self.request("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/status", token="wrong")[0], 403)
        self.assertEqual(self.request("GET", "/api/status")[0], 200)

    def test_other_host_names_refused(self):  # DNS rebinding
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/status", host=f"evil.example:{self.server.port}")[0], 403)
        self.assertEqual(self.request("GET", "/", host=f"localhost:{self.server.port}")[0], 200)

    def test_requests_from_other_web_pages_refused(self):
        status, _, _ = self.request("GET", "/api/status", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_json_only_for_changes(self):
        status, _, _ = self.request("POST", "/api/run", body=None, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_paths_stay_in_the_output_folder(self):
        for run in ("..", "../data", "/etc", "C:/Windows"):
            status, _, _ = self.request("GET", f"/api/table?run={run}&name=review.csv")
            self.assertIn(status, (403, 404), run)
        status, _, _ = self.request("GET", "/api/table?run=x&name=../../finances.xhb")
        self.assertIn(status, (400, 404))
        status, _, _ = self.request("POST", "/api/open", body={"path": "../data"})
        self.assertEqual(status, 403)
        self.assertEqual(self.opened, [])

    def test_static_files_and_headers(self):
        status, body, res = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn("HomeBank toolkit", body)
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        for name in ("app.js", "style.css", "icon.svg"):
            self.assertEqual(self.request("GET", f"/static/{name}", token=False)[0], 200)
        self.assertEqual(self.request("GET", "/static/../server.py", token=False)[0], 404)

    def test_page_has_no_external_resources(self):
        for name in ("index.html", "app.js", "style.css"):
            text = (ui.STATIC / name).read_text(encoding="utf-8")
            self.assertNotIn("http://", text.replace("http://127.0.0.1", ""), name)
            self.assertNotIn("https://", text, name)

    def test_page_never_inserts_file_text_as_html(self):
        js = (ui.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)


class ApiTests(ServerTestCase):
    def test_status_and_file(self):
        status = self.api("/api/status")
        self.assertEqual([f["name"] for f in status["data_files"]], ["finances.xhb"])
        self.assertEqual(status["selected"]["name"], "finances.xhb")  # the only file is chosen
        info = self.api("/api/structure")
        self.assertEqual(info["facts"]["transactions"], sample().transactions)
        self.assertEqual(info["facts"]["written_by"], "5.10.3")
        self.assertIn("HomeBank file structure report", info["report"][0])

    def test_select_other_file(self):
        other = self.tmp / "other.xhb"
        other.write_bytes(sample(seed=3).data)
        data = self.api("/api/select", {"path": f'"{other}"'})  # quotes from "Copy as path" are fine
        self.assertEqual(data["file"]["name"], "other.xhb")
        status, data, _ = self.request("POST", "/api/select", {"path": str(self.tmp / "missing.xhb")})
        self.assertEqual(status, 400)
        not_xhb = self.tmp / "notes.txt"
        not_xhb.write_text("x")
        self.assertEqual(self.request("POST", "/api/select", {"path": str(not_xhb)})[0], 400)
        self.assertEqual(self.api("/api/status")["selected"]["name"], "other.xhb")

    def test_analyses_listed_with_options(self):
        analyses = {a["name"]: a for a in self.api("/api/analyses")}
        self.assertIn("rule-mining", analyses)
        params = {p["name"]: p for p in analyses["rule-mining"]["params"]}
        self.assertEqual(params["min_count"]["default"], 5)
        self.assertEqual(params["field"]["choices"], ["memo", "payee"])

    def test_bad_option_value(self):
        status, data, _ = self.request("POST", "/api/run", {"analysis": "rule-mining", "params": {"min_count": "abc"}})
        self.assertEqual(status, 400)
        self.assertIn("min_count", data["error"])
        self.assertEqual(self.request("POST", "/api/run", {"analysis": "nope"})[0], 404)

    def test_full_rule_review_flow(self):
        before = self.src.read_bytes()
        res = self.api("/api/run", {"analysis": "rule-mining", "params": {"min_count": "5", "merge": True}})
        run = res["run"]
        self.assertTrue(run.startswith("rule-mining/"))
        labels = [r["label"] for r in res["summary"]["rows"]]
        self.assertIn("Suggested rules", [label.strip() for label in labels])
        self.assertNotIn("python -m hbkit apply-rules --review <that folder>", labels)  # terminal-only hint

        runs = self.api("/api/runs")
        self.assertEqual(runs[0]["run"], run)
        self.assertIn("review.csv", runs[0]["files"])
        table = self.api(f"/api/table?run={run}&name=review.csv")
        self.assertEqual(table["header"][0], "approve")
        ids = [row[1] for row in table["rows"]]
        existing = self.api(f"/api/table?run={run}&name=existing_rules.csv")
        self.assertEqual(len(existing["rows"]), 10)
        cats = self.api("/api/categories")
        self.assertIn("Transport:Fuel", cats)

        edits = [
            {"rule_id": ids[0], "approve": True},
            {"rule_id": ids[1], "approve": True, "rule_text": "MY OWN TEXT", "category": "Transport:Fuel"},
        ]
        saved = self.api("/api/review/save", {"run": run, "edits": edits})
        self.assertEqual(saved["approved"], 2)
        with open(self.out / run / "review.csv", encoding="utf-8-sig", newline="") as fh:
            rows = {r["rule_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(rows[ids[1]]["rule_text"], "MY OWN TEXT")
        self.assertEqual(rows[ids[1]]["approve"], "y")

        applied = self.api("/api/review/apply", {"run": run, "edits": edits})
        self.assertTrue(applied["output"].endswith(".xhb"))
        new = read_bytes(Path(applied["output"]).read_bytes())
        self.assertEqual(len(new.rules), 12)
        self.assertIn(("MY OWN TEXT", "Transport:Fuel"),
                      [(r.search, new.category_path(r.category_key)) for r in new.rules])
        self.assertEqual(self.src.read_bytes(), before)  # the original is never changed

        self.api("/api/open", {"path": applied["output"]})
        self.assertEqual(self.opened, [self.out.resolve()])

    def test_export_sqlite(self):
        res = self.api("/api/export-sqlite", {})
        self.assertTrue((self.out / "homebank.sqlite").is_file())
        self.assertIn("transactions", [r["label"] for r in res["summary"]["rows"]])

    def test_file_changes_are_picked_up(self):
        self.api("/api/structure")
        self.src.write_bytes(sample(years=2).data)
        info = self.api("/api/structure")
        self.assertEqual(info["facts"]["first_year"], 2020)
        self.assertEqual(info["facts"]["last_year"], 2021)

    def test_no_file_chosen(self):
        state = ui.UiState(out_root=self.out, data_dir=self.tmp / "empty")
        with self.assertRaises(Exception):
            state.book()


class CommandTests(TempDirTestCase):
    def test_ui_command_exists(self):
        from hbkit.cli import build_parser

        args = build_parser().parse_args(["ui", "--no-browser", "--port", "0"])
        self.assertTrue(args.no_browser)


class ReportsApiTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        from hbkit.fixtures.generator import write_cpi

        self.src.write_bytes(sample(reporting=True).data)
        write_cpi(self.tmp / "cpi.csv")
        settings = self.tmp / "settings.toml"
        settings.write_text('[period]\nyear_mode = "tax"\n[accounts]\nexclude = ["Business account"]\n'
                            '[groups."tax: medical"]\ncategories = ["Medical"]\n[inflation]\ncpi_file = "cpi.csv"\n',
                            encoding="utf-8")
        self.state.settings_path = settings

    def test_list_and_settings(self):
        info = self.api("/api/reports")
        self.assertEqual({r["name"] for r in info["reports"]},
                         {"annual-overview", "category-trends", "tax-year-statement"})
        self.assertEqual(info["settings"]["year_mode"], "tax")
        self.assertEqual(info["settings"]["groups"], ["tax: medical"])
        self.assertTrue(info["settings"]["cpi_file"].endswith("cpi.csv"))
        self.assertEqual(info["files"], [])

    def test_run_and_open(self):
        if not importlib.util.find_spec("plotly"):
            self.skipTest("Plotly is not installed")
        res = self.api("/api/report/run", {"report": "tax-year-statement", "year": "2025", "params": {"groups": ""}})
        self.assertTrue(res["output"].endswith(".html"))
        self.assertIn("Report", [r["label"] for r in res["summary"]["rows"]])
        res2 = self.api("/api/report/run", {"report": "annual-overview", "from": "2023", "to": ""})
        self.assertIn("annual-overview_2022-23_to_", res2["output"])
        self.assertEqual(len(self.api("/api/reports")["files"]), 2)
        self.api("/api/open", {"path": res["output"], "file": True})
        self.assertEqual(self.opened, [Path(res["output"])])

    def test_bad_requests(self):
        self.assertEqual(self.request("POST", "/api/report/run", {"report": "nope"})[0], 404)
        status, data, _ = self.request("POST", "/api/report/run", {"report": "annual-overview", "year": "abc"})
        self.assertEqual(status, 400)
        # only report pages and CSV files can be opened, and only inside the output folder
        (self.out).mkdir(parents=True, exist_ok=True)
        (self.out / "x.txt").write_text("x")
        self.assertEqual(self.request("POST", "/api/open", {"path": "x.txt", "file": True})[0], 404)
        self.assertEqual(self.request("POST", "/api/open", {"path": str(self.src), "file": True})[0], 403)
        self.assertEqual(self.opened, [])

    def test_settings_problem_is_reported(self):
        self.state.settings_path.write_text("[period\n", encoding="utf-8")
        info = self.api("/api/reports")
        self.assertIn("not valid TOML", info["settings"]["error"])

    def test_export_includes_report_views(self):
        import sqlite3

        self.api("/api/export-sqlite", {})
        conn = sqlite3.connect(self.out / "homebank.sqlite")
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM r_period_totals").fetchone()[0], 0)
        conn.close()
