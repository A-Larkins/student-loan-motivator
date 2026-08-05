"""Student Loan Motivator - watch the interest bleed, then go kill it.

The mental model is a monthly battle. Every month interest accrues whether you
look at it or not, and every month you throw payments at it. The app scores that
fight.

Balances are SNAPSHOT-based, not simulated from origination. You type in the real
number your servicer shows you, the app accrues simple daily interest forward
from that instant, and once a month you re-snapshot to resync with reality. That
keeps a nine-year-old loan honest without pretending to replay nine years of
history the app never saw.

Privacy: every real number lives in ~/Library/Application Support/, never in the
repo. See README.md.
"""

import json
import math
import random
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "StudentLoanMotivator"

if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR))
else:
    RESOURCE_DIR = SCRIPT_DIR

# Real data ALWAYS goes to Application Support, even running from source. This is
# deliberate: the repo is public, so there must be no code path that writes
# balances next to the script where a stray `git add -A` could scoop them up.
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
DATA_PATH = APP_SUPPORT_DIR / "student_loans.json"
QUOTES_PATH = RESOURCE_DIR / "loan_quotes.json"

# ---------------------------------------------------------------------------
# Loan math
#
# Federal loans use SIMPLE daily interest on principal only, with a 365.25-day
# year (confirmed against servicer documentation - see README sources):
#
#     daily interest = principal x (rate / 100) / 365.25
#
# Unpaid interest sits in its own bucket and does NOT itself earn interest unless
# it capitalizes, and post-2023 rules removed most capitalization triggers. So
# for a loan in ordinary repayment, principal and accrued interest stay separate.
# Payments pay accrued interest first, principal second.
# ---------------------------------------------------------------------------

DAYS_PER_YEAR = 365.25
SECONDS_PER_DAY = 86400.0

TICK_MS = 1000
QUOTE_ROTATION_SECONDS = 240

# ---------------------------------------------------------------------------
# Theme - ember to emerald. The accent color is computed live from payoff
# progress, so the app literally warms up as the debt dies.
#
# The dashboard is drawn on a single canvas rather than assembled from Tk
# widgets: Aqua overrides half of what you ask a native widget for, and the
# panels here want rounded corners, arcs, and hover states that no Tk widget
# offers. Canvas items also don't reflow the window, so a full repaint every
# second is calm - the old widget-per-row build strobed once a second.
# ---------------------------------------------------------------------------

THEME_BG = "#0d0709"        # near-black plum, the deck itself
THEME_PANEL = "#1c0f14"     # panel fill
THEME_PANEL_ALT = "#2a151c"  # raised rows, track fills
THEME_HOVER = "#3a1d26"     # row under the cursor
THEME_BORDER = "#48212b"    # panel edge
THEME_BORDER_HI = "#7a2b36"  # emphasized edge
THEME_TEXT = "#fdf0e6"
THEME_MUTED = "#c9a3a8"
THEME_DIM = "#8d6b70"
THEME_FAINT = "#5d454a"
THEME_DANGER = "#f87171"
THEME_GOOD = "#4ade80"
THEME_WARN = "#fbbf24"
ENTRY_BG = "#fdf4ec"
ENTRY_FG = "#1a0b0e"

INFLATION_UNDER = "#166534"  # balance below CPI - inflation's problem, not yours

ACCENT_LOW = "#ef4444"   # drowning
ACCENT_MID = "#f59e0b"   # fighting
ACCENT_HIGH = "#22c55e"  # winning

FONT_TITLE = ("Avenir Next", 15, "bold")
FONT_CARD = ("Avenir Next", 10, "bold")
FONT_LABEL = ("Avenir Next", 9, "bold")
FONT_BODY = ("Avenir Next", 9)
FONT_SMALL = ("Avenir Next", 8)
FONT_TINY = ("Avenir Next", 7, "bold")
FONT_HERO = ("Menlo", 30, "bold")
FONT_VALUE_L = ("Menlo", 17, "bold")
FONT_VALUE_M = ("Menlo", 13, "bold")
FONT_VALUE_S = ("Menlo", 11, "bold")
FONT_MONO = ("Menlo", 9)
FONT_MONO_S = ("Menlo", 8)

PANEL_PAD = 14  # inset from a panel's edge to its content

WIN_WIDTH = 1240
WIN_HEIGHT = 1020
MIN_WIDTH = 1000
MIN_HEIGHT = 640

