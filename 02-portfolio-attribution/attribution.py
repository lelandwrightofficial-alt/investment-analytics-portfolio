"""
Performance and attribution for a simple U.S. large-cap growth portfolio.

The portfolio holds the two largest companies in each sector of a fixed
77-name coverage universe, equal-weighted, and reforms the book at each
quarter-end. The question is how much of its return gap versus IWF (the
iShares Russell 1000 Growth ETF, used as a proxy for the Russell 1000
Growth) is sector weights, stock selection inside those sectors, and a
handful of style-factor ETF spreads.

Yahoo Finance does not provide an official history of Russell 1000 Growth
membership or of the index's sector weights. Holdings-based attribution
is therefore run against a cap-weighted portfolio of the same 77 names,
using a market-cap proxy known on the rebalance date. IWF is the external
benchmark for the performance table and for the style regression. The gap
between the cap-weighted 77 and IWF is reported on its own. It is not
pushed into the Brinson terms.

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
DB_PATH = DATA / "attribution.db"

# Two names per sector is the whole portfolio rule. It is a concentration
# limit, not a return forecast. Equal weight then stops the larger of the
# two from dominating the book.
NAMES_PER_SECTOR = 2
# Study window. Quarter-ends outside this range are not rebalance dates.
# The last return runs through the last completed month on or before LAST.
FIRST_REBALANCE = pd.Timestamp("2021-09-30")
LAST_MONTH = pd.Timestamp("2026-09-30")

BENCHMARK = "IWF"
# Style ETFs. Each spread is a long-short of two total-return prices.
# They are ETF proxies, not Ken French factor portfolios.
FACTOR_ETFS = ["IWF", "IWD", "IWB", "IWM", "MTUM", "QUAL", "SPY", "BIL"]
FACTOR_PAIRS = [
    ("value_minus_growth", "IWD", "IWF"),
    ("small_minus_large", "IWM", "IWB"),
    ("momentum_minus_market", "MTUM", "SPY"),
    ("quality_minus_market", "QUAL", "SPY"),
]
# One-way turnover cost used only in the sensitivity, not in the base results.
COST_BPS = 10

NAVY = "#1F4E79"
TEAL = "#1D7874"
GOLD = "#C5922C"
BRONZE = "#8C5A3C"
SLATE = "#4B5563"
LIGHT = "#F4F7FB"


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
        except Exception as exc:
            last = exc
            time.sleep(pause * (i + 1))
    print(f"  giving up: {last}")
    return None


def _flatten_download(px: pd.DataFrame, field: str, tickers: list[str]) -> pd.DataFrame:
    """Pull one price field out of a yfinance download frame."""
    if px is None or len(px) == 0:
        raise RuntimeError("Price download was empty.")
    if isinstance(px.columns, pd.MultiIndex):
        if field not in px.columns.get_level_values(0):
            raise RuntimeError(f"Download has no {field} column: {px.columns.get_level_values(0).unique().tolist()}")
        out = px[field].copy()
    else:
        if field not in px.columns:
            raise RuntimeError(f"Download has no {field} column: {list(px.columns)}")
        out = px[[field]].copy()
        out.columns = tickers[:1]
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out.sort_index()
    out = out.loc[~out.index.duplicated(keep="last")]
    return out


def download_raw(force: bool = False) -> None:
    """Download prices, shares outstanding, sectors, and the current IWF snapshot.

    Raw responses are cached as CSV. Re-runs use the cache unless force=True,
    so the notebook still runs if the vendor is offline.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    tickers = universe_tickers()
    symbols = tickers + FACTOR_ETFS
    needed = [
        RAW / "prices_adjusted.csv",
        RAW / "prices_unadjusted.csv",
        RAW / "shares.csv",
        RAW / "snapshot.csv",
        RAW / "iwf_top_holdings.csv",
        RAW / "iwf_sector_weights.csv",
        RAW / "splits.csv",
        RAW / "asof.txt",
    ]
    if not force and all(p.exists() for p in needed):
        print("Using cached raw files in data/raw/")
        return

    print(f"Downloading daily prices for {len(symbols)} symbols...")
    px = yf.download(
        symbols,
        start="2020-01-01",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    adj = _flatten_download(px, "Adj Close", symbols)
    raw = _flatten_download(px, "Close", symbols)
    keep = [c for c in symbols if c in adj.columns]
    missing = [c for c in symbols if c not in adj.columns]
    if missing:
        raise RuntimeError(f"No prices returned for: {missing}")
    adj[keep].to_csv(RAW / "prices_adjusted.csv")
    raw[keep].to_csv(RAW / "prices_unadjusted.csv")
    print(f"  prices: {adj.shape[0]} days, last {adj.index.max().date()}")

    share_rows = []
    snap_rows = []
    for i, ticker in enumerate(tickers, start=1):
        print(f"[{i:02d}/{len(tickers)}] {ticker}")
        t = yf.Ticker(ticker)
        info = _retry(lambda: t.info) or {}
        snap_rows.append(
            {
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),
                "shares_outstanding": info.get("sharesOutstanding"),
            }
        )
        shares = _retry(lambda: t.get_shares_full(start="2019-01-01"))
        if shares is not None and len(shares):
            s = shares.copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            s = s.sort_index()
            s = s[~s.index.duplicated(keep="last")]
            tmp = s.rename("shares").reset_index()
            tmp.columns = ["date", "shares"]
            tmp.insert(0, "ticker", ticker)
            share_rows.append(tmp)
        time.sleep(0.15)

    snap = pd.DataFrame(snap_rows)
    if snap["sector"].isna().any() or snap["market_cap"].isna().any():
        bad = snap.loc[snap["sector"].isna() | snap["market_cap"].isna(), "ticker"].tolist()
        raise RuntimeError(f"Snapshot missing sector or market cap for: {bad}")
    snap.to_csv(RAW / "snapshot.csv", index=False)
    if not share_rows:
        raise RuntimeError("No shares-outstanding history came back.")
    pd.concat(share_rows, ignore_index=True).to_csv(RAW / "shares.csv", index=False)

    iwf = yf.Ticker(BENCHMARK)
    holdings = iwf.funds_data.top_holdings.reset_index()
    holdings.to_csv(RAW / "iwf_top_holdings.csv", index=False)
    sectors = iwf.funds_data.sector_weightings
    pd.DataFrame(
        [{"sector_key": k, "weight": v} for k, v in sectors.items()]
    ).to_csv(RAW / "iwf_sector_weights.csv", index=False)

    download_splits(tickers, force=True)

    asof = pd.Timestamp.today().strftime("%Y-%m-%d")
    (RAW / "asof.txt").write_text(asof + "\n")
    print("Raw cache written.")


def download_splits(tickers: list[str] | None = None, force: bool = False) -> None:
    """Cache Yahoo's split file. Spinoff factors are kept; the market-cap code sorts them out."""
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / "splits.csv"
    if path.exists() and not force:
        return
    tickers = tickers or universe_tickers()
    rows = []
    print(f"Downloading split history for {len(tickers)} names...")
    for ticker in tickers:
        series = _retry(lambda t=ticker: yf.Ticker(t).splits)
        if series is not None and len(series):
            series = series.copy()
            series.index = pd.to_datetime(series.index).tz_localize(None).normalize()
            for dt, ratio in series.items():
                rows.append({"ticker": ticker, "date": pd.Timestamp(dt), "ratio": float(ratio)})
        time.sleep(0.05)
    if not rows:
        raise RuntimeError("No split history came back. Refusing to invent a scale for market cap.")
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"  splits: {len(rows)} rows")


def load_splits() -> pd.DataFrame:
    path = RAW / "splits.csv"
    if not path.exists():
        download_splits()
    sp = pd.read_csv(path, parse_dates=["date"])
    sp["ticker"] = sp["ticker"].astype(str)
    sp["date"] = pd.to_datetime(sp["date"]).dt.tz_localize(None).dt.normalize()
    sp["ratio"] = pd.to_numeric(sp["ratio"], errors="coerce")
    sp = sp.dropna(subset=["ratio"])
    return sp.sort_values(["ticker", "date"])


def asof_date() -> str:
    return (RAW / "asof.txt").read_text().strip()


def load_prices(kind: str) -> pd.DataFrame:
    path = RAW / ("prices_adjusted.csv" if kind == "adjusted" else "prices_unadjusted.csv")
    px = pd.read_csv(path, index_col=0, parse_dates=True)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    return px


