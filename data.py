"""data — download and cache US stock market data.

Currently provides :func:`get_symbols`, which returns every stock listed on the
US market (NASDAQ / NYSE / NYSE American) together with metadata: company name,
sector, market cap, last price, volume, dollar volume, country, IPO year and
membership of the S&P 400 / 500 / 600 indices.

Usage
-----
Python::

    import data
    df = data.get_symbols(refresh=True)   # download + cache, returns DataFrame
    df = data.get_symbols()               # load from cache (download if absent)

Command line::

    python data.py get_symbols refresh=True
    python data.py get_symbols            # refresh defaults to False

Data is cached to ``cache/symbols.csv`` next to this module and is only
re-downloaded when ``refresh=True`` (or the cache file is missing).
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
from curl_cffi import requests

try:
    from yfinance.exceptions import YFRateLimitError
except Exception:  # pragma: no cover - older/newer yfinance without this symbol
    class YFRateLimitError(Exception):
        """Fallback so ``except YFRateLimitError`` is always valid."""

import config

# --------------------------------------------------------------------------- #
# Paths / configuration
# --------------------------------------------------------------------------- #
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(MODULE_DIR, "cache")
SYMBOLS_CACHE = os.path.join(CACHE_DIR, "symbols.csv")
PRICES_CACHE_DIR = os.path.join(CACHE_DIR, "prices")

PRICE_HISTORY_YEARS = 20  # fallback if not set in vcp.json ("price.history_years")
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Rate-limit defaults (overridable via the "price" section of vcp.json).
PRICE_MAX_WORKERS = 4          # concurrent downloads; Yahoo throttles bursts
PRICE_MAX_RETRIES = 4          # attempts per symbol (an empty result is retried)
PRICE_SWEEP_COOLDOWN = 10.0    # seconds to pause before the final retry sweep
PRICE_PROGRESS_EVERY = 25      # print a progress line every N completed downloads

NASDAQ_STOCKS_URL = "https://api.nasdaq.com/api/screener/stocks"
SP_PAGES = {
    400: "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    500: "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    600: "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

# Column order for the returned / cached table.
COLUMNS = [
    "symbol", "name", "sector",
    "market_cap", "last_price", "volume", "dollar_volume",
    "country", "ipo_year", "sp400", "sp500", "sp600",
]
BOOL_COLS = ["sp400", "sp500", "sp600"]


# --------------------------------------------------------------------------- #
# HTTP helper (robust: browser impersonation + retry w/ exponential backoff)
# --------------------------------------------------------------------------- #
def _http_get(url, params=None, *, retries=4, backoff=1.5, timeout=30):
    """GET ``url`` with retries. Returns the response or raises RuntimeError."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                params=params,
                impersonate="chrome",  # real browser TLS/HTTP fingerprint
                timeout=timeout,
                headers={"Accept": "application/json, text/plain, */*"},
            )
            resp.raise_for_status()
            return resp
        except Exception as err:  # noqa: BLE001 - retry on any transport error
            last_err = err
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def _norm(symbol) -> str:
    """Normalise a ticker so share-class notations match across sources.

    NASDAQ uses ``BRK/B`` while Wikipedia uses ``BRK.B``; both map to ``BRK-B``.
    """
    return str(symbol).strip().upper().replace(".", "-").replace("/", "-")


# --------------------------------------------------------------------------- #
# Individual source fetchers
# --------------------------------------------------------------------------- #
def _fetch_stocks() -> pd.DataFrame:
    """All common stocks with sector metadata (single bulk call)."""
    resp = _http_get(
        NASDAQ_STOCKS_URL,
        params={"tableonly": "true", "limit": "25", "offset": "0", "download": "true"},
    )
    rows = resp.json()["data"]["rows"]
    df = pd.DataFrame(rows)
    df = df.rename(columns={"marketCap": "market_cap", "ipoyear": "ipo_year",
                            "lastsale": "last_price"})
    df = df[["symbol", "name", "sector", "market_cap",
             "last_price", "volume", "country", "ipo_year"]].copy()
    return df


