from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from embodied_portfolio.cli import main
from embodied_portfolio.contracts import PortfolioError
from embodied_portfolio.decision import PortfolioService


TARGET = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}


def offer_body(package: str, *, unit_price: str, license_term: str, path: str = "teleoperation",
               provider: str | None = None,
               ids: list[str] | None = None) -> dict:
    ids = ids or ["ep_01", "ep_02", "ep_03", "ep_04"]
    return {"provider": provider or f"prov_{package}", "package": package, "path": path,
            "robots": ["arm_a"],
            "observations": [{"slice": TARGET, "count": len(ids), "episode_ids": ids}],
            "hours": "1", "price_mode": "flat", "unit_price": unit_price,
            "license": "nonexclusive", "allowed_clients": [],
            "validity": "monthly" if license_term == "subscription" else "indefinite",
            "notes": "", "license_term": license_term}


def requirement_body(*, budget: str = "130", phases: int = 3) -> dict:
    return {"key": "phased_req", "client": "client_a", "title": "分阶段训练采购",
            "budget": budget, "phases": phases,
            "targets": [{"slice": TARGET, "minimum": 4, "mandatory": True, "weight": "1"}],
            "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 4}


class PhasedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "phased.db"
        self.service = PortfolioService(self.path)
        self.addCleanup(self.service.close)
        self.buyout = offer_body("buy_pkg", unit_price="120", license_term="perpetual")
        self.subscription = offer_body(
            "sub_pkg", unit_price="50", license_term="subscription", path="portable",
            provider="prov_sub", ids=["s1", "s2", "s3", "s4"])
        self.service.import_requirement(requirement_body())
        self.service.import_offer(self.buyout)
        self.service.import_offer(self.subscription)

    def test_plan_report_contains_phase_schedule_and_prefers_buyout(self):
        analysis = self.service.plan("phased_req")
        report = analysis["report"]
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["phases"], 3)
        self.assertEqual(report["selection"], ["buy_pkg"])
        schedule = report["schedule"]
        self.assertEqual([period["period_cost"] for period in schedule["periods"]],
                         ["120.00", "0.00", "0.00"])
        self.assertEqual(schedule["total_cost"], "120.00")
        self.assertTrue(schedule["every_period_within_budget"])
        self.assertEqual(len(schedule["exit_options"]), 3)
        # 旧的一次性 quotes 口径仍保留（第 1 期口径），不受分期影响。
        self.assertEqual(report["quotes"][0]["amount"], "120.00")

    def test_plan_marks_subscription_infeasible_when_period_budget_too_tight(self):
        self.service.import_requirement(requirement_body(budget="40"))
        analysis = self.service.plan("phased_req")
        self.assertFalse(analysis["report"]["feasible"])
        self.assertEqual(analysis["report"]["search"]["reason"],
                         "all_combinations_exceed_budget")

    def test_schedule_command_for_explicit_subscription_selection(self):
        schedule = self.service.schedule("phased_req", ["sub_pkg"])
        self.assertEqual(schedule["total_cost"], "150.00")
        self.assertEqual([period["period_cost"] for period in schedule["periods"]],
                         ["50.00", "50.00", "50.00"])
        self.assertEqual(schedule["exit_options"][0]["stoppable_recurring_cost"], "100.00")
        self.assertEqual(schedule["exit_options"][0]["lapsed_packages"], ["sub_pkg"])

    def test_schedule_defaults_to_recommended_selection(self):
        schedule = self.service.schedule("phased_req")
        self.assertEqual(schedule["selected_packages"], ["buy_pkg"])

    def test_schedule_rejects_unknown_and_empty_selection(self):
        with self.assertRaises(PortfolioError) as caught:
            self.service.schedule("phased_req", ["missing_pkg"])
        self.assertEqual(caught.exception.code, "unknown_offer")
        with self.assertRaises(PortfolioError) as caught:
            self.service.schedule("phased_req", [])
        self.assertEqual(caught.exception.code, "empty_selection")

    def test_renewal_failure_service_reflects_real_rights_and_gap(self):
        result = self.service.renewal_failure(
            "phased_req", ["sub_pkg"], "sub_pkg", 2)
        self.assertEqual(result["rights_state"], "lapsed_from_failure_phase")
        periods = result["schedule"]["periods"]
        self.assertEqual(periods[0]["active_packages"], ["sub_pkg"])
        self.assertEqual(periods[1]["active_packages"], [])
        self.assertEqual(result["failure_period_gap"]["total_shortage"], 4)
        self.assertEqual(result["recovery"]["replacement_packages"], ["buy_pkg"])

    def test_compare_packages_shows_phase_costs_for_both_terms(self):
        comparison = self.service.compare_packages(
            "phased_req", [["buy_pkg"], ["sub_pkg"]])
        buy_row, sub_row = comparison["portfolios"]
        self.assertEqual(buy_row["period_costs"], ["120.00", "0.00", "0.00"])
        self.assertEqual(sub_row["period_costs"], ["50.00", "50.00", "50.00"])
        self.assertTrue(buy_row["every_period_within_budget"])
        self.assertTrue(sub_row["every_period_within_budget"])

    def test_compare_packages_feasible_uses_every_period_budget(self):
        # 订阅每期 50；把每期预算压到 40，首期和后续期都超预算，组合不可行。
        self.service.import_requirement(requirement_body(budget="40"))
        comparison = self.service.compare_packages("phased_req", [["sub_pkg"]])
        row = comparison["portfolios"][0]
        self.assertFalse(row["every_period_within_budget"])
        self.assertFalse(row["feasible"])
        self.assertFalse(row["period_one_within_budget"])

    def test_approved_snapshot_reproduces_schedule_after_price_revision(self):
        analysis = self.service.plan("phased_req")
        self.service.approve(analysis["id"], analysis["input_digest"])
        # 供应商事后调价：订阅涨到 80/期。
        self.service.import_offer(offer_body(
            "sub_pkg", unit_price="80", license_term="subscription", path="portable",
            provider="prov_sub", ids=["s1", "s2", "s3", "s4"]))
        audit = self.service.audit_analysis(analysis["id"])
        self.assertTrue(audit["historical_valid"])
        self.assertFalse(audit["input_current"])
        # 历史快照仍按冻结的 50/期合同复现。
        self.assertEqual(audit["schedule"]["total_cost"], "120.00")
        self.assertEqual(audit["schedule"]["periods"][0]["period_cost"], "120.00")
        # 新分析反映新价格。
        new_analysis = self.service.plan("phased_req")
        new_schedule = new_analysis["report"]["schedule"]
        self.assertEqual(new_schedule["periods"][1]["period_cost"], "0.00")
        self.assertTrue(self.service.verify(analysis["id"])["verified"])

    def test_audit_detects_tampered_schedule_in_snapshot(self):
        analysis = self.service.plan("phased_req")
        report = analysis["report"]
        report["schedule"]["total_cost"] = "0.01"
        self.service.ledger.db.execute(
            "UPDATE analyses SET report=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False), analysis["id"]))
        audit = self.service.audit_analysis(analysis["id"])
        self.assertFalse(audit["historical_valid"])
        self.assertIn("schedule", audit["mismatches"])


class PhasedCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "cli.db"
        self.buyout = offer_body("buy_pkg", unit_price="120", license_term="perpetual")
        self.subscription = offer_body(
            "sub_pkg", unit_price="50", license_term="subscription", path="portable",
            provider="prov_sub", ids=["s1", "s2", "s3", "s4"])
        for name, body in (("requirement", requirement_body()),
                           ("buy", self.buyout), ("sub", self.subscription)):
            (self.root / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")

    def invoke(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def seed(self):
        self.assertEqual(self.invoke("import", "requirement", str(self.root / "requirement.json"))[0], 0)
        self.assertEqual(self.invoke("import", "offer", str(self.root / "buy.json"))[0], 0)
        self.assertEqual(self.invoke("import", "offer", str(self.root / "sub.json"))[0], 0)

    def test_cli_schedule_and_renewal_failure(self):
        self.seed()
        code, schedule, error = self.invoke("schedule", "phased_req", "sub_pkg")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(schedule["total_cost"], "150.00")
        code, failure, error = self.invoke(
            "renewal-failure", "phased_req", "sub_pkg", "2", "sub_pkg")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(failure["failure_phase"], 2)
        self.assertEqual(failure["schedule"]["periods"][1]["active_packages"], [])
        self.assertEqual(failure["recovery"]["replacement_packages"], ["buy_pkg"])

    def test_cli_renewal_failure_rejects_bad_phase(self):
        self.seed()
        code, _, error = self.invoke(
            "renewal-failure", "phased_req", "sub_pkg", "9", "sub_pkg")
        self.assertEqual(code, 2)
        self.assertIn("invalid_phase", error)


if __name__ == "__main__":
    unittest.main()
