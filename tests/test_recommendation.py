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
from app.recommendation.vocabulary import CatalogVocabulary, negative_taste_evidence, taste_evidence


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
    assert [item.wine_id for item in result.recommendations] == [berry.id]


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
    result = RecommendationService(populated, FakeLLM(baseline.intent, explanations)).recommend(
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
        UserIntent(
            hard_constraints=Filters(color="красное"),
            budget_max_rub=Decimal("2000"),
            allow_defaults=True,
        ),
        limit=5,
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
    for message in ("красное сухое", "белое сухое к рыбе", "Мерло", "Кубань сухое"):
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
    fake = FakeLLM(baseline.intent, '{"explanations":[{"wine_id":1,"reason_ids":["rating"]}]}')
    result = RecommendationService(populated, fake).recommend(request())
    assert result.recommendations[0].explanation == baseline.recommendations[0].explanation


@pytest.mark.parametrize(
    "values",
    [
        {"budget_max_rub": -1},
        {"hard_constraints": {"color": "фиолетовое"}},
        {"taste_preferences": ["выдуманное"]},
        {"negative_taste_preferences": ["нет танинов"]},
        {"extra": "не поддерживается"},
    ],
)
def test_intent_schema_rejects_unsupported_values(values):
    with pytest.raises(ValidationError):
        UserIntent.model_validate(values)


@pytest.mark.parametrize("relevant_count", [1, 2])
def test_limit_is_maximum_not_rating_only_padding(make_wine, repository, relevant_count):
    relevant = [
        make_wine(description="В аромате малина.", food_pairings=["Блюда из птицы"], rating=None)
        for _ in range(relevant_count)
    ]
    unrelated = [
        make_wine(
            description="Нейтральное учебное описание.", food_pairings=[], rating=Decimal("5")
        )
        for _ in range(3)
    ]
    intent = UserIntent(
        dish="утка с ягодным соусом",
        normalized_food_categories=["Блюда из птицы"],
        taste_preferences=["ягодное"],
    )
    fake = FakeLLM(intent)
    result = RecommendationService(repository, fake).recommend_intent(intent, limit=3)
    assert result.status == "ok"
    assert [item.wine_id for item in result.recommendations] == [wine.id for wine in relevant]
    assert len(result.recommendations) < 3
    assert all(
        item.wine_id not in {wine.id for wine in unrelated} for item in result.recommendations
    )
    assert [item.wine_id for item in fake.explanation_calls[0]] == [wine.id for wine in relevant]
    assert all(repository.get(item.wine_id) == item.wine for item in result.recommendations)


def test_no_meaningful_match_returns_no_candidates_even_with_high_ratings(make_wine, repository):
    make_wine(
        description="Нейтральное учебное описание.",
        food_pairings=["Мясо и стейки"],
        rating=Decimal("5"),
    )
    fake = FakeLLM(
        UserIntent(
            dish="индейка",
            normalized_food_categories=["Блюда из птицы"],
            taste_preferences=["ягодное"],
        )
    )
    result = RecommendationService(repository, fake).recommend(
        request("Ищу вино к индейке с ягодами")
    )
    assert result.status == "no_candidates"
    assert result.recommendations == []
    assert fake.explanation_calls == []


@pytest.mark.parametrize("rating", [None, Decimal("0"), Decimal("5")])
def test_default_request_does_not_bypass_relevance_gate(make_wine, repository, rating):
    make_wine(rating=rating)
    result = RecommendationService(repository).recommend(request("Выберите сами"))
    assert result.status == "no_candidates"
    assert result.recommendations == []


@pytest.mark.parametrize(
    "soft",
    [
        Filters(color="красное"),
        Filters(sweetness="сухое"),
        Filters(grapes=["Мерло"]),
        Filters(regions=["Кубань"]),
        Filters(producer="ДРОЗД. Вино&Виноградник"),
        Filters(source_categories=["Красное сухое"]),
    ],
)
def test_explicit_soft_match_qualifies_without_food_or_rating(make_wine, repository, soft):
    wine = make_wine(rating=None, description=None, food_pairings=[])
    result = RecommendationService(repository).recommend_intent(
        UserIntent(soft_preferences=soft, allow_defaults=True)
    )
    assert result.status == "ok"
    assert [item.wine_id for item in result.recommendations] == [wine.id]


def test_relevance_is_rechecked_on_fresh_repository_record(make_wine, repository, monkeypatch):
    wine = make_wine(description="В аромате малина.", food_pairings=[], rating=Decimal("5"))
    original_get = repository.get

    def refreshed(wine_id):
        item = original_get(wine_id)
        item.description = "Обновлённое описание без вкусовых упоминаний."
        return item

    monkeypatch.setattr(repository, "get", refreshed)
    result = RecommendationService(repository).recommend_intent(
        UserIntent(taste_preferences=["ягодное"], allow_defaults=True)
    )
    assert wine.id == 1
    assert result.status == "no_candidates"
    assert result.recommendations == []


def test_duck_smoke_regression_does_not_force_meat_or_pad_results(make_wine, repository):
    relevant = make_wine(
        description="Насыщенный вкус. Аромат вишни и малины.", food_pairings=["Мясо и стейки"]
    )
    make_wine(description="Нейтральное описание.", food_pairings=["Мясо и стейки"])
    make_wine(description="Нейтральное описание.", food_pairings=[])
    fake = FakeLLM(
        UserIntent(
            dish=None, normalized_food_categories=["Мясо и стейки"], taste_preferences=["ягодное"]
        )
    )
    message = "Ищу вино к утке с ягодным соусом. Хочется чего-нибудь достаточно насыщенного, но без слишком грубой терпкости."
    result = RecommendationService(repository, fake).recommend(request(message, limit=3))
    assert result.status == "ok"
    assert result.intent.dish == "утке с ягодным соусом"
    assert result.intent.normalized_food_categories == []
    assert result.intent.taste_preferences == ["насыщенное"]
    assert result.intent.negative_taste_preferences == ["грубая терпкость"]
    assert [item.wine_id for item in result.recommendations] == [relevant.id]
    assert not any(part.key.startswith("food:") for part in result.recommendations[0].breakdown)
    assert any(part.key.startswith("taste:") for part in result.recommendations[0].breakdown)


@pytest.mark.parametrize(
    "message, expected, proposed, accepted",
    [
        (
            "Посоветуйте вино к запечённой индейке с розмарином и овощами.",
            "запечённой индейке с розмарином и овощами",
            ["Мясо и стейки", "Блюда из птицы"],
            ["Блюда из птицы"],
        ),
        (
            "Ищу белое вино под жареного лосося с лимонным соусом!",
            "жареного лосося с лимонным соусом",
            ["Мясо и стейки", "Рыба и морепродукты"],
            ["Рыба и морепродукты"],
        ),
        (
            "Нужно вино для курицы с грибами, бюджет до 2000 рублей.",
            "курицы с грибами",
            ["Десерты", "Блюда из птицы"],
            ["Блюда из птицы"],
        ),
        (
            "Подберите вино к стейку из лосося с травами.",
            "стейку из лосося с травами",
            ["Мясо и стейки", "Рыба и морепродукты"],
            ["Рыба и морепродукты"],
        ),
    ],
)
def test_raw_dish_recovered_and_categories_checked_for_varied_requests(
    make_wine, repository, message, expected, proposed, accepted
):
    make_wine(food_pairings=["Мясо и стейки", "Блюда из птицы", "Рыба и морепродукты", "Десерты"])
    fake = FakeLLM(UserIntent(dish=None, normalized_food_categories=proposed))
    result = RecommendationService(repository, fake).recommend(request(message))
    assert result.intent.dish == expected
    assert result.intent.normalized_food_categories == accepted


def test_complete_llm_dish_not_overwritten_by_fallback_token(populated):
    dish = "лосось с лимонным соусом и овощами"
    fake = FakeLLM(UserIntent(dish=dish, normalized_food_categories=["Рыба и морепродукты"]))
    result = RecommendationService(populated, fake).recommend(
        request("На ужин — лосось с лимонным соусом и овощами. Предпочитаю сухое.")
    )
    assert result.intent.dish == dish
    assert result.intent.normalized_food_categories == ["Рыба и морепродукты"]


def test_empty_llm_food_normalization_is_not_refilled_by_fallback(populated):
    fake = FakeLLM(UserIntent(dish="стейк рибай с овощами", normalized_food_categories=[]))
    result = RecommendationService(populated, fake).recommend(
        request("Красное сухое к стейку рибай с овощами.")
    )
    assert result.intent.dish == "стейк рибай с овощами"
    assert result.intent.normalized_food_categories == []
    assert all(
        not part.key.startswith("food:")
        for item in result.recommendations
        for part in item.breakdown
    )


def test_absent_poultry_category_not_fabricated_even_when_model_returns_it(make_wine, repository):
    make_wine(food_pairings=["Мясо и стейки"], description="Нейтральное учебное описание.")
    fake = FakeLLM(
        UserIntent(
            dish="гусь с яблоками", normalized_food_categories=["Блюда из птицы", "Мясо и стейки"]
        )
    )
    result = RecommendationService(repository, fake).recommend(
        request("Посоветуйте вино к гусю с яблоками.")
    )
    assert result.intent.dish == "гусь с яблоками"
    assert result.intent.normalized_food_categories == []
    assert result.status == "no_candidates"


def test_full_llm_dish_not_replaced_by_purpose_phrase(populated):
    dish = "запечённая утка с ягодным соусом"
    fake = FakeLLM(UserIntent(dish=dish, taste_preferences=["ягодное"]))
    result = RecommendationService(populated, fake).recommend(
        request("Для праздничного ужина готовлю запечённую утку с ягодным соусом.")
    )
    assert result.intent.dish == dish


@pytest.mark.parametrize(
    "dish, category", [("утка по-пекински", "Азиатская кухня"), ("лосось на гриле", "BBQ")]
)
def test_cuisine_or_cooking_category_not_rejected_as_wrong_protein(
    make_wine, repository, dish, category
):
    make_wine(food_pairings=[category, "Мясо и стейки"])
    intent = UserIntent(dish=dish, normalized_food_categories=[category, "Мясо и стейки"])
    result = RecommendationService(repository).recommend_intent(intent)
    assert result.intent.normalized_food_categories == [category]


@pytest.mark.parametrize(
    "message",
    [
        "Ищу вино к утке с ягодным соусом. Хочется насыщенного вина без слишком грубой терпкости.",
        "К индейке с яблоками предпочитаю полнотелое вино, без жёстких танинов.",
        "К утке с ягодным соусом — насыщенное вино, без грубой терпкости.",
    ],
)
def test_positive_and_negative_style_survive_missing_food_normalization(
    make_wine, repository, message
):
    wine = make_wine(description="Полнотелое вино с насыщенным вкусом.", food_pairings=[])
    make_wine(description="Нейтральное описание.", food_pairings=[], rating=Decimal(5))
    fake = FakeLLM(UserIntent())
    result = RecommendationService(repository, fake).recommend(request(message, limit=3))
    assert result.status == "ok"
    assert result.intent.dish
    assert result.intent.normalized_food_categories == []
    assert result.intent.taste_preferences == ["насыщенное"]
    assert result.intent.negative_taste_preferences == ["грубая терпкость"]
    assert [item.wine_id for item in result.recommendations] == [wine.id]
    assert len(fake.explanation_calls[0]) == 1
    assert result.recommendations[0].wine == repository.get(wine.id)


@pytest.mark.parametrize(
    "message, taste",
    [
        ("Вино к утке с ягодным соусом", "ягодное"),
        ("Хочу ягодное желе", "ягодное"),
        ("Ягодный соус", "ягодное"),
        ("Насыщенный соус", "насыщенное"),
        ("Вино к насыщенному рагу", "насыщенное"),
    ],
)
def test_dish_adjective_is_not_a_wine_preference(populated, message, taste):
    fake = FakeLLM(UserIntent(taste_preferences=[taste]))
    result = RecommendationService(populated, fake).recommend(request(message))
    assert result.intent.taste_preferences == []
    assert result.status == "no_candidates"
    assert result.recommendations == []
    parsed = parse_fallback(message, CatalogVocabulary.from_wines(populated.list_wines()))
    assert parsed.intent.taste_preferences == []


def test_separately_requested_berry_wine_is_not_erased_with_berry_sauce(populated):
    result = RecommendationService(
        populated, FakeLLM(UserIntent(taste_preferences=["ягодное"]))
    ).recommend(request("Вино к утке с ягодным соусом. Хочу ягодное вино."))
    assert result.intent.taste_preferences == ["ягодное"]
    assert result.status == "ok"
    assert any(part.key.startswith("taste:") for part in result.recommendations[0].breakdown)


@pytest.mark.parametrize(
    "message, positive, negative",
    [
        ("Хочется насыщенного вина", ["насыщенное"], []),
        ("Хочу полнотелое вино", ["насыщенное"], []),
        ("Красное плотное вино без грубой терпкости", ["насыщенное"], ["грубая терпкость"]),
        (
            "Хочу вино без грубой терпкости и достаточно насыщенное",
            ["насыщенное"],
            ["грубая терпкость"],
        ),
        ("Красное без слишком грубых танинов", [], ["грубая терпкость"]),
        ("Красное без жёсткой терпкости", [], ["грубая терпкость"]),
    ],
)
def test_small_fallback_vocabulary_distinguishes_positive_and_avoidance(
    populated, message, positive, negative
):
    result = parse_fallback(message, CatalogVocabulary.from_wines(populated.list_wines()))
    assert result.supported
    assert result.intent.taste_preferences == positive
    assert result.intent.negative_taste_preferences == negative


@pytest.mark.parametrize("message", ["Без терпкости", "Красное не сухое", "Хочу невесомое вино"])
def test_unrepresented_style_expressions_are_not_guessed(populated, message):
    result = RecommendationService(populated).recommend(request(message))
    assert result.status == "uninterpretable"
    assert result.recommendations == []


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Насыщенный вкус с долгим послевкусием.", True),
        ("Вкус насыщенный и длительный.", True),
        ("Полнотелое вино.", True),
        ("Интенсивный вкус с нотами специй.", True),
        ("Плотное вино с полным телом.", True),
        ("Тело плотное.", True),
        ("Вино плотное.", True),
        ("Интенсивный аромат с нотами специй.", False),
        ("Насыщенный аромат. Тело и кислотность средние.", False),
        ("Тело среднее.", False),
        ("Плотные танины и интенсивная кислотность.", False),
        ("Сладкое, фруктовое, свежее и мягкое вино.", False),
        ("Насыщенный вкусовой аромат.", False),
        ("Насыщенный рубиновый цвет.", False),
        ("Вино насыщенного рубинового цвета.", False),
        ("Плотная этикетка.", False),
        ("Вкус не насыщенный.", False),
        ("Без интенсивного аромата.", False),
    ],
)
def test_rich_style_requires_source_backed_taste_or_body_evidence(description, expected):
    evidence = taste_evidence(description, "насыщенное")
    assert bool(evidence) is expected
    if evidence:
        assert evidence in description


