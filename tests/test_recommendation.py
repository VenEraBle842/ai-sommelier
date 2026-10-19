"""Правильность подбора, бюджет и невозможность создания вин через LLM."""

import json
from decimal import Decimal

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from app.catalog.models import WineData
from app.catalog.parser import parse_wine
from app.llm.base import LLMResponseError, LLMUnavailable
from app.llm.fake import FakeLLM
from app.recommendation.fallback import parse_fallback
from app.recommendation.ranking import matches_constraints, rank_wines
from app.recommendation.schemas import Filters, RecommendationRequest, UserIntent
from app.recommendation.service import RecommendationService
from app.recommendation.vocabulary import CatalogVocabulary, taste_evidence


@pytest.fixture
def populated(repository, merlo):
    """Три реальные карточки в изолированной тестовой базе."""
    repository.upsert(merlo)
    for filename, slug in (
        ("shenen.html", "shenen-blan-beton-drozd"),
        ("blend.html", "suhoe-beloe-traminer-pino-blan-sh-ardone"),
    ):
        repository.upsert(
            parse_wine((FIXTURES / filename).read_text(), f"https://vino-svoe.ru/wines/{slug}")
        )
    return repository


@pytest.fixture
def make_wine(repository, merlo):
    """Искусственные граничные случаи создаются только во временной SQLite теста."""
    counter = [0]

    def create(**changes):
        counter[0] += 1
        slug = f"uchebnyj-test-{counter[0]}"
        values = merlo.model_dump()
        values.update(
            slug=slug,
            source_url=f"https://vino-svoe.ru/wines/{slug}",
            name=f"Учебная позиция {counter[0]}",
        )
        values.update(changes)
        return repository.upsert(WineData.model_validate(values))

    return create


def request(message="Красное сухое к стейку", **values):
    return RecommendationRequest(message=message, **values)


@pytest.mark.parametrize(
    "hard, expected",
    [
        ({"color": "красное"}, [1]),
        ({"color": "белое"}, [2, 3]),
        ({"sweetness": "сухое"}, [1, 2, 3]),
        ({"sweetness": "полусладкое"}, []),
        ({"grapes": ["Мерло"]}, [1]),
        ({"grapes": ["Пино Блан", "Шардоне"]}, [3]),
        ({"grapes": ["Пино Блан", "Мерло"]}, []),
        ({"regions": ["Кубань"]}, [1, 2, 3]),
        ({"regions": ["Неизвестный регион"]}, []),
        ({"producer": "Усадьба Маскага"}, [3]),
        ({"source_categories": ["Красное сухое"]}, [1]),
        (
            {"color": "белое", "sweetness": "сухое", "regions": ["Кубань"], "grapes": ["Шардоне"]},
            [3],
        ),
    ],
)
def test_hard_constraints(populated, hard, expected):
    actual = [
        w.id for w in populated.list_wines() if matches_constraints(w, Filters.model_validate(hard))
    ]
    assert actual == expected


def test_sweetness_not_confused_with_semisweet(make_wine, repository):
    dry = make_wine(sweetness="сухое")
    semi = make_wine(sweetness="полусладкое")
    result = RecommendationService(repository).recommend_intent(
        UserIntent(hard_constraints=Filters(sweetness="полусладкое"), allow_defaults=True)
    )
    assert [r.wine_id for r in result.recommendations] == [semi.id]
    assert dry.id != semi.id


def test_unknown_values_never_satisfy_hard_constraints(make_wine):
    wine = make_wine(
        color=None, sweetness=None, region=None, grapes=[], producer=None, source_category=None
    )
    for hard in (
        Filters(color="красное"),
        Filters(sweetness="сухое"),
        Filters(regions=["Кубань"]),
        Filters(grapes=["Мерло"]),
        Filters(producer="ДРОЗД. Вино&Виноградник"),
        Filters(source_categories=["Красное сухое"]),
    ):
        assert not matches_constraints(wine, hard)