def _fetch_sp_members(url: str) -> set[str]:
    """Return the set of normalised tickers in an S&P index (from Wikipedia)."""
    resp = _http_get(url)
    tables = pd.read_html(StringIO(resp.text), attrs={"id": "constituents"})
    table = tables[0]
    symcol = next(
        c for c in table.columns
        if str(c).strip().lower() in ("symbol", "ticker", "ticker symbol")
    )
    return {_norm(s) for s in table[symcol].dropna()}


# --------------------------------------------------------------------------- #
# Download + assemble
# --------------------------------------------------------------------------- #
def _download_symbols() -> pd.DataFrame:
    """Fetch every source concurrently and assemble the symbol table."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_stocks = pool.submit(_fetch_stocks)
        f_sp = {level: pool.submit(_fetch_sp_members, url) for level, url in SP_PAGES.items()}

        df = f_stocks.result()
        sp_members = {level: fut.result() for level, fut in f_sp.items()}

    # Clean up tickers and drop empties / duplicates. Drop nulls *before*
    # astype(str), otherwise NaN becomes the literal string "nan" and survives.
    df = df[df["symbol"].notna()]
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[~df["symbol"].isin(("", "NAN", "NONE"))]
    df = df.drop_duplicates(subset="symbol", keep="first")

    # Index membership flags (matched on the normalised ticker).
    key = df["symbol"].map(_norm)
    for level in (400, 500, 600):
        df[f"sp{level}"] = key.isin(sp_members[level])

    # Coerce numeric metadata.
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["last_price"] = pd.to_numeric(
        df["last_price"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce"
    )
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["dollar_volume"] = df["last_price"] * df["volume"]
    df["ipo_year"] = pd.to_numeric(df["ipo_year"], errors="coerce").astype("Int64")

    df = df[COLUMNS].sort_values("symbol").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Cache load
# --------------------------------------------------------------------------- #
def _load_cache() -> pd.DataFrame:
    df = pd.read_csv(SYMBOLS_CACHE)
    for col in BOOL_COLS:
        if col in df.columns:
            df[col] = df[col].astype(str).str.lower().isin(("true", "1"))
    if "ipo_year" in df.columns:
        df["ipo_year"] = pd.to_numeric(df["ipo_year"], errors="coerce").astype("Int64")
    return df


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _normalize_universe(df: pd.DataFrame) -> pd.DataFrame:
    """Tidy the universe regardless of source (fresh download or cache).

    Drops rows without a real symbol (defensive against legacy caches where a
    blank ticker round-trips as NaN), fills missing categorical metadata
    (``sector``, ``country``) with ``"Unknown"``, and fills missing
    ``ipo_year`` with ``0`` (sentinel for an unknown / long-established listing)
    so no record is ever dropped from analysis for want of a value.
    """
    df = df[df["symbol"].notna()].copy()
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[~df["symbol"].isin(("", "NAN", "NONE"))]
    for col in ("sector", "country"):
        if col in df.columns:
            df[col] = df[col].replace("", pd.NA).fillna("Unknown")
    if "ipo_year" in df.columns:
        df["ipo_year"] = df["ipo_year"].fillna(0).astype("int64")
    return df.reset_index(drop=True)


def _apply_symbol_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Filter the symbol universe per the ``filters`` section of vcp.json.

    Supported keys (each may be omitted / null to disable that filter):
    ``min_market_cap``, ``min_price`` and ``min_dollar_volume``. Rows with a
    missing value in a filtered column are excluded, since they cannot satisfy
    the threshold.
    """
    filters = config.get("filters", {}) or {}
    thresholds = {
        "market_cap": filters.get("min_market_cap"),
        "last_price": filters.get("min_price"),
        "dollar_volume": filters.get("min_dollar_volume"),
    }
    for column, minimum in thresholds.items():
        if minimum is not None:
            df = df[df[column] >= minimum]
    return df.reset_index(drop=True)


