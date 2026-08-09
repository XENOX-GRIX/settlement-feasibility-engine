"""Ledger simulation, provably-optimal fee timing, and the search over k.

The core trick (see README): given a fixed list of k creditor payments, the
program fee's collection dates are the only remaining freedom. Collecting the
fee "as early as possible" has a closed form — see ``fee_schedule`` — so
there's no need to search over fee timings, only over (shape, k).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.shapes import build_balloon, build_even, build_staircase, floor_sequence


def cadence_dates_within_horizon(start: date, horizon: date, cap: int = 1000) -> list[date]:
    """All monthly cadence dates from ``start`` through ``horizon`` inclusive."""
    return [d for d in monthly_payment_dates(start, cap) if d <= horizon]


def simulate_mandatory(
    client: Client,
    cadence_dates: list[date],
    k: int,
    payments: list[int],
    bank_fee_cents: int,
    extra_credits: tuple[tuple[date, int], ...] = (),
) -> dict[date, int]:
    """Running end-of-day SDA balance at every relevant date, excluding program fee.

    "Relevant" = every committed ledger entry dated after as_of_date, every
    cadence date (so fee-only dates are represented even with a zero delta),
    and any extra credits under consideration (Part 2 lump/increment).
    Credits are applied before debits on each date.
    """
    events: dict[date, list[int]] = defaultdict(lambda: [0, 0])

    for d in cadence_dates:
        events[d]  # ensure every cadence date is represented, even fee-only ones

    for entry in client.ledger:
        if entry.date > client.as_of_date:
            slot = events[entry.date]
            if entry.type == "credit":
                slot[0] += entry.amount_cents
            else:
                slot[1] += entry.amount_cents

    for i, d in enumerate(cadence_dates):
        if i < k:
            events[d][1] += payments[i]
            if bank_fee_cents:
                events[d][1] += bank_fee_cents

    for d, amount in extra_credits:
        events[d][0] += amount

    balance = client.current_balance_cents
    result: dict[date, int] = {}
    for d in sorted(events):
        credit, debit = events[d]
        balance += credit
        balance -= debit
        result[d] = balance
    return result


def fee_schedule(
    mandatory: dict[date, int], cadence_dates: list[date], program_fee: int
) -> tuple[list[int], bool]:
    """The most front-loaded valid program-fee schedule for a fixed mandatory trajectory.

    F(i) = min(program_fee, min(mandatory[t] for t >= cadence_dates[i])) is the
    pointwise-maximal non-decreasing sequence satisfying F(t) <= mandatory[t]
    everywhere (proof: F is non-decreasing so F(i) <= F(t) <= mandatory[t] for
    every t >= i, which forces F(i) <= that suffix min; and the suffix min is
    itself non-decreasing in i, so the bound is achievable). Per-date fee is
    the increment of F.
    """
    if any(v < 0 for v in mandatory.values()):
        return [0] * len(cadence_dates), False

    dates_sorted = sorted(mandatory)
    suffix_min: dict[date, int] = {}
    running: int | None = None
    for d in reversed(dates_sorted):
        running = mandatory[d] if running is None else min(running, mandatory[d])
        suffix_min[d] = running

    fees = []
    cumulative = 0
    for d in cadence_dates:
        cap = min(program_fee, suffix_min[d])
        fees.append(cap - cumulative)
        cumulative = cap
    return fees, cumulative == program_fee


def score_schedule(fees: list[int]) -> tuple[int, int]:
    """Higher is more front-loaded: (negative index of first full collection, total area)."""
    fee_total = sum(fees)
    cumulative = 0
    first_full = len(fees)
    area = 0
    for i, f in enumerate(fees):
        cumulative += f
        area += cumulative
        if cumulative == fee_total and first_full == len(fees):
            first_full = i
    return (-first_full, area)


@dataclass
class SearchResult:
    feasible: bool
    k: int | None = None
    shape: str | None = None
    payments: list[int] | None = None
    cadence_dates: list[date] | None = None
    fees: list[int] | None = None


def search_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: tuple[tuple[date, int], ...] = (),
) -> SearchResult:
    """Search shapes/k for the most fee-front-loaded feasible schedule.

    ``extra_credits`` lets Part 2's binary search reuse this exact predicate
    with hypothetical extra money merged into the ledger.
    """
    start = offer.first_payment_date or default_first_payment_date(client)
    cadence_dates = cadence_dates_within_horizon(start, client.last_draft_date)
    n = len(cadence_dates)
    if n == 0:
        return SearchResult(feasible=False)

    k_max = min(rules.max_payments, rules.max_terms, n)
    if k_max < 1:
        return SearchResult(feasible=False)

    total = offer_total_cents(offer)
    fee_total = program_fee_cents(offer, rules)

    if rules.even_pays:
        shape = "even"
    elif rules.is_ballooning_allowed:
        shape = "balloon"
    else:
        shape = "staircase"

    best: SearchResult | None = None
    best_score: tuple[int, int, int] | None = None

    for k in range(1, k_max + 1):
        if shape == "even":
            payments = build_even(total, k)
            floors = floor_sequence(k, rules)
            if any(p < f for p, f in zip(payments, floors)):
                continue
        elif shape == "balloon":
            payments = build_balloon(total, k, rules)
        else:
            payments = build_staircase(total, k, rules)
        if payments is None:
            continue

        mandatory = simulate_mandatory(client, cadence_dates, k, payments, rules.bank_fee_cents, extra_credits)
        fees, ok = fee_schedule(mandatory, cadence_dates, fee_total)
        if not ok:
            continue

        first_full_neg, area = score_schedule(fees)
        score = (first_full_neg, area, k)
        if best is None or score > best_score:
            best = SearchResult(
                feasible=True, k=k, shape=shape, payments=payments, cadence_dates=cadence_dates, fees=fees
            )
            best_score = score

    return best if best is not None else SearchResult(feasible=False)


def minimal_feasible_amount(is_feasible, cap: int = 10**9) -> int | None:
    """Binary search the minimal non-negative integer for which ``is_feasible`` holds.

    Assumes monotonicity (more money never hurts). Returns None if not even
    ``cap`` is feasible, signalling a structural (not funding) blocker.
    """
    if is_feasible(0):
        return 0
    if not is_feasible(cap):
        return None
    lo, hi = 0, cap
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if is_feasible(mid):
            hi = mid
        else:
            lo = mid
    return hi
