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

    def test_update_while_running_is_explained(self):
        # Simulate a toolkit folder whose files are replaced while the server runs.
        code = self.tmp / "toolkit"
        code.mkdir()
        (code / "server.py").write_text("old")
        (code / "__pycache__").mkdir()
        saved = ui.CODE_DIR
        ui.CODE_DIR = code
        try:
            server = ui.make_server(self.state, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                def status():
                    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
                    conn.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{server.port}",
                                                                "X-Hbkit-Token": server.token})
                    res = conn.getresponse()
                    body = json.loads(res.read())
                    conn.close()
                    return res.status, body

                self.assertEqual(status()[0], 200)
                (code / "__pycache__" / "server.cpython-313.pyc").write_text("cache")  # Python's own caches
                self.assertEqual(status()[0], 200)
                (code / "server.py").write_text("new version")
                code_, body = status()
                self.assertEqual(code_, 409)
                self.assertIn("start hb-ui.cmd again", body["error"])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            ui.CODE_DIR = saved

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

    def test_rule_group_transactions(self):
        from urllib.parse import quote

        run = self.api("/api/run", {"analysis": "rule-mining", "params": {}})["run"]
        review = {r[1]: r for r in self.api(f"/api/table?run={run}&name=review.csv")["rows"]}
        with open(self.out / run / "rule_transactions.csv", encoding="utf-8-sig", newline="") as fh:
            written = list(csv.DictReader(fh))
        rule_id, row = next((k, r) for k, r in review.items() if r[2] == "WOOLWORTHS")
        text, category = row[2], row[3]

        def matches(text, category, run=run):
            return self.api(f"/api/review/matches?run={quote(run)}&text={quote(text)}&category={quote(category)}")

        res = matches(text, category)
        self.assertEqual(res["header"][-1], "pattern")
        self.assertEqual(res["field"], "memo")
        self.assertTrue(res["category_found"])
        self.assertFalse(res["file_changed"])
        same = [w for w in written if w["rule_id"] == rule_id]
        self.assertEqual(len(res["rows"]), len(same))  # the same list as the file written by the run
        self.assertEqual(sorted(r[2] for r in res["rows"]), sorted(w["memo"] for w in same))
        self.assertGreater(len({r[-1] for r in res["rows"]}), 2)  # several branches: several description groups

        narrower = matches("WOOLWORTHS SANDTON", category)  # an edited text is matched on the spot
        self.assertLess(len(narrower["rows"]), len(res["rows"]))
        self.assertTrue(all("SANDTON" in r[2].upper() for r in narrower["rows"]))
        self.assertFalse(matches(text, "No such category")["category_found"])
        self.assertEqual(matches("", category)["rows"], [])

        self.src.write_bytes(sample(seed=3).data)  # the file changed since the run
        self.assertTrue(matches(text, category)["file_changed"])
        self.assertEqual(self.request("GET", "/api/review/matches?run=../..&text=x")[0], 403)
        self.assertEqual(self.request("GET", "/api/review/matches?run=rule-mining/none&text=x")[0], 404)

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


