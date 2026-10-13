import shutil
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits the real chess.com API (skip offline: -m 'not live')")
    config.addinivalue_line("markers", "engine: needs Stockfish")


@pytest.fixture(scope="session")
def engine():
    from chess_coach.analysis import Engine
    from chess_coach.config import stockfish_path

    try:
        stockfish_path()
    except FileNotFoundError:
        pytest.skip("stockfish not installed")
    e = Engine()
    yield e
    e.close()


@pytest.fixture
def tmp_store(tmp_path):
    from chess_coach.db import Store

    return Store(tmp_path / "cache.db")
