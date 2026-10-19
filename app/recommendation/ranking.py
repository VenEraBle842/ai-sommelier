"""Жёсткая фильтрация и объяснимые баллы без случайности и LLM."""

from dataclasses import dataclass
from decimal import Decimal

from app.catalog.models import Wine
from app.recommendation.schemas import Filters, ScoreComponent, UserIntent
from app.recommendation.vocabulary import negative_taste_evidence, normalize, taste_evidence
from app.users.preferences import PreferenceProfile

WEIGHTS = {
    "food": Decimal("10"),
    "taste": Decimal("3"),
    "negative_taste": Decimal("-4"),
    "grape": Decimal("3"),
    "region": Decimal("2"),
    "producer": Decimal("2"),
    "color": Decimal("2"),
    "sweetness": Decimal("2"),
    "category": Decimal("2"),
    "rating": Decimal("0.2"),
}


def matches_constraints(wine: Wine, hard: Filters) -> bool:
    """Неизвестное поле не удовлетворяет требованию; сорта должны присутствовать все."""
    if hard.color and normalize(wine.color or "") != normalize(hard.color):
        return False
    if hard.sweetness and normalize(wine.sweetness or "") != normalize(hard.sweetness):
        return False
    if hard.grapes and not {normalize(v) for v in hard.grapes}.issubset(
        {normalize(v) for v in wine.grapes}
    ):
        return False
    if hard.regions and normalize(wine.region or "") not in {normalize(v) for v in hard.regions}:
        return False
    if hard.producer and normalize(wine.producer or "") != normalize(hard.producer):
        return False
    if hard.source_categories and normalize(wine.source_category or "") not in {
        normalize(v) for v in hard.source_categories
    }:
        return False
    return True


@dataclass(frozen=True)
class RankedWine:
    """Промежуточный результат не содержит фактов, созданных моделью."""

    wine_id: int
    score: Decimal
    breakdown: list[ScoreComponent]


def has_meaningful_relevance(candidate: RankedWine) -> bool:
    """Допускать совпадение с запросом, но не рейтинг или сам факт наличия в каталоге."""
    return any(
        part.key == "constraints"
        or (part.score > 0 and part.key.startswith(("food:", "taste:", "soft:")))
        for part in candidate.breakdown
    )


def score_wine(
    wine: Wine, intent: UserIntent, profile: PreferenceProfile | None = None
) -> RankedWine:
    """Начислить баллы только за совпадения с полями или описанием источника."""
    parts: list[ScoreComponent] = []

    def add(key: str, weight: str, reason: str) -> None:
        parts.append(ScoreComponent(key=key, score=WEIGHTS[weight], reason=reason))

    food_values = {normalize(v): v for v in wine.food_pairings}
    for index, food in enumerate(dict.fromkeys(map(normalize, intent.normalized_food_categories))):
        if food in food_values:
            add(
                f"food:{index}",
                "food",
                f"К вашему блюду подходит указанное в каталоге сочетание: «{food_values[food]}».",
            )
    for index, taste in enumerate(dict.fromkeys(intent.taste_preferences)):
        if evidence := taste_evidence(wine.description, taste):
            add(
                f"taste:{index}",
                "taste",
                f"К пожеланию «{taste}» найдено упоминание в описании: «{evidence}».",
            )
    for index, negative_taste in enumerate(dict.fromkeys(intent.negative_taste_preferences)):
        evidence = negative_taste_evidence(wine.description, negative_taste)
        parts.append(
            ScoreComponent(
                key=f"negative_taste:{index}",
                score=WEIGHTS["negative_taste"] if evidence else Decimal(0),
                reason=(
                    f"Вы просили избегать свойства «{negative_taste}». В описании указано: «{evidence}». Балл снижен."
                    if evidence
                    else f"Каталог не даёт достаточных сведений, чтобы подтвердить отсутствие нежелательного свойства «{negative_taste}»."
                ),
            )
        )

    soft = intent.soft_preferences
    for field, weight, label in (
        ("color", "color", "Предпочитаемый цвет"),
        ("sweetness", "sweetness", "Предпочитаемая категория сладости"),
        ("producer", "producer", "Предпочитаемый производитель"),
    ):
        value = getattr(soft, field)
        actual = getattr(wine, field)
        if value and actual and normalize(value) == normalize(actual):
            add(f"soft:{field}", weight, f"{label}: {actual}.")
    for field, actual, weight, label in (
        ("grapes", wine.grapes, "grape", "Предпочитаемый сорт винограда"),
        ("regions", [wine.region] if wine.region else [], "region", "Предпочитаемый регион"),
        (
            "source_categories",
            [wine.source_category] if wine.source_category else [],
            "category",
            "Предпочитаемая категория",
        ),
    ):
        preferences = {normalize(v) for v in getattr(soft, field)}
        for index, value in enumerate(actual):
            if normalize(value) in preferences:
                add(f"soft:{field}:{index}", weight, f"{label}: {value}.")
    hard = intent.hard_constraints
    if matches_constraints(wine, hard) and any(
        (
            hard.color,
            hard.sweetness,
            hard.grapes,
            hard.regions,
            hard.producer,
            hard.source_categories,
        )
    ):
        parts.append(
            ScoreComponent(
                key="constraints",
                score=Decimal(0),
                reason="Карточка соответствует указанным вами обязательным ограничениям.",
            )
        )
    if wine.rating is not None:
        parts.append(
            ScoreComponent(
                key="rating",
                score=wine.rating * WEIGHTS["rating"],
                reason=f"Народный рейтинг в каталоге: {wine.rating} из 5.",
            )
        )
    if not parts:
        parts.append(
            ScoreComponent(
                key="catalog",
                score=Decimal(0),
                reason="Это реальная позиция локального каталога; подтверждённых совпадений с дополнительными пожеланиями нет.",
            )
        )
    if profile is not None:
        parts.extend(profile.components(wine))
    return RankedWine(wine.id, sum((p.score for p in parts), Decimal(0)), parts)


def rank_wines(
    wines: list[Wine], intent: UserIntent, profile: PreferenceProfile | None = None
) -> list[RankedWine]:
    """При равенстве баллов порядок определяется постоянным локальным id."""
    ranked = [score_wine(wine, intent, profile) for wine in wines]
    return sorted(ranked, key=lambda item: (-item.score, item.wine_id))
