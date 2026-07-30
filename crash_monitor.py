"""
=============================================================
  Inspirational Sharing — Crash Buy Signal Monitor
  GitHub Actions Edition (stateless, single-run) · v5

  HOW THIS VERSION IS DIFFERENT (and more reliable):
  - No always-on daemon. GitHub runs this ONCE per schedule, then exits.
    Nothing can "silently die" between runs.
  - Schedule lives in GitHub (UTC cron) — no timezone guessing in code.
  - Secrets (Telegram token, chat id) come from GitHub Secrets via env
    vars — never hardcoded, never committed.
  - State (all-time highs + which tranches already fired) is saved to
    monitor_state.json and committed back to the repo, so it survives
    between runs. Committing daily also keeps the scheduled workflow
    from being auto-disabled for inactivity.
  - Price fetch retries 3x and reports failures IN the Telegram message
    instead of silently skipping.
  - Tracks the actual ETFs (SPY/QQQ/IWDA) so shown prices are real.
  - Optional healthchecks.io ping = a dead-man's-switch that emails you
    if a run is ever missed.

  RUN LOCALLY (for testing):
    set TELEGRAM_BOT_TOKEN=...   (PowerShell: $env:TELEGRAM_BOT_TOKEN="...")
    set TELEGRAM_CHAT_ID=...
    python crash_monitor.py
=============================================================
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd

try:
    from zoneinfo import ZoneInfo          # Python 3.9+ (stdlib)
    SGT = ZoneInfo("Asia/Singapore")
except Exception:                          # pragma: no cover
    SGT = timezone.utc

import yfinance as yf

# ─────────────────────────────────────────────────────────
# CONFIG (secrets from environment — set in GitHub Secrets)
# ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
HEALTHCHECK_URL    = os.environ.get("HEALTHCHECK_URL", "").strip()   # optional

# DRY_POWDER_SGD may be unset OR passed as an empty string by GitHub Actions
# when the secret doesn't exist — treat both as "use the default".
_dry_powder_raw = os.environ.get("DRY_POWDER_SGD", "").strip()
try:
    TOTAL_DRY_POWDER_SGD = float(_dry_powder_raw) if _dry_powder_raw else 20000.0
except ValueError:
    TOTAL_DRY_POWDER_SGD = 20000.0

# When true, always send the full status summary. When false (the intraday
# price checks), stay silent unless there's a new signal to report — so you
# only get pinged mid-session when something actually triggers.
SEND_SUMMARY = os.environ.get("SEND_SUMMARY", "1").strip().lower() not in ("0", "false", "no", "")

STATE_FILE = "monitor_state.json"

# ─────────────────────────────────────────────────────────
# WHAT WE TRACK
#   ticker = the instrument we price (real ETF, real prices)
#   buy    = what to actually buy (Singapore-tax-efficient note)
# ─────────────────────────────────────────────────────────
# "weight" = share of total dry powder for that sleeve (Core-weighted split).
# URTH and IWDA are the SAME MSCI World sleeve — buy one, not both — so they
# share the 35% and you never double-deploy.
INDICES = {
    "sp500":     {"name": "S&P 500",           "ticker": "SPY",    "buy": "SPY (US) / CSPX (LSE, 15% tax)", "emoji": "🇺🇸", "sleeve": "S&P 500",    "weight": 0.40},
    "nasdaq":    {"name": "Nasdaq 100",        "ticker": "QQQ",    "buy": "QQQ (US) / CNDX (LSE)",          "emoji": "💻", "sleeve": "Nasdaq 100", "weight": 0.25},
    "msci_urth": {"name": "MSCI World (URTH)", "ticker": "URTH",   "buy": "URTH (US, 30% tax)",             "emoji": "🌍", "sleeve": "MSCI World", "weight": 0.35},
    "msci_iwda": {"name": "MSCI World (IWDA)", "ticker": "IWDA.L", "buy": "IWDA (LSE, 15% tax ✓)",          "emoji": "🌐", "sleeve": "MSCI World", "weight": 0.35},
}

# ─────────────────────────────────────────────────────────
# TRANCHES — Mr. Loo's signal-based system
# ─────────────────────────────────────────────────────────
# Loo Cheng Chuan's "Method 1 Allocation Rule" (from his own slide): 5 tranches,
# buying more the deeper it falls. The % is the share of that sleeve's dry
# powder deployed at each level; they sum to 100%.
#   -10% -> 10% | -15% -> 15% | -20% -> 20% | -25% -> 25% | -30% -> 30%
TRANCHES = [
    {"label": "Tranche 1", "drawdown": 10, "powder_pct": 10, "urgency": "Buy (small)"},
    {"label": "Tranche 2", "drawdown": 15, "powder_pct": 15, "urgency": "Buy"},
    {"label": "Tranche 3", "drawdown": 20, "powder_pct": 20, "urgency": "Buy more"},
    {"label": "Tranche 4", "drawdown": 25, "powder_pct": 25, "urgency": "Buy heavy"},
    {"label": "Tranche 5", "drawdown": 30, "powder_pct": 30, "urgency": "Buy heaviest"},
]

RESET_RECOVERY_PCT = 5.0   # when drawdown recovers above this, re-arm all tranches


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
def sgt_now() -> str:
    return datetime.now(SGT).strftime("%d %b %Y, %I:%M %p SGT")


def fmt_sgd(amount: float) -> str:
    return f"SGD ${amount:,.0f}"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] could not read state file, starting fresh: {e}")
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Telegram credentials missing (env vars not set).")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            print("  [Telegram sent OK]")
            return True
        print(f"  [Telegram error] {r.status_code}: {r.text}")
        return False
    except Exception as e:
        print(f"  [Telegram failed] {e}")
        return False


def ping_healthcheck() -> None:
    """Tell healthchecks.io the run finished. If this stops arriving,
    healthchecks.io emails Amelie that the monitor went dark."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=15)
        print("  [Healthcheck pinged]")
    except Exception as e:
        print(f"  [Healthcheck ping failed] {e}")


