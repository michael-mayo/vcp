"""vcp — Volatility Contraction Pattern analysis built on the ``data`` layer.

Provides :func:`get_symbol_price_data`, which loads a symbol's adjusted daily
OHLCV price history (via :mod:`data`) and enriches it with:

  * ``<src>_EMA_short``   — short-period EMA of ``High``, ``Low``, ``Close`` and ``Volume``
  * ``<src>_EMA_long``    — long-period EMA of ``High``, ``Low``, ``Close`` and ``Volume``
  * ``ADR``               — Average Daily Range percent over ``adr_period`` days
  * ``Signal_Low``        — percentile rank of ``exp(Low_EMA_short/Low_EMA_long)``
                            (high when the daily lows are rising)
  * ``Signal_Volume``     — percentile rank of ``exp(Volume_EMA_long/Volume_EMA_short)``
                            (high when volume is drying up)
  * ``Signal_High``       — percentile rank of the symmetric high-EMA ratio
                            (high when the daily highs are going sideways)
  * ``VCP``               — composite contraction score, the product of the
                            three signal ranks (0-100)
  * ``VCP_Rank``          — percentile rank of ``VCP``, comparable across symbols

Each signal is ranked over the trailing ``signal_rank_period`` window so it is
self-normalising per symbol; ``VCP_Rank`` ranks the composite the same way. The
EMA periods, ADR look-back and ranking window are all read from the ``vcp``
section of ``vcp.json`` (``ema_short_period`` / ``ema_long_period`` /
``adr_period`` / ``signal_rank_period``).

Usage
-----
Python::

    import vcp
    df = vcp.get_symbol_price_data("MSFT", refresh=True)   # returns a DataFrame

Command line::

    python vcp.py get_symbol_price_data symbol=MSFT refresh=True
    python vcp.py get_symbol_price_data symbol=MSFT        # refresh defaults to False

    # Save a three-panel plot of the last 252 records instead of printing:
    python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png
    python vcp.py get_symbol_price_data symbol=MSFT plot_filename=msft.png \
        plot_from_rec=-100 plot_to_rec=-1
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import data
import config

# A short- and long-period EMA is computed for each of these columns, in order
# (High/Low EMAs land before the Close EMAs in the output frame).
EMA_SOURCES = ("High", "Low", "Close", "Volume")
EMA_SHORT_PERIOD = 10  # fallback if not in vcp.json ("vcp.ema_short_period")
EMA_LONG_PERIOD = 20   # fallback if not in vcp.json ("vcp.ema_long_period")

# Look-back for the Average Daily Range percent (ADR) column.
ADR_PERIOD = 20  # fallback if not in vcp.json ("vcp.adr_period")

# Trailing window (trading days) for the percentile rank of each Signal line
# (and of the composite VCP score that feeds VCP_Rank).
SIGNAL_RANK_PERIOD = 500  # fallback if not in vcp.json ("vcp.signal_rank_period")


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
    """Return adjusted daily OHLCV for ``symbol`` with EMA and ADR columns.

    Parameters
    ----------
    symbol:
        Ticker to load (e.g. ``"MSFT"``).
    refresh:
        Passed through to :func:`data.get_symbol_price_data` — ``True`` forces a
        re-download, ``False`` (default) uses the cache when present.
    plot_filename:
        If given, a three-panel plot (Close + Close EMAs, ADR%, and the signal
        ranks with VCP_Rank) is saved to this path (format inferred from the
        extension, e.g. ``.png``). When ``None`` (default) no plot is produced.
    plot_from_rec, plot_to_rec:
        Inclusive record range to plot, as positions into the date-sorted frame.
        Negative values count from the end (``-1`` is the last record), positive
        values count from the start (``0`` is the first). Defaults ``-252`` to
        ``-1`` plot roughly the last year of trading days. Ignored when
        ``plot_filename`` is ``None``.

    Returns
    -------
    pandas.DataFrame
        Indexed by ``Date`` with columns ``Open, High, Low, Close, Volume``,
        the EMA columns (``High``/``Low``/``Close``/``Volume`` each with a
        ``_EMA_short`` and ``_EMA_long``, in that order), and
        ``ADR`` — the Average Daily Range percent over ``vcp.adr_period`` days
        (the initial ``adr_period - 1`` warm-up rows without a full window are
        dropped) — and three signal lines. Each signal is first built from the
        EMA ratios via ``f(x, y) = exp(x / y)`` — ``Signal_Low`` =
        ``f(Low_EMA_short, Low_EMA_long)``, ``Signal_Volume`` =
        ``f(Volume_EMA_long, Volume_EMA_short)``, ``Signal_High`` =
        ``min(f(High_EMA_short, High_EMA_long), f(High_EMA_long,
        High_EMA_short))`` — then converted to a causal percentile rank (0-100)
        over the shared trailing ``vcp.signal_rank_period`` window, so the three
        share a common scale. The warm-up rows without a full ranking window are
        dropped. A ``VCP`` column is the composite contraction score
        ``(Signal_Low/100) * (Signal_Volume/100) * Signal_High`` (the product of
        the three ranks scaled to 0-100; high only when all three signals rank
        high at once), and ``VCP_Rank`` is the causal percentile (0-100) of
        ``VCP`` over the same ``vcp.signal_rank_period`` window, making the
        composite comparable across symbols (its warm-up rows are dropped too,
        so a full ``VCP_Rank`` needs two stacked ranking windows of history). An
        empty frame is returned if no price data is available.

    Raises
    ------
    ValueError
        If price data exists but there are too few daily records to fill a
        single ``vcp.signal_rank_period`` window (so no signal ranks can be
        computed).
    """
    prices = data.get_symbol_price_data(symbols=[symbol], refresh=refresh)
    # One symbol in -> one frame out; grab it regardless of the dict key.
    df = next(iter(prices.values())).copy()
    if df.empty:
        return df

    short = config.get("vcp.ema_short_period", EMA_SHORT_PERIOD)
    long = config.get("vcp.ema_long_period", EMA_LONG_PERIOD)

    for source in EMA_SOURCES:
        df[f"{source}_EMA_short"] = df[source].ewm(span=short, adjust=False).mean()
        df[f"{source}_EMA_long"] = df[source].ewm(span=long, adjust=False).mean()

    # Average Daily Range percent: mean of the daily High/Low ratio over the
    # look-back window, expressed as a percentage (Minervini-style ADR%).
    adr_period = config.get("vcp.adr_period", ADR_PERIOD)
    df["ADR"] = ((df["High"] / df["Low"]).rolling(adr_period).mean() - 1.0) * 100.0
    # Drop the early warm-up rows that lack a full ADR window.
    df = df.dropna(subset=["ADR"])

    # Signal lines built from the EMA ratios via f(x, y) = exp(x / y), then
    # converted to a causal percentile rank (0-100) over a shared trailing
    # window so the three land on a common, dominance-free scale.
    df["Signal_Low"] = np.exp(df["Low_EMA_short"] / df["Low_EMA_long"])
    df["Signal_Volume"] = np.exp(df["Volume_EMA_long"] / df["Volume_EMA_short"])
    df["Signal_High"] = np.minimum(
        np.exp(df["High_EMA_short"] / df["High_EMA_long"]),
        np.exp(df["High_EMA_long"] / df["High_EMA_short"]),
    )
    rank_period = config.get("vcp.signal_rank_period", SIGNAL_RANK_PERIOD)
    signal_cols = ["Signal_Low", "Signal_Volume", "Signal_High"]
    for col in signal_cols:
        df[col] = _causal_pct_rank(df[col], rank_period)
    # Drop the warm-up rows that lack a full ranking window; if none survive
    # there is not enough daily history to compute the signals -- flag it.
    df = df.dropna(subset=signal_cols)
    if df.empty:
        raise ValueError(
            f"insufficient price history for {symbol!r}: need more than "
            f"{rank_period} daily records (after the {adr_period}-day ADR "
            f"warm-up) to compute the signal percentile ranks"
        )

    # Composite VCP score: product of the three signal ranks (each scaled to
    # 0-1), rescaled to 0-100 to share the signals' axis. High only when all
    # three contraction conditions hold at once.
    df["VCP"] = (df["Signal_Low"] / 100.0) * (df["Signal_Volume"] / 100.0) * df["Signal_High"]

    # VCP_Rank: causal percentile of VCP over the same trailing window, making
    # the composite itself comparable across symbols regardless of how the three
    # signals co-move. Needs its own full window on top of the signal warm-up.
    df["VCP_Rank"] = _causal_pct_rank(df["VCP"], rank_period)
    df = df.dropna(subset=["VCP_Rank"])
    if df.empty:
        raise ValueError(
            f"insufficient price history for {symbol!r}: need more than "
            f"{2 * rank_period} daily records (after the {adr_period}-day ADR "
            f"warm-up) to compute VCP_Rank (two stacked {rank_period}-day "
            f"ranking windows)"
        )

    if plot_filename:
        _save_close_ema_plot(
            df, symbol, plot_filename, plot_from_rec, plot_to_rec, short, long,
        )
    return df


def _causal_pct_rank(series: pd.Series, window: int) -> pd.Series:
    """Causal percentile rank (0-100) of each value within its trailing window.

    Uses only the trailing ``window`` observations (no look-ahead), so the
    result is backtest-safe; the leading ``window - 1`` rows without a full
    window are left ``NaN``.
    """
    return series.rolling(window).apply(
        lambda w: (w <= w[-1]).mean() * 100.0, raw=True
    )


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
    """Save a three-panel plot of ``df[from_rec:to_rec]`` to disk.

    Panels share the x-axis: Close + Close EMAs on top, ADR% in the middle, and
    the three signal ranks with the composite VCP_Rank at the bottom.
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

    # Three stacked panels sharing the x-axis: price/EMAs on top, ADR% in the
    # middle, and the signal lines at the bottom.
    fig, (ax_price, ax_adr, ax_sig) = plt.subplots(
        3, 1, figsize=(12, 10), sharex=True,
        gridspec_kw={"height_ratios": [3, 1, 1]},
    )

    ax_price.plot(window.index, window["Close"], label="Close", color="black", linewidth=1.3)
    ax_price.plot(window.index, window["Close_EMA_short"], label=f"Close EMA ({short})", linewidth=1.0)
    ax_price.plot(window.index, window["Close_EMA_long"], label=f"Close EMA ({long})", linewidth=1.0)
    ax_price.set_title(f"{symbol} — Close & EMAs "
                       f"({window.index.min().date()} → {window.index.max().date()})")
    ax_price.set_ylabel("Price")
    ax_price.legend()
    ax_price.grid(True, alpha=0.3)

    ax_adr.plot(window.index, window["ADR"], label="ADR%", color="tab:orange", linewidth=1.0)
    ax_adr.set_ylabel("ADR %")
    ax_adr.legend(loc="upper left")
    ax_adr.grid(True, alpha=0.3)

    # Component signal ranks (faded) with the composite VCP_Rank (bold) on top,
    # all sharing the same 0-100 axis.
    ax_sig.plot(window.index, window["Signal_Low"], label="Signal_Low", color="tab:blue", linewidth=0.8, alpha=0.4)
    ax_sig.plot(window.index, window["Signal_Volume"], label="Signal_Volume", color="tab:green", linewidth=0.8, alpha=0.4)
    ax_sig.plot(window.index, window["Signal_High"], label="Signal_High", color="tab:red", linewidth=0.8, alpha=0.4)
    ax_sig.plot(window.index, window["VCP_Rank"], label="VCP_Rank", color="black", linewidth=1.8)
    ax_sig.set_ylabel("Signal rank / VCP_Rank")
    ax_sig.set_ylim(0, 100)
    ax_sig.set_xlabel("Date")
    ax_sig.legend(loc="upper left", ncol=4)
    ax_sig.grid(True, alpha=0.3)

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

    try:
        result = func(**kwargs)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_symbol(result)
    if kwargs.get("plot_filename") and not result.empty:
        print(f"plot saved to {kwargs['plot_filename']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