class ImportPageTests(ServerTestCase):
    """The imports page: month, files, accounts, preview and prepared files (made-up files only)."""

    def setUp(self):
        super().setUp()
        self.src.write_bytes(sample(home_loans=True).data)

    def upload(self, month, name, raw):
        import base64

        return self.request("POST", "/api/import/upload",
                            {"month": month, "name": name, "content": base64.b64encode(raw).decode()})

    def test_month_flow(self):
        from hbkit.fixtures.generator import ACC_LOAN_A, ACC_LOAN_B

        from .test_import_prepare import LOAN_A

        info = self.api("/api/import/month?month=2025-10")
        accounts = {a["key"]: a for a in info["accounts"]}
        self.assertTrue(accounts[ACC_LOAN_A]["expected"])
        self.assertEqual((info["files"], info["expected_chosen"]), ([], False))

        status, res, _ = self.upload("2025-10", "Loan_A.qif", LOAN_A)
        self.assertEqual((status, res["name"]), (200, "Loan_A.qif"))
        (f,) = self.api("/api/import/month?month=2025-10")["files"]
        self.assertEqual((f["account"], f["suggested"], f["transactions"]), (None, ACC_LOAN_A, 4))
        self.assertTrue(f["reason"].startswith("from its contents"))

        self.api("/api/import/link", {"month": "2025-10", "file": "Loan_A.qif", "account": ACC_LOAN_A})
        info = self.api("/api/import/month?month=2025-10")
        self.assertEqual(info["files"][0]["account"], ACC_LOAN_A)
        self.assertEqual({a["key"]: a["files"] for a in info["accounts"]}[ACC_LOAN_A],
                         [{"name": "Loan_A.qif", "prepared": False}])

        preview = self.api(f"/api/import/preview?month=2025-10&file=Loan_A.qif&account={ACC_LOAN_A}")
        self.assertEqual([r["suggested"] for r in preview["rows"]][2], "Property A:Bond interest")
        other = self.api(f"/api/import/preview?month=2025-10&file=Loan_A.qif&account={ACC_LOAN_B}")
        self.assertEqual([r["suggested"] for r in other["rows"]][2], "Property B:Bond interest")

        done = self.api("/api/import/prepare", {"month": "2025-10", "file": "Loan_A.qif", "account": ACC_LOAN_A,
                                                "categories": {"1": "Property A:Insurance", "2": "Property A:Bond interest"}})
        self.assertEqual((done["categorised"], done["rel"]), (2, "imports/2025-10/prepared/Loan_A-prepared.qif"))
        prepared = self.out / done["rel"]
        self.assertIn(b"LProperty A:Bond interest", prepared.read_bytes())
        self.assertEqual((self.out / "imports/2025-10/originals/Loan_A.qif").read_bytes(), LOAN_A)
        memory = json.loads((self.data / "import-accounts.json").read_text())
        (link,) = memory["links"]
        self.assertEqual((link["account"], link["categorised"], link["file"]), (ACC_LOAN_A, 2, "Loan_A.qif"))

        # Next month: the same file name is linked to the same account.
        self.upload("2025-11", "Loan_A.qif", LOAN_A.replace(b"/10/2025", b"/11/2025"))
        (f,) = self.api("/api/import/month?month=2025-11")["files"]
        self.assertEqual((f["suggested"], f["reason"]), (ACC_LOAN_A, "same file name as in 2025-10"))
        record = self.api("/api/import/month?month=2025-11")["record"]
        self.assertEqual([r["month"] for r in record], ["2025-10"])

        self.api("/api/import/expected", {"accounts": [ACC_LOAN_B]})
        info = self.api("/api/import/month?month=2025-11")
        self.assertTrue(info["expected_chosen"])
        self.assertEqual([a["key"] for a in info["accounts"] if a["expected"]], [ACC_LOAN_B])

        self.api("/api/import/remove", {"month": "2025-10", "file": "Loan_A.qif"})
        self.assertFalse(prepared.exists())
        self.assertEqual(self.api("/api/import/month?month=2025-10")["files"], [])

    def test_bad_requests(self):
        from .test_import_prepare import LOAN_A

        status, res, _ = self.upload("2025-10", "../../outside.qif", LOAN_A)  # stays in the month folder
        self.assertEqual((status, res["name"]), (200, "outside.qif"))
        self.assertTrue((self.out / "imports/2025-10/originals/outside.qif").is_file())
        self.assertEqual(self.upload("2025-13", "a.qif", LOAN_A)[0], 400)
        self.assertEqual(self.upload("2025-10", "notes.csv", b"hello\n")[0], 400)
        status, _, _ = self.request("POST", "/api/import/upload", {"month": "2025-10", "name": "a.qif", "content": "%%"})
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/import/preview?month=2025-10&file=../review.csv&account=7")[0], 400)
        self.assertEqual(self.request("GET", "/api/import/preview?month=2025-10&file=outside.qif&account=999")[0], 400)
        status, res, _ = self.request("POST", "/api/import/prepare", {
            "month": "2025-10", "file": "outside.qif", "account": 7, "categories": {"2": "No such category"}})
        self.assertEqual(status, 400)
        self.assertIn("does not exist", res["error"])
        self.assertEqual(self.request("POST", "/api/import/link", {"month": "2025-10", "file": "outside.qif",
                                                                   "account": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/import/expected", {"accounts": "7"})[0], 400)
