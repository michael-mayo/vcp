"""config — shared configuration for all programs in this project.

Reads ``vcp.json`` (located next to this module) and exposes it to any program::

    import config
    cfg = config.load()                       # full config dict
    min_mc = config.get("filters.min_market_cap", 0)   # dotted-path lookup

The file is read once and cached; pass ``reload=True`` (or call
:func:`reload`) to pick up on-disk changes within a running process.
"""

from __future__ import annotations

import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vcp.json")

_cache: dict | None = None


def load(reload: bool = False) -> dict:
    """Return the parsed ``vcp.json`` config (cached after first read).

    Returns an empty dict (with a warning) if the file is missing so that
    callers degrade gracefully rather than crash.
    """
    global _cache
    if _cache is None or reload:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except FileNotFoundError:
            print(f"warning: config file not found at {CONFIG_PATH}; using empty config")
            _cache = {}
    return _cache


def reload() -> dict:
    """Force a re-read of ``vcp.json`` from disk."""
    return load(reload=True)


def get(dotted_key: str, default=None):
    """Look up a value by dotted path, e.g. ``"filters.min_market_cap"``."""
    node = load()
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node
