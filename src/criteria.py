"""Suspicion criteria per trade (Stage 5). Thresholds come from config/events.yaml.

Criteria for a taker trade, relative to a detection window [anchor - w, anchor):
  1 in_window       placed inside the detection window
  2 on_winner       bought the winning outcome (or sold the losing one)
  3 low_price       entry price on the side taken below max_entry_price
  4 large           size above the size_percentile of trade sizes in the baseline period
  5 fresh           first Polymarket trade of the wallet less than fresh_wallet_hours before the bet

Score is the number of criteria met. A trade with score >= flag_min_score is flagged.
The first trade time of a wallet costs one request, so it is looked up only where it can
change the flag, that is where the other four criteria already reach flag_min_score - 1.

Wallet rating (Stage 6) for every wallet with a flagged trade:
  watch              flagged trade but normal history
  suspicious         a flagged trade that is both large and placed less than rating_fresh_hours
                     after the wallet's first trade, and at most few_markets_max other markets
  highly suspicious  suspicious and at least one of: flagged in repeat_wins_min or more events,
                     repeat_wins_min or more other markets won at an entry price below
                     max_entry_price, or a first funder shared with another flagged wallet

A funder does not count as shared when it is a service: it has a public label such as an
exchange, or it sent at least service_min_transfers ERC20 transfers on Polygon. The second
condition was added after the first run, where one unlabelled address funded 137 flagged wallets.

Usage:
  python src/criteria.py            flag trades
  python src/criteria.py wallets    rate the wallets behind flagged trades
  python src/criteria.py events     rate each event
"""
from __future__ import annotations

import sys

import pandas as pd

from data_io import ROOT, load_config, wallet_first_trade, wallet_transfers
from features import event_trades, load_events, wallet_profile


def score_trades(trades: pd.DataFrame, ev: dict, anchor: pd.Timestamp) -> pd.DataFrame:
    th = load_config()["thresholds"]
    w0 = anchor - pd.Timedelta(hours=th["detection_window_hours"])
    base_start = anchor - pd.Timedelta(days=th["baseline_days"])
    base = trades[(trades["block_time"] >= base_start) & (trades["block_time"] < w0)]
    size_cut = base["amount_usdc"].quantile(th["size_percentile"] / 100)

    df = trades[(trades["block_time"] >= w0) & (trades["block_time"] < anchor)].copy()
    df["in_window"] = True
    df["low_price"] = df["entry_price"] < th["max_entry_price"]
    df["large"] = df["amount_usdc"] > size_cut
    df["size_cut_usd"] = size_cut
    pre = df[["in_window", "on_winner", "low_price", "large"]].sum(axis=1)

    df["fresh_checked"] = pre >= th["flag_min_score"] - 1
    first = {a: wallet_first_trade(a) for a in df.loc[df["fresh_checked"], "address"].unique()}
    # a wallet without any recorded trade counts as not fresh
    df["first_trade"] = pd.to_datetime(df["address"].map(lambda a: first.get(a)), utc=True)
    age = df["block_time"] - df["first_trade"]
    df["fresh"] = df["fresh_checked"] & (age < pd.Timedelta(hours=th["fresh_wallet_hours"])).fillna(False)
    df["score"] = pre + df["fresh"]
    df["flagged"] = df["score"] >= th["flag_min_score"]
    return df


def flagged_trades() -> pd.DataFrame:
    cols = ["event", "block_time", "address", "outcome", "side", "price", "entry_price", "amount_usdc",
            "amount_shares", "transaction_hash", "first_trade", "on_winner", "low_price", "large", "fresh",
            "score", "size_cut_usd"]
    frames, summary = [], []
    for ev in load_events():
        scored = score_trades(event_trades(ev), ev, ev["t0"]).assign(event=ev["id"])
        flagged = scored[scored["flagged"]]
        frames.append(flagged[cols])
        summary.append({
            "event": ev["id"],
            "window_trades": len(scored),
            "window_wallets": scored["address"].nunique(),
            "wallets_checked_for_age": scored.loc[scored["fresh_checked"], "address"].nunique(),
            "flagged_trades": len(flagged),
            "flagged_wallets": flagged["address"].nunique(),
            "score_counts": scored["score"].value_counts().sort_index().to_dict(),
        })
    df = pd.concat(frames, ignore_index=True).sort_values(["event", "block_time"])
    df.to_csv(ROOT / "output" / "flagged_trades.csv", index=False, float_format="%.4f")
    print(pd.DataFrame(summary).to_string(index=False))
    return df


