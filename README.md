# Settlement Feasibility & Fee Engine

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

## Implementation notes

### Approach

`evaluate_offer` is a search over `(shape, k)`, where `shape` is dictated by
the creditor flags and `k` (number of creditor-payment dates, `1..min(max_payments,
max_terms, N)` where `N` is the count of cadence dates ≤ horizon) is searched
exhaustively — the space is tiny (`k ≤ ~24` in practice), so brute force is
simpler and more obviously correct than trying to prove which `k` is optimal
in closed form.

For a fixed `(shape, k)`, the payment amounts are constructed directly (see
"Payment shape interpretation" below), then **fee timing is not searched at
all** — it has a closed form. Given a fixed payment schedule, the SDA balance
trajectory *without* the program fee (call it `M(t)`) is fully determined by
the committed ledger + creditor payments + bank fees. The fee only subtracts
from that trajectory, so the question "collect the fee as early as possible"
reduces to: find the pointwise-maximal non-decreasing sequence `F(t) ≤ M(t)`.
That sequence is `F(i) = min(program_fee, min(M(t) for t ≥ i))` — proof: since
`F` is non-decreasing, `F(i) ≤ F(t) ≤ M(t)` for every `t ≥ i`, which forces
`F(i)` below that suffix minimum, and the bound is achievable because the
suffix minimum is itself non-decreasing in `i`. This is implemented in
`feasibility/simulate.py::fee_schedule`. Among the `(shape, k)` combinations
that clear both `min(M) ≥ 0` and `F` reaching the full program fee by the
horizon, the best one is scored by (earliest date the fee is fully collected,
total front-loadedness as a tie-break, larger `k` as a final tie-break).

Part 2 reuses that exact same feasibility search as a black-box predicate
with hypothetical extra credits merged into the ledger. Since adding money
never hurts feasibility, both the lump sum and the monthly increment are
found by plain binary search over integer cents (`feasibility/simulate.py::
minimal_feasible_amount`) rather than solved in closed form — this made the
guardrail cases and the "increment can exceed the lump" case (see below)
fall out for free instead of needing special-casing.

**Alternative considered:** an LP/ILP formulation (e.g. via `ortools`) that
jointly optimizes payment amounts and fee timing. I decided against it —
the objective ("as early as possible") isn't naturally an LP objective
without picking an arbitrary discount schedule, and the closed-form
suffix-min argument above is exact, cheap, and easy to test, so a solver
dependency would have added risk without adding correctness.

### Payment shape interpretation

- **`even_pays`**: spec-mandated — equal payments, remainder cents onto the
  latest payments.
