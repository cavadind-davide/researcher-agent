"""Gemeinsame Test-Fixtures."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from researcher import store


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Leite den gesamten Store auf eine temporäre SQLite-Datei um.

    Die Store-Funktionen rufen ``connect()`` ohne Argument auf, dessen Default
    ``DB_PATH`` bereits zur Definitionszeit gebunden ist. Ein Monkeypatch von
    ``store.DB_PATH`` allein würde sie also nicht erreichen — daher ersetzen wir
    ``store.connect`` durch eine Variante, die immer die Temp-DB nutzt.
    """
    db = tmp_path / "test.sqlite"
    real_connect = store.connect

    @contextmanager
    def connect_temp(db_path=None):
        with real_connect(db) as conn:
            yield conn

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "connect", connect_temp)
    store.init_db(db)
    yield db
