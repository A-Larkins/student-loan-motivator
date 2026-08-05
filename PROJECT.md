# PROJECT.md

Working notes, decisions, and backlog. README is the "what it is" doc; this is
the "why it is that way and what's next" doc.

**Rule for this file: no real balances, rates tied to real loans, or servicer
account details. This repo is public.**

---

## Status

| | |
|---|---|
| Phase | v1 built, real data loaded, running |
| Entry point | `StudentLoanMotivatorApp.py` |
| Storage | `~/Library/Application Support/StudentLoanMotivator/student_loans.json` |
| Verified | 4 suites: loan math, account payments, no-flicker, GUI — all passing |
| Packaging | `make_icon.py` + `build_app.py`, both dependency-free |

**Next action:** run it for a month, take a fresh snapshot on the 1st, see what
feels wrong.

---

## Decisions

### Snapshot model over simulation
The loans are ~9 years old. Replaying that history would be fiction. Instead the
user types the servicer's real number with a date, and the app accrues forward
from there. Re-snapshot monthly to resync.

*Consequence:* the live number is an estimate between snapshots and ground truth
at each one. Drift never compounds past a month. Also means capitalization, odd
fees, and servicer quirks self-heal at the next snapshot instead of needing to be
modeled.

### Penny precision, not sub-penny
Considered showing 5–6 decimals so digits visibly roll every second. Rejected —
at these balances the cent ticks every minute or two and that's fine. Two
decimals everywhere.

### Kill order = highest rate, smallest balance breaks ties
Not pure avalanche, not pure snowball. Sort key is `(-rate, balance)`. Avalanche
on rate because it's mathematically cheapest; snowball within a rate tier so a
kill lands sooner. Matches the strategy already settled on.

### Month is the unit
It's a bill and a debt, so the fight is monthly. The Race compares this calendar
month's accrued interest against this calendar month's payments. Break-even
(`total daily interest × days in month`) is the line that matters.

### Two buckets: principal and accrued interest
Federal loans accrue on principal only, and payments hit interest first. Keeping
them separate is a few extra lines and buys the payment autopsy, which is the
best dopamine in the app.

### Account-level payments
Autopay hits the *account*, not a loan — the servicer decides the split, and the
transaction history doesn't say which loan got what. So a payment may carry an
empty `loan_id`, meaning "whole account."

Those get expanded into synthetic per-loan payments split proportionally to
snapshot principal, tagged with `parent_id` so the UI regroups them into the one
payment that was actually made. It's an approximation, and deliberately so: the
next snapshot overwrites it with ground truth, so the error can't compound.

Payments targeted at a specific loan (the normal case when working the kill
order) bypass all of this and apply directly.

### Principal and accrued interest are separate fields
A servicer's "Current Balance" is principal **plus** unpaid interest. Loading the
whole balance as principal overstates the daily bleed, because federal interest
accrues on principal only. Both numbers get entered separately.

### Data never in the repo
Application Support path is used in *both* dev and frozen mode — deliberately no
code path writes financial data next to the script. `.gitignore` is backup, not
the primary defense.

### Dropped
- **Streak counter** — not wanted.
- **Sub-penny live counter** — see above.
- **Simulating from origination** — see snapshot model.

---

## Verification

Two scripts were used during the build (kept out of the repo, in scratchpad):

- **Loan math** — daily interest against the servicer-published example
  ($25,000 @ 6.8% → $4.65/day, matches to the penny), 30-day projection,
  interest-first payment split, pre-snapshot payments ignored, overpayment floors
  at zero, kill-order tie-break, month helpers incl. leap years and negative
  month arithmetic, freedom-date incl. the payment-below-interest case, blended
  rate, mid-month snapshot handling, input normalization.
- **GUI** — full widget construction, render path with seeded fake data, every
  modal opened and torn down, empty state, save/load round-trip.

Worth promoting these into a real `tests/` dir if the math grows.

---

## Backlog

### Probably next
- [ ] **Month-over-month card.** `snapshot_history` is already being written but
      nothing reads it. Show "principal down $X since last snapshot" — the single
      most honest progress number in the app.
- [ ] **Edit / delete a logged payment.** Currently add-only; a typo means hand-
      editing JSON.

### Maybe
- [ ] Payoff waterfall for the freedom date — pay the kill-order target down,
      roll its payment into the next. More accurate than the blended-rate
      estimate, and would make the projection respond to strategy.
- [ ] Milestone moments — flash/confetti at 25/50/75% and on each elimination.
- [ ] Extra-payment what-if slider: "$100 more per month → X months sooner."
- [ ] Chart of balance over time from `snapshot_history`.
- [ ] Import from a servicer CSV.

### Known rough edges
- **Reduced-payment / IDR plans.** The servicer's stated monthly minimum can be
  far below the interest accruing. The break-even line already exposes that gap,
  which is arguably the single most useful thing the app shows — but the app has
  no concept of the plan itself, or of an interest subsidy if one applies.