def load_snapshot() -> pd.DataFrame:
    snap = pd.read_csv(RAW / "snapshot.csv")
    snap["ticker"] = snap["ticker"].astype(str)
    return snap


def load_shares() -> pd.DataFrame:
    sh = pd.read_csv(RAW / "shares.csv", parse_dates=["date"])
    sh["ticker"] = sh["ticker"].astype(str)
    sh["date"] = pd.to_datetime(sh["date"]).dt.tz_localize(None).dt.normalize()
    sh["shares"] = pd.to_numeric(sh["shares"], errors="coerce")
    sh = sh.dropna(subset=["shares"])
    sh = sh[sh["shares"] > 0]
    return sh.sort_values(["ticker", "date"])


def month_end_frame(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Last available close in each completed month, plus the session it came from.

    The current month is dropped when the last trade is more than four days
    before month-end, so a download early in October cannot treat October
    as a finished month. Same rule as the earnings-revision project.
    """
    daily = daily.sort_index()
    px = daily.resample("ME").last()
    trade_day = daily.index.to_series().groupby(daily.index.to_period("M")).max()
    trade_day.index = trade_day.index.to_timestamp("M")
    last_trade = daily.dropna(how="all").index.max()
    month_end = last_trade + pd.offsets.MonthEnd(0)
    if (month_end - last_trade).days > 4:
        px = px.loc[px.index < month_end]
    trade_day = trade_day.reindex(px.index)
    if BENCHMARK in px.columns:
        ok = px[BENCHMARK].notna()
        px = px.loc[ok]
        trade_day = trade_day.loc[ok]
    if trade_day.isna().any():
        raise RuntimeError("A month-end price has no trading session behind it.")
    return px, trade_day


def is_share_split(ratio: float) -> bool:
    """True for a split that changes the share count, false for a spinoff adjustment.

    Yahoo's split feed mixes real splits (2-for-1, 10-for-1, 1-for-8) with
    spinoff factors that only rescale the historical price (GE HealthCare,
    GE Vernova, and a few others in this universe). Those spinoff factors
    sit near 1 and are not round ratios. Applying them to the share count
    would invent shares.
    """
    if ratio <= 0:
        return False
    if abs(ratio - 1.5) < 0.02:
        return True
    if ratio >= 1.9 or ratio <= 0.55:
        return True
    return False


def _split_is_in_count(shares_now: float, pre: float, ratio: float) -> bool:
    """Whether `shares_now` already sits on the post-split side of `pre`."""
    if ratio >= 1:
        return shares_now >= pre * ratio * 0.75
    return shares_now <= pre * ratio / 0.75


def _share_factor_for_day(sh: pd.DataFrame, splits: pd.DataFrame, day: pd.Timestamp, shares_now: float) -> float:
    """Multiply shares by any split that the price series already reflects and the share count does not.

    Yahoo's Close is adjusted for every split in the file, including splits
    that happen later. The shares file is the count on that date, and it
    sometimes updates a few days before or after the ex-date. The factor
    puts the two on the same scale. The anchor for "pre-split shares" is
    taken 21 days before the ex-date, so an early update does not become
    the baseline. Once any later print shows the post-split count, later
    dates are left alone, which keeps a multi-year buyback from looking
    like a split that was never applied.
    """
    factor = 1.0
    if splits is None or splits.empty:
        return factor
    for row in splits.itertuples(index=False):
        ratio = float(row.ratio)
        if not is_share_split(ratio):
            continue
        ex_date = pd.Timestamp(row.date)
        pre_cut = ex_date - pd.Timedelta(days=21)
        pre_rows = sh.loc[sh["date"] <= pre_cut]
        if pre_rows.empty:
            # The shares file starts after this split. The count is already
            # on the post-split scale, and so is a price after the ex-date.
            continue
        pre = float(pre_rows.iloc[-1]["shares"])
        later = sh.loc[sh["date"] > pre_cut]
        reflected_from = None
        for obs in later.itertuples(index=False):
            if _split_is_in_count(float(obs.shares), pre, ratio):
                reflected_from = pd.Timestamp(obs.date)
                break
        reflected = reflected_from is not None and day >= reflected_from
        if not reflected and _split_is_in_count(shares_now, pre, ratio):
            reflected = True
        if not reflected:
            factor *= ratio
    return factor


def _price_only_factor(splits: pd.DataFrame, day: pd.Timestamp) -> float:
    """Undo spinoff adjustments that Yahoo applied to the historical price only.

    Close before a spinoff is scaled so the chart does not gap when the
    spun-off company leaves. Market cap has to use the price the stock
    actually traded at. Factors dated after `day` are the ones still
    baked into that session's Close.
    """
    if splits is None or splits.empty:
        return 1.0
    factor = 1.0
    for row in splits.itertuples(index=False):
        ratio = float(row.ratio)
        if is_share_split(ratio):
            continue
        if pd.Timestamp(row.date) > day:
            factor *= ratio
    return factor


def build_market_caps(
    unadj_m: pd.DataFrame,
    trade_day: pd.Series,
    shares: pd.DataFrame,
    snap: pd.DataFrame,
    splits: pd.DataFrame,
) -> pd.DataFrame:
    """Point-in-time market cap at each month-end.

    Market cap is shares outstanding known on that session, times the
    as-traded price. Yahoo's Close is split-adjusted and, for a few names,
    spinoff-adjusted, so it is rescaled onto the share count before the
    multiplication. Shares dated after the session are not used.

    If the last month still disagrees with Yahoo's market cap by more than
    25%, that name's share history is not a usable scale. The fallback
    scales the snapshot market cap by the split-adjusted price path. That
    applies today's share count to the past. It is flagged, and it is the
    exception rather than the method.
    """
    tickers = [t for t in snap["ticker"] if t in unadj_m.columns]
    snap_ix = snap.set_index("ticker")
    records = []
    for ticker in tickers:
        px = unadj_m[ticker].dropna()
        if px.empty:
            continue
        sh = shares.loc[shares["ticker"] == ticker, ["date", "shares"]].drop_duplicates("date", keep="last")
        sh = sh.sort_values("date")
        sp = splits.loc[splits["ticker"] == ticker] if len(splits) else splits
        level_source = "shares_x_price"
        yahoo_mcap = float(snap_ix.loc[ticker, "market_cap"])
        for month_end, trade in trade_day.items():
            if month_end not in px.index or pd.isna(trade):
                continue
            price = float(px.loc[month_end])
            trade = pd.Timestamp(trade)
            if not np.isfinite(price) or price <= 0:
                continue
            known = sh.loc[sh["date"] <= trade]
            if known.empty:
                shares_used = np.nan
                shares_asof = pd.NaT
                mcap = np.nan
                source = "missing"
                share_factor = np.nan
                price_factor = np.nan
            else:
                shares_used = float(known.iloc[-1]["shares"])
                shares_asof = pd.Timestamp(known.iloc[-1]["date"])
                if (trade - shares_asof).days > 180:
                    mcap = np.nan
                    source = "stale_shares"
                    share_factor = np.nan
                    price_factor = np.nan
                else:
                    share_factor = _share_factor_for_day(sh, sp, trade, shares_used)
                    price_factor = _price_only_factor(sp, trade)
                    mcap = shares_used * share_factor * price * price_factor
                    source = "shares_x_price"
            records.append(
                {
                    "ticker": ticker,
                    "month_end": month_end,
                    "trade_date": trade,
                    "unadj_close": price,
                    "shares": shares_used,
                    "shares_asof": shares_asof,
                    "share_factor": share_factor,
                    "price_factor": price_factor,
                    "market_cap": mcap,
                    "source": source,
                }
            )
        mine = [r for r in records if r["ticker"] == ticker]
        anchor = mine[-1] if mine else None
        if anchor is not None and yahoo_mcap > 0 and anchor["unadj_close"] > 0:
            if np.isfinite(anchor["market_cap"]) and anchor["unadj_close"] > 0:
                # Compare with the spinoff factor removed, on the last session,
                # where no future split should remain.
                ratio = anchor["market_cap"] / yahoo_mcap
            else:
                ratio = np.nan
            if not np.isfinite(ratio) or ratio < 0.75 or ratio > 1.25:
                level_source = "price_scaled_snapshot"
                anchor_price = anchor["unadj_close"]
                for r in mine:
                    # Scale the snapshot by the split-adjusted price. This is
                    # only a size rank, and only for names whose share file
                    # does not match the vendor's own market cap.
                    r["market_cap"] = yahoo_mcap * r["unadj_close"] / anchor_price
                    r["source"] = "price_scaled_snapshot"
                    r["shares"] = np.nan
                    r["shares_asof"] = pd.NaT
                    r["share_factor"] = np.nan
                    r["price_factor"] = np.nan
        for r in mine:
            r["level_source"] = level_source

    return pd.DataFrame(records)


def quarter_ends(month_index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Calendar quarter-ends that are present as month-end labels in the price file."""
    start = FIRST_REBALANCE
    end = min(LAST_MONTH, month_index.max())
    stamps = pd.date_range(start, end, freq="QE")
    have = set(pd.Timestamp(d) for d in month_index)
    out = [pd.Timestamp(d) for d in stamps if pd.Timestamp(d) in have]
    if len(out) < 8:
        raise RuntimeError(f"Only {len(out)} quarter-ends in the price file.")
    return out


def _sector_return(weights: pd.Series, rets: pd.Series) -> float:
    w = weights.sum()
    if w <= 0 or not np.isfinite(w):
        return np.nan
    return float((weights * rets).sum() / w)


def build_quarter_panel(
    adj_m: pd.DataFrame,
    mcap: pd.DataFrame,
    snap: pd.DataFrame,
) -> pd.DataFrame:
    """One row per name per quarter: weights, market cap, and the quarterly return.

    The portfolio is the two largest names in each sector by the market cap
    known on the rebalance date, equal-weighted. The holdings benchmark is
    every eligible name in the coverage universe, weighted by that same
    market cap. A name is eligible only if it has a market cap on the
    rebalance date and an adjusted price at both ends of the quarter.
    """
    sectors = snap.set_index("ticker")["sector"]
    qends = quarter_ends(adj_m.index)
    rows = []
    for i, start in enumerate(qends[:-1]):
        end = qends[i + 1]
        if (end - start).days > 100:
            raise RuntimeError(f"Quarter gap is too long: {start.date()} -> {end.date()}")
        cap = mcap.loc[mcap["month_end"] == start, ["ticker", "market_cap", "source", "shares_asof"]].copy()
        cap = cap.dropna(subset=["market_cap"])
        cap = cap[cap["market_cap"] > 0]
        p0 = adj_m.loc[start]
        p1 = adj_m.loc[end]
        eligible = []
        for ticker, m in cap.set_index("ticker")["market_cap"].items():
            if ticker not in sectors.index or ticker not in adj_m.columns:
                continue
            if pd.isna(p0.get(ticker)) or pd.isna(p1.get(ticker)) or p0[ticker] <= 0:
                continue
            eligible.append(ticker)
        if len(eligible) < 50:
            raise RuntimeError(f"Only {len(eligible)} eligible names on {start.date()}")
        block = cap[cap["ticker"].isin(eligible)].copy()
        block["sector"] = block["ticker"].map(sectors)
        block["ret"] = block["ticker"].map(lambda t: float(p1[t] / p0[t] - 1.0))
        block = block.dropna(subset=["sector", "ret"])
        # Stable tie-break so a dead heat does not depend on row order.
        block = block.sort_values(["sector", "market_cap", "ticker"], ascending=[True, False, True])
        block["rank_in_sector"] = block.groupby("sector")["market_cap"].rank(ascending=False, method="first")
        block["selected"] = block["rank_in_sector"] <= NAMES_PER_SECTOR
        n_sel = int(block["selected"].sum())
        if n_sel < 10:
            raise RuntimeError(f"Portfolio has only {n_sel} names on {start.date()}")
        block["port_weight"] = np.where(block["selected"], 1.0 / n_sel, 0.0)
        block["bench_weight"] = block["market_cap"] / block["market_cap"].sum()
        block["eq_weight"] = 1.0 / len(block)
        if abs(block["port_weight"].sum() - 1.0) > 1e-8:
            raise RuntimeError("Portfolio weights do not sum to 1.")
        if abs(block["bench_weight"].sum() - 1.0) > 1e-8:
            raise RuntimeError("Benchmark weights do not sum to 1.")
        # Look-ahead guard on the shares date, when we used the shares file.
        used_shares = block["source"].isin(["shares_x_price"])
        if used_shares.any():
            asof = pd.to_datetime(block.loc[used_shares, "shares_asof"])
            if (asof > start).any():
                raise RuntimeError("Look-ahead: a shares date is after the rebalance.")
        block["rebalance_date"] = start
        block["next_date"] = end
        rows.append(block)
    panel = pd.concat(rows, ignore_index=True)
    return panel


def brinson_fachler(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Brinson-Fachler attribution of the sector-pair book versus the cap-weighted universe.

    For sector i in a quarter:

        allocation  = (w_p - w_b) * (R_b,i - R_b)
        selection   =  w_b       * (R_p,i - R_b,i)
        interaction = (w_p - w_b) * (R_p,i - R_b,i)

    R_b is the cap-weighted universe return that quarter. R_b,i is the
    cap-weighted return of the benchmark names in the sector. R_p,i is the
    equal-weighted return of the portfolio names in the sector.

    If the portfolio holds nothing in a sector, not holding it is treated
    as an allocation decision: R_p,i is set equal to R_b,i, so selection
    and interaction are zero and the whole effect sits in allocation.
    The three columns sum to the portfolio return minus the benchmark return.
    That add-up is an identity. It is not a statistical test.
    """
    sector_rows = []
    quarter_rows = []
    for start, block in panel.groupby("rebalance_date"):
        r_b = float((block["bench_weight"] * block["ret"]).sum())
        r_p = float((block["port_weight"] * block["ret"]).sum())
        r_eq = float(block["ret"].mean())
        alloc = sel = inter = 0.0
        for sector, g in block.groupby("sector"):
            w_p = float(g["port_weight"].sum())
            w_b = float(g["bench_weight"].sum())
            r_b_i = _sector_return(g["bench_weight"], g["ret"])
            if w_p > 0:
                r_p_i = _sector_return(g["port_weight"], g["ret"])
            else:
                r_p_i = r_b_i
            a = (w_p - w_b) * (r_b_i - r_b)
            s = w_b * (r_p_i - r_b_i)
            n = (w_p - w_b) * (r_p_i - r_b_i)
            alloc += a
            sel += s
            inter += n
            sector_rows.append(
                {
                    "rebalance_date": start,
                    "next_date": g["next_date"].iloc[0],
                    "sector": sector,
                    "w_port": w_p,
                    "w_bench": w_b,
                    "active_weight": w_p - w_b,
                    "ret_port": r_p_i,
                    "ret_bench": r_b_i,
                    "allocation": a,
                    "selection": s,
                    "interaction": n,
                    "n_port": int((g["port_weight"] > 0).sum()),
                    "n_bench": int(len(g)),
                }
            )
        total = alloc + sel + inter
        if abs(total - (r_p - r_b)) > 1e-8:
            raise RuntimeError(
                f"Brinson identity failed on {pd.Timestamp(start).date()}: {total} vs {r_p - r_b}"
            )
        # Stock-level active contribution. Each name has one return, so the
        # whole stock-level gap is a weight gap. Measured against the
        # benchmark's own return, the contributions sum to r_p - r_b.
        contrib = (block["port_weight"] - block["bench_weight"]) * (block["ret"] - r_b)
        if abs(float(contrib.sum()) - (r_p - r_b)) > 1e-8:
            raise RuntimeError(f"Stock contribution identity failed on {pd.Timestamp(start).date()}")
        quarter_rows.append(
            {
                "rebalance_date": start,
                "next_date": block["next_date"].iloc[0],
                "ret_port": r_p,
                "ret_bench": r_b,
                "ret_equal": r_eq,
                "excess_vs_bench": r_p - r_b,
                "allocation": alloc,
                "selection": sel,
                "interaction": inter,
                "n_port": int((block["port_weight"] > 0).sum()),
                "n_bench": int(len(block)),
            }
        )
    sectors = pd.DataFrame(sector_rows)
    quarters = pd.DataFrame(quarter_rows).sort_values("rebalance_date")
    return sectors, quarters


def carino_scales(r_p: np.ndarray, r_b: np.ndarray) -> np.ndarray:
    """Carino (1999) coefficients that make period effects add up to the cumulative gap.

    k_t / k, where k_t is the log-return gap divided by the simple gap in
    period t, and k is the same ratio for the full-window cumulative returns.
    Sum of (scale_t * period_excess_t) equals the cumulative portfolio return
    minus the cumulative benchmark return.
    """
    r_p = np.asarray(r_p, dtype=float)
    r_b = np.asarray(r_b, dtype=float)
    growth_p = float(np.prod(1.0 + r_p) - 1.0)
    growth_b = float(np.prod(1.0 + r_b) - 1.0)

    def _k(a: float, b: float) -> float:
        if abs(a - b) < 1e-12:
            return 1.0 / (1.0 + a)
        return float((np.log1p(a) - np.log1p(b)) / (a - b))

    k = _k(growth_p, growth_b)
    scales = np.array([_k(a, b) / k for a, b in zip(r_p, r_b)])
    check = float(np.sum(scales * (r_p - r_b)))
    if abs(check - (growth_p - growth_b)) > 1e-8:
        raise RuntimeError(f"Carino link failed: {check} vs {growth_p - growth_b}")
    return scales


def link_effects(quarters: pd.DataFrame, r_p_col: str, r_b_col: str, effect_cols: list[str]) -> dict:
    """Apply one set of Carino scales so the effects sum to the cumulative gap."""
    scales = carino_scales(quarters[r_p_col].to_numpy(), quarters[r_b_col].to_numpy())
    out = {"carino_scale": scales}
    gap = float(np.prod(1.0 + quarters[r_p_col]) - np.prod(1.0 + quarters[r_b_col]))
    running = 0.0
    for col in effect_cols:
        linked = float(np.sum(scales * quarters[col].to_numpy()))
        out[col] = linked
        running += linked
    if abs(running - gap) > 1e-7:
        raise RuntimeError(f"Linked effects do not sum to the cumulative gap: {running} vs {gap}")
    out["cumulative_gap"] = gap
    return out


def attach_iwf(quarters: pd.DataFrame, adj_m: pd.DataFrame) -> pd.DataFrame:
    q = quarters.copy()
    iwf = []
    for row in q.itertuples(index=False):
        start = pd.Timestamp(row.rebalance_date)
        end = pd.Timestamp(row.next_date)
        if pd.isna(adj_m.loc[start, BENCHMARK]) or pd.isna(adj_m.loc[end, BENCHMARK]):
            raise RuntimeError(f"IWF price missing for {start.date()}")
        iwf.append(float(adj_m.loc[end, BENCHMARK] / adj_m.loc[start, BENCHMARK] - 1.0))
    q["ret_iwf"] = iwf
    q["excess_vs_iwf"] = q["ret_port"] - q["ret_iwf"]
    q["bench_vs_iwf"] = q["ret_bench"] - q["ret_iwf"]
    return q


def monthly_book_returns(panel: pd.DataFrame, adj_m: pd.DataFrame, weight_col: str) -> pd.DataFrame:
    """Monthly returns with weights set at quarter-end and then allowed to drift.

    Drifting weights, compounded across the quarter, equal the beginning-weight
    portfolio return. The function checks that identity.
    """
    rows = []
    for start, block in panel.groupby("rebalance_date"):
        end = pd.Timestamp(block["next_date"].iloc[0])
        start = pd.Timestamp(start)
        w = block.set_index("ticker")[weight_col]
        w = w[w > 0]
        months = adj_m.index[(adj_m.index >= start) & (adj_m.index <= end)]
        px = adj_m.loc[months, w.index]
        if px.isna().any().any():
            # A missing month would break the drift path. The quarterly return
            # still exists; skip the monthly path only if this happens, and say so.
            raise RuntimeError(f"Missing month-end price inside {start.date()} -> {end.date()}")
        rets = px.pct_change().iloc[1:]
        weights = w.astype(float).copy()
        path = []
        for dt, row in rets.iterrows():
            r = row.astype(float)
            port_r = float((weights * r).sum())
            path.append(port_r)
            rows.append({"date": dt, "return": port_r})
            weights = weights * (1.0 + r)
            weights = weights / weights.sum()
        compounded = float(np.prod(1.0 + np.array(path)) - 1.0)
        quarterly = float((w * block.set_index("ticker").loc[w.index, "ret"]).sum())
        if abs(compounded - quarterly) > 1e-8:
            raise RuntimeError(
                f"Monthly path does not match the quarterly return on {start.date()}: {compounded} vs {quarterly}"
            )
    return pd.DataFrame(rows)


def monthly_etf(adj_m: pd.DataFrame, ticker: str, dates: pd.DatetimeIndex) -> pd.Series:
    """Month-end total return of one ETF, on the dates the book has a return."""
    # The book's first monthly date is the month-end after the first rebalance.
    # The ETF return on that date is from the prior month-end, which is the rebalance.
    px = adj_m[ticker].dropna().sort_index()
    out = {}
    for dt in dates:
        prev = px.index[px.index < dt].max()
        if pd.isna(prev):
            continue
        out[dt] = float(px.loc[dt] / px.loc[prev] - 1.0)
    return pd.Series(out, name=ticker)


def build_monthly(panel: pd.DataFrame, adj_m: pd.DataFrame) -> pd.DataFrame:
    port = monthly_book_returns(panel, adj_m, "port_weight")
    bench = monthly_book_returns(panel, adj_m, "bench_weight")
    equal = monthly_book_returns(panel, adj_m, "eq_weight")
    dates = pd.DatetimeIndex(port["date"])
    frame = pd.DataFrame(
        {
            "date": port["date"].to_numpy(),
            "sector_pair": port["return"].to_numpy(),
            "cap_weight": bench["return"].to_numpy(),
            "equal_weight": equal["return"].to_numpy(),
        }
    )
    for ticker in FACTOR_ETFS:
        frame[ticker] = monthly_etf(adj_m, ticker, pd.DatetimeIndex(frame["date"])).reindex(frame["date"]).to_numpy()
    if frame[FACTOR_ETFS + ["sector_pair", "cap_weight", "equal_weight"]].isna().any().any():
        raise RuntimeError("Monthly return panel has a gap.")
    # The quarterly IWF return must match the compounded monthly ETF path.
    return frame


def performance_row(name: str, monthly: pd.Series, quarterly: pd.Series, iwf_q: pd.Series, bil_m: pd.Series) -> dict:
    """CAGR, volatility, two Sharpes, max drawdown, and hit rates.

    sharpe_rf0 uses a zero risk-free rate, so it can be compared with the
    earnings-revision project. sharpe_bil subtracts the monthly return of
    BIL, a 1–3 month T-bill ETF, and is the closer of the two to a textbook
    Sharpe. Neither one is a promise about future risk.
    """
    r = monthly.astype(float).dropna()
    wealth = (1.0 + r).cumprod()
    n = len(r)
    cagr = float(wealth.iloc[-1] ** (12.0 / n) - 1.0)
    vol = float(r.std(ddof=1) * np.sqrt(12.0))
    sharpe0 = float(r.mean() / r.std(ddof=1) * np.sqrt(12.0))
    excess = r - bil_m.reindex(r.index).astype(float)
    sharpe_bil = float(excess.mean() / excess.std(ddof=1) * np.sqrt(12.0))
    dd = wealth / wealth.cummax() - 1.0
    q = quarterly.astype(float)
    iwf = iwf_q.reindex(q.index).astype(float)
    return {
        "portfolio": name,
        "months": n,
        "quarters": int(q.notna().sum()),
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe_rf0": sharpe0,
        "sharpe_bil": sharpe_bil,
        "max_drawdown": float(dd.min()),
        "hit_rate_positive": float((q > 0).mean()),
        "hit_rate_vs_iwf": float((q > iwf).mean()),
        "avg_quarter_return": float(q.mean()),
        "total_return": float(wealth.iloc[-1] - 1.0),
    }


def performance_table(monthly: pd.DataFrame, quarters: pd.DataFrame) -> pd.DataFrame:
    q = quarters.set_index("rebalance_date")
    m = monthly.set_index("date")
    rows = [
        performance_row("Sector-pair", m["sector_pair"], q["ret_port"], q["ret_iwf"], m["BIL"]),
        performance_row("Cap-weight universe", m["cap_weight"], q["ret_bench"], q["ret_iwf"], m["BIL"]),
        performance_row("Equal-weight universe", m["equal_weight"], q["ret_equal"], q["ret_iwf"], m["BIL"]),
        performance_row("IWF", m["IWF"], q["ret_iwf"], q["ret_iwf"], m["BIL"]),
    ]
    # Hit rate versus IWF is not meaningful for IWF itself.
    rows[-1]["hit_rate_vs_iwf"] = np.nan
    return pd.DataFrame(rows)


def turnover_and_active_share(panel: pd.DataFrame) -> pd.DataFrame:
    """One-way turnover from the drifted end-of-quarter book into the next book.

    Turnover is half the sum of absolute weight changes, which is the share
    of the book that is sold (and bought) at the rebalance. The first quarter
    has no prior book, so it has no turnover.
    """
    rows = []
    prev = None
    for start, block in panel.groupby("rebalance_date"):
        b = block.set_index("ticker")
        w = b["port_weight"]
        active_share = 0.5 * float((b["port_weight"] - b["bench_weight"]).abs().sum())
        row = {
            "rebalance_date": start,
            "active_share": active_share,
            "n_names": int((w > 0).sum()),
            "one_way_turnover": np.nan,
        }
        if prev is not None:
            names = sorted(set(prev.index).union(w.index))
            w_prev = prev.reindex(names).fillna(0.0)
            w_now = w.reindex(names).fillna(0.0)
            row["one_way_turnover"] = 0.5 * float((w_now - w_prev).abs().sum())
        # Drift the current weights to the end of the quarter for the next comparison.
        drifted = w * (1.0 + b["ret"])
        drifted = drifted / drifted.sum()
        prev = drifted
        rows.append(row)
    return pd.DataFrame(rows)


def cost_sensitivity(quarters: pd.DataFrame, turnover: pd.DataFrame) -> dict:
    """Recompute the sector-pair CAGR after charging COST_BPS one-way at each rebalance.

    The first quarter is charged the average later turnover, because the book
    has to be built from cash. This is a sensitivity, not the base result.
    """
    t = turnover.set_index("rebalance_date")["one_way_turnover"]
    avg = float(t.dropna().mean())
    costs = t.fillna(avg)
    q = quarters.set_index("rebalance_date")
    gross = q["ret_port"]
    net = (1.0 - costs * COST_BPS / 10000.0) * (1.0 + gross) - 1.0
    # Map quarterly nets onto a monthly-equivalent CAGR using the quarter count.
    wealth = float(np.prod(1.0 + net.to_numpy()))
    years = len(net) / 4.0
    cagr_net = wealth ** (1.0 / years) - 1.0
    wealth_g = float(np.prod(1.0 + gross.to_numpy()))
    cagr_gross = wealth_g ** (1.0 / years) - 1.0
    return {
        "cost_bps_one_way": COST_BPS,
        "avg_one_way_turnover": avg,
        "cagr_gross_quarterly": float(cagr_gross),
        "cagr_net_quarterly": float(cagr_net),
        "cagr_drag": float(cagr_gross - cagr_net),
    }


def _ols(X: np.ndarray, y: np.ndarray) -> dict:
    """Fit y on X with an intercept. Standard errors assume independent months.

    Months in a return series are not independent, so the t-statistics are
    descriptive. They are not a license to quote a p-value.
    """
    n, k = X.shape
    model = LinearRegression()
    model.fit(X, y)
    pred = model.predict(X)
    resid = y - pred
    dof = n - k - 1
    sse = float(np.sum(resid**2))
    sst = float(np.sum((y - y.mean()) ** 2))
    sigma2 = sse / dof if dof > 0 else np.nan
    X1 = np.column_stack([np.ones(n), X])
    xtx_inv = np.linalg.pinv(X1.T @ X1)
    se = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0.0))
    coefs = np.r_[float(model.intercept_), model.coef_.astype(float)]
    return {
        "intercept": float(model.intercept_),
        "coef": [float(c) for c in model.coef_],
        "se": [float(s) for s in se],
        "tstat": [float(c / s) if s > 0 else np.nan for c, s in zip(coefs, se)],
        "r2": float(1.0 - sse / sst) if sst > 0 else np.nan,
        "n": int(n),
        "pred": pred,
        "resid": resid,
    }


def _r2_oos(y: np.ndarray, pred: np.ndarray) -> float:
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot <= 0:
        return np.nan
    return 1.0 - ss_res / ss_tot


def style_regression(monthly: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Does a handful of style-ETF spreads account for the gap versus IWF?

    The left-hand side is the sector-pair monthly return minus IWF. The
    right-hand side is four contemporaneous spreads:

        IWD - IWF     Russell 1000 Value minus Russell 1000 Growth
        IWM - IWB     Russell 2000 minus Russell 1000
        MTUM - SPY    USA momentum ETF minus the S&P 500 ETF
        QUAL - SPY    USA quality ETF minus the S&P 500 ETF

    This is a description of the window, not a forecast. The time split
    asks a narrower question: do betas fit on the earlier months describe
    the later months? A random split would put the same regime on both sides.
    """
    df = monthly.copy()
    df["excess_vs_iwf"] = df["sector_pair"] - df["IWF"]
    df["cap_excess_vs_iwf"] = df["cap_weight"] - df["IWF"]
    for name, long_t, short_t in FACTOR_PAIRS:
        df[name] = df[long_t] - df[short_t]
    factor_names = [p[0] for p in FACTOR_PAIRS]
    # Correlation of the factors, so a reader can see they are not independent.
    corr = df[factor_names].corr()
    y = df["excess_vs_iwf"].to_numpy()
    X = df[factor_names].to_numpy()
    full = _ols(X, y)
    # Single-index beta of the book on IWF, and of the cap-weight proxy on IWF.
    beta_port = _ols(df[["IWF"]].to_numpy(), df["sector_pair"].to_numpy())
    beta_cap = _ols(df[["IWF"]].to_numpy(), df["cap_weight"].to_numpy())
    # Excess-return beta using BIL as cash. Same question, with a cash rate.
    y_ex = (df["sector_pair"] - df["BIL"]).to_numpy()
    x_ex = (df["IWF"] - df["BIL"]).to_numpy().reshape(-1, 1)
    beta_excess = _ols(x_ex, y_ex)

    mid = len(df) // 2
    train = df.iloc[:mid]
    test = df.iloc[mid:]
    X_train = train[factor_names].to_numpy()
    y_train = train["excess_vs_iwf"].to_numpy()
    X_test = test[factor_names].to_numpy()
    y_test = test["excess_vs_iwf"].to_numpy()
    split_model = LinearRegression().fit(X_train, y_train)
    pred_test = split_model.predict(X_test)
    # Cap-weight residual: how close is the 77-name proxy to IWF?
    te = df["cap_excess_vs_iwf"]
    tracking = float(te.std(ddof=1) * np.sqrt(12.0))
    corr_cap = float(df["cap_weight"].corr(df["IWF"]))

    fitted = full["pred"]
    detail = df[["date", "sector_pair", "cap_weight", "equal_weight", "IWF", "excess_vs_iwf"] + factor_names].copy()
    detail["fitted_excess"] = fitted
    detail["residual_excess"] = y - fitted

    coef_rows = []
    labels = ["intercept"] + factor_names
    for i, label in enumerate(labels):
        coef_rows.append(
            {
                "term": label,
                "coef_monthly": full["coef"][i - 1] if i else full["intercept"],
                "se": full["se"][i],
                "tstat_descriptive": full["tstat"][i],
            }
        )
    result = {
        "factor_names": factor_names,
        "n_months": int(len(df)),
        "train_end_inclusive": str(pd.Timestamp(train["date"].iloc[-1]).date()),
        "test_start": str(pd.Timestamp(test["date"].iloc[0]).date()),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "r2_full": full["r2"],
        "alpha_monthly": full["intercept"],
        "alpha_annualized": full["intercept"] * 12.0,
        "mean_excess_monthly": float(y.mean()),
        "mean_fitted_monthly": float(fitted.mean()),
        "r2_test": float(_r2_oos(y_test, pred_test)),
        "alpha_train_monthly": float(split_model.intercept_),
        "coef_train": [float(c) for c in split_model.coef_],
        "beta_on_iwf": beta_port["coef"][0],
        "alpha_on_iwf_monthly": beta_port["intercept"],
        "alpha_on_iwf_annualized": beta_port["intercept"] * 12.0,
        "r2_on_iwf": beta_port["r2"],
        "beta_on_iwf_tstat": beta_port["tstat"][1],
        "beta_excess_bil": beta_excess["coef"][0],
        "alpha_excess_bil_annualized": beta_excess["intercept"] * 12.0,
        "r2_excess_bil": beta_excess["r2"],
        "cap_beta_on_iwf": beta_cap["coef"][0],
        "cap_alpha_on_iwf_annualized": beta_cap["intercept"] * 12.0,
        "cap_r2_on_iwf": beta_cap["r2"],
        "cap_tracking_error": tracking,
        "cap_corr_iwf": corr_cap,
        "coefficients": coef_rows,
        "factor_correlation": corr.round(4).to_dict(),
    }
    return result, detail


def linked_contributors(panel: pd.DataFrame, quarters: pd.DataFrame) -> pd.DataFrame:
    """Carino-linked stock contributions versus the cap-weighted universe.

    The bars sum to the cumulative gap between the sector-pair book and the
    cap-weighted universe, not to the gap versus IWF.
    """
    scales = carino_scales(quarters["ret_port"].to_numpy(), quarters["ret_bench"].to_numpy())
    scale_map = dict(zip(pd.to_datetime(quarters["rebalance_date"]), scales))
    rows = []
    for start, block in panel.groupby("rebalance_date"):
        r_b = float((block["bench_weight"] * block["ret"]).sum())
        scale = float(scale_map[pd.Timestamp(start)])
        for row in block.itertuples(index=False):
            raw = (row.port_weight - row.bench_weight) * (row.ret - r_b)
            rows.append(
                {
                    "rebalance_date": start,
                    "ticker": row.ticker,
                    "sector": row.sector,
                    "port_weight": row.port_weight,
                    "bench_weight": row.bench_weight,
                    "active_weight": row.port_weight - row.bench_weight,
                    "ret": row.ret,
                    "contribution": raw,
                    "linked_contribution": scale * raw,
                    "selected": bool(row.selected),
                }
            )
    detail = pd.DataFrame(rows)
    summary = (
        detail.groupby("ticker")
        .agg(
            sector=("sector", "first"),
            linked_contribution=("linked_contribution", "sum"),
            avg_active_weight=("active_weight", "mean"),
            avg_port_weight=("port_weight", "mean"),
            quarters_held=("selected", "sum"),
            avg_return=("ret", "mean"),
        )
        .reset_index()
    )
    total = float(summary["linked_contribution"].sum())
    gap = float(np.prod(1.0 + quarters["ret_port"]) - np.prod(1.0 + quarters["ret_bench"]))
    if abs(total - gap) > 1e-6:
        raise RuntimeError(f"Linked stock contributions do not sum to the gap: {total} vs {gap}")
    return summary.sort_values("linked_contribution", ascending=False)


def sector_summary(sectors: pd.DataFrame, quarters: pd.DataFrame) -> pd.DataFrame:
    scales = carino_scales(quarters["ret_port"].to_numpy(), quarters["ret_bench"].to_numpy())
    s = sectors.copy()
    scale_map = dict(zip(pd.to_datetime(quarters["rebalance_date"]), scales))
    s["scale"] = pd.to_datetime(s["rebalance_date"]).map(scale_map)
    for col in ["allocation", "selection", "interaction"]:
        s[f"linked_{col}"] = s[col] * s["scale"]
    out = (
        s.groupby("sector")
        .agg(
            avg_w_port=("w_port", "mean"),
            avg_w_bench=("w_bench", "mean"),
            avg_active_weight=("active_weight", "mean"),
            linked_allocation=("linked_allocation", "sum"),
            linked_selection=("linked_selection", "sum"),
            linked_interaction=("linked_interaction", "sum"),
        )
        .reset_index()
    )
    out["linked_total"] = out["linked_allocation"] + out["linked_selection"] + out["linked_interaction"]
    return out.sort_values("linked_total", ascending=False)


def _style_ax(ax, title: str, xlabel: str, ylabel: str):
    ax.set_title(title, loc="left", fontsize=13, color="#1A1A1A", pad=10)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def make_charts(
    monthly: pd.DataFrame,
    quarters: pd.DataFrame,
    sectors_linked: pd.DataFrame,
    contributors: pd.DataFrame,
    style: dict,
    style_detail: pd.DataFrame,
) -> None:
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
    m = monthly.sort_values("date")
    start = pd.Timestamp(quarters["rebalance_date"].min()).strftime("%b %Y")
    end = pd.Timestamp(quarters["next_date"].max()).strftime("%b %Y")

    # 1. Growth of $1
    fig, ax = plt.subplots(figsize=(10.2, 6.0))
    series = [
        ("sector_pair", "Sector-pair book", NAVY),
        ("cap_weight", "Cap-weight universe", TEAL),
        ("equal_weight", "Equal-weight universe", GOLD),
        ("IWF", "IWF", BRONZE),
    ]
    for col, label, color in series:
        wealth = (1.0 + m[col]).cumprod()
        ax.plot(m["date"], wealth, label=label, color=color, lw=2.0)
    ax.legend(frameon=False)
    _style_ax(ax, f"Growth of $1, {start} to {end}", "", "Growth of $1 (monthly, total return)")
    fig.tight_layout()
    fig.savefig(IMAGES / "01_growth_of_one.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # 2. Relative wealth versus IWF
    fig, ax = plt.subplots(figsize=(10.2, 6.0))
    for col, label, color in series[:3]:
        rel = (1.0 + m[col]).cumprod() / (1.0 + m["IWF"]).cumprod()
        ax.plot(m["date"], rel, label=label, color=color, lw=2.0)
    ax.axhline(1.0, color=SLATE, lw=0.8)
    ax.legend(frameon=False)
    _style_ax(
        ax,
        "Wealth relative to IWF",
        "",
        "Growth of $1 in the book divided by growth of $1 in IWF",
    )
    fig.tight_layout()
    fig.savefig(IMAGES / "02_relative_wealth.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # 3. Quarterly Brinson effects versus the cap-weight universe
    fig, ax = plt.subplots(figsize=(10.2, 6.0))
    q = quarters.sort_values("rebalance_date")
    x = np.arange(len(q))
    labels = [pd.Timestamp(d).strftime("%Y-%m") for d in q["rebalance_date"]]
    a = q["allocation"].to_numpy() * 100
    s = q["selection"].to_numpy() * 100
    n = q["interaction"].to_numpy() * 100
    ax.bar(x, a, color=NAVY, width=0.72, label="Allocation")
    ax.bar(x, s, bottom=a, color=TEAL, width=0.72, label="Selection")
    ax.bar(x, n, bottom=a + s, color=GOLD, width=0.72, label="Interaction")
    ax.axhline(0, color=SLATE, lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=8)
    ax.legend(frameon=False, ncol=3)
    _style_ax(
        ax,
        "Quarterly attribution versus the cap-weight universe",
        "Rebalance date",
        "Contribution to the quarter's return gap (percentage points)",
    )
    fig.tight_layout()
    fig.savefig(IMAGES / "03_brinson_quarters.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # 4. Sector weights and linked allocation
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 5.8))
    sec = sectors_linked.sort_values("avg_active_weight")
    axes[0].barh(sec["sector"], sec["avg_w_port"] * 100, color=NAVY, height=0.38, label="Sector-pair")
    axes[0].barh(
        np.arange(len(sec)) + 0.38,
        sec["avg_w_bench"] * 100,
        color=GOLD,
        height=0.38,
        label="Cap-weight universe",
    )
    axes[0].set_yticks(np.arange(len(sec)) + 0.19)
    axes[0].set_yticklabels(sec["sector"])
    axes[0].legend(frameon=False, fontsize=9)
    _style_ax(axes[0], "Average sector weight", "Percent of the book", "")
    sec2 = sectors_linked.sort_values("linked_allocation")
    colors = [TEAL if v >= 0 else BRONZE for v in sec2["linked_allocation"]]
    axes[1].barh(sec2["sector"], sec2["linked_allocation"] * 100, color=colors, height=0.62)
    axes[1].axvline(0, color=SLATE, lw=0.8)
    _style_ax(
        axes[1],
        "Linked allocation effect",
        "Percentage points of the cumulative gap vs the cap-weight universe",
        "",
    )
    fig.tight_layout()
    fig.savefig(IMAGES / "04_sector_weights.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # 5. Active contributors
    fig, ax = plt.subplots(figsize=(10.2, 6.2))
    top = contributors.head(8)
    bot = contributors.tail(8)
    show = pd.concat([bot, top]).drop_duplicates("ticker")
    show = show.sort_values("linked_contribution")
    colors = [TEAL if v >= 0 else BRONZE for v in show["linked_contribution"]]
    ax.barh(show["ticker"], show["linked_contribution"] * 100, color=colors, height=0.72)
    ax.axvline(0, color=SLATE, lw=0.8)
    _style_ax(
        ax,
        "Largest linked active contributions versus the cap-weight universe",
        "Percentage points of the cumulative gap (Carino-linked)",
        "",
    )
    fig.tight_layout()
    fig.savefig(IMAGES / "05_active_contributors.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # 6. Style betas and the cumulative monthly gap
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 5.6))
    coefs = [c for c in style["coefficients"] if c["term"] != "intercept"]
    names = [c["term"].replace("_", "\n") for c in coefs]
    vals = [c["coef_monthly"] for c in coefs]
    colors = [TEAL if v >= 0 else BRONZE for v in vals]
    axes[0].bar(np.arange(len(vals)), vals, color=colors, width=0.7)
    axes[0].axhline(0, color=SLATE, lw=0.8)
    axes[0].set_xticks(np.arange(len(vals)))
    axes[0].set_xticklabels(names, fontsize=8)
    _style_ax(axes[0], "Style betas on the IWF gap", "", "Monthly excess return per unit of the factor")
    d = style_detail.sort_values("date")
    # The fitted value includes the intercept, so it ends at the same place
    # as the actual gap by construction. The line that tests the spreads is
    # the fitted value with the intercept removed.
    factor_only = d["fitted_excess"] - style["alpha_monthly"]
    axes[1].plot(d["date"], d["excess_vs_iwf"].cumsum() * 100, color=NAVY, lw=2.0, label="Actual gap")
    axes[1].plot(
        d["date"],
        factor_only.cumsum() * 100,
        color=GOLD,
        lw=2.0,
        label="Four spreads, intercept removed",
    )
    axes[1].axhline(0, color=SLATE, lw=0.8)
    axes[1].legend(frameon=False, fontsize=9)
    _style_ax(
        axes[1],
        "Sum of monthly gaps versus IWF",
        "",
        "Sum of monthly return differences (percentage points)",
    )
    fig.tight_layout()
    fig.savefig(IMAGES / "06_style_fit.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def assert_market_cap_scale(mcap: pd.DataFrame) -> None:
    """Stop if a split-scale bug has blown up two well-known market caps.

    The bands are wide on purpose. They are public facts about size, not
    targets for the book. NVIDIA at the end of September 2021 was a few
    hundred billion dollars, not a few trillion, because the 2024 split
    had not happened. By September 2026 it was several trillion.
    """
    def grab(ticker: str, month: str) -> float:
        row = mcap[(mcap["ticker"] == ticker) & (mcap["month_end"] == pd.Timestamp(month))]
        if row.empty or not np.isfinite(row["market_cap"].iloc[0]):
            raise RuntimeError(f"Missing market cap for {ticker} on {month}")
        return float(row["market_cap"].iloc[0])

    checks = [
        ("NVDA", "2021-09-30", 3.5e11, 8.0e11),
        ("NVDA", "2026-09-30", 4.0e12, 7.0e12),
        ("AAPL", "2021-09-30", 1.8e12, 3.0e12),
        ("AAPL", "2026-09-30", 4.0e12, 6.0e12),
    ]
    for ticker, month, lo, hi in checks:
        value = grab(ticker, month)
        if not (lo < value < hi):
            raise RuntimeError(f"{ticker} market cap on {month} is {value:.3e}, outside {lo:.3e} to {hi:.3e}")


def save_database(
    snap: pd.DataFrame,
    adj_m: pd.DataFrame,
    mcap: pd.DataFrame,
    panel: pd.DataFrame,
    sectors: pd.DataFrame,
    sectors_linked: pd.DataFrame,
    quarters: pd.DataFrame,
    monthly: pd.DataFrame,
    contributors: pd.DataFrame,
) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        snap.to_sql("universe", conn, index=False)
        long_px = (
            adj_m.reset_index(names="month_end")
            .melt(id_vars="month_end", var_name="ticker", value_name="adj_close")
            .dropna()
        )
        # Keep the coverage universe and the ETFs used in the study.
        keep = set(snap["ticker"]).union(FACTOR_ETFS)
        long_px = long_px[long_px["ticker"].isin(keep)]
        long_px.to_sql("prices_monthly", conn, index=False)
        mcap.to_sql("market_cap_monthly", conn, index=False)
        hold_cols = [
            "rebalance_date",
            "next_date",
            "ticker",
            "sector",
            "market_cap",
            "source",
            "shares_asof",
            "rank_in_sector",
            "selected",
            "port_weight",
            "bench_weight",
            "eq_weight",
            "ret",
        ]
        panel[hold_cols].to_sql("holdings", conn, index=False)
        sectors.to_sql("sector_attribution", conn, index=False)
        sectors_linked.to_sql("sector_linked", conn, index=False)
        quarters.to_sql("quarterly_returns", conn, index=False)
        monthly.to_sql("monthly_returns", conn, index=False)
        contributors.to_sql("contributors", conn, index=False)
        conn.executescript(
            """
            CREATE INDEX idx_prices_ticker_month ON prices_monthly(ticker, month_end);
            CREATE INDEX idx_holdings_date ON holdings(rebalance_date, ticker);
            CREATE INDEX idx_sector_date ON sector_attribution(rebalance_date, sector);
            CREATE INDEX idx_mcap_ticker_month ON market_cap_monthly(ticker, month_end);
            """
        )
    finally:
        conn.close()


def run_sql(sql: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(sql, conn)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        if not np.isfinite(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return str(obj.date())
    return obj


def data_quality(mcap: pd.DataFrame, snap: pd.DataFrame, panel: pd.DataFrame) -> dict:
    last = mcap.sort_values("month_end").groupby("ticker").tail(1).set_index("ticker")
    yahoo = snap.set_index("ticker")["market_cap"]
    ratio = (last["market_cap"] / yahoo.reindex(last.index)).replace([np.inf, -np.inf], np.nan).dropna()
    sources = mcap.groupby("ticker")["level_source"].first()
    return {
        "asof": asof_date(),
        "n_universe": int(snap["ticker"].nunique()),
        "n_sectors": int(snap["sector"].nunique()),
        "sectors": sorted(snap["sector"].dropna().unique().tolist()),
        "n_quarters": int(panel["rebalance_date"].nunique()),
        "first_rebalance": str(pd.Timestamp(panel["rebalance_date"].min()).date()),
        "last_rebalance": str(pd.Timestamp(panel["rebalance_date"].max()).date()),
        "last_month": str(pd.Timestamp(panel["next_date"].max()).date()),
        "n_price_scaled_fallback": int((sources == "price_scaled_snapshot").sum()),
        "fallback_tickers": sorted(sources[sources == "price_scaled_snapshot"].index.tolist()),
        "n_months_with_share_factor": int((mcap["share_factor"].fillna(1.0) - 1.0).abs().gt(1e-9).sum()),
        "n_months_with_spinoff_factor": int((mcap["price_factor"].fillna(1.0) - 1.0).abs().gt(1e-9).sum()),
        "median_mcap_ratio_vs_yahoo": float(ratio.median()) if len(ratio) else None,
        "min_mcap_ratio_vs_yahoo": float(ratio.min()) if len(ratio) else None,
        "max_mcap_ratio_vs_yahoo": float(ratio.max()) if len(ratio) else None,
    }


def build_summary(
    quality: dict,
    perf: pd.DataFrame,
    quarters: pd.DataFrame,
    sectors_linked: pd.DataFrame,
    contributors: pd.DataFrame,
    style: dict,
    turnover: pd.DataFrame,
    costs: dict,
    snap: pd.DataFrame,
    panel: pd.DataFrame,
) -> dict:
    q = quarters.copy()
    scales_bench = carino_scales(q["ret_port"].to_numpy(), q["ret_bench"].to_numpy())
    linked_bench = {
        "allocation": float(np.sum(scales_bench * q["allocation"])),
        "selection": float(np.sum(scales_bench * q["selection"])),
        "interaction": float(np.sum(scales_bench * q["interaction"])),
        "cumulative_gap": float(np.prod(1.0 + q["ret_port"]) - np.prod(1.0 + q["ret_bench"])),
    }
    # Same Brinson pieces, rescaled so they add up to the cumulative gap versus IWF
    # together with the cap-weight-versus-IWF residual. The within-universe
    # effects are not re-estimated. Only the linking changes.
    scales_iwf = carino_scales(q["ret_port"].to_numpy(), q["ret_iwf"].to_numpy())
    linked_iwf = {
        "allocation": float(np.sum(scales_iwf * q["allocation"])),
        "selection": float(np.sum(scales_iwf * q["selection"])),
        "interaction": float(np.sum(scales_iwf * q["interaction"])),
        "cap_weight_vs_iwf": float(np.sum(scales_iwf * q["bench_vs_iwf"])),
    }
    linked_iwf["sum"] = sum(linked_iwf.values())
    gap_iwf = float(np.prod(1.0 + q["ret_port"]) - np.prod(1.0 + q["ret_iwf"]))
    if abs(linked_iwf["sum"] - gap_iwf) > 1e-6:
        raise RuntimeError("IWF link does not add up.")

    # Who was held, and when the membership changed.
    held = panel.loc[panel["selected"], ["rebalance_date", "ticker", "sector", "market_cap"]].copy()
    membership = (
        held.groupby("ticker")
        .agg(
            sector=("sector", "first"),
            quarters_held=("rebalance_date", "nunique"),
            first_held=("rebalance_date", "min"),
            last_held=("rebalance_date", "max"),
        )
        .reset_index()
        .sort_values(["sector", "quarters_held", "ticker"], ascending=[True, False, True])
    )
    membership["first_held"] = membership["first_held"].map(lambda d: str(pd.Timestamp(d).date()))
    membership["last_held"] = membership["last_held"].map(lambda d: str(pd.Timestamp(d).date()))

    last_q = pd.Timestamp(q["rebalance_date"].max())
    last_book = (
        panel.loc[(panel["rebalance_date"] == last_q) & (panel["selected"]), ["ticker", "sector", "port_weight", "bench_weight", "market_cap"]]
        .sort_values(["sector", "ticker"])
    )

    # Average quarterly effects, which are easier to say out loud than the linked total.
    avg_effects = {
        "allocation": float(q["allocation"].mean()),
        "selection": float(q["selection"].mean()),
        "interaction": float(q["interaction"].mean()),
        "excess_vs_bench": float(q["excess_vs_bench"].mean()),
        "excess_vs_iwf": float(q["excess_vs_iwf"].mean()),
        "bench_vs_iwf": float(q["bench_vs_iwf"].mean()),
    }
    # Descriptive t-stat on the quarterly excess versus IWF and versus the proxy.
    def _t(series: pd.Series) -> float:
        n = len(series)
        sd = float(series.std(ddof=1))
        if n < 2 or sd == 0:
            return np.nan
        return float(series.mean() / (sd / np.sqrt(n)))

    summary = {
        "quality": quality,
        "performance": perf.to_dict(orient="records"),
        "linked_vs_cap_weight": linked_bench,
        "linked_vs_iwf": linked_iwf,
        "cumulative_gap_vs_iwf": gap_iwf,
        "avg_quarterly_effects": avg_effects,
        "tstat_excess_vs_cap": _t(q["excess_vs_bench"]),
        "tstat_excess_vs_iwf": _t(q["excess_vs_iwf"]),
        "quarters_beat_cap": int((q["excess_vs_bench"] > 0).sum()),
        "quarters_beat_iwf": int((q["excess_vs_iwf"] > 0).sum()),
        "n_quarters": int(len(q)),
        "sector_linked": sectors_linked.to_dict(orient="records"),
        "top_contributors": contributors.head(8).to_dict(orient="records"),
        "bottom_contributors": contributors.tail(8).sort_values("linked_contribution").to_dict(orient="records"),
        "style": {k: v for k, v in style.items() if k != "coefficients"},
        "style_coefficients": style["coefficients"],
        "turnover": {
            "avg_one_way": float(turnover["one_way_turnover"].mean(skipna=True)),
            "avg_active_share": float(turnover["active_share"].mean()),
            "names_per_quarter": int(turnover["n_names"].mode().iloc[0]),
        },
        "costs": costs,
        "membership": membership.to_dict(orient="records"),
        "last_book": last_book.assign(
            rebalance_date=str(last_q.date()),
        ).to_dict(orient="records"),
        "iwf_top_holdings": pd.read_csv(RAW / "iwf_top_holdings.csv").to_dict(orient="records"),
        "iwf_sector_weights": pd.read_csv(RAW / "iwf_sector_weights.csv").to_dict(orient="records"),
    }
    return _jsonable(summary)


def run(force_download: bool = False) -> dict:
    download_raw(force=force_download)
    snap = load_snapshot()
    # The coverage list is U.S. issuers. The snapshot is whatever Yahoo returned
    # for those tickers. Drop nothing here: the CSV is the universe.
    tickers = universe_tickers()
    snap = snap[snap["ticker"].isin(tickers)].copy()
    if len(snap) != len(tickers):
        raise RuntimeError("Snapshot does not cover the universe.")

    adj = load_prices("adjusted")
    unadj = load_prices("unadjusted")
    shares = load_shares()
    adj_m, trade_day = month_end_frame(adj)
    unadj_m, trade_day_u = month_end_frame(unadj)
    # Use the adjusted-file calendar. Unadjusted prices must cover the same months.
    trade_day = trade_day.reindex(adj_m.index)
    unadj_m = unadj_m.reindex(adj_m.index)
    splits = load_splits()
    mcap = build_market_caps(unadj_m, trade_day, shares, snap, splits)
    assert_market_cap_scale(mcap)
    if (DATA / "mcap_repairs.csv").exists():
        (DATA / "mcap_repairs.csv").unlink()

    panel = build_quarter_panel(adj_m, mcap, snap)
    sectors, quarters = brinson_fachler(panel)
    quarters = attach_iwf(quarters, adj_m)
    monthly = build_monthly(panel, adj_m)
    # Compounded monthly IWF must match the quarterly IWF return.
    m = monthly.set_index("date")
    for row in quarters.itertuples(index=False):
        start = pd.Timestamp(row.rebalance_date)
        end = pd.Timestamp(row.next_date)
        path = m.loc[(m.index > start) & (m.index <= end), "IWF"]
        compounded = float(np.prod(1.0 + path.to_numpy()) - 1.0)
        if abs(compounded - row.ret_iwf) > 1e-8:
            raise RuntimeError(f"IWF monthly path mismatch on {start.date()}")

    perf = performance_table(monthly, quarters)
    turnover = turnover_and_active_share(panel)
    costs = cost_sensitivity(quarters, turnover)
    style, style_detail = style_regression(monthly)
    contributors = linked_contributors(panel, quarters)
    sectors_linked = sector_summary(sectors, quarters)
    quality = data_quality(mcap, snap, panel)
    make_charts(monthly, quarters, sectors_linked, contributors, style, style_detail)
    save_database(snap, adj_m, mcap, panel, sectors, sectors_linked, quarters, monthly, contributors)

    perf.to_csv(DATA / "performance_stats.csv", index=False)
    quarters.to_csv(DATA / "quarterly_attribution.csv", index=False)
    sectors.to_csv(DATA / "sector_attribution.csv", index=False)
    sectors_linked.to_csv(DATA / "sector_linked.csv", index=False)
    contributors.to_csv(DATA / "contributors.csv", index=False)
    monthly.to_csv(DATA / "monthly_returns.csv", index=False)
    turnover.to_csv(DATA / "turnover.csv", index=False)
    panel.to_csv(DATA / "holdings.csv", index=False)

    summary = build_summary(
        quality, perf, quarters, sectors_linked, contributors, style, turnover, costs, snap, panel
    )
    (DATA / "results_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ["quality", "performance", "linked_vs_cap_weight", "linked_vs_iwf", "avg_quarterly_effects", "style"]}, indent=2))
    return summary


if __name__ == "__main__":
    run(force_download=False)
