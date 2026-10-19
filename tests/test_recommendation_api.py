"""Проверка API рекомендаций без порта, внешней модели и межпоточных сокетов."""

import asyncio

import httpx
import pytest
from fastapi import Request
from pydantic import ValidationError

from app.api.main import create_app
from app.llm.fake import FakeLLM
from app.recommendation.schemas import RecommendationRequest, UserIntent


def test_recommendation_endpoint_with_fake_llm(tmp_path, merlo):
    fake = FakeLLM(UserIntent())
    app = create_app(tmp_path / "catalog.sqlite3", llm=fake)

    async def verify():
        async with app.router.lifespan_context(app):
            saved = app.state.repository.upsert(merlo)
            endpoint = next(
                route.endpoint for route in app.routes if route.path == "/recommendations"
            )
            response = endpoint(
                RecommendationRequest(message="красное сухое к стейку"),
                Request({"type": "http", "app": app}),
            )
            assert response.status == "ok"
            assert response.recommendations[0].wine_id == saved.id
            assert response.recommendations[0].wine == saved
            assert len(fake.extraction_calls) == 1
            assert app.state.repository.count() == 1

    asyncio.run(verify())


def test_api_localizes_invalid_recommendation_requests(tmp_path):
    app = create_app(tmp_path / "catalog.sqlite3")

    async def verify():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for payload in (
                    {},
                    {"message": ""},
                    {"message": "   "},
                    {"message": "красное", "limit": 0},
                    {"message": "красное", "previous_intent": {"budget_max_rub": -1}},
                ):
                    response = await client.post("/recommendations", json=payload)
                    assert response.status_code == 422
                    assert response.json()["detail"].startswith("Некорректные параметры")

    asyncio.run(verify())


@pytest.mark.parametrize("message", ["", "   ", "а" * 2001])
def test_request_schema_rejects_empty_and_oversized_text(message):
    with pytest.raises(ValidationError):
        RecommendationRequest(message=message)


def test_incomplete_configuration_keeps_api_usable(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "test-secret-not-logged")
    app = create_app(tmp_path / "catalog.sqlite3")

    async def verify():
        async with app.router.lifespan_context(app):
            assert app.state.recommendations.llm is None

    asyncio.run(verify())
    assert "test-secret-not-logged" not in caplog.text
    assert "Настройки внешней модели некорректны" in caplog.text
