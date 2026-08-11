"""
Pull current NSE Midcap150 + Smallcap250 constituent lists and rebuild data/universe.csv.

Run this LOCALLY (not reliably from GitHub Actions - NSE frequently blocks
datacenter IPs even with correct headers). Re-run every 1-3 months since
index constituents change at each periodic NSE reshuffle (semi-annual).

Usage:
    python fetch_universe.py
"""

import csv
import io
import urllib.request

NSE_URLS = {
    "midcap": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "smallcap": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}


def fetch_csv_text(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    rows = []
    for category, url in NSE_URLS.items():
        try:
            text = fetch_csv_text(url)
        except Exception as exc:
            print(f"[WARN] Could not fetch {category} list from {url}: {exc}")
            print("       NSE may be blocking this connection. Try downloading")
            print("       the CSV manually from nseindia.com > Indices and")
            print("       dropping it into data/ instead.")
            continue

        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            symbol = row.get("Symbol", "").strip()
            name = row.get("Company Name", "").strip()
            if symbol:
                rows.append({"symbol": symbol, "name": name, "category": category})

    if not rows:
        print("No data fetched - universe.csv left unchanged.")
        return

    with open("data/universe.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "category"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} symbols to data/universe.csv")


if __name__ == "__main__":
    main()
