"""
Earnings-momentum screen for a U.S. large-cap growth universe.

The live screen combines four things an investment analyst can explain:
  1. EPS surprise consistency (did the company beat, and by how much?)
  2. Consensus EPS revisions (are estimates rising over the last 90 days?)
  3. EPS growth acceleration (is the growth rate itself speeding up?)
  4. A GARP valuation check (forward P/E and PEG; cheaper scores better)

Free Yahoo Finance data only has a *current* snapshot of estimate revisions
and valuation. The backtest therefore uses only historical EPS surprises,
which are known on the announcement date, and never uses data from after
the rebalance date.

This is an educational project, not investment advice.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import yfinance as yf
from sklearn.linear_model import LinearRegression

logging.getLogger("yfinance").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
IMAGES = ROOT / "images"
DB_PATH = DATA / "earnings_screen.db"

# A surprise beyond +/- 25% is already an extreme large-cap beat or miss.
# Bigger percentages in this feed are usually a penny-sized estimate or a
# one-time gain, not a repeatable earnings trend. Cap them.
SURPRISE_CAP = 25.0
MIN_ABS_ESTIMATE = 0.05  # dollars per share; below this, surprise % is unstable
# Year-over-year EPS growth beyond +/- 100% is treated as a base effect
# (a trough, a loss, or a one-time item) and is capped before we measure
# whether growth is accelerating.
YOY_CAP = 1.0
# Same idea for consensus growth rates used in the forward acceleration leg.
FORWARD_GROWTH_CAP = 0.40
# After z-scoring, no factor may vote more than two standard deviations.
# Otherwise one strange acceleration reading can outvote three normal factors.
Z_CAP = 2.0
MIN_FACTORS = 3
BENCHMARK = "IWF"

# Readable palette. Blue = stronger / preferred, bronze = weaker.
NAVY = "#1F4E79"
TEAL = "#1D7874"
GOLD = "#C5922C"
BRONZE = "#8C5A3C"
SLATE = "#4B5563"
LIGHT = "#F4F7FB"
Q_COLORS = ["#8C5A3C", "#C5922C", "#7A8B99", "#1D7874", "#1F4E79"]


def universe_tickers() -> list[str]:
    tickers = pd.read_csv(DATA / "universe.csv")["ticker"].astype(str).str.strip().tolist()
    if len(tickers) != len(set(tickers)):
        raise ValueError("Duplicate tickers in universe.csv")
    return tickers


def _retry(fn, tries: int = 3, pause: float = 1.5):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # network / vendor hiccups are expected
            last = exc
            time.sleep(pause * (i + 1))
    print(f"  giving up: {last}")
    return None


def _flatten_period_frame(df: pd.DataFrame | None, ticker: str) -> pd.DataFrame:
    """Turn a yfinance period-indexed frame into a plain table."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    out = out.reset_index()
    # The period label is the index, sometimes named 'period' and sometimes
    # carried as the first column after reset.
    if "period" not in out.columns:
        out = out.rename(columns={out.columns[0]: "period"})
    out.insert(0, "ticker", ticker)
    return out


