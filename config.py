"""Configuration loader for the Shopify margin dashboard.

All cost/fee assumptions live in ``config.yaml``. This module loads them and
provides a helper to overlay runtime overrides (from the Streamlit sidebar) on
top, without ever mutating the file on disk. The calculation logic in
``margin.py`` consumes the resulting dict and contains no hard-coded numbers.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    """Read config.yaml into a plain nested dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def deep_merge(base: dict, overrides: dict) -> dict:
    """Return a deep-merged copy of ``base`` with ``overrides`` applied.

    Only keys present in ``overrides`` are touched, so the sidebar can override
    a single rate without restating the whole config.
    """
    out = copy.deepcopy(base)
    for key, val in (overrides or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out