def get_symbols(refresh: bool = False, filtered: bool = True) -> pd.DataFrame:
    """Return a DataFrame of US stocks with metadata.

    The full universe is always cached; the ``filters`` in ``vcp.json`` are
    applied to the returned table (not the cache), so editing the config takes
    effect immediately without re-downloading.

    Parameters
    ----------
    refresh:
        If ``True`` the data is downloaded fresh and the cache is rewritten.
        If ``False`` (default) the cached table is returned when it exists,
        otherwise the data is downloaded (and cached) on first use.
    filtered:
        If ``True`` (default) apply the ``vcp.json`` filters. Pass ``False`` to
        get the unfiltered universe.
    """
    if not refresh and os.path.exists(SYMBOLS_CACHE):
        df = _load_cache()
    else:
        df = _download_symbols()
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(SYMBOLS_CACHE, index=False)

    df = _normalize_universe(df)
    return _apply_symbol_filters(df) if filtered else df


# --------------------------------------------------------------------------- #
# Price history (daily OHLCV from Yahoo Finance)
# --------------------------------------------------------------------------- #
def _price_cache_path(yahoo_symbol: str) -> str:
    return os.path.join(PRICES_CACHE_DIR, f"{yahoo_symbol}.csv")


def _rate_limit_backoff(attempt: int) -> float:
    """Exponentially growing, jittered backoff (2s, 4s, 8s, 16s ... capped 30s).

    Full jitter (50-100% of the target) desynchronises concurrent workers so
    they don't all retry in lockstep and re-trip the rate limit together.
    """
    target = min(30.0, 2.0 * (2 ** attempt))
    return target * (0.5 + random.random() * 0.5)


def _download_prices(yahoo_symbol: str, *, retries: int | None = None) -> pd.DataFrame:
    """Download daily OHLCV for one symbol from Yahoo Finance.

    The look-back window is ``price.history_years`` in ``vcp.json`` (default
    :data:`PRICE_HISTORY_YEARS`). Robust against rate limiting: an empty
    response (Yahoo's usual "soft" throttle signal) is treated as retryable,
    explicit ``YFRateLimitError``\\ s are retried, and each retry waits a
    jittered, exponentially-growing backoff. Returns an empty frame only if
    every attempt fails.
    """
    years = config.get("price.history_years", PRICE_HISTORY_YEARS)
    if retries is None:
        retries = config.get("price.max_retries", PRICE_MAX_RETRIES)
    start = pd.Timestamp.today().normalize() - pd.DateOffset(years=years)

    # Small randomised delay so a burst of workers doesn't hit Yahoo in unison.
    time.sleep(random.uniform(0.1, 0.4))

    for attempt in range(retries):
        try:
            raw = yf.download(
                yahoo_symbol,
                start=start,
                interval="1d",
                auto_adjust=True,      # fully split/dividend-adjusted OHLC
                repair=True,           # fix Yahoo glitches (unit errors, missing splits)
                actions=False,
                progress=False,
                threads=False,
            )
            if raw is not None and not raw.empty:
                # Single-ticker downloads come back with a (field, ticker)
                # column MultiIndex; flatten to just the field names.
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.get_level_values(0)
                df = raw[[c for c in PRICE_COLUMNS if c in raw.columns]].copy()
                df.index.name = "Date"
                df.columns.name = None  # drop yfinance's "Price" columns-index label
                return df
            # Empty response: yfinance swallows most errors (incl. HTTP 429) and
            # returns an empty frame rather than raising, so treat empty as a
            # retryable throttle signal instead of accepting a silent hole.
        except YFRateLimitError:
            pass  # explicit rate limit -> back off and retry
        except Exception:  # noqa: BLE001 - any transport error is retryable
            pass

        if attempt < retries - 1:
            time.sleep(_rate_limit_backoff(attempt))

    # Exhausted all attempts. Genuinely dataless tickers also land here; the
    # caller records this as "no data" and the sweep gives it one more chance.
    return pd.DataFrame(columns=PRICE_COLUMNS)


