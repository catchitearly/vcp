"""
Entry point: universe -> Stage 1 EMA prefilter -> Stage 2 curve+OBV screener -> CSV.
"""

import csv
import datetime as dt
import pathlib

import config
from ema_prefilter import run_prefilter
from curve_screener import run_screener


def load_universe(path: str) -> list:
    p = pathlib.Path(path)
    with p.open() as f:
        reader = csv.DictReader(f)
        return [row["symbol"].strip() for row in reader if row.get("symbol")]


def write_results(results: list, out_dir: str = "output"):
    pathlib.Path(out_dir).mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    out_path = pathlib.Path(out_dir) / f"rounding_bottom_{today}.csv"

    if not results:
        print("[main] no matches found")
        out_path.write_text("symbol\n")
        return out_path

    fieldnames = list(results[0].keys())
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"[main] wrote {len(results)} results to {out_path}")
    return out_path


def main():
    symbols = load_universe(config.UNIVERSE_CSV)
    print(f"[main] universe loaded: {len(symbols)} symbols")

    survivors = run_prefilter(symbols)
    results = run_screener(survivors)

    write_results(results)


if __name__ == "__main__":
    main()
