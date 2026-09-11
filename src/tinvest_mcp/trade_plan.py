"""Trade-plan assembly logic (advisory stage 6).

Pure functions over already-normalized values — NO LLM, NO SDK. The service
layer feeds market data / operations in; this module handles the deterministic
parts the plan tool adds on top of the stage-7 engine
(:func:`tinvest_mcp.risk_engine.evaluate_plan`): FIFO tax lots with the ЛДВ
exemption, money↔lot sizing and the cost-benefit verdict. Built plans are kept
in an in-memory TTL store (like proposals) so a later execution stage can pick
them up by ``plan_id``.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from .schemas import (
    ASSET_CLASSES,
    BrokerOperation,
    PlanStepState,
    SaleTaxEstimate,
    TradePlan,
    TradePlanCostBenefit,
    TradePlanStatus,
    TradePlanVerificationReport,
)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_RATIO = Decimal("0.0001")
_HUNDRED = Decimal("100")
_DAYS_PER_YEAR = Decimal("365.25")

# Operation types that move inventory (same split services uses for totals).
BUY_OPERATION_TYPES = frozenset({"buy", "buy_card", "buy_margin", "delivery_buy"})
SELL_OPERATION_TYPES = frozenset({"sell", "sell_card", "sell_margin", "delivery_sell"})

LDV_NOTE = "ЛДВ: lots held ≥ {years} years are exempt (3 млн ₽/year cap is not modeled)."
TAX_CAVEATS = [
    "Tax is an estimate: flat rate (the 15% bracket above 2.4 млн ₽/year is not modeled).",
    "Losses of one sale do not offset gains of another in this preview.",
    "НКД received on bond sales is taxed as coupon income and is not modeled.",
]


def bond_unit_money(price_pct: Decimal, nominal: Decimal | None) -> Decimal | None:
    """Money price per bond from a %-of-nominal quote. None when nominal is unknown."""
    if nominal is None or nominal <= 0:
        return None
    return (nominal * price_pct / _HUNDRED).quantize(_CENT)


def lots_from_amount(amount: Decimal, lot_cash_cost: Decimal) -> int:
    """Whole lots the amount buys/raises at the given full cash cost per lot."""
    if lot_cash_cost <= 0 or amount <= 0:
        return 0
    return int((amount / lot_cash_cost).to_integral_value(rounding=ROUND_DOWN))


# --- FIFO tax lots + ЛДВ ------------------------------------------------------


@dataclass
class TaxLot:
    """Remaining units from one historical purchase (clean money cost basis)."""

    acquired_at: date | None
    quantity: Decimal  # units still held
    cost_per_unit: Decimal  # clean money price per unit (НКД excluded)


def build_tax_lots(operations: list[BrokerOperation]) -> tuple[list[TaxLot], list[str]]:
    """Rebuild open FIFO tax lots from the instrument's operation history.

    Buys append lots (clean cost = |payment| - |НКД| over quantity); sells
    consume from the oldest lot — the broker's НДФЛ accounting works the same
    way. Operations without usable quantity/price are skipped with a note; the
    caller then falls back to the position's average price.
    """
    notes: list[str] = []
    lots: list[TaxLot] = []
    for op in sorted(operations, key=lambda o: o.date):
        qty = op.quantity or _ZERO
        if qty <= 0:
            continue
        if op.type in BUY_OPERATION_TYPES:
            payment = abs(op.payment or _ZERO)
            aci = abs(op.accrued_int or _ZERO)
            if payment > 0:
                cost = (payment - aci) / qty
            elif op.price is not None and op.price > 0:
                cost = op.price
            else:
                notes.append(f"Buy operation {op.id} has no payment/price — excluded from tax lots.")
                continue
            lots.append(TaxLot(acquired_at=op.date.date(), quantity=qty, cost_per_unit=cost))
        elif op.type in SELL_OPERATION_TYPES:
            remaining = qty
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(lot.quantity, remaining)
                lot.quantity -= take
                remaining -= take
                if lot.quantity <= 0:
                    lots.pop(0)
            if remaining > 0:
                notes.append("Operation history shows more sold than bought — tax lots may be incomplete.")
    return lots, notes


def estimate_sale_tax(
    lots: list[TaxLot],
    units_to_sell: Decimal,
    sell_price_per_unit: Decimal,
    *,
    as_of: date,
    tax_rate_pct: Decimal,
    ldv_min_holding_years: int,
    fallback_cost_per_unit: Decimal | None = None,
) -> SaleTaxEstimate:
    """НДФЛ estimate for selling ``units_to_sell`` at a clean money price.

    Consumes FIFO lots (oldest first); lots held at least
    ``ldv_min_holding_years`` are ЛДВ-exempt. Units not covered by the history
    use ``fallback_cost_per_unit`` (position average price) or, absent that, a
    zero basis — deliberately overestimating the tax.
    """
    notes = [LDV_NOTE.format(years=ldv_min_holding_years), *TAX_CAVEATS]
    cost_basis = _ZERO
    exempt_gain = _ZERO
    taxable_gain = _ZERO
    used_fifo = False

    remaining = units_to_sell
    queue = [TaxLot(lot.acquired_at, lot.quantity, lot.cost_per_unit) for lot in lots]
    while remaining > 0 and queue:
        lot = queue.pop(0)
        take = min(lot.quantity, remaining)
        remaining -= take
        used_fifo = True
        cost_basis += lot.cost_per_unit * take
        gain = (sell_price_per_unit - lot.cost_per_unit) * take
        held_years = Decimal((as_of - lot.acquired_at).days) / _DAYS_PER_YEAR if lot.acquired_at is not None else _ZERO
        if held_years >= ldv_min_holding_years:
            exempt_gain += gain
        else:
            taxable_gain += gain

    used_fallback = False
    if remaining > 0:
        used_fallback = True
        basis = fallback_cost_per_unit if fallback_cost_per_unit is not None else _ZERO
        cost_basis += basis * remaining
        taxable_gain += (sell_price_per_unit - basis) * remaining
        if fallback_cost_per_unit is not None:
            notes.append(
                "Part of the sale is not covered by the operation history — "
                "position average price used as the cost basis (no ЛДВ)."
            )
        else:
            notes.append("No cost basis found for part of the sale — zero basis assumed (tax overestimated).")

    if used_fifo and used_fallback:
        method = "mixed"
    elif used_fifo:
        method = "fifo"
    elif fallback_cost_per_unit is not None:
        method = "average_price"
    else:
        method = "none"

    gross = sell_price_per_unit * units_to_sell
    tax = (max(taxable_gain, _ZERO) * tax_rate_pct / _HUNDRED).quantize(_CENT)
    return SaleTaxEstimate(
        method=method,
        cost_basis=cost_basis.quantize(_CENT),
        gross_proceeds=gross.quantize(_CENT),
        gross_gain=(gross - cost_basis).quantize(_CENT),
        exempt_gain_ldv=exempt_gain.quantize(_CENT),
        taxable_gain=taxable_gain.quantize(_CENT),
        tax=tax,
        tax_rate_pct=tax_rate_pct,
        notes=notes,
    )


# --- cost-benefit verdict ------------------------------------------------------


def _misallocation(
    allocation_pct: Mapping[str, Decimal],
    total_value: Decimal,
    target: Mapping[str, int],
) -> tuple[Decimal, Decimal]:
    """(money in the wrong class = Σ|pct-target|×total/2, max |deviation| in pp)."""
    value = _ZERO
    max_dev = _ZERO
    for cls in ASSET_CLASSES:
        deviation = abs(allocation_pct.get(cls, _ZERO) - Decimal(target.get(cls, 0)))
        max_dev = max(max_dev, deviation)
        value += deviation / _HUNDRED * total_value
    return (value / 2).quantize(_CENT), max_dev.quantize(_CENT)


def build_cost_benefit(
    *,
    allocation_before_pct: Mapping[str, Decimal],
    allocation_after_pct: Mapping[str, Decimal],
    total_value_before: Decimal,
    total_value_after: Decimal,
    target_allocation: Mapping[str, int],
    commissions: Decimal,
    taxes: Decimal,
    rebalance_threshold_pct: Decimal,
    max_cost_to_benefit_ratio: Decimal,
) -> TradePlanCostBenefit:
    """WORTH_IT / NOT_WORTH_IT from costs vs how much misallocation the plan removes."""
    total_costs = (commissions + taxes).quantize(_CENT)
    before, max_dev_before = _misallocation(allocation_before_pct, total_value_before, target_allocation)
    after, max_dev_after = _misallocation(allocation_after_pct, total_value_after, target_allocation)
    reduction = (before - after).quantize(_CENT)
    ratio = (total_costs / reduction).quantize(_RATIO) if reduction > 0 else None

    if max_dev_before < rebalance_threshold_pct:
        verdict, reason = (
            "NOT_WORTH_IT",
            (
                f"Max drift {max_dev_before}% is below the rebalance threshold "
                f"{rebalance_threshold_pct}% — doing nothing is the honest answer."
            ),
        )
    elif reduction <= 0:
        verdict, reason = "NOT_WORTH_IT", ("The plan does not move the portfolio toward the target allocation.")
    elif ratio is not None and ratio > max_cost_to_benefit_ratio:
        verdict, reason = (
            "NOT_WORTH_IT",
            (
                f"Costs {total_costs} are {(ratio * _HUNDRED).quantize(_CENT)}% of the misallocation "
                f"removed ({reduction}) — above the {(max_cost_to_benefit_ratio * _HUNDRED).quantize(_CENT)}% limit."
            ),
        )
    else:
        verdict, reason = (
            "WORTH_IT",
            (
                f"Plan removes {reduction} of misallocation for {total_costs} in costs "
                f"({(ratio * _HUNDRED).quantize(_CENT)}% — within the "
                f"{(max_cost_to_benefit_ratio * _HUNDRED).quantize(_CENT)}% limit)."
            ),
        )

    return TradePlanCostBenefit(
        commissions=commissions.quantize(_CENT),
        taxes=taxes.quantize(_CENT),
        total_costs=total_costs,
        misallocation_before=before,
        misallocation_after=after,
        drift_reduction_value=reduction,
        cost_to_benefit_ratio=ratio,
        max_abs_deviation_before_pct=max_dev_before,
        max_abs_deviation_after_pct=max_dev_after,
        rebalance_threshold_pct=rebalance_threshold_pct,
        max_cost_to_benefit_ratio=max_cost_to_benefit_ratio,
        verdict=verdict,
        reason=reason,
    )


# --- in-memory plan store + execution state (stages 8-10) --------------------


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PlanExecutionRecord:
    """Mutable server-side execution state; never supplied by the caller."""

    plan_id: str
    status: TradePlanStatus
    steps: list[PlanStepState]
    account_id: str | None = None
    confirmed_at: datetime | None = None
    paused_reason: str | None = None
    verification_report: TradePlanVerificationReport | None = None
    recommendation_entries: int = 0
    last_plan_checks: list[object] = field(default_factory=list)


class TradePlanStore:
    """Thread-safe TTL store keyed by ``plan_id`` (mirrors ProposalStore).

    TTL protects the first, whole-plan confirmation gate. Once confirmed, a
    plan remains addressable while broker orders are in flight — otherwise an
    overnight ``SUBMITTED`` order could become detached from its audit trail.
    A per-plan re-entrant lock serializes preview/execute/status calls locally;
    the broker idempotency key remains the final duplicate-order safeguard.
    """

    def __init__(self, ttl_seconds: int = 900) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, TradePlan] = {}
        self._execution: dict[str, PlanExecutionRecord] = {}
        self._plan_locks: dict[str, threading.RLock] = {}
        self._lock = threading.Lock()

    def new_id_and_window(self) -> tuple[str, datetime, datetime]:
        now = _now()
        return str(uuid.uuid4()), now, now + timedelta(seconds=self._ttl)

    def put(self, plan: TradePlan, *, account_id: str | None = None) -> None:
        with self._lock:
            self._items[plan.plan_id] = plan
            self._execution[plan.plan_id] = PlanExecutionRecord(
                plan_id=plan.plan_id,
                status=plan.status,
                account_id=account_id,
                steps=[
                    PlanStepState(
                        sequence=leg.sequence,
                        action=leg.action,
                        instrument_uid=leg.instrument_uid,
                        ticker=leg.ticker,
                        name=leg.name,
                        quantity_lots=leg.quantity_lots,
                    )
                    for leg in plan.items
                ],
            )
            self._plan_locks.setdefault(plan.plan_id, threading.RLock())

    def get(self, plan_id: str) -> TradePlan | None:
        with self._lock:
            plan = self._items.get(plan_id)
            record = self._execution.get(plan_id)
            if plan is not None and record is not None and record.confirmed_at is None and _now() >= plan.expires_at:
                record.status = "EXPIRED"
                plan.status = "EXPIRED"
        return plan

    def get_execution(self, plan_id: str) -> PlanExecutionRecord | None:
        # Apply lazy expiry through get() before returning the record.
        if self.get(plan_id) is None:
            return None
        with self._lock:
            return self._execution.get(plan_id)

    def lock_for(self, plan_id: str) -> threading.RLock:
        with self._lock:
            return self._plan_locks.setdefault(plan_id, threading.RLock())

    def sync_status(self, plan_id: str, status: TradePlanStatus) -> None:
        with self._lock:
            plan = self._items.get(plan_id)
            record = self._execution.get(plan_id)
            if plan is not None:
                plan.status = status
            if record is not None:
                record.status = status


_store: TradePlanStore | None = None


def get_plan_store(ttl_seconds: int = 900) -> TradePlanStore:
    global _store
    if _store is None:
        _store = TradePlanStore(ttl_seconds=ttl_seconds)
    return _store