def _download_and_cache(symbols, manifest: dict, years, max_workers: int,
                        label: str = "download") -> dict:
    """Download symbols concurrently, caching each the instant it completes.

    Every non-empty result is written to its cache file and recorded in
    ``manifest`` immediately (the manifest is persisted periodically), so an
    interrupted run keeps all downloads finished so far and is resumable.
    Progress (saved / no-data / outstanding) is printed every
    :data:`PRICE_PROGRESS_EVERY` completions. Returns {symbol: DataFrame}
    (an empty frame for symbols that yielded no data).
    """
    total = len(symbols)
    workers = max(1, min(max_workers, total))
    results: dict[str, pd.DataFrame] = {}
    done = saved = 0
    print(f"[{label}] starting {total} symbol(s) with {workers} workers")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_prices, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = fut.result()
            except Exception:  # noqa: BLE001 - defensive; _download_prices swallows
                df = pd.DataFrame(columns=PRICE_COLUMNS)
            if not df.empty:
                df.to_csv(_price_cache_path(sym))          # save immediately
                manifest[sym] = {"history_years": years}
                saved += 1
            results[sym] = df
            done += 1
            if done % PRICE_PROGRESS_EVERY == 0 or done == total:
                _save_manifest(manifest)                   # persist progress
                print(f"[{label}] {saved} saved, {done - saved} no-data, "
                      f"{total - done} outstanding ({done}/{total})")
    return results


