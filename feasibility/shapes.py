"""Per-position payment floors and the three payment-shape builders.

All three builders take a fixed count ``k`` and the exact ``offer_total`` and
return a length-``k`` list of non-decreasing creditor payments summing exactly
to ``offer_total`` (or ``None`` if that ``k`` cannot satisfy the structural
rules — floors, tiers, or the max_segments cap — regardless of cash).

The one open-ended shape is ``build_staircase``; see the README for the
reasoning behind the "carve one new top segment out of the tail half of the
top floor block" rule.
"""

from __future__ import annotations

import math

from feasibility.models import CreditorRules


def position_floor(p: int, rules: CreditorRules) -> int:
    """The floor for the ``p``-th (1-based) creditor payment in a run.

    Max of: the base minimum (raised by 1 cent once the token-pay budget for
    sitting exactly at the base minimum is exhausted) and any applicable
    tier floor.
    """
    base = rules.min_payment_cents if p <= rules.max_token_pays else rules.min_payment_cents + 1
    tier = 0
    for from_payment, min_cents in rules.min_payment_tiers:
        if from_payment <= p:
            tier = max(tier, min_cents)
    return max(base, tier)


def floor_sequence(k: int, rules: CreditorRules) -> list[int]:
    """Non-decreasing floor for each of the ``k`` positions."""
    return [position_floor(p, rules) for p in range(1, k + 1)]


def _blocks(values: list[int]) -> list[tuple[int, int, int]]:
    """Contiguous equal-value runs of a non-decreasing list, as (start, end, value)."""
    blocks: list[tuple[int, int, int]] = []
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            blocks.append((start, i, values[start]))
            start = i
    return blocks


def build_even(offer_total: int, k: int) -> list[int]:
    """Equal payments; remainder cents go onto the *latest* payments (spec-mandated)."""
    base, remainder = divmod(offer_total, k)
    seq = [base] * k
    for i in range(k - remainder, k):
        seq[i] += 1
    return seq


def build_balloon(offer_total: int, k: int, rules: CreditorRules) -> list[int] | None:
    """Every payment but the last sits at its floor; the last absorbs the remainder."""
    floors = floor_sequence(k, rules)
    head_sum = sum(floors[:-1])
    last = offer_total - head_sum
    if last < floors[-1]:
        return None
    return floors[:-1] + [last]


def build_staircase(offer_total: int, k: int, rules: CreditorRules) -> list[int] | None:
    """Front-loaded staircase: floors first, remainder pushed as late as possible.

    See README "Payment shape interpretation" for the full reasoning. Summary:
    the floor sequence already partitions positions into contiguous equal-value
    blocks (from tiers / the token-pay cutoff) — that's the structural segment
    count. If there's budget left under max_segments, carve exactly one new
    top segment out of the tail half of the topmost floor block to absorb the
    remainder (never a lone final payment — that would read as a disguised
    balloon). Otherwise absorb the remainder by uniformly raising the entire
    topmost block.
    """
    floors = floor_sequence(k, rules)
    blocks = _blocks(floors)
    if len(blocks) > rules.max_segments:
        return None

    total_floor = sum(floors)
    remainder = offer_total - total_floor
    if remainder < 0:
        return None

    seq = floors[:]
    if remainder == 0:
        return seq

    base_levels = len(blocks)
    extra_budget = rules.max_segments - base_levels
    last_start, last_end, last_val = blocks[-1]
    last_size = last_end - last_start

    can_split = extra_budget >= 1 and last_size >= 3
    if can_split:
        new_size = math.ceil(last_size / 2)
        split_at = last_end - new_size
        inc, rem_cents = divmod(remainder, new_size)
        for idx in range(split_at, last_end):
            seq[idx] = last_val + inc
        for idx in range(last_end - rem_cents, last_end):
            seq[idx] += 1
    else:
        inc, rem_cents = divmod(remainder, last_size)
        for idx in range(last_start, last_end):
            seq[idx] += inc
        for idx in range(last_end - rem_cents, last_end):
            seq[idx] += 1
    return seq