- **`is_ballooning_allowed`** (and not even): I use a balloon *whenever it's
  legal*, not only when a flatter shape would be infeasible. Pinning every
  payment but the last at its floor is the most extreme expression of "keep
  creditor payments as low as the rules allow, early" — it strictly
  dominates any gentler shape for the front-load objective, so there's no
  reason not to use it when the creditor allows it. This matches
  `test_case3`'s expectation that the flag alone drives `pay_shape_used ==
  "balloon"`.
- **Staircase** (neither flag) — the genuinely open-ended case. My rule:
  1. Start from the per-position floor sequence (`min_payment_cents`, bumped
     by 1 cent once the `max_token_pays` budget for sitting exactly at the
     base minimum is exhausted, and raised further by any applicable
     `min_payment_tiers`). This sequence already partitions the `k`
     positions into contiguous equal-value blocks — that's the *structural*
     segment count, forced by the creditor's own rules.
  2. If the floors already sum to `offer_total`, ship them as-is.
  3. Otherwise there's a remainder to place. If there's no `max_segments`
     budget left beyond the structural blocks, absorb the remainder by
     raising the **entire topmost block** by an equal amount — this adds no
     new distinct level.
  4. If there is budget, carve out **one** new top segment from the **tail
     half** of the topmost block (`ceil(size/2)` payments) and size it to
     absorb the remainder exactly. I never let that carved segment be a
     single payment — if the tail half would be a singleton, I fall back to
     step 3 (raise the whole block together) instead. A lone elevated final
     payment is indistinguishable from a balloon, and constraint 8 gates
     balloons behind `is_ballooning_allowed`; I didn't want the staircase
     path to produce a de facto balloon through the back door.
  5. I deliberately only ever add **one** new segment even when
     `max_segments` allows more — `max_segments` is a cap, not a target, and
     fewer/later/larger steps front-load harder than many small ones.

  A remainder that doesn't divide evenly across a level still gets its
  leftover cents distributed one-by-one onto that level's latest payments
  (the same "as equal as possible" idea `even_pays` uses explicitly) — I
  don't count the resulting ≤1-cent spread within a level as a *new*
  segment; `max_segments` is about intentional step levels, not
  integer-division noise. `tests/test_engine.py::_count_payment_levels`
  encodes this precisely (a "level" is a maximal run of consecutive
  payments differing by ≤1 cent; more than that is a real step).

- I hand-verified this pipeline against all four provided cases before
  writing code — in particular `case4_tiers`'s floor sequence `[2500]×6 +
  [5000]×6` with `max_segments=2` fills the whole top block uniformly
  (`+2500` each → `7500`), matching the test's `payments[6:] >= 5000` check.

### Assumptions

- **`current_balance_cents` vs `creditor_balance_cents`.** §3 of
  `ASSIGNMENT.md` says the offer's balance field was renamed to
  `creditor_balance_cents`, but the provided `feasibility/models.py`
  (`Offer.current_balance_cents`, `load_offer`) and all four
  `cases/*/offer.json` fixtures still use `current_balance_cents`. Since the
  runnable code and the fixtures agree with each other, I kept
  `current_balance_cents` as the real field name rather than renaming
  something the graders' own fixtures depend on. If the intent really was
  the rename, `load_offer` would need a one-line key change — everything
  downstream is agnostic to the field's name.
- **Lump-sum placement date.** The spec says "an earlier lump is weakly more
  useful" and lets the solver choose the date, so I always place it at the
  earliest legal date, `as_of_date + 1 day` (the first date not already
  baked into `current_balance_cents`).
- **Balance check granularity.** "Running balance ≥ 0 at every date" is
  checked end-of-day (after that date's full credit total, then full debit
  total), not between individual same-day debits — same-day order among
  multiple debits is commutative for an end-of-day sum, so "credits before
  debits" only matters at the credit/debit boundary, which is what's
  implemented.
- **Fee-only dates beyond `k`.** Cadence continues (as fee-only dates, no
  bank fee) from the last creditor-payment date through the horizon; the
  solver can choose a `k` smaller than the max allowed specifically to free
  up trailing cadence dates purely for fee collection, and does so if that
  scores better.
- **Choosing `k`** is a brute-force search, scored by (earliest full fee
  collection, total front-loadedness, larger `k` as a tie-break) — see
  Approach above.

### Known edge cases / limitations

- **Structural infeasibility that no amount of money can fix** (e.g.
  `first_payment_date` already past the horizon, so there are zero legal
  cadence dates, or `min_payment_tiers` force more distinct floor levels
  than `max_segments` allows for every legal `k`). Part 2's binary search
  has a generous cap (`10**9` cents); if even that isn't feasible, both
  `FundsOption`s report `within_guardrail=False` with a reason noting the
  blocker is structural, not a funding gap, rather than returning a
  meaningless huge number as if it were a real minimum.
- **`max_payments`/`max_terms` redundancy** is handled as documented
  (`k ≤ min(max_payments, max_terms)`); I didn't invent a distinct meaning
  for the second field since the assignment explicitly flags them as
  currently redundant.
- The staircase construction is one defensible reading of an intentionally
  open-ended rule, not the only valid one — see "Payment shape
  interpretation" above for the reasoning I'd defend it on.