def download_raw(force: bool = False) -> None:
    """Download prices, earnings history, and the current estimate snapshot.

    Raw responses are cached as CSV. Re-runs use the cache unless force=True,
    so the project still runs if the vendor changes or is offline.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    tickers = universe_tickers()
    needed = [
        RAW / "prices_daily.csv",
        RAW / "earnings_history.csv",
        RAW / "eps_trend.csv",
        RAW / "eps_revisions.csv",
        RAW / "earnings_estimates.csv",
        RAW / "snapshot.csv",
        RAW / "asof.txt",
    ]
    if not force and all(p.exists() for p in needed):
        print("Using cached raw files in data/raw/")
        return

    print(f"Downloading daily prices for {len(tickers)} stocks + {BENCHMARK}...")
    # One batch is faster and keeps the price date index aligned.
    px = yf.download(
        tickers + [BENCHMARK],
        start="2020-01-01",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="column",
    )
    # yfinance returns a MultiIndex (field, ticker) when several tickers are asked for.
    if isinstance(px.columns, pd.MultiIndex):
        close = px["Close"].copy()
    else:
        close = px[["Close"]].copy()
        close.columns = tickers[:1]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close.sort_index()
    close.to_csv(RAW / "prices_daily.csv")
    print(f"  prices: {close.shape[0]} days x {close.shape[1]} symbols")

    earnings_rows = []
    trend_rows = []
    rev_rows = []
    est_rows = []
    snap_rows = []

    for i, ticker in enumerate(tickers, start=1):
        print(f"[{i:02d}/{len(tickers)}] {ticker}")
        t = yf.Ticker(ticker)

        ed = _retry(lambda: t.earnings_dates)
        if ed is not None and len(ed):
            tmp = ed.copy().reset_index()
            tmp.columns = [str(c).strip().lower().replace(" ", "_").replace("(%)", "pct") for c in tmp.columns]
            # First column is the earnings timestamp.
            ts_col = tmp.columns[0]
            tmp = tmp.rename(columns={ts_col: "earnings_datetime"})
            tmp.insert(0, "ticker", ticker)
            earnings_rows.append(tmp)

        trend_rows.append(_flatten_period_frame(_retry(lambda: t.eps_trend), ticker))
        rev_rows.append(_flatten_period_frame(_retry(lambda: t.eps_revisions), ticker))
        est_rows.append(_flatten_period_frame(_retry(lambda: t.earnings_estimate), ticker))

        info = _retry(lambda: t.info) or {}
        snap_rows.append(
            {
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),
                "forward_pe": info.get("forwardPE"),
                "trailing_pe": info.get("trailingPE"),
                "peg_ratio": info.get("pegRatio"),
            }
        )
        time.sleep(0.25)

    def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        frames = [f for f in frames if f is not None and len(f)]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    _concat(earnings_rows).to_csv(RAW / "earnings_history.csv", index=False)
    _concat(trend_rows).to_csv(RAW / "eps_trend.csv", index=False)
    _concat(rev_rows).to_csv(RAW / "eps_revisions.csv", index=False)
    _concat(est_rows).to_csv(RAW / "earnings_estimates.csv", index=False)
    pd.DataFrame(snap_rows).to_csv(RAW / "snapshot.csv", index=False)
    asof = pd.Timestamp.today().strftime("%Y-%m-%d")
    (RAW / "asof.txt").write_text(asof + "\n")
    print("Raw cache written.")


def load_prices() -> pd.DataFrame:
    px = pd.read_csv(RAW / "prices_daily.csv", index_col=0, parse_dates=True)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    return px.sort_index().apply(pd.to_numeric, errors="coerce")


def load_snapshot() -> pd.DataFrame:
    snap = pd.read_csv(RAW / "snapshot.csv")
    for col in ["market_cap", "forward_pe", "trailing_pe", "peg_ratio"]:
        snap[col] = pd.to_numeric(snap[col], errors="coerce")
    snap["sector"] = snap["sector"].fillna("Unknown")
    snap["name"] = snap["name"].fillna(snap["ticker"])
    return snap


def limit_to_universe(earnings: pd.DataFrame, snap: pd.DataFrame, daily: pd.DataFrame):
    """Keep the study universe (plus the IWF benchmark in the price file)."""
    tickers = set(universe_tickers())
    earnings = earnings.loc[earnings["ticker"].isin(tickers)].copy()
    snap = snap.loc[snap["ticker"].isin(tickers)].copy()
    keep = [c for c in daily.columns if c in tickers or c == BENCHMARK]
    return earnings, snap, daily[keep].copy()


def asof_date() -> str:
    return (RAW / "asof.txt").read_text().strip()


def _naive_local(series: pd.Series) -> pd.Series:
    """Earnings timestamps -> America/New_York clock time, timezone-naive."""
    ts = pd.to_datetime(series, utc=True, errors="coerce")
    local = ts.dt.tz_convert("America/New_York").dt.tz_localize(None)
    return local


def clean_earnings(path: Path | None = None) -> pd.DataFrame:
    """Keep reported quarters with a usable consensus estimate.

    `available_date` is the first regular session that could have traded on
    the news. A print at or after 4:00 p.m. New York time is treated as
    after the close, so it becomes known on the next business day. That
    stops the backtest from using a surprise before the market had it.
    """
    raw = pd.read_csv(path or (RAW / "earnings_history.csv"))
    df = raw.copy()
    # Vendor column names vary slightly in punctuation; normalize.
    df.columns = [c.strip().lower() for c in df.columns]
    rename = {}
    for c in df.columns:
        if "estimate" in c and "eps" in c:
            rename[c] = "eps_estimate"
        elif "reported" in c:
            rename[c] = "reported_eps"
        elif "surprise" in c:
            rename[c] = "surprise_vendor"
        elif "datetime" in c or c in {"earnings date", "earnings_date"}:
            rename[c] = "earnings_datetime"
    df = df.rename(columns=rename)
    df["eps_estimate"] = pd.to_numeric(df["eps_estimate"], errors="coerce")
    df["reported_eps"] = pd.to_numeric(df["reported_eps"], errors="coerce")
    df["surprise_vendor"] = pd.to_numeric(df.get("surprise_vendor"), errors="coerce")
    df["earnings_datetime"] = _naive_local(df["earnings_datetime"])
    df = df.dropna(subset=["earnings_datetime", "ticker"])

    # Unpublished future dates have no reported EPS. They are not history.
    df = df.dropna(subset=["reported_eps"]).copy()
    df["report_date"] = df["earnings_datetime"].dt.normalize()
    # A few vendor histories ship an old fragment, then a multi-year gap
    # (BlackRock has 2008-2009 and then resumes in 2022). Drop pre-2019
    # prints. Year-over-year math below also matches on the calendar, so a
    # remaining gap cannot be treated as "four quarters ago."
    df = df.loc[df["report_date"] >= "2019-01-01"].copy()
    after_close = df["earnings_datetime"].dt.hour >= 16
    df["available_date"] = df["report_date"]
    nxt = df.loc[after_close, "report_date"].map(
        lambda d: pd.Timestamp(np.busday_offset(np.datetime64(d.date()), 1, roll="forward"))
    )
    df.loc[after_close, "available_date"] = nxt.values

    # Positive, non-tiny estimates only. Negative bases make "surprise %"
    # point the wrong way, and penny estimates explode the percentage.
    usable = df["eps_estimate"].notna() & (df["eps_estimate"] >= MIN_ABS_ESTIMATE)
    df["surprise_pct"] = np.where(
        usable,
        (df["reported_eps"] - df["eps_estimate"]) / df["eps_estimate"].abs() * 100.0,
        np.nan,
    )
    df["surprise_capped"] = df["surprise_pct"].clip(-SURPRISE_CAP, SURPRISE_CAP)
    df["estimate_usable"] = usable & df["surprise_pct"].notna()
    df = df.sort_values(["ticker", "available_date", "earnings_datetime"])
    df = df.drop_duplicates(["ticker", "report_date"], keep="last")
    keep = [
        "ticker",
        "earnings_datetime",
        "report_date",
        "available_date",
        "eps_estimate",
        "reported_eps",
        "surprise_vendor",
        "surprise_pct",
        "surprise_capped",
        "estimate_usable",
    ]
    return df[keep].reset_index(drop=True)


def month_end_prices(daily: pd.DataFrame) -> pd.DataFrame:
    """Last available close in each completed month.

    The current month is dropped when we do not yet have a price near
    month-end, so an October 8 download cannot pretend October is finished.
    """
    px = daily.resample("ME").last()
    last_trade = daily.dropna(how="all").index.max()
    month_end = last_trade + pd.offsets.MonthEnd(0)
    if (month_end - last_trade).days > 4:
        px = px.loc[px.index < month_end]
    # Drop a month only if the benchmark (or the whole row) is missing.
    if BENCHMARK in px.columns:
        px = px.loc[px[BENCHMARK].notna()]
    return px


def _winsorize(s: pd.Series, p: float = 0.05) -> pd.Series:
    x = s.astype(float)
    if x.notna().sum() < 10:
        return x
    lo, hi = x.quantile(p), x.quantile(1 - p)
    return x.clip(lo, hi)


def _zscore(s: pd.Series) -> pd.Series:
    x = s.astype(float)
    sd = x.std(skipna=True, ddof=0)
    if sd is None or not np.isfinite(sd) or sd == 0:
        return pd.Series(np.nan, index=s.index)
    return (x - x.mean(skipna=True)) / sd


def _combine_z(parts: list[pd.Series]) -> pd.Series:
    """Average the available ingredient z-scores, then re-standardize.

    Re-standardizing puts every factor on the same scale before the
    equal-weight composite. A factor that is itself an average of two
    ingredients would otherwise have a smaller standard deviation and a
    quieter vote.
    """
    stacked = pd.concat(parts, axis=1)
    raw = stacked.mean(axis=1, skipna=True)
    raw = raw.where(stacked.notna().any(axis=1))
    return _zscore(raw)


def _revision_pct(current: pd.Series, past: pd.Series) -> pd.Series:
    past_ok = past.where(past.abs() >= MIN_ABS_ESTIMATE)
    return (current - past_ok) / past_ok.abs() * 100.0


def build_current_factors(earnings: pd.DataFrame, snap: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time *current* factors. Revisions and valuation are snapshots."""
    hist = earnings.loc[earnings["estimate_usable"]].copy()
    # Acceleration uses every reported quarter, in order. Filtering to
    # "usable estimates" would drop a quarter and break the four-quarter lag.
    reported = (
        earnings.dropna(subset=["reported_eps"])
        .sort_values(["ticker", "report_date"])
        .drop_duplicates(["ticker", "report_date"], keep="last")
    )
    accel_map = {
        ticker: _realized_acceleration(g) for ticker, g in reported.groupby("ticker")
    }
    rows = []
    for ticker, g in hist.groupby("ticker"):
        g = g.sort_values("available_date")
        last8 = g.tail(8)
        if len(last8) < 4:
            avg8 = beat8 = np.nan
            n_q = int(len(last8))
        else:
            avg8 = float(last8["surprise_capped"].mean())
            beat8 = float((last8["surprise_capped"] > 0).mean())
            n_q = int(len(last8))
        rows.append(
            {
                "ticker": ticker,
                "n_surprise_q": n_q,
                "avg_surprise_8q": avg8,
                "beat_rate_8q": beat8,
                "realized_accel": accel_map.get(ticker, np.nan),
                "last_report": g["report_date"].max(),
            }
        )
    surprise = pd.DataFrame(rows)
    have = set(surprise["ticker"]) if len(surprise) else set()
    extra_accel = [
        {
            "ticker": ticker,
            "n_surprise_q": 0,
            "avg_surprise_8q": np.nan,
            "beat_rate_8q": np.nan,
            "realized_accel": val,
            "last_report": pd.NaT,
        }
        for ticker, val in accel_map.items()
        if ticker not in have
    ]
    if extra_accel:
        surprise = pd.concat([surprise, pd.DataFrame(extra_accel)], ignore_index=True)

    trend = pd.read_csv(RAW / "eps_trend.csv")
    trend.columns = [c.strip().lower() for c in trend.columns]
    revs = pd.read_csv(RAW / "eps_revisions.csv")
    revs.columns = [c.strip().lower() for c in revs.columns]
    est = pd.read_csv(RAW / "earnings_estimates.csv")
    est.columns = [c.strip().lower() for c in est.columns]

    for frame in (trend, revs, est):
        for c in frame.columns:
            if c not in {"ticker", "period", "currency"}:
                frame[c] = pd.to_numeric(frame[c], errors="coerce")

    annual = trend[trend["period"].isin(["0y", "+1y"])].copy()
    annual["rev90"] = _revision_pct(annual["current"], annual["90daysago"])
    annual["rev30"] = _revision_pct(annual["current"], annual["30daysago"])
    rev_wide = annual.groupby("ticker")[["rev90", "rev30"]].mean()

    breadth_src = revs[revs["period"] == "0y"].set_index("ticker")
    # Column spelling from the vendor is inconsistent (downLast7Days).
    up30 = breadth_src.get("uplast30days")
    down30 = breadth_src.get("downlast30days")
    est_0y = est[est["period"] == "0y"].set_index("ticker")
    n_analysts = est_0y["numberofanalysts"]
    breadth = (up30 - down30) / n_analysts.replace(0, np.nan)

    growth = est[est["period"].isin(["0y", "+1y"])].pivot(index="ticker", columns="period", values="growth")
    avg_eps = est[est["period"].isin(["0y", "+1y"])].pivot(index="ticker", columns="period", values="avg")
    # Cap each consensus growth rate before subtracting. A one-time gain can
    # make "this year" look like +90% and "next year" like a decline; capping
    # stops that gap from becoming a triple-digit acceleration score.
    g0 = growth["0y"].clip(-FORWARD_GROWTH_CAP, FORWARD_GROWTH_CAP)
    g1 = growth["+1y"].clip(-FORWARD_GROWTH_CAP, FORWARD_GROWTH_CAP)
    fwd_accel = (g1 - g0) * 100.0  # percentage points
    bad_base = (avg_eps["0y"] <= 0) | (avg_eps["+1y"] <= 0)
    fwd_accel = fwd_accel.mask(bad_base)
    fwd_growth = growth["+1y"] * 100.0  # next-year expected growth, for the memo only

    factors = snap.merge(surprise, on="ticker", how="left")
    factors = factors.merge(rev_wide, left_on="ticker", right_index=True, how="left")
    factors = factors.merge(breadth.rename("breadth_30d"), left_on="ticker", right_index=True, how="left")
    factors = factors.merge(fwd_accel.rename("forward_accel"), left_on="ticker", right_index=True, how="left")
    factors = factors.merge(fwd_growth.rename("forward_growth"), left_on="ticker", right_index=True, how="left")
    factors = factors.merge(avg_eps["0y"].rename("fy1_eps"), left_on="ticker", right_index=True, how="left")
    factors = factors.merge(avg_eps["+1y"].rename("fy2_eps"), left_on="ticker", right_index=True, how="left")
    factors = factors.merge(
        est_0y["numberofanalysts"].rename("n_analysts_fy0"),
        left_on="ticker",
        right_index=True,
        how="left",
    )
    return factors


