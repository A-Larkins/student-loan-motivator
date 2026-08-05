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
# ---------------------------------------------------------------------------

THEME_BG = "#120a0c"
THEME_PANEL = "#241016"
THEME_PANEL_ALT = "#31161d"
THEME_BORDER = "#7a2b36"
THEME_TEXT = "#fdf0e6"
THEME_MUTED = "#c9a3a8"
THEME_DIM = "#8d6b70"
THEME_DANGER = "#f87171"
THEME_GOOD = "#4ade80"
THEME_WARN = "#fbbf24"
ENTRY_BG = "#fdf4ec"
ENTRY_FG = "#1a0b0e"

INFLATION_UNDER = "#14532d"  # balance sitting below CPI - inflation's problem, not yours

ACCENT_LOW = "#ef4444"   # drowning
ACCENT_MID = "#f59e0b"   # fighting
ACCENT_HIGH = "#22c55e"  # winning

FONT_TITLE = ("Avenir Next", 15, "bold")
FONT_CARD = ("Avenir Next", 10, "bold")
FONT_LABEL = ("Avenir Next", 9, "bold")
FONT_BODY = ("Avenir Next", 9)
FONT_SMALL = ("Avenir Next", 8)
FONT_HERO = ("Menlo", 26, "bold")
FONT_VALUE_L = ("Menlo", 17, "bold")
FONT_VALUE_M = ("Menlo", 13, "bold")
FONT_MONO = ("Menlo", 9)

WIN_WIDTH = 1180
WIN_HEIGHT = 1020
MIN_WIDTH = 960
MIN_HEIGHT = 620

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

