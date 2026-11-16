"""分阶段训练下的授权权利、逐期费用与提前退出推演。

关键不变量：
- 订阅（subscription）只在实际续费的期次产生费用并授予使用权，绝不能折算成一次性包价；
- 买断（perpetual）在购入期一次性付费，之后各期持续保留使用权且不再产生费用；
- 续费失败后，订阅包自失败期起失去权利，后续期次既不计费也不计覆盖；
- 预算是*每期承诺支出*的上限，逐期检查，而不是对假想总价检查一次。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .contracts import Offer, PortfolioError, Requirement
from .coverage import CoverageEngine, CoverageResult
from .economics import PriceEngine, money

if TYPE_CHECKING:
    from .optimizer import PortfolioOptimizer


@dataclass(frozen=True)
class PeriodCharge:
    package: str
    provider: str
    license_term: str
    charged: bool
    amount: str
    rights_active: bool
    reason: str | None

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PeriodView:
    phase: int
    active_packages: tuple[str, ...]
    charges: tuple[PeriodCharge, ...]
    period_cost: str
    cumulative_cost: str
    within_budget: bool
    coverage: CoverageResult

    def document(self) -> dict[str, Any]:
        return {"phase": self.phase, "active_packages": list(self.active_packages),
                "charges": [charge.document() for charge in self.charges],
                "period_cost": self.period_cost, "cumulative_cost": self.cumulative_cost,
                "within_budget": self.within_budget, "coverage": self.coverage.document()}


@dataclass(frozen=True)
class ExitOption:
    exit_after_phase: int
    cumulative_committed_cost: str
    stoppable_recurring_cost: str
    retained_packages: tuple[str, ...]
    lapsed_packages: tuple[str, ...]

    def document(self) -> dict[str, Any]:
        return {"exit_after_phase": self.exit_after_phase,
                "cumulative_committed_cost": self.cumulative_committed_cost,
                "stoppable_recurring_cost": self.stoppable_recurring_cost,
                "retained_packages": list(self.retained_packages),
                "lapsed_packages": list(self.lapsed_packages)}


@dataclass(frozen=True)
class Schedule:
    requirement: str
    phases: int
    period_budget: str
    selected_packages: tuple[str, ...]
    periods: tuple[PeriodView, ...]
    total_cost: str
    every_period_within_budget: bool
    mandatory_complete_every_period: bool
    exit_options: tuple[ExitOption, ...]

    def document(self) -> dict[str, Any]:
        return {"requirement": self.requirement, "phases": self.phases,
                "period_budget": self.period_budget,
                "selected_packages": list(self.selected_packages),
                "periods": [period.document() for period in self.periods],
                "total_cost": self.total_cost,
                "every_period_within_budget": self.every_period_within_budget,
                "mandatory_complete_every_period": self.mandatory_complete_every_period,
                "exit_options": [option.document() for option in self.exit_options]}


class ScheduleEngine:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine,
                 optimizer: PortfolioOptimizer | None = None):
        self.coverage = coverage
        self.prices = prices
        self.optimizer = optimizer

    def _period_amount(self, offer: Offer, requirement: Requirement) -> Decimal:
        """单个订阅期内该包的真实承诺金额（与一次性报价口径一致，但逐期发生）。"""
        return Decimal(self.prices.quote(offer, requirement).amount)

    def evaluate_schedule(self, requirement: Requirement, selected: list[Offer],
                          *, inactive: frozenset[str] = frozenset(),
                          inactive_from_phase: int | None = None,
                          activation_phase: dict[str, int] | None = None) -> Schedule:
        """逐期推演权利与费用。

        inactive 中的订阅包自 inactive_from_phase 期起失去权利（默认从第 1 期起）；
        失败期之前的续费仍然有效并正常计费。activation_phase 声明某包从哪一期才
        取得权利（用于失败后的替换包），缺省为第 1 期。
        """
        if inactive_from_phase is None:
            inactive_from_phase = 1
        if not 1 <= inactive_from_phase <= requirement.phases:
            raise PortfolioError("invalid_phase", "inactive phase must be within the plan horizon")
        activation_phase = activation_phase or {}
        for package, first_phase in activation_phase.items():
            if not 1 <= first_phase <= requirement.phases:
                raise PortfolioError("invalid_phase", "activation phase must be within the plan horizon")
        phases = requirement.phases
        budget = Decimal(requirement.budget)
        selected = sorted(selected, key=lambda item: item.package)
        periods: list[PeriodView] = []
        cumulative = Decimal(0)
        all_within = True
        all_complete = True
        for phase in range(1, phases + 1):
            charges: list[PeriodCharge] = []
            period_total = Decimal(0)
            active: list[Offer] = []
            for offer in selected:
                starts = activation_phase.get(offer.package, 1)
                if phase < starts:
                    charges.append(PeriodCharge(
                        offer.package, offer.provider, offer.license_term, False,
                        money(Decimal(0)), False, "not_yet_activated"))
                    continue
                lapsed = offer.package in inactive and phase >= inactive_from_phase
                if lapsed:
                    charges.append(PeriodCharge(
                        offer.package, offer.provider, offer.license_term, False,
                        money(Decimal(0)), False, "rights_lapsed"))
                    continue
                if offer.is_subscription:
                    amount = self._period_amount(offer, requirement)
                    period_total += amount
                    charges.append(PeriodCharge(
                        offer.package, offer.provider, offer.license_term, True,
                        money(amount), True, "subscription_renewal"))
                    active.append(offer)
                else:
                    if phase == starts:
                        amount = self._period_amount(offer, requirement)
                        period_total += amount
                        charges.append(PeriodCharge(
                            offer.package, offer.provider, offer.license_term, True,
                            money(amount), True, "perpetual_purchase"))
                    else:
                        charges.append(PeriodCharge(
                            offer.package, offer.provider, offer.license_term, False,
                            money(Decimal(0)), True, "perpetual_rights_retained"))
                    active.append(offer)
            cumulative += period_total
            within = period_total <= budget
            all_within = all_within and within
            coverage = self.coverage.evaluate(requirement, active)
            all_complete = all_complete and coverage.mandatory_complete
            periods.append(PeriodView(
                phase, tuple(offer.package for offer in active), tuple(charges),
                money(period_total), money(cumulative), within, coverage))
        exit_options = self._exit_options(selected, inactive, periods)
        return Schedule(requirement.key, phases, money(budget),
                        tuple(offer.package for offer in selected), tuple(periods),
                        money(cumulative), all_within, all_complete, tuple(exit_options))

    def _exit_options(self, selected: list[Offer], inactive: frozenset[str],
                      periods: list[PeriodView]) -> list[ExitOption]:
        """每个可能的提前退出点：哪些费用还能停、哪些授权会失效。"""
        options: list[ExitOption] = []
        for phase in range(1, len(periods) + 1):
            view = periods[phase - 1]
            retained: list[str] = []
            lapsed: list[str] = []
            stoppable = Decimal(0)
            for offer in selected:
                if offer.package in inactive:
                    # 已因续费失败而失效的授权：退出时既无费用可停，也无权利可失。
                    lapsed.append(offer.package)
                    continue
                if offer.is_subscription:
                    # 退出后不再续费：未来订阅费可停止，已购使用权到期失效。
                    stoppable += self._stoppable_after(offer, phase, periods)
                    lapsed.append(offer.package)
                else:
                    retained.append(offer.package)
            options.append(ExitOption(
                phase, view.cumulative_cost, money(stoppable),
                tuple(sorted(retained)), tuple(sorted(lapsed))))
        return options

    @staticmethod
    def _stoppable_after(offer: Offer, exit_phase: int, periods: list[PeriodView]) -> Decimal:
        """该订阅在退出点之后原本还会发生的续费金额合计。"""
        saved = Decimal(0)
        for view in periods[exit_phase:]:
            charge = next(item for item in view.charges if item.package == offer.package)
            if charge.charged:
                saved += Decimal(charge.amount)
        return saved

    def renewal_failure(self, requirement: Requirement, selected: list[Offer],
                        failed_package: str, failure_phase: int,
                        catalog: list[Offer] | None = None) -> dict[str, Any]:
        """模拟某期续费失败：自失败期起该订阅失去权利，给出真实缺口与恢复方案。"""
        names = {offer.package for offer in selected}
        if failed_package not in names:
            raise PortfolioError("unknown_offer", "failed package is not in the selected portfolio")
        if not 1 <= failure_phase <= requirement.phases:
            raise PortfolioError("invalid_phase", "failure phase must be within the plan horizon")
        failed_offer = next(offer for offer in selected if offer.package == failed_package)
        if not failed_offer.is_subscription:
            raise PortfolioError("not_subscription", "a perpetual license cannot fail renewal")
        inactive = frozenset({failed_package})
        schedule = self.evaluate_schedule(
            requirement, selected, inactive=inactive,
            inactive_from_phase=failure_phase)
        failure_view = schedule.periods[failure_phase - 1]
        gap_report = self.coverage.gap_report(requirement, [
            offer for offer in selected
            if offer.package in failure_view.active_packages])
        recovery = None
        if self.optimizer is not None and catalog is not None:
            recovery = self._recovery_plan(requirement, selected, failed_package,
                                           failure_phase, catalog)
        return {"requirement": requirement.key, "failed_package": failed_package,
                "provider": failed_offer.provider, "failure_phase": failure_phase,
                "phases": requirement.phases,
                "rights_state": "lapsed_from_failure_phase",
                "schedule": schedule.document(),
                "failure_period_gap": gap_report,
                "recovery": recovery}

    def _recovery_plan(self, requirement: Requirement, selected: list[Offer],
                       failed_package: str, failure_phase: int,
                       catalog: list[Offer]) -> dict[str, Any] | None:
        """在失败期用目录中的其他包恢复强制覆盖，替换权利自失败期起生效。"""
        surviving = [offer for offer in selected if offer.package != failed_package]
        candidates = [offer for offer in catalog
                      if offer.package != failed_package
                      and offer.package not in {item.package for item in surviving}]
        search = self.optimizer.search(requirement, [*surviving, *candidates])
        if search.best is None:
            return {"feasible": False, "reason": search.reason,
                    "replacement_packages": [], "replacement_period_cost": None,
                    "failure_period_within_budget": None,
                    "failure_period_mandatory_complete": False}
        replacement = sorted(package for package in search.best.packages
                             if package not in {item.package for item in surviving})
        recovered = [offer for offer in catalog if offer.package in search.best.packages]
        activation = {package: failure_phase for package in replacement}
        recovered_schedule = self.evaluate_schedule(
            requirement, recovered,
            inactive=frozenset({failed_package}),
            inactive_from_phase=failure_phase,
            activation_phase=activation)
        failure_view = recovered_schedule.periods[failure_phase - 1]
        return {"feasible": True, "reason": None,
                "replacement_packages": replacement,
                "full_selection": list(search.best.packages),
                "replacement_period_cost": failure_view.period_cost,
                "failure_period_within_budget": failure_view.within_budget,
                "failure_period_mandatory_complete":
                failure_view.coverage.mandatory_complete}