def _realized_acceleration(g: pd.DataFrame) -> float:
    """Change in year-over-year reported EPS growth.

    For each quarter, the base is the print about a year earlier (300 to 450
    days before), not "four rows up." That way a gap in the history cannot
    line 2022 up with 2009. Growth rates are capped at +/- 100% before the
    comparison, so a trough-to-peak cycle cannot print a +600 point jump.

    Acceleration, in percentage points, is the average of the last two YoY
    rates minus the average of the two before that.
    """
    g = g.dropna(subset=["reported_eps", "report_date"]).sort_values("report_date")
    if len(g) < 6:
        return np.nan
    dates = list(pd.to_datetime(g["report_date"]))
    eps = g["reported_eps"].astype(float).tolist()
    yoy: list[float] = []
    for i in range(len(dates)):
        best = None
        best_gap = 10**9
        for j in range(i):
            delta = (dates[i] - dates[j]).days
            if 300 <= delta <= 450 and abs(delta - 365) < best_gap:
                best_gap = abs(delta - 365)
                best = j
        if best is None:
            continue
        base = eps[best]
        if not np.isfinite(base) or base < MIN_ABS_ESTIMATE:
            continue
        growth = float(np.clip((eps[i] - base) / base, -YOY_CAP, YOY_CAP))
        yoy.append(growth)
    if len(yoy) < 4:
        return np.nan
    recent = float(np.mean(yoy[-2:]))
    prior = float(np.mean(yoy[-4:-2]))
    return (recent - prior) * 100.0