DEFAULT_QUOTES = [
    "Every dollar of principal you kill never charges you rent again.",
    "The interest never takes a day off. Neither does your plan.",
    "Small payments, made relentlessly, beat big payments made someday.",
    "You are not paying a bill. You are buying back your future income.",
    "The balance only looks big until you start swinging at it.",
    "Compound interest works for whoever is patient. Make that you.",
    "A loan eliminated is a raise that lasts forever.",
    "Attack the rate, not the fear.",
    "Debt is a countdown, not a life sentence.",
    "The month you beat the interest is the month the math flips.",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def money(value: float) -> str:
    return f"${value:,.2f}"


def parse_date_str(raw: str) -> date:
    return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()


def parse_dt_str(raw: str) -> datetime:
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.combine(parse_date_str(text), time.min)


def daily_interest(principal: float, rate_pct: float) -> float:
    if principal <= 0 or rate_pct <= 0:
        return 0.0
    return principal * (rate_pct / 100.0) / DAYS_PER_YEAR


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    if day.month == 12:
        return day.replace(year=day.year + 1, month=1, day=1) - timedelta(days=1)
    return day.replace(month=day.month + 1, day=1) - timedelta(days=1)


def days_in_month(day: date) -> int:
    return month_end(day).day


def add_months(start: date, count: int) -> date:
    total = start.month - 1 + count
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, days_in_month(date(year, month, 1)))
    return date(year, month, day)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def lerp_color(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return "#{:02x}{:02x}{:02x}".format(
        round(r1 + (r2 - r1) * t),
        round(g1 + (g2 - g1) * t),
        round(b1 + (b2 - b1) * t),
    )


def progress_accent(fraction: float) -> str:
    """Red when you're drowning, amber mid-fight, green when it's nearly dead."""
    fraction = max(0.0, min(1.0, fraction))
    if fraction < 0.5:
        return lerp_color(ACCENT_LOW, ACCENT_MID, fraction * 2.0)
    return lerp_color(ACCENT_MID, ACCENT_HIGH, (fraction - 0.5) * 2.0)


# ---------------------------------------------------------------------------
# Projection - roll a loan forward from its snapshot
# ---------------------------------------------------------------------------

def project_loan(loan: dict, payments: list[dict], until: datetime) -> dict:
    """Advance a loan from its snapshot instant to `until`.

    Payments dated before the snapshot are ignored on purpose: a snapshot is a
    statement of ground truth, so anything older is already baked into it.
    """
    rate = float(loan.get("rate", 0.0))
    principal = max(0.0, float(loan.get("snapshot_principal", 0.0)))
    accrued = max(0.0, float(loan.get("snapshot_accrued", 0.0)))
    snapshot_at = parse_dt_str(loan.get("snapshot_at") or datetime.now().isoformat())
    snapshot_day = snapshot_at.date()

    cursor = snapshot_at
    interest_since_snapshot = 0.0
    interest_paid = 0.0
    principal_paid = 0.0
    payments_applied: list[dict] = []

    if until < cursor:
        until = cursor

    mine = []
    for payment in payments:
        if payment.get("loan_id") != loan.get("id"):
            continue
        try:
            pay_day = parse_date_str(payment.get("date"))
        except (ValueError, TypeError):
            continue
        if pay_day < snapshot_day:
            continue
        mine.append((pay_day, payment))
    mine.sort(key=lambda item: item[0])

    for pay_day, payment in mine:
        when = max(datetime.combine(pay_day, time.min), snapshot_at)
        if when > until:
            break
        if when > cursor:
            elapsed = (when - cursor).total_seconds() / SECONDS_PER_DAY
            gained = daily_interest(principal, rate) * elapsed
            accrued += gained
            interest_since_snapshot += gained
            cursor = when

        amount = max(0.0, float(payment.get("amount", 0.0)))
        to_interest = min(amount, accrued)
        accrued -= to_interest
        to_principal = min(amount - to_interest, principal)
        principal -= to_principal
        interest_paid += to_interest
        principal_paid += to_principal
        payments_applied.append(
            {
                "payment": payment,
                "date": pay_day,
                "amount": amount,
                "to_interest": to_interest,
                "to_principal": to_principal,
            }
        )

    if until > cursor:
        elapsed = (until - cursor).total_seconds() / SECONDS_PER_DAY
        gained = daily_interest(principal, rate) * elapsed
        accrued += gained
        interest_since_snapshot += gained

    return {
        "loan": loan,
        "id": loan.get("id"),
        "name": loan.get("name", "Loan"),
        "rate": rate,
        "principal": principal,
        "accrued": accrued,
        "total": principal + accrued,
        "daily": daily_interest(principal, rate),
        "interest_since_snapshot": interest_since_snapshot,
        "interest_paid": interest_paid,
        "principal_paid": principal_paid,
        "payments_applied": payments_applied,
        "original": max(0.0, float(loan.get("original_amount", 0.0))),
        "snapshot_at": snapshot_at,
    }


ACCOUNT = ""  # loan_id of a payment made to the account as a whole


def expand_payments(payments: list[dict], loans: list[dict]) -> list[dict]:
    """Turn account-level payments into per-loan synthetic payments.

    Autopay usually hits the account, not a loan, and the servicer spreads it
    however it likes. We approximate that split proportionally to each active
    loan's snapshot principal. It's an approximation on purpose - the next
    monthly snapshot overwrites it with ground truth, so error can't compound.

    Synthetic rows carry `parent_id` so the UI can re-group them back into the
    single payment the user actually made.
    """
    active = [l for l in loans if l.get("status") == "active"]
    total = sum(max(0.0, float(l.get("snapshot_principal", 0.0))) for l in active)

    expanded: list[dict] = []
    for payment in payments:
        if payment.get("loan_id"):
            expanded.append(payment)
            continue
        if total <= 0:
            continue
        for loan in active:
            share = max(0.0, float(loan.get("snapshot_principal", 0.0))) / total
            if share <= 0:
                continue
            expanded.append(
                {
                    **payment,
                    "loan_id": loan["id"],
                    "amount": float(payment.get("amount", 0.0)) * share,
                    "parent_id": payment["id"],
                }
            )
    return expanded


def month_interest_for(loan: dict, payments: list[dict], now: datetime) -> float:
    """Interest this loan has accrued since the 1st of the current month.

    When the snapshot predates the 1st this is exact — we just project to both
    instants and subtract.

    When the snapshot lands mid-month, the balance on the 1st was never recorded,
    so the days before it are estimated: take the snapshot principal, add back
    any payments made earlier in the month (those dollars were still in the
    balance then), and accrue at that rate. Principal barely moves inside a
    single month, so the error is pennies — and it beats the alternative of
    reporting a month as nearly interest-free just because the snapshot is new.
    """
    start = datetime.combine(month_start(now.date()), time.min)
    snapshot_at = parse_dt_str(loan.get("snapshot_at") or now.isoformat())

    if snapshot_at <= start:
        at_now = project_loan(loan, payments, now)
        at_start = project_loan(loan, payments, start)
        return max(0.0, at_now["interest_since_snapshot"] - at_start["interest_since_snapshot"])

    rate = float(loan.get("rate", 0.0))
    principal = max(0.0, float(loan.get("snapshot_principal", 0.0)))
    for payment in payments:
        if payment.get("loan_id") != loan.get("id"):
            continue
        try:
            pay_day = parse_date_str(payment.get("date"))
        except (ValueError, TypeError):
            continue
        if start.date() <= pay_day < snapshot_at.date():
            principal += max(0.0, float(payment.get("amount", 0.0)))

    estimated_days = max(0.0, (min(now, snapshot_at) - start).total_seconds() / SECONDS_PER_DAY)
    estimated = daily_interest(principal, rate) * estimated_days

    if now <= snapshot_at:
        return estimated
    return estimated + project_loan(loan, payments, now)["interest_since_snapshot"]


def kill_order(snapshots: list[dict]) -> list[dict]:
    """Highest rate first; among equal rates, smallest balance first.

    That's the hybrid you asked for - avalanche on rate (mathematically the
    cheapest), snowball on balance as the tiebreak so you get a kill quickly.
    """
    return sorted(snapshots, key=lambda s: (-s["rate"], s["total"]))


def above_floor_split(snapshots: list[dict]) -> dict:
    """Balance sitting above the cheapest rate tier, and its share of the total.

    The floor is read from the loans rather than pinned to 2.75% so it follows
    the data - kill the cheap tier and the floor rises on its own. Loans within
    a hundredth of a point of the lowest rate count as the same tier, which is
    how the 2.75% pair reads as one floor instead of two.
    """
    total = sum(s["total"] for s in snapshots)
    if not snapshots or total <= 0:
        return {"floor": 0.0, "above": 0.0, "at_floor": 0.0, "total": total,
                "fraction": 0.0, "count": 0}

    floor = min(s["rate"] for s in snapshots)
    above = [s for s in snapshots if s["rate"] > floor + 0.005]
    above_total = sum(s["total"] for s in above)
    return {
        "floor": floor,
        "above": above_total,
        "at_floor": total - above_total,
        "total": total,
        "fraction": above_total / total,
        "count": len(above),
    }


def blended_rate(snapshots: list[dict]) -> float:
    total = sum(s["principal"] for s in snapshots)
    if total <= 0:
        return 0.0
    return sum(s["principal"] * s["rate"] for s in snapshots) / total


def freedom_date(balance: float, rate_pct: float, monthly_payment: float, today: date):
    """Standard amortization payoff estimate. Returns (date, months) or (None, None)."""
    if balance <= 0:
        return today, 0
    if monthly_payment <= 0:
        return None, None
    monthly_rate = (rate_pct / 100.0) / 12.0
    if monthly_rate <= 0:
        months = balance / monthly_payment
    else:
        if monthly_payment <= balance * monthly_rate:
            return None, None  # payment doesn't even cover interest
        months = -math.log(1 - (balance * monthly_rate) / monthly_payment) / math.log(1 + monthly_rate)
    months = max(1, math.ceil(months))
    if months > 12 * 60:
        return None, None
    return add_months(today, months), months


# ---------------------------------------------------------------------------
# Inflation
#
# A 2.75% loan while prices rise 3.5% isn't really costing 2.75% - it's being
# repaid in dollars worth less than the ones borrowed, so the real rate is
# negative and time is on your side. Above the CPI line the opposite is true.
#
# Numbers come straight from the BLS public API: no key, no dependency, stdlib
# urllib. Series used:
#
#   CUUR0000SA0     headline CPI-U, not seasonally adjusted
#   CUUR0000SA0L1E  core CPI-U (less food and energy), NSA
#   CUSR0000SA0     headline, seasonally adjusted
#   CUSR0000SA0L1E  core, seasonally adjusted
#
# Twelve-month changes read off the NSA series because that IS the published
# headline number. The 3- and 6-month annualized figures read off the SA series
# instead - over a short window NSA data is mostly seasonality, which is how you
# get a "6.2% inflation" reading in a 3.5% year.
# ---------------------------------------------------------------------------

BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
CPI_HEADLINE = "CUUR0000SA0"
CPI_CORE = "CUUR0000SA0L1E"
CPI_HEADLINE_SA = "CUSR0000SA0"
CPI_CORE_SA = "CUSR0000SA0L1E"
INFLATION_TIMEOUT = 8.0
INFLATION_MAX_AGE_HOURS = 12.0

# Bundled fallback so the card is never blank offline. Overwritten in the data
# file the first time BLS answers.
FALLBACK_INFLATION = {
    "headline": 3.53,
    "core": 2.59,
    "headline_3mo": 2.78,
    "headline_6mo": 4.05,
    "core_3mo": 2.29,
    "as_of": "Jun 2026",
    "live": False,
    "fetched_at": "",
}

MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _cpi_points(series: dict) -> dict[tuple[int, int], float]:
    """(year, month) -> index value, skipping anything unpublished."""
    points: dict[tuple[int, int], float] = {}
    for row in series.get("data", []):
        period = str(row.get("period", ""))
        if not period.startswith("M") or period == "M13":  # M13 is the annual average
            continue
        try:
            points[(int(row["year"]), int(period[1:]))] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue  # BLS prints "-" for months it never published (the 2025 lapse)
    return points


def _months_back(key: tuple[int, int], count: int) -> tuple[int, int]:
    year, month = key
    month -= count
    while month <= 0:
        month += 12
        year -= 1
    return year, month


def _annualized(points: dict, latest: tuple[int, int], back: int, per_year: float) -> float | None:
    """Percent change over `back` months, raised to `per_year` to annualize."""
    start = points.get(_months_back(latest, back))
    end = points.get(latest)
    if not start or not end or start <= 0:
        return None
    return ((end / start) ** per_year - 1.0) * 100.0


def fetch_inflation(timeout: float = INFLATION_TIMEOUT) -> dict | None:
    """Pull CPI-U from BLS. Returns None on any failure - the caller keeps its cache."""
    this_year = datetime.now().year
    body = json.dumps({
        "seriesid": [CPI_HEADLINE, CPI_CORE, CPI_HEADLINE_SA, CPI_CORE_SA],
        "startyear": str(this_year - 2),
        "endyear": str(this_year),
    }).encode("utf-8")
    request = urllib.request.Request(
        BLS_API_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
        return None

    series = payload.get("Results", {}).get("series", [])
    points = {s.get("seriesID"): _cpi_points(s) for s in series if isinstance(s, dict)}
    headline = points.get(CPI_HEADLINE) or {}
    if not headline:
        return None

    latest = max(headline)
    headline_yoy = _annualized(headline, latest, 12, 1.0)
    if headline_yoy is None:
        return None

    core = points.get(CPI_CORE) or {}
    head_sa = points.get(CPI_HEADLINE_SA) or {}
    core_sa = points.get(CPI_CORE_SA) or {}
    sa_latest = max(head_sa) if head_sa else None
    core_sa_latest = max(core_sa) if core_sa else None

    return {
        "headline": headline_yoy,
        "core": _annualized(core, max(core), 12, 1.0) if core else None,
        "headline_3mo": _annualized(head_sa, sa_latest, 3, 4.0) if sa_latest else None,
        "headline_6mo": _annualized(head_sa, sa_latest, 6, 2.0) if sa_latest else None,
        "as_of": f"{MONTH_ABBR[latest[1] - 1]} {latest[0]}",
        "live": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def inflation_verdict(snapshots: list[dict], cpi: float) -> dict:
    """Split active balance by whether its rate outruns inflation."""
    above = [s for s in snapshots if s["rate"] > cpi]
    below = [s for s in snapshots if s["rate"] <= cpi]
    total = sum(s["total"] for s in snapshots)
    above_total = sum(s["total"] for s in above)
    return {
        "above": above_total,
        "below": total - above_total,
        "above_count": len(above),
        "below_count": len(below),
        "fraction": (above_total / total) if total > 0 else 0.0,
        "real_blended": blended_rate(snapshots) - cpi,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def blank_data() -> dict:
    return {
        "loans": [],
        "payments": [],
        "snapshot_history": [],
        "monthly_minimum": 0.0,
        "monthly_target": 0.0,
        "default_payment": 375.0,
        "inflation": {},
    }


def normalize_loan(raw: dict, index: int) -> dict:
    now_iso = datetime.now().isoformat(timespec="seconds")
    loan = {
        "id": str(raw.get("id") or uuid.uuid4()),
        "name": str(raw.get("name") or f"Loan {index + 1}").strip() or f"Loan {index + 1}",
        "rate": max(0.0, float(raw.get("rate", 0.0) or 0.0)),
        "original_amount": max(0.0, float(raw.get("original_amount", 0.0) or 0.0)),
        "snapshot_principal": max(0.0, float(raw.get("snapshot_principal", 0.0) or 0.0)),
        "snapshot_accrued": max(0.0, float(raw.get("snapshot_accrued", 0.0) or 0.0)),
        "snapshot_at": str(raw.get("snapshot_at") or now_iso),
        "status": "eliminated" if raw.get("status") == "eliminated" else "active",
        "eliminated_on": str(raw.get("eliminated_on") or ""),
        "note": str(raw.get("note") or ""),
    }
    try:
        parse_dt_str(loan["snapshot_at"])
    except (ValueError, TypeError):
        loan["snapshot_at"] = now_iso
    return loan


def normalize_payment(raw: dict, index: int) -> dict | None:
    try:
        amount = float(raw.get("amount", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    try:
        day = parse_date_str(raw.get("date"))
    except (ValueError, TypeError):
        return None
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "loan_id": str(raw.get("loan_id") or ""),
        "amount": amount,
        "date": day.isoformat(),
        "note": str(raw.get("note") or ""),
    }


def load_data() -> dict:
    if not DATA_PATH.exists():
        return blank_data()
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return blank_data()
    if not isinstance(raw, dict):
        return blank_data()

    data = blank_data()
    data["loans"] = [normalize_loan(item, i) for i, item in enumerate(raw.get("loans", [])) if isinstance(item, dict)]
    payments = []
    for i, item in enumerate(raw.get("payments", [])):
        if isinstance(item, dict):
            normalized = normalize_payment(item, i)
            if normalized:
                payments.append(normalized)
    data["payments"] = payments
    history = []
    for item in raw.get("snapshot_history", []):
        if isinstance(item, dict) and item.get("at"):
            history.append(item)
    data["snapshot_history"] = history
    for key, fallback in (("monthly_minimum", 0.0), ("monthly_target", 0.0), ("default_payment", 375.0)):
        try:
            data[key] = max(0.0, float(raw.get(key, fallback) or 0.0))
        except (TypeError, ValueError):
            data[key] = fallback
    cached = raw.get("inflation")
    data["inflation"] = cached if isinstance(cached, dict) else {}
    return data


def save_data(data: dict) -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def load_quotes() -> list[str]:
    if QUOTES_PATH.exists():
        try:
            raw = json.loads(QUOTES_PATH.read_text(encoding="utf-8"))
            quotes = [str(q).strip() for q in raw if str(q).strip()]
            if quotes:
                return quotes
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return list(DEFAULT_QUOTES)


# ---------------------------------------------------------------------------
# Canvas progress bar - ttk styling can't do per-bar colors cleanly, and the
# whole point here is that the color carries meaning.
# ---------------------------------------------------------------------------

def rounded(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, radius: float,
            fill: str, outline: str = "", width: int = 1, **kwargs) -> int:
    """Rounded rectangle as a single smoothed polygon.

    Tk has no rounded-rect primitive. Doubling each corner point and asking for
    a spline rounds exactly the corners and leaves the edges straight, which is
    one item per panel instead of four arcs plus two rectangles.
    """
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
        x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(
        points, smooth=True, splinesteps=16, fill=fill,
        outline=outline, width=width, **kwargs
    )


def track_bar(canvas: tk.Canvas, x: float, y: float, width: float, height: float,
              fraction: float, color: str, track: str = THEME_PANEL_ALT, **kwargs) -> None:
    """Pill-shaped progress bar: full-width track, fill clipped to fraction."""
    radius = height / 2
    rounded(canvas, x, y, x + width, y + height, radius, track, **kwargs)
    filled = max(0.0, min(1.0, fraction)) * width
    if filled > 1:
        rounded(canvas, x, y, x + max(filled, height * 0.8), y + height, radius, color, **kwargs)


def donut(canvas: tk.Canvas, cx: float, cy: float, radius: float, thickness: float,
          fraction: float, color: str, track: str = THEME_PANEL_ALT) -> None:
    """Progress ring, drawn clockwise from twelve o'clock."""
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    canvas.create_arc(*box, start=0, extent=359.99, style="arc", outline=track, width=thickness)
    sweep = max(0.0, min(1.0, fraction)) * 359.99
    if sweep > 0.5:
        canvas.create_arc(*box, start=90, extent=-sweep, style="arc", outline=color, width=thickness)


class Popover:
    """Hover detail panel - a borderless window that follows the cursor.

    This is where the density comes from: rows stay one line tall, and anything
    you'd have to widen a panel to show lives in here instead.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self.frame = tk.Frame(self.win, bg=THEME_BORDER_HI, padx=1, pady=1)
        self.frame.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.frame, bg=THEME_PANEL, padx=13, pady=11)
        self.inner.pack(fill="both", expand=True)
        self.title = tk.Label(self.inner, text="", font=FONT_CARD, bg=THEME_PANEL,
                              fg=THEME_TEXT, anchor="w", justify="left")
        self.title.pack(anchor="w")
        self.rows = tk.Frame(self.inner, bg=THEME_PANEL)
        self.rows.pack(anchor="w", pady=(7, 0))
        self.rows.grid_columnconfigure(1, weight=1)
        self.row_widgets: list[tk.Widget] = []
        self.key = None

    def show(self, key, title: str, lines: list[tuple[str, str, str]], x: int, y: int) -> None:
        """lines is (label, value, value color). Rebuilt only when the key changes."""
        if key != self.key:
            self.key = key
            self.title.configure(text=title)
            for widget in self.row_widgets:
                widget.destroy()
            self.row_widgets = []
            for i, (label, value, color) in enumerate(lines):
                if not label and not value:  # spacer
                    spacer = tk.Frame(self.rows, bg=THEME_PANEL, height=6)
                    spacer.grid(row=i, column=0, columnspan=2)
                    self.row_widgets.append(spacer)
                    continue
                left = tk.Label(self.rows, text=label, font=FONT_SMALL, bg=THEME_PANEL,
                                fg=THEME_DIM, anchor="w")
                left.grid(row=i, column=0, sticky="w", padx=(0, 18))
                right = tk.Label(self.rows, text=value, font=FONT_MONO, bg=THEME_PANEL,
                                 fg=color, anchor="e")
                right.grid(row=i, column=1, sticky="e")
                self.row_widgets.extend((left, right))

        self.win.update_idletasks()
        width = self.win.winfo_reqwidth()
        height = self.win.winfo_reqheight()
        # Flip to the other side of the cursor rather than run off the screen.
        if x + width + 24 > self.root.winfo_screenwidth():
            x -= width + 22
        else:
            x += 18
        y = min(y + 16, self.root.winfo_screenheight() - height - 12)
        self.win.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
        self.win.deiconify()
        self.win.lift()

    def hide(self) -> None:
        if self.key is not None:
            self.key = None
            self.win.withdraw()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class StudentLoanMotivatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.data = load_data()
        self.quotes = load_quotes()
        self.current_quote = random.choice(self.quotes)
        self._quote_slot = -1

        # Bundled numbers first so the card renders instantly; the cached ones
        # win if they exist, and a live fetch overwrites both a moment later.
        self._inflation_pending: dict | None = None
        self.inflation = dict(FALLBACK_INFLATION)
        cached = self.data.get("inflation") or {}
        if cached.get("headline") is not None:
            self.inflation.update(cached)

        root.title("Student Loan Motivator")
        root.configure(bg=THEME_BG)
        # A hard-coded size larger than the screen gets clamped by the window
        # manager. The deck lays itself out from the canvas size, so clamping is
        # harmless now, but starting inside the screen still avoids a resize
        # flash on launch.
        usable_w = root.winfo_screenwidth() - 60
        usable_h = root.winfo_screenheight() - 150  # menu bar + Dock
        width = max(MIN_WIDTH, min(WIN_WIDTH, usable_w))
        height = max(MIN_HEIGHT, min(WIN_HEIGHT, usable_h))
        root.minsize(min(MIN_WIDTH, width), min(MIN_HEIGHT, height))
        root.geometry(f"{width}x{height}+{max(0, (usable_w - width) // 2)}+30")

        root.bind("<Escape>", self.exit_fullscreen)
        root.bind("<F11>", self.toggle_fullscreen)

        self.build_ui()
        self.tick()
        self.refresh_inflation()

    # -- inflation --------------------------------------------------------

    def inflation_is_stale(self) -> bool:
        stamp = str(self.inflation.get("fetched_at") or "")
        if not stamp:
            return True
        try:
            age = datetime.now() - parse_dt_str(stamp)
        except (ValueError, TypeError):
            return True
        return age.total_seconds() > INFLATION_MAX_AGE_HOURS * 3600

    def refresh_inflation(self) -> None:
        """Kick off a background CPI fetch. Never blocks the UI, never raises.

        CPI prints once a month, so a stale cache costs nothing and the fetch is
        skipped entirely most launches. BLS also rate-limits keyless callers.
        """
        if not self.inflation_is_stale():
            return

        def worker() -> None:
            result = fetch_inflation()
            if result:
                # Drop it in a slot and let the tick pick it up. Tcl is not
                # thread-safe and even root.after() from another thread raises
                # "main thread is not in main loop" - no Tk call happens here.
                self._inflation_pending = result

        threading.Thread(target=worker, name="cpi-fetch", daemon=True).start()

    def apply_inflation(self, figures: dict) -> None:
        self.inflation = dict(FALLBACK_INFLATION)
        self.inflation.update(figures)
        self.data["inflation"] = dict(self.inflation)
        self.persist()

    # -- persistence ------------------------------------------------------

    def persist(self) -> None:
        save_data(self.data)

    def active_loans(self) -> list[dict]:
        return [l for l in self.data["loans"] if l.get("status") == "active"]

    def eliminated_loans(self) -> list[dict]:
        return [l for l in self.data["loans"] if l.get("status") == "eliminated"]

    def loan_by_id(self, loan_id: str) -> dict | None:
        for loan in self.data["loans"]:
            if loan.get("id") == loan_id:
                return loan
        return None

    # -- chrome -----------------------------------------------------------


    def button(self, parent: tk.Widget, text: str, command, accent: str = THEME_BORDER) -> tk.Label:
        """A Label pretending to be a button.

        Aqua's native tk.Button paints its own background and silently ignores
        `bg`, which renders every button as a white slab on this dark theme. A
        Label honors bg, so we bind the click ourselves.
        """
        hover = lerp_color(accent, "#ffffff", 0.18)
        btn = tk.Label(
            parent,
            text=text,
            font=FONT_LABEL,
            bg=accent,
            fg=THEME_TEXT,
            padx=14,
            pady=7,
            cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: command())
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=accent))
        return btn

    def build_ui(self) -> None:
        """One canvas, one paint pass. Everything below is drawn, not packed."""
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.deck = tk.Canvas(self.root, bg=THEME_BG, highlightthickness=0, bd=0)
        self.deck.grid(row=0, column=0, sticky="nsew")

        self.hotspots: list[dict] = []
        self.hover_key: str | None = None
        self.popover = Popover(self.root)

        self.deck.bind("<Configure>", lambda _e: self.refresh_display())
        self.deck.bind("<Motion>", self.on_hover)
        self.deck.bind("<Button-1>", self.on_click)
        self.deck.bind("<Leave>", self.on_leave)

    # -- hit testing ------------------------------------------------------

    def zone(self, x0: float, y0: float, x1: float, y1: float, key: str,
             title: str = "", lines: list | None = None, action=None,
             cursor: str = "") -> None:
        """Register a rectangle for hover detail and/or click."""
        self.hotspots.append({
            "box": (x0, y0, x1, y1), "key": key, "title": title,
            "lines": lines or [], "action": action, "cursor": cursor,
        })

    def zone_at(self, x: float, y: float) -> dict | None:
        for spot in reversed(self.hotspots):  # later zones sit on top
            x0, y0, x1, y1 = spot["box"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return spot
        return None

    def on_hover(self, event) -> None:
        spot = self.zone_at(event.x, event.y)
        key = spot["key"] if spot else None
        if key != self.hover_key:
            self.hover_key = key
            self.deck.configure(cursor=(spot or {}).get("cursor", ""))
            self.refresh_display()  # repaint so the hovered row lights up
        if spot and spot["lines"]:
            self.popover.show(key, spot["title"], spot["lines"],
                              event.x_root, event.y_root)
        else:
            self.popover.hide()

    def on_leave(self, _event=None) -> None:
        self.popover.hide()
        if self.hover_key is not None:
            self.hover_key = None
            self.deck.configure(cursor="")
            self.refresh_display()

    def on_click(self, event) -> None:
        spot = self.zone_at(event.x, event.y)
        if spot and spot["action"]:
            self.popover.hide()
            spot["action"]()

    # -- drawing primitives -----------------------------------------------

    def panel(self, x: float, y: float, w: float, h: float, title: str = "",
              note: str = "", note_color: str = THEME_DIM,
              accent: str | None = None) -> tuple[float, float, float]:
        """Draw a panel, return the content origin (x, y) and usable width."""
        c = self.deck
        rounded(c, x, y, x + w, y + h, 13, THEME_PANEL, outline=THEME_BORDER, width=1)
        if not title:
            return x + PANEL_PAD, y + PANEL_PAD, w - 2 * PANEL_PAD
        c.create_text(x + PANEL_PAD, y + 15, text=title, font=FONT_CARD,
                      fill=THEME_MUTED, anchor="w")
        if accent:
            # Short underline in the live accent color - the only chrome that
            # changes as the debt dies.
            rounded(c, x + PANEL_PAD, y + 25, x + PANEL_PAD + 34, y + 27, 1, accent, outline="")
        if note:
            c.create_text(x + w - PANEL_PAD, y + 15, text=note, font=FONT_SMALL,
                          fill=note_color, anchor="e")
        return x + PANEL_PAD, y + 34, w - 2 * PANEL_PAD

    def deck_button(self, x: float, y: float, text: str, accent: str, action) -> float:
        """Rounded canvas button. Returns its left edge so callers can stack right to left."""
        c = self.deck
        width = 15 + 7.4 * len(text)
        hovered = self.hover_key == f"btn:{text}"
        fill = lerp_color(accent, "#ffffff", 0.22) if hovered else accent
        rounded(c, x - width, y, x, y + 27, 7, fill, outline="")
        c.create_text(x - width / 2, y + 14, text=text, font=FONT_LABEL, fill=THEME_TEXT)
        self.zone(x - width, y, x, y + 27, f"btn:{text}", action=action, cursor="pointinghand")
        return x - width


    def toggle_fullscreen(self, _event=None) -> None:
        self.root.attributes("-fullscreen", not bool(self.root.attributes("-fullscreen")))

    def exit_fullscreen(self, _event=None) -> None:
        self.root.attributes("-fullscreen", False)

    def expanded_payments(self) -> list[dict]:
        """Payments with account-level ones split across active loans."""
        return expand_payments(self.data["payments"], self.data["loans"])

    # -- modals -----------------------------------------------------------

    def modal(self, title: str, width: int = 560, height: int = 460) -> tuple[tk.Toplevel, tk.Frame]:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=THEME_BG)
        win.transient(self.root)
        win.geometry(f"{width}x{height}")
        win.grab_set()
        frame = tk.Frame(win, bg=THEME_BG)
        frame.pack(fill="both", expand=True, padx=14, pady=14)
        return win, frame

    def labeled_entry(self, parent: tk.Widget, label: str, row: int, initial: str = "", hint: str = "") -> tk.Entry:
        tk.Label(parent, text=label, font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(
            row=row, column=0, sticky="w", pady=(6, 0)
        )
        entry = tk.Entry(
            parent, font=FONT_BODY, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat",
            insertbackground=ENTRY_FG, highlightthickness=1, highlightbackground=THEME_BORDER,
        )
        entry.insert(0, initial)
        entry.grid(row=row, column=1, sticky="ew", pady=(6, 0), padx=(10, 0))
        if hint:
            tk.Label(parent, text=hint, font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM, anchor="w").grid(
                row=row + 1, column=1, sticky="w", padx=(10, 0)
            )
        return entry

    def open_loans_modal(self) -> None:
        win, frame = self.modal("Manage Loans", 720, 560)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        top = tk.Frame(frame, bg=THEME_BG)
        top.grid(row=0, column=0, sticky="ew")
        tk.Label(top, text="Loans", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT).pack(side="left")
        self.button(top, "+ Add Loan", lambda: (win.destroy(), self.open_loan_editor(None)), "#166534").pack(side="right")

        canvas = tk.Canvas(frame, bg=THEME_BG, highlightthickness=0)
        canvas.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scroll = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        canvas.configure(yscrollcommand=scroll.set)
        inner = tk.Frame(canvas, bg=THEME_BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        if not self.data["loans"]:
            tk.Label(inner, text="No loans yet.", font=FONT_BODY, bg=THEME_BG, fg=THEME_DIM).pack(anchor="w", pady=8)

        now = datetime.now()
        for loan in self.data["loans"]:
            snap = project_loan(loan, self.expanded_payments(), now)
            row = tk.Frame(inner, bg=THEME_PANEL)
            row.pack(fill="x", pady=3)
            row.grid_columnconfigure(0, weight=1)

            eliminated = loan.get("status") == "eliminated"
            name_text = loan["name"] + ("  [ELIMINATED]" if eliminated else "")
            tk.Label(
                row, text=name_text, font=FONT_CARD, bg=THEME_PANEL,
                fg=THEME_DIM if eliminated else THEME_TEXT, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))
            detail = f"{loan['rate']:.2f}%   balance {money(snap['total'])}   snapshot {parse_dt_str(loan['snapshot_at']).strftime('%Y-%m-%d')}"
            tk.Label(row, text=detail, font=FONT_MONO, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w").grid(
                row=1, column=0, sticky="w", padx=10, pady=(0, 8)
            )

            actions = tk.Frame(row, bg=THEME_PANEL)
            actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=8)
            self.button(actions, "Edit", lambda l=loan: (win.destroy(), self.open_loan_editor(l))).pack(side="left", padx=2)
            if eliminated:
                self.button(actions, "Revive", lambda l=loan: (self.revive_loan(l), win.destroy(), self.open_loans_modal()), "#3f3f46").pack(side="left", padx=2)
            else:
                self.button(actions, "Eliminate", lambda l=loan: (self.eliminate_loan(l), win.destroy(), self.open_loans_modal()), "#166534").pack(side="left", padx=2)
            self.button(actions, "Delete", lambda l=loan: (self.delete_loan(l, win)), "#7f1d1d").pack(side="left", padx=2)

        bottom = tk.Frame(frame, bg=THEME_BG)
        bottom.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tk.Label(bottom, text="Monthly minimum payment:", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED).pack(side="left")
        min_entry = tk.Entry(bottom, font=FONT_BODY, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat", width=12)
        min_entry.insert(0, f"{self.data.get('monthly_minimum', 0.0):.2f}")
        min_entry.pack(side="left", padx=8)

        def save_min() -> None:
            try:
                self.data["monthly_minimum"] = max(0.0, float(min_entry.get().strip() or 0))
            except ValueError:
                messagebox.showerror("Invalid", "Monthly minimum must be a number.", parent=win)
                return
            self.persist()
            win.destroy()

        self.button(bottom, "Save & Close", save_min, "#166534").pack(side="right")

    def open_loan_editor(self, loan: dict | None) -> None:
        creating = loan is None
        win, frame = self.modal("Add Loan" if creating else f"Edit {loan['name']}", 560, 440)
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="Add Loan" if creating else "Edit Loan", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        name_e = self.labeled_entry(frame, "Nickname", 1, "" if creating else loan["name"])
        rate_e = self.labeled_entry(frame, "Interest rate %", 2, "" if creating else f"{loan['rate']:.2f}", "Fixed federal rate, e.g. 6.53")
        orig_e = self.labeled_entry(frame, "Original amount", 4, "" if creating else f"{loan['original_amount']:.2f}", "Optional - only used for the % paid off stat")
        prin_e = self.labeled_entry(
            frame, "Current principal", 6,
            "" if creating else f"{loan['snapshot_principal']:.2f}",
            "The real number from your servicer right now",
        )
        accr_e = self.labeled_entry(
            frame, "Unpaid interest", 8,
            "0.00" if creating else f"{loan['snapshot_accrued']:.2f}",
            "Optional - leave 0 if your servicer shows none",
        )
        date_e = self.labeled_entry(
            frame, "Snapshot date", 10,
            date.today().isoformat() if creating else parse_dt_str(loan["snapshot_at"]).date().isoformat(),
            "YYYY-MM-DD - when that balance was true",
        )

        def save() -> None:
            name = name_e.get().strip()
            if not name:
                messagebox.showerror("Invalid", "Give the loan a nickname.", parent=win)
                return
            try:
                rate = float(rate_e.get().strip() or 0)
                original = float(orig_e.get().strip() or 0)
                principal = float(prin_e.get().strip() or 0)
                accrued = float(accr_e.get().strip() or 0)
            except ValueError:
                messagebox.showerror("Invalid", "Amounts and rate must be numbers.", parent=win)
                return
            if rate < 0 or principal < 0 or accrued < 0:
                messagebox.showerror("Invalid", "Values can't be negative.", parent=win)
                return
            try:
                snap_day = parse_date_str(date_e.get())
            except ValueError:
                messagebox.showerror("Invalid", "Snapshot date must be YYYY-MM-DD.", parent=win)
                return

            payload = {
                "id": loan["id"] if loan else str(uuid.uuid4()),
                "name": name,
                "rate": rate,
                "original_amount": original,
                "snapshot_principal": principal,
                "snapshot_accrued": accrued,
                "snapshot_at": datetime.combine(snap_day, time.min).isoformat(timespec="seconds"),
                "status": loan.get("status", "active") if loan else "active",
                "eliminated_on": loan.get("eliminated_on", "") if loan else "",
                "note": loan.get("note", "") if loan else "",
            }
            if loan:
                index = self.data["loans"].index(loan)
                self.data["loans"][index] = normalize_loan(payload, index)
            else:
                self.data["loans"].append(normalize_loan(payload, len(self.data["loans"])))
            self.record_snapshot(payload["id"], principal, accrued, snap_day)
            self.persist()
            win.destroy()
            self.refresh_display()

        buttons = tk.Frame(frame, bg=THEME_BG)
        buttons.grid(row=12, column=0, columnspan=2, sticky="e", pady=(16, 0))
        self.button(buttons, "Cancel", win.destroy, "#3f3f46").pack(side="left", padx=4)
        self.button(buttons, "Save", save, "#166534").pack(side="left", padx=4)

    def open_payment_modal(self) -> None:
        actives = self.active_loans()
        if not actives:
            messagebox.showinfo("No loans", "Add a loan first.", parent=self.root)
            return

        win, frame = self.modal("Log Payment", 620, 560)
        frame.grid_columnconfigure(0, weight=1)

        tk.Label(frame, text="Log Payment", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT).grid(
            row=0, column=0, sticky="w"
        )
        tk.Label(
            frame, text="Send it at the servicer first, then record it here.",
            font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM, anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 16))

        # ---- which loan ----
        ordered = kill_order([project_loan(l, self.expanded_payments(), datetime.now()) for l in actives])
        account_option = "Whole account (servicer spreads it)"
        options = [f"{s['name']}  -  {s['rate']:.2f}%" for s in ordered] + [account_option]
        selected = tk.StringVar(value=options[0])

        tk.Label(frame, text="LOAN", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(
            row=2, column=0, sticky="w"
        )
        menu = tk.OptionMenu(frame, selected, *options)
        menu.configure(bg=ENTRY_BG, fg=ENTRY_FG, font=FONT_BODY, relief="flat",
                       highlightthickness=0, anchor="w")
        menu["menu"].configure(bg=ENTRY_BG, fg=ENTRY_FG, font=FONT_BODY)
        menu.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        tk.Label(
            frame, text=f"TARGET  {ordered[0]['name']} - highest rate, {money(ordered[0]['daily'])}/day",
            font=FONT_SMALL, bg=THEME_BG, fg=THEME_WARN, anchor="w",
        ).grid(row=4, column=0, sticky="w", pady=(4, 16))

        # ---- amount, with steppers ----
        tk.Label(frame, text="AMOUNT", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(
            row=5, column=0, sticky="w"
        )
        default_amount = float(self.data.get("default_payment", 375.0) or 375.0)
        amount_var = tk.StringVar(value=f"{default_amount:.2f}")

        def bump(delta: float) -> None:
            try:
                current = float(amount_var.get().strip() or 0)
            except ValueError:
                current = 0.0
            amount_var.set(f"{max(0.0, current + delta):.2f}")

        stepper = tk.Frame(frame, bg=THEME_BG)
        stepper.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        stepper.grid_columnconfigure(1, weight=1)

        minus = tk.Label(stepper, text="\u2212", font=("Menlo", 22, "bold"), bg=THEME_PANEL_ALT,
                         fg=THEME_TEXT, width=3, cursor="hand2")
        minus.grid(row=0, column=0, sticky="ns")
        minus.bind("<Button-1>", lambda _e: bump(-25))

        entry = tk.Entry(stepper, textvariable=amount_var, font=FONT_HERO, bg=ENTRY_BG, fg=ENTRY_FG,
                         relief="flat", justify="center", highlightthickness=1,
                         highlightbackground=THEME_BORDER)
        entry.grid(row=0, column=1, sticky="ew", padx=8, ipady=4)

        plus = tk.Label(stepper, text="+", font=("Menlo", 22, "bold"), bg=THEME_PANEL_ALT,
                        fg=THEME_TEXT, width=3, cursor="hand2")
        plus.grid(row=0, column=2, sticky="ns")
        plus.bind("<Button-1>", lambda _e: bump(25))

        for widget in (minus, plus):
            widget.bind("<Enter>", lambda e: e.widget.configure(bg=THEME_BORDER))
            widget.bind("<Leave>", lambda e: e.widget.configure(bg=THEME_PANEL_ALT))

        picks = tk.Frame(frame, bg=THEME_BG)
        picks.grid(row=7, column=0, sticky="w", pady=(8, 16))
        tk.Label(picks, text="quick", font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM).pack(side="left", padx=(0, 8))
        for amount in (250, 375, 500, 750, 1000):
            self.button(
                picks, f"${amount:,}", lambda a=amount: amount_var.set(f"{a:.2f}"), THEME_PANEL_ALT
            ).pack(side="left", padx=(0, 5))

        # ---- date ----
        tk.Label(frame, text="DATE", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(
            row=8, column=0, sticky="w"
        )
        date_row = tk.Frame(frame, bg=THEME_BG)
        date_row.grid(row=9, column=0, sticky="ew", pady=(4, 16))
        date_row.grid_columnconfigure(0, weight=1)
        date_var = tk.StringVar(value=date.today().isoformat())
        tk.Entry(date_row, textvariable=date_var, font=FONT_BODY, bg=ENTRY_BG, fg=ENTRY_FG,
                 relief="flat", highlightthickness=1, highlightbackground=THEME_BORDER).grid(
            row=0, column=0, sticky="ew", ipady=4
        )
        self.button(date_row, "Today", lambda: date_var.set(date.today().isoformat()),
                    THEME_PANEL_ALT).grid(row=0, column=1, padx=(8, 0))

        # ---- note ----
        tk.Label(frame, text="NOTE", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(
            row=10, column=0, sticky="w"
        )
        note_e = tk.Entry(frame, font=FONT_BODY, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat",
                          highlightthickness=1, highlightbackground=THEME_BORDER)
        note_e.grid(row=11, column=0, sticky="ew", pady=(4, 0), ipady=4)

        def save() -> None:
            try:
                amount = float(amount_var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid", "Amount must be a number.", parent=win)
                return
            if amount <= 0:
                messagebox.showerror("Invalid", "Amount must be greater than zero.", parent=win)
                return
            try:
                pay_day = parse_date_str(date_var.get())
            except ValueError:
                messagebox.showerror("Invalid", "Date must be YYYY-MM-DD.", parent=win)
                return

            choice = selected.get()
            if choice == account_option:
                loan = None
                loan_id = ACCOUNT
            else:
                loan = ordered[options.index(choice)]["loan"]
                loan_id = loan["id"]

            payment = {
                "id": str(uuid.uuid4()),
                "loan_id": loan_id,
                "amount": amount,
                "date": pay_day.isoformat(),
                "note": note_e.get().strip(),
            }
            self.data["payments"].append(payment)
            self.data["default_payment"] = amount  # next time, start where you left off
            self.persist()
            win.destroy()
            self.refresh_display()
            self.show_autopsy(loan, payment)

        buttons = tk.Frame(frame, bg=THEME_BG)
        buttons.grid(row=12, column=0, sticky="e", pady=(20, 0))
        self.button(buttons, "Cancel", win.destroy, "#3f3f46").pack(side="left", padx=4)
        self.button(buttons, "Log It", save, "#166534").pack(side="left", padx=4)
        entry.focus_set()
        entry.select_range(0, "end")
        win.bind("<Return>", lambda _e: save())

    def show_autopsy(self, loan: dict | None, payment: dict) -> None:
        """What that payment actually did. This is the dopamine hit.

        `loan` is None for an account-level payment, in which case the split is
        summed over every loan the servicer would have spread it across.
        """
        now = datetime.now()
        others = expand_payments(
            [p for p in self.data["payments"] if p["id"] != payment["id"]], self.data["loans"]
        )
        expanded = self.expanded_payments()
        touched = [loan] if loan else self.active_loans()

        befores = [project_loan(l, others, now) for l in touched]
        afters = [project_loan(l, expanded, now) for l in touched]

        before = {"total": sum(b["total"] for b in befores), "rate": blended_rate(befores)}
        after = {"total": sum(a["total"] for a in afters), "rate": blended_rate(afters)}

        to_interest = 0.0
        to_principal = 0.0
        for snap in afters:
            for applied in snap["payments_applied"]:
                origin = applied["payment"].get("parent_id") or applied["payment"]["id"]
                if origin == payment["id"]:
                    to_interest += applied["to_interest"]
                    to_principal += applied["to_principal"]

        # Lifetime interest saved: killing principal stops it charging rent for
        # the remaining life of the loan. Approximated against the loan's own
        # projected payoff horizon at the current payment pace.
        pace, _ = self.planned_monthly()
        saved = 0.0
        moved_days = 0
        if pace > 0:
            _, months_before = freedom_date(before["total"], before["rate"], pace, now.date())
            _, months_after = freedom_date(after["total"], after["rate"], pace, now.date())
            if months_before and months_after:
                moved_days = max(0, round((months_before - months_after) * 30.44))
                saved = max(0.0, (pace * months_before - before["total"]) - (pace * months_after - after["total"]))

        win, frame = self.modal("Payment Logged", 460, 330)
        tk.Label(frame, text="PAYMENT AUTOPSY", font=FONT_CARD, bg=THEME_BG, fg=THEME_MUTED).pack(anchor="w")
        tk.Label(frame, text=money(payment["amount"]), font=FONT_HERO, bg=THEME_BG, fg=THEME_GOOD).pack(anchor="w")
        target = loan["name"] if loan else f"across {len(touched)} loans"
        tk.Label(frame, text=f"to {target}", font=FONT_BODY, bg=THEME_BG, fg=THEME_MUTED).pack(anchor="w")

        breakdown = tk.Frame(frame, bg=THEME_BG)
        breakdown.pack(fill="x", pady=(14, 0))
        lines = [
            ("Killed interest", money(to_interest), THEME_DANGER),
            ("Killed principal", money(to_principal), THEME_GOOD),
            ("New balance", money(after["total"]), THEME_TEXT),
        ]
        if moved_days > 0:
            lines.append(("Freedom date moved", f"{moved_days} days closer", THEME_WARN))
        if saved > 0.01:
            lines.append(("Lifetime interest saved", money(saved), THEME_WARN))

        for i, (label, value, color) in enumerate(lines):
            tk.Label(breakdown, text=label, font=FONT_BODY, bg=THEME_BG, fg=THEME_MUTED, anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            tk.Label(breakdown, text=value, font=FONT_VALUE_M, bg=THEME_BG, fg=color, anchor="e").grid(row=i, column=1, sticky="e", padx=(24, 0), pady=2)
        breakdown.grid_columnconfigure(1, weight=1)

        if to_principal <= 0 and to_interest > 0:
            tk.Label(
                frame, text="All interest, no principal. Anything above the interest\nis what actually shrinks the debt.",
                font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM, justify="left",
            ).pack(anchor="w", pady=(10, 0))

        self.button(frame, "Nice", win.destroy, "#166534").pack(anchor="e", pady=(16, 0))

    def open_snapshot_modal(self) -> None:
        actives = self.active_loans()
        if not actives:
            messagebox.showinfo("No loans", "Add a loan first.", parent=self.root)
            return

        win, frame = self.modal("Monthly Snapshot", 640, 520)
        frame.grid_rowconfigure(2, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        tk.Label(frame, text="Monthly Snapshot", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT).grid(row=0, column=0, sticky="w")
        tk.Label(
            frame,
            text="Pull up your servicer and type in the real numbers. This resets the\nclock so the live counter tracks reality instead of drifting.",
            font=FONT_SMALL, bg=THEME_BG, fg=THEME_MUTED, justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        holder = tk.Frame(frame, bg=THEME_BG)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.grid_columnconfigure(1, weight=1)
        holder.grid_columnconfigure(2, weight=1)

        tk.Label(holder, text="Loan", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED).grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="Principal", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED).grid(row=0, column=1, sticky="w", padx=8)
        tk.Label(holder, text="Unpaid interest", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED).grid(row=0, column=2, sticky="w", padx=8)

        now = datetime.now()
        entries = []
        for i, loan in enumerate(actives):
            snap = project_loan(loan, self.expanded_payments(), now)
            tk.Label(holder, text=loan["name"], font=FONT_BODY, bg=THEME_BG, fg=THEME_TEXT, anchor="w").grid(
                row=i + 1, column=0, sticky="w", pady=3
            )
            p_entry = tk.Entry(holder, font=FONT_MONO, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat", width=14)
            p_entry.insert(0, f"{snap['principal']:.2f}")
            p_entry.grid(row=i + 1, column=1, sticky="ew", padx=8, pady=3)
            a_entry = tk.Entry(holder, font=FONT_MONO, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat", width=14)
            a_entry.insert(0, f"{snap['accrued']:.2f}")
            a_entry.grid(row=i + 1, column=2, sticky="ew", padx=8, pady=3)
            entries.append((loan, p_entry, a_entry))

        date_row = tk.Frame(frame, bg=THEME_BG)
        date_row.grid(row=3, column=0, sticky="w", pady=(12, 0))
        tk.Label(date_row, text="As of", font=FONT_LABEL, bg=THEME_BG, fg=THEME_MUTED).pack(side="left")
        date_entry = tk.Entry(date_row, font=FONT_BODY, bg=ENTRY_BG, fg=ENTRY_FG, relief="flat", width=14)
        date_entry.insert(0, date.today().isoformat())
        date_entry.pack(side="left", padx=8)

        def save() -> None:
            try:
                as_of = parse_date_str(date_entry.get())
            except ValueError:
                messagebox.showerror("Invalid", "Date must be YYYY-MM-DD.", parent=win)
                return
            parsed = []
            for loan, p_entry, a_entry in entries:
                try:
                    principal = float(p_entry.get().strip() or 0)
                    accrued = float(a_entry.get().strip() or 0)
                except ValueError:
                    messagebox.showerror("Invalid", f"{loan['name']}: amounts must be numbers.", parent=win)
                    return
                if principal < 0 or accrued < 0:
                    messagebox.showerror("Invalid", f"{loan['name']}: amounts can't be negative.", parent=win)
                    return
                parsed.append((loan, principal, accrued))

            stamp = datetime.combine(as_of, time.min).isoformat(timespec="seconds")
            for loan, principal, accrued in parsed:
                loan["snapshot_principal"] = principal
                loan["snapshot_accrued"] = accrued
                loan["snapshot_at"] = stamp
                self.record_snapshot(loan["id"], principal, accrued, as_of)
                if principal <= 0 and accrued <= 0:
                    loan["status"] = "eliminated"
                    loan["eliminated_on"] = as_of.isoformat()
            self.persist()
            win.destroy()
            self.refresh_display()

        buttons = tk.Frame(frame, bg=THEME_BG)
        buttons.grid(row=4, column=0, sticky="e", pady=(14, 0))
        self.button(buttons, "Cancel", win.destroy, "#3f3f46").pack(side="left", padx=4)
        self.button(buttons, "Save Snapshot", save, "#92400e").pack(side="left", padx=4)

    # -- loan actions -----------------------------------------------------

    def record_snapshot(self, loan_id: str, principal: float, accrued: float, as_of: date) -> None:
        self.data["snapshot_history"].append(
            {
                "at": datetime.combine(as_of, time.min).isoformat(timespec="seconds"),
                "loan_id": loan_id,
                "principal": principal,
                "accrued": accrued,
            }
        )

    def eliminate_loan(self, loan: dict) -> None:
        loan["status"] = "eliminated"
        loan["eliminated_on"] = date.today().isoformat()
        loan["snapshot_principal"] = 0.0
        loan["snapshot_accrued"] = 0.0
        loan["snapshot_at"] = datetime.now().isoformat(timespec="seconds")
        self.persist()
        self.refresh_display()

    def revive_loan(self, loan: dict) -> None:
        loan["status"] = "active"
        loan["eliminated_on"] = ""
        self.persist()
        self.refresh_display()

    def delete_loan(self, loan: dict, parent: tk.Toplevel) -> None:
        if not messagebox.askyesno("Delete loan", f"Delete {loan['name']} and its payments?", parent=parent):
            return
        self.data["loans"] = [l for l in self.data["loans"] if l["id"] != loan["id"]]
        self.data["payments"] = [p for p in self.data["payments"] if p["loan_id"] != loan["id"]]
        self.data["snapshot_history"] = [s for s in self.data["snapshot_history"] if s.get("loan_id") != loan["id"]]
        self.persist()
        parent.destroy()
        self.refresh_display()
        self.open_loans_modal()

    # -- derived ----------------------------------------------------------

    def payments_in_month(self, day: date) -> list[dict]:
        start, end = month_start(day), month_end(day)
        result = []
        for payment in self.data["payments"]:
            try:
                pay_day = parse_date_str(payment["date"])
            except (ValueError, TypeError):
                continue
            if start <= pay_day <= end:
                result.append(payment)
        return result

    def recent_monthly_pace(self) -> float:
        """Average monthly payment over the last 3 complete months, else the minimum."""
        today = date.today()
        totals = []
        for back in range(1, 4):
            anchor = add_months(month_start(today), -back)
            month_total = sum(p["amount"] for p in self.payments_in_month(anchor))
            totals.append(month_total)
        real = [t for t in totals if t > 0]
        pace = sum(real) / len(real) if real else 0.0
        return max(pace, float(self.data.get("monthly_minimum", 0.0)))

    def planned_monthly(self) -> tuple[float, str]:
        """What to assume gets paid monthly, and where that figure came from.

        An explicit target wins: it's a commitment, not a measurement. Without
        one we fall back to the trailing three-month average.
        """
        target = float(self.data.get("monthly_target", 0.0) or 0.0)
        if target > 0:
            return target, "target"
        return self.recent_monthly_pace(), "recent pace"

    def open_target_modal(self, _event=None) -> None:
        win, frame = self.modal("Monthly Target", 430, 300)
        frame.grid_columnconfigure(0, weight=1)

        tk.Label(frame, text="Monthly Target", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT).grid(
            row=0, column=0, sticky="w"
        )
        tk.Label(
            frame,
            text="What you intend to pay each month. Drives the freedom date.\nLeave at 0 to use your recent 3-month average instead.",
            font=FONT_SMALL, bg=THEME_BG, fg=THEME_MUTED, justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        var = tk.StringVar(value=f"{self.data.get('monthly_target', 0.0):.2f}")
        entry = tk.Entry(
            frame, textvariable=var, font=FONT_VALUE_L, bg=ENTRY_BG, fg=ENTRY_FG,
            relief="flat", justify="center", highlightthickness=1, highlightbackground=THEME_BORDER,
        )
        entry.grid(row=2, column=0, sticky="ew", ipady=6)

        picks = tk.Frame(frame, bg=THEME_BG)
        picks.grid(row=3, column=0, sticky="w", pady=(10, 0))
        for amount in (500, 750, 1000, 1500):
            self.button(
                picks, f"${amount:,}", lambda a=amount: var.set(f"{a:.2f}"), THEME_PANEL_ALT
            ).pack(side="left", padx=(0, 6))

        def save() -> None:
            try:
                value = max(0.0, float(var.get().strip() or 0))
            except ValueError:
                messagebox.showerror("Invalid", "Target must be a number.", parent=win)
                return
            self.data["monthly_target"] = value
            self.persist()
            win.destroy()
            self.refresh_display()

        buttons = tk.Frame(frame, bg=THEME_BG)
        buttons.grid(row=4, column=0, sticky="e", pady=(18, 0))
        self.button(buttons, "Cancel", win.destroy, "#3f3f46").pack(side="left", padx=4)
        self.button(buttons, "Save", save, "#166534").pack(side="left", padx=4)

    # -- render -----------------------------------------------------------
    #
    # One pass, top to bottom: gather every number into a model, then draw it.
    # Nothing is cached between frames - a canvas repaint is cheap and it means
    # there is exactly one code path, whether the trigger was the clock, a
    # resize, a hover, or a payment landing.

    def refresh_display(self) -> None:
        if not hasattr(self, "deck"):
            return
        self.paint(self.build_model())

    def build_model(self) -> dict:
        now = datetime.now()
        today = now.date()
        payments = self.expanded_payments()

        actives = self.active_loans()
        snapshots = [project_loan(l, payments, now) for l in actives]
        ordered = kill_order(snapshots)

        total_owed = sum(s["total"] for s in snapshots)
        total_principal = sum(s["principal"] for s in snapshots)
        total_daily = sum(s["daily"] for s in snapshots)

        original_all = sum(max(0.0, float(l.get("original_amount", 0.0))) for l in self.data["loans"])
        paid_fraction = 0.0
        if original_all > 0:
            paid_fraction = max(0.0, min(1.0, 1.0 - (total_principal / original_all)))

        month_payments = self.payments_in_month(today)
        pace, pace_source = self.planned_monthly()
        free_date, months = freedom_date(total_owed, blended_rate(snapshots), pace, today)

        cpi = self.inflation.get("headline")
        return {
            "now": now, "today": today,
            "snapshots": snapshots, "ordered": ordered,
            "total_owed": total_owed,
            "total_principal": total_principal,
            "total_accrued": sum(s["accrued"] for s in snapshots),
            "total_daily": total_daily,
            "original_all": original_all,
            "paid_fraction": paid_fraction,
            "accent": progress_accent(paid_fraction),
            "month_interest": sum(month_interest_for(l, payments, now) for l in actives),
            "month_payments": month_payments,
            "month_paid": sum(p["amount"] for p in month_payments),
            "break_even": total_daily * days_in_month(today),
            "pace": pace, "pace_source": pace_source,
            "free_date": free_date, "months": months,
            "blended": blended_rate(snapshots),
            "cpi": cpi,
            "floor": above_floor_split(snapshots),
            "vs_cpi": inflation_verdict(snapshots, cpi) if cpi is not None else None,
            "dead": self.eliminated_loans(),
        }

    def paint(self, m: dict) -> None:
        c = self.deck
        c.delete("all")
        self.hotspots = []

        width = c.winfo_width()
        height = c.winfo_height()
        if width <= 1 or height <= 1:
            return

        pad, gut = 15, 11
        y = self.paint_header(m, pad, width - 2 * pad)
        y = self.paint_hero(m, pad, y + gut, width - 2 * pad) + gut

        status_h = 27
        left_w = int((width - 2 * pad - gut) * 0.58)
        right_x = pad + left_w + gut
        right_w = width - pad - right_x
        bottom = height - pad - status_h

        race_h = 122
        self.paint_race(m, pad, y, left_w, race_h)
        self.paint_loans(m, pad, y + race_h + gut, left_w, bottom - y - race_h - gut)

        infl_h = 196
        self.paint_inflation(m, right_x, y, right_w, infl_h)
        month_h = 104
        self.paint_month(m, right_x, y + infl_h + gut, right_w, month_h)
        self.paint_trophies(m, right_x, y + infl_h + month_h + 2 * gut, right_w,
                            bottom - y - infl_h - month_h - 2 * gut)

        self.paint_status(m, pad, height - pad - status_h + 4, width - 2 * pad)

    # -- bands ------------------------------------------------------------

    def paint_header(self, m: dict, x: float, w: float) -> float:
        c = self.deck
        c.create_text(x, 20, text="STUDENT LOAN MOTIVATOR", font=FONT_TITLE,
                      fill=THEME_TEXT, anchor="w")
        c.create_text(x, 39, text=self.current_quote, font=FONT_SMALL,
                      fill=THEME_DIM, anchor="w")

        right = x + w
        right = self.deck_button(right, 13, "Manage Loans", "#5b2531", self.open_loans_modal) - 7
        right = self.deck_button(right, 13, "Monthly Snapshot", "#92400e", self.open_snapshot_modal) - 7
        self.deck_button(right, 13, "Log Payment", "#166534", self.open_payment_modal)
        return 52

    def paint_hero(self, m: dict, x: float, y: float, w: float) -> float:
        c = self.deck
        h = 128
        snapshot_note = ""
        if m["snapshots"]:
            oldest = min(s["snapshot_at"] for s in m["snapshots"])
            age = (m["now"] - oldest).days
            snapshot_note = f"snapshot {age} day{'s' if age != 1 else ''} old"
            if age >= 35:
                snapshot_note += "  -  time to re-snapshot"
        cx, cy, cw = self.panel(x, y, w, h, "TOTAL OWED RIGHT NOW", snapshot_note,
                                THEME_WARN if "re-snapshot" in snapshot_note else THEME_FAINT,
                                accent=m["accent"])

        # Left: the number that ticks.
        c.create_text(cx, cy + 22, text=money(m["total_owed"]), font=FONT_HERO,
                      fill=m["accent"] if m["total_owed"] > 0 else THEME_GOOD, anchor="w")
        if m["snapshots"]:
            detail = (f"{money(m['total_principal'])} principal  +  {money(m['total_accrued'])} interest"
                      f"     across {len(m['snapshots'])} loans")
        else:
            detail = "Add your loans to begin."
        c.create_text(cx, cy + 48, text=detail, font=FONT_BODY, fill=THEME_MUTED, anchor="w")

        self.zone(cx, cy, cx + 300, cy + 58, "hero", "Where the balance sits", [
            ("Principal", money(m["total_principal"]), THEME_TEXT),
            ("Unpaid interest", money(m["total_accrued"]), THEME_WARN),
            ("", "", ""),
            ("Interest per day", money(m["total_daily"]), THEME_DANGER),
            ("Interest per hour", money(m["total_daily"] / 24.0), THEME_DANGER),
            ("Blended rate", f"{m['blended']:.2f}%", THEME_TEXT),
            ("", "", ""),
            ("Borrowed all-time", money(m["original_all"]), THEME_DIM),
            ("Killed so far", money(m["original_all"] - m["total_principal"]), THEME_GOOD),
        ])

        # Middle: payoff ring.
        ring_x = cx + cw * 0.56
        ring_y = cy + 30
        donut(c, ring_x, ring_y, 30, 7, m["paid_fraction"], m["accent"])
        c.create_text(ring_x, ring_y - 3, text=f"{m['paid_fraction'] * 100:.0f}%",
                      font=FONT_VALUE_M, fill=THEME_TEXT)
        c.create_text(ring_x, ring_y + 12, text="paid off", font=FONT_TINY, fill=THEME_DIM)
        self.zone(ring_x - 34, ring_y - 34, ring_x + 34, ring_y + 34, "ring",
                  "Lifetime progress", [
                      ("Borrowed", money(m["original_all"]), THEME_DIM),
                      ("Principal left", money(m["total_principal"]), THEME_TEXT),
                      ("Killed", money(m["original_all"] - m["total_principal"]), THEME_GOOD),
                      ("", "", ""),
                      ("Loans eliminated", str(len(m["dead"])), THEME_GOOD),
                      ("Still active", str(len(m["snapshots"])), THEME_TEXT),
                  ])

        # Right: freedom date.
        rx = cx + cw
        c.create_text(rx, cy + 4, text="FREEDOM DATE", font=FONT_LABEL, fill=THEME_MUTED, anchor="e")
        if m["total_owed"] <= 0 and self.data["loans"]:
            c.create_text(rx, cy + 26, text="DEBT FREE", font=FONT_VALUE_L, fill=THEME_GOOD, anchor="e")
            sub = ""
        elif m["free_date"] and m["months"]:
            c.create_text(rx, cy + 26, text=m["free_date"].strftime("%b %Y"),
                          font=FONT_VALUE_L, fill=THEME_WARN, anchor="e")
            years, rem = divmod(m["months"], 12)
            span = f"{years}y {rem}m" if years else f"{rem} months"
            sub = f"{span} at {money(m['pace'])}/mo {m['pace_source']}"
        else:
            c.create_text(rx, cy + 26, text="--", font=FONT_VALUE_L, fill=THEME_DIM, anchor="e")
            sub = "click to set a monthly target"
        c.create_text(rx, cy + 44, text=sub, font=FONT_SMALL, fill=THEME_DIM, anchor="e")
        self.zone(rx - 210, cy, rx, cy + 52, "freedom", "Payoff projection", [
            ("Monthly pace", money(m["pace"]), THEME_TEXT),
            ("Source", m["pace_source"].strip("()") or "measured", THEME_DIM),
            ("Blended rate", f"{m['blended']:.2f}%", THEME_TEXT),
            ("Balance today", money(m["total_owed"]), THEME_TEXT),
            ("", "", ""),
            ("Click", "to change the target", THEME_WARN),
        ], action=self.open_target_modal, cursor="pointinghand")

        # Bottom: lifetime bar plus the above-the-floor readout.
        bar_y = cy + 68
        track_bar(c, cx, bar_y, cw, 9, m["paid_fraction"], m["accent"])
        c.create_text(cx, bar_y + 22,
                      text=f"{m['paid_fraction'] * 100:.1f}% paid off   -   "
                           f"{money(m['original_all'] - m['total_principal'])} killed of "
                           f"{money(m['original_all'])} borrowed",
                      font=FONT_SMALL, fill=THEME_MUTED, anchor="w")

        floor = m["floor"]
        if floor["count"] and floor["above"] > 0:
            text = (f"{money(floor['above'])} above {floor['floor']:.2f}%"
                    f"   -   {floor['fraction'] * 100:.1f}% of the balance")
            color = THEME_WARN
            lines = [
                ("Above the floor", money(floor["above"]), THEME_WARN),
                ("At the floor", money(floor["at_floor"]), THEME_GOOD),
                ("", "", ""),
                ("Loans above", f"{floor['count']} of {len(m['snapshots'])}", THEME_TEXT),
                ("Cheapest rate", f"{floor['floor']:.2f}%", THEME_GOOD),
            ]
        elif m["snapshots"]:
            text = f"everything left is at {floor['floor']:.2f}%"
            color = THEME_GOOD
            lines = [("Cheapest rate", f"{floor['floor']:.2f}%", THEME_GOOD)]
        else:
            text, color, lines = "", THEME_DIM, []
        if text:
            c.create_text(cx + cw, bar_y + 22, text=text, font=FONT_SMALL, fill=color, anchor="e")
            self.zone(cx + cw - 260, bar_y + 13, cx + cw, bar_y + 31, "floor",
                      "Debt above the cheapest tier", lines)
        return y + h

    def paint_race(self, m: dict, x: float, y: float, w: float, h: float) -> None:
        c = self.deck
        cx, cy, cw = self.panel(x, y, w, h, "THE RACE",
                                m["today"].strftime("%B %Y").upper())

        # Headroom in the scale so the break-even marker lands inside the track
        # instead of on top of the value column - break-even is usually the
        # largest of the three.
        scale = max(m["month_interest"], m["month_paid"], m["break_even"], 1.0) * 1.14
        label_w, value_w = 52, 82
        bar_w = cw - label_w - value_w - 16

        for i, (label, value, color) in enumerate((
            ("Interest", m["month_interest"], THEME_DANGER),
            ("You paid", m["month_paid"], THEME_GOOD),
        )):
            row_y = cy + i * 24
            c.create_text(cx, row_y + 6, text=label, font=FONT_LABEL, fill=color, anchor="w")
            track_bar(c, cx + label_w, row_y, bar_w, 12, value / scale, color)
            c.create_text(cx + cw, row_y + 6, text=money(value), font=FONT_VALUE_S,
                          fill=color, anchor="e")

        # Break-even tick crosses both bars - the line you have to clear.
        mark = cx + label_w + bar_w * min(1.0, m["break_even"] / scale)
        c.create_line(mark, cy - 4, mark, cy + 40, fill=THEME_TEXT, width=1, dash=(2, 2))
        c.create_text(mark + 5, cy + 50, text=f"break-even {money(m['break_even'])}",
                      font=FONT_TINY, fill=THEME_MUTED, anchor="w")

        ahead = m["month_paid"] - m["break_even"]
        if not m["snapshots"]:
            verdict, color = "", THEME_TEXT
        elif m["month_paid"] <= 0:
            verdict, color = "NOT IN THE FIGHT YET", THEME_DIM
        elif ahead >= 0:
            verdict, color = f"WINNING  -  {money(ahead)} into principal", THEME_GOOD
        else:
            verdict, color = f"LOSING  -  {money(-ahead)} short of break-even", THEME_DANGER
        c.create_text(cx, cy + 74, text=verdict, font=FONT_CARD, fill=color, anchor="w")

        days_left = max(0, (month_end(m["today"]) - m["today"]).days)
        c.create_text(cx + cw, cy + 74,
                      text=f"burning {money(m['total_daily'])}/day  -  {days_left} days left",
                      font=FONT_SMALL, fill=THEME_DIM, anchor="e")

        self.zone(x, y, x + w, y + h, "race", f"The month's fight - {m['today']:%B %Y}", [
            ("Interest accrued", money(m["month_interest"]), THEME_DANGER),
            ("Payments logged", money(m["month_paid"]), THEME_GOOD),
            ("Break-even", money(m["break_even"]), THEME_TEXT),
            ("", "", ""),
            ("Ahead by" if ahead >= 0 else "Short by", money(abs(ahead)),
             THEME_GOOD if ahead >= 0 else THEME_DANGER),
            ("Per day", money(m["total_daily"]), THEME_DANGER),
            ("Per hour", money(m["total_daily"] / 24.0), THEME_DANGER),
            ("Days left", str(days_left), THEME_TEXT),
        ])

    def paint_loans(self, m: dict, x: float, y: float, w: float, h: float) -> None:
        c = self.deck
        cx, cy, cw = self.panel(x, y, w, h, "KILL ORDER",
                                "highest rate first, smallest balance breaks ties")
        if not m["ordered"]:
            c.create_text(cx, cy + 10, text='No active loans yet - hit "Manage Loans" to add one.',
                          font=FONT_BODY, fill=THEME_DIM, anchor="w")
            return

        # Rows breathe into the space the panel has rather than clumping at the
        # top of an empty box, but never so far apart that the list stops
        # reading as one thing.
        footer_h = 24
        available = y + h - PANEL_PAD - footer_h - cy
        row_h = max(30, min(40, available / max(1, len(m["ordered"]))))
        fits = max(1, int(available // row_h))
        shown = m["ordered"][:fits]
        cpi = m["cpi"]

        for i, snap in enumerate(shown):
            row_y = cy + i * row_h
            key = f"loan:{snap['id']}"
            hovered = self.hover_key == key
            is_target = i == 0
            if hovered or is_target:
                rounded(c, cx - 6, row_y - 2, cx + cw + 6, row_y + row_h - 6, 7,
                        THEME_HOVER if hovered else THEME_PANEL_ALT, outline="")

            if is_target:
                rounded(c, cx, row_y + 3, cx + 48, row_y + 17, 4, THEME_WARN, outline="")
                c.create_text(cx + 24, row_y + 10, text="TARGET", font=FONT_TINY, fill=ENTRY_FG)
            else:
                c.create_text(cx + 24, row_y + 10, text=f"#{i + 1}", font=FONT_LABEL,
                              fill=THEME_FAINT)

            c.create_text(cx + 60, row_y + 10, text=snap["name"], font=FONT_CARD,
                          fill=THEME_TEXT, anchor="w")

            rate_color = THEME_DANGER if (cpi is not None and snap["rate"] > cpi) else THEME_GOOD
            c.create_text(cx + 208, row_y + 10, text=f"{snap['rate']:.2f}%", font=FONT_MONO,
                          fill=rate_color, anchor="w")
            c.create_text(cx + 262, row_y + 10, text=f"{money(snap['daily'])}/day",
                          font=FONT_MONO_S, fill=THEME_FAINT, anchor="w")

            # Right side: balance, then a slim progress pill.
            pill_w = 74
            c.create_text(cx + cw - pill_w - 46, row_y + 10, text=money(snap["total"]),
                          font=FONT_VALUE_S, fill=THEME_TEXT, anchor="e")
            if snap["original"] > 0:
                done = max(0.0, min(1.0, 1.0 - snap["principal"] / snap["original"]))
                track_bar(c, cx + cw - pill_w - 34, row_y + 6, pill_w, 6, done,
                          progress_accent(done))
                c.create_text(cx + cw, row_y + 10, text=f"{done * 100:.0f}%",
                              font=FONT_MONO_S, fill=THEME_DIM, anchor="e")

            real = snap["rate"] - cpi if cpi is not None else None
            self.zone(cx - 6, row_y - 2, cx + cw + 6, row_y + row_h - 6, key, snap["name"], [
                ("Principal", money(snap["principal"]), THEME_TEXT),
                ("Unpaid interest", money(snap["accrued"]), THEME_WARN),
                ("Balance", money(snap["total"]), THEME_TEXT),
                ("", "", ""),
                ("Rate", f"{snap['rate']:.2f}%", rate_color),
                ("Real rate vs CPI", f"{real:+.2f}%" if real is not None else "--",
                 THEME_DANGER if (real or 0) > 0 else THEME_GOOD),
                ("Costs you", f"{money(snap['daily'])}/day", THEME_DANGER),
                ("This month", money(snap["daily"] * days_in_month(m["today"])), THEME_DANGER),
                ("", "", ""),
                ("Originally", money(snap["original"]) if snap["original"] else "not set", THEME_DIM),
                ("Paid off", f"{(1 - snap['principal'] / snap['original']) * 100:.1f}%"
                 if snap["original"] > 0 else "--", THEME_GOOD),
                ("Snapshot", snap["snapshot_at"].strftime("%Y-%m-%d"), THEME_DIM),
            ], action=self.open_payment_modal, cursor="pointinghand")

        if len(m["ordered"]) > len(shown):
            c.create_text(cx, cy + len(shown) * row_h + 8,
                          text=f"+ {len(m['ordered']) - len(shown)} more - resize the window",
                          font=FONT_SMALL, fill=THEME_DIM, anchor="w")

        # Footer pinned to the panel: what the current target is actually worth.
        target = m["ordered"][0]
        foot_y = y + h - PANEL_PAD - 4
        c.create_text(cx, foot_y,
                      text=f"Kill {target['name']} and you stop paying "
                           f"{money(target['daily'] * 365.25 / 12)}/mo in interest on it",
                      font=FONT_SMALL, fill=THEME_DIM, anchor="w")
        c.create_text(cx + cw, foot_y,
                      text=f"{len(m['ordered'])} active  -  {money(m['total_daily'])}/day total",
                      font=FONT_SMALL, fill=THEME_FAINT, anchor="e")

    def paint_inflation(self, m: dict, x: float, y: float, w: float, h: float) -> None:
        c = self.deck
        figures = self.inflation
        stamp = f"CPI-U {figures.get('as_of', '--')}"
        if not figures.get("live"):
            stamp += "  (offline)"
        cx, cy, cw = self.panel(x, y, w, h, "vs INFLATION", stamp)

        cpi = m["cpi"]
        if cpi is None or not m["snapshots"]:
            c.create_text(cx, cy + 12, text="No CPI data yet.", font=FONT_BODY,
                          fill=THEME_DIM, anchor="w")
            return

        split = m["vs_cpi"]
        real = split["real_blended"]
        real_color = THEME_DANGER if real > 0 else THEME_GOOD
        c.create_text(cx, cy + 12, text=f"{real:+.2f}%", font=FONT_VALUE_L,
                      fill=real_color, anchor="w")
        c.create_text(cx, cy + 32, text=f"{m['blended']:.2f}% blended  -  {cpi:.2f}% CPI",
                      font=FONT_SMALL, fill=THEME_DIM, anchor="w")
        self.zone(cx, cy, cx + 130, cy + 40, "real", "Real cost of the debt", [
            ("Your blended rate", f"{m['blended']:.2f}%", THEME_TEXT),
            ("Headline CPI", f"{cpi:.2f}%", THEME_TEXT),
            ("Real rate", f"{real:+.2f}%", real_color),
            ("", "", ""),
            ("Meaning", "above zero costs you" if real > 0 else "below zero: inflation pays",
             real_color),
        ])

        # The four CPI figures, right-aligned in two mono columns.
        pairs = (
            (("headline 12-mo", figures.get("headline")), ("3-mo ann.", figures.get("headline_3mo"))),
            (("core 12-mo", figures.get("core")), ("6-mo ann.", figures.get("headline_6mo"))),
        )
        # Stat strip: value over label, centered per column. Side-by-side
        # label/value columns kept colliding as soon as the window narrowed.
        stats = (
            ("headline", figures.get("headline")),
            ("core", figures.get("core")),
            ("3-mo ann.", figures.get("headline_3mo")),
            ("6-mo ann.", figures.get("headline_6mo")),
        )
        strip_x = cx + 140
        strip_w = cx + cw - strip_x
        step = strip_w / len(stats)
        for i, (label, value) in enumerate(stats):
            mid = strip_x + step * (i + 0.5)
            c.create_text(mid, cy + 10, text=f"{value:.2f}%" if value is not None else "--",
                          font=FONT_MONO, fill=THEME_MUTED)
            c.create_text(mid, cy + 26, text=label, font=FONT_TINY, fill=THEME_FAINT)
        self.zone(strip_x - 8, cy - 4, cx + cw, cy + 34, "cpi", "Consumer Price Index", [
            ("Headline, 12-mo", f"{figures.get('headline', 0):.2f}%", THEME_TEXT),
            ("Core, 12-mo", f"{figures.get('core', 0):.2f}%", THEME_TEXT),
            ("Headline, 3-mo ann.", f"{figures.get('headline_3mo', 0):.2f}%", THEME_MUTED),
            ("Headline, 6-mo ann.", f"{figures.get('headline_6mo', 0):.2f}%", THEME_MUTED),
            ("", "", ""),
            ("As of", str(figures.get("as_of", "--")), THEME_DIM),
            ("Source", "BLS, live" if figures.get("live") else "bundled fallback", THEME_DIM),
            ("12-mo basis", "not seasonally adj.", THEME_DIM),
            ("Annualized basis", "seasonally adj.", THEME_DIM),
        ])

        # Rate ladder: every loan placed on a rate axis, CPI drawn as the line
        # that decides which side of the fight it's on.
        axis_y = cy + 104
        axis_x0, axis_x1 = cx + 6, cx + cw - 6
        top_rate = max([s["rate"] for s in m["snapshots"]] + [cpi]) * 1.15 or 1.0

        def rate_x(rate: float) -> float:
            return axis_x0 + (axis_x1 - axis_x0) * min(1.0, rate / top_rate)

        c.create_line(axis_x0, axis_y, axis_x1, axis_y, fill=THEME_BORDER, width=1)
        for tick in range(0, int(top_rate) + 1):
            tx = rate_x(tick)
            c.create_line(tx, axis_y, tx, axis_y + 4, fill=THEME_BORDER, width=1)
            c.create_text(tx, axis_y + 12, text=f"{tick}%", font=FONT_TINY, fill=THEME_FAINT)

        cpi_x = rate_x(cpi)
        c.create_line(cpi_x, axis_y - 54, cpi_x, axis_y + 5, fill=THEME_TEXT, width=1, dash=(3, 3))
        c.create_text(cpi_x, axis_y - 62, text=f"CPI {cpi:.2f}%", font=FONT_TINY,
                      fill=THEME_TEXT)

        biggest = max(s["total"] for s in m["snapshots"]) or 1.0
        # Loans at the same rate would stack into one blob (a sub/unsub pair
        # always shares a rate), so nudge each repeat up a row.
        seen: dict[int, int] = {}
        for snap in sorted(m["snapshots"], key=lambda s: -s["total"]):
            dot_x = rate_x(snap["rate"])
            radius = 4 + 9 * math.sqrt(snap["total"] / biggest)
            bucket = int(dot_x / 14)
            level = seen.get(bucket, 0)
            seen[bucket] = level + 1
            dot_y = axis_y - 21 - level * 15
            above = snap["rate"] > cpi
            color = THEME_DANGER if above else THEME_GOOD
            key = f"dot:{snap['id']}"
            if self.hover_key == key:
                c.create_oval(dot_x - radius - 3, dot_y - radius - 3,
                              dot_x + radius + 3, dot_y + radius + 3,
                              outline=THEME_TEXT, width=1)
            c.create_oval(dot_x - radius, dot_y - radius, dot_x + radius, dot_y + radius,
                          fill=color, outline=THEME_PANEL, width=1)
            self.zone(dot_x - radius - 2, dot_y - radius - 2, dot_x + radius + 2,
                      dot_y + radius + 2, key, snap["name"], [
                          ("Balance", money(snap["total"]), THEME_TEXT),
                          ("Rate", f"{snap['rate']:.2f}%", color),
                          ("CPI", f"{cpi:.2f}%", THEME_TEXT),
                          ("Real rate", f"{snap['rate'] - cpi:+.2f}%", color),
                          ("", "", ""),
                          ("Verdict", "costs you in real terms" if above
                           else "inflation outruns it", color),
                      ])

        # Legend doubles as the above/below split.
        legend_y = axis_y + 30
        for i, (color, amount, count, note) in enumerate((
            (THEME_DANGER, split["above"], split["above_count"], f"priced over {cpi:.2f}%"),
            (INFLATION_UNDER, split["below"], split["below_count"], f"under {cpi:.2f}%"),
        )):
            if not count:
                continue
            row_y = legend_y + i * 17
            c.create_oval(cx + 2, row_y - 3, cx + 8, row_y + 3,
                          fill=THEME_GOOD if i else color, outline="")
            c.create_text(cx + 16, row_y, text=money(amount), font=FONT_MONO,
                          fill=THEME_TEXT, anchor="w")
            c.create_text(cx + 96, row_y,
                          text=f"on {count} loan{'s' if count != 1 else ''} {note}",
                          font=FONT_SMALL, fill=THEME_MUTED, anchor="w")

    def paint_month(self, m: dict, x: float, y: float, w: float, h: float) -> None:
        c = self.deck
        cx, cy, cw = self.panel(x, y, w, h, "PAYMENTS THIS MONTH", money(m["month_paid"]),
                                THEME_GOOD if m["month_paid"] > 0 else THEME_DIM)

        shown = sorted(m["month_payments"], key=lambda p: p["date"], reverse=True)
        if not shown:
            c.create_text(cx, cy + 10, text="Nothing logged this month yet.",
                          font=FONT_BODY, fill=THEME_DIM, anchor="w")
            self.zone(x, y, x + w, y + h, "month", "This month", [
                ("Break-even", money(m["break_even"]), THEME_TEXT),
                ("Paid so far", money(0), THEME_DIM),
                ("", "", ""),
                ("Click", "to log a payment", THEME_GOOD),
            ], action=self.open_payment_modal, cursor="pointinghand")
            return

        row_h = 21
        fits = max(1, int((y + h - PANEL_PAD - cy) // row_h))
        for i, payment in enumerate(shown[:fits]):
            row_y = cy + i * row_h
            loan = self.loan_by_id(payment.get("loan_id", ""))
            c.create_text(cx, row_y + 7, text=parse_date_str(payment["date"]).strftime("%m/%d"),
                          font=FONT_MONO_S, fill=THEME_DIM, anchor="w")
            c.create_text(cx + 42, row_y + 7, text=money(payment["amount"]),
                          font=FONT_VALUE_S, fill=THEME_GOOD, anchor="w")
            c.create_text(cx + cw, row_y + 7, text=loan["name"] if loan else "whole account",
                          font=FONT_SMALL, fill=THEME_MUTED, anchor="e")
        if len(shown) > fits:
            c.create_text(cx, cy + fits * row_h + 6,
                          text=f"+ {len(shown) - fits} more", font=FONT_SMALL,
                          fill=THEME_DIM, anchor="w")
        self.zone(x, y, x + w, y + h, "month", "This month", [
            ("Payments", str(len(shown)), THEME_TEXT),
            ("Total paid", money(m["month_paid"]), THEME_GOOD),
            ("Break-even", money(m["break_even"]), THEME_TEXT),
            ("", "", ""),
            ("Into principal", money(max(0.0, m["month_paid"] - m["break_even"])), THEME_GOOD),
            ("Click", "to log another", THEME_GOOD),
        ], action=self.open_payment_modal, cursor="pointinghand")

    def paint_trophies(self, m: dict, x: float, y: float, w: float, h: float) -> None:
        c = self.deck
        dead = m["dead"]
        killed = sum(max(0.0, float(l.get("original_amount", 0.0))) for l in dead)
        cx, cy, cw = self.panel(x, y, w, h, "ELIMINATED",
                                f"{len(dead)} down" if dead else "")
        if not dead:
            c.create_text(cx, cy + 10, text="No kills yet.", font=FONT_BODY,
                          fill=THEME_DIM, anchor="w")
            return

        row_h = 22
        fits = max(1, int((y + h - PANEL_PAD - cy) // row_h))
        order = sorted(dead, key=lambda l: str(l.get("eliminated_on") or ""), reverse=True)
        for i, loan in enumerate(order[:fits]):
            row_y = cy + i * row_h
            rounded(c, cx, row_y, cx + 46, row_y + 15, 4, "#166534", outline="")
            c.create_text(cx + 23, row_y + 8, text="KILLED", font=FONT_TINY, fill=THEME_TEXT)
            c.create_text(cx + 58, row_y + 8, text=loan["name"], font=FONT_SMALL,
                          fill=THEME_MUTED, anchor="w")
            c.create_text(cx + cw, row_y + 8, text=loan.get("eliminated_on") or "",
                          font=FONT_MONO_S, fill=THEME_FAINT, anchor="e")
        if len(order) > fits:
            c.create_text(cx, cy + fits * row_h + 6, text=f"+ {len(order) - fits} earlier",
                          font=FONT_SMALL, fill=THEME_DIM, anchor="w")

        self.zone(x, y, x + w, y + h, "dead", "Loans killed", [
            ("Eliminated", str(len(dead)), THEME_GOOD),
            ("Borrowed on them", money(killed), THEME_TEXT),
            ("", "", ""),
            ("Still active", str(len(m["snapshots"])), THEME_TEXT),
            ("Next target", m["ordered"][0]["name"] if m["ordered"] else "--", THEME_WARN),
        ])

    def paint_status(self, m: dict, x: float, y: float, w: float) -> None:
        c = self.deck
        bits = []
        if m["snapshots"]:
            oldest = min(s["snapshot_at"] for s in m["snapshots"])
            age = (m["now"] - oldest).days
            bits.append(f"Oldest snapshot {oldest:%Y-%m-%d} ({age}d)")
        bits.append("Data in Application Support")
        bits.append("hover anything for detail")
        c.create_text(x, y, text="   |   ".join(bits), font=FONT_SMALL,
                      fill=THEME_FAINT, anchor="w")
        c.create_text(x + w, y, text=f"{m['now']:%a %d %b  %H:%M:%S}", font=FONT_MONO_S,
                      fill=THEME_FAINT, anchor="e")
    # -- loop -------------------------------------------------------------

    def tick(self) -> None:
        now = datetime.now()

        pending, self._inflation_pending = self._inflation_pending, None
        if pending:
            self.apply_inflation(pending)

        slot = int(now.timestamp()) // QUOTE_ROTATION_SECONDS
        if slot != self._quote_slot:
            self._quote_slot = slot
            if len(self.quotes) > 1:
                pick = random.choice(self.quotes)
                while pick == self.current_quote:
                    pick = random.choice(self.quotes)
                self.current_quote = pick

        self.refresh_display()
        self.root.after(TICK_MS, self.tick)


def main() -> None:
    root = tk.Tk()
    StudentLoanMotivatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
