"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_db_path(monkeypatch, tmp_path):
    """Each test gets its own DB unless it overrides db_path itself."""
    db = tmp_path / "test.db"
    monkeypatch.setattr("clipsys._env.settings.db_path", db)
    from clipsys.db.db import init_db
    init_db(db)
    yield
