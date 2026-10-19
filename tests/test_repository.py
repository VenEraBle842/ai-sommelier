"""Проверки сохранения, повторной загрузки и независимости от сети."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.catalog.models import WineData
from app.db.database import init_database


def test_upsert_preserves_id_and_updates_fields(repository, merlo):
    first = repository.upsert(merlo)
    values = merlo.model_dump()
    values.update(
        description="Обновлённое учебное описание",
        scraped_at=merlo.scraped_at + timedelta(minutes=5),
    )
    second = repository.upsert(WineData.model_validate(values))
    assert second.id == first.id
    assert second.description == values["description"]
    assert second.scraped_at == values["scraped_at"]
    assert repository.count() == 1
    assert repository.get(first.id) == second
    assert repository.get_by_source_url(merlo.source_url + "/?utm=1") == second


def test_missing_price_is_sql_null(repository, merlo):
    saved = repository.upsert(merlo)
    assert saved.price_rub is None
    with repository.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT price_rub IS NULL FROM wines").scalar() == 1


def test_decimal_price_roundtrip_for_future_trusted_provider(repository, merlo):
    values = merlo.model_dump()
    values["price_rub"] = Decimal("1234.56")
    saved = repository.upsert(WineData.model_validate(values))
    assert repository.get(saved.id).price_rub == Decimal("1234.56")


def test_empty_reads_and_pagination(repository, merlo):
    assert repository.count() == 0
    assert repository.list_wines() == []
    assert repository.get(999) is None
    repository.upsert(merlo)
    assert len(repository.list_wines(limit=1)) == 1
    assert repository.list_wines(offset=1) == []
    with pytest.raises(ValueError):
        repository.list_wines(limit=0)


def test_snapshot_clears_fields_absent_in_new_source(repository, merlo):
    repository.upsert(merlo)
    values = merlo.model_dump()
    values.update(description=None, rating=None, grapes=[], food_pairings=[])
    saved = repository.upsert(WineData.model_validate(values))
    assert saved.description is saved.rating is None
    assert saved.grapes == saved.food_pairings == []


def test_database_initialization_is_repeatable(tmp_path):
    path = tmp_path / "новая-папка" / "catalog.sqlite3"
    for _ in range(2):
        engine = init_database(path)
        engine.dispose()
    assert path.is_file()


def test_database_survives_reopening(tmp_path, merlo):
    from app.catalog.repository import WineRepository

    path = tmp_path / "catalog.sqlite3"
    engine = init_database(path)
    first = WineRepository(engine).upsert(merlo)
    engine.dispose()
    engine = init_database(path)
    try:
        assert WineRepository(engine).get(first.id) == first
    finally:
        engine.dispose()