- **Scheduled-payoff comparison.** Servicers publish an estimated payment
  schedule and final payoff date. Contrasting that against the current pace is a
  strong motivator and is currently computed by hand, not in the app.

### Open questions
- Should an eliminated loan's original amount still count toward the lifetime
  payoff percentage? Currently **yes** — it makes the bar reflect the whole
  journey, including the two already killed.
- Break-even uses *current* daily interest × days in month. Slightly overstates
  when you pay early in the month. Precise enough? Probably, but worth revisiting
  if the number ever feels off.
- Subsidized loans in deferment: currently the workaround is rate 0 or omit.
  Worth a real "not accruing" flag?

---

## Log

**2026-08-05 (UI rebuild)** — The dashboard is now a single canvas instead of a
tree of frames and labels. Aqua overrides half of what you ask a native widget
for, and the panels wanted rounded corners, a progress ring, a rate-axis plot,
and hover states that no Tk widget offers. One `paint()` pass draws everything
from a model dict; there is exactly one code path whether the trigger was the
clock, a resize, a hover, or a payment landing.

Density now comes from hover rather than layout: every panel, loan row, and
ladder dot registers a rectangle in a hotspot list, and a borderless popover
follows the cursor with the detail that used to need its own column. Rows stay
one line tall as a result.

Also settled the vertical fight for good - panels are placed by arithmetic from
the canvas size, so no widget can win height at Kill Order's expense the way the
grid weights kept letting it.

**2026-08-05** — Added an "above the floor" readout to the hero card, opposite
the lifetime-progress line: total balance on every loan priced above the cheapest
rate tier, plus that slice as a percentage of the whole balance. The floor is
derived as the minimum active rate (with a 0.005-point tolerance so a matching
sub/unsub pair reads as one tier) rather than hard-coded, so it rises on its own
as tiers get killed. When only the floor tier remains, the line flips to a green
"everything left is cheap" note.

**2026-07-29** — Layout pass. The Race now measures interest from the 1st of the
month rather than from the snapshot instant: exact when the snapshot predates the
1st, estimated at the snapshot's daily rate (with intra-month payments added back
to principal) when it doesn't. A fresh snapshot no longer makes a month look
nearly interest-free.

Kill Order was invisible outside fullscreen for a second reason beyond the
earlier weight/leftover bug: the window geometry was a hard-coded 1180x1020,
larger than the display, so the window manager clamped it and the squeeze landed
on the weighted panel. Geometry is now derived from `winfo_screenheight()` minus
menu bar and Dock. Paired with collapsing loan rows from two lines to one (61px
to 32px) and a general tightening of fonts, card padding, and the burn-rate
block, all six loans now fit without scrolling.

**2026-07-28 (UI pass)** — Fixed two bugs found by screenshot. Every row in Kill
Order / Payments / Eliminated was being destroyed and rebuilt on each one-second
tick, which strobed the panels and left them blank mid-frame; rows are now built
once and only rebuilt when the underlying set changes, with a regression test
asserting zero widget churn across repeated refreshes. Separately, Aqua's native
`tk.Button` ignores `bg`, so every button rendered as a white slab — replaced
with click-bound Labels.

Kill Order was *still* empty after that, for an unrelated reason: the default
window was 1000x700, other cards consumed 584px, and the panel's `weight=1`
earned it only leftover space — 66px for 407px of rows. Fixed by enlarging the
default window and moving the panel into a scroller that only shows its bar when
needed, so no window size can clip it again.

Also: rebuilt the payment form (remembered default, -/+ steppers, presets, Today
button, Return-to-submit), added an explicit monthly target that overrides the
measured pace for the freedom-date projection, dropped the redundant "Account"
label from payment rows, generated an app icon with a dependency-free PNG
encoder, and added a .app bundler that wraps the source rather than freezing it.

**2026-07-28 (later)** — Real data loaded. Added account-level payments
(empty `loan_id`, proportional expansion, `parent_id` regrouping) so a real
transaction history could be imported, since servicer payments are account-wide.
Corrected the loan schema usage: a servicer "Current Balance" is principal plus
unpaid interest, and loading it all as principal overstated the daily bleed by
~3.5%. Every total now reconciles to the servicer's stated figures to the cent.
Added a payoff-order simulation showing that killing a small low-rate loan ahead
of strict avalanche order costs almost nothing and does not move the final payoff
month at all — worth surfacing in the app eventually.

**2026-07-28** — Design settled and v1 built. Researched the federal interest
formula (simple daily, 365.25-day year, interest-first payment allocation,
capitalization mostly retired post-2023). Chose the snapshot architecture over
simulation. Built the full app: hero total, The Race, Bleeding, Kill Order,
payments with per-payment interest/principal split, payment autopsy, freedom
date, elimination trophies, progress-driven accent color. Math and GUI verified.
Awaiting real loan data.
