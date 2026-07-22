# vcp — US stock market data & analysis

Tooling for downloading/caching US stock market data (NASDAQ screener, Wikipedia
S&P membership, Yahoo Finance) and analysing it for Volatility Contraction
Patterns. Three modules, each usable from Python and the command line:

| Module | Command | What it does |
|---|---|---|
| `data` | `get_symbols` | The universe of US stocks + metadata, filtered per `vcp.json` |
| `data` | `get_symbol_price_data` | ~20 years of daily OHLCV for one or more symbols |
| `vcp` | `get_symbol_price_data` | One symbol's OHLCV enriched with EMAs, ADR%, contraction signals & a `VCP` score, plus optional plotting |
| `rank` | `rank` | The filtered universe screened by momentum & `VCP_Rank`, ranked strongest-first |

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
    "signal_rank_period": 500
  },
  "rank": {
    "momentum_period": 242,
    "momentum_filter": 30,
    "min_vcp": 90
  }
}
```

- **`filters`** — applied to `get_symbols`. Any key can be removed / set to
  `null` to disable that filter. Editing these takes effect immediately (the
  full universe is cached; filters are applied on read).
- **`price`** — `history_years` sets the download look-back; the rest tune the
  Yahoo Finance download (concurrency, retries, and the rate-limit sweep).
- **`vcp`** — used by `vcp.get_symbol_price_data`: the two EMA periods
  (`ema_short_period`, `ema_long_period`; defaults `10`, `20`), the
  `adr_period` look-back for the `ADR` score (default `20`), and
  `signal_rank_period` — the trailing window (trading days) for the percentile
  rank of each signal line and of the composite `VCP` (default `500`).
- **`rank`** — used by `rank.rank`: `momentum_period` — the trailing window
  (trading days) for the "n-day low" (default `242` ≈ one year); `momentum_filter`
  — the minimum percent above that low a stock must be (default `30`); and
  `min_vcp` — the minimum latest `VCP_Rank` a stock must have (default `90`).

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

## 3. `vcp.get_symbol_price_data` — EMAs, ADR & signal lines

The analysis layer. Loads a **single** symbol's adjusted OHLCV (via
`data.get_symbol_price_data`) and adds derived columns:

- **Short- and long-period EMAs** of `High`, `Low`, `Close` and `Volume`:
  `High_EMA_short`, `High_EMA_long`, `Low_EMA_short`, `Low_EMA_long`,
  `Close_EMA_short`, `Close_EMA_long`, `Volume_EMA_short`, `Volume_EMA_long`
- **`ADR`** — Average Daily Range percent (see below)
- **`Signal_Low`, `Signal_Volume`, `Signal_High`** — three contraction signals,
  each a 0–100 percentile rank (see below)
- **`VCP`** — composite contraction score (product of the three signals)
- **`VCP_Rank`** — percentile rank of `VCP`, comparable across symbols

The EMA periods come from the `vcp` section of `vcp.json` (`ema_short_period`,
`ema_long_period`; defaults 10 and 20). EMAs use the standard
`ewm(span=period, adjust=False)` (seeded at the first value). Full columns:
`Open, High, Low, Close, Volume` + the 8 EMAs + `ADR` + `Signal_Low` +
`Signal_Volume` + `Signal_High` + `VCP` + `VCP_Rank`.

#### The `ADR` score

`ADR` is the **Average Daily Range percent** — how much the stock swings
high-to-low on a typical day, averaged over `adr_period` days (default 20):

```
ADR = 100 × (meanₙ(High / Low) − 1)
```

A higher value means a wider-swinging (more volatile) stock. It's a common
liquidity/volatility filter in VCP screening. The initial `adr_period − 1`
warm-up rows without a full window are dropped.

#### The signal lines

Three signals aim to detect a **compression of range** — a tightening base —
rather than a trend. Each is first built from a short/long EMA ratio through
`f(x, y) = exp(x / y)`, then converted to a **causal percentile rank (0–100)**
over the trailing `signal_rank_period` window (default 500). The exponential is
just a positive, monotonic rescaling; ranking is what does the real work —
it puts all three on a common, self-normalising 0–100 scale so no single one
dominates:

```
Signal_Low    = pct_rank( exp(Low_EMA_short  / Low_EMA_long) )
Signal_Volume = pct_rank( exp(Volume_EMA_long / Volume_EMA_short) )
Signal_High   = pct_rank( min( exp(High_EMA_short / High_EMA_long),
                               exp(High_EMA_long  / High_EMA_short) ) )