def score_factors(factors: pd.DataFrame) -> pd.DataFrame:
    """Winsorize, z-score, and equal-weight the four factors. Rank 1 is best."""
    f = factors.copy()
    f["avg_surprise_w"] = _winsorize(f["avg_surprise_8q"])
    f["rev90_w"] = _winsorize(f["rev90"])
    f["breadth_w"] = _winsorize(f["breadth_30d"])
    f["realized_w"] = _winsorize(f["realized_accel"])
    f["forward_w"] = _winsorize(f["forward_accel"])
    # Valuation: keep only economically sensible positive multiples.
    # A negative PEG is not "cheap"; it usually means negative expected growth.
    f["forward_pe_use"] = f["forward_pe"].where((f["forward_pe"] > 0) & (f["forward_pe"] < 250))
    f["peg_use"] = f["peg_ratio"].where((f["peg_ratio"] > 0) & (f["peg_ratio"] < 10))
    f["forward_pe_w"] = _winsorize(f["forward_pe_use"])
    f["peg_w"] = _winsorize(f["peg_use"])

    surprise_z = _combine_z([_zscore(f["avg_surprise_w"]), _zscore(f["beat_rate_8q"])]).clip(-Z_CAP, Z_CAP)
    revision_z = _combine_z([_zscore(f["rev90_w"]), _zscore(f["breadth_w"])]).clip(-Z_CAP, Z_CAP)
    accel_z = _combine_z([_zscore(f["realized_w"]), _zscore(f["forward_w"])]).clip(-Z_CAP, Z_CAP)
    # Lower multiple = more attractive, so the sign is flipped before the z-score.
    value_z = _combine_z([_zscore(-f["forward_pe_w"]), _zscore(-f["peg_w"])]).clip(-Z_CAP, Z_CAP)

    f["surprise_z"] = surprise_z
    f["revision_z"] = revision_z
    f["accel_z"] = accel_z
    f["value_z"] = value_z
    zcols = ["surprise_z", "revision_z", "accel_z", "value_z"]
    f["n_factors"] = f[zcols].notna().sum(axis=1)
    f["composite"] = f[zcols].mean(axis=1, skipna=True)
    f.loc[f["n_factors"] < MIN_FACTORS, "composite"] = np.nan
    # Re-standardize the composite across names that qualified, for readability.
    # Rank uses this value; the level is "standard deviations above the peer average."
    qualified = f["composite"].notna()
    f.loc[qualified, "composite"] = _zscore(f.loc[qualified, "composite"])
    f["rank"] = f["composite"].rank(ascending=False, method="min")
    # 100 = best composite in the universe. Handy next to the z-score.
    f["peer_percentile"] = f["composite"].rank(ascending=True, method="average", pct=True) * 100.0
    f = f.sort_values(["rank", "ticker"], na_position="last").reset_index(drop=True)
    return f


def _signal_asof(earnings: pd.DataFrame, asof: pd.Timestamp, window: int) -> pd.DataFrame:
    """Trailing surprise stats using only rows available on or before `asof`."""
    hist = earnings.loc[
        earnings["estimate_usable"] & (earnings["available_date"] <= asof),
        ["ticker", "available_date", "surprise_capped", "reported_eps"],
    ]
    rows = []
    for ticker, g in hist.groupby("ticker"):
        g = g.sort_values("available_date")
        last = g.tail(window)
        if len(last) < window:
            continue
        rows.append(
            {
                "ticker": ticker,
                "signal": last["surprise_capped"].mean(),
                "beat_rate": (last["surprise_capped"] > 0).mean(),
                "max_available_date": g["available_date"].max(),
                "n_quarters": int(len(last)),
            }
        )
    return pd.DataFrame(rows)


def _price_on(px_m: pd.DataFrame, date: pd.Timestamp) -> pd.Series:
    if date not in px_m.index:
        raise KeyError(date)
    return px_m.loc[date]


def build_surprise_panel(earnings: pd.DataFrame, px_m: pd.DataFrame, window: int = 4) -> pd.DataFrame:
    """Quarter-end panel. Signal at T uses surprises known by T. Return is T -> T+1 quarter.

    Quintile 5 is the highest trailing surprise (best). Quintile 1 is the lowest.
    """
    quarter_ends = pd.date_range("2021-03-31", px_m.index.max(), freq="QE")
    quarter_ends = [d for d in quarter_ends if d in px_m.index]
    members = []
    for i, start in enumerate(quarter_ends[:-1]):
        end = quarter_ends[i + 1]
        # Only consecutive quarter-ends (about 90 days). A missing month would
        # otherwise glue two periods together and mis-state the holding period.
        if (end - start).days > 100:
            continue
        sig = _signal_asof(earnings, start, window)
        if len(sig) < 30:
            continue
        p0 = _price_on(px_m, start)
        p1 = _price_on(px_m, end)
        sig = sig[sig["ticker"].isin(p0.index) & sig["ticker"].isin(p1.index)].copy()
        sig = sig.dropna(subset=["ticker"])
        both = sig["ticker"].map(lambda t: pd.notna(p0.get(t)) and pd.notna(p1.get(t)))
        sig = sig.loc[both.values].copy()
        if len(sig) < 30:
            continue
        sig["fwd_return"] = sig["ticker"].map(lambda t: p1[t] / p0[t] - 1.0)
        sig = sig.dropna(subset=["fwd_return", "signal"])
        if len(sig) < 30:
            continue
        # rank-then-cut so ties cannot collapse a bin
        order = sig["signal"].rank(method="first", ascending=True)
        sig["quintile"] = pd.qcut(order, 5, labels=[1, 2, 3, 4, 5]).astype(int)
        sig["signal_z"] = _zscore(sig["signal"])
        sig["rebalance_date"] = start
        sig["next_date"] = end
        # Look-ahead guard: every print in the signal was available by the rebalance.
        if (sig["max_available_date"] > start).any():
            raise RuntimeError("Look-ahead: an earnings date is after the rebalance.")
        members.append(sig)
    if not members:
        raise RuntimeError("No backtest dates had enough data.")
    panel = pd.concat(members, ignore_index=True)
    return panel


