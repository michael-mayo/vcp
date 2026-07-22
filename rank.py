"""rank — momentum ranking of the filtered stock universe.

Builds on the :mod:`data` layer (which already applies the liquidity ``filters``
from ``vcp.json`` to remove illiquid stocks) and applies a momentum filter and
ranking on top:

  1. Pull the filtered symbol universe and its daily price history (from the
     cache, or freshly downloaded when ``refresh=True``).
  2. For each symbol, measure momentum as how far its latest close sits above
     its lowest low over the trailing ``momentum_period`` days.
  3. Keep only stocks at least ``momentum_filter`` percent above that low whose
     latest ``VCP_Rank`` (from :mod:`vcp`) is at least ``min_vcp``, and return
     them ranked by momentum (strongest first). Symbols without enough history
     to compute ``VCP_Rank`` are dropped.

All three settings live in the ``rank`` section of ``vcp.json``:
``momentum_period`` (default 242 trading days ≈ one year), ``momentum_filter``
(default 30 percent), and ``min_vcp`` — the ``VCP_Rank`` floor (default 90).

Usage
-----
Python::

    import rank
    df = rank.rank()                # ranked DataFrame, from cache
    df = rank.rank(refresh=True)    # re-download data first

Command line::

    python rank.py                  # refresh defaults to False
    python rank.py refresh=True
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import data
import config
import vcp

# Trailing window (trading days) over which the "n-day low" is measured.
MOMENTUM_PERIOD = 242  # fallback if not in vcp.json ("rank.momentum_period")

# Minimum percent above that low a stock must be to survive the filter.
MOMENTUM_FILTER = 30  # fallback if not in vcp.json ("rank.momentum_filter")

# Minimum VCP_Rank a stock must have to appear in the ranking.
MIN_VCP = 90  # fallback if not in vcp.json ("rank.min_vcp")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def rank(refresh: bool = False) -> pd.DataFrame:
    """Return the filtered universe ranked by momentum (strongest first).

    Momentum is the percent the latest close sits above the lowest low of the
    trailing ``rank.momentum_period`` days. Only stocks at least
    ``rank.momentum_filter`` percent above that low, and with a latest
    ``VCP_Rank`` of at least ``rank.min_vcp``, are kept.

    Parameters
    ----------
    refresh:
        If ``True`` both the symbol universe and every symbol's price history
        are re-downloaded and the cache is rewritten. If ``False`` (default)
        cached data is used where present (and downloaded only when missing).

    Returns
    -------
    pandas.DataFrame
        Indexed by ``rank`` (1 = strongest momentum) with columns ``symbol``,
        ``sector`` (from :func:`data.get_symbols`), ``last_close``,
        ``period_low`` (the trailing ``momentum_period`` low),
        ``pct_above_low`` (momentum, in percent), and ``VCP_Rank`` (the latest
        volatility-contraction percentile from :func:`vcp.get_symbol_price_data`).
        Stocks that don't clear the momentum filter, fall below ``min_vcp``, or
        lack enough history to compute ``VCP_Rank`` are dropped; empty if none
        remain.
    """
    period = config.get("rank.momentum_period", MOMENTUM_PERIOD)
    threshold = config.get("rank.momentum_filter", MOMENTUM_FILTER)
    min_vcp = config.get("rank.min_vcp", MIN_VCP)

    universe = data.get_symbols(refresh=refresh)
    symbols = universe["symbol"].tolist()
    prices = data.get_symbol_price_data(symbols=symbols, refresh=refresh)

    # Map each ticker to its sector, keyed on the same normalised form the price
    # dict uses (so share-class tickers like BRK.B / BRK-B still line up).
    sector_by_symbol = {
        data._norm(sym): sec for sym, sec in zip(universe["symbol"], universe["sector"])
    }

    rows = []
    for symbol, df in prices.items():
        row = _momentum(df, period)
        if row is not None:
            sector = sector_by_symbol.get(symbol, "Unknown")
            rows.append({"symbol": symbol, "sector": sector, **row})

    # Apply the momentum filter and order the survivors strongest-first.
    result = pd.DataFrame(
        rows, columns=["symbol", "sector", "last_close", "period_low", "pct_above_low"]
    )
    result = result[result["pct_above_low"] >= threshold]
    result = result.sort_values("pct_above_low", ascending=False).reset_index(drop=True)

    # Annotate each survivor with its latest VCP_Rank (cache is already current,
    # so refresh=False), then drop symbols without enough history to compute it.
    if not result.empty:
        print(f"computing VCP_Rank for {len(result)} ranked symbol(s)...")
    result["VCP_Rank"] = [_vcp_rank(symbol) for symbol in result["symbol"]]
    result = result.dropna(subset=["VCP_Rank"])
    result = result[result["VCP_Rank"] >= min_vcp].reset_index(drop=True)

    result.index = result.index + 1  # 1-based rank
    result.index.name = "rank"
    return result


def _momentum(df: pd.DataFrame, period: int) -> dict | None:
    """Momentum stats for one price frame, or ``None`` if it can't be computed.

    Needs a full ``period`` window of history; the "low" is the lowest daily
    ``Low`` in that window and momentum is the latest ``Close`` expressed as a
    percent above it.
    """
    if df.empty or len(df) < period:
        return None
    period_low = df["Low"].tail(period).min()
    last_close = df["Close"].iloc[-1]
    if pd.isna(period_low) or pd.isna(last_close) or period_low <= 0:
        return None
    return {
        "last_close": last_close,
        "period_low": period_low,
        "pct_above_low": (last_close / period_low - 1.0) * 100.0,
    }


def _vcp_rank(symbol: str) -> float:
    """Latest VCP_Rank for ``symbol``, or ``NaN`` if it can't be computed.

    Reads from the (already-current) cache via the frozen :mod:`vcp` module;
    symbols with too little history raise ``ValueError`` there and map to NaN.
    The ``exp`` overflow warning some illiquid tickers trigger inside ``vcp`` is
    harmless (the signal is ranked immediately after) and is suppressed here.
    """
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            df = vcp.get_symbol_price_data(symbol)
    except ValueError:
        return float("nan")
    if df.empty:
        return float("nan")
    return float(df["VCP_Rank"].iloc[-1])


# --------------------------------------------------------------------------- #
# Command-line interface
# --------------------------------------------------------------------------- #
def _coerce_refresh(value: str) -> bool:
    return value.strip().lower() == "true"


def _print_ranking(df: pd.DataFrame, period: int, threshold, min_vcp) -> None:
    if df.empty:
        print(f"no stocks are ≥{threshold}% above their {period}-day low "
              f"with VCP_Rank ≥ {min_vcp}")
        return
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")
    print(df)
    print(f"\n[{len(df)} stocks ranked by momentum "
          f"(≥{threshold}% above their {period}-day low, VCP_Rank ≥ {min_vcp})]")


def _main(argv) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print("usage:\n  python rank.py [refresh=True|False]")
        return 0

    kwargs = {}
    for arg in argv:
        if "=" not in arg:
            print(f"ignoring positional argument {arg!r}; use refresh=True|False")
            continue
        key, val = arg.split("=", 1)
        if key != "refresh":
            print(f"ignoring unknown option {key!r} (valid: refresh)")
            continue
        kwargs["refresh"] = _coerce_refresh(val)

    result = rank(**kwargs)
    period = config.get("rank.momentum_period", MOMENTUM_PERIOD)
    threshold = config.get("rank.momentum_filter", MOMENTUM_FILTER)
    min_vcp = config.get("rank.min_vcp", MIN_VCP)
    _print_ranking(result, period, threshold, min_vcp)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