def _load_price_cache(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
    return df


# The manifest records the config a symbol's cache was downloaded under, so a
# cache can be invalidated when the relevant vcp.json settings change.
def _manifest_path() -> str:
    return os.path.join(PRICES_CACHE_DIR, "_manifest.json")


def _load_manifest() -> dict:
    path = _manifest_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_manifest(manifest: dict) -> None:
    with open(_manifest_path(), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def get_symbol_price_data(symbols=("QQQ",), refresh: bool = False) -> dict:
    """Return daily OHLCV price history for one or more symbols.

    Parameters
    ----------
    symbols:
        A single ticker string, an iterable of tickers (e.g. ``["QQQ", "MSFT"]``),
        or the sentinel ``"all"`` to download the entire filtered universe
        returned by :func:`get_symbols`.
    refresh:
        If ``True`` each symbol is re-downloaded and the cache is rewritten.
        If ``False`` (default) a symbol is loaded from its cache when present and
        current, and downloaded (and cached) when missing or stale. A cache is
        stale when ``price.history_years`` in ``vcp.json`` has changed since it
        was downloaded, so editing the config invalidates affected caches.

    Returns
    -------
    dict[str, pandas.DataFrame]
        Mapping of ticker -> DataFrame indexed by ``Date`` with columns
        ``Open, High, Low, Close, Volume``.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = list(symbols)
    # "all" (as symbols="all" or the CLI's ["all"]) => the full filtered universe.
    if len(symbols) == 1 and str(symbols[0]).strip().lower() == "all":
        symbols = get_symbols()["symbol"].tolist()
        print(f"downloading price data for all {len(symbols)} filtered symbols")
    # Normalise to Yahoo's ticker format (BRK.B / BRK/B -> BRK-B) and de-dupe.
    yahoo_symbols = list(dict.fromkeys(_norm(s) for s in symbols if str(s).strip()))

    os.makedirs(PRICES_CACHE_DIR, exist_ok=True)

    years = config.get("price.history_years", PRICE_HISTORY_YEARS)
    manifest = _load_manifest()

    to_download = []
    result: dict[str, pd.DataFrame] = {}
    for sym in yahoo_symbols:
        path = _price_cache_path(sym)
        cache_current = manifest.get(sym, {}).get("history_years") == years
        if not refresh and os.path.exists(path) and cache_current:
            result[sym] = _load_price_cache(path)
        else:
            to_download.append(sym)

    # Download the outstanding symbols, caching each the instant it completes.
    if to_download:
        max_workers = config.get("price.max_workers", PRICE_MAX_WORKERS)
        downloaded = _download_and_cache(to_download, manifest, years,
                                         max_workers, label="download")

        # Sweep: symbols that came back empty are more often throttled than
        # truly dataless, so retry them once after a cooldown, at low concurrency.
        empties = [s for s, df in downloaded.items() if df.empty]
        if empties:
            cooldown = config.get("price.sweep_cooldown", PRICE_SWEEP_COOLDOWN)
            print(f"retrying {len(empties)} symbol(s) with no data after "
                  f"{cooldown:.0f}s cooldown...")
            time.sleep(cooldown)
            downloaded.update(_download_and_cache(empties, manifest, years,
                                                  min(2, len(empties)), label="sweep"))

        for sym in to_download:
            result[sym] = downloaded[sym]

        failed = [s for s in to_download if downloaded[s].empty]
        if failed:
            preview = ", ".join(failed[:20]) + (" ..." if len(failed) > 20 else "")
            print(f"finished: {len(to_download) - len(failed)} saved, "
                  f"{len(failed)} returned no data: {preview}")
        else:
            print(f"finished: all {len(to_download)} symbol(s) saved to cache")

    # Preserve the requested order.
    return {sym: result[sym] for sym in yahoo_symbols}


# --------------------------------------------------------------------------- #
# Command-line interface
# --------------------------------------------------------------------------- #
_DISPATCH = {
    "get_symbols": get_symbols,
    "get_symbol": get_symbol_price_data,
    "get_symbol_price_data": get_symbol_price_data,
}

# Options accepted on the command line, per command. Everything else (e.g.
# history_years, which is config-only via vcp.json) is ignored with a warning.
_CLI_OPTIONS = {
    "get_symbols": {"refresh"},
    "get_symbol": {"symbols", "refresh"},
    "get_symbol_price_data": {"symbols", "refresh"},
}


def _coerce(key: str, value: str):
    if key == "symbols":
        return [s.strip() for s in value.split(",") if s.strip()]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.isdigit():
        return int(value)
    return value


def _print_symbols(df: pd.DataFrame) -> None:
    pd.set_option("display.max_rows", 100)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    print(df)
    filters = config.get("filters", {}) or {}
    active = ", ".join(f"{k}={v}" for k, v in filters.items()) or "none"
    print(
        f"\n[{len(df)} stocks after filters: {active}]  "
        f"S&P500={int(df['sp500'].sum())}  "
        f"S&P400={int(df['sp400'].sum())}  "
        f"S&P600={int(df['sp600'].sum())}"
    )


def _print_prices(data: dict) -> None:
    """Print a compact summary: one line per ticker with its daily-record count."""
    print(f"\n{'symbol':<10}{'records':>10}")
    total = 0
    for sym, df in data.items():
        print(f"{sym:<10}{len(df):>10}")
        total += len(df)
    print(f"\n{len(data)} symbol(s), {total} total daily records")


def _main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage:\n"
              "  python data.py get_symbols [refresh=True|False]\n"
              "  python data.py get_symbol_price_data symbols=QQQ,MSFT [refresh=True|False]\n"
              "  python data.py get_symbol_price_data symbols=all [refresh=True|False]")
        return 0 if argv else 1

    func_name, *rest = argv
    func = _DISPATCH.get(func_name)
    if func is None:
        print(f"unknown function: {func_name!r}. available: {', '.join(_DISPATCH)}")
        return 1

    allowed = _CLI_OPTIONS[func_name]
    kwargs = {}
    for arg in rest:
        if "=" not in arg:
            print(f"ignoring positional argument {arg!r}; use key=value")
            continue
        key, val = arg.split("=", 1)
        if key not in allowed:
            print(f"ignoring unknown option {key!r} for {func_name} "
                  f"(valid: {', '.join(sorted(allowed))})")
            continue
        kwargs[key] = _coerce(key, val)

    result = func(**kwargs)
    if func_name == "get_symbols":
        _print_symbols(result)
    else:
        _print_prices(result)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