def _holding_period_path(px_m: pd.DataFrame, tickers: list[str], start, end) -> pd.Series:
    """Monthly portfolio returns from start to end with weights drifting between rebalances.

    Weights start equal on `start`. They are not reset each month. The compound
    of this path equals the equal-weight average of stock returns over the quarter.
    """
    months = px_m.index[(px_m.index >= start) & (px_m.index <= end)]
    cols = [t for t in tickers if t in px_m.columns]
    px = px_m.loc[months, cols].dropna(axis=1, how="any")
    if px.shape[1] == 0 or len(px) < 2:
        return pd.Series(dtype=float)
    rets = px.pct_change().iloc[1:]
    w = pd.Series(1.0 / px.shape[1], index=px.columns)
    out = {}
    for dt, row in rets.iterrows():
        r = row.astype(float)
        out[dt] = float((w * r).sum())
        w = w * (1.0 + r)
        w = w / w.sum()
    return pd.Series(out)


def portfolio_returns(panel: pd.DataFrame, px_m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build monthly and quarterly return series for quintiles, the universe, and IWF."""
    monthly_rows = []
    quarterly_rows = []
    dates = sorted(panel["rebalance_date"].unique())
    for start in dates:
        block = panel.loc[panel["rebalance_date"] == start]
        end = block["next_date"].iloc[0]
        groups = {f"Q{q}": block.loc[block["quintile"] == q, "ticker"].tolist() for q in range(1, 6)}
        groups["Equal-weight universe"] = block["ticker"].tolist()
        for name, tickers in groups.items():
            path = _holding_period_path(px_m, tickers, start, end)
            for dt, r in path.items():
                monthly_rows.append({"date": dt, "portfolio": name, "return": r})
            if len(path):
                qret = float((1.0 + path).prod() - 1.0)
            else:
                qret = np.nan
            # Cross-check against the simple equal-weight of quarterly stock returns.
            if name.startswith("Q") or name.startswith("Equal"):
                simple = block.loc[block["ticker"].isin(tickers), "fwd_return"]
                if name.startswith("Q"):
                    q = int(name[1])
                    simple = block.loc[block["quintile"] == q, "fwd_return"]
                if len(simple) and np.isfinite(qret) and abs(qret - simple.mean()) > 1e-8:
                    raise RuntimeError(f"Return identity failed for {name} on {start.date()}")
            quarterly_rows.append(
                {
                    "rebalance_date": start,
                    "next_date": end,
                    "portfolio": name,
                    "quarter_return": qret,
                    "n_names": len(tickers),
                }
            )
        # Benchmark: buy and hold the ETF over the same quarter.
        if BENCHMARK in px_m.columns and pd.notna(px_m.loc[start, BENCHMARK]) and pd.notna(px_m.loc[end, BENCHMARK]):
            iwf_q = float(px_m.loc[end, BENCHMARK] / px_m.loc[start, BENCHMARK] - 1.0)
            months = px_m.index[(px_m.index > start) & (px_m.index <= end)]
            prev = start
            for dt in months:
                r = float(px_m.loc[dt, BENCHMARK] / px_m.loc[prev, BENCHMARK] - 1.0)
                monthly_rows.append({"date": dt, "portfolio": "IWF", "return": r})
                prev = dt
            quarterly_rows.append(
                {
                    "rebalance_date": start,
                    "next_date": end,
                    "portfolio": "IWF",
                    "quarter_return": iwf_q,
                    "n_names": 1,
                }
            )
    monthly = pd.DataFrame(monthly_rows)
    quarterly = pd.DataFrame(quarterly_rows)
    return monthly, quarterly


def performance_stats(monthly: pd.DataFrame, quarterly: pd.DataFrame) -> pd.DataFrame:
    """CAGR, volatility, Sharpe with rf = 0, max drawdown, and hit rates.

    Sharpe uses the arithmetic mean of monthly returns divided by their
    standard deviation, annualized with sqrt(12). The risk-free rate is
    zero, so this is a return/risk ratio, not a true excess-return Sharpe.
    Max drawdown is computed on the monthly wealth curve.
    """
    rows = []
    order = ["Q5", "Q4", "Q3", "Q2", "Q1", "Equal-weight universe", "IWF"]
    q_wide = quarterly.pivot(index="rebalance_date", columns="portfolio", values="quarter_return")
    for name in order:
        r = monthly.loc[monthly["portfolio"] == name, ["date", "return"]].dropna()
        r = r.sort_values("date")
        if r.empty:
            continue
        wealth = (1.0 + r["return"]).cumprod()
        n = len(r)
        cagr = float(wealth.iloc[-1] ** (12.0 / n) - 1.0)
        vol = float(r["return"].std(ddof=1) * np.sqrt(12.0))
        sharpe = float(r["return"].mean() / r["return"].std(ddof=1) * np.sqrt(12.0))
        dd = wealth / wealth.cummax() - 1.0
        maxdd = float(dd.min())
        q = q_wide[name].dropna() if name in q_wide.columns else pd.Series(dtype=float)
        hit_pos = float((q > 0).mean()) if len(q) else np.nan
        if name not in {"Equal-weight universe", "IWF"} and "Equal-weight universe" in q_wide and "IWF" in q_wide:
            aligned = pd.concat(
                [q.rename("p"), q_wide["Equal-weight universe"].rename("ew"), q_wide["IWF"].rename("iwf")],
                axis=1,
            ).dropna()
            hit_ew = float((aligned["p"] > aligned["ew"]).mean()) if len(aligned) else np.nan
            hit_iwf = float((aligned["p"] > aligned["iwf"]).mean()) if len(aligned) else np.nan
        elif name == "Equal-weight universe" and "IWF" in q_wide:
            aligned = pd.concat([q.rename("p"), q_wide["IWF"].rename("iwf")], axis=1).dropna()
            hit_ew = np.nan
            hit_iwf = float((aligned["p"] > aligned["iwf"]).mean()) if len(aligned) else np.nan
        else:
            hit_ew = np.nan
            hit_iwf = np.nan
        rows.append(
            {
                "portfolio": name,
                "months": n,
                "quarters": int(q.notna().sum()),
                "cagr": cagr,
                "ann_vol": vol,
                "sharpe_rf0": sharpe,
                "max_drawdown": maxdd,
                "hit_rate_positive": hit_pos,
                "hit_rate_vs_equal_weight": hit_ew,
                "hit_rate_vs_iwf": hit_iwf,
                "avg_quarter_return": float(q.mean()) if len(q) else np.nan,
                "total_return": float(wealth.iloc[-1] - 1.0),
            }
        )
    return pd.DataFrame(rows)


def sklearn_signal_check(panel: pd.DataFrame) -> dict:
    """Does a higher surprise z-score line up with a higher next-quarter excess return?

    Excess return is the stock's quarter return minus the equal-weight universe
    that same quarter, so the market's up and down months do not do the work.

    The sample is split by time, not at random: fit on the earlier rebalance
    dates, then score the later ones. A random split would leak the future
    into the fit because the same market regime would show up on both sides.
    """
    df = panel[["rebalance_date", "ticker", "signal_z", "fwd_return", "quintile"]].dropna().copy()
    df["excess"] = df["fwd_return"] - df.groupby("rebalance_date")["fwd_return"].transform("mean")
    dates = sorted(df["rebalance_date"].unique())
    cut = dates[len(dates) // 2]
    train = df[df["rebalance_date"] < cut]
    test = df[df["rebalance_date"] >= cut]
    model = LinearRegression()
    model.fit(train[["signal_z"]], train["excess"])
    pred = model.predict(test[["signal_z"]])
    ss_res = float(np.sum((test["excess"].to_numpy() - pred) ** 2))
    ss_tot = float(np.sum((test["excess"] - test["excess"].mean()) ** 2))
    r2_test = np.nan if ss_tot == 0 else 1.0 - ss_res / ss_tot

    full = LinearRegression()
    full.fit(df[["signal_z"]], df["excess"])

    def _spread(part: pd.DataFrame) -> float:
        q5 = part.loc[part["quintile"] == 5, "excess"].mean()
        q1 = part.loc[part["quintile"] == 1, "excess"].mean()
        return float(q5 - q1)

    return {
        "train_end_exclusive": str(pd.Timestamp(cut).date()),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "coef_train_excess_per_z": float(model.coef_[0]),
        "intercept_train": float(model.intercept_),
        "r2_test": float(r2_test),
        "coef_full_sample": float(full.coef_[0]),
        "q5_minus_q1_excess_first_half": _spread(train),
        "q5_minus_q1_excess_second_half": _spread(test),
        "spearman_signal_excess": float(df["signal_z"].corr(df["excess"], method="spearman")),
    }


def spread_tstat(quarterly: pd.DataFrame) -> dict:
    wide = quarterly.pivot(index="rebalance_date", columns="portfolio", values="quarter_return")
    spread = (wide["Q5"] - wide["Q1"]).dropna()
    n = len(spread)
    mean = float(spread.mean())
    sd = float(spread.std(ddof=1))
    tstat = float(mean / (sd / np.sqrt(n))) if n > 1 and sd > 0 else np.nan
    return {
        "n_quarters": n,
        "mean_q5_minus_q1": mean,
        "tstat_descriptive": tstat,
        "first_rebalance": str(pd.Timestamp(spread.index.min()).date()),
        "last_rebalance": str(pd.Timestamp(spread.index.max()).date()),
    }


def save_database(
    earnings: pd.DataFrame,
    px_m: pd.DataFrame,
    snap: pd.DataFrame,
    scores: pd.DataFrame,
    panel: pd.DataFrame,
    quarterly: pd.DataFrame,
) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        universe = snap[["ticker", "name", "sector", "industry", "market_cap"]].copy()
        universe.to_sql("universe", conn, index=False)
        earnings.to_sql("earnings", conn, index=False)
        long_px = (
            px_m.reset_index(names="month_end")
            .melt(id_vars="month_end", var_name="ticker", value_name="adj_close")
            .dropna()
        )
        long_px.to_sql("prices_monthly", conn, index=False)
        score_cols = [
            "rank",
            "ticker",
            "composite",
            "n_factors",
            "surprise_z",
            "revision_z",
            "accel_z",
            "value_z",
            "avg_surprise_8q",
            "beat_rate_8q",
            "rev90",
            "rev30",
            "breadth_30d",
            "realized_accel",
            "forward_accel",
            "forward_growth",
            "forward_pe",
            "peg_ratio",
            "trailing_pe",
            "market_cap",
            "price",
            "fy1_eps",
            "fy2_eps",
            "peer_percentile",
            "n_analysts_fy0",
            "sector",
            "name",
        ]
        scores[score_cols].to_sql("scores", conn, index=False)
        panel.to_sql("backtest_members", conn, index=False)
        quarterly.to_sql("backtest_quarterly", conn, index=False)
        conn.executescript(
            """
            CREATE INDEX idx_earnings_ticker_date ON earnings(ticker, available_date);
            CREATE INDEX idx_prices_ticker_month ON prices_monthly(ticker, month_end);
            CREATE INDEX idx_scores_rank ON scores(rank);
            CREATE INDEX idx_members_date ON backtest_members(rebalance_date, quintile);
            """
        )
    finally:
        conn.close()


def run_sql(sql: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(sql, conn)


def _style_ax(ax, title: str, xlabel: str, ylabel: str):
    ax.set_title(title, loc="left", fontsize=13, color="#1A1A1A", pad=10)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.figure.tight_layout()


def make_charts(scores: pd.DataFrame, quarterly: pd.DataFrame, monthly: pd.DataFrame) -> dict[str, Path]:
    IMAGES.mkdir(parents=True, exist_ok=True)
    sns.set_theme(style="whitegrid", context="notebook")
    plt.rcParams.update(
        {
            "axes.edgecolor": "#D0D5DD",
            "grid.color": "#E6E8EC",
            "font.size": 11,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "text.color": "#1A1A1A",
            "axes.labelcolor": "#1A1A1A",
        }
    )
    paths = {}
    ranked = scores.dropna(subset=["rank"]).sort_values("rank")
    sample_start = pd.Timestamp(quarterly["rebalance_date"].min()).strftime("%b %Y")
    sample_end = pd.Timestamp(quarterly["next_date"].max()).strftime("%b %Y")

    # 1. Top 15 composite scores
    top = ranked.nsmallest(15, "rank").iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, 6.2))
    colors = [TEAL if z >= 0 else BRONZE for z in top["composite"]]
    ax.barh(top["ticker"], top["composite"], color=colors, height=0.72)
    ax.axvline(0, color=SLATE, lw=0.8)
    _style_ax(
        ax,
        "Top 15 composite scores",
        "Composite z-score (0 = peer average among names with a score)",
        "",
    )
    fig.savefig(IMAGES / "01_composite_ranking.png", dpi=150, bbox_inches="tight")
    paths["ranking"] = IMAGES / "01_composite_ranking.png"
    plt.close(fig)

    # 2. Factor heatmap
    heat_n = ranked.nsmallest(15, "rank")
    heat = heat_n.set_index("ticker")[["surprise_z", "revision_z", "accel_z", "value_z"]]
    heat = heat.rename(
        columns={
            "surprise_z": "Surprise",
            "revision_z": "Revisions",
            "accel_z": "Acceleration",
            "value_z": "Valuation",
        }
    )
    fig, ax = plt.subplots(figsize=(9.2, 6.4))
    sns.heatmap(
        heat,
        cmap="RdBu",
        center=0,
        annot=True,
        fmt=".2f",
        linewidths=0.4,
        linecolor="white",
        cbar_kws={"label": "Factor z-score"},
        ax=ax,
    )
    ax.set_title("What drives the top 15", loc="left", fontsize=13, pad=10)
    ax.set_xlabel("")
    ax.set_ylabel("")
    fig.tight_layout()
    fig.savefig(IMAGES / "02_factor_heatmap.png", dpi=150, bbox_inches="tight")
    paths["heatmap"] = IMAGES / "02_factor_heatmap.png"
    plt.close(fig)

    # 3. Momentum vs valuation
    sc = ranked.dropna(subset=["composite"]).copy()
    sc["earnings_z"] = sc[["surprise_z", "revision_z", "accel_z"]].mean(axis=1, skipna=True)
    fig, ax = plt.subplots(figsize=(10, 6.4))
    plot_df = sc.dropna(subset=["earnings_z", "forward_pe"])
    plot_df = plot_df[(plot_df["forward_pe"] > 0) & (plot_df["forward_pe"] < 120)]
    left_out = sc.loc[sc["forward_pe"] >= 120, "ticker"].tolist()
    ax.scatter(
        plot_df["earnings_z"],
        plot_df["forward_pe"],
        c=plot_df["composite"],
        cmap="RdBu",
        s=46,
        alpha=0.9,
        edgecolor="white",
        linewidth=0.4,
    )
    label_set = set(ranked.nsmallest(8, "rank")["ticker"])
    for _, row in plot_df.iterrows():
        if row["ticker"] in label_set:
            ax.annotate(row["ticker"], (row["earnings_z"], row["forward_pe"]), fontsize=8, color=NAVY, xytext=(4, 4), textcoords="offset points")
    if left_out:
        ax.text(
            0.99,
            0.98,
            "Not shown (forward P/E above 120): " + ", ".join(left_out),
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            color=SLATE,
        )
    _style_ax(
        ax,
        "Earnings momentum versus forward P/E",
        "Earnings-factor z-score (surprise, revisions, acceleration)",
        "Forward P/E",
    )
    fig.savefig(IMAGES / "03_momentum_vs_valuation.png", dpi=150, bbox_inches="tight")
    paths["scatter"] = IMAGES / "03_momentum_vs_valuation.png"
    plt.close(fig)

    # 4. Average next-quarter return by quintile
    q_order = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    avg = (
        quarterly[quarterly["portfolio"].isin(q_order)]
        .groupby("portfolio")["quarter_return"]
        .mean()
        .reindex(q_order)
    )
    ew = quarterly.loc[quarterly["portfolio"] == "Equal-weight universe", "quarter_return"].mean()
    iwf = quarterly.loc[quarterly["portfolio"] == "IWF", "quarter_return"].mean()
    fig, ax = plt.subplots(figsize=(9.4, 5.6))
    ax.bar(q_order, avg.values * 100, color=Q_COLORS, width=0.72)
    for i, v in enumerate(avg.values):
        ax.text(i, v * 100 + 0.18, f"{v * 100:.1f}", ha="center", va="bottom", fontsize=9, color=SLATE)
    ax.axhline(ew * 100, color=TEAL, ls="--", lw=1.2, label=f"Equal-weight universe ({ew*100:.2f}%)")
    ax.axhline(iwf * 100, color=GOLD, ls=":", lw=1.6, label=f"IWF ({iwf*100:.2f}%)")
    ax.legend(frameon=False, loc="upper left")
    _style_ax(
        ax,
        f"Average next-quarter return by surprise quintile ({sample_start} – {sample_end})",
        "Quintile at quarter-end (Q5 = highest trailing 4-quarter EPS surprise)",
        "Average quarterly total return (%)",
    )
    fig.savefig(IMAGES / "04_quintile_returns.png", dpi=150, bbox_inches="tight")
    paths["quintiles"] = IMAGES / "04_quintile_returns.png"
    plt.close(fig)

    # 5. Cumulative wealth
    fig, ax = plt.subplots(figsize=(10, 6.0))
    # Q3 is included because, in this sample, it was close to Q5.
    # Hiding it would make the high-surprise basket look uniquely successful.
    series_spec = [
        ("Q5", NAVY, 2.2),
        ("Q3", "#7A8B99", 1.4),
        ("Q1", BRONZE, 1.6),
        ("Equal-weight universe", TEAL, 1.6),
        ("IWF", GOLD, 1.8),
    ]
    for name, color, lw in series_spec:
        r = monthly.loc[monthly["portfolio"] == name, ["date", "return"]].dropna().sort_values("date")
        wealth = (1.0 + r["return"]).cumprod()
        ax.plot(r["date"], wealth, label=name, color=color, lw=lw)
    ax.legend(frameon=False)
    _style_ax(
        ax,
        f"Growth of $1 ({sample_start} – {sample_end})",
        "",
        "Growth of $1 (monthly, dividend-adjusted)",
    )
    fig.savefig(IMAGES / "05_cumulative_returns.png", dpi=150, bbox_inches="tight")
    paths["cumulative"] = IMAGES / "05_cumulative_returns.png"
    plt.close(fig)

    # 6. Sector average composite
    sec = (
        ranked.dropna(subset=["composite"])
        .groupby("sector")
        .agg(avg_composite=("composite", "mean"), n=("ticker", "size"))
        .sort_values("avg_composite")
    )
    fig, ax = plt.subplots(figsize=(10, 5.8))
    bar_colors = [TEAL if v >= 0 else BRONZE for v in sec["avg_composite"]]
    ax.barh(sec.index, sec["avg_composite"], color=bar_colors, height=0.7)
    span = max(float(sec["avg_composite"].abs().max()), 0.2)
    for y, (val, n) in enumerate(zip(sec["avg_composite"], sec["n"])):
        offset = span * 0.04
        ax.text(
            val + (offset if val >= 0 else -offset),
            y,
            f"n={int(n)}",
            va="center",
            ha="left" if val >= 0 else "right",
            fontsize=9,
            color=SLATE,
        )
    ax.set_xlim(-span * 1.45, span * 1.45)
    ax.axvline(0, color=SLATE, lw=0.8)
    _style_ax(ax, "Average composite score by sector", "Average composite z-score", "")
    fig.savefig(IMAGES / "06_sector_scores.png", dpi=150, bbox_inches="tight")
    paths["sectors"] = IMAGES / "06_sector_scores.png"
    plt.close(fig)
    return paths


def data_quality_summary(earnings: pd.DataFrame, scores: pd.DataFrame) -> dict:
    usable = earnings["estimate_usable"]
    compared = earnings.loc[usable & earnings["surprise_vendor"].notna()].copy()
    gap = (compared["surprise_pct"] - compared["surprise_vendor"]).abs()
    return {
        "asof": asof_date(),
        "n_universe": int(universe_tickers().__len__()),
        "n_reported_rows": int(len(earnings)),
        "n_usable_surprises": int(usable.sum()),
        "n_dropped_tiny_or_negative_estimate": int((~usable).sum()),
        "n_surprise_capped": int(((earnings["surprise_pct"].abs() > SURPRISE_CAP) & usable).sum()),
        "median_abs_surprise_vs_vendor": float(gap.median()) if len(gap) else None,
        "surprise_start": str(pd.to_datetime(earnings["report_date"]).min().date()),
        "surprise_end": str(pd.to_datetime(earnings["report_date"]).max().date()),
        "n_scored": int(scores["composite"].notna().sum()),
        "n_unscored": int(scores["composite"].isna().sum()),
        "median_beat_rate": float(scores["beat_rate_8q"].median(skipna=True)),
        "median_avg_surprise": float(scores["avg_surprise_8q"].median(skipna=True)),
    }


def build_summary(
    scores: pd.DataFrame,
    stats: pd.DataFrame,
    quarterly: pd.DataFrame,
    quality: dict,
    sk: dict,
    spread: dict,
) -> dict:
    top = scores.dropna(subset=["rank"]).nsmallest(10, "rank")
    show = [
        "rank",
        "ticker",
        "name",
        "sector",
        "composite",
        "surprise_z",
        "revision_z",
        "accel_z",
        "value_z",
        "avg_surprise_8q",
        "beat_rate_8q",
        "rev90",
        "breadth_30d",
        "realized_accel",
        "forward_accel",
        "forward_growth",
        "forward_pe",
        "peg_ratio",
        "fy1_eps",
        "fy2_eps",
        "price",
        "market_cap",
        "peer_percentile",
    ]
    top_records = json.loads(top[show].to_json(orient="records", date_format="iso"))
    stats_records = json.loads(stats.to_json(orient="records"))
    qmeans = (
        quarterly[quarterly["portfolio"].isin(["Q1", "Q2", "Q3", "Q4", "Q5", "Equal-weight universe", "IWF"])]
        .groupby("portfolio")["quarter_return"]
        .mean()
        .to_dict()
    )
    return {
        "quality": quality,
        "top10": top_records,
        "stats": stats_records,
        "avg_quarter_return": {k: float(v) for k, v in qmeans.items()},
        "sklearn": sk,
        "spread": spread,
    }


def run(force_download: bool = False) -> dict:
    download_raw(force=force_download)
    earnings = clean_earnings()
    daily = load_prices()
    snap = load_snapshot()
    earnings, snap, daily = limit_to_universe(earnings, snap, daily)
    px_m = month_end_prices(daily)
    last_price = daily.ffill().iloc[-1]
    snap["price"] = snap["ticker"].map(last_price)
    factors = build_current_factors(earnings, snap)
    # Names in the universe with no earnings history still appear, unscored.
    missing = set(universe_tickers()) - set(factors["ticker"])
    if missing:
        extra = snap[snap["ticker"].isin(missing)].copy()
        factors = pd.concat([factors, extra], ignore_index=True)
    scores = score_factors(factors)
    panel = build_surprise_panel(earnings, px_m, window=4)
    monthly, quarterly = portfolio_returns(panel, px_m)
    stats = performance_stats(monthly, quarterly)
    sk = sklearn_signal_check(panel)
    # Fix spread window using quarterly next_date.
    wide_dates = quarterly.loc[quarterly["portfolio"] == "Q5", ["rebalance_date", "next_date"]]
    spread = spread_tstat(quarterly)
    spread["last_period_end"] = str(pd.Timestamp(wide_dates["next_date"].max()).date())
    spread["first_rebalance"] = str(pd.Timestamp(wide_dates["rebalance_date"].min()).date())
    quality = data_quality_summary(earnings, scores)
    save_database(earnings, px_m, snap, scores, panel, quarterly)
    make_charts(scores, quarterly, monthly)

    # Clean tables a reader can open without the database.
    scores.to_csv(DATA / "scores.csv", index=False)
    stats.to_csv(DATA / "backtest_stats.csv", index=False)
    quarterly.to_csv(DATA / "backtest_quarterly.csv", index=False)
    summary = build_summary(scores, stats, quarterly, quality, sk, spread)
    (DATA / "results_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"quality": quality, "spread": spread, "sklearn": sk}, indent=2))
    print(stats.to_string(index=False))
    print(scores.dropna(subset=["rank"]).nsmallest(15, "rank")[
        ["rank", "ticker", "composite", "surprise_z", "revision_z", "accel_z", "value_z", "forward_pe", "peg_ratio"]
    ].to_string(index=False))
    return {
        "earnings": earnings,
        "prices_monthly": px_m,
        "snapshot": snap,
        "scores": scores,
        "panel": panel,
        "monthly": monthly,
        "quarterly": quarterly,
        "stats": stats,
        "summary": summary,
    }


if __name__ == "__main__":
    run(force_download=False)
