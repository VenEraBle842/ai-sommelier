"""Сквозные HTTP-сценарии продукта: настоящая SQLite, подмена LLM, запрет реальной сети."""

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from conftest import FIXTURES

from app.api.main import create_app
from app.catalog.parser import parse_wine
from app.llm.base import LLMUnavailable
from app.llm.fake import FakeLLM
from app.recommendation.schemas import UserIntent


@pytest.fixture(autouse=True)
def inline_thread_adapter(monkeypatch):
    """В тесте HTTP исполнять обработчики напрямую: песочница запрещает сигнальные сокеты пула."""

    async def inline(function, *args, **kwargs):
        return function(*args)

    monkeypatch.setattr("anyio.to_thread.run_sync", inline)


@asynccontextmanager
async def product(tmp_path, merlo, *, llm=None, empty=False):
    """Изолировать каталог и историю, сохраняя настоящий жизненный цикл приложения."""
    app = create_app(tmp_path / "catalog.sqlite3", llm=llm or FakeLLM(UserIntent()))
    async with app.router.lifespan_context(app):
        if not empty:
            app.state.repository.upsert(merlo)
            for filename, slug in (
                ("shenen.html", "shenen-blan-beton-drozd"),
                ("blend.html", "suhoe-beloe-traminer-pino-blan-sh-ardone"),
            ):
                app.state.repository.upsert(
                    parse_wine(
                        (FIXTURES / filename).read_text(), f"https://vino-svoe.ru/wines/{slug}"
                    )
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield app, client


async def chat(client, user_id, message="Белое сухое", **extra):
    response = await client.post(
        "/chat", json={"user_id": str(user_id), "message": message, **extra}
    )
    assert response.status_code == 200
    return response.json()


async def feedback(client, user_id, wine_id, kind="like"):
    return await client.post(
        "/feedback", json={"user_id": str(user_id), "wine_id": wine_id, "event_type": kind}
    )


def test_pages_assets_health_and_real_details(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (app, client):
            page = await client.get("/")
            assert page.status_code == 200
            assert '<html lang="ru">' in page.text
            for text in ("Ваши оценки", "Что подберём сегодня?", "18+"):
                assert text in page.text
            assert (await client.get("/static/app.js")).status_code == 200
            assert (await client.get("/static/style.css")).status_code == 200
            health = (await client.get("/health")).json()
            assert health["catalog_count"] == 3
            assert health["status"] == "ok"
            card = (await client.get("/wines/1")).json()
            assert card["name"] == merlo.name
            assert card["price_rub"] is None
            assert card["source_url"] == merlo.source_url
            assert app.state.repository.count() == 3

    asyncio.run(verify())


def test_chat_clarification_then_recommendation_and_shown_history(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (app, client):
            user = uuid4()
            first = await chat(client, user, "Красное")
            assert first["status"] == "clarification"
            assert first["message"].count("?") == 1
            assert not first["recommendations"]
            assert app.state.users.history(user) == []
            second = await chat(client, user, "Сухое к стейку", previous_intent=first["intent"])
            assert second["status"] == "ok"
            assert len(second["recommendations"]) == 1
            for item in second["recommendations"]:
                saved = app.state.repository.get(item["wine_id"])
                assert saved is not None
                assert item["wine"] == saved.model_dump(mode="json")
                assert saved.color == "красное"
                assert saved.sweetness == "сухое"
            history = (await client.get(f"/users/{user}/history")).json()
            assert [event["event_type"] for event in history] == ["shown"]
            assert history[0]["wine_id"] == second["recommendations"][0]["wine_id"]

    asyncio.run(verify())


def test_like_moves_real_wine_up_and_exposes_history_score(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (app, client):
            user = uuid4()
            before = await chat(client, user)
            assert [item["wine_id"] for item in before["recommendations"]] == [3, 2]
            saved = await feedback(client, user, 2)
            assert saved.status_code == 200
            assert "Оценка сохранена" in saved.json()["message"]
            after = await chat(client, user)
            assert [item["wine_id"] for item in after["recommendations"]] == [2, 3]
            liked = after["recommendations"][0]
            assert Decimal(liked["score"]) > Decimal(before["recommendations"][1]["score"])
            assert any(
                part["key"] == "personalization" and Decimal(part["score"]) > 0
                for part in liked["breakdown"]
            )
            assert "вам понравилось это вино" in liked["explanation"]
            assert liked["wine"] == app.state.repository.get(2).model_dump(mode="json")
            assert Decimal(liked["score"]) == sum(
                Decimal(part["score"]) for part in liked["breakdown"]
            )
            other = await chat(client, uuid4())
            assert [item["wine_id"] for item in other["recommendations"]] == [3, 2]

    asyncio.run(verify())


def test_dislike_lowers_score_and_latest_vote_wins_without_stacking(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (_, client):
            user = uuid4()
            await feedback(client, user, 3, "dislike")
            disliked = await chat(client, user)
            assert [item["wine_id"] for item in disliked["recommendations"]] == [2, 3]
            assert "не подошло" in disliked["recommendations"][1]["explanation"]
            await feedback(client, user, 3, "like")
            liked = await chat(client, user)
            await feedback(client, user, 3, "like")
            repeated = await chat(client, user)
            assert repeated["recommendations"] == liked["recommendations"]
            assert [item["wine_id"] for item in repeated["recommendations"]] == [3, 2]

    asyncio.run(verify())


def test_history_never_overrides_hard_constraints_or_creates_relevance(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (_, client):
            user = uuid4()
            await feedback(client, user, 1)
            white = await chat(client, user)
            assert white["status"] == "ok"
            assert all(
                item["wine"]["color"] == "белое" and item["wine_id"] != 1
                for item in white["recommendations"]
            )
            grape = await chat(client, user, "Шенен Блан")
            assert [item["wine_id"] for item in grape["recommendations"]] == [2]
            unsupported = await chat(client, user, "Насыщенное вино к утке")
            assert unsupported["status"] == "no_candidates"
            assert unsupported["recommendations"] == []

    asyncio.run(verify())


def test_missing_budget_and_known_budget_stay_honest_with_history(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (app, client):
            user = uuid4()
            await feedback(client, user, 2)
            missing = await chat(client, user, "Белое сухое до 2000 рублей")
            assert missing["budget_status"] == "budget_constraint_unavailable"
            assert any("подтверждённых цен" in text for text in missing["warnings"])
            assert all(
                item["budget_satisfied"] is None and item["wine"]["price_rub"] is None
                for item in missing["recommendations"]
            )
            wine = app.state.repository.get(3)
            app.state.repository.upsert(wine.model_copy(update={"price_rub": Decimal(1500)}))
            known = await chat(client, user, "Белое сухое до 2000 рублей")
            assert known["budget_status"] == "applied"
            assert [item["wine_id"] for item in known["recommendations"]] == [3]
            assert known["recommendations"][0]["budget_satisfied"] is True

    asyncio.run(verify())


def test_unavailable_llm_uses_fallback_without_losing_history(tmp_path, merlo):
    async def verify():
        fake = FakeLLM(LLMUnavailable("Модель недоступна."))
        async with product(tmp_path, merlo, llm=fake) as (_, client):
            user = uuid4()
            await feedback(client, user, 2)
            result = await chat(client, user)
            assert result["status"] == "ok"
            assert result["extraction_source"] == "fallback"
            assert result["recommendations"][0]["wine_id"] == 2
            assert any("локальный разбор" in warning for warning in result["warnings"])

    asyncio.run(verify())


def test_unknown_llm_wine_and_facts_never_enter_product(tmp_path, merlo):
    async def verify():
        fake = FakeLLM(
            UserIntent(),
            json.dumps(
                {
                    "explanations": [
                        {
                            "wine_id": 999999,
                            "reason_ids": ["taste:0"],
                            "name": "Выдуманное вино",
                            "price_rub": 1,
                        },
                        {
                            "wine_id": 3,
                            "reason_ids": ["constraints"],
                            "name": "Подмена названия",
                            "price_rub": 1,
                        },
                    ]
                }
            ),
        )
        async with product(tmp_path, merlo, llm=fake) as (app, client):
            result = await chat(client, uuid4())
            assert result["status"] == "ok"
            assert "Выдуманное" not in json.dumps(result, ensure_ascii=False)
            assert "Подмена названия" not in json.dumps(result, ensure_ascii=False)
            for item in result["recommendations"]:
                assert item["wine"] == app.state.repository.get(item["wine_id"]).model_dump(
                    mode="json"
                )
            missing = await feedback(client, uuid4(), 999999)
            assert missing.status_code == 404
            assert missing.json()["detail"] == "Запись или страница не найдена."

    asyncio.run(verify())


def test_empty_catalog_and_no_matches(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo, empty=True) as (_, client):
            user = uuid4()
            result = await chat(client, user)
            assert result["status"] == "empty_catalog"
            assert result["recommendations"] == []
            assert (await client.get(f"/users/{user}/history")).json() == []
        async with product(tmp_path, merlo) as (_, client):
            result = await chat(client, uuid4(), "Розовое сухое")
            assert result["status"] == "no_candidates"
            assert result["recommendations"] == []

    asyncio.run(verify())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_id": "не UUID", "message": "Красное"},
        {"user_id": str(uuid4()), "message": ""},
        {"user_id": str(uuid4()), "message": "Белое", "limit": 0},
    ],
)
def test_chat_validation_is_russian(tmp_path, merlo, payload):
    async def verify():
        async with product(tmp_path, merlo) as (_, client):
            response = await client.post("/chat", json=payload)
            assert response.status_code == 422
            assert "Некорректные параметры" in response.json()["detail"]

    asyncio.run(verify())


def test_invalid_feedback_and_history_cannot_create_records(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (app, client):
            user = uuid4()
            for payload in (
                {"user_id": str(user), "wine_id": 1, "event_type": "shown"},
                {"user_id": str(user), "wine_id": -1, "event_type": "like"},
                {"user_id": str(user), "wine_id": 1, "event_type": "like", "name": "Подмена"},
            ):
                assert (await client.post("/feedback", json=payload)).status_code == 422
            assert (await client.get("/users/не-UUID/history")).status_code == 422
            assert (await client.get(f"/users/{user}/history?limit=201")).status_code == 422
            assert app.state.users.history(user) == []

    asyncio.run(verify())


def test_history_survives_application_restart(tmp_path, merlo):
    async def verify():
        user = uuid4()
        async with product(tmp_path, merlo) as (_, client):
            await feedback(client, user, 2)
        async with product(tmp_path, merlo) as (_, client):
            history = (await client.get(f"/users/{user}/history")).json()
            assert len(history) == 1
            assert history[0]["user_id"] == str(user)
            assert history[0]["event_type"] == "like"
            assert history[0]["timestamp"].endswith("Z")
            assert (await chat(client, user))["recommendations"][0]["wine_id"] == 2

    asyncio.run(verify())


def test_opened_and_shown_do_not_change_affinity(tmp_path, merlo):
    async def verify():
        async with product(tmp_path, merlo) as (_, client):
            user = uuid4()
            before = await chat(client, user)
            await feedback(client, user, 2, "opened")
            after = await chat(client, user)
            assert before["recommendations"] == after["recommendations"]

    asyncio.run(verify())
