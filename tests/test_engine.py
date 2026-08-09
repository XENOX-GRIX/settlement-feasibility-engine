"""Our own test suite, beyond the minimum bar in tests/test_cases.py.

Covers (per ASSIGNMENT.md's checklist): even/staircase/balloon shape
selection, token-pay and tier floors (including their interaction),
max_segments enforcement, the exact-sum invariant, same-day credit-before-debit
ordering, a balance landing exactly on $0, the horizon cutoff, fee-before-
first-payment-date, and both Part 2 minima (including a guardrail rejection).

Most tests build Client/Offer/CreditorRules directly rather than via case
folders, so each scenario is self-contained and easy to read.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from feasibility.engine import evaluate_offer
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    add_months,
    load_case,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.money import pct_of_cents, round_half_up
from feasibility.shapes import build_balloon, build_even, build_staircase, floor_sequence, position_floor
from feasibility.simulate import cadence_dates_within_horizon, simulate_mandatory


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_client(
    draft_amount_cents: int,
    draft_day: int,
    first_draft_date: date,
    last_draft_date: date,
    as_of_date: date | None = None,
    current_balance_cents: int = 0,
    extra_ledger: list[LedgerEntry] | None = None,
) -> Client:
    as_of_date = as_of_date or (first_draft_date - timedelta(days=1))
    ledger = []
    d = first_draft_date
    while d <= last_draft_date:
        ledger.append(LedgerEntry(date=d, amount_cents=draft_amount_cents, type="credit"))
        d = add_months(d, 1)
    ledger.extend(extra_ledger or [])
    return Client(
        draft_amount_cents=draft_amount_cents,
        draft_day=draft_day,
        first_draft_date=first_draft_date,
        last_draft_date=last_draft_date,
        as_of_date=as_of_date,
        current_balance_cents=current_balance_cents,
        ledger=ledger,
    )


def make_offer(
    current_balance_cents: int,
    original_balance_cents: int,
    settlement_pct: float,
    first_payment_date: date | None = None,
) -> Offer:
    return Offer(
        creditor="TestCo",
        current_balance_cents=current_balance_cents,
        original_balance_cents=original_balance_cents,
        settlement_pct=settlement_pct,
        first_payment_date=first_payment_date,
    )


def make_rules(**overrides) -> CreditorRules:
    defaults = dict(
        max_terms=12,
        max_payments=12,
        min_payment_cents=2500,
        max_token_pays=12,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=4,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    defaults.update(overrides)
    return CreditorRules(**defaults)


# ---------------------------------------------------------------------------
# money.py — round-half-up
# ---------------------------------------------------------------------------

def test_round_half_up_rounds_away_from_zero_not_bankers_rounding():
    assert round_half_up(2.5) == 3
    assert round_half_up(-2.5) == -3
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2  # Python's round(1.5) == 2 too, but round(2.5) == 2 (banker's) — checked above


def test_pct_of_cents_half_up_not_bankers_rounding():
    # 0.125 * 4 = 0.5 exactly; Python's round(0.5) == 0 (banker's), we require 1.
    assert pct_of_cents(0.125, 4) == 1
    # 0.5 * 5 = 2.5 exactly; Python's round(2.5) == 2 (banker's), we require 3.
    assert pct_of_cents(0.5, 5) == 3


# ---------------------------------------------------------------------------
# shapes.py — floors
# ---------------------------------------------------------------------------

def test_position_floor_token_pay_budget_then_base_plus_one():
    rules = make_rules(min_payment_cents=2500, max_token_pays=2)
    assert position_floor(1, rules) == 2500
    assert position_floor(2, rules) == 2500
    assert position_floor(3, rules) == 2501  # token budget exhausted, must strictly exceed base


def test_position_floor_zero_token_pays_elevates_immediately():
    rules = make_rules(min_payment_cents=2500, max_token_pays=0)
    assert position_floor(1, rules) == 2501


def test_floor_sequence_tier_overrides_base():
    rules = make_rules(min_payment_cents=2500, max_token_pays=10, min_payment_tiers=[(3, 6000)])
    assert floor_sequence(4, rules) == [2500, 2500, 6000, 6000]


def test_floor_sequence_multiple_tiers_take_running_max():
    # From position 2 on, floor >= 8000; from position 5 on, floor >= 3000 (weaker).
    # The running max must keep 8000 once it applies, not drop to 3000.
    rules = make_rules(min_payment_cents=1000, max_token_pays=10, min_payment_tiers=[(5, 3000), (2, 8000)])
    assert floor_sequence(6, rules) == [1000, 8000, 8000, 8000, 8000, 8000]


# ---------------------------------------------------------------------------
# shapes.py — shape builders
# ---------------------------------------------------------------------------

def test_build_even_remainder_goes_on_latest_payments():
    seq = build_even(100, 3)
    assert seq == [33, 33, 34]
    assert sum(seq) == 100
    assert seq == sorted(seq)


def test_build_balloon_floors_then_final_absorbs_remainder():
    rules = make_rules(min_payment_cents=2500, max_token_pays=10)
    seq = build_balloon(30000, 6, rules)
    assert seq[:-1] == [2500] * 5
    assert seq[-1] == 30000 - 2500 * 5
    assert sum(seq) == 30000
    assert seq == sorted(seq)


def test_build_staircase_respects_max_segments_cap():
    rules = make_rules(min_payment_cents=2500, max_token_pays=10, max_segments=2)
    seq = build_staircase(100000, 10, rules)
    assert sum(seq) == 100000
    assert seq == sorted(seq)
    assert len(set(seq)) <= 2


def test_build_staircase_never_creates_a_lone_final_payment():
    # Last floor block has only 2 members -> too small to split (would need a
    # singleton). Must raise both together instead of creating a disguised balloon.
    rules = make_rules(min_payment_cents=2500, max_token_pays=10, max_segments=3)
    seq = build_staircase(10000, 2, rules)
    assert seq[0] == seq[1]
    assert sum(seq) == 10000


def _count_payment_levels(seq: list[int]) -> int:
    """Count distinct payment *levels*, tolerating the +-1 cent spread a
    single level can get from distributing an indivisible remainder (the
    same non-issue even_pays has — see README). A real step is a jump of
    more than 1 cent between consecutive payments.
    """
    levels = 1
    for prev, cur in zip(seq, seq[1:]):
        if cur - prev > 1:
            levels += 1
    return levels


def test_build_staircase_exact_sum_and_segment_cap_property():
    for k in range(1, 9):
        for max_segments in (1, 2, 3, 5):
            rules = make_rules(
                min_payment_cents=1500, max_token_pays=3, min_payment_tiers=[(4, 4000)], max_segments=max_segments
            )
            seq = build_staircase(90000, k, rules)
            if seq is None:
                continue
            assert sum(seq) == 90000
            assert seq == sorted(seq)
            assert _count_payment_levels(seq) <= max_segments


# ---------------------------------------------------------------------------
# simulate.py — ledger mechanics
# ---------------------------------------------------------------------------

def test_same_day_credits_applied_before_debits():
    d = date(2026, 1, 31)
    client = make_client(
        10000,
        1,
        date(2026, 1, 1),
        date(2026, 1, 1),
        current_balance_cents=500,
        extra_ledger=[
            LedgerEntry(date=d, amount_cents=1000, type="credit"),
            LedgerEntry(date=d, amount_cents=2000, type="credit"),
            LedgerEntry(date=d, amount_cents=1500, type="debit"),
        ],
    )
    mandatory = simulate_mandatory(client, [], k=0, payments=[], bank_fee_cents=0)
    # 500 (opening) + draft(10000, on Jan1) + 1000 + 2000 (credits) - 1500 (debit)
    assert mandatory[date(2026, 1, 1)] == 10500
    assert mandatory[d] == 500 + 10000 + 1000 + 2000 - 1500


def test_cadence_dates_exclude_past_horizon_but_include_horizon_itself():
    dates = cadence_dates_within_horizon(date(2026, 1, 15), date(2026, 3, 1))
    assert dates == [date(2026, 1, 15), date(2026, 2, 15)]  # Mar 15 excluded, past horizon

    dates_on_horizon = cadence_dates_within_horizon(date(2026, 1, 15), date(2026, 1, 15))
    assert dates_on_horizon == [date(2026, 1, 15)]  # the horizon date itself is allowed


# ---------------------------------------------------------------------------
# engine.py — end-to-end invariants over the provided cases
# ---------------------------------------------------------------------------

def test_feasible_cases_satisfy_all_hard_invariants():
    for case in ["case1_feasible_even", "case3_balloon", "case4_tiers"]:
        client, offer, rules = load_case(f"cases/{case}")
        r = evaluate_offer(client, offer, rules)
        assert r.feasible is True, case

        # exact sum
        assert sum(row.creditor_payment_cents for row in r.schedule) == offer_total_cents(offer), case
        assert sum(row.program_fee_cents for row in r.schedule) == program_fee_cents(offer, rules), case

        # never negative
        assert all(row.balance_cents >= 0 for row in r.schedule), case

        # non-decreasing creditor payments
        pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
        assert pays == sorted(pays), case

        # fee never appears before the first payment date; no schedule row past horizon
        assert all(row.date >= offer.first_payment_date for row in r.schedule), case
        assert all(row.date <= client.last_draft_date for row in r.schedule), case

        # bank fee only on dates carrying a creditor payment
        for row in r.schedule:
            if row.creditor_payment_cents == 0:
                assert row.bank_fee_cents == 0, case
            elif rules.bank_fee_cents:
                assert row.bank_fee_cents == rules.bank_fee_cents, case

        # a balance landing exactly on $0 shows up somewhere on the feasible boundary cases
        if case in ("case1_feasible_even",):
            assert any(row.balance_cents == 0 for row in r.schedule), case


def test_case4_max_segments_cap_enforced_end_to_end():
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert len(set(pays)) <= rules.max_segments
    # tier floor (positions 7+, $50 min) must hold
    assert all(p >= 5000 for p in pays[6:])


def test_evaluate_offer_never_schedules_past_horizon():
    first_draft, last_draft = date(2026, 1, 1), date(2026, 3, 1)
    client = make_client(50000, 1, first_draft, last_draft)
    offer = make_offer(20000, 20000, 0.5, first_payment_date=date(2026, 1, 15))
    rules = make_rules(min_payment_cents=1000, max_token_pays=10, max_segments=2)

    r = evaluate_offer(client, offer, rules)
    assert r.feasible is True
    assert date(2026, 3, 15) not in [row.date for row in r.schedule]  # would be the 3rd cadence date, past horizon
    assert all(row.date <= last_draft for row in r.schedule)


# ---------------------------------------------------------------------------
# Part 2 — minimum additional funds
# ---------------------------------------------------------------------------

def test_part2_matches_case2_minima_exactly():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds
    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.within_guardrail is True
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True
    # the increment total legitimately exceeds the lump: the 5th draft's
    # increment lands after the last usable cadence date and is wasted.
    assert af.monthly_increment.amount_cents * af.monthly_increment.num_drafts > af.lump_sum.amount_cents


def test_part2_guardrail_rejection_when_deficit_is_too_large():
    # Only 1 usable cadence date and a debt far beyond what 2 small drafts can
    # ever cover -> both minima should come back well above their guardrail caps.
    first_draft, last_draft = date(2026, 1, 1), date(2026, 2, 1)
    client = make_client(1000, 1, first_draft, last_draft)  # drafts: Jan 1, Feb 1 ($10 each)
    offer = make_offer(1000000, 1000000, 1.0, first_payment_date=date(2026, 1, 31))
    rules = make_rules(max_terms=1, max_payments=1, min_payment_cents=100, max_token_pays=1, max_segments=1)

    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds
    assert af.lump_sum.within_guardrail is False
    assert af.lump_sum.reason != ""
    assert af.monthly_increment.within_guardrail is False
    assert af.monthly_increment.reason != ""
    # With only one usable cadence date, both minima are driven by the same
    # single deficit (the 2nd draft lands after the only payment date and
    # can't help either way), so lump and increment happen to coincide here.
    assert af.monthly_increment.amount_cents == af.lump_sum.amount_cents == 999000