def is_service(address: str) -> bool:
    """An address that sent at least service_min_transfers ERC20 transfers on Polygon is treated as a service."""
    n = load_config()["thresholds"]["service_min_transfers"]
    return len(wallet_transfers(address, "out", cap=-(-n // 1000))) >= n


def rate_wallets() -> pd.DataFrame:
    th = load_config()["thresholds"]
    flagged = pd.read_csv(ROOT / "output" / "flagged_trades.csv", parse_dates=["block_time", "first_trade"])
    conditions = {ev["id"]: ev["condition_id"] for ev in load_events()}
    age = flagged["block_time"] - flagged["first_trade"]
    flagged["fresh_for_rating"] = (age < pd.Timedelta(hours=th["rating_fresh_hours"])).fillna(False)
    g = flagged.groupby("address")
    per_wallet = pd.DataFrame({
        "flagged_events": g["event"].nunique(),
        "events": g["event"].agg(lambda s: ",".join(sorted(set(s)))),
        "flagged_trades": g.size(),
        "flagged_usd": g["amount_usdc"].sum(),
        "fresh_and_large": g.apply(lambda d: bool((d["fresh_for_rating"] & d["large"]).any()), include_groups=False),
        "first_flagged_trade": g["block_time"].min(),
    })
    profiles = []
    for i, address in enumerate(per_wallet.index, 1):
        profiles.append(wallet_profile(address, conditions))
        if i % 25 == 0:
            print(f"  ... {i} of {len(per_wallet)} wallets", flush=True)
    df = per_wallet.join(pd.DataFrame(profiles).set_index("address"))

    unlabelled = df.loc[df["funder_label"].isna() & df["funder"].notna(), "funder"]
    repeated = unlabelled[unlabelled.duplicated(keep=False)]
    service = {f for f in repeated.unique() if is_service(f)}
    shared = repeated[~repeated.isin(service)]
    df["funder_is_service"] = df["funder"].isin(service) | df["funder_label"].notna()
    df["shared_funder"] = df.index.isin(shared.index)
    df["suspicious"] = df["fresh_and_large"] & (df["other_markets"] <= th["few_markets_max"])
    df["highly"] = df["suspicious"] & (
        (df["flagged_events"] >= th["repeat_wins_min"])
        | (df["low_price_wins_other"] >= th["repeat_wins_min"])
        | df["shared_funder"]
    )
    df["rating"] = "watch"
    df.loc[df["suspicious"], "rating"] = "suspicious"
    df.loc[df["highly"], "rating"] = "highly suspicious"
    df = df.drop(columns=["suspicious", "highly"]).sort_values(["rating", "flagged_usd"], ascending=[True, False])
    df.to_csv(ROOT / "output" / "wallets.csv", float_format="%.4f")
    print(df.groupby(["events", "rating"]).size().to_string())
    return df


def rate_events() -> pd.DataFrame:
    """Event rating (Stage 8), defined by the user after the results were known:

    highly suspicious  the window volume z score is at least event_min_z, the share of volume on the
                       winner is higher in the window than in the baseline, and at least one wallet
                       is rated suspicious or higher
    anomalous          one or two of these three conditions hold
    normal             none of them holds
    """
    min_z = load_config()["thresholds"]["event_min_z"]
    stats = pd.read_csv(ROOT / "output" / "event_stats.csv").set_index("event")
    flagged = pd.read_csv(ROOT / "output" / "flagged_trades.csv")
    wallets = pd.read_csv(ROOT / "output" / "wallets.csv")
    rows = []
    for event, st in stats.iterrows():
        fe = flagged[flagged["event"] == event]
        rated = wallets[wallets["events"].str.split(",").apply(lambda e: event in e)]
        n_susp = int((rated["rating"] != "watch").sum())
        met = [st["volume_z"] >= min_z, st["win_share_window"] > st["win_share_baseline"], n_susp > 0]
        rows.append({
            "event": event,
            "window_volume_usd": st["window_volume_usd"],
            "volume_percentile": st["volume_percentile"],
            "volume_z": st["volume_z"],
            "win_share_window": st["win_share_window"],
            "win_share_baseline": st["win_share_baseline"],
            "flagged_trades": len(fe),
            "flagged_wallets": fe["address"].nunique(),
            "wallets_suspicious_or_higher": n_susp,
            "conditions_met": sum(met),
            "rating": "highly suspicious" if all(met) else "anomalous" if any(met) else "normal",
        })
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "output" / "event_ratings.csv", index=False, float_format="%.4f")
    print(df.to_string(index=False))
    return df


if __name__ == "__main__":
    if sys.argv[1:] == ["wallets"]:
        rate_wallets()
    elif sys.argv[1:] == ["events"]:
        rate_events()
    else:
        flagged_trades()
