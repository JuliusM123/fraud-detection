# Polymarket Fraud Detection Prototype

This prototype looks for unusual Polymarket bets placed shortly before political announcements by Donald Trump on Truth Social. It compares betting activity before each announcement with a baseline, flags trades by fixed rules and looks closer at the wallets behind them. It does not prove insider trading. It shows how unlikely an observed pattern is without prior knowledge.

Results are in [`output/summary.md`](output/summary.md).

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Put a Goldsky Feeds API key (it starts with `gs_edge`) in a file `.env` in the project root (see `.env.example`) or in the environment variable `GOLDSKY_API_KEY`. The file is ignored by git. A normal Goldsky project key does not work for this API.

## Run

```powershell
.venv\Scripts\python.exe src\features.py download     # trades of all events
.venv\Scripts\python.exe src\features.py stats        # Stage 4 statistics
.venv\Scripts\python.exe src\criteria.py              # Stage 5 flagged trades
.venv\Scripts\python.exe src\criteria.py wallets      # Stage 6 wallet rating
.venv\Scripts\python.exe src\criteria.py events       # Stage 8 event rating
.venv\Scripts\python.exe src\plots.py                 # charts, run last
```

Outputs in `output/`: `charts/volume_charts.html`, `event_stats.csv`, `flagged_trades.csv`, `wallets.csv`, `event_ratings.csv` and `summary.md`.

Every call to an API is cached, so a second run sends no request. Helpers for exploring markets:

```powershell
.venv\Scripts\python.exe src\data_io.py find "Iran ceasefire"
.venv\Scripts\python.exe src\data_io.py tokens 0x...
.venv\Scripts\python.exe src\data_io.py trades 0x... "2026-03-24 00:00" "2026-03-31 00:00"
.venv\Scripts\python.exe src\data_io.py test
```

All times are UTC. A time without a zone counts as UTC.

## Data sources

The spec names Dune as the data source. Dune free accounts are view only and have no API access. The trial was not available on our account, so Dune would need a paid plan. The prototype uses these sources instead.

| Source | Used for | Key |
| --- | --- | --- |
| Goldsky Edge Data API `/polymarket/activity` | trades of one outcome token per UTC day | yes |
| Goldsky Edge Data API `/feeds/wallets/transfers` | first funder of a wallet | yes |
| Polymarket Data API v2 `/v2/tokens` | token ids, outcome labels and final prices of a market | no |
| Polymarket Data API v2 `/v2/trades` | trade history of a wallet | no |
| Polymarket Data API v2 `/v2/activity` | first trade of a wallet | no |
| Polymarket Gamma API `/public-search` | find a market by keyword | no |

Goldsky is used for market trades because it filters by token and time. The Polymarket Data API ignores time filters for market queries and returns newest first. Some networks block the Polymarket domains.

Every result is cached in `data/raw/` as parquet with a JSON file next to it. The JSON holds the parameters, row count and number of requests.

## Data notes

* Goldsky returns one row per wallet and fill. `order_type` is `taker` for the wallet that took the order and `maker` for the wallets whose orders were filled. The prototype loads taker rows only.
* Goldsky taker rows match the Polymarket Data API trade list exactly. For the test market week both gave the same 3,385 trades, the same USDC amounts and the same transaction hashes.
* The Goldsky `address` is the same wallet as the Polymarket `proxy_wallet`.
* The volume shown by Gamma and on polymarket.com counts shares, not dollars. For the test market the Gamma volume of 739,420 equals its total traded shares, while the dollar value was 382,610 USD.

## Method

### Events

Three events are configured in `config/events.yaml`. Each has a market, its winning outcome and t0. For the ceasefire and the tariff pause t0 is the Truth Social post. For Maduro t0 is the first public report of explosions in Caracas at about 06:00 UTC, because the post came more than three hours later. Post times come from the CNN Truth Social archive and were checked against news reports.

### Trades and sides