def test_normalized_exact_hard_comparison(populated):
    assert matches_constraints(
        populated.get(1),
        Filters(grapes=[" мЕРЛО "], regions=["КУБАНЬ"], producer="ДРОЗД. Вино&Виноградник"),
    )
    assert not matches_constraints(populated.get(1), Filters(grapes=["Мер"]))


def test_soft_preferences_change_order_without_bypassing_hard_filters(populated):
    intent = UserIntent(
        hard_constraints=Filters(color="красное"),
        soft_preferences=Filters(color="белое", grapes=["Шардоне"], producer="Усадьба Маскага"),
        allow_defaults=True,
    )
    result = RecommendationService(populated).recommend_intent(intent)
    assert [r.wine_id for r in result.recommendations] == [1]
    intent.hard_constraints = Filters()
    ranked = rank_wines(populated.list_wines(), intent)
    assert ranked[0].wine_id == 3


def test_food_match_beats_weak_rating(populated):
    intent = UserIntent(
        hard_constraints=Filters(color="белое"),
        dish="рыба",
        normalized_food_categories=["Рыба и морепродукты"],
    )
    result = RecommendationService(populated).recommend_intent(intent)
    assert result.recommendations[0].wine_id == 3
    assert any(p.key.startswith("food:") for p in result.recommendations[0].breakdown)


def test_fallback_food_normalization_and_matching(populated):
    result = RecommendationService(populated).recommend(request("красное сухое к стейку рибай"))
    assert result.status == "ok"
    assert "Мясо и стейки" in result.intent.normalized_food_categories
    assert result.recommendations[0].wine_id == 1
    assert all(
        food in result.recommendations[0].wine.food_pairings
        for food in result.intent.normalized_food_categories
    )


def test_invented_food_category_not_treated_as_catalog_fact(populated):
    intent = UserIntent(
        hard_constraints=Filters(color="красное", sweetness="сухое"),
        dish="неизвестное блюдо",
        normalized_food_categories=["Выдуманная гастрономия"],
    )
    result = RecommendationService(populated, FakeLLM(intent)).recommend(
        request("Подберите необычное вино")
    )
    assert result.intent.normalized_food_categories == []
    assert result.warnings
    assert all(
        not p.key.startswith("food:") for item in result.recommendations for p in item.breakdown
    )


def test_taste_scoring_uses_literal_source_evidence(make_wine, repository):
    plain = make_wine(description="Нейтральное учебное описание.", rating=Decimal(5))
    berry = make_wine(description="Аромат вишни и малины.", rating=None)
    intent = UserIntent(taste_preferences=["ягодное"], allow_defaults=True)
    result = RecommendationService(repository).recommend_intent(intent)
    assert result.recommendations[0].wine_id == berry.id
    assert berry.id != plain.id
    assert "вишни и малины" in result.recommendations[0].explanation
    assert not any(p.key.startswith("taste:") for p in result.recommendations[1].breakdown)


def test_missing_or_negated_taste_is_unknown():
    assert taste_evidence(None, "дубовое") is None
    assert taste_evidence("Без дубовой выдержки.", "дубовое") is None
    assert taste_evidence("Нет ванильных тонов.", "ванильное") is None
    assert taste_evidence("Цветочное описание.", "несуществующий признак") is None


def test_single_material_question_and_followup(populated):
    service = RecommendationService(populated)
    first = service.recommend(request("Хочу красное вино."))
    assert first.status == "clarification"
    assert first.message.count("?") == 1
    assert first.intent.missing_information == ["sweetness"]
    second = service.recommend(request("сухое к стейку", previous_intent=first.intent))
    assert second.status == "ok"
    assert second.intent.hard_constraints.color == "красное"
    assert second.intent.hard_constraints.sweetness == "сухое"
    assert not second.intent.needs_clarification


