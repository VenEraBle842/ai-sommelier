"""Общие фикстуры: изолированная SQLite и полный запрет реальной сети."""

import socket
from pathlib import Path

import pytest

from app.catalog.parser import parse_wine
from app.catalog.repository import WineRepository
from app.db.database import init_database

FIXTURES = Path(__file__).parent / "fixtures"
MERLO_URL = "https://vino-svoe.ru/wines/merlo-drozd"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Любой случайный сетевой запрос в тесте должен завершиться ошибкой."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Сетевые запросы в модульных тестах запрещены.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def merlo_html():
    return (FIXTURES / "merlo.html").read_text(encoding="utf-8")


@pytest.fixture
def merlo(merlo_html):
    return parse_wine(merlo_html, MERLO_URL)


@pytest.fixture
def repository(tmp_path):
    engine = init_database(tmp_path / "вложенный" / "catalog.sqlite3")
    yield WineRepository(engine)
    engine.dispose()