@pytest.mark.parametrize(
    "description",
    [
        "Интенсивный аромат.",
        "Аромат насыщенный. Тело и кислотность средние.",
        "Тело среднее. Плотные танины.",
        "Мягкое, свежее и фруктовое вино.",
    ],
)
def test_other_sensory_properties_do_not_qualify_rich_wine_request(
    make_wine, repository, description
):
    wine = make_wine(description=description, food_pairings=[], rating=Decimal(5))
    intent = UserIntent(taste_preferences=["насыщенное"], allow_defaults=True)
    ranked = rank_wines([wine], intent)
    assert all(not part.key.startswith("taste:") for part in ranked[0].breakdown)
    result = RecommendationService(repository).recommend_intent(intent)
    assert result.status == "no_candidates"
    assert result.recommendations == []


def test_real_intense_aroma_and_medium_body_are_not_richness_evidence(repository, merlo):
    wine = repository.upsert(merlo)
    assert "Аромат интенсивный" in wine.description
    assert "Тело и кислотность средние" in wine.description
    intent = UserIntent(
        dish="утка с ягодным соусом",
        taste_preferences=["насыщенное"],
        negative_taste_preferences=["грубая терпкость"],
    )
    result = RecommendationService(repository).recommend_intent(intent)
    assert result.status == "no_candidates"
    assert result.intent.dish == intent.dish
    assert result.intent.taste_preferences == ["насыщенное"]
    assert result.intent.negative_taste_preferences == ["грубая терпкость"]
    assert result.recommendations == []


