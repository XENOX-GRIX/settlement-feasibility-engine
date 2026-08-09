"""Settlement feasibility & fee engine.

See README for the algorithm walkthrough. In short: ``search_schedule``
(feasibility/simulate.py) finds the most fee-front-loaded feasible schedule
across every legal (shape, k) combination; when nothing is feasible, the same
predicate — with hypothetical extra credits merged in — drives two binary
searches for the Part 2 minima.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from feasibility.models import Client, CreditorRules, Offer, offer_total_cents
from feasibility.money import pct_of_cents
from feasibility.simulate import SearchResult, minimal_feasible_amount, search_schedule, simulate_mandatory


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


def _build_rows(client: Client, rules: CreditorRules, result: SearchResult) -> list[ScheduleRow]:
    mandatory = simulate_mandatory(client, result.cadence_dates, result.k, result.payments, rules.bank_fee_cents)
    rows: list[ScheduleRow] = []
    cumulative_fee = 0
    for i, d in enumerate(result.cadence_dates):
        payment = result.payments[i] if i < result.k else 0
        bank_fee = rules.bank_fee_cents if i < result.k else 0
        fee = result.fees[i]
        if payment == 0 and fee == 0:
            continue
        cumulative_fee += fee
        balance = mandatory[d] - cumulative_fee
        rows.append(
            ScheduleRow(
                date=d,
                creditor_payment_cents=payment,
                program_fee_cents=fee,
                bank_fee_cents=bank_fee,
                balance_cents=balance,
            )
        )
    return rows


def _lump_sum_option(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    lump_date = client.as_of_date + timedelta(days=1)

    def is_feasible(amount: int) -> bool:
        return search_schedule(client, offer, rules, extra_credits=((lump_date, amount),)).feasible

    amount = minimal_feasible_amount(is_feasible)
    if amount is None:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            date=lump_date,
            reason=(
                "No feasible schedule found even with a very large lump sum — this is a "
                "structural blocker (e.g. no cadence date within the horizon), not a funding gap."
            ),
        )

    cap = pct_of_cents(0.65, offer_total_cents(offer))
    within = amount <= cap
    reason = "" if within else f"Lump sum {amount}c exceeds the guardrail cap of {cap}c (65% of offer total)."
    return FundsOption(amount_cents=amount, within_guardrail=within, date=lump_date, reason=reason)


def _monthly_increment_option(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    future_draft_dates = [e.date for e in client.ledger if e.type == "credit" and e.date > client.as_of_date]
    n_drafts = len(future_draft_dates)
    if n_drafts == 0:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            num_drafts=0,
            reason="No future drafts exist to increment.",
        )

    def is_feasible(amount: int) -> bool:
        credits = tuple((d, amount) for d in future_draft_dates)
        return search_schedule(client, offer, rules, extra_credits=credits).feasible

    amount = minimal_feasible_amount(is_feasible)
    if amount is None:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            num_drafts=n_drafts,
            reason=(
                "No feasible schedule found even with a very large monthly increment — this is a "
                "structural blocker, not a funding gap."
            ),
        )

    cap = max(10000, pct_of_cents(0.40, client.draft_amount_cents))
    within = amount <= cap
    reason = (
        ""
        if within
        else f"Monthly increment {amount}c exceeds the guardrail cap of {cap}c (max of $100 or 40% of draft amount)."
    )
    return FundsOption(amount_cents=amount, within_guardrail=within, num_drafts=n_drafts, reason=reason)


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification.

    Return a Result with feasible=True and a schedule when the offer fits, or
    feasible=False with additional_funds (minimum lump sum AND minimum monthly
    increment) when it does not.
    """
    result = search_schedule(client, offer, rules)
    if result.feasible:
        rows = _build_rows(client, rules, result)
        return Result(feasible=True, pay_shape_used=result.shape, schedule=rows, additional_funds=None)

    additional_funds = AdditionalFunds(
        lump_sum=_lump_sum_option(client, offer, rules),
        monthly_increment=_monthly_increment_option(client, offer, rules),
    )
    return Result(feasible=False, pay_shape_used=None, schedule=None, additional_funds=additional_funds)
