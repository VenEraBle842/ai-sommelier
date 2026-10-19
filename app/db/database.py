"""Создание SQLite и фабрики подключений."""

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from app.db.tables import Base

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "catalog.sqlite3"


def init_database(path: str | Path | None = None) -> Engine:
    """Создать каталог и таблицы при первом запуске без внешней СУБД."""
    path = Path(path or os.environ.get("WINE_DB_PATH", str(DEFAULT_DB)))
    options: dict = {"connect_args": {"check_same_thread": False, "timeout": 10}}
    if str(path) == ":memory:":
        url = "sqlite+pysqlite:///:memory:"
        options["poolclass"] = StaticPool
    else:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url, **options)
    Base.metadata.create_all(engine)
    return engine
