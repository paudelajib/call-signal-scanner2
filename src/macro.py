"""Macro data: Treasury yields, breakeven inflation, Fed funds, CPI (FRED) and VIX (Yahoo).

Source order per series: FRED CSV  ->  US Treasury daily yield-curve CSVs (nominal + real
yields; 10Y breakeven = nominal 10Y - real 10Y)  ->  Yahoo (^TNX for 10Y, ^IRX 13-week bill as a
stand-in for Fed funds). Every fallback is real published data; the source used is recorded.

Stored as one wide daily parquet. Monthly CPI is kept for display only (it is published with a
lag, so using it in scores would leak future information); the scoring uses the daily
10-year breakeven inflation rate (T10YIE) as the market's real-time inflation expectation.
"""
from __future__ import annotations

import io
import time
from typing import Dict

import pandas as pd
import requests
import yfinance as yf

from .config import Paths, ns

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "daily-treasury-rates.csv/{year}/all")
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Yahoo fallbacks if FRED and Treasury are unreachable (yields in percent)
YAHOO_FALLBACK = {"DGS10": "^TNX", "DFF": "^IRX"}


def _get(url: str, params: dict, tries: int = 2, timeout: int = 20) -> requests.Response:
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"})
            if r.status_code == 200 and r.text.strip():
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        if k + 1 < tries:
            time.sleep(2)
    raise last or RuntimeError("no response")


def _fred(series_id: str, start: str) -> pd.Series:
    r = _get(FRED_URL, {"id": series_id, "cosd": start})
    df = pd.read_csv(io.StringIO(r.text))
    date_col = df.columns[0]
    val_col = series_id if series_id in df.columns else df.columns[-1]
    s = pd.to_numeric(df[val_col], errors="coerce")
    s.index = ns(pd.DatetimeIndex(df[date_col]))
    return s[s.index >= pd.Timestamp(start)].dropna().rename(series_id)


def _treasury_curve(kind: str, start: str) -> pd.DataFrame:
    """kind: 'daily_treasury_yield_curve' (nominal) or 'daily_treasury_real_yield_curve'."""
    frames = []
    years = list(range(pd.Timestamp(start).year, pd.Timestamp.now().year + 1))
    for i, year in enumerate(reversed(years)):  # newest first; bail out fast if unreachable
        try:
            r = _get(TREASURY_URL.format(year=year),
                     {"type": kind, "field_tdr_date_value": year, "page": "", "_format": "csv"},
                     tries=1)
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip().lower() for c in df.columns]
            df.index = ns(pd.DatetimeIndex(pd.to_datetime(df["date"], format="mixed")))
            frames.append(df.drop(columns=["date"]).apply(pd.to_numeric, errors="coerce"))
        except Exception:
            if i == 0:  # the current year failed: the site is unreachable, don't try 12 more
                break
            continue
    if not frames:
        raise RuntimeError("treasury.gov unreachable")
    return pd.concat(frames).sort_index()


def _treasury_series(start: str) -> Dict[str, pd.Series]:
    out: Dict[str, pd.Series] = {}
    nom = _treasury_curve("daily_treasury_yield_curve", start)
    if "10 yr" in nom:
        out["DGS10"] = nom["10 yr"].dropna().rename("DGS10")
    if "2 yr" in nom:
        out["DGS2"] = nom["2 yr"].dropna().rename("DGS2")
    try:
        real = _treasury_curve("daily_treasury_real_yield_curve", start)
        if "10 yr" in real and "DGS10" in out:
            be = (out["DGS10"] - real["10 yr"]).dropna()
            out["T10YIE"] = be.rename("T10YIE")
    except Exception:
        pass
    return out


def _yahoo_close(symbol: str, start: str) -> pd.Series:
    raw = yf.download(symbol, start=start, interval="1d", auto_adjust=False, progress=False,
                      multi_level_index=False, timeout=30)
    if raw is None or raw.empty:
        return pd.Series(dtype=float)
    s = raw["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.astype(float)
    idx = pd.DatetimeIndex(s.index)
    s.index = ns((idx.tz_localize(None) if idx.tz is not None else idx).normalize())
    return s


def update_macro(cfg: Dict, paths: Paths) -> Dict[str, str]:
    start = cfg["history"]["start"]
    cols, status = {}, {}
    treasury: Dict[str, pd.Series] | None = None
    fred_down = False
    for sid in cfg["macro"]["fred_series"]:
        err: Exception = RuntimeError("FRED unreachable")
        if not fred_down:
            try:
                cols[sid] = _fred(sid, start)
                status[sid] = "fred"
                continue
            except Exception as e:
                err = e
                if not isinstance(e, RuntimeError) or "HTTP" not in str(e):
                    fred_down = True  # timeout / connection error: skip FRED for the rest
        if sid in ("DGS10", "DGS2", "T10YIE"):
            if treasury is None:
                try:
                    treasury = _treasury_series(start)
                except Exception:
                    treasury = {}
            if sid in treasury and len(treasury[sid]):
                cols[sid] = treasury[sid]
                status[sid] = "treasury.gov"
                continue
        fb = YAHOO_FALLBACK.get(sid)
        if fb:
            try:
                ys = _yahoo_close(fb, start)
                if len(ys):
                    cols[sid] = ys.rename(sid)
                    status[sid] = f"yahoo {fb}"
                    continue
            except Exception:
                pass
        status[sid] = f"failed: {err}"
    for sym, name in cfg["macro"].get("yahoo_series", {}).items():
        try:
            cols[name] = _yahoo_close(sym, start).rename(name)
            status[name] = "yahoo"
        except Exception as e:
            status[name] = f"failed: {e}"

    if not cols:
        return status
    for k in list(cols):
        cols[k] = cols[k][~cols[k].index.duplicated(keep="last")]
    new = pd.concat(cols.values(), axis=1).sort_index()
    if paths.macro.exists():  # keep any history we had if a source failed today
        old = pd.read_parquet(paths.macro)
        new = new.combine_first(old)
    new.index.name = "date"
    new.to_parquet(paths.macro)
    return status


def load_macro(paths: Paths) -> pd.DataFrame:
    if not paths.macro.exists():
        return pd.DataFrame()
    df = pd.read_parquet(paths.macro)
    df.index = ns(df.index)
    return df.sort_index()


def macro_snapshot(macro: pd.DataFrame, trend_days: int = 63) -> Dict[str, Dict[str, float]]:
    """Latest level + change over the trend window, for the UI header."""
    out = {}
    for c in macro.columns:
        s = macro[c].dropna()
        if s.empty:
            continue
        last = float(s.iloc[-1])
        prev_date = s.index[-1] - pd.Timedelta(days=int(trend_days * 365 / 252))
        prev = s[s.index <= prev_date]
        chg = float(last - prev.iloc[-1]) if not prev.empty else None
        if c == "CPIAUCSL":  # show CPI as year-over-year inflation
            yago = s[s.index <= s.index[-1] - pd.Timedelta(days=365)]
            if not yago.empty:
                out["CPI_YOY"] = {"value": (last / float(yago.iloc[-1]) - 1) * 100,
                                  "change": None, "asof": str(s.index[-1].date())}
            continue
        out[c] = {"value": last, "change": chg, "asof": str(s.index[-1].date())}
    return out
