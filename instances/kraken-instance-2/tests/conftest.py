"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_entry_threshold():
    """Isolate mutable ENTRY_THRESHOLD across the suite."""
    from trading_bot.utils.entry_proximity import (
        DEFAULT_ENTRY_THRESHOLD,
        set_entry_threshold,
    )

    prev = os.environ.get("ENTRY_THRESHOLD")
    os.environ["ENTRY_THRESHOLD"] = str(int(DEFAULT_ENTRY_THRESHOLD))
    set_entry_threshold(DEFAULT_ENTRY_THRESHOLD)
    yield
    set_entry_threshold(DEFAULT_ENTRY_THRESHOLD)
    if prev is None:
        os.environ.pop("ENTRY_THRESHOLD", None)
    else:
        os.environ["ENTRY_THRESHOLD"] = prev
