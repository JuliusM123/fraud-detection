"""Event windows, baseline and trade features.

Trade rows are Goldsky taker rows, one per filled taker order. The taker is the wallet that
actively took liquidity. MINT and MERGE matches need no special case: from the taker's view they
are ordinary buys and sells of one outcome token at one price, so every taker row counts once.

A row bets on the winner when the taker buys the winning token or sells the losing token.

Usage:
  python src/features.py download
  python src/features.py stats
"""
from __future__ import annotations

import argparse

import pandas as pd

from data_io import (ROOT, load_config, market_trades, tokens_for, utc, wallet_first_trade, wallet_trades,
                     wallet_transfers_in)

STABLECOINS = {"USDC", "USDC.E", "PUSD", "USDT"}


def load_events() -> list[dict]:
    events = []
    for ev in load_config()["events"]:
        ev = dict(ev)
        for key in ("t0", "post_time", "market_start"):
            ev[key] = utc(ev[key])
        events.append(ev)
    return events


def data_range(ev: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Trades are loaded from the start of the oldest baseline window (not before market start) to
    hours_after_t0 after t0."""
    th = load_config()["thresholds"]
    reach = pd.Timedelta(days=th["baseline_days"]) + pd.Timedelta(hours=th["detection_window_hours"])
    start = max(ev["market_start"], ev["t0"] - reach)
    return start, ev["t0"] + pd.Timedelta(hours=th["hours_after_t0"])


def chart_range(ev: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Baseline days before t0 (not before market start) to hours_after_t0 after."""
    th = load_config()["thresholds"]
    start = max(ev["market_start"], ev["t0"] - pd.Timedelta(days=th["baseline_days"]))
    return start, ev["t0"] + pd.Timedelta(hours=th["hours_after_t0"])


def event_trades(ev: dict) -> pd.DataFrame:
    """Taker trade rows of the event market over data_range, with features relative to the winning outcome."""
    df = market_trades(ev["condition_id"], *data_range(ev))
    df = df[df["event_type"] == "TRADE"].copy()
    win = df["outcome"].str.upper() == ev["winning_outcome"].upper()
    df["win_token"] = win
    df["on_winner"] = (win & (df["side"] == "BUY")) | (~win & (df["side"] == "SELL"))
    # implied price of the winning outcome, and the price the taker paid for the side it took
    df["win_price"] = df["price"].where(win, 1 - df["price"])
    df["entry_price"] = df["win_price"].where(df["on_winner"], 1 - df["win_price"])
    return df.reset_index(drop=True)


def hourly(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, anchor: pd.Timestamp) -> pd.DataFrame:
    """Hourly USD volume on the winning and losing side and the share weighted winning price.

    Hours are counted from the anchor, so no hour spans both sides of it.
    """
    first = anchor - pd.Timedelta(hours=-((start - anchor) // pd.Timedelta(hours=1)))
    index = pd.date_range(first, end, freq="h", inclusive="left", name="hour")
    df = df[(df["block_time"] >= first) & (df["block_time"] < end)]
    hour = anchor + ((df["block_time"] - anchor) // pd.Timedelta(hours=1)) * pd.Timedelta(hours=1)
    out = pd.DataFrame(index=index)
    for col, mask in [("vol_winner", df["on_winner"]), ("vol_loser", ~df["on_winner"])]:
        out[col] = df[mask].groupby(hour[mask])["amount_usdc"].sum().reindex(index, fill_value=0.0)
    weighted = (df["win_price"] * df["amount_shares"]).groupby(hour).sum()
    out["win_price"] = (weighted / df["amount_shares"].groupby(hour).sum()).reindex(index)
    out["trades"] = hour.value_counts().reindex(index, fill_value=0)
    return out


def windows(ev: dict, anchor: pd.Timestamp) -> pd.DataFrame:
    """Detection window [anchor - w, anchor) and the baseline windows of the same length before it.

    Baseline windows cover baseline_days and stop at market start or at the start of the loaded data.
    """
    th = load_config()["thresholds"]
    w = pd.Timedelta(hours=th["detection_window_hours"])
    floor = data_range(ev)[0]
    rows = [{"kind": "detection", "start": anchor - w, "end": anchor}]
    for k in range(1, int(pd.Timedelta(days=th["baseline_days"]) / w) + 1):
        if anchor - w * (k + 1) < floor:
            break
        rows.append({"kind": "baseline", "start": anchor - w * (k + 1), "end": anchor - w * k})
    return pd.DataFrame(rows)


def price_at(trades: pd.DataFrame, when: pd.Timestamp, n: int = 20) -> float:
    """Winning outcome price at a moment: share weighted mean of the last n trades before it."""
    last = trades[trades["block_time"] < when].tail(n)
    return float((last["win_price"] * last["amount_shares"]).sum() / last["amount_shares"].sum()) if len(last) else float("nan")


def window_stats(trades: pd.DataFrame, ev: dict, anchor: pd.Timestamp) -> dict:
    """Volume of the detection window against its baseline windows, winning side share and price move."""
    wins = windows(ev, anchor)
    for i, w in wins.iterrows():
        part = trades[(trades["block_time"] >= w["start"]) & (trades["block_time"] < w["end"])]
        wins.loc[i, "volume"] = part["amount_usdc"].sum()
        wins.loc[i, "vol_winner"] = part.loc[part["on_winner"], "amount_usdc"].sum()
        wins.loc[i, "trades"] = len(part)
    det = wins.iloc[0]
    base = wins[wins["kind"] == "baseline"]
    start_price, end_price = price_at(trades, det["start"]), price_at(trades, det["end"])
    return {
        "anchor": anchor,
        "window_start": det["start"],
        "window_volume_usd": det["volume"],
        "window_trades": int(det["trades"]),
        "baseline_windows": len(base),
        "baseline_mean_usd": base["volume"].mean(),
        "baseline_std_usd": base["volume"].std(),
        "volume_percentile": 100 * (base["volume"] < det["volume"]).mean(),
        "volume_z": (det["volume"] - base["volume"].mean()) / base["volume"].std(),
        "win_share_window": det["vol_winner"] / det["volume"] if det["volume"] else float("nan"),
        "win_share_baseline": base["vol_winner"].sum() / base["volume"].sum(),
        "win_price_start": start_price,
        "win_price_end": end_price,
        "win_price_move": end_price - start_price,
    }


def event_stats() -> pd.DataFrame:
    rows = []
    for ev in load_events():
        rows.append({"event": ev["id"], "t0": ev["t0"], **window_stats(event_trades(ev), ev, ev["t0"])})
    df = pd.DataFrame(rows).drop(columns="anchor")
    path = ROOT / "output" / "event_stats.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format="%.4f")
    return df


def wallet_markets(hist: pd.DataFrame) -> pd.DataFrame:
    """One row per market in a wallet's trade history, with money spent and profit if held to resolution.

    Profit counts trades only: a buy earns (final price - price) per share, a sell the negative.
    """
    hist = hist.copy()
    tokens = tokens_for(hist["condition_id"].unique()).set_index("token_id")
    hist["final"] = hist["token_id"].map(tokens["final_price"])
    hist["resolved"] = hist["token_id"].map(tokens["resolved"]).fillna(False).astype(bool)
    sign = hist["side"].map({"BUY": 1, "SELL": -1})
    hist["pnl"] = sign * hist["size"] * (hist["final"] - hist["price"])
    hist["spent"] = (hist["size"] * hist["price"]).where(hist["side"] == "BUY", 0.0)
    win_buy = (hist["side"] == "BUY") & (hist["final"] == 1)
    hist["win_shares"] = hist["size"].where(win_buy, 0.0)
    hist["win_cost"] = (hist["size"] * hist["price"]).where(win_buy, 0.0)
    g = hist.groupby("condition_id")
    out = pd.DataFrame({
        "title": g["title"].first(),
        "first_trade": g["block_time"].min(),
        "trades": g.size(),
        "spent_usd": g["spent"].sum(),
        "pnl_usd": g["pnl"].sum(),
        "resolved": g["resolved"].all(),
        "win_shares": g["win_shares"].sum(),
        "win_cost": g["win_cost"].sum(),
    })
    out["won"] = out["resolved"] & (out["pnl_usd"] > 0)
    # share weighted entry price of the buys on the side that won
    out["win_entry_price"] = out["win_cost"] / out["win_shares"].where(out["win_shares"] > 0)
    return out.drop(columns=["win_shares", "win_cost"])


def wallet_funder(address: str, cap: int) -> dict:
    """Sender of the first incoming stablecoin transfer on Polygon. Unknown when the transfer list is capped."""
    none = {"funder": None, "funder_label": None, "funded_at": None, "funded_usd": None}
    tr = wallet_transfers_in(address, cap=cap)
    if tr.empty or len(tr) >= cap * 1000:
        return none
    tr = tr[tr["token_symbol"].str.upper().isin(STABLECOINS)]
    if tr.empty:
        return none
    first = tr.sort_values("block_number").iloc[0]
    return {"funder": first["from_address"], "funder_label": first.get("from_label"),
            "funded_at": utc(first["block_timestamp"]), "funded_usd": first.get("amount_usd")}


def wallet_profile(address: str, event_conditions: dict[str, str]) -> dict:
    """History features of one wallet. event_conditions maps event id to condition id.

    A history longer than wallet_history_max_pages pages is cut. Such a wallet trades far too much
    to count as fresh, so win rate, likelihood and funder are left empty for it.
    """
    cap = load_config()["data"]["wallet_history_max_pages"]
    hist = wallet_trades(address, cap=cap)
    base = {
        "address": address,
        "pseudonym": hist["pseudonym"].iloc[0] if len(hist) else None,
        "first_trade": wallet_first_trade(address),
        "history_capped": len(hist) >= cap * 1000,
        "trades": len(hist),
        "markets": hist["condition_id"].nunique() if len(hist) else 0,
    }
    if hist.empty or base["history_capped"]:
        return base
    m = wallet_markets(hist)
    own = m[m.index.isin([c.lower() for c in event_conditions.values()])]
    other = m[~m.index.isin(own.index)]
    other_resolved = other[other["resolved"]]
    won = m[m["won"]]
    return {
        **base,
        "other_markets": len(other),
        "event_bet_usd": own["spent_usd"].sum(),
        "event_pnl_usd": own["pnl_usd"].sum(),
        "event_entry_price": own["win_entry_price"].mean(),
        "other_resolved": len(other_resolved),
        "other_won": int(other_resolved["won"].sum()),
        "win_rate": m.loc[m["resolved"], "won"].mean() if m["resolved"].any() else float("nan"),
        "low_price_wins_other": int((other_resolved["won"] & (other_resolved["win_entry_price"] < 0.5)).sum()),
        # chance to win every winning bet without prior knowledge, using the entry price as probability
        "likelihood_all_wins": won["win_entry_price"].prod() if len(won) else float("nan"),
        "total_pnl_usd": m["pnl_usd"].sum(),
        **wallet_funder(address, cap),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["download", "stats"])
    args = parser.parse_args()
    if args.cmd == "stats":
        pd.set_option("display.width", 250)
        print(event_stats().T.to_string())
    elif args.cmd == "download":
        for ev in load_events():
            start, end = data_range(ev)
            print(f"\n=== {ev['id']}: {start} to {end}", flush=True)
            df = event_trades(ev)
            print(f"=== {ev['id']}: {len(df)} taker trades, {df['amount_usdc'].sum():,.0f} USDC, "
                  f"{df['address'].nunique()} wallets", flush=True)


if __name__ == "__main__":
    main()
