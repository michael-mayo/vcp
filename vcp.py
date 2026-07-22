"""vcp — Volatility Contraction Pattern analysis built on the ``data`` layer.

Provides :func:`get_symbol_price_data`, which loads a symbol's adjusted daily
OHLCV price history (via :mod:`data`) and enriches it with:

  * ``Range``             — the daily high-low range (High - Low)
  * ``<src>_EMA_short``   — short-period EMA of ``Close``, ``Range``, ``Volume``
  * ``<src>_EMA_long``    — long-period EMA of ``Close``, ``Range``, ``Volume``

The two EMA periods are read from the ``vcp`` section of ``vcp.json``
(``ema_short_period`` / ``ema_long_period``).

Usage
-----
Python::

    import vcp
    df = vcp.get_symbol_price_data("MSFT", refresh=True)   # returns a DataFrame

Command line::

    python vcp.py get_symbol_price_data symbol=MSFT refresh=True
    python vcp.py get_symbol_price_data symbol=MSFT        # refresh defaults to False

    # Save a Close + EMA plot of the last 252 records instead of printing:
    python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png
    python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png \
        plot_from_rec=-100 plot_to_rec=-1
"""

from __future__ import annotations

import sys

import pandas as pd

import data
import config

# A short- and long-period EMA is computed for each of these columns.
EMA_SOURCES = ("Close", "Range", "Volume")
EMA_SHORT_PERIOD = 10  # fallback if not in vcp.json ("vcp.ema_short_period")
EMA_LONG_PERIOD = 20   # fallback if not in vcp.json ("vcp.ema_long_period")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_symbol_price_data(
    symbol: str,
    refresh: bool = False,
    plot_filename: str | None = None,
    plot_from_rec: int = -252,
    plot_to_rec: int = -1,
) -> pd.DataFrame:
    """Return adjusted daily OHLCV for ``symbol`` with Range and EMA columns.

    Parameters
    ----------
    symbol:
        Ticker to load (e.g. ``"MSFT"``).
    refresh:
        Passed through to :func:`data.get_symbol_price_data` — ``True`` forces a
        re-download, ``False`` (default) uses the cache when present.
    plot_filename:
        If given, a Close-price plot (with the short and long Close EMAs) is
        saved to this path (format inferred from the extension, e.g. ``.png``).
        When ``None`` (default) no plot is produced.
    plot_from_rec, plot_to_rec:
        Inclusive record range to plot, as positions into the date-sorted frame.
        Negative values count from the end (``-1`` is the last record), positive
        values count from the start (``0`` is the first). Defaults ``-252`` to
        ``-1`` plot roughly the last year of trading days. Ignored when
        ``plot_filename`` is ``None``.

    Returns
    -------
    pandas.DataFrame
        Indexed by ``Date`` with columns ``Open, High, Low, Close, Volume,
        Range`` and the EMA columns (e.g. ``Close_EMA_short``,
        ``Volume_EMA_long``). Empty if no price data is available for ``symbol``.
    """
    prices = data.get_symbol_price_data(symbols=[symbol], refresh=refresh)
    # One symbol in -> one frame out; grab it regardless of the dict key.
    df = next(iter(prices.values())).copy()
    if df.empty:
        return df

    short = config.get("vcp.ema_short_period", EMA_SHORT_PERIOD)
    long = config.get("vcp.ema_long_period", EMA_LONG_PERIOD)

    df["Range"] = df["High"] - df["Low"]
    for source in EMA_SOURCES:
        df[f"{source}_EMA_short"] = df[source].ewm(span=short, adjust=False).mean()
        df[f"{source}_EMA_long"] = df[source].ewm(span=long, adjust=False).mean()

    if plot_filename:
        _save_close_ema_plot(
            df, symbol, plot_filename, plot_from_rec, plot_to_rec, short, long
        )
    return df


def _resolve_rec(idx: int, n: int) -> int:
    """Map a possibly-negative record index to a clamped 0-based position."""
    pos = idx if idx >= 0 else n + idx
    return max(0, min(pos, n - 1))


def _save_close_ema_plot(
    df: pd.DataFrame,
    symbol: str,
    filename: str,
    from_rec: int,
    to_rec: int,
    short: int,
    long: int,
) -> None:
    """Save a Close-price + Close-EMA plot of ``df[from_rec:to_rec]`` to disk.

    ``from_rec``/``to_rec`` are inclusive record positions (negative counts from
    the end); matplotlib is imported lazily so it is only required when plotting.
    """
    import matplotlib
    matplotlib.use("Agg")  # non-interactive, file-only backend
    import matplotlib.pyplot as plt

    n = len(df)
    start = _resolve_rec(from_rec, n)
    stop = _resolve_rec(to_rec, n)
    if start > stop:
        start, stop = stop, start
    window = df.iloc[start:stop + 1]

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(window.index, window["Close"], label="Close", color="black", linewidth=1.3)
    ax.plot(window.index, window["Close_EMA_short"], label=f"Close EMA ({short})", linewidth=1.0)
    ax.plot(window.index, window["Close_EMA_long"], label=f"Close EMA ({long})", linewidth=1.0)
    ax.set_title(f"{symbol} — Close & EMAs "
                 f"({window.index.min().date()} → {window.index.max().date()})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(filename, dpi=120)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Command-line interface
# --------------------------------------------------------------------------- #
_DISPATCH = {"get_symbol_price_data": get_symbol_price_data}
_CLI_OPTIONS = {
    "get_symbol_price_data": {
        "symbol", "refresh", "plot_filename", "plot_from_rec", "plot_to_rec",
    }
}


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)  # handles negatives (e.g. plot_from_rec=-252)
    except ValueError:
        return value


def _print_symbol(df: pd.DataFrame) -> None:
    if df.empty:
        print("no price data available for that symbol")
        return
    pd.set_option("display.max_rows", 60)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")
    print(df)
    print(f"\n[{len(df)} daily records, {df.index.min().date()} -> {df.index.max().date()}]")


def _main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage:\n"
              "  python vcp.py get_symbol_price_data symbol=MSFT [refresh=True|False]\n"
              "    [plot_filename=out.png] [plot_from_rec=-252] [plot_to_rec=-1]")
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
        kwargs[key] = _coerce(val)

    result = func(**kwargs)
    _print_symbol(result)
    if kwargs.get("plot_filename") and not result.empty:
        print(f"plot saved to {kwargs['plot_filename']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
