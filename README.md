# Student Loan Motivator

A desktop dashboard for the monthly fight against federal student loans. Interest
accrues to the second whether you look at it or not — this makes you look at it,
then scores whether you beat it.

Sibling app to Money Motivator, same bones: single-file Tkinter, JSON storage,
one-second tick, packaged as a macOS `.app`.

---

## Privacy first

**No real balance ever lives in this repo.**

The app reads and writes exactly one data file:

```
~/Library/Application Support/StudentLoanMotivator/student_loans.json
```

That path is used whether you run from source or from a packaged `.app` — there
is deliberately no code path that writes financial data next to the script, so a
stray `git add -A` cannot pick it up. `.gitignore` blocks the filenames anyway as
a second line of defense.

`example_loans.json` is a schema template. **Every number in it is invented.**

If you ever paste real numbers into an issue, a commit message, or a screenshot,
that is on you — the app itself keeps them out.

---

## The core idea: snapshots, not simulation

These loans are years old. The app never saw that history and shouldn't pretend
it did.

So instead of amortizing from origination:

1. You open your servicer and read the **actual current principal**.
2. You type it in with the date it was true. That's a **snapshot**.
3. The app accrues simple daily interest forward from that instant and subtracts
   payments you log.
4. Once a month you hit **Monthly Snapshot** and retype the real numbers, which
   resyncs everything and kills any accumulated drift.

The live counter is therefore an *estimate between snapshots* and *ground truth
at* each snapshot. That's the honest version, and it's the one that survives
servicer quirks, capitalization events, and fee oddities without lying to you.

Payments dated before a loan's snapshot are ignored on purpose — the snapshot
already reflects them.

### Two kinds of payment

**Targeted** — you picked a loan. Applies straight to it, interest first.

**Whole account** — how autopay usually works. The servicer decides the split and
the transaction history rarely says which loan got what, so the app spreads it
across active loans in proportion to principal. That's an approximation by
design; the next snapshot replaces it with ground truth, so error can't
accumulate.

### Balance is not principal

A servicer's *Current Balance* is unpaid principal **plus** unpaid interest.
Enter them in the separate fields — interest accrues on principal only, so
loading the whole balance as principal overstates your daily bleed.

---

## The math

Federal student loans use **simple daily interest** on principal only, with a
**365.25-day year**:

```
daily interest = current principal × (rate / 100) ÷ 365.25
```

Three consequences the app relies on:

- **Interest does not compound day to day.** Unpaid interest sits in its own
  bucket and does not itself earn interest, unless it *capitalizes*. Post-2023
  rules removed most capitalization triggers, so a loan in ordinary repayment
  keeps principal and accrued interest cleanly separate. The app models them as
  two buckets.
- **Payments hit accrued interest first, principal second.** This is why the
  payment autopsy can tell you how much of a payment actually shrank the debt
  versus just paying rent.
- **Rates are fixed for the life of each loan**, set by disbursement year, so
  every loan carries its own rate forever.

Sanity check: the servicer-published example of $25,000 at 6.8% yields
$25,000 × 0.068 ÷ 365.25 = **$4.65/day**, which the app reproduces to the penny.

Reference rates by disbursement year — undergrad / grad unsub / PLUS:

| Year | Undergrad | Grad Unsub | PLUS |
|---|---|---|---|
| 2026–27 | 6.52% | 8.07% | 9.07% |
| 2025–26 | 6.39% | 7.94% | 8.94% |
| 2024–25 | 6.53% | 8.08% | 9.08% |
| 2023–24 | 5.50% | 7.05% | 8.05% |

