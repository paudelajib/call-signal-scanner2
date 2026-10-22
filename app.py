"""Call Signal Scanner v2 - local web app.

    python app.py            ->  open http://127.0.0.1:8050
"""
from __future__ import annotations

import argparse
import math
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from src import engine, news, prices
from src.backtest import load_backtest, run_backtest
from src.config import PROJECT_ROOT, Paths, load_config
from src.features import FEATURE_INFO
from src.fundamentals import load_info
from src.macro import load_macro, macro_snapshot
from src.scoring import load_weights, save_weights
from src.universe import load_universe

app = Flask(__name__, static_folder=str(PROJECT_ROOT / "web"), static_url_path="/static")
app.json.sort_keys = False  # keep horizon / feature / period order as defined


# --------------------------------------------------------------------------- background jobs
class Job:
    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"running": False, "stage": None, "done": 0, "total": 0,
                                      "error": None, "started": None, "finished": None,
                                      "result": None}

    def progress(self, stage: str, done: int, total: int):
        with self.lock:
            self.state.update(stage=stage, done=done, total=total)

    def start(self, fn, **kw) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, stage="starting", done=0, total=0, error=None,
                              started=datetime.now().isoformat(timespec="seconds"),
                              finished=None, result=None)

        def run():
            try:
                res = fn(progress=self.progress, **kw)
                with self.lock:
                    self.state["result"] = _json_safe(res)
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.state["error"] = f"{type(e).__name__}: {e}"
            finally:
                with self.lock:
                    self.state["running"] = False
                    self.state["finished"] = datetime.now().isoformat(timespec="seconds")
                _cache.clear()

        threading.Thread(target=run, daemon=True).start()
        return True

    def snapshot(self):
        with self.lock:
            return dict(self.state)


JOBS = {"refresh": Job("refresh"), "backtest": Job("backtest")}


# --------------------------------------------------------------------------- helpers
class _Cache(dict):
    pass


_cache = _Cache()


def _json_safe(x):
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.isoformat()
    if x is pd.NA or x is pd.NaT:
        return None
    return x


def _df_records(df: pd.DataFrame):
    return [_json_safe(r) for r in df.to_dict(orient="records")]


def ctx():
    cfg = load_config()
    return cfg, Paths(cfg)


def _prices_long(paths: Paths) -> pd.DataFrame:
    key = ("prices", paths.prices.stat().st_mtime if paths.prices.exists() else 0)
    if key not in _cache:
        _cache.clear() if len(_cache) > 20 else None
        _cache[key] = prices.load_prices(paths)
    return _cache[key]


def _recent_scores(cfg, paths):
    w = load_weights()
    mt = paths.features_recent.stat().st_mtime if paths.features_recent.exists() else 0
    key = ("recent", mt, str(sorted((h, tuple(sorted(v.items()))) for h, v in w.items())))
    if key not in _cache:
        _cache[key] = engine.recent_scores(cfg, paths, w)
    return _cache[key]


# --------------------------------------------------------------------------- pages
@app.get("/vendor/plotly.min.js")
def plotly_js():
    """Serve plotly.js from the installed `plotly` Python package (works offline)."""
    import plotly
    from pathlib import Path
    return send_from_directory(Path(plotly.__file__).parent / "package_data", "plotly.min.js",
                               max_age=86400)


@app.get("/")
def index():
    return send_from_directory(PROJECT_ROOT / "web", "index.html")


@app.get("/api/meta")
def meta():
    cfg, paths = ctx()
    st = paths.read_state()
    mac = load_macro(paths)
    snap = macro_snapshot(mac, int(cfg["macro"].get("trend_window_days", 63))) if not mac.empty else {}
    bench = None
    if paths.prices.exists():
        pl = _prices_long(paths)
        b = pl[pl["ticker"] == cfg["universe"].get("benchmark", "SPY")].sort_values("date")
        if len(b) > 1:
            c = b["close"].values
            bench = {"ticker": cfg["universe"].get("benchmark", "SPY"), "close": c[-1],
                     "chg_1d": c[-1] / c[-2] - 1,
                     "vs_200d": c[-1] / pd.Series(c).ewm(span=200, adjust=False).mean().iloc[-1] - 1}
    uni = load_universe(cfg, paths) if paths.universe.exists() else pd.DataFrame()
    return jsonify(_json_safe({
        "horizons": cfg["horizons"], "last_bar": st.get("last_bar"),
        "last_refresh": st.get("last_refresh"), "refresh_log": st.get("refresh_log"),
        "macro": snap, "benchmark": bench, "features": FEATURE_INFO,
        "sectors": sorted(uni["sector"].dropna().unique().tolist()) if not uni.empty else [],
        "has_data": paths.scores_latest.exists(),
        "news_model": cfg["news"].get("model", "vader"),
    }))