def test_model_cannot_force_unnecessary_question(populated):
    intent = UserIntent(
        hard_constraints=Filters(color="красное", sweetness="сухое"),
        needs_clarification=True,
        missing_information=["food"],
    )
    result = RecommendationService(populated, FakeLLM(intent)).recommend(request())
    assert result.status == "ok"
    assert result.intent.missing_information == []


@pytest.mark.parametrize(
    "error",
    [
        LLMUnavailable("Учебный тайм-аут"),
        LLMResponseError("Учебная ошибка"),
        '{"hard_constraints":',
        '{"unknown_field": "учебное"}',
    ],
)
def test_model_failure_uses_deterministic_fallback(populated, error):
    fake = FakeLLM(error)
    result = RecommendationService(populated, fake).recommend(request())
    assert result.status == "ok"
    assert result.extraction_source == "fallback"
    assert result.recommendations[0].wine_id == 1
    assert any("ограниченный локальный разбор" in w for w in result.warnings)
    assert fake.explanation_calls == []


@pytest.mark.parametrize(
    "message, field, expected",
    [
        ("красное сухое", "color", "красное"),
        ("белое полусухое", "sweetness", "полусухое"),
        ("розовое сладкое", "sweetness", "сладкое"),
        ("Мерло", "grapes", ["Мерло"]),
        ("сухое из Кубани", "regions", ["Кубань"]),
        ("Кубань", "regions", ["Кубань"]),
    ],
)
def test_limited_fallback_vocabulary(populated, message, field, expected):
    parsed = parse_fallback(message, CatalogVocabulary.from_wines(populated.list_wines()))
    assert parsed.supported
    assert getattr(parsed.intent.hard_constraints, field) == expected


@pytest.mark.parametrize(
    "message",
    [
        "абракадабра",
        "красное не сухое",
        "красное из Атлантиды",
        "красное или белое",
        "красное с телепатией",
    ],
)
def test_unsupported_fallback_does_not_guess(populated, message):
    result = RecommendationService(populated).recommend(request(message))
    assert result.status == "uninterpretable"
    assert result.recommendations == []
    assert "Не удалось надёжно разобрать" in result.message


def test_no_candidates_does_not_relax_constraints(populated):
    intent = UserIntent(hard_constraints=Filters(color="розовое", sweetness="сухое"))
    result = RecommendationService(populated, FakeLLM(intent)).recommend(request("Розовое сухое"))
    assert result.status == "no_candidates"
    assert result.recommendations == []


def test_empty_repository_is_not_seeded(repository):
    result = RecommendationService(repository).recommend(request())
    assert result.status == "empty_catalog"
    assert result.recommendations == []
    assert repository.count() == 0


def test_unknown_wine_id_and_hallucinated_position_rejected(populated, caplog):
    fake = FakeLLM(
        UserIntent(hard_constraints=Filters(color="красное", sweetness="сухое")),
        '{"explanations":[{"wine_id":999999,"reason_ids":["rating"],"name":"Выдуманное вино","price_rub":1}]}',
    )
    result = RecommendationService(populated, fake).recommend(request())
    assert [item.wine_id for item in result.recommendations] == [1]
    assert "Выдуманное" not in result.model_dump_json()
    assert "999999" in caplog.text


def test_existing_but_filtered_out_id_rejected(populated, caplog):
    fake = FakeLLM(UserIntent(), '{"explanations":[{"wine_id":2,"reason_ids":["rating"]}]}')
    result = RecommendationService(populated, fake).recommend(request())
    assert [item.wine_id for item in result.recommendations] == [1]
    assert "Отклонён" in caplog.text


