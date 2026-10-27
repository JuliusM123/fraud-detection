"""Interactive charts. Writes output/charts/volume_charts.html.

Per event the page shows the hourly volume chart (Stage 3) and, once Stages 5 and 6 have run,
a scatter of the flagged trades colored by wallet rating. A table of the wallets rated
suspicious or higher closes the page. Run this script last.

The page is plain HTML, SVG and JavaScript from src/chart_template.html, with no external
library. The data is embedded as JSON. The winning price and the volume share one time axis
in two panels instead of a second y axis.

Usage:
  python src/plots.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from data_io import ROOT, load_config
from features import chart_range, event_trades, hourly, load_events

OUT_PATH = ROOT / "output" / "charts" / "volume_charts.html"
TEMPLATE = Path(__file__).with_name("chart_template.html")


FLAGGED_PATH = ROOT / "output" / "flagged_trades.csv"
WALLETS_PATH = ROOT / "output" / "wallets.csv"


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _num(value, digits: int = 4):
    """JSON has no NaN, so missing numbers become null."""
    return None if pd.isna(value) else round(float(value), digits)


def _ratings() -> tuple[pd.DataFrame, pd.DataFrame] | None:
    if not (FLAGGED_PATH.exists() and WALLETS_PATH.exists()):
        return None
    flagged = pd.read_csv(FLAGGED_PATH, parse_dates=["block_time", "first_trade"])
    wallets = pd.read_csv(WALLETS_PATH, parse_dates=["first_trade", "first_flagged_trade"]).set_index("address")
    return flagged, wallets


def flagged_points(ev: dict, ratings) -> list[dict] | None:
    if ratings is None:
        return None
    flagged, wallets = ratings
    f = flagged[flagged["event"] == ev["id"]].join(wallets[["rating", "pseudonym", "other_markets"]], on="address")
    return [{
        "t": _ms(r.block_time),
        "price": _num(r.entry_price),
        "usd": _num(r.amount_usdc, 2),
        "address": r.address,
        "handle": r.pseudonym if isinstance(r.pseudonym, str) else None,
        "rating": r.rating if isinstance(r.rating, str) else "watch",
        "side": r.side,
        "outcome": r.outcome,
        "age_h": _num((r.block_time - r.first_trade).total_seconds() / 3600, 1) if pd.notna(r.first_trade) else None,
        "other_markets": None if pd.isna(r.other_markets) else int(r.other_markets),
    } for r in f.itertuples()]


def wallet_rows(ratings, names: dict[str, str]) -> list[dict]:
    if ratings is None:
        return []
    order = {"highly suspicious": 0, "suspicious": 1}
    w = ratings[1]
    w = w[w["rating"] != "watch"].copy()
    w["order"] = w["rating"].map(order)
    w = w.sort_values(["order", "event_bet_usd"], ascending=[True, False])
    return [{
        "address": address,
        "handle": r.pseudonym if isinstance(r.pseudonym, str) else None,
        "event_names": ", ".join(names[e] for e in r.events.split(",")),
        "rating": r.rating,
        "first_trade": _ms(r.first_trade),
        "age_h": _num((r.first_flagged_trade - r.first_trade).total_seconds() / 3600, 1),
        "bet_usd": _num(r.event_bet_usd, 2),
        "entry_price": _num(r.event_entry_price),
        "pnl_usd": _num(r.event_pnl_usd, 2),
        "other_markets": int(r.other_markets),
        "other_won": int(r.other_won),
    } for address, r in w.iterrows()]


def chart_data(ev: dict, trades: pd.DataFrame, ratings) -> dict:
    th = load_config()["thresholds"]
    start, end = chart_range(ev)
    h = hourly(trades, start, end, ev["t0"])
    win = ev["winning_outcome"].upper()
    return {
        "id": ev["id"],
        "name": ev["name"],
        "question": ev["question"],
        "winner": win,
        "loser": "NO" if win == "YES" else "YES",
        "start": _ms(h.index[0]),
        "end": _ms(end),
        "t0": _ms(ev["t0"]),
        "window_start": _ms(ev["t0"] - pd.Timedelta(hours=th["detection_window_hours"])),
        "window_hours": th["detection_window_hours"],
        "post_time": _ms(ev["post_time"]) if ev["post_time"] != ev["t0"] else None,
        "hours": [_ms(t) for t in h.index],
        "win": h["vol_winner"].round(2).tolist(),
        "lose": h["vol_loser"].round(2).tolist(),
        "price": [None if pd.isna(p) else round(float(p), 4) for p in h["win_price"]],
        "trades": h["trades"].astype(int).tolist(),
        "max_entry_price": th["max_entry_price"],
        "flagged": flagged_points(ev, ratings),
    }


def main() -> None:
    events = load_events()
    ratings = _ratings()
    data = {
        "events": [chart_data(ev, event_trades(ev), ratings) for ev in events],
        "wallets": wallet_rows(ratings, {ev["id"]: ev["name"] for ev in events}),
    }
    # keep the JSON from closing the script tag it sits in
    payload = json.dumps(data).replace("</", "<\\/")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    print(OUT_PATH.relative_to(ROOT))


if __name__ == "__main__":
    main()