@app.get("/api/ranking")
def ranking():
    cfg, paths = ctx()
    rk = engine.load_ranking(paths)
    if rk.empty:
        return jsonify({"rows": [], "date": None})
    h = request.args.get("horizon", "medium")
    col = f"score_{h}" if f"score_{h}" in rk else "score_medium"
    rk = rk.sort_values(col, ascending=False, na_position="last")
    keep = ["ticker", "name", "sector", "close", "chg_1d", "pe", "fwd_pe", "sector_pe", "pe_rel",
            "score_short", "score_medium", "score_long", "news_sentiment", "market_cap", "mom_3m",
            "rsi14"]
    rk = rk[[c for c in keep if c in rk.columns]]
    date = None
    if paths.scores_latest.exists():
        d = pd.read_parquet(paths.scores_latest, columns=["date"])["date"]
        date = str(pd.Timestamp(d.iloc[0]).date()) if len(d) else None
    return jsonify({"rows": _df_records(rk), "date": date, "sorted_by": col})


@app.get("/api/stock/<ticker>")
def stock(ticker: str):
    cfg, paths = ctx()
    t = ticker.strip().upper().replace(".", "-")
    rk = engine.load_ranking(paths)
    if rk.empty:
        return jsonify({"error": "No data yet. Run Refresh data first."}), 404
    if t not in set(rk["ticker"]):
        # allow lookup by company name
        m = rk[rk["name"].str.lower().str.contains(ticker.strip().lower(), na=False, regex=False)]
        if m.empty:
            return jsonify({"error": f"{ticker} isn't in the S&P 500 list this app tracks."}), 404
        t = m.iloc[0]["ticker"]
    row = rk[rk["ticker"] == t].iloc[0].to_dict()

    # price history (2y) + EMAs
    pl = _prices_long(paths)
    p = pl[pl["ticker"] == t].sort_values("date").tail(520)
    c = p["close"]
    full_close = pl[pl["ticker"] == t].sort_values("date")["close"]
    e50 = full_close.ewm(span=50, adjust=False).mean().reindex(c.index)
    e200 = full_close.ewm(span=200, adjust=False).mean().reindex(c.index)
    price = {"dates": [str(d.date()) for d in p["date"]], "close": c.round(4).tolist(),
             "open": p["open"].round(4).tolist(), "high": p["high"].round(4).tolist(),
             "low": p["low"].round(4).tolist(), "volume": p["volume"].fillna(0).tolist(),
             "ema50": e50.round(4).tolist(), "ema200": e200.round(4).tolist()}
    rets = {}
    for lab, n in [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("1Y", 252)]:
        rets[lab] = float(full_close.iloc[-1] / full_close.iloc[-1 - n] - 1) if len(full_close) > n else None

    # scores: history + contributions with current weights
    rs = _recent_scores(cfg, paths)
    weights = load_weights()
    hist, contrib = {}, {}
    for h, d in rs.items():
        s = d["score"][t] if t in d["score"].columns else None
        if s is not None:
            hist[h] = {"dates": [str(x.date()) for x in s.index], "values": s.round(4).tolist()}
        parts = []
        for f, pdf in d["parts"].items():
            if t in pdf.columns:
                parts.append({"feature": f, "weight": weights[h].get(f),
                              "contribution": pdf[t].iloc[-1], "desc": FEATURE_INFO.get(f, f)})
        parts.sort(key=lambda x: -abs(x["contribution"] or 0))
        contrib[h] = parts

    raw = pd.read_parquet(paths.features_latest).set_index("ticker")
    rr = raw.loc[t] if t in raw.index else pd.Series(dtype=float)
    z = engine.load_recent_z(paths)
    zt = {f: z[f][t].iloc[-1] for f in z if t in z[f].columns}
    feature_table = [{"feature": f, "desc": FEATURE_INFO[f], "raw": rr.get(f), "z": zt.get(f)}
                     for f in FEATURE_INFO]

    info = load_info(paths)
    inf = info[info["ticker"] == t].iloc[0].to_dict() if not info.empty and t in set(info["ticker"]) else {}

    # sector peers on valuation
    peers = rk[rk["sector"] == row.get("sector")][["ticker", "name", "pe", "fwd_pe", "score_medium",
                                                 "score_long"]].sort_values("pe")
    heads = news.headlines(paths, t, int(cfg["news"].get("lookback_days", 7)))
    return jsonify(_json_safe({
        "ticker": t, "row": row, "price": price, "returns": rets, "score_history": hist,
        "contributions": contrib, "features": feature_table, "info": inf,
        "macro": {"beta_mkt": rr.get("_beta_mkt"), "beta_rate": rr.get("_beta_rate"),
                  "beta_infl": rr.get("_beta_infl"), "corr_rate": rr.get("_corr_rate")},
        "peers": _df_records(peers), "headlines": heads,
    }))