# ─────────────────────────────────────────────────────────
# PRICE + ATH (one network call per ticker, retried 3x)
# Returns dict or None.
# ─────────────────────────────────────────────────────────
def fetch_quote(ticker: str):
    last_err = None
    for attempt in range(1, 4):
        try:
            hist = yf.Ticker(ticker).history(period="max", interval="1d")
            if hist is None or hist.empty:
                raise ValueError("empty history")

            # Drop rows with no closing price. yfinance sometimes returns a
            # trailing blank bar (common for LSE tickers like IWDA.L) which
            # otherwise shows up as "$nan" in the alert.
            hist = hist.dropna(subset=["Close"])
            if hist.empty:
                raise ValueError("no valid close prices")

            price      = float(hist["Close"].iloc[-1])
            last_dt    = hist.index[-1]
            price_date = last_dt.strftime("%d %b %Y")

            # Day high/low — fall back to the close if that cell is blank.
            _dh = hist["High"].iloc[-1]
            _dl = hist["Low"].iloc[-1]
            day_high = float(_dh) if pd.notna(_dh) else price
            day_low  = float(_dl) if pd.notna(_dl) else price

            # Guard against bad data ticks. Yahoo occasionally reports a
            # spurious intraday High far above the day's close (e.g. IWDA.L
            # showed a fake $161.35 high on 27 Feb 2026, +20% over its close).
            # A broad-market index ETF never gaps >6% between High and Close
            # in one day, so drop such Highs when finding the all-time high.
            # (Real all-time highs are set on calm up-days where High~Close,
            # so this never discards a genuine peak.)
            good = hist[hist["High"] <= hist["Close"] * 1.06]
            ath_src   = good if not good.empty else hist
            ath       = float(ath_src["High"].max())
            ath_date  = ath_src["High"].idxmax().strftime("%d %b %Y")

            return {
                "price": price, "day_high": day_high, "day_low": day_low,
                "price_date": price_date, "ath": ath, "ath_date": ath_date,
            }
        except Exception as e:
            last_err = e
            print(f"  [retry {attempt}/3] {ticker}: {e}")
            time.sleep(2 * attempt)
    print(f"  [FAILED] {ticker}: {last_err}")
    return None


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 55)
    print("  Crash Buy Monitor v5 (GitHub Actions) —", sgt_now())
    print("=" * 55)

    state = load_state()
    now_str = sgt_now()

    summary_lines = [
        "GLOBAL EQUITY DRAWDOWN MONITOR",
        f"Inspirational Sharing | {now_str}",
        "Data via Yahoo Finance (ATH = highest price ever)",
        "──────────────────────────────────────────",
    ]
    signal_blocks = []   # prominent BUY SIGNAL / NEW ATH / RECOVERY messages
    any_failure = False

    for key, index in INDICES.items():
        st = state.get(key, {
            "triggered": [], "peak": 0.0, "peak_date": "",
            "last_price": 0.0, "last_drawdown": 0.0,
        })
        triggered = st.get("triggered", [])

        q = fetch_quote(index["ticker"])
        if q is None:
            any_failure = True
            summary_lines.append(f"{index['emoji']} {index['name']}: ⚠️ price fetch failed (will retry next run)")
            summary_lines.append("──────────────────────────────────────")
            state[key] = st
            continue

        price = q["price"]

        # ATH: use the higher of stored peak vs freshly fetched ATH.
        stored_peak = float(st.get("peak", 0.0) or 0.0)
        peak = max(stored_peak, q["ath"])
        peak_date = st.get("peak_date", "") or q["ath_date"]
        if q["ath"] >= stored_peak:
            peak_date = q["ath_date"]

        # Live new high (today's price above every prior high)
        if price > peak:
            old_peak = peak
            peak = price
            peak_date = q["price_date"]
            signal_blocks.append(
                f"NEW ALL-TIME HIGH — {index['emoji']} {index['name']}\n"
                f"Old ATH: ${old_peak:,.2f}\n"
                f"New ATH: ${price:,.2f} ({q['price_date']})\n"
                f"All drawdown thresholds recalculated from the new high."
            )

        drawdown = ((peak - price) / peak) * 100 if peak else 0.0

        # Recovery reset — re-arm all tranches once we climb back near the top
        if drawdown < RESET_RECOVERY_PCT and len(triggered) > 0:
            triggered = []
            signal_blocks.append(
                f"RECOVERY — {index['emoji']} {index['name']}\n"
                f"Drawdown back to -{drawdown:.1f}%. All tranche signals reset."
            )

        # This sleeve's dry powder (Core-weighted share of the total).
        sleeve_powder = TOTAL_DRY_POWDER_SGD * index["weight"]
        shared_note = ("\nNote: URTH & IWDA share this MSCI World sleeve — buy ONE."
                       if index["sleeve"] == "MSCI World" else "")

        # Check tranches (fire only newly-crossed ones)
        for t in TRANCHES:
            if t["label"] in triggered:
                continue
            if drawdown >= t["drawdown"]:
                deploy = sleeve_powder * (t["powder_pct"] / 100)
                recov  = ((1 / (1 - t["drawdown"] / 100)) - 1) * 100
                trigger_price = peak * (1 - t["drawdown"] / 100)
                signal_blocks.append(
                    f"BUY SIGNAL — {index['emoji']} {index['name']}\n"
                    f"{'='*35}\n"
                    f"{t['label']} triggered at -{t['drawdown']}% drawdown\n"
                    f"Signal time: {now_str}\n\n"
                    f"What to buy: {index['buy']}\n"
                    f"Current price:         ${price:,.2f}\n"
                    f"Today's high:          ${q['day_high']:,.2f}\n"
                    f"Today's low:           ${q['day_low']:,.2f}\n"
                    f"All-time high:         ${peak:,.2f} ({peak_date})\n"
                    f"Trigger level:         ${trigger_price:,.2f}\n"
                    f"Drawdown from ATH:     -{drawdown:.1f}%\n\n"
                    f"Deploy: {fmt_sgd(deploy)}\n"
                    f"({t['powder_pct']}% of your {fmt_sgd(sleeve_powder)} {index['sleeve']} sleeve)\n\n"
                    f"Urgency: {t['urgency']}\n"
                    f"Recovery needed to ATH: +{recov:.0f}%"
                    f"{shared_note}\n"
                    f"{'='*35}\n"
                    f"Inspirational Sharing | Not Financial Advice"
                )
                triggered.append(t["label"])
                print(f"    SIGNAL: {index['name']} {t['label']} at -{drawdown:.1f}%")

        # Zone label + next trigger for the summary
        zone = "Healthy — no signal"
        for t in reversed(TRANCHES):
            if drawdown >= t["drawdown"]:
                zone = f"{t['label']} zone (-{t['drawdown']}%+)"
                break
        next_t = next((t for t in TRANCHES if t["label"] not in triggered), None)
        if next_t:
            nt_price  = peak * (1 - next_t["drawdown"] / 100)
            nt_deploy = sleeve_powder * (next_t["powder_pct"] / 100)
            next_line = (f"  Next: {next_t['label']} at ${nt_price:,.2f} "
                         f"(-{next_t['drawdown']}%) — deploy {fmt_sgd(nt_deploy)}")
        else:
            next_line = "  All tranches deployed"

        summary_lines.append(
            f"{index['emoji']} {index['name']}  ${price:,.2f}  "
            f"ATH ${peak:,.2f}  DD -{drawdown:.2f}%\n"
            f"{next_line}\n"
            f"  Status: {zone}\n"
            f"  Today H/L: ${q['day_high']:,.2f} / ${q['day_low']:,.2f}  "
            f"(as of {q['price_date']})\n"
            f"  ATH date: {peak_date}\n"
            "──────────────────────────────────────"
        )

        # Persist per-index state
        state[key] = {
            "triggered": triggered,
            "peak": peak,
            "peak_date": peak_date,
            "last_price": price,
            "last_drawdown": drawdown,
        }

    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Sleeve-split footer (URTH & IWDA collapse into one MSCI World sleeve)
    sleeves = {}
    for ix in INDICES.values():
        sleeves[ix["sleeve"]] = TOTAL_DRY_POWDER_SGD * ix["weight"]
    split = "  |  ".join(f"{name} {fmt_sgd(amt)}" for name, amt in sleeves.items())
    summary_lines.append(f"Dry powder {fmt_sgd(TOTAL_DRY_POWDER_SGD)}  →  {split}")
    summary_lines.append("(URTH & IWDA share the MSCI World sleeve — buy one)")
    if any_failure:
        summary_lines.append("⚠️ Some prices failed this run — check next run.")
    summary_lines.append("Not Financial Advice")

    # Send prominent signal blocks first (always), then the summary.
    # Intraday checks (SEND_SUMMARY off) stay silent unless a signal fired.
    ok = True
    for block in signal_blocks:
        ok = send_telegram(block) and ok
        time.sleep(1)
    if SEND_SUMMARY:
        ok = send_telegram("\n".join(summary_lines)) and ok
    elif not signal_blocks:
        print("  [intraday check — no new signals, nothing sent]")

    # Only ping the dead-man's-switch if the run genuinely succeeded
    if ok and not any_failure:
        ping_healthcheck()

    print("[Done]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
