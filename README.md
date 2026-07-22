# vcp — US stock market data & analysis

Tooling for downloading/caching US stock market data (NASDAQ screener, Wikipedia
S&P membership, Yahoo Finance) and analysing it for Volatility Contraction
Patterns. Two modules, each usable from Python and the command line:

| Module | Command | What it does |
|---|---|---|
| `data` | `get_symbols` | The universe of US stocks + metadata, filtered per `vcp.json` |
| `data` | `get_symbol_price_data` | ~20 years of daily OHLCV for one or more symbols |
| `vcp` | `get_symbol_price_data` | One symbol's OHLCV enriched with Range, EMAs, a VC score, and optional plotting |

## Setup

Everything runs in the `vcp` conda environment:

```bash
conda activate vcp
pip install -r requirements.txt
```

Dependencies: `pandas`, `curl_cffi`, `lxml` (symbol universe) and `yfinance`,
`scipy` (price history — `scipy` backs yfinance's data-glitch repair). See
`requirements.txt`.

## Configuration — `vcp.json`

All programs read shared settings from `vcp.json`:

```json
{
  "filters": {
    "min_market_cap": 300000000,
    "min_price": 10,
    "min_dollar_volume": 20000000
  },
  "price": {
    "history_years": 20,
    "max_workers": 4,
    "max_retries": 4,
    "sweep_cooldown": 10
  },
  "vcp": {
    "ema_short_period": 10,
    "ema_long_period": 20,
    "adr_period": 20,
    "alpha": 1.0,
    "beta": 1.0
  }
}
```

- **`filters`** — applied to `get_symbols`. Any key can be removed / set to
  `null` to disable that filter. Editing these takes effect immediately (the
  full universe is cached; filters are applied on read).
- **`price`** — `history_years` sets the download look-back; the rest tune the
  Yahoo Finance download (concurrency, retries, and the rate-limit sweep).
- **`vcp`** — used by `vcp.get_symbol_price_data`: the two EMA periods
  (`ema_short_period`, `ema_long_period`), the `adr_period` look-back for the
  `ADR` score, and the `alpha` / `beta` exponents (both default `1.0`) that
  weight the Range and Volume terms of the `VC` score.

---

## 1. `get_symbols`

Returns every US stock (NASDAQ / NYSE / NYSE American) with metadata, filtered
per `vcp.json`.

**Columns:** `symbol, name, sector, market_cap, last_price, volume,
dollar_volume, country, ipo_year, sp400, sp500, sp600`

### From Python

```python
import data

df = data.get_symbols()                 # from cache (downloads on first use), filtered
df = data.get_symbols(refresh=True)     # re-download and rewrite the cache
df = data.get_symbols(filtered=False)   # the full, unfiltered universe
```

Returns a pandas `DataFrame`.

### From the command line

```bash
python data.py get_symbols                 # from cache, filtered
python data.py get_symbols refresh=True    # re-download and cache
```

Prints the table plus a summary of the active filters and S&P index counts.

---

## 2. `get_symbol_price_data`

Downloads daily OHLCV price history (split/dividend-adjusted) from Yahoo Finance
for one or more symbols. The look-back window is `price.history_years` in
`vcp.json` (default 20 years).

**Returns** a `dict` mapping each ticker to a `DataFrame` indexed by `Date` with
columns `Open, High, Low, Close, Volume`.

### From Python

```python
import data

data.get_symbol_price_data(symbols=["QQQ"])               # single symbol
data.get_symbol_price_data(symbols=["QQQ", "MSFT"])       # several
data.get_symbol_price_data(symbols="all")                 # entire filtered universe
data.get_symbol_price_data(symbols=["QQQ"], refresh=True) # force re-download

prices = data.get_symbol_price_data(symbols=["QQQ", "MSFT"])
prices["QQQ"].tail()
```

### From the command line

```bash
python data.py get_symbol_price_data symbols=QQQ,MSFT
python data.py get_symbol_price_data symbols=QQQ,MSFT refresh=True
python data.py get_symbol_price_data symbols=all refresh=True
```

`symbols` is a comma-separated list, or `all` for the full filtered universe.

---

## 3. `vcp.get_symbol_price_data` — Range, EMAs, ADR & VC

The analysis layer. Loads a **single** symbol's adjusted OHLCV (via
`data.get_symbol_price_data`) and adds derived columns:

- **`Range`** = `High − Low` (daily high-low range)
- **Short- and long-period EMAs** of `Close`, `Range`, and `Volume`:
  `Close_EMA_short`, `Close_EMA_long`, `Range_EMA_short`, `Range_EMA_long`,
  `Volume_EMA_short`, `Volume_EMA_long`