```

- **`Signal_Low`** is **directional**: high when recent lows sit above their
  longer-run average → **the floor is rising**.
- **`Signal_Volume`** inverts the ratio (long/short): high when recent volume is
  *below* its baseline → **volume is drying up**; it drops on volume surges.
- **`Signal_High`** is **symmetric** (`min(r, 1/r)` peaks at `r = 1`): high only
  when the fast and slow high-EMAs coincide → **the ceiling is flat**. It falls
  whenever the highs trend in *either* direction, so it acts as a trend rejector.

Together they capture a VCP coil: a **rising floor into a flat ceiling on
drying-up volume**. A genuine uptrend (highs *and* lows rising) is rejected
because rising highs collapse `Signal_High`.

Each rank uses only its trailing window (no look-ahead, so it's backtest-safe).
The warm-up rows without a full window are dropped; if a symbol has too few
records to fill one window, `get_symbol_price_data` raises `ValueError`.

#### The `VCP` and `VCP_Rank` scores

`VCP` is the composite — the three signals must line up *at once* for it to be
high, so it's a **product** (an additive blend would let a strong signal mask a
weak one). Each rank is scaled to 0–1 and the product rescaled to 0–100:

```
VCP = (Signal_Low / 100) × (Signal_Volume / 100) × Signal_High
```

Because each input is a self-normalised rank, `VCP` is already far more
comparable across symbols than a raw product would be. But a product's
distribution still depends on how the three signals *co-move*, which varies by
symbol. `VCP_Rank` removes that last dependence by ranking `VCP` against its own
history the same way the signals are ranked:

```
VCP_Rank = pct_rank(VCP)   over the trailing signal_rank_period window
```

`VCP_Rank = 90` means today's compression is in the top 10% of this symbol's own
recent history — a statement that holds identically on every ticker, so it's the
line to use for a single cross-symbol cutoff. Note that ranking `VCP` needs a
full window *on top of* the signal warm-up, so `VCP_Rank` requires roughly
**two stacked `signal_rank_period` windows** of history (its warm-up rows are
dropped too).

> Note: `vcp.get_symbol_price_data` takes **one** `symbol` and returns a
> `DataFrame`; `data.get_symbol_price_data` takes a list of `symbols` and returns
> a `dict`. Same name, different module and shape.

### From Python

```python
import vcp

df = vcp.get_symbol_price_data("MSFT")                # from cache
df = vcp.get_symbol_price_data("MSFT", refresh=True)  # force re-download

# Also save a three-panel chart (Close + EMAs, ADR%, then the signal ranks + VCP_Rank):
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

Pass `plot_filename=...` to save a three-panel chart (Close + EMAs, ADR%, then
the three signal ranks with `VCP_Rank` overlaid) instead of just printing;
`plot_from_rec` / `plot_to_rec` select the record range (defaults `-252` to
`-1`, i.e. the last ~year):

```bash
python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png
python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png plot_from_rec=-100 plot_to_rec=-1
```

Handy one-liner — clear old charts, plot a symbol, and open it:

```bash
rm *.png && SYMBOL=IBKR && python vcp.py get_symbol_price_data symbol=$SYMBOL plot_filename=$SYMBOL.png && eog $SYMBOL.png
```

---

## 4. `rank` — momentum + `VCP_Rank` screen

The screening layer. Builds on `data` (whose `vcp.json` `filters` already remove
illiquid stocks) and ranks the survivors:

1. Pull the filtered universe and its price history (from cache, or freshly
   downloaded when `refresh=True`).
2. **Momentum** = how far the latest `Close` sits above the lowest `Low` of the
   trailing `momentum_period` days (default `242` ≈ one year — effectively
   "percent above the 52-week low").
3. Keep stocks at least `momentum_filter` percent (default `30`) above that low
   **and** whose latest `VCP_Rank` (from `vcp`) is at least `min_vcp` (default
   `90`), and return them ranked by momentum, strongest first.

**Columns:** `symbol, sector, last_close, period_low, pct_above_low, VCP_Rank`,
indexed by `rank` (1 = strongest). `sector` comes from `get_symbols`;
`period_low` is the trailing `momentum_period` low; `pct_above_low` is the
momentum measure (percent); `VCP_Rank` is the latest contraction percentile.

> **On the momentum filter:** "percent above the n-day low" is a naturally high,
> right-skewed quantity — the low is a hard one-year floor, so the median stock
> sits ~40% above it. A `30` threshold is therefore a *loose* pre-filter that
> mainly drops laggards still hugging their lows; `min_vcp` (default `90`,
> i.e. top-decile contraction) does the selective cut.

> **Minimum history:** each survivor's `VCP_Rank` needs roughly two stacked
> `signal_rank_period` windows (~1000 trading days) of history; symbols with
> less are dropped from the ranking.

### From Python

```python
import rank

df = rank.rank()               # from cache
df = rank.rank(refresh=True)   # re-download universe + prices first
```

Returns a pandas `DataFrame` (empty if nothing clears the filters). The thresholds
are read from the `rank` section of `vcp.json`. Computing `VCP_Rank` across the
survivors takes roughly one to two minutes.

### From the command line

```bash
python rank.py                 # refresh defaults to False
python rank.py refresh=True
```

Prints the ranked table with a footer noting the active thresholds. Tune
`momentum_period` / `momentum_filter` / `min_vcp` in `vcp.json`.

---

## `refresh` and caching

Every command shares the same caching rule (`rank` passes `refresh` straight
through to the `data` layer):

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
