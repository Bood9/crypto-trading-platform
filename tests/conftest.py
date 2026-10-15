"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_book_locks(tmp_path, monkeypatch):
    """Book locks live in a temp directory: a test must not hold the
    real lock file and get in the way of the nightly run."""
    import src.live.lock as lock

    monkeypatch.setattr(lock, "LOCK_DIR", tmp_path / "locks")
