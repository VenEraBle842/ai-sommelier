"""API использует сохранённый снимок и возвращает русские ошибки."""

import asyncio

import httpx
import pytest
from fastapi import Request
from starlette.exceptions import HTTPException

from app.api.main import create_app
from app.catalog.repository import WineRepository
from app.db.database import init_database


def test_api_reads_only_persisted_records(tmp_path, merlo):
    path = tmp_path / "catalog.sqlite3"
    engine = init_database(path)
    saved = WineRepository(engine).upsert(merlo)
    engine.dispose()
    app = create_app(path)

    async def verify():
        async with app.router.lifespan_context(app):
            # Обработчики проверяются напрямую: тест не требует сокетов и фонового сервера.
            routes = {
                route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")
            }
            request = Request({"type": "http", "app": app})
            wines = routes["/wines"](request, limit=50, offset=0)
            assert [wine.id for wine in wines] == [saved.id]
            assert wines[0].price_rub is None
            assert wines[0].source_url == merlo.source_url
            assert routes["/wines/{wine_id}"](saved.id, request).name == "Мерло. Дрозд"
            assert routes["/wines"](request, limit=50, offset=1) == []
            with pytest.raises(HTTPException) as error:
                routes["/wines/{wine_id}"](999, request)
            assert error.value.status_code == 404

    asyncio.run(verify())


def test_api_creates_empty_database_and_localizes_errors(tmp_path):
    path = tmp_path / "новая" / "catalog.sqlite3"
    app = create_app(path)

    async def verify():
        async with app.router.lifespan_context(app):
            assert app.state.repository.list_wines() == []
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for url in ("/wines?limit=0", "/wines?limit=501", "/wines?offset=-1", "/wines/нет"):
                    response = await client.get(url)
                    assert response.status_code == 422
                    assert response.json()["detail"].startswith("Некорректные параметры")
                response = await client.get("/unknown")
                assert response.json()["detail"] == "Запись или страница не найдена."
                response = await client.post("/wines")
                assert response.json()["detail"] == "Этот HTTP-метод не поддерживается."

    asyncio.run(verify())
    assert path.is_file()