@pytest.mark.parametrize("description", ["Полнотелое вино.", "Плотное тело.", "Вкус насыщенный."])
def test_grounded_richness_qualifies_without_aroma_only_padding(make_wine, repository, description):
    relevant = make_wine(description=description, food_pairings=[], rating=None)
    make_wine(description="Аромат интенсивный. Тело среднее.", food_pairings=[], rating=Decimal(5))
    result = RecommendationService(repository).recommend_intent(
        UserIntent(taste_preferences=["насыщенное"], allow_defaults=True), limit=3
    )
    assert result.status == "ok"
    assert [item.wine_id for item in result.recommendations] == [relevant.id]
    item = result.recommendations[0]
    assert item.wine == repository.get(item.wine_id)
    assert description in item.explanation
    assert any(part.key == "taste:0" and part.score > 0 for part in item.breakdown)


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Во вкусе грубая терпкость.", True),
        ("Жёсткие танины в послевкусии.", True),
        ("Танины грубые.", True),
        ("Танины мягкие.", False),
        ("Без грубых танинов.", False),
        ("Танины не грубые.", False),
        ("Грубая терпкость отсутствует.", False),
        ("Грубые танины не выражены.", False),
        (None, False),
    ],
)
def test_only_explicit_undesirable_source_characteristics_have_evidence(description, expected):
    evidence = negative_taste_evidence(description, "грубая терпкость")
    assert bool(evidence) is expected
    if evidence:
        assert evidence in description


