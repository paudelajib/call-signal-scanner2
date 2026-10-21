"""Export a read-only static copy of the web app (for GitHub Pages).

Calls the Flask JSON API in-process (no server needed) and writes every GET response
to site/api/*.json, next to a copy of web/ with paths rewritten so it works from any
sub-folder URL. The front end detects window.STATIC_SITE and reads these files instead
of calling the API; Refresh / Run backtest / Save weights are hidden.

    python scripts/export_static.py            # writes ./site
    python scripts/export_static.py --out dist
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site"))
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "api" / "stock").mkdir(parents=True)

    import app as webapp  # noqa: E402  (Flask app; importing does not start the server)
    c = webapp.app.test_client()

    def get(url: str):
        r = c.get(url)
        if r.status_code != 200:
            raise SystemExit(f"{url} -> HTTP {r.status_code}: {r.get_data(as_text=True)[:200]}")
        return r.get_json()

    def dump(rel: str, obj) -> None:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))

    meta = get("/api/meta")
    if not meta.get("has_data"):
        raise SystemExit("No scores yet - run `python -m src.cli refresh` first.")
    dump("api/meta.json", meta)
    for h in meta["horizons"]:
        dump(f"api/ranking_{h}.json", get(f"/api/ranking?horizon={h}"))
    dump("api/weights.json", get("/api/weights"))
    dump("api/backtest.json", get("/api/backtest"))

    tickers = [r["ticker"] for r in get("/api/ranking?horizon=medium")["rows"]]
    for i, t in enumerate(tickers, 1):
        r = c.get(f"/api/stock/{t}")
        if r.status_code == 200:
            dump(f"api/stock/{t}.json", r.get_json())
        if i % 100 == 0:
            print(f"stock pages: {i}/{len(tickers)}")

    # static assets
    web = ROOT / "web"
    shutil.copytree(web, out / "static", ignore=shutil.ignore_patterns("index.html"))
    (out / "vendor").mkdir()
    (out / "vendor" / "plotly.min.js").write_bytes(c.get("/vendor/plotly.min.js").get_data())
    html = (web / "index.html").read_text()
    html = (html.replace('href="/static/', 'href="static/')
                .replace('src="/static/', 'src="static/')
                .replace('src="/vendor/', 'src="vendor/')
                .replace('<script src="vendor/plotly.min.js"></script>',
                         '<script>window.STATIC_SITE = true;</script>\n'
                         '<script src="vendor/plotly.min.js"></script>'))
    (out / "index.html").write_text(html)
    (out / ".nojekyll").write_text("")
    print(f"wrote {len(tickers)} stocks to {out}")


if __name__ == "__main__":
    main()