class Bar:
    def __init__(self, parent: tk.Widget, height: int = 16, track: str = THEME_PANEL_ALT):
        self.canvas = tk.Canvas(parent, height=height, bg=track, highlightthickness=0, bd=0)
        self.height = height
        self.fraction = 0.0
        self.color = ACCENT_MID
        self.canvas.bind("<Configure>", lambda _e: self._draw())

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)
        return self

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    def set(self, fraction: float, color: str | None = None) -> None:
        self.fraction = max(0.0, min(1.0, fraction))
        if color:
            self.color = color
        self._draw()

    def _draw(self) -> None:
        self.canvas.delete("fill")
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        filled = int(width * self.fraction)
        if filled > 0:
            self.canvas.create_rectangle(0, 0, filled, self.height, fill=self.color, width=0, tags="fill")


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
        self.loan_rows: list[dict] = []
        self.payment_rows: list[dict] = []
        self.trophy_rows: list[dict] = []
        self._kill_signature: tuple | None = None
        self._pay_signature: tuple | None = None
        self._trophy_signature: tuple | None = None

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
        # manager, and the squeeze lands on whichever panel has weight - which
        # is how Kill Order ended up invisible outside fullscreen.
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

    def card(self, parent: tk.Widget, title: str, row: int, col: int, **grid) -> tuple[tk.Frame, tk.Label]:
        outer = tk.Frame(parent, bg=THEME_BORDER, bd=0)
        outer.grid(row=row, column=col, sticky="nsew", padx=5, pady=3, **grid)
        inner = tk.Frame(outer, bg=THEME_PANEL, bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Label(
            inner, text=title, font=FONT_CARD, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w"
        )
        header.pack(fill="x", padx=12, pady=(6, 3))
        body = tk.Frame(inner, bg=THEME_PANEL)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 7))
        return body, header

    def legend_row(self, parent: tk.Widget, row: int, color: str) -> dict:
        """Dot + amount + explanation, one line, aligned across rows."""
        holder = tk.Frame(parent, bg=THEME_PANEL)
        holder.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        holder.grid_columnconfigure(2, weight=1)

        tk.Label(holder, text="●", font=FONT_SMALL, bg=THEME_PANEL, fg=color).grid(
            row=0, column=0, padx=(0, 6)
        )
        amount = tk.Label(holder, text="", font=FONT_MONO, bg=THEME_PANEL, fg=THEME_TEXT,
                          width=11, anchor="w")
        amount.grid(row=0, column=1, sticky="w")
        note = tk.Label(holder, text="", font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w")
        note.grid(row=0, column=2, sticky="w")
        return {"amount": amount, "note": note}

    def scrollable(self, parent: tk.Widget) -> tk.Frame:
        """A vertically scrolling region that only shows its bar when needed."""
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(parent, bg=THEME_PANEL, highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)

        inner = tk.Frame(canvas, bg=THEME_PANEL)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.grid_columnconfigure(0, weight=1)

        def sync(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            if inner.winfo_reqheight() > canvas.winfo_height():
                bar.grid(row=0, column=1, sticky="ns")
            else:
                bar.grid_remove()
                canvas.yview_moveto(0)

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(window, width=e.width), sync()))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 3 or (1 if e.delta < 0 else -1)), "units"))
        return inner

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
        self.root.grid_columnconfigure(0, weight=1)

        # ---- header ----
        header = tk.Frame(self.root, bg=THEME_BG)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
        header.grid_columnconfigure(1, weight=1)

        left = tk.Frame(header, bg=THEME_BG)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="STUDENT LOAN MOTIVATOR", font=FONT_TITLE, bg=THEME_BG, fg=THEME_TEXT).pack(anchor="w")
        self.quote_label = tk.Label(
            left, text=self.current_quote, font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM, anchor="w"
        )
        self.quote_label.pack(anchor="w")

        buttons = tk.Frame(header, bg=THEME_BG)
        buttons.grid(row=0, column=2, sticky="e")
        self.button(buttons, "Log Payment", self.open_payment_modal, "#166534").pack(side="left", padx=3)
        self.button(buttons, "Monthly Snapshot", self.open_snapshot_modal, "#92400e").pack(side="left", padx=3)
        self.button(buttons, "Manage Loans", self.open_loans_modal).pack(side="left", padx=3)

        # ---- hero ----
        hero_body, _ = self.card(self.root, "TOTAL OWED RIGHT NOW", 1, 0)
        self.root.grid_rowconfigure(1, weight=0)
        hero_body.grid_columnconfigure(0, weight=1)
        hero_body.grid_columnconfigure(1, weight=0)

        hero_left = tk.Frame(hero_body, bg=THEME_PANEL)
        hero_left.grid(row=0, column=0, sticky="w")
        self.total_value = tk.Label(hero_left, text="$0.00", font=FONT_HERO, bg=THEME_PANEL, fg=THEME_TEXT)
        self.total_value.pack(anchor="w")
        self.total_detail = tk.Label(
            hero_left, text="Add your loans to begin.", font=FONT_BODY, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w",
            justify="left",
        )
        self.total_detail.pack(anchor="w")

        hero_right = tk.Frame(hero_body, bg=THEME_PANEL, cursor="hand2")
        hero_right.grid(row=0, column=1, sticky="e")
        freedom_title = tk.Label(hero_right, text="FREEDOM DATE", font=FONT_LABEL, bg=THEME_PANEL, fg=THEME_MUTED)
        freedom_title.pack(anchor="e")
        self.freedom_value = tk.Label(hero_right, text="--", font=FONT_VALUE_L, bg=THEME_PANEL, fg=THEME_WARN, cursor="hand2")
        self.freedom_value.pack(anchor="e")
        self.freedom_detail = tk.Label(hero_right, text="", font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_DIM, cursor="hand2")
        self.freedom_detail.pack(anchor="e")
        for widget in (hero_right, freedom_title, self.freedom_value, self.freedom_detail):
            widget.bind("<Button-1>", self.open_target_modal)

        self.payoff_bar = Bar(hero_body, height=10)
        self.payoff_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.payoff_label = tk.Label(
            hero_body, text="", font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w"
        )
        self.payoff_label.grid(row=2, column=0, sticky="w", pady=(3, 0))

        # The cheap tier isn't worth attacking; this is the part that is.
        self.above_floor_label = tk.Label(
            hero_body, text="", font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_WARN, anchor="e"
        )
        self.above_floor_label.grid(row=2, column=1, sticky="e", pady=(3, 0))

        # ---- body: two columns ----
        #
        # Everything used to stack in full-width bands, which meant Kill Order -
        # the panel you actually work in - was last in line for leftover height
        # and got squeezed to a couple of visible rows. Splitting the body lets
        # the short reference cards stack down the right while Kill Order takes
        # every pixel the left column has left.
        body = tk.Frame(self.root, bg=THEME_BG)
        body.grid(row=2, column=0, sticky="nsew", padx=4)
        self.root.grid_rowconfigure(2, weight=1)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left_col = tk.Frame(body, bg=THEME_BG)
        left_col.grid(row=0, column=0, sticky="nsew")
        left_col.grid_columnconfigure(0, weight=1)
        left_col.grid_rowconfigure(1, weight=1)

        right_col = tk.Frame(body, bg=THEME_BG)
        right_col.grid(row=0, column=1, sticky="nsew")
        right_col.grid_columnconfigure(0, weight=1)
        right_col.grid_rowconfigure(2, weight=1)

        race_body, self.race_header = self.card(left_col, "THE RACE", 0, 0)
        race_body.grid_columnconfigure(1, weight=1)

        tk.Label(race_body, text="Interest", font=FONT_LABEL, bg=THEME_PANEL, fg=THEME_DANGER, width=9, anchor="w").grid(row=0, column=0, sticky="w")
        self.race_interest_bar = Bar(race_body, height=16)
        self.race_interest_bar.grid(row=0, column=1, sticky="ew", padx=6)
        self.race_interest_value = tk.Label(race_body, text="$0.00", font=FONT_VALUE_M, bg=THEME_PANEL, fg=THEME_DANGER, width=11, anchor="e")
        self.race_interest_value.grid(row=0, column=2, sticky="e")

        tk.Label(race_body, text="You paid", font=FONT_LABEL, bg=THEME_PANEL, fg=THEME_GOOD, width=9, anchor="w").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.race_paid_bar = Bar(race_body, height=16)
        self.race_paid_bar.grid(row=1, column=1, sticky="ew", padx=6, pady=(5, 0))
        self.race_paid_value = tk.Label(race_body, text="$0.00", font=FONT_VALUE_M, bg=THEME_PANEL, fg=THEME_GOOD, width=11, anchor="e")
        self.race_paid_value.grid(row=1, column=2, sticky="e", pady=(5, 0))

        # The old BLEEDING card showed the same month-interest number as the
        # Interest bar right here, so it was two cards saying one thing. Its
        # burn rates live on as a footnote under the bars instead.
        self.race_burn = tk.Label(race_body, text="", font=FONT_MONO, bg=THEME_PANEL, fg=THEME_DIM, anchor="w")
        self.race_burn.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.race_verdict = tk.Label(race_body, text="", font=FONT_CARD, bg=THEME_PANEL, fg=THEME_TEXT, anchor="w")
        self.race_verdict.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.race_detail = tk.Label(race_body, text="", font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w", justify="left")
        self.race_detail.grid(row=4, column=0, columnspan=3, sticky="w")

        # ---- inflation ----
        # Hero number is the REAL rate, not CPI - that's the thing you can't
        # read anywhere else. The CPI figures themselves are the supporting
        # column on the right.
        infl_body, self.infl_header = self.card(right_col, "vs INFLATION", 0, 0)
        infl_body.grid_columnconfigure(0, weight=1)
        infl_body.grid_columnconfigure(1, weight=0)

        infl_left = tk.Frame(infl_body, bg=THEME_PANEL)
        infl_left.grid(row=0, column=0, sticky="sw")
        self.infl_real = tk.Label(
            infl_left, text="--", font=FONT_VALUE_L, bg=THEME_PANEL, fg=THEME_DANGER, anchor="w",
        )
        self.infl_real.pack(anchor="w")
        self.infl_real_caption = tk.Label(
            infl_left, text="real rate after CPI", font=FONT_SMALL, bg=THEME_PANEL,
            fg=THEME_DIM, anchor="w",
        )
        self.infl_real_caption.pack(anchor="w")

        self.infl_figures = tk.Label(
            infl_body, text="", font=FONT_MONO, bg=THEME_PANEL, fg=THEME_MUTED,
            anchor="e", justify="left",
        )
        self.infl_figures.grid(row=0, column=1, sticky="e")

        # One bar, two meanings: the red fill is balance priced above CPI, the
        # green track behind it is the balance inflation is quietly eating.
        self.infl_bar = Bar(infl_body, height=9, track=INFLATION_UNDER)
        self.infl_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(9, 3))

        # Legend under the bar. Colored dot, amount, then what it MEANS - the
        # first cut said "above CPI / under it" and nobody could tell what was
        # above what.
        self.infl_above = self.legend_row(infl_body, 2, THEME_DANGER)
        self.infl_below = self.legend_row(infl_body, 3, THEME_GOOD)

        # ---- kill order: fills whatever the left column has left ----
        kill_body, self.kill_header = self.card(left_col, "KILL ORDER", 1, 0)
        self.kill_container = self.scrollable(kill_body)
        self.kill_empty = tk.Label(
            self.kill_container,
            text="No active loans yet - hit \"Manage Loans\" to add one.",
            font=FONT_BODY,
            bg=THEME_PANEL,
            fg=THEME_DIM,
            anchor="w",
        )

        # ---- payments + trophies stack down the right column ----
        pay_body, self.pay_header = self.card(right_col, "PAYMENTS THIS MONTH", 1, 0)
        pay_body.grid_columnconfigure(0, weight=1)
        self.pay_container = tk.Frame(pay_body, bg=THEME_PANEL)
        self.pay_container.grid(row=0, column=0, sticky="nsew")
        self.pay_container.grid_columnconfigure(0, weight=1)
        self.pay_empty = tk.Label(
            self.pay_container, text="Nothing logged this month yet.", font=FONT_BODY,
            bg=THEME_PANEL, fg=THEME_DIM, anchor="w",
        )

        trophy_body, self.trophy_header = self.card(right_col, "ELIMINATED", 2, 0)
        trophy_body.grid_columnconfigure(0, weight=1)
        self.trophy_container = tk.Frame(trophy_body, bg=THEME_PANEL)
        self.trophy_container.grid(row=0, column=0, sticky="nsew")
        self.trophy_container.grid_columnconfigure(0, weight=1)
        self.trophy_empty = tk.Label(
            self.trophy_container, text="No kills yet.", font=FONT_BODY,
            bg=THEME_PANEL, fg=THEME_DIM, anchor="w",
        )

        # ---- footer ----
        self.status_label = tk.Label(
            self.root, text="", font=FONT_SMALL, bg=THEME_BG, fg=THEME_DIM, anchor="w"
        )
        self.status_label.grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 7))

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

    def refresh_display(self) -> None:
        now = datetime.now()
        today = now.date()

        actives = self.active_loans()
        snapshots = [project_loan(l, self.expanded_payments(), now) for l in actives]
        ordered = kill_order(snapshots)

        total_owed = sum(s["total"] for s in snapshots)
        total_principal = sum(s["principal"] for s in snapshots)
        total_accrued = sum(s["accrued"] for s in snapshots)
        total_daily = sum(s["daily"] for s in snapshots)

        original_all = sum(max(0.0, float(l.get("original_amount", 0.0))) for l in self.data["loans"])
        paid_off_fraction = 0.0
        if original_all > 0:
            paid_off_fraction = max(0.0, min(1.0, 1.0 - (total_principal / original_all)))
        accent = progress_accent(paid_off_fraction)

        # -- hero --
        self.total_value.configure(text=money(total_owed), fg=accent if total_owed > 0 else THEME_GOOD)
        if snapshots:
            self.total_detail.configure(
                text=f"{money(total_principal)} principal  +  {money(total_accrued)} accrued interest"
                     f"     across {len(snapshots)} loan{'s' if len(snapshots) != 1 else ''}"
            )
        else:
            self.total_detail.configure(text="Add your loans to begin.")

        if original_all > 0:
            self.payoff_bar.set(paid_off_fraction, accent)
            self.payoff_label.configure(
                text=f"{paid_off_fraction * 100:.1f}% paid off  -  {money(original_all - total_principal)} killed of {money(original_all)} borrowed"
            )
        else:
            self.payoff_bar.set(0.0, THEME_PANEL_ALT)
            self.payoff_label.configure(text="Add original loan amounts to track lifetime progress.")

        split = above_floor_split(snapshots)
        if split["count"] and split["above"] > 0:
            self.above_floor_label.configure(
                text=f"Above {split['floor']:.2f}%:  {money(split['above'])}"
                     f"  -  {split['fraction'] * 100:.1f}% of the balance"
                     f"  ({split['count']} loan{'s' if split['count'] != 1 else ''})",
                fg=THEME_WARN,
            )
        elif snapshots:
            self.above_floor_label.configure(
                text=f"Everything left is at {split['floor']:.2f}% - the cheap tier", fg=THEME_GOOD
            )
        else:
            self.above_floor_label.configure(text="")

        pace, pace_source = self.planned_monthly()
        free_date, months = freedom_date(total_owed, blended_rate(snapshots), pace, today)
        if total_owed <= 0 and self.data["loans"]:
            self.freedom_value.configure(text="DEBT FREE", fg=THEME_GOOD)
            self.freedom_detail.configure(text="")
        elif free_date and months:
            self.freedom_value.configure(text=free_date.strftime("%b %Y"), fg=THEME_WARN)
            years, rem = divmod(months, 12)
            span = f"{years}y {rem}m" if years else f"{rem} months"
            self.freedom_detail.configure(text=f"{span} at {money(pace)}/mo {pace_source}  -  click to change")
        else:
            self.freedom_value.configure(text="--", fg=THEME_DIM)
            self.freedom_detail.configure(
                text="Click to set a monthly target" if total_owed > 0 else ""
            )

        # -- the race --
        month_interest = sum(month_interest_for(l, self.expanded_payments(), now) for l in actives)
        month_payments = self.payments_in_month(today)
        month_paid = sum(p["amount"] for p in month_payments)
        break_even = total_daily * days_in_month(today)

        self.race_header.configure(text=f"THE RACE  -  {today.strftime('%B %Y')}")
        scale = max(month_interest, month_paid, break_even, 1.0)
        self.race_interest_bar.set(month_interest / scale, THEME_DANGER)
        self.race_paid_bar.set(month_paid / scale, THEME_GOOD)
        self.race_interest_value.configure(text=money(month_interest))
        self.race_paid_value.configure(text=money(month_paid))

        if not snapshots:
            self.race_verdict.configure(text="", fg=THEME_TEXT)
            self.race_detail.configure(text="")
        elif month_paid <= 0:
            self.race_verdict.configure(text="NOT IN THE FIGHT YET", fg=THEME_DIM)
            self.race_detail.configure(
                text=f"{money(break_even)} is the rent to stand still this month. "
                     f"Everything past it shrinks the debt."
            )
        elif month_paid >= break_even:
            ahead = month_paid - break_even
            self.race_verdict.configure(text=f"WINNING  -  {money(ahead)} into principal", fg=THEME_GOOD)
            self.race_detail.configure(
                text=f"Break-even was {money(break_even)}. You cleared it with {len(month_payments)} payment"
                     f"{'s' if len(month_payments) != 1 else ''}."
            )
        else:
            behind = break_even - month_paid
            self.race_verdict.configure(text=f"LOSING  -  {money(behind)} short of break-even", fg=THEME_DANGER)
            days_left = max(0, (month_end(today) - today).days)
            self.race_detail.configure(
                text=f"Break-even is {money(break_even)} this month. {days_left} day"
                     f"{'s' if days_left != 1 else ''} left to catch up."
            )

        self.race_burn.configure(
            text=f"burning {money(total_daily)}/day  -  {money(total_daily / 24.0)}/hr"
                 f"  -  {money(break_even)} for the full month"
        )

        # -- inflation --
        self.render_inflation(snapshots)

        # -- kill order --
        self.render_kill_order(ordered, accent)

        # -- payments --
        self.render_payments(month_payments, now)
        self.pay_header.configure(text=f"PAYMENTS THIS MONTH  -  {money(month_paid)}")

        # -- trophies --
        self.render_trophies()

        # -- status --
        bits = []
        if snapshots:
            oldest = min(s["snapshot_at"] for s in snapshots)
            age = (now - oldest).days
            bits.append(f"Oldest snapshot: {oldest.strftime('%Y-%m-%d')} ({age} day{'s' if age != 1 else ''} ago)")
            if age >= 35:
                bits.append("Time to re-snapshot")
        bits.append("Data in Application Support")
        self.status_label.configure(text="   |   ".join(bits))

    # Rebuilding these rows on every tick made the panel strobe once a second
    # and left it blank for part of each frame. Build only when the underlying
    # set actually changes; otherwise just retext what moved.

    def render_inflation(self, snapshots: list[dict]) -> None:
        figures = self.inflation
        cpi = figures.get("headline")

        # Two by two, padded to fixed widths so the mono columns line up on the
        # decimal point. Four figures in two lines instead of four - vertical
        # space in this window belongs to Kill Order.
        def cell(label: str, value: float | None, width: int) -> str:
            return f"{label:<{width}}" + (f"{value:5.2f}%" if value is not None else "   --")

        pairs = (
            (("headline 12-mo", figures.get("headline")), ("3-mo ann.", figures.get("headline_3mo"))),
            (("core 12-mo", figures.get("core")), ("6-mo ann.", figures.get("headline_6mo"))),
        )
        self.infl_figures.configure(text="\n".join(
            f"{cell(*left, 15)}   {cell(*right, 11)}" for left, right in pairs
        ))

        stamp = f"CPI-U  {figures.get('as_of', '--')}"
        if not figures.get("live"):
            stamp += "  (offline)"
        self.infl_header.configure(text=f"vs INFLATION  -  {stamp}")

        if cpi is None or not snapshots:
            self.infl_real.configure(text="--", fg=THEME_DIM)
            self.infl_real_caption.configure(text="add loans to compare")
            self.infl_bar.set(0.0, THEME_PANEL_ALT)
            for legend in (self.infl_above, self.infl_below):
                legend["amount"].configure(text="")
                legend["note"].configure(text="")
            return

        split = inflation_verdict(snapshots, cpi)
        real = split["real_blended"]
        blended = blended_rate(snapshots)
        self.infl_real.configure(
            text=f"{real:+.2f}%", fg=THEME_DANGER if real > 0 else THEME_GOOD
        )
        # Spell out the arithmetic - a bare "+0.37%" doesn't say where it came from.
        self.infl_real_caption.configure(text=f"{blended:.2f}% blended - {cpi:.2f}% CPI")

        self.infl_bar.set(split["fraction"], THEME_DANGER)

        above_n, below_n = split["above_count"], split["below_count"]
        self.infl_above["amount"].configure(text=money(split["above"]) if above_n else "")
        self.infl_above["note"].configure(
            text=f"on {above_n} loan{'s' if above_n != 1 else ''} priced over {cpi:.2f}% - really costing you"
            if above_n else ""
        )
        self.infl_below["amount"].configure(text=money(split["below"]) if below_n else "")
        self.infl_below["note"].configure(
            text=f"on {below_n} loan{'s' if below_n != 1 else ''} under {cpi:.2f}% - inflation eats these"
            if below_n else ""
        )

    def render_kill_order(self, ordered: list[dict], accent: str) -> None:
        signature = tuple(s["id"] for s in ordered)
        if signature != self._kill_signature:
            self.build_kill_rows(ordered)
            self._kill_signature = signature

        for snap, row in zip(ordered, self.loan_rows):
            row["name"].configure(text=snap["name"])
            row["detail"].configure(text=f"{snap['rate']:.2f}%   {money(snap['daily'])}/day")
            row["total"].configure(text=money(snap["total"]))
            if row["bar"] is not None and snap["original"] > 0:
                done = max(0.0, min(1.0, 1.0 - snap["principal"] / snap["original"]))
                row["bar"].set(done, progress_accent(done))
                row["pct"].configure(text=f"{done * 100:.0f}%")

    def build_kill_rows(self, ordered: list[dict]) -> None:
        for row in self.loan_rows:
            row["frame"].destroy()
        self.loan_rows = []

        if not ordered:
            self.kill_empty.grid(row=0, column=0, sticky="w", pady=4)
            self.kill_header.configure(text="KILL ORDER")
            return
        self.kill_empty.grid_forget()
        self.kill_header.configure(text="KILL ORDER  -  highest rate first, smallest balance breaks ties")

        # One line per loan. Two-line rows ran 61px each, which pushed the panel
        # past what fits on screen once the other cards took their share.
        for i, snap in enumerate(ordered):
            is_target = i == 0
            bg = THEME_PANEL_ALT if is_target else THEME_PANEL
            frame = tk.Frame(self.kill_container, bg=bg)
            frame.grid(row=i, column=0, sticky="ew", pady=1)
            frame.grid_columnconfigure(2, weight=1)

            tk.Label(
                frame, text="TARGET" if is_target else f"#{i + 1}", font=FONT_LABEL,
                bg=THEME_WARN if is_target else bg,
                fg=ENTRY_FG if is_target else THEME_DIM, width=7,
            ).grid(row=0, column=0, padx=(8, 11), pady=2)

            name = tk.Label(frame, text="", font=FONT_CARD, bg=bg, fg=THEME_TEXT,
                            anchor="w", width=16)
            name.grid(row=0, column=1, sticky="w")

            # Fixed width, or a narrow window trims the "/day" off the end.
            detail = tk.Label(frame, text="", font=FONT_MONO, bg=bg, fg=THEME_MUTED,
                              anchor="w", width=17)
            detail.grid(row=0, column=2, sticky="w")

            total = tk.Label(frame, text="", font=FONT_VALUE_M, bg=bg, fg=THEME_TEXT,
                             anchor="e", width=11)
            total.grid(row=0, column=3, sticky="e", padx=(8, 10))

            bar = pct = None
            if snap["original"] > 0:
                holder = tk.Frame(frame, bg=bg)
                holder.grid(row=0, column=4, sticky="e", padx=(0, 11))
                bar = Bar(holder, height=6)
                bar.canvas.configure(width=64)
                bar.pack(side="left")
                pct = tk.Label(holder, text="", font=FONT_SMALL, bg=bg, fg=THEME_DIM,
                               width=4, anchor="e")
                pct.pack(side="left", padx=(6, 0))

            self.loan_rows.append(
                {"frame": frame, "name": name, "detail": detail, "total": total,
                 "bar": bar, "pct": pct}
            )

    def render_payments(self, month_payments: list[dict], now: datetime) -> None:
        shown = sorted(month_payments, key=lambda p: p["date"], reverse=True)[:5]
        signature = tuple(p["id"] for p in shown) + (len(month_payments),)
        if signature != self._pay_signature:
            self.build_payment_rows(shown, len(month_payments))
            self._pay_signature = signature

        if not shown:
            return

        # An account payment lands as several synthetic rows; sum them back into
        # the single payment actually made.
        expanded = self.expanded_payments()
        allocations: dict[str, dict] = {}
        for loan in self.data["loans"]:
            snap = project_loan(loan, expanded, now)
            for applied in snap["payments_applied"]:
                origin = applied["payment"].get("parent_id") or applied["payment"]["id"]
                slot = allocations.setdefault(origin, {"to_interest": 0.0, "to_principal": 0.0})
                slot["to_interest"] += applied["to_interest"]
                slot["to_principal"] += applied["to_principal"]

        for payment, row in zip(shown, self.payment_rows):
            loan = self.loan_by_id(payment["loan_id"])
            if loan:
                name = loan["name"]
            elif payment.get("loan_id"):
                name = "(deleted loan)"
            else:
                name = ""  # account-wide; the amount already says everything
            applied = allocations.get(payment["id"])
            if applied:
                split = (
                    f"{money(applied['to_interest'])} interest"
                    f" / {money(applied['to_principal'])} principal"
                )
                name = f"{name}   {split}" if name else split
            row["target"].configure(text=name)

    def build_payment_rows(self, shown: list[dict], total_count: int) -> None:
        for row in self.payment_rows:
            row["frame"].destroy()
        self.payment_rows = []

        if not shown:
            self.pay_empty.grid(row=0, column=0, sticky="w", pady=4)
            return
        self.pay_empty.grid_forget()

        for i, payment in enumerate(shown):
            frame = tk.Frame(self.pay_container, bg=THEME_PANEL)
            frame.grid(row=i, column=0, sticky="ew", pady=2)
            frame.grid_columnconfigure(2, weight=1)

            tk.Label(
                frame, text=parse_date_str(payment["date"]).strftime("%m/%d"),
                font=FONT_MONO, bg=THEME_PANEL, fg=THEME_DIM, width=6, anchor="w",
            ).grid(row=0, column=0, sticky="w")
            tk.Label(
                frame, text=money(payment["amount"]), font=FONT_VALUE_M, bg=THEME_PANEL,
                fg=THEME_GOOD, width=10, anchor="w",
            ).grid(row=0, column=1, sticky="w", padx=(6, 12))

            target = tk.Label(frame, text="", font=FONT_MONO, bg=THEME_PANEL, fg=THEME_MUTED, anchor="w")
            target.grid(row=0, column=2, sticky="w")
            self.payment_rows.append({"frame": frame, "target": target})

        if total_count > len(shown):
            more = tk.Frame(self.pay_container, bg=THEME_PANEL)
            more.grid(row=len(shown), column=0, sticky="w", pady=(5, 0))
            tk.Label(
                more, text=f"+ {total_count - len(shown)} more this month",
                font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_DIM, anchor="w",
            ).pack(anchor="w")
            self.payment_rows.append({"frame": more, "target": tk.Label(more)})

    def render_trophies(self) -> None:
        dead = self.eliminated_loans()
        signature = tuple(l["id"] for l in dead)
        if signature == self._trophy_signature:
            return
        self._trophy_signature = signature

        for row in self.trophy_rows:
            row.destroy()
        self.trophy_rows = []

        if not dead:
            self.trophy_empty.grid(row=0, column=0, sticky="w", pady=4)
            self.trophy_header.configure(text="ELIMINATED")
            return
        self.trophy_empty.grid_forget()

        killed = sum(max(0.0, float(l.get("original_amount", 0.0))) for l in dead)
        self.trophy_header.configure(
            text=f"ELIMINATED  -  {len(dead)} down"
                 + (f", {money(killed)} borrowed" if killed > 0 else "")
        )

        # Newest kills first, capped - the count and total are in the header,
        # and this card must not outgrow the panel you actually work in.
        shown = sorted(dead, key=lambda l: str(l.get("eliminated_on") or ""), reverse=True)[:3]
        for i, loan in enumerate(shown):
            frame = tk.Frame(self.trophy_container, bg=THEME_PANEL)
            frame.grid(row=i, column=0, sticky="ew", pady=2)
            frame.grid_columnconfigure(1, weight=1)

            tk.Label(
                frame, text="KILLED", font=FONT_SMALL, bg="#166534", fg=THEME_TEXT, width=8, pady=1,
            ).grid(row=0, column=0, padx=(0, 11))
            tk.Label(
                frame, text=loan["name"], font=(FONT_SMALL[0], FONT_SMALL[1], "overstrike"),
                bg=THEME_PANEL, fg=THEME_MUTED, anchor="w",
            ).grid(row=0, column=1, sticky="w")
            tk.Label(
                frame, text=loan.get("eliminated_on") or "", font=FONT_SMALL,
                bg=THEME_PANEL, fg=THEME_DIM, anchor="e",
            ).grid(row=0, column=2, sticky="e")
            self.trophy_rows.append(frame)

        if len(dead) > len(shown):
            more = tk.Label(
                self.trophy_container, text=f"+ {len(dead) - len(shown)} earlier",
                font=FONT_SMALL, bg=THEME_PANEL, fg=THEME_DIM, anchor="w",
            )
            more.grid(row=len(shown), column=0, sticky="w", pady=(3, 0))
            self.trophy_rows.append(more)

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
            self.quote_label.configure(text=self.current_quote)

        self.refresh_display()
        self.root.after(TICK_MS, self.tick)


def main() -> None:
    root = tk.Tk()
    StudentLoanMotivatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
