from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import unittest

from embodied_portfolio.cli import demo_payloads
from embodied_portfolio.contracts import Offer, PortfolioError, Requirement
from embodied_portfolio.coverage import CoverageEngine
from embodied_portfolio.economics import PriceEngine
from embodied_portfolio.optimizer import PortfolioOptimizer
from embodied_portfolio.phasing import ScheduleEngine


TARGET = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}


def make_offer(package: str, *, unit_price: str, license_term: str,
               path: str = "teleoperation", provider: str | None = None,
               ids: tuple[str, ...] = ("ep_01", "ep_02", "ep_03", "ep_04")) -> Offer:
    body = {"provider": provider or f"prov_{package}", "package": package, "path": path,
            "robots": ["arm_a"],
            "observations": [{"slice": TARGET, "count": len(ids), "episode_ids": list(ids)}],
            "hours": "1", "price_mode": "flat", "unit_price": unit_price,
            "license": "nonexclusive", "allowed_clients": [],
            "validity": "monthly" if license_term == "subscription" else "indefinite",
            "notes": "", "license_term": license_term}
    return Offer.parse(body)


def make_requirement(*, budget: str = "130", phases: int = 3) -> Requirement:
    return Requirement.parse({
        "key": "phased_req", "client": "client_a", "title": "分阶段训练采购",
        "budget": budget, "phases": phases,
        "targets": [{"slice": TARGET, "minimum": 4, "mandatory": True, "weight": "1"}],
        "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 4})


class PhasingContractTests(unittest.TestCase):
    def test_license_term_defaults_to_perpetual_and_round_trips(self):
        _, offers = demo_payloads()
        offer = Offer.parse(offers[0])
        self.assertEqual(offer.license_term, "perpetual")
        self.assertFalse(offer.is_subscription)
        self.assertEqual(Offer.parse(offer.document()), offer)

    def test_subscription_flag_reflects_term(self):
        subscription = make_offer("sub_pkg", unit_price="50", license_term="subscription")
        self.assertTrue(subscription.is_subscription)
        self.assertEqual(subscription.document()["license_term"], "subscription")

    def test_unknown_license_term_is_rejected(self):
        body = make_offer("bad_pkg", unit_price="50", license_term="perpetual").document()
        body["license_term"] = "rent_to_own"
        with self.assertRaises(PortfolioError) as caught:
            Offer.parse(body)
        self.assertEqual(caught.exception.code, "license_term")

    def test_requirement_phases_defaults_to_one_and_validates_range(self):
        req, _ = demo_payloads()
        self.assertEqual(Requirement.parse(req).phases, 1)
        body = make_requirement().document()
        self.assertEqual(Requirement.parse(body).phases, 3)
        for bad in (0, 25):
            body["phases"] = bad
            with self.assertRaises(PortfolioError):
                Requirement.parse(body)

    def test_requirement_round_trips_phases(self):
        requirement = make_requirement()
        self.assertEqual(Requirement.parse(requirement.document()), requirement)


class ScheduleEngineTests(unittest.TestCase):
    def setUp(self):
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.engine = ScheduleEngine(self.coverage, self.prices, self.optimizer)
        self.buyout = make_offer("buy_pkg", unit_price="120", license_term="perpetual")
        self.subscription = make_offer("sub_pkg", unit_price="50",
                                       license_term="subscription", path="portable",
                                       provider="prov_sub", ids=("s1", "s2", "s3", "s4"))
        self.requirement = make_requirement(budget="130", phases=3)

    def test_perpetual_charges_once_and_retains_rights(self):
        schedule = self.engine.evaluate_schedule(self.requirement, [self.buyout])
        self.assertEqual(schedule.total_cost, "120.00")
        self.assertEqual([view.period_cost for view in schedule.periods],
                         ["120.00", "0.00", "0.00"])
        self.assertTrue(all(view.within_budget for view in schedule.periods))
        self.assertTrue(all("buy_pkg" in view.active_packages for view in schedule.periods))
        reasons = [charge.reason for view in schedule.periods for charge in view.charges]
        self.assertEqual(reasons, ["perpetual_purchase", "perpetual_rights_retained",
                                   "perpetual_rights_retained"])

    def test_subscription_charges_every_period_and_total_costs_more(self):
        schedule = self.engine.evaluate_schedule(self.requirement, [self.subscription])
        self.assertEqual(schedule.total_cost, "150.00")
        self.assertEqual([view.period_cost for view in schedule.periods],
                         ["50.00", "50.00", "50.00"])
        self.assertTrue(all(charge.charged for view in schedule.periods
                            for charge in view.charges))
        self.assertTrue(schedule.every_period_within_budget)
        self.assertTrue(schedule.mandatory_complete_every_period)

    def test_optimizer_prefers_buyout_when_horizon_total_is_lower(self):
        search = self.optimizer.search(self.requirement, [self.buyout, self.subscription])
        self.assertEqual(search.best.packages, ("buy_pkg",))
        self.assertEqual(search.best.cost, "120.00")
        self.assertEqual(search.best.period_costs, ("120.00", "0.00", "0.00"))

    def test_single_period_subscription_keeps_legacy_cost_semantics(self):
        requirement = make_requirement(budget="130", phases=1)
        search = self.optimizer.search(requirement, [self.subscription])
        self.assertEqual(search.best.cost, "50.00")
        self.assertEqual(search.best.period_costs, ("50.00",))
        self.assertTrue(search.best.every_period_within_budget)

    def test_per_period_budget_rejects_subscription_even_if_total_looks_affordable(self):
        # 订阅每期 50；每期预算仅 40。不存在把订阅折成总价后蒙混过关的路径。
        requirement = make_requirement(budget="40", phases=3)
        search = self.optimizer.search(requirement, [self.subscription])
        self.assertIsNone(search.best)
        self.assertEqual(search.reason, "all_combinations_exceed_budget")

    def test_per_period_budget_does_not_use_aggregate_total(self):
        # 每期预算 100：买断第 1 期 120 超预算，即使其后各期免费也不能进入可行集。
        requirement = make_requirement(budget="100", phases=3)
        search = self.optimizer.search(requirement, [self.buyout, self.subscription])
        self.assertEqual(search.best.packages, ("sub_pkg",))
        self.assertTrue(search.best.every_period_within_budget)

    def test_exit_options_identify_stoppable_fees_and_lapsed_licenses(self):
        schedule = self.engine.evaluate_schedule(self.requirement, [self.subscription])
        first, second, last = schedule.exit_options
        self.assertEqual(first.cumulative_committed_cost, "50.00")
        self.assertEqual(first.stoppable_recurring_cost, "100.00")
        self.assertEqual(first.lapsed_packages, ("sub_pkg",))
        self.assertEqual(first.retained_packages, ())
        self.assertEqual(second.stoppable_recurring_cost, "50.00")
        self.assertEqual(last.stoppable_recurring_cost, "0.00")

    def test_exit_options_keep_perpetual_rights_after_exit(self):
        schedule = self.engine.evaluate_schedule(self.requirement, [self.buyout])
        option = schedule.exit_options[0]
        self.assertEqual(option.retained_packages, ("buy_pkg",))
        self.assertEqual(option.lapsed_packages, ())
        self.assertEqual(option.stoppable_recurring_cost, "0.00")

    def test_renewal_failure_lapses_rights_only_from_failure_phase(self):
        result = self.engine.renewal_failure(
            self.requirement, [self.subscription], "sub_pkg", 2,
            [self.buyout, self.subscription])
        periods = result["schedule"]["periods"]
        self.assertEqual(periods[0]["period_cost"], "50.00")
        self.assertEqual(periods[0]["active_packages"], ["sub_pkg"])
        self.assertTrue(periods[0]["coverage"]["mandatory_complete"])
        self.assertEqual(periods[1]["period_cost"], "0.00")
        self.assertEqual(periods[1]["active_packages"], [])
        self.assertFalse(periods[1]["coverage"]["mandatory_complete"])
        self.assertEqual(periods[2]["active_packages"], [])
        self.assertEqual(result["failure_period_gap"]["gaps"][0]["missing"], 4)

    def test_renewal_failure_recovery_activates_replacement_at_failure_phase(self):
        result = self.engine.renewal_failure(
            self.requirement, [self.subscription], "sub_pkg", 2,
            [self.buyout, self.subscription])
        recovery = result["recovery"]
        self.assertTrue(recovery["feasible"])
        self.assertEqual(recovery["replacement_packages"], ["buy_pkg"])
        self.assertEqual(recovery["replacement_period_cost"], "120.00")
        self.assertTrue(recovery["failure_period_within_budget"])
        self.assertTrue(recovery["failure_period_mandatory_complete"])

    def test_renewal_failure_reports_gap_when_no_replacement_exists(self):
        result = self.engine.renewal_failure(
            self.requirement, [self.subscription], "sub_pkg", 2, [self.subscription])
        self.assertFalse(result["recovery"]["feasible"])
        self.assertEqual(result["recovery"]["replacement_packages"], [])

    def test_renewal_failure_rejects_perpetual_package(self):
        with self.assertRaises(PortfolioError) as caught:
            self.engine.renewal_failure(
                self.requirement, [self.buyout], "buy_pkg", 2, [self.buyout])
        self.assertEqual(caught.exception.code, "not_subscription")

    def test_renewal_failure_validates_phase_and_membership(self):
        with self.assertRaises(PortfolioError) as caught:
            self.engine.renewal_failure(
                self.requirement, [self.subscription], "sub_pkg", 4,
                [self.buyout, self.subscription])
        self.assertEqual(caught.exception.code, "invalid_phase")
        with self.assertRaises(PortfolioError) as caught:
            self.engine.renewal_failure(
                self.requirement, [self.subscription], "buy_pkg", 2,
                [self.buyout, self.subscription])
        self.assertEqual(caught.exception.code, "unknown_offer")

    def test_mixed_portfolio_tracks_two_terms_in_one_schedule(self):
        extra = make_offer("extra_pkg", unit_price="20", license_term="subscription",
                           path="portable", provider="prov_extra",
                           ids=("x1", "x2"))
        requirement = make_requirement(budget="130", phases=2)
        schedule = self.engine.evaluate_schedule(requirement, [self.buyout, extra])
        self.assertEqual([view.period_cost for view in schedule.periods],
                         ["140.00", "20.00"])
        self.assertEqual(schedule.total_cost, "160.00")
        self.assertFalse(schedule.every_period_within_budget)
        first_charges = {charge.package: charge for charge in schedule.periods[0].charges}
        self.assertTrue(first_charges["buy_pkg"].charged)
        self.assertTrue(first_charges["extra_pkg"].charged)


if __name__ == "__main__":
    unittest.main()
