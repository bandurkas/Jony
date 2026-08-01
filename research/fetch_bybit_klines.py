"""Fetch fresh BTCUSDT/ETHUSDT linear-perp klines from Bybit's public v5 API
(no auth needed) for 5m/15m/1h, paginating backward from now. Mirrors the
exact category/params Jony's live bybit_client.py uses (category=linear),
so the resulting backtest data stays faithful to what the live bot actually
sees. Output format matches the repo's existing data/*.json convention:
list of {start_ms, open, high, low, close, volume}, oldest -> newest.

Run: python3 fetch_bybit_klines.py <SYMBOL> <interval:5|15|60> <days_back> <out.json>
"""
from __future__ import annotations
import json
import sys
import time
import urllib.request
import urllib.parse

BASE = "https://api.bybit.com/v5/market/kline"


def fetch_page(symbol, interval, end_ms=None, limit=1000):
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    if end_ms is not None:
        params["end"] = end_ms
    url = BASE + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
            if data.get("retCode") != 0:
                raise RuntimeError(f"retCode={data.get('retCode')} {data.get('retMsg')}")
            return data["result"]["list"]
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gave up fetching {symbol} {interval} end={end_ms}")


def fetch_all(symbol, interval, days_back):
    cutoff_ms = int(time.time() * 1000) - int(days_back * 86_400_000)
    out = []
    end_ms = None
    while True:
        raw = fetch_page(symbol, interval, end_ms=end_ms)
        if not raw:
            break
        chunk = [{"start_ms": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                  "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                 for r in raw]
        chunk.sort(key=lambda c: c["start_ms"])
        out = chunk + out
        oldest = chunk[0]["start_ms"]
        print(f"  {symbol} {interval}: got {len(chunk)}, oldest={oldest}, total={len(out)}", flush=True)
        if oldest <= cutoff_ms:
            break
        end_ms = oldest - 1
        if len(raw) < 1000:
            break
    out = [c for c in out if c["start_ms"] >= cutoff_ms]
    return out


def main():
    symbol, interval, days_back, out_path = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    print(f"fetching {symbol} interval={interval} days_back={days_back} -> {out_path}")
    data = fetch_all(symbol, interval, days_back)
    print(f"total bars: {len(data)}  range: {data[0]['start_ms']} .. {data[-1]['start_ms']}" if data else "NO DATA")
    with open(out_path, "w") as f:
        json.dump(data, f)
    print("done")


if __name__ == "__main__":
    main()