Sources: [MOHELA – Student Loan Interest](https://mohela.studentaid.gov/DL/resourceCenter/StudentLoanInterest.aspx) ·
[StudentAid.gov – Interest Rates](https://studentaid.gov/understand-aid/types/loans/interest-rates) ·
[FSA Partners – 2026-27 Direct Loan rates](https://fsapartners.ed.gov/knowledge-center/library/electronic-announcements/2026-06-04/interest-rates-federal-direct-loans-first-disbursed-between-july-1-2026-and-june-30-2027)

---

## What's on screen

**Total Owed** — principal plus accrued interest, live to the penny. Rolls when
it rolls; at typical balances that's every minute or two. The number is colored
by lifetime payoff progress: red when you're drowning, amber mid-fight, green as
it dies.

**The Race** — the centerpiece. Two bars for the current month: interest accrued
versus what you paid. The dividing line is **break-even**, the total daily
interest times days in the month — the rent you owe just to not go backwards.
Clear it and you're WINNING, with the overage stated in dollars. Don't and it
tells you how short you are and how many days are left.

**vs Inflation** — CPI-U pulled live from the BLS public API: headline and core
twelve-month change, plus three- and six-month annualized. Your blended rate
minus headline CPI is the **real rate** — above zero the debt genuinely costs
you, below zero inflation is retiring it faster than it accrues. The rate ladder
plots every loan against the CPI line, dot size by balance, so which side of the
line each loan sits on is a glance rather than a calculation. Figures cache for
twelve hours and fall back to bundled values offline.

**Kill Order** — loans ranked by **highest interest rate first, smallest balance
breaking ties**. The top one wears a TARGET tag. Avalanche on rate because that's
mathematically cheapest, snowball on balance within a rate tier so you get a kill
sooner. Each row shows what that specific loan costs you per day, and the rate is
colored by whether it beats inflation.

**Hover anything** — the dashboard is drawn on a canvas, and every panel, row,
and dot has a detail popover behind it: per-loan principal/interest split, real
rate, daily and monthly cost, percent paid, snapshot age. Rows stay one line tall
because the depth lives in the hover.

**Payments This Month** — every payment logged in the current calendar month,
each broken into what went to interest versus principal.

**Log Payment** — remembers your last amount as the default, with −/+ steppers
in $25 increments and one-tap presets. Pick a specific loan (the kill-order
target is flagged) or log it against the whole account.

**Payment Autopsy** — fires when you log a payment. Shows the interest/principal
split, the new balance, how many days closer your freedom date moved, and
estimated lifetime interest saved.

**Freedom Date** — projected payoff at a blended rate across active loans.
Click it to set a monthly target; without one it falls back to your measured
three-month average, so the number is always grounded in either a commitment or
a measurement — never a guess.

**Eliminated** — dead loans stay visible, struck through, as trophies.

---

## Running it

Needs Python 3.10+ with Tkinter (stock macOS `python3` has it). No third-party
packages — the whole app, the icon generator, and the bundler are stdlib only.

```bash
python3 StudentLoanMotivatorApp.py
```

First run shows an empty dashboard. Hit **Manage Loans → + Add Loan** and enter,
per loan: nickname, rate, original amount (optional, drives the % paid-off stat),
current principal, unpaid interest, and the snapshot date.

Keys: `F11` fullscreen, `Esc` exit fullscreen.

### Installing it as a real app

```bash
python3 make_icon.py     # writes icon.png and icon.icns
python3 build_app.py     # builds into ~/Applications
```

Then open it once from Launchpad, right-click the Dock icon, and choose
**Options → Keep in Dock**.

The bundle is a few kilobytes: its executable is a shell script that runs the
Python source in place. That means **editing the `.py` updates the app** — no
rebuild needed. Only re-run `build_app.py` if you move the project or change the
icon. It reads and writes the same Application Support path, so installing
doesn't move or duplicate your data.

---

## Data format

See `example_loans.json` for the full schema with fake values. In short:

- `loans[]` — `id`, `name`, `rate`, `original_amount`, `snapshot_principal`,
  `snapshot_accrued`, `snapshot_at`, `status` (`active` | `eliminated`),
  `eliminated_on`
- `payments[]` — `id`, `loan_id`, `amount`, `date`, `note`
- `snapshot_history[]` — append-only log of every snapshot taken
- `monthly_minimum` — required payment floor, used for the freedom-date estimate

Deleting a loan also deletes its payments and snapshot history.

---

## Caveats

- Interest between snapshots is an estimate. Your servicer is authoritative;
  re-snapshot monthly and the two stay close.
- Capitalization is not modeled. If a loan capitalizes, just take a fresh
  snapshot — the new principal absorbs it.
- Freedom date uses a blended rate across active loans, not a per-loan payoff
  waterfall. It's a motivator, not an amortization schedule.
- Subsidized loans in an in-school or deferment period still accrue in this app.
  Set the rate to 0 for those, or leave them out until repayment starts.
