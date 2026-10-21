"""Config loading/saving and shared paths."""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
load_dotenv(PROJECT_ROOT / ".env")

_lock = threading.Lock()


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if os.getenv("SCANNER_DATA_DIR"):  # used by tests / demos to point at another folder
        cfg["data_dir"] = os.environ["SCANNER_DATA_DIR"]
    if os.getenv("SCANNER_NEWS_MODEL"):  # e.g. finbert on the server, vader on a laptop
        cfg["news"]["model"] = os.environ["SCANNER_NEWS_MODEL"]
    return cfg


def ns(x):
    """Coerce dates to naive datetime64[ns].

    pandas 3 keeps whatever resolution a source used (ms from parquet, us from Yahoo, ...), and
    joins refuse to match mixed resolutions, so every date column/index goes through this."""
    import pandas as pd
    if isinstance(x, pd.DataFrame):
        raise TypeError("pass a Series or Index")
    out = pd.to_datetime(x)
    if getattr(out, "tz", None) is not None or getattr(getattr(out, "dt", None), "tz", None) is not None:
        out = out.tz_localize(None) if isinstance(out, pd.DatetimeIndex) else out.dt.tz_localize(None)
    return out.astype("datetime64[ns]")


class Paths:
    """All on-disk locations, derived from config['data_dir']."""

    def __init__(self, cfg: Dict[str, Any]):
        root = Path(cfg.get("data_dir", "data"))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        self.root = root
        self.universe = root / "universe.csv"
        self.prices = root / "prices" / "ohlcv.parquet"
        self.macro = root / "macro" / "macro.parquet"
        self.valuation = root / "fundamentals" / "valuation_history.parquet"
        self.info_latest = root / "fundamentals" / "info_latest.parquet"
        self.info_history = root / "fundamentals" / "info_history.parquet"
        self.news_raw = root / "news" / "raw"
        self.news_sent = root / "news" / "sentiment.parquet"
        self.news_budget = root / "news" / "budget.json"
        self.features_latest = root / "scores" / "features_latest.parquet"
        self.features_recent = root / "scores" / "features_recent.parquet"
        self.scores_latest = root / "scores" / "scores_latest.parquet"
        self.scores_history = root / "scores" / "scores_history.parquet"
        self.stock_extras = root / "scores" / "stock_extras.parquet"
        self.state = root / "state.json"
        self.backtest = root / "backtest" / "results.json"
        for p in [self.prices, self.macro, self.valuation, self.news_sent, self.features_latest,
                  self.backtest]:
            p.parent.mkdir(parents=True, exist_ok=True)
        self.news_raw.mkdir(parents=True, exist_ok=True)

    # small key/value state file (last refresh time etc.)
    def read_state(self) -> Dict[str, Any]:
        if self.state.exists():
            try:
                return json.loads(self.state.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def write_state(self, **kv) -> None:
        s = self.read_state()
        s.update(kv)
        s["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.state.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
