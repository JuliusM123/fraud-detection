"""Fetch Polymarket data and cache every result in data/raw/.

Sources (the spec names Dune, the README explains the change):
  Goldsky Edge Data API   trades of one outcome token in a time window
  Polymarket Data API v2  token lookup per market, wallet trade history
  Polymarket Gamma API    market search by keyword

Each call series is cached under a hash of its name and parameters.
A cached result is never fetched again.

Usage:
  python src/data_io.py find "Iran ceasefire"
  python src/data_io.py tokens 0x...
  python src/data_io.py trades 0x... "2026-04-01 00:00" "2026-04-08 00:00"
  python src/data_io.py test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "events.yaml"
CACHE_DIR = ROOT / "data" / "raw"

GOLDSKY = "https://edge.goldsky.com/data"
PM_DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"

GOLDSKY_COLUMNS = [
    "block_timestamp", "block_number", "transaction_hash", "id", "event_type", "order_type", "side",
    "address", "counterparty_address", "token_id", "price", "amount_usdc", "amount_shares", "fee", "order_id",
]


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def utc(value) -> pd.Timestamp:
    """Parse a datetime or string as UTC. Naive input counts as UTC."""
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value) -> str:
    return utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(name: str) -> str:
    value = os.environ.get(name)
    env_file = ROOT / ".env"
    if not value and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition("=")
            if key.strip() == name:
                value = val.strip().strip("\"'")
    if not value:
        raise RuntimeError(f"{name} is not set. Export it or put it in .env (see .env.example).")
    return value


class _Http:
    """Shared session. Spaces requests and retries on 429 and 5xx errors."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.last = 0.0
        self.count = 0

    def get(self, url: str, params: dict, headers: dict | None = None) -> dict:
        for attempt in range(self.cfg["max_retries"] + 1):
            wait = self.cfg["min_interval_seconds"] - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            r = self.session.get(url, params=params, headers=headers, timeout=60)
            self.count += 1
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.cfg["max_retries"]:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"GET {url} {params} -> {r.status_code}: {r.text[:300]}")
            return r.json()


HTTP = _Http(load_config()["http"])