The trading wallet is the taker. A MINT or MERGE match needs no special rule. From the taker's view it is an ordinary buy or sell of one token at one price, so every taker row counts once. A trade bets on the winner when the taker buys the winning token or sells the losing token. Its entry price is the price paid for the side taken.

### Charts (Stage 3)

`output/charts/volume_charts.html` holds one interactive volume chart per event, drawn as SVG by plain JavaScript from `src/chart_template.html`. Below each volume chart a scatter shows the flagged trades of the detection window by time before t0 and entry price, with the bet in USD as dot size and the wallet rating as color. A table of the wallets rated suspicious or higher closes the page. The page needs no external library and embeds its data. The spec asked for PNG files, the user asked for charts drawn in code instead.

Each chart covers 7 days before t0 and 12 hours after. Hours are counted from t0, so no bar mixes trading before and after it. The winning price and the volume share one time axis in two panels instead of a second y axis. When the bars after t0 are much taller than any bar before, the scale follows the bars before t0 and the peak after t0 is written on the chart. Hovering or the arrow keys show the values of one hour, and a table view lists every hour.

### Statistics (Stage 4)

The detection window is the 12 hours before t0. The spec set 6 hours. The user changed it to 12 hours after the first results, because a wallet described by AP bought 6 to 8.5 hours before the ceasefire post. The 6 hour results are kept in `output/window_6h/`. The baseline is every 12 hour window of the 7 days before it, in the same market. A baseline stops at market start, so the tariff market has 10 windows instead of 14. The percentile is the share of baseline windows with less volume. The price at a moment is the share weighted mean of the last 20 trades before it.

### Criteria (Stage 5)

These rules and thresholds were fixed before any result was seen.

1. placed inside the detection window
2. bet on the winning outcome
3. entry price below 0.5
4. size above the 95th percentile of trade sizes in the baseline period (7 days before t0 without the detection window)
5. fresh wallet: first Polymarket trade less than 48 hours before the bet

A trade with 4 or 5 criteria is flagged. The first trade of a wallet costs one request, so it is looked up only for trades that already meet 3 of the other 4 criteria.

### Wallets (Stage 6)

For every wallet with a flagged trade the prototype reads its Polymarket trade history and the final price of every market in it. Profit assumes every position is held to resolution. A market counts as won when this profit is positive. The likelihood without prior knowledge is the product of the entry prices of all won markets. Histories longer than 20,000 trades are cut. Such wallets cannot be fresh, so their win rate and funder are left empty.

| Rating | Rule |
| --- | --- |
| watch | flagged trade but normal history |
| suspicious | a flagged trade that is both large and placed less than 7 days after the wallet's first trade, and at most 3 other markets |
| highly suspicious | suspicious and one of: flagged in 2 or more events, 2 or more other markets won at an entry price below 0.5, a first funder shared with another flagged wallet |

A funder does not count as shared when it is a service. That is an address with a public label, such as an exchange, or with at least 1,000 outgoing ERC20 transfers on Polygon.

Two of these rules changed after the first results. The freshness limit for the rating was 48 hours as in Stage 5 and became 7 days by decision of the user, because the wallet in the charged Maduro case was about 6 days old. The service test by transfer count was added after one unlabelled address turned out to have funded 137 flagged wallets.

### Event rating (Stage 8)

An event is rated by three conditions. The volume z score of the detection window is at least 3. The share of volume on the winner is higher in the window than in the baseline. At least one wallet is rated suspicious or higher. An event is highly suspicious when all three hold, anomalous when one or two hold and normal when none does. The user set this rule after the results were known.

### Controls (Stage 7)

The Maduro event is the positive control. The method has to flag at least one wallet there without a change to the Stage 5 thresholds. For multiple testing the summary reports how many trades and wallets were checked in total, not only how many were flagged.

The spec also asked for a placebo test at 20 random moments per market. It was run once and then removed by decision of the user, together with the event rating that compared against it.