@app.post("/api/news/<ticker>")
def news_now(ticker: str):
    cfg, paths = ctx()
    uni = load_universe(cfg, paths)
    t = ticker.upper()
    if t not in set(uni["ticker"]):
        return jsonify({"error": "unknown ticker"}), 404
    res = news.update_news(uni, cfg, paths, priority=[t], only=[t], force=True)
    try:
        engine.compute_live(cfg, paths)
    except Exception as e:
        return jsonify({"ok": True, "warning": str(e), **res})
    _cache.clear()
    return jsonify({"ok": True, **_json_safe(res)})


@app.post("/api/refresh")
def refresh():
    body = request.get_json(silent=True) or {}
    ok = JOBS["refresh"].start(engine.run_refresh,
                               fundamentals_force=bool(body.get("fundamentals_force")),
                               skip_news=bool(body.get("skip_news")))
    return jsonify({"started": ok, **JOBS["refresh"].snapshot()})


@app.get("/api/jobs")
def jobs():
    return jsonify({k: _json_safe(j.snapshot()) for k, j in JOBS.items()})


@app.get("/api/weights")
def get_weights():
    cfg, paths = ctx()
    bt = load_backtest(paths) or {}
    fic = {h: {f: (v or {}).get("mean") for f, v in d.get("feature_ic", {}).items()}
           for h, d in bt.get("horizons", {}).items()}
    learned = {h: d.get("learned_weights", {}) for h, d in bt.get("horizons", {}).items()}
    return jsonify(_json_safe({"weights": load_weights(), "features": FEATURE_INFO,
                               "horizons": cfg["horizons"], "feature_ic": fic,
                               "learned": learned}))


@app.post("/api/weights")
def set_weights():
    cfg, paths = ctx()
    body = request.get_json(force=True)
    w = body.get("weights")
    if not isinstance(w, dict) or not all(h in w for h in cfg["horizons"]):
        return jsonify({"error": "weights must contain every horizon"}), 400
    for h, fw in w.items():
        for f, v in fw.items():
            if f not in FEATURE_INFO:
                return jsonify({"error": f"unknown feature {f}"}), 400
            float(v)
    save_weights(w)
    _cache.clear()
    if paths.features_recent.exists():
        engine.rescore(cfg, paths)
    return jsonify({"ok": True})


@app.post("/api/backtest")
def backtest_start():
    ok = JOBS["backtest"].start(lambda progress: run_backtest(progress))
    return jsonify({"started": ok, **JOBS["backtest"].snapshot()})


@app.get("/api/backtest")
def backtest_get():
    _, paths = ctx()
    return jsonify(load_backtest(paths) or {})


# --------------------------------------------------------------------------- auto refresh
def expected_last_session(now_et: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    """Date of the most recent US session whose daily bar should be final by now
    (weekday and at least 16:20 New York time; holidays just cause a harmless retry)."""
    now_et = now_et or pd.Timestamp.now(tz="America/New_York")
    d = now_et.normalize().tz_localize(None)
    if not (d.weekday() < 5 and now_et.hour * 60 + now_et.minute >= 16 * 60 + 20):
        d -= pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def _auto_refresh_loop(check_every: int = 300, retry_minutes: int = 45):
    last_try: Optional[float] = None
    while True:
        try:
            _, paths = ctx()
            st = paths.read_state()
            last_bar = pd.Timestamp(st["last_bar"]) if st.get("last_bar") else None
            want = expected_last_session()
            stale = last_bar is None or last_bar < want
            due = last_try is None or time.time() - last_try > retry_minutes * 60
            if stale and due and not JOBS["refresh"].snapshot()["running"]:
                print(f"[auto] data ends {last_bar.date() if last_bar is not None else None}, "
                      f"expected {want.date()} -> refreshing", flush=True)
                last_try = time.time()
                JOBS["refresh"].start(engine.run_refresh)
        except Exception:
            traceback.print_exc()
        time.sleep(check_every)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't refresh automatically after the US market close")
    a = ap.parse_args()
    if not a.no_auto:
        threading.Thread(target=_auto_refresh_loop, daemon=True).start()
    print(f"Call Signal Scanner running at http://{a.host}:{a.port}"
          + ("" if a.no_auto else "  (auto-refresh after the US close while this is running)"))
    app.run(host=a.host, port=a.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
