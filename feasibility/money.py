"""Money helpers: exact round-half-up rounding for cent amounts.

Python's builtin ``round`` uses round-half-to-even, which the spec explicitly
forbids relying on. Everything here goes through ``Decimal`` so a ``.5`` cent
always rounds away from zero.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value: Decimal | float | int) -> int:
    """Round to the nearest integer; a ``.5`` always rounds away from zero."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pct_of_cents(pct: float, cents: int) -> int:
    """``round_half_up(pct * cents)``, computed via Decimal to avoid float drift."""
    return round_half_up(Decimal(str(pct)) * Decimal(cents))