def test_avoidance_penalizes_explicit_evidence_but_never_rewards_silence(make_wine, repository):
    rough = make_wine(description="Насыщенный вкус. Грубая терпкость.", food_pairings=[])
    unknown = make_wine(description="Насыщенный вкус.", food_pairings=[])
    baseline = RecommendationService(repository).recommend_intent(
        UserIntent(taste_preferences=["насыщенное"], allow_defaults=True)
    )
    intent = UserIntent(
        taste_preferences=["насыщенное"],
        negative_taste_preferences=["грубая терпкость"],
        allow_defaults=True,
    )
    result = RecommendationService(repository).recommend_intent(intent)
    assert [item.wine_id for item in result.recommendations] == [unknown.id, rough.id]
    old_scores = {item.wine_id: item.score for item in baseline.recommendations}
    assert result.recommendations[0].score == old_scores[unknown.id]
    assert result.recommendations[1].score < old_scores[rough.id]
    assert "Грубая терпкость" in result.recommendations[1].explanation
    assert "не даёт достаточных сведений" in result.recommendations[0].explanation
    for item in result.recommendations:
        assert item.score == sum((part.score for part in item.breakdown), Decimal(0))
        assert item.wine == repository.get(item.wine_id)
        assert "точно не терпкое" not in item.explanation