def _cached(name: str, params: dict, fetch) -> pd.DataFrame:
    """Return the cached result of name(params), or run fetch() once and cache it."""
    blob = json.dumps({"name": name, "params": params}, sort_keys=True)
    path = CACHE_DIR / f"{name}_{hashlib.sha256(blob.encode()).hexdigest()[:12]}.parquet"
    if path.exists():
        print(f"[cache] {name} {params}")
        return pd.read_parquet(path)
    started, before = time.time(), HTTP.count
    df = fetch()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    meta = {
        "name": name,
        "params": params,
        "rows": len(df),
        "requests": HTTP.count - before,
        "seconds": round(time.time() - started, 1),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[fetch] {name} {params}: {len(df)} rows, {meta['requests']} requests -> {path.relative_to(ROOT)}")
    return df


def _pages(url: str, params: dict, headers: dict | None, next_cursor, cursor_param: str = "cursor",
           cap: int | None = None) -> list[dict]:
    """Follow cursors until the last page. Every page repeats the same filters.

    With cap, stop quietly after cap pages and return what was read so far.
    """
    max_pages = cap or HTTP.cfg["max_pages"]
    rows, cursor = [], None
    for n in range(1, max_pages + 1):
        page = HTTP.get(url, {**params, **({cursor_param: cursor} if cursor else {})}, headers)
        rows += page["data"]
        cursor = next_cursor(page)
        if not cursor or (cap and n == cap):
            return rows
        if n % 50 == 0:
            print(f"  ... {n} pages, {len(rows)} rows", flush=True)
    raise RuntimeError(f"more than {max_pages} pages for {url} {params}. Raise http.max_pages in config/events.yaml.")


def token_activity(token_id: str, day, order_type: str = "taker") -> pd.DataFrame:
    """Goldsky trade rows of one outcome token on one UTC day. Taker rows only by default."""
    start = utc(day).floor("D")
    params = {
        "token_id": str(token_id),
        "start_time": _iso(start),
        "end_time": _iso(start + pd.Timedelta(days=1)),
        "order_type": order_type,
        "limit": 1000,
    }

    def fetch() -> pd.DataFrame:
        rows = _pages(f"{GOLDSKY}/polymarket/activity", params, {"x-api-key": _env("GOLDSKY_API_KEY")},
                      lambda page: page.get("next_cursor"))
        df = pd.DataFrame(rows, columns=GOLDSKY_COLUMNS)
        df["block_time"] = pd.to_datetime(df["block_timestamp"], unit="s", utc=True)
        return df.sort_values(["block_number", "id"]).reset_index(drop=True)

    return _cached("goldsky_activity", params, fetch)


def market_tokens(condition_id: str) -> pd.DataFrame:
    """Polymarket token lookup: one row per outcome with token_id, outcome label and final_price."""
    return tokens_for([condition_id])


def tokens_for(condition_ids) -> pd.DataFrame:
    """Token lookup for many markets, 10 per request (the API limit)."""
    ids = sorted({c.lower() for c in condition_ids})
    frames = []
    for i in range(0, len(ids), 10):
        params = {"condition": ",".join(ids[i:i + 10])}
        frames.append(_cached("pm_tokens", params, lambda p=params: pd.DataFrame(HTTP.get(f"{PM_DATA}/tokens", p)["data"])))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def wallet_transfers(address: str, direction: str, cap: int | None = None) -> pd.DataFrame:
    """Goldsky: ERC20 transfers of a wallet on Polygon in one direction ("in" or "out"), newest first.

    With cap, at most cap pages of 1000 rows.
    """
    params = {"address": address.lower(), "chains": "polygon", "direction": direction, "transfer_type": "erc20", "page_size": 1000}

    def fetch() -> pd.DataFrame:
        rows = _pages(f"{GOLDSKY}/feeds/wallets/transfers", params, {"x-api-key": _env("GOLDSKY_API_KEY")},
                      lambda page: page["pagination"].get("next_page_token"), cursor_param="page_token", cap=cap)
        return pd.DataFrame(rows)

    return _cached(f"goldsky_transfers_{direction}", {**params, **({"cap": cap} if cap else {})}, fetch)


def wallet_transfers_in(address: str, cap: int | None = None) -> pd.DataFrame:
    return wallet_transfers(address, "in", cap)


def market_trades(condition_id: str, start, end, order_type: str = "taker") -> pd.DataFrame:
    """Goldsky rows of both outcome tokens of a market in [start, end), labelled with the outcome.

    Downloads whole UTC days so that overlapping requests share the cache.
    """
    days = pd.date_range(utc(start).floor("D"), utc(end) - pd.Timedelta(seconds=1), freq="D")
    frames = [
        token_activity(t.token_id, day, order_type).assign(outcome=t.outcome, outcome_index=t.outcome_index)
        for t in market_tokens(condition_id).itertuples()
        for day in days
    ]
    # day boundaries are inclusive on both ends, so a row at exactly midnight can appear twice
    df = pd.concat(frames, ignore_index=True).drop_duplicates("id")
    df = df[(df["block_time"] >= utc(start)) & (df["block_time"] < utc(end))]
    return df.sort_values(["block_number", "id"]).reset_index(drop=True)


def wallet_trades(address: str, start=None, end=None, cap: int | None = None) -> pd.DataFrame:
    """Polymarket trades of one wallet on maker and taker side, newest first. No start means full history.

    With cap, read at most cap pages of 1000 trades.
    """
    params = {
        "user": address.lower(),
        "start": int(utc(start).timestamp()) if start is not None else 1,
        "end": int(utc(end).timestamp()) if end is not None else 0,
        "taker_only": "false",
        "limit": 1000,
    }

    def fetch() -> pd.DataFrame:
        rows = _pages(f"{PM_DATA}/trades", params, None, lambda page: page["pagination"].get("next_cursor"), cap=cap)
        df = pd.DataFrame(rows)
        if len(df):
            df["block_time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return df

    return _cached("pm_wallet_trades", {**params, **({"cap": cap} if cap else {})}, fetch)


def wallet_first_trade(address: str) -> pd.Timestamp | None:
    """Time of the first Polymarket trade of a wallet, or None if it has no trade."""
    params = {"user": address.lower(), "type": "TRADE", "start": 1, "sort_direction": "ASC", "limit": 1}
    df = _cached("pm_first_trade", params, lambda: pd.DataFrame(HTTP.get(f"{PM_DATA}/activity", params)["data"]))
    return pd.to_datetime(df["timestamp"].iloc[0], unit="s", utc=True) if len(df) else None


def find_markets(keyword: str) -> pd.DataFrame:
    """Gamma search. One row per market with condition_id, outcomes, final prices and volume."""
    params = {"q": keyword, "limit_per_type": 50, "events_status": "all"}

    def fetch() -> pd.DataFrame:
        rows = []
        for ev in HTTP.get(f"{GAMMA}/public-search", params).get("events") or []:
            for m in ev.get("markets") or []:
                rows.append({
                    "event": ev.get("title"),
                    "question": m.get("question"),
                    "condition_id": m.get("conditionId"),
                    "outcomes": m.get("outcomes"),
                    "final_prices": m.get("outcomePrices"),
                    "volume": float(m.get("volume") or 0),
                    "start": m.get("startDate"),
                    "end": m.get("endDate"),
                    "closed": m.get("closed"),
                })
        return pd.DataFrame(rows)

    return _cached("gamma_search", params, fetch)


def _print_profile(df: pd.DataFrame) -> None:
    trades = df[df["event_type"] == "TRADE"]
    print(f"rows {len(df)}, time range {df['block_time'].min()} to {df['block_time'].max()}")
    for col in ["outcome", "event_type", "order_type", "side"]:
        print(f"{col}: {df[col].value_counts(dropna=False).to_dict()}")
    for role in ["taker", "maker"]:
        part = trades[trades["order_type"] == role]
        print(f"TRADE {role} rows: {len(part)}, amount {part['amount_usdc'].sum():,.0f} USDC, wallets {part['address'].nunique()}")
    print(trades.head(8)[["block_time", "outcome", "order_type", "side", "price", "amount_usdc", "address", "counterparty_address"]].to_string())


def _cross_check(df: pd.DataFrame, n: int = 3) -> None:
    """Look up the largest taker trades in the Polymarket Data API by wallet and transaction hash."""
    taker = df[(df["event_type"] == "TRADE") & (df["order_type"] == "taker")]
    for row in taker.nlargest(n, "amount_usdc").itertuples():
        pm = wallet_trades(row.address, row.block_time - pd.Timedelta(minutes=2), row.block_time + pd.Timedelta(minutes=2))
        match = pm[pm["transaction_hash"] == row.transaction_hash] if len(pm) else pm
        print(f"{row.address} {row.transaction_hash[:12]}... goldsky {row.side} {row.amount_shares:.2f} shares @ {row.price:.3f}"
              f" | polymarket rows in tx: {len(match)}"
              + (f", proxy_wallet match {set(match['proxy_wallet'].str.lower()) == {row.address.lower()}},"
                 f" sides {match['side'].tolist()}, sizes {match['size'].tolist()}" if len(match) else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find").add_argument("keyword")
    sub.add_parser("tokens").add_argument("condition_id")
    p = sub.add_parser("trades")
    p.add_argument("condition_id")
    p.add_argument("start")
    p.add_argument("end")
    sub.add_parser("test")
    args = parser.parse_args()

    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 70)
    if args.cmd == "find":
        df = find_markets(args.keyword)
        print(df.sort_values("volume", ascending=False).to_string(max_rows=60))
    elif args.cmd == "tokens":
        print(market_tokens(args.condition_id).T.to_string())
    elif args.cmd == "trades":
        _print_profile(market_trades(args.condition_id, args.start, args.end))
    elif args.cmd == "test":
        m = load_config()["test_market"]
        print(f"test market: {m['question']}\n")
        df = market_trades(m["condition_id"], m["start_time"], m["end_time"], order_type=None)
        _print_profile(df)
        print("\ncross check with Polymarket Data API:")
        _cross_check(df)


if __name__ == "__main__":
    main()
