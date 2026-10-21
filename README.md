# Signal Scanner v2: S&P 500 buy scores on daily data

A local web app that ranks all S&P 500 stocks by three **buy scores from −1 to +1**:

| Score | Horizon | Leans on |
|---|---|---|
| **1 Week** | next 5 trading days | short-term reversal, RSI, volume surges, MACD, headline sentiment |
| **1 Month** | next 21 trading days | 3- and 12-month momentum, trend, 52-week-high proximity, sector-relative strength, news |
| **6 Months** | next 126 trading days | 12-1 momentum, **sector-relative P/E**, low volatility, rate & inflation sensitivity |

Everything runs on **real daily market data**: prices from Yahoo Finance, yields and breakeven inflation from FRED, P/E history from Yahoo's quarterly valuation tables, and headlines from Google News, Yahoo and (optionally) NewsAPI. Nothing is simulated. The synthetic data in `tests/` exists only to check that the code runs.

## Live demo

A read-only copy updates automatically after each U.S. market close: **https://paudelajib.github.io/call-signal-scanner2/**
(built by `.github/workflows/pages.yml` with `scripts/export_static.py`; Refresh / weights editing are local-only).

## Quick start

```bash
conda create -n scanner python=3.12 -y && conda activate scanner
pip install -r requirements.txt
python app.py                # then open http://127.0.0.1:8050
```

Press **Refresh data**. The first run downloads about 12 years of history for ~500 stocks and takes roughly 10–20 minutes. After that, each refresh only appends the new days, re-pulls headlines and re-ranks, which takes a few minutes.

Optional: put a NewsAPI key in `.env` as `NEWSAPI_KEY=...`. Google News and Yahoo headlines work without any key.

## What the app shows

- **Ranking**: all 500 stocks sorted by the chosen horizon. The bar glyph shows all three scores at once (right = bullish, left = bearish). Filter by sector or text and click any column to sort.
- **Stock detail** (click a row or search by ticker or company name):
  - the three scores, and which features pushed each one up or down
  - a 2-year price chart with the 50- and 200-day averages, plus score history
  - P/E and forward P/E against the **median of its own GICS sector**, with a list of peers
  - rate and inflation sensitivity: how the stock has moved per +0.25pp in the 10-year yield and in breakeven inflation, after removing the market's move
  - fundamentals, analyst targets, and the latest headlines with per-headline sentiment, newest first
- **Backtest**: walk-forward test over real history (see below).
- **Weights**: edit every feature's weight for every horizon. Saving re-scores instantly with no downloads. The backtest's IC for each feature is shown beside each input.

The header shows the market backdrop: SPY, 10Y and 2Y yields, breakeven inflation, Fed funds, CPI year-on-year and VIX.

## How scores are built

1. **Features**: 17 per stock per day, e.g. `mom_12_1`, `trend_50_200`, `rsi14`, `pe_vs_sector`, `rate_tilt`, `news_sentiment`. The full list with descriptions is in `src/features.py`.
2. **Cross-sectional z-scores**: each day, each feature is winsorised and z-scored across the S&P 500. Valuation is z-scored *within its GICS sector*, using earnings yield (EPS/price) so loss-makers are handled properly. Banks are compared to banks and software to software.
3. **Weighted composite per horizon**: weights are in `weights.yaml` and signed (negative = bearish). Only their relative size matters.
4. **Scaling**: the composite is re-standardised and passed through `tanh`, giving −1…+1. **Scores are relative**: 0 means average among S&P 500 stocks today. They are not a forecast of the market's direction.

**Rates and inflation**: for each stock, a rolling 2-year weekly regression of its market-adjusted return on changes in the 10Y yield (DGS10) and 10Y breakeven inflation (T10YIE) gives a sensitivity. The `rate_tilt` / `infl_tilt` features multiply that sensitivity by the last 3 months' trend in yields or inflation. Monthly CPI is shown for context but kept out of the scores, because it is published with a lag.

**News**: each headline's sentiment comes from VADER, or optionally FinBERT. Headlines are weighted by recency with a 1-day half-life (6h old ≈ 0.84, 1 day = 0.5, 3 days ≈ 0.13), so the latest news dominates. Every day's raw headlines and aggregate sentiment are stored, so the app builds its own point-in-time news history.

## Backtest

`Backtest → Run backtest` (or `python -m src.cli backtest`) scores the market every 5 trading days since 2016, using **only data available at each date**. It runs exactly the code the live app uses. Each position enters at the next day's open and is measured over 5, 21 and 126 days. Per horizon it reports:

- **IC**: the rank correlation between score and forward return, each week. About 0.02–0.05 is useful and above 0.05 is strong. The t-stat is computed on non-overlapping periods only.
- **Quintile returns**, top-minus-bottom spread, top-quintile hit rate and cumulative excess return.
- **Feature-level IC**, showing which inputs actually predicted returns.
- **Out-of-sample check**: weights learned from feature ICs on earlier years (with an embargo) are compared against your weights on the last 2 years. Apply them from the Weights tab if they win.

`tests/test_pipeline.py::test_no_lookahead` verifies that features on any date are identical whether or not later data exists.

**Known limits.** The universe is today's S&P 500 members (survivorship bias). P/E history only reaches back as far as Yahoo provides. News sentiment has no history before you start running the app. Results ignore trading costs.

## Command line

```bash
python -m src.cli refresh      # same as the Refresh button
python -m src.cli backtest
python -m src.cli rescore      # after editing weights.yaml by hand
python -m pytest -q            # code-path tests (synthetic data, never touches data/)
```

## Layout

```
app.py              Flask server + JSON API
config.yaml         data sources, horizons, news settings, backtest settings
weights.yaml        feature weights per horizon (also edited from the UI)
src/universe.py     S&P 500 list with GICS sectors
src/prices.py       incremental daily OHLCV store (repairs dividend/split re-adjustments)
src/macro.py        FRED yields / breakeven / Fed funds / CPI, VIX
src/fundamentals.py P/E history + company info -> point-in-time earnings-yield panels
src/news.py         Google News RSS, Yahoo, NewsAPI; VADER / FinBERT sentiment
src/features.py     feature engine (wide date x ticker panels)
src/scoring.py      z-scoring, weighting, tanh scaling
src/engine.py       live scoring + refresh orchestration
src/backtest.py     walk-forward evaluation
web/                single-page UI (fonts and charts served locally, works offline)
src/legacy/         the original v1 scanner, kept for reference
```

## Roadmap

- ML models (LightGBM / ridge per horizon) trained with purged walk-forward CV, compared against this baseline in the Backtest tab
- Point-in-time S&P 500 membership to remove survivorship bias
- Intraday / live data