@pytest.mark.parametrize("description", [None, "Нейтральное описание.", "Грубая терпкость."])
def test_avoidance_and_rating_alone_never_qualify_a_candidate(make_wine, repository, description):
    make_wine(description=description, food_pairings=[], rating=Decimal(5))
    result = RecommendationService(repository).recommend_intent(
        UserIntent(negative_taste_preferences=["грубая терпкость"], allow_defaults=True)
    )
    assert result.status == "no_candidates"
    assert result.recommendations == []


@pytest.mark.parametrize("description", ["Насыщенный вкус.", "Насыщенный вкус. Грубая терпкость."])
def test_llm_cannot_hide_avoidance_penalty_or_uncertainty(make_wine, repository, description):
    wine = make_wine(description=description, food_pairings=[])
    fake = FakeLLM(
        UserIntent(),
        json.dumps({"explanations": [{"wine_id": wine.id, "reason_ids": ["taste:0"]}]}),
    )
    intent = UserIntent(
        taste_preferences=["насыщенное"],
        negative_taste_preferences=["грубая терпкость"],
        allow_defaults=True,
    )
    result = RecommendationService(repository, fake).recommend_intent(intent)
    negative_reason = next(
        part.reason
        for part in result.recommendations[0].breakdown
        if part.key.startswith("negative_taste:")
    )
    assert negative_reason in result.recommendations[0].reasons
    assert negative_reason in result.recommendations[0].explanation


def test_no_rich_source_evidence_remains_no_match_even_when_intent_is_preserved(
    make_wine, repository
):
    make_wine(description="Насыщенный рубиновый цвет.", food_pairings=[], rating=Decimal(5))
    message = "Ищу вино к утке с ягодным соусом. Хочется насыщенного вина без грубой терпкости."
    result = RecommendationService(repository, FakeLLM(UserIntent())).recommend(request(message))
    assert result.intent.taste_preferences == ["насыщенное"]
    assert result.intent.negative_taste_preferences == ["грубая терпкость"]
    assert result.intent.normalized_food_categories == []
    assert result.status == "no_candidates"


def test_negative_preferences_persist_in_dialogue_context(populated):
    service = RecommendationService(populated)
    first = service.recommend(request("Красное насыщенное без грубой терпкости"))
    second = service.recommend(request("к стейку", previous_intent=first.intent))
    assert second.intent.negative_taste_preferences == ["грубая терпкость"]
    assert second.intent.taste_preferences == ["насыщенное"]
    assert second.intent.hard_constraints.color == "красное"
    assert all(item.wine == populated.get(item.wine_id) for item in second.recommendations)


def test_avoidance_describing_sauce_not_transferred_to_wine(make_wine, repository):
    make_wine(description="Насыщенный вкус.", food_pairings=[])
    intent = UserIntent(negative_taste_preferences=["грубая терпкость"])
    message = "Хочу вино к соусу без слишком грубой терпкости. Предпочитаю насыщенное вино."
    result = RecommendationService(repository, FakeLLM(intent)).recommend(request(message))
    assert result.intent.negative_taste_preferences == []
    assert result.intent.taste_preferences == ["насыщенное"]


def test_negated_style_is_not_preserved_as_positive_from_model(populated):
    fake = FakeLLM(UserIntent(taste_preferences=["насыщенное"]))
    result = RecommendationService(populated, fake).recommend(request("Не хочу насыщенного вина"))
    assert result.intent.taste_preferences == []
    assert result.recommendations == []