- **`ADR`** — Average Daily Range percent (see below)
- **`VC`** — a volatility-contraction score (see below)

The two EMA periods come from the `vcp` section of `vcp.json`
(`ema_short_period`, `ema_long_period`; defaults 10 and 20). EMAs use the
standard `ewm(span=period, adjust=False)` (seeded at the first value). Full
columns: `Open, High, Low, Close, Volume, Range` + the 6 EMAs + `ADR` + `VC`.

#### The `ADR` score

`ADR` is the **Average Daily Range percent** — how much the stock swings
high-to-low on a typical day, averaged over `adr_period` days (default 20):

```
ADR = 100 × (meanₙ(High / Low) − 1)
```

A higher value means a wider-swinging (more volatile) stock. It's a common
liquidity/volatility filter in VCP screening.

#### The `VC` score

`VC` combines how much recent **range** and **volume** have contracted relative
to their own longer-term baselines:

```
VC = 1 − (Range_EMA_short / Range_EMA_long) ** alpha
       × (Volume_EMA_short / Volume_EMA_long) ** beta
```

Each factor is a fast/slow EMA ratio centred on 1 (recent vs. baseline), raised
to its exponent from `vcp.json` (`alpha` for range, `beta` for volume; both
default `1.0`). The product is flipped via `1 − …` so the sign reads intuitively:

- **`VC > 0`** → range **and** volume are contracting (the product < 1) — the
  quiet, tightening state a VCP setup is built on.
- **`VC < 0`** → expansion (elevated range and/or volume) — e.g. a high-volume
  breakout or sharp selloff.
- **`VC ≈ 0`** → neutral (recent ≈ baseline).

`alpha` / `beta` weight the two dimensions; setting one to `0` drops that factor
out entirely (it becomes a constant `1`), leaving a pure range- or volume-based
score.

> Note: `vcp.get_symbol_price_data` takes **one** `symbol` and returns a
> `DataFrame`; `data.get_symbol_price_data` takes a list of `symbols` and returns
> a `dict`. Same name, different module and shape.

### From Python

```python
import vcp

df = vcp.get_symbol_price_data("MSFT")                # from cache
df = vcp.get_symbol_price_data("MSFT", refresh=True)  # force re-download

# Also save a two-panel chart (Close + EMAs on top, VC below):
df = vcp.get_symbol_price_data("MSFT", plot_filename="msft.png")
df = vcp.get_symbol_price_data("MSFT", plot_filename="msft.png",
                               plot_from_rec=-100, plot_to_rec=-1)
```

Returns a pandas `DataFrame` indexed by `Date`. The API call is silent (no
console output); pass `plot_filename` to also write a chart. `plot_from_rec` /
`plot_to_rec` are inclusive record positions (negative counts from the end;
defaults `-252` to `-1`, the last ~year). Requires `matplotlib`.

### From the command line

```bash
python vcp.py get_symbol_price_data symbol=MSFT
python vcp.py get_symbol_price_data symbol=MSFT refresh=True
```

Prints the enriched table with a record-count / date-range footer.

Pass `plot_filename=...` to save a two-panel chart (Close + EMAs on top, the VC
score below) instead of just printing; `plot_from_rec` / `plot_to_rec` select the
record range (defaults `-252` to `-1`, i.e. the last ~year):

```bash
python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png
python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png plot_from_rec=-100 plot_to_rec=-1
```

Handy one-liner — clear old charts, plot a symbol, and open it:

```bash
rm *.png && SYMBOL=IBKR && python vcp.py get_symbol_price_data symbol=$SYMBOL plot_filename=$SYMBOL.png && eog $SYMBOL.png
```

---

## `refresh` and caching

Both commands share the same caching rule:

- **`refresh=False`** (default) — load from cache if present; download (and cache)
  only what's missing.
- **`refresh=True`** — re-download and rewrite the cache.

Cache layout (next to the code, in `cache/`):

```
cache/
  symbols.csv              # full symbol universe (unfiltered)
  prices/
    QQQ.csv                # one file per symbol
    MSFT.csv
    _manifest.json         # records the history_years each price cache was built with
```

Notes:

- The symbol universe is cached in full; `vcp.json` filters are applied when you
  read it, so changing filters needs no re-download.
- Price caches auto-invalidate if `price.history_years` changes in `vcp.json`.
  Price data is otherwise **not** refreshed automatically for new trading days —
  pass `refresh=True` to pull current data.
- Downloading many symbols (e.g. `symbols=all`) is rate-limit hardened and
  resumable: rerun the command and it only fetches what's still missing.