def test_llm_cannot_modify_any_factual_fields(populated):
    forged = {
        "explanations": [
            {
                "wine_id": 1,
                "reason_ids": ["constraints"],
                "name": "Выдуманное вино",
                "producer": "Выдуманный завод",
                "region": "Атлантида",
                "grapes": ["Несуществующий сорт"],
                "price_rub": 1,
                "rating": 999,
                "source_url": "https://evil.test",
                "image_url": "https://evil.test/1.png",
                "reasons": ["Награждено выдуманной премией."],
            }
        ]
    }
    result = RecommendationService(
        populated, FakeLLM(UserIntent(), json.dumps(forged, ensure_ascii=False))
    ).recommend(request())
    recommendation = result.recommendations[0]
    assert recommendation.wine == populated.get(recommendation.wine_id)
    assert recommendation.wine.name == "Мерло. Дрозд"
    assert "Выдуман" not in result.model_dump_json()
    assert "Атлантида" not in result.model_dump_json()
    assert recommendation.reasons == [
        "Карточка соответствует указанным вами обязательным ограничениям."
    ]


@pytest.mark.parametrize(
    "explanations",
    [
        '{"explanations":[{"wine_id":1,"reason_ids":["выдуманное свойство"]}]}',
        '{"explanations":[{"wine_id":1,"reasons":["Придуманная цена"]}]}',
        "не JSON",
        LLMUnavailable("Учебный сбой объяснения"),
    ],
)
def test_unsafe_explanation_falls_back_to_grounded_reasons(populated, explanations):
    baseline = RecommendationService(populated).recommend(request())
    result = RecommendationService(populated, FakeLLM(UserIntent(), explanations)).recommend(
        request()
    )
    assert result.recommendations[0].explanation == baseline.recommendations[0].explanation


def test_budget_missing_prices_is_explicit_not_zero(populated):
    result = RecommendationService(populated).recommend(request("красное сухое до 2000 рублей"))
    assert result.status == "ok"
    assert result.budget_status == "budget_constraint_unavailable"
    assert any("гарантировать нельзя" in warning for warning in result.warnings)
    assert all(
        item.wine.price_rub is None and item.budget_satisfied is None
        for item in result.recommendations
    )
    assert all("бюджет" not in item.explanation for item in result.recommendations)


def test_budget_known_prices_is_strict(make_wine, repository):
    cheap = make_wine(price_rub=Decimal("1999.99"))
    equal = make_wine(price_rub=Decimal("2000"))
    make_wine(price_rub=Decimal("2000.01"))
    make_wine(price_rub=None)
    result = RecommendationService(repository).recommend_intent(
        UserIntent(budget_max_rub=Decimal("2000"), allow_defaults=True), limit=5
    )
    assert result.budget_status == "applied"
    assert {r.wine_id for r in result.recommendations} == {cheap.id, equal.id}
    assert all(r.budget_satisfied is True for r in result.recommendations)


def test_unknown_price_cannot_satisfy_budget_even_if_known_prices_are_other_color(
    make_wine, repository
):
    make_wine(color="белое", price_rub=Decimal("1000"))
    make_wine(color="красное", price_rub=None)
    result = RecommendationService(repository).recommend_intent(
        UserIntent(
            hard_constraints=Filters(color="красное", sweetness="сухое"),
            budget_max_rub=Decimal("2000"),
        )
    )
    assert result.budget_status == "applied"
    assert result.status == "no_candidates"
    assert result.recommendations == []


def test_deterministic_order_for_identical_catalog_and_inputs(populated):
    service = RecommendationService(populated)
    results = [service.recommend(request("белое сухое")) for _ in range(4)]
    assert all(result == results[0] for result in results)
    assert [r.wine_id for r in results[0].recommendations] == [3, 2]


def test_equal_scores_break_ties_by_id(make_wine, repository):
    first, second = make_wine(), make_wine()
    ranked = rank_wines(list(reversed(repository.list_wines())), UserIntent())
    assert [r.wine_id for r in ranked] == [first.id, second.id]


def test_every_recommendation_id_exists_and_facts_come_from_repository(populated):
    for message in ("красное сухое", "белое сухое к рыбе", "Мерло", "выберите сами"):
        result = RecommendationService(populated, FakeLLM(UserIntent())).recommend(request(message))
        assert result.status == "ok"
        for item in result.recommendations:
            assert populated.get(item.wine_id) is not None
            assert item.wine == populated.get(item.wine_id)


