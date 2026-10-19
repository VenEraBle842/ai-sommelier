"""Уточнение, персонализация и безопасный запуск без фиктивных вин."""

import asyncio
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from app import demo
from app.api.main import create_app
from app.catalog.repository import WineRepository
from app.db.database import init_database
from app.users.repository import UserRepository


def test_demo_preserves_existing_catalog_and_feedback(tmp_path):
    path = tmp_path / "демо.sqlite3"
    assert demo.prepare_demo(path) == 3
    engine = init_database(path)
    try:
        repository = WineRepository(engine)
        users = UserRepository(engine)
        wine = repository.get(1)
        wine.description = "Сохранённое пользователем описание"
        repository.upsert(wine)
        users.record(UUID(int=1), [1], "like")
        catalog, events = repository.list_wines(), users.history(UUID(int=1))
        assert demo.prepare_demo(path) == 3
        assert repository.list_wines() == catalog
        assert users.history(UUID(int=1)) == events
    finally:
        engine.dispose()


def test_demo_offline_ignores_llm_and_restores_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "учебный-секрет")
    calls = []

    def run(app, **options):
        assert all(key not in demo.os.environ for key in demo.LLM_ENVIRONMENT)
        assert options == {"host": "127.0.0.1", "port": 8765, "log_level": "warning"}
        calls.append(app)

    monkeypatch.setattr(demo.uvicorn, "run", run)
    assert demo.main(["--db", str(tmp_path / "демо.sqlite3"), "--port", "8765", "--offline"]) == 0
    assert len(calls) == 1
    assert demo.os.environ["LLM_API_KEY"] == "учебный-секрет"


def test_demo_invalid_arguments_and_help_are_russian(capsys):
    with pytest.raises(SystemExit) as error:
        demo.main(["--port", "0"])
    assert error.value.code == 2
    assert "Ошибка: проверьте параметры" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        demo.main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "Параметры" in output
    assert "Запустить винный гид" in output
    assert "options:" not in output


def test_clarification_and_feedback_over_http(tmp_path, monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args)

    monkeypatch.setattr("anyio.to_thread.run_sync", inline)
    path = tmp_path / "демо.sqlite3"
    demo.prepare_demo(path)
    app = create_app(path)

    async def verify():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                user = str(UUID(int=1))
                first = (
                    await client.post("/chat", json={"user_id": user, "message": "Сухое"})
                ).json()
                assert first["status"] == "clarification"
                assert first["intent"]["missing_information"] == ["color"]
                clarified = (
                    await client.post(
                        "/chat",
                        json={
                            "user_id": user,
                            "message": "На ваш вкус, Кубань",
                            "previous_intent": first["intent"],
                        },
                    )
                ).json()
                assert clarified["status"] == "ok"
                assert [r["wine_id"] for r in clarified["recommendations"]] == [1, 3, 2]
                for r in clarified["recommendations"]:
                    wine = app.state.repository.get(r["wine_id"])
                    assert r["wine"] == wine.model_dump(mode="json")
                    assert wine.price_rub is None
                    assert r["reasons"]
                    assert wine.source_url.startswith("https://vino-svoe.ru/wines/")
                feedback = await client.post(
                    "/feedback", json={"user_id": user, "wine_id": 2, "event_type": "like"}
                )
                assert feedback.status_code == 200
                repeated = (
                    await client.post("/chat", json={"user_id": user, "message": "Сухое Кубань"})
                ).json()
                assert [r["wine_id"] for r in repeated["recommendations"]] == [2, 3, 1]
                top = repeated["recommendations"][0]
                assert top["wine"]["name"] == "Шенен Блан. Бетон. Дрозд"
                assert any(
                    p["key"] == "personalization" and Decimal(p["score"]) > 0
                    for p in top["breakdown"]
                )
                assert any("понравилось это вино" in r for r in top["reasons"])

    asyncio.run(verify())