def test_final_facts_are_reread_from_repository(populated, monkeypatch):
    original_get = populated.get

    def refreshed(wine_id):
        wine = original_get(wine_id)
        if wine:
            wine.description = "Обновлённое учебное описание, полученное репозиторием."
        return wine

    monkeypatch.setattr(populated, "get", refreshed)
    result = RecommendationService(populated).recommend(request())
    assert result.recommendations[0].wine.description.startswith("Обновлённое учебное")


def test_llm_receives_only_shortlist_and_real_reasons(populated):
    fake = FakeLLM(UserIntent())
    result = RecommendationService(populated, fake).recommend(request("белое сухое", limit=1))
    assert len(fake.explanation_calls) == 1
    assert len(fake.explanation_calls[0]) == 1
    assert fake.explanation_calls[0][0].wine_id == result.recommendations[0].wine_id
    assert all("price_rub" not in candidate.model_dump() for candidate in fake.explanation_calls[0])


def test_score_breakdown_totals_and_explanations_are_grounded(populated):
    result = RecommendationService(populated).recommend(request("белое сухое к рыбе"))
    for item in result.recommendations:
        assert item.score == sum((part.score for part in item.breakdown), Decimal(0))
        assert all(reason in [part.reason for part in item.breakdown] for reason in item.reasons)
        assert item.explanation == " ".join(item.reasons)


def test_grape_alternatives_are_not_misread_as_all_required(populated):
    result = RecommendationService(populated).recommend(request("Мерло или Шардоне"))
    assert result.status == "uninterpretable"
    assert result.recommendations == []


def test_literal_requirement_overrides_model_guess(populated):
    fake = FakeLLM(UserIntent(hard_constraints=Filters(color="белое", sweetness="сладкое")))
    result = RecommendationService(populated, fake).recommend(request())
    assert result.intent.hard_constraints.color == "красное"
    assert result.intent.hard_constraints.sweetness == "сухое"
    assert [r.wine_id for r in result.recommendations] == [1]


def test_literal_budget_and_color_still_override_model_in_complex_request(populated):
    fake = FakeLLM(
        UserIntent(hard_constraints=Filters(color="белое"), budget_max_rub=Decimal("10000"))
    )
    result = RecommendationService(populated, fake).recommend(
        request("Насыщенное красное сухое до 2000 рублей")
    )
    assert result.extraction_source == "llm"
    assert result.intent.hard_constraints.color == "красное"
    assert result.intent.hard_constraints.sweetness == "сухое"
    assert result.intent.budget_max_rub == Decimal("2000")
    assert [r.wine_id for r in result.recommendations] == [1]


def test_valid_llm_json_handles_text_outside_fallback_vocabulary(populated):
    fake = FakeLLM(
        '{"hard_constraints":{"color":"белое","sweetness":"сухое"},"taste_preferences":["цветочное"]}'
    )
    result = RecommendationService(populated, fake).recommend(
        request("Мне бы бокал с цветочным характером")
    )
    assert result.extraction_source == "llm"
    assert result.status == "ok"
    assert all(item.wine.color == "белое" for item in result.recommendations)


def test_explanation_cannot_ignore_request_in_favor_of_rating(populated):
    baseline = RecommendationService(populated).recommend(request())
    fake = FakeLLM(UserIntent(), '{"explanations":[{"wine_id":1,"reason_ids":["rating"]}]}')
    result = RecommendationService(populated, fake).recommend(request())
    assert result.recommendations[0].explanation == baseline.recommendations[0].explanation


@pytest.mark.parametrize(
    "values",
    [
        {"budget_max_rub": -1},
        {"hard_constraints": {"color": "фиолетовое"}},
        {"taste_preferences": ["выдуманное"]},
        {"extra": "не поддерживается"},
    ],
)
def test_intent_schema_rejects_unsupported_values(values):
    with pytest.raises(ValidationError):
        UserIntent.model_validate(values)
